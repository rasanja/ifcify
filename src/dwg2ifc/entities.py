"""Decodes DWG objects into plain dicts (spec chapter 20.4)."""

import math
import re
import struct
from collections import Counter, defaultdict

from dwg2ifc import acis, datastore, dwg, header, proxy
from dwg2ifc.dwg import since


class Drawing:
    def __init__(self) -> None:
        self.release = ""
        self.units = 0  # INSUNITS code; 0 means the drawing declares none
        self.metric = False  # MEASUREMENT: fallback when the drawing declares no units
        self.layers: dict[int, dict] = {}  # handle -> {name, color, lineweight, off, frozen, locked}
        self.blocks: dict[int, dict] = {}  # block header handle -> {name, base, units}
        self.fields: dict[int, list] = {}  # object handle -> [(property, value as displayed, value as a number)]
        self.linetypes: dict[int, dict] = {}  # handle -> {name, dashes}; a dash is a length, negative for a gap
        self.apps: dict[int, str] = {}  # registered application handle -> name, for xdata
        self.dimstyles: dict[int, dict] = {}  # handle -> {scale, arrow, text}
        self.spaces: dict[str, int] = {}  # "model" and "paper" -> block header handle, from the block table
        self.dictionaries: dict[int, dict] = {}  # handle -> {entry name: handle of the object it owns}
        self.xrecords: dict[int, list] = {}  # handle -> [(DXF group code, value)]
        self.groups: dict[int, dict] = {}  # handle -> {description, members}; its name is its key in a dictionary
        self.styles: dict[int, dict] = {}  # text style handle -> {name, font, width, oblique}
        self.materials: dict[int, str] = {}  # material handle -> name
        self.geodata: dict[int, dict] = {}  # at most one: where on Earth the drawing sits (see _geodata)
        self.images: dict[int, str] = {}  # image definition handle -> path of the picture file
        self.histories: dict[int, int] = {}  # solid history handle -> handle of its graph of modelling steps
        self.steps: dict[int, dict] = {}  # modelling step handle -> {owner (the graph), kind, sizes}
        self.entities: list[dict] = []  # each has type, handle, owner (block header handle), layer
        self.skipped: Counter[str] = Counter()  # entity types we could not convert

    def block_named(self, name: str) -> int | None:
        return next((h for h, b in self.blocks.items() if b["name"].lower() == name.lower()), None)


def bspline_points(degree: int, knots: list, control: list, weights: list, per_span: int = 8) -> list:
    """Points along a (rational) B-spline, by de Boor's algorithm."""
    n = len(control)
    if degree < 1 or n <= degree or len(knots) < n + degree + 1:
        return list(control)
    weights = weights if len(weights) == n else [1.0] * n
    homogeneous = [(*(c * w for c in p), w) for p, w in zip(control, weights)]

    def at(span, u):
        d = homogeneous[span - degree : span + 1]
        for level in range(1, degree + 1):
            for j in range(degree, level - 1, -1):
                i = span - degree + j
                width = knots[i + degree - level + 1] - knots[i]
                a = (u - knots[i]) / width if width else 0.0
                d[j] = tuple((1 - a) * p + a * q for p, q in zip(d[j - 1], d[j]))
        w = d[degree][-1] or 1.0
        return tuple(c / w for c in d[degree][:-1])

    out, last = [], None
    for span in range(degree, n):
        u0, u1 = knots[span], knots[span + 1]
        if u1 > u0:
            out += [at(span, u0 + (u1 - u0) * s / per_span) for s in range(per_span)]
            last = (span, u1)
    if last:
        out.append(at(*last))
    return out or list(control)


def fit_spline(fit: list, parameter: int = 0) -> tuple[list, list]:
    """Knots and control points of the cubic B-spline AutoCAD draws through fit points.

    Natural end conditions (no curvature at the ends). `parameter` is the knot spacing:
    0 chord length, 1 square root of it, 2 uniform.
    """
    points = [fit[0]] + [q for p, q in zip(fit, fit[1:]) if p != q]
    n = len(points) - 1
    if n < 2:
        return [], []
    power = {1: 0.5, 2: 0.0}.get(parameter, 1.0)
    u = [0.0]
    for p, q in zip(points, points[1:]):
        u.append(u[-1] + math.dist(p, q) ** power)
    knots = [u[0]] * 3 + u + [u[-1]] * 3

    def basis(i, degree, x):
        if degree == 0:
            return 1.0 if knots[i] <= x < knots[i + 1] else 0.0
        left = knots[i + degree] - knots[i]
        right = knots[i + degree + 1] - knots[i + 1]
        return (
            ((x - knots[i]) / left * basis(i, degree - 1, x) if left else 0.0)
            + ((knots[i + degree + 1] - x) / right * basis(i + 1, degree - 1, x) if right else 0.0)
        )

    # one row per control point, each touching only its neighbours: lower, diagonal, upper, right-hand side
    zero = tuple(0.0 for _ in points[0])
    rows = [(0.0, 1.0, 0.0, points[0])]
    a, b = 1 / (u[1] - u[0]), 1 / (u[2] - u[0])
    rows.append((-a, a + b, -b, zero))  # no curvature at the start
    for k in range(1, n):
        rows.append((basis(k, 3, u[k]), basis(k + 1, 3, u[k]), basis(k + 2, 3, u[k]), points[k]))
    a, b = 1 / (u[n] - u[n - 2]), 1 / (u[n] - u[n - 1])
    rows.append((-a, a + b, -b, zero))  # no curvature at the end
    rows.append((0.0, 1.0, 0.0, points[n]))

    # Thomas algorithm
    diagonal, upper, rhs = [rows[0][1]], [rows[0][2]], [rows[0][3]]
    for lower, d, up, value in rows[1:]:
        m = lower / diagonal[-1]
        diagonal.append(d - m * upper[-1])
        rhs.append(tuple(v - m * w for v, w in zip(value, rhs[-1])))
        upper.append(up)
    control = [tuple(c / diagonal[-1] for c in rhs[-1])]
    for i in range(len(rows) - 2, -1, -1):
        control.append(tuple((c - upper[i] * nxt) / diagonal[i] for c, nxt in zip(rhs[i], control[-1])))
    return knots, control[::-1]


def _object_common(o: dwg.Obj) -> int:
    r = o.r
    if not since(o.release, "R2000"):
        o.set_size(r.rl())
    reactors = r.bl()
    has_dictionary = not (since(o.release, "R2004") and r.b())
    if since(o.release, "R2013"):
        r.b()  # has data-store data
    return reactors + has_dictionary  # handles between the owner and the object's own handles


def _table_name(o: dwg.Obj, with_handle_count: bool = False):
    """Start of every table record (LAYER, BLOCK_HEADER, ...): its name and xref flags."""
    skip = _object_common(o)
    name = o.text()
    if since(o.release, "R2007"):
        o.r.bs()  # xref index, with the 64-flag and xdep bits folded in
    else:
        o.r.b(), o.r.bs(), o.r.b()  # 64-flag, xref index, xdep
    return (name, skip) if with_handle_count else name


def _true_color(word: int):
    """A 32-bit colour value: its top byte says whether the rest is red-green-blue or a palette index."""
    method = word >> 24
    if method == 0xC2:
        return ((word >> 16) & 0xFF, (word >> 8) & 0xFF, word & 0xFF)
    if method == 0xC3:
        return word & 0xFF
    return None


def _layer(o: dwg.Obj) -> dict:
    """Colours are a palette index (1-255) or an (r, g, b) tuple, here and on entities."""
    name, skip = _table_name(o, True)
    r = o.r
    if since(o.release, "R2000"):
        flags = r.bs()
        frozen, off, locked, lineweight = bool(flags & 1), bool(flags & 2), bool(flags & 8), (flags >> 5) & 0x1F
    else:
        # the second bit means off, though the spec calls it "on" (checked against a layer AutoCAD turned off)
        frozen, off, _, locked, lineweight = bool(r.b()), bool(r.b()), r.b(), bool(r.b()), 31
    if since(o.release, "R2004"):
        r.bs()
        color = _true_color(r.bl())
        names = r.rc()
        if not o.unicode:
            for bit in (1, 2):
                if names & bit:
                    o.text()
    else:
        color = r.bs()
        if color > 0x7FFF:  # stored negative when the layer is off
            color, off = 0x10000 - color, True
    # handles: owner, reactors and dictionary, xref block, plot style (R2000+), material (R2007+), then the linetype
    for _ in range(2 + skip + since(o.release, "R2000")):
        o.handles.h(o.handle)
    material = o.handles.h(o.handle) if since(o.release, "R2007") else 0
    return {"name": name, "color": color or 7, "lineweight": lineweight, "off": off, "frozen": frozen, "locked": locked,
            "material": material, "linetype": o.handles.h(o.handle)}  # fmt: skip


def _field(o: dwg.Obj) -> list:
    """A field that reports a property of other objects, such as a polyline's area.

    AutoCAD stores the result it last computed next to the formula, so the value is the drawing's own.
    Returns [(object handle, property, value as text, value as a number or None)].
    Layout from AutoCAD's own reader (AcDbImpField and AcImpValue ::dwgInFields), not from the spec.
    """
    skip = _object_common(o)
    if not o.unicode or o.strings is None:
        return []  # shortcut: before R2007 the strings are inline and a format string is added; not stepped through
    r, text = o.r, o.strings.tu
    evaluator, code = text(), text()
    if evaluator != "AcObjProp":
        return []  # other evaluators (dates, file names, formulas) do not describe an object
    named = re.search(r"\)\.(\w+)", code)
    children, objects = r.bl(), r.bl()
    for _ in range(5):
        r.bl()  # evaluation option, filing option, field state, evaluation status, error code
    text()  # error message
    # the cached value: flags, data type, the value itself, a unit type, then its format and its text
    flags, kind = r.bl(), r.bl()
    number = None
    if not flags & 1:
        if kind == 2:
            number = r.bd()
        elif kind in (0, 1):
            number = float(r.bl())
        else:
            return []  # text, dates, points and handles: not needed for a measured property
    r.bl()
    text(), text()
    value = text()  # the field's result as displayed, e.g. "405 SF"
    if not named:
        return []
    for _ in range(1 + skip + children):
        o.handles.h(o.handle)
    return [(o.handles.h(o.handle), named.group(1), value, number) for _ in range(min(objects, 16))]


def _linetype(o: dwg.Obj) -> dict:
    name = _table_name(o)
    r = o.r
    o.text()  # description
    r.bd(), r.rc()  # pattern length, alignment
    dashes = []
    for _ in range(r.rc()):
        dashes.append(r.bd())
        r.bs(), r.rd(), r.rd(), r.bd(), r.bd(), r.bs()  # shape or text carried by the dash
    return {"name": name, "dashes": dashes}


def _dictionary(o: dwg.Obj) -> dict:
    """Entry name -> handle of the object the dictionary owns (AcDbImpDictionary::dwgInFields)."""
    skip = _object_common(o)
    r = o.r
    count = r.bl()
    if since(o.release, "R2000"):
        r.bs()  # what to do with a duplicate name when drawings are merged
    if since(o.release, "R14"):
        r.rc()  # whether it owns its entries
    names = [o.text() for _ in range(count)]
    for _ in range(1 + skip):
        o.handles.h(o.handle)
    return {name: o.handles.h(o.handle) for name in names}


# How a record's value is stored, by DXF group code: AutoCAD's own table (acdbGroupCodeToType in libacdb).
_VALUE_TYPES = (
    ("text", "0-9 100-102 105 300-309 320-329 410-419 430-439 470-479 999-1003 1005"),
    ("point", "10-17 110-112 210-219 1010-1013"),
    ("real", "38-59 140-149 460-469 1040-1042"),
    ("int16", "60-79 170-179 270-279 370-389 400-409 1070"),
    ("int32", "90-99 420-429 440-459 1071"),
    ("int64", "160-169"),
    ("int8", "280-299"),
    ("chunk", "310-319 1004"),
    ("handle", "330-369 390-399"),
)
_VALUE_TYPE = {
    code: kind
    for kind, ranges in _VALUE_TYPES
    for first, _, last in (span.partition("-") for span in ranges.split())
    for code in range(int(first), int(last or first) + 1)
}
_FIXED_VALUES = {"real": "<d", "int16": "<h", "int32": "<i", "int64": "<q", "int8": "<b", "point": "<3d"}


def _values(raw: bytes, unicode: bool) -> list:
    """The (group code, value) pairs of an XRECORD (AcDbImpXrecord::stepPosition gives the sizes)."""
    out, i = [], 0
    try:
        while i < len(raw):
            code = struct.unpack_from("<h", raw, i)[0]
            kind = _VALUE_TYPE.get(code)
            i += 2
            if kind == "text":
                n = struct.unpack_from("<H", raw, i)[0]
                if unicode:
                    value, i = raw[i + 2 : i + 2 + 2 * n].decode("utf-16-le", "replace"), i + 2 + 2 * n
                else:  # shortcut: decoded as cp1252, use the code page byte for other scripts
                    value, i = raw[i + 3 : i + 3 + n].decode("cp1252", "replace"), i + 3 + n
            elif kind == "chunk":
                value, i = raw[i + 1 : i + 1 + raw[i]].hex(), i + 1 + raw[i]
            elif kind == "handle":
                value, i = int.from_bytes(raw[i : i + 8], "little"), i + 8
            elif kind:
                value = struct.unpack_from(_FIXED_VALUES[kind], raw, i)
                value, i = value if kind == "point" else value[0], i + struct.calcsize(_FIXED_VALUES[kind])
            else:
                break  # AutoCAD stops at a code it has no type for, too
            out.append((code, value))
    except (IndexError, struct.error):
        pass
    return out


def _xrecord(o: dwg.Obj) -> list:
    """Free-form data another object or program attached: a list of typed values."""
    _object_common(o)
    r = o.r
    raw = r.raw(r.bl())
    if since(o.release, "R2000"):
        r.bs()  # duplicate handling, as in a dictionary
    return _values(raw, o.unicode)


def _group(o: dwg.Obj) -> dict:
    """A named selection of entities (AcDbImpGroup::dwgInFields)."""
    skip = _object_common(o)
    r = o.r
    description = o.text()
    r.bs(), r.bs()  # unnamed, selectable
    count = r.bl()
    for _ in range(1 + skip):
        o.handles.h(o.handle)
    return {"description": description, "members": [o.handles.h(o.handle) for _ in range(count)]}


def _style(o: dwg.Obj) -> dict:
    """A text style: the font and the proportions text is drawn with."""
    name = _table_name(o)
    r = o.r
    r.b(), r.b()  # shape file, vertical
    r.bd()  # fixed height
    width, oblique = r.bd(), r.bd()
    r.rc(), r.bd()  # mirroring, last height used
    font = o.text()
    o.text()  # big font, for Asian scripts
    # a TrueType font is named in the style's xdata; load() picks AutoCAD's own entry once application names are known
    faces = [(app, next((v for code, v in xdata(raw, o.unicode) if code == 1000 and v), "")) for app, raw in o.eed]
    return {"name": name, "font": font, "faces": faces, "width": width, "oblique": oblique}


def _colour(o: dwg.Obj) -> None:
    """Steps over a colour stored inside an object (AcCmColor::dwgIn)."""
    r = o.r
    r.bs()
    if since(o.release, "R2004"):
        r.bl()
        names = r.rc()
        for bit in (1, 2):
            if names & bit:
                o.text()


def _material(o: dwg.Obj) -> str:
    """A material's name; its colours and texture maps are rendering settings and are not read."""
    _object_common(o)
    name = o.text()
    o.r.p = o.end
    return name


def _geodata(o: dwg.Obj) -> dict:
    """GEODATA (AcDbImpGeoData::dwgInFields): the drawing point that sits at a known point of a coordinate system."""
    _object_common(o)
    r = o.r
    if r.bl() < 2:
        raise ValueError("geographic location in the AutoCAD 2009 layout")  # shortcut: only the layout since 2010 is read
    kind = {1: "local grid", 2: "projected grid", 3: "geographic"}.get(r.bl(), "unknown")
    geo = {"kind": kind, "design": r.bd3(), "reference": r.bd3(), "scale": r.bd()}
    r.bl(), r.bd(), r.bl()  # horizontal units, vertical scale and units
    r.bd3()  # up direction
    geo["north"] = r.rd2()
    r.bl(), r.bd(), r.b(), r.bd(), r.bd()  # scale estimate and sea-level correction
    geo["system"] = o.text()  # the coordinate system, described in XML
    r.p = o.end  # observation notes and the mesh used to warp map images
    return geo


def _image_file(o: dwg.Obj) -> str:
    """IMAGEDEF: the picture file an IMAGE entity shows. The drawing holds the path, not the picture."""
    _object_common(o)
    o.r.bl(), o.r.rd2()  # class version, size in pixels
    path = o.text()
    o.r.b(), o.r.rc(), o.r.rd2()  # loaded, resolution unit, size of a pixel
    return path


def _history(o: dwg.Obj) -> int:
    """A solid's modelling history (AcDbImpShHistory::dwgInFields): it owns a graph of steps."""
    skip = _object_common(o)
    r = o.r
    r.bl(), r.bl(), r.bl(), r.b(), r.b()  # version, record-history and show-history settings
    for _ in range(1 + skip):
        o.handles.h(o.handle)
    return o.handles.h(o.handle)


# the sizes a primitive solid is created from, in file order (AcDbImpSh<Kind>::dwgInFields)
_PRIMITIVES = {
    "ACSH_BOX_CLASS": ("Length", "Width", "Height"),
    "ACSH_WEDGE_CLASS": ("Length", "Width", "Height"),
    "ACSH_SPHERE_CLASS": ("Radius",),
    "ACSH_CYLINDER_CLASS": ("Height", "BaseRadius", "BaseMinorRadius", "TopRadius"),
    "ACSH_CONE_CLASS": ("Height", "BaseRadius", "BaseMinorRadius", "TopRadius"),
    "ACSH_TORUS_CLASS": ("MajorRadius", "MinorRadius"),
}


def _step(o: dwg.Obj) -> dict:
    """One step of a solid's history. A primitive keeps the sizes the user gave it, so they need not be measured."""
    skip = _object_common(o)
    r = o.r
    step = {"owner": o.handles.h(o.handle), "kind": o.type[5:-6].title(), "sizes": {}}
    if o.type == "ACSH_SUBENT_MATERIAL_CLASS":  # a material given to some faces: the step's one handle of its own
        for _ in range(skip):
            o.handles.h(o.handle)
        step["material"] = o.handles.h(o.handle)
    if o.type not in _PRIMITIVES:
        r.p = o.end  # fillets, booleans, sweeps: counted as steps, their settings are not read
        return step
    if r.bl() == 0xFFFFFFFF:  # AcDbEvalExpr: a newer layout carries a version and a last value
        r.bl(), r.bl()
        kind = _VALUE_TYPE.get(struct.unpack("<h", r.bs().to_bytes(2, "little"))[0])
        {"real": r.bd, "int32": r.bl, "int16": r.bs, "int8": r.bs, "point": r.bd3, "text": o.text}.get(kind, int)()
        r.bl()
    r.bl(), r.bl()  # AcDbShHistoryNode: version
    for _ in range(16):
        r.bd()  # where the primitive sits, as a 4x4 matrix
    _colour(o)
    r.bl()  # step number
    r.bl(), r.bl()  # the primitive's own version
    step["sizes"] = {name: r.bd() for name in _PRIMITIVES[o.type]}
    return step


def _block_control(o: dwg.Obj) -> dict:
    """The block table: its last two handles name model space and the active paper space."""
    skip = _object_common(o)
    count = o.r.bl()
    hr = o.handles
    for _ in range(1 + skip + count):  # owner, reactors and dictionary, then one per block
        hr.h(o.handle)
    return {"model": hr.h(o.handle), "paper": hr.h(o.handle)}


def _block_header(o: dwg.Obj) -> dict:
    name = _table_name(o)
    r = o.r
    r.b(), r.b()  # anonymous, has attribs
    is_xref, overlaid = r.b(), r.b()
    if since(o.release, "R2000"):
        r.b()  # loaded
    if since(o.release, "R2004") and not is_xref and not overlaid:
        r.bl()  # owned object count
    base = r.bd3()
    path = o.text()
    xref = path if is_xref or overlaid else ""  # the file an external reference block stands for
    layout = 0
    if since(o.release, "R2000"):
        while r.rc():  # one non-zero byte per insert, zero-terminated
            pass
        o.text()  # description
        preview_size = r.bl()
        r.p += preview_size * 8
        while o.handles.p + 8 <= o.stop:  # the layout this block is the sheet of comes last
            layout = o.handles.h(o.handle)
    return {"name": name, "base": base, "xref": xref, "layout": layout, "units": r.bs() if since(o.release, "R2007") else 0}


def _entity_common(o: dwg.Obj) -> dict:
    """Common entity data and handles (20.4.1, 20.4.2). Leaves o.handles at the entity's own handles."""
    r, release = o.r, o.release
    graphics = b""
    if r.b():  # proxy graphics: a drawing of the entity for programs that do not know its type
        graphics = r.raw(r.bll() if since(release, "R2010") else r.rl())
    if not since(release, "R2000"):
        o.set_size(r.rl())
    hr = o.handles
    entmode = r.bb()
    reactors = r.bl()
    xdic_missing = since(release, "R2004") and r.b()
    if since(release, "R2013"):
        r.b()  # has data-store data
    color_flags, no_links = 0, True
    ltype = plotstyle = material = visual_styles = 0
    if not since(release, "R2000"):
        ltype = 0 if r.b() else 3  # "by layer" bit: when clear, a linetype handle follows
    if since(release, "R2004"):
        color = r.bs()
        color_flags, color = color >> 8, color & 0x1FF
        if color_flags & 0x80:
            color = _true_color(r.bl())
        if color_flags & 0x20:
            r.bl()  # transparency
    else:
        no_links = r.b()
        color = r.bs()
    color = None if color in (None, 256) else color  # None: by layer. 0: by block
    r.bd()  # linetype scale
    if since(release, "R2000"):
        ltype, plotstyle = r.bb(), r.bb()
    if since(release, "R2007"):
        material = r.bb()
        r.rc()  # shadow flags
    if since(release, "R2010"):
        visual_styles = r.b() + r.b() + r.b()
    invisible = r.bs() & 1
    lineweight = r.rc() if since(release, "R2000") else 29  # 29: by layer

    e = {"type": o.type, "handle": o.handle, "invisible": invisible, "graphics": graphics, "color": color, "lineweight": lineweight}
    if o.eed:
        e["xdata"] = [(app, xdata(raw, o.unicode)) for app, raw in o.eed]
    e["owner"] = hr.h(o.handle) if entmode == 0 else {1: "paper", 2: "model"}.get(entmode)
    for _ in range(reactors):
        hr.h()
    if not xdic_missing:
        e["dictionary"] = hr.h(o.handle)  # records other programs attached to this entity
    links = not since(release, "R2004")  # older files chain entities; sub-entity lists (vertices) rely on it
    if links:
        e["next"] = o.handle + 1
    e["linetype"] = {0: None, 1: "ByBlock", 2: "Continuous"}.get(ltype)  # None: by layer; a handle when set below
    if not since(release, "R2000"):
        e["layer"] = hr.h(o.handle)
        if ltype == 3:
            e["linetype"] = hr.h(o.handle)
    if links and not no_links:
        hr.h(o.handle)
        e["next"] = hr.h(o.handle)
    if color_flags & 0x40:
        hr.h()  # colour book reference
    if since(release, "R2000"):
        e["layer"] = hr.h(o.handle)
        if ltype == 3:
            e["linetype"] = hr.h(o.handle)
        if material == 3:
            e["material"] = hr.h(o.handle)  # otherwise the layer's
        if plotstyle == 3:
            hr.h()
    for _ in range(visual_styles):
        hr.h()
    return e


def _thickness(o) -> float:
    """How far a flat entity is extruded upwards, which is how some drawings record a height."""
    o.thickness = o.r.bt() if since(o.release, "R2000") else o.r.bd()
    return o.thickness


def _extrusion(o) -> tuple:
    return o.r.be() if since(o.release, "R2000") else o.r.bd3()


def _owned(o: dwg.Obj) -> dict:
    """Sub-entity links of a POLYLINE: a handle list from R2004 on, a first/last pair before."""
    if since(o.release, "R2004"):
        count = o.r.bl()
        return {"owned": [o.handles.h(o.handle) for _ in range(count)]}
    return {"first": o.handles.h(o.handle), "last": o.handles.h(o.handle)}


def _line(o, r):
    if not since(o.release, "R2000"):
        e = {"start": r.bd3(), "end": r.bd3()}
        _thickness(o)
        return e | {"extrusion": r.bd3()}
    z_is_zero = r.b()
    x1 = r.rd()
    x2 = r.dd(x1)
    y1 = r.rd()
    y2 = r.dd(y1)
    z1 = z2 = 0.0
    if not z_is_zero:
        z1 = r.rd()
        z2 = r.dd(z1)
    _thickness(o)
    return {"start": (x1, y1, z1), "end": (x2, y2, z2), "extrusion": r.be()}


def _circle(o, r):
    e = {"center": r.bd3(), "radius": r.bd()}
    _thickness(o)
    e["extrusion"] = _extrusion(o)
    return e


def _arc(o, r):
    return _circle(o, r) | {"start_angle": r.bd(), "end_angle": r.bd()}


def _point(o, r):
    e = {"point": r.bd3()}
    _thickness(o)
    e["extrusion"] = _extrusion(o)
    r.bd()  # x axis angle
    return e


def _ellipse(o, r):
    return {"center": r.bd3(), "major": r.bd3(), "extrusion": r.bd3(), "ratio": r.bd(), "start_angle": r.bd(), "end_angle": r.bd()}


def _lwpolyline(o, r):
    flag = r.bs()
    width = r.bd() if flag & 4 else 0.0  # one width for the whole polyline
    elevation = r.bd() if flag & 8 else 0.0
    if flag & 2:
        o.thickness = r.bd()
    extrusion = r.bd3() if flag & 1 else (0.0, 0.0, 1.0)
    n = r.bl()
    n_bulges = r.bl() if flag & 16 else 0
    n_ids = r.bl() if flag & 1024 and since(o.release, "R2010") else 0
    n_widths = r.bl() if flag & 32 else 0
    if since(o.release, "R2000"):
        points = [r.rd2()] if n else []
        for _ in range(n - 1):
            x = r.dd(points[-1][0])
            points.append((x, r.dd(points[-1][1])))
    else:
        points = [r.rd2() for _ in range(n)]
    bulges = [r.bd() for _ in range(n_bulges)]
    for _ in range(n_ids):
        r.bl()
    widths = {(r.bd(), r.bd()) for _ in range(n_widths)}
    if len(widths) == 1 and len(set(*widths)) == 1:
        width = widths.pop()[0]  # every vertex has the same start and end width
    return {"points": points, "bulges": bulges, "closed": bool(flag & 512), "elevation": elevation, "extrusion": extrusion,
            "width": width}  # fmt: skip


def _polyline_2d(o, r):
    flags = r.bs()
    r.bs()  # curve type
    start_width, end_width = r.bd(), r.bd()
    _thickness(o)
    return {"closed": bool(flags & 1), "elevation": r.bd(), "extrusion": _extrusion(o),
            "width": start_width if start_width == end_width else 0.0} | _owned(o)  # fmt: skip


def _polyline_3d(o, r):
    r.rc()  # spline flags
    return {"closed": bool(r.rc() & 1)} | _owned(o)


def _polyline_pface(o, r):
    r.bs(), r.bs()  # vertex and face counts
    return _owned(o)


def _polyline_mesh(o, r):
    flags = r.bs()
    r.bs()  # curve type
    e = {"m": r.bs(), "n": r.bs(), "closed_m": bool(flags & 1), "closed_n": bool(flags & 32)}
    r.bs(), r.bs()  # densities
    return e | _owned(o)


def _vertex_2d(o, r):
    e = {"flags": r.rc(), "point": r.bd3()}
    if r.bd() >= 0:  # start width; a negative one stands for both widths
        r.bd()
    e["bulge"] = r.bd()
    if since(o.release, "R2010"):
        r.bl()  # vertex id
    r.bd()  # tangent direction
    return e


def _vertex_3d(o, r):
    return {"flags": r.rc(), "point": r.bd3(), "bulge": 0.0}


def _vertex_pface_face(o, r):
    return {"indices": [r.bs(), r.bs(), r.bs(), r.bs()]}


def _insert(o, r):
    point = r.bd3()
    flags = r.bb() if since(o.release, "R2000") else None
    if flags is None:
        scale = r.bd3()
    elif flags == 3:
        scale = (1.0, 1.0, 1.0)
    elif flags == 1:
        scale = (1.0, r.dd(1.0), r.dd(1.0))
    elif flags == 2:
        x = r.rd()
        scale = (x, x, x)
    else:
        x = r.rd()
        scale = (x, r.dd(x), r.dd(x))
    e = {"point": point, "scale": scale, "rotation": r.bd(), "extrusion": r.bd3()}
    if r.b() and since(o.release, "R2004"):  # has attribs
        r.bl()
    if o.type == "MINSERT":
        e |= {"columns": r.bs(), "rows": r.bs(), "column_spacing": r.bd(), "row_spacing": r.bd()}
    e["block"] = o.handles.h(o.handle)
    return e


# definition points after the common dimension data, by kind: DXF group -> order in the file
_DIMENSION_POINTS = {
    "DIMENSION_ORDINATE": (10, 13, 14),
    "DIMENSION_LINEAR": (13, 14, 10),
    "DIMENSION_ALIGNED": (13, 14, 10),
    "DIMENSION_ANG3PT": (10, 13, 14, 15),
    "DIMENSION_ANG2LN": (13, 14, 15, 10),
    "DIMENSION_RADIUS": (10, 15),
    "DIMENSION_DIAMETER": (15, 10),
}


def _dimension(o, r):
    """Every DIMENSION kind. AutoCAD usually keeps the drawn dimension in an anonymous block, which is then all
    we place; the definition points are for drawings saved without that block."""
    release = o.release
    if since(release, "R2010"):
        r.rc()  # version
    extrusion = r.bd3()
    text_point = r.rd2()
    elevation = r.bd()
    r.rc()  # flags
    text = o.text()  # what the user typed over the measurement, if anything
    r.bd(), r.bd()  # text rotation, horizontal direction
    scale = tuple(s or 1.0 for s in r.bd3())
    rotation = r.bd()
    measurement = None
    if since(release, "R2000"):
        r.bs(), r.bs(), r.bd()  # attachment, line spacing style and factor
        measurement = r.bd()
    if since(release, "R2007"):
        r.b(), r.b(), r.b()
    point = (*r.rd2(), elevation)
    e = {"point": point, "scale": scale, "rotation": rotation, "extrusion": extrusion, "text": text, "measurement": measurement,
         "text_point": (*text_point, elevation), "angle": 0.0}  # fmt: skip
    if o.type == "DIMENSION_ANG2LN":
        r.rd2()  # arc point
    e["defs"] = {group: r.bd3() for group in _DIMENSION_POINTS.get(o.type, ())}
    if o.type == "DIMENSION_ORDINATE":
        r.rc()  # x or y ordinate
    elif o.type in ("DIMENSION_LINEAR", "DIMENSION_ALIGNED"):
        r.bd()  # extension line slant
        if o.type == "DIMENSION_LINEAR":
            e["angle"] = r.bd()
    elif o.type in ("DIMENSION_RADIUS", "DIMENSION_DIAMETER"):
        r.bd()  # leader length
    else:
        r.p = o.end  # arc-length and jogged dimensions: their points are not read, the block shows them as drawn
    e["style"] = o.handles.h(o.handle)
    e["block"] = o.handles.h(o.handle)
    return e


def _ray(o, r):
    """RAY and XLINE: a point and a direction (AcDbImpRay and AcDbImpXline ::dwgInFields read exactly these)."""
    return {"point": r.bd3(), "direction": r.bd3()}


def _tolerance(o, r):
    if not since(o.release, "R2000"):
        r.bs(), r.bd(), r.bd()  # unknown, height, gap
    e = {"point": r.bd3(), "x_axis": r.bd3(), "extrusion": r.bd3(), "text": o.text()}
    e["style"] = o.handles.h(o.handle)
    return e


def _mline(o, r):
    """A multiline: several parallel lines. Each vertex gives, per line, how far along the miter direction it sits."""
    r.bd(), r.rc(), r.bd3()  # scale, justification, base point
    e = {"extrusion": r.bd3(), "closed": r.bs() == 3}
    lines, vertices = r.rc(), r.bs()
    e["elements"] = [[] for _ in range(lines)]
    for _ in range(vertices):
        vertex = r.bd3()
        r.bd3()  # direction to the next vertex
        miter = r.bd3()
        for element in e["elements"]:
            parameters = [r.bd() for _ in range(r.bs())]
            for _ in range(r.bs()):
                r.bd()  # area fill parameter
            offset = parameters[0] if parameters else 0.0
            element.append(tuple(v + offset * m for v, m in zip(vertex, miter)))
    return e


def _dimstyle(o: dwg.Obj) -> dict:
    """The three sizes needed to draw a dimension ourselves: overall scale, arrow size, text height."""
    _table_name(o)
    r = o.r
    o.text(), o.text()  # prefix/suffix
    scale, arrow = r.bd(), r.bd()
    for _ in range(7):
        r.bd()
    if since(o.release, "R2007"):
        r.bd(), r.bd(), r.bs(), r.bs(), r.bl(), r.rc()  # fixed extension length, jog angle, fill, fill colour
    for _ in range(6):
        r.b()
    r.bs(), r.bs(), r.bs()
    if since(o.release, "R2007"):
        r.bs()
    return {"scale": scale or 1.0, "arrow": arrow, "text": r.bd()}


def _text(o, r):
    if not since(o.release, "R2000"):
        elevation, point = r.bd(), r.rd2()
        r.rd2()  # alignment point
        extrusion = r.bd3()
        o.thickness = r.bd()
        r.bd()  # oblique angle
        rotation, height = r.bd(), r.bd()
        r.bd()  # width factor
        text = o.text()
        r.bs(), r.bs(), r.bs()  # generation, horizontal and vertical alignment
        return {"point": (*point, elevation), "rotation": rotation, "height": height, "extrusion": extrusion, "text": text,
                "style": o.handles.h(o.handle)}  # fmt: skip
    flags = r.rc()
    elevation = 0.0 if flags & 1 else r.rd()
    point = r.rd2()
    if not flags & 2:
        r.dd(point[0]), r.dd(point[1])  # alignment point
    extrusion = r.be()
    _thickness(o)
    if not flags & 4:
        r.rd()  # oblique angle
    rotation = 0.0 if flags & 8 else r.rd()
    height = r.rd()
    if not flags & 0x10:
        r.rd()  # width factor
    text = o.text()
    for bit in (0x20, 0x40, 0x80):
        if not flags & bit:
            r.bs()  # generation, horizontal and vertical alignment
    return {"point": (*point, elevation), "rotation": rotation, "height": height, "extrusion": extrusion, "text": text,
            "style": o.handles.h(o.handle)}  # fmt: skip


def _attrib(o, r):
    """A block attribute: text drawn with an insert, plus the tag that names it (door number, room name, ...)."""
    e = _text(o, r)
    multiline = False
    if since(o.release, "R2010"):
        r.rc()  # version
    if since(o.release, "R2018"):
        multiline = r.rc() != 1
    e["tag"] = ""
    if not multiline:  # shortcut: a multiline attribute embeds a whole MTEXT; its value is read but not its tag
        e["tag"] = o.text()
        r.bs()  # field length
        e["hidden"] = bool(r.rc() & 1)  # the "invisible" attribute flag: data only, not drawn
    o.r.p = o.end
    return e


def xdata(raw: bytes, unicode: bool) -> list:
    """Extended entity data items as (DXF group code, value) (spec chapter 28)."""
    out, i = [], 0
    try:
        while i < len(raw):
            code = raw[i]
            i += 1
            if code == 0:
                if unicode:
                    n = int.from_bytes(raw[i : i + 2], "little")
                    out.append((1000, raw[i + 2 : i + 2 + 2 * n].decode("utf-16-le", "replace")))
                    i += 2 + 2 * n
                else:
                    n = raw[i]
                    out.append((1000, raw[i + 3 : i + 3 + n].decode("cp1252", "replace")))
                    i += 3 + n
            elif code == 2:
                out.append((1002, "}" if raw[i] else "{"))
                i += 1
            elif code in (3, 5):
                out.append((1000 + code, f"{int.from_bytes(raw[i : i + 8], 'big'):X}"))
                i += 8
            elif code == 4:
                out.append((1004, raw[i + 1 : i + 1 + raw[i]].hex()))
                i += 1 + raw[i]
            elif 10 <= code <= 13:
                out.append((1000 + code, struct.unpack_from("<3d", raw, i)))
                i += 24
            elif 40 <= code <= 42:
                out.append((1000 + code, struct.unpack_from("<d", raw, i)[0]))
                i += 8
            elif code == 70:
                out.append((1070, struct.unpack_from("<h", raw, i)[0]))
                i += 2
            elif code == 71:
                out.append((1071, struct.unpack_from("<i", raw, i)[0]))
                i += 4
            else:
                break
    except (IndexError, struct.error):
        pass
    return out


def _mtext(o, r):
    e = {"point": r.bd3(), "extrusion": r.bd3(), "x_axis": r.bd3()}
    r.bd()  # rectangle width
    if since(o.release, "R2007"):
        r.bd()  # rectangle height
    e["height"] = r.bd()
    r.bs(), r.bs()  # attachment, drawing direction
    extent_height, extent_width = r.bd(), r.bd()
    e["extent"] = (extent_width, extent_height)  # the box AutoCAD measured around the text as drawn
    e["text"] = o.text()
    e["style"] = o.handles.h(o.handle)
    o.r.p = o.end  # the remaining fields are layout details we do not use
    return e


def _spline(o, r):
    scenario = r.bl()
    knot_parameter = 0
    if since(o.release, "R2013"):
        flags, knot_parameter = r.bl(), r.bl()
        if flags & 1:
            scenario = 2
        if knot_parameter == 15:
            scenario = 1
    e = {"degree": r.bl(), "closed": False, "knot_parameter": knot_parameter}
    n_knots = n_control = n_fit = 0
    weighted = False
    if scenario == 2:  # fit points only
        r.bd(), r.bd3(), r.bd3()  # fit tolerance, end tangents
        n_fit = r.bl()
    elif scenario == 1:
        r.b()  # rational
        e["closed"] = bool(r.b())
        r.b(), r.bd(), r.bd()  # periodic, knot and control tolerance
        n_knots, n_control = r.bl(), r.bl()
        weighted = r.b()
    else:
        raise ValueError(f"unknown spline scenario {scenario}")
    e["knots"] = [r.bd() for _ in range(n_knots)]
    e["control"], e["weights"] = [], []
    for _ in range(n_control):
        e["control"].append(r.bd3())
        if weighted:
            e["weights"].append(r.bd())
    e["fit"] = [r.bd3() for _ in range(n_fit)]
    return e


def _solid(o, r):  # SOLID and TRACE: a filled quadrilateral
    _thickness(o)
    return {"elevation": r.bd(), "corners": [r.rd2() for _ in range(4)], "extrusion": _extrusion(o)}


def _face3d(o, r):
    if not since(o.release, "R2000"):
        corners = [r.bd3() for _ in range(4)]
        r.bs()  # invisible edges
        return {"corners": corners}
    no_flags, z_is_zero = r.b(), r.b()
    corners = [(r.rd(), r.rd(), 0.0 if z_is_zero else r.rd())]
    for _ in range(3):
        x, y, z = corners[-1]
        corners.append((r.dd(x), r.dd(y), r.dd(z)))
    if not no_flags:
        r.bs()  # invisible edges
    return {"corners": corners}


def _leader(o, r):
    r.b(), r.bs(), r.bs()  # unknown, annotation type, path type
    e = {"points": [r.bd3() for _ in range(r.bl())]}
    r.p = o.end  # annotation box and arrow settings are not needed for the line
    return e


def _acis(o, r):
    """3DSOLID, REGION, BODY: an ACIS save file, then cached wireframes we do not need (20.4.41)."""
    empty = r.b()
    r.b()
    version = r.bs()
    data = b""
    if empty:
        pass  # R2013+ keeps the ACIS data in the AcDs data store instead
    elif version == 1:  # blocks of text with printable characters mirrored
        while size := r.bl():
            data += bytes(0x9F - c if 0x20 < c <= 0x7E else c for c in r.raw(size))
    else:  # an unframed file: binary ends at its end marker, text is cut by the parser
        data = r.raw((o.end - r.p) // 8)
    r.p = o.end
    # the handles left are the materials of its faces and, for a solid, its history; load() tells them apart
    links = []
    while since(o.release, "R2007") and o.handles.p + 8 <= o.stop:
        links.append(o.handles.h(o.handle))
    return {"bodies": acis.bodies(data) if data else None, "links": links}


def _arc_vertices(center, radius, start, sweep):
    """An arc as polyline vertices with bulges, excluding its end point. Split in two so no bulge is infinite."""
    half = sweep / 2
    bulge = math.tan(half / 4)
    return [((center[0] + radius * math.cos(a), center[1] + radius * math.sin(a)), bulge) for a in (start, start + half)]


def _hatch_path(r, release):
    """One boundary loop as flags, points, bulges, closed: the shape LWPOLYLINE uses."""
    flag = r.bl()
    vertices = []  # (point, bulge to the next vertex)
    closed = True
    if flag & 2:  # polyline
        has_bulges, closed = r.b(), bool(r.b())
        for _ in range(r.bl()):
            vertices.append((r.rd2(), r.bd() if has_bulges else 0.0))
    else:
        for _ in range(r.bl()):
            kind = r.rc()
            if kind == 1:  # line
                vertices.append((r.rd2(), 0.0))
                r.rd2()  # its end is the next edge's start
            elif kind == 2:  # circular arc
                center, radius, start, end, ccw = r.rd2(), r.bd(), r.bd(), r.bd(), r.b()
                sweep = (end - start) % (2 * math.pi) or 2 * math.pi
                # clockwise arcs store their angles mirrored
                vertices += _arc_vertices(center, radius, start if ccw else -start, sweep if ccw else -sweep)
            elif kind == 3:  # elliptical arc
                (cx, cy), (mx, my), ratio, start, end, ccw = r.rd2(), r.rd2(), r.bd(), r.bd(), r.bd(), r.b()
                sweep = (end - start) % (2 * math.pi) or 2 * math.pi
                if not ccw:
                    start, sweep = -start, -sweep
                steps = max(2, math.ceil(abs(sweep) / (math.pi / 16)))
                for i in range(steps):
                    a = start + sweep * i / steps
                    c, s = math.cos(a), math.sin(a) * ratio
                    vertices.append(((cx + mx * c - my * s, cy + my * c + mx * s), 0.0))
            elif kind == 4:  # spline
                degree, rational = r.bl(), r.b()
                r.b()  # periodic
                n_knots, n_control = r.bl(), r.bl()
                knots = [r.bd() for _ in range(n_knots)]
                control, weights = [], []
                for _ in range(n_control):
                    control.append(r.rd2())
                    if rational:
                        weights.append(r.bd())
                if since(release, "R2010"):
                    n_fit = r.bl()
                    for _ in range(n_fit):
                        r.rd2()
                    if n_fit:
                        r.rd2(), r.rd2()  # end tangents
                vertices += [(p, 0.0) for p in bspline_points(degree, knots, control, weights)[:-1]]
            else:
                raise ValueError(f"unknown hatch edge type {kind}")
    r.bl()  # number of boundary object handles
    return {"flags": flag, "points": [p for p, _ in vertices], "bulges": [b for _, b in vertices], "closed": closed}


def _hatch(o, r):
    release = o.release
    if since(release, "R2004"):
        r.bl(), r.bl(), r.bd(), r.bd(), r.bl(), r.bd()  # gradient settings
        for _ in range(r.bl()):
            r.bd(), r.bs(), r.bl(), r.rc()  # gradient colour
        o.text()  # gradient name
    e = {"elevation": r.bd(), "extrusion": r.bd3()}
    o.text()  # pattern name
    e["solid"] = bool(r.b())
    r.b()  # associative
    e["paths"] = [_hatch_path(r, release) for _ in range(r.bl())]
    r.bs(), r.bs()  # style, pattern type
    if not e["solid"]:
        r.bd(), r.bd(), r.b()  # angle, scale, double
        for _ in range(r.bs()):  # pattern definition lines
            r.bd(), r.bd(), r.bd(), r.bd(), r.bd()
            for _ in range(r.bs()):
                r.bd()  # dash length
    if any(p["flags"] & 4 for p in e["paths"]):
        r.bd()  # pixel size
    for _ in range(r.bl()):
        r.rd2()  # seed point
    return e


def _image(o, r):
    """IMAGE and WIPEOUT: a frame for a picture (or a blank mask), given as a clip outline in pixel coordinates."""
    r.bl()  # class version
    origin, u, v = r.bd3(), r.bd3(), r.bd3()  # lower-left corner, and the size of one pixel along each side
    _, tall = r.rd2()
    r.bs(), r.b(), r.rc(), r.rc(), r.rc()  # display flags, clipping on, brightness, contrast, fade
    if since(o.release, "R2010"):
        r.b()  # clip inverted
    if r.bs() == 1:  # a rectangle by two corners
        (x1, y1), (x2, y2) = r.rd2(), r.rd2()
        outline = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    else:
        outline = [r.rd2() for _ in range(r.bl())]
        outline = outline[:-1] if len(outline) > 1 and outline[0] == outline[-1] else outline
    # pixel rows count downwards from the top, and a pixel's centre is its coordinate
    points = [tuple(origin[i] + u[i] * (x + 0.5) + v[i] * (tall - y - 0.5) for i in range(3)) for x, y in outline]
    return {"points": points, "closed": True, "definition": o.handles.h(o.handle)}


def _helix(o, r):
    """A spline AutoCAD keeps in step with the radius, turns and pitch stored after it (AcDbImpHelix)."""
    e = _spline(o, r)
    r.bl(), r.bl()  # version
    r.bd3(), r.bd3(), r.bd3()  # axis base point, start point, axis direction
    e["stored"] = {"Radius": r.bd(), "Turns": r.bd(), "TurnHeight": r.bd()}
    r.b(), r.rc()  # handedness, which of the three sizes follows from the others
    return e


def _mesh(o, r):
    """MESH, a subdivision surface (AcDbImpSubDMesh): the cage of points and faces it is smoothed from."""
    r.bs(), r.b()  # version, blend creases
    level = r.bl()
    points = [r.bd3() for _ in range(r.bl())]
    listed = [r.bl() for _ in range(r.bl())]  # per face: a corner count, then that many point numbers
    faces, i = [], 0
    while i < len(listed):
        faces.append([n + 1 for n in listed[i + 1 : i + 1 + listed[i]]])
        i += 1 + listed[i]
    r.p = o.end  # edges, creases and per-face overrides are not used
    # shortcut: the cage is written as it is, so a mesh with smoothing levels comes out faceted
    return {"points": points, "faces": faces, "stored": {"SmoothingLevel": level}}


def _table(o, r):
    """ACAD_TABLE: AutoCAD keeps the table as drawn in a block, and the table is an insert of it (AcDbImpTable)."""
    e = _insert(o, r)
    r.p = o.end  # the cell contents and styles the block was drawn from
    return e


def _mleader(o, r):
    """MULTILEADER: leader lines running to a text or a block (AcDbMLeaderObjectContextData::dwgInMLContextData).

    Returned as parts, the form custom entities take: a polyline per leader line, then the content.
    """
    newer = since(o.release, "R2010")
    if newer:
        r.bs()  # version
    parts, lines = [], 0
    for _ in range(r.bl()):  # a root is one side of the content, where its leader lines meet
        r.b(), r.b()
        connection, direction = r.bd3(), r.bd3()
        for _ in range(r.bl()):
            r.bd3(), r.bd3()  # gaps where the landing crosses other objects
        r.bl()
        landing = r.bd()
        end = tuple(connection[i] + landing * direction[i] for i in range(3))
        for _ in range(r.bl()):
            points = [r.bd3() for _ in range(r.bl())]
            for _ in range(r.bl()):  # gaps in the line itself
                r.bl()
                for _ in range(r.bl()):
                    r.bd3(), r.bd3()
            r.bl()
            if newer:
                r.bs()
                _colour(o)
                r.bl(), r.bd(), r.bl()  # per-line overrides of weight and arrow size
                lines += 1
            parts.append({"type": "POLYLINE_3D", "points": [*points, connection, end], "closed": False})
        if newer:
            r.bs()
    r.bd(), r.bd3()  # overall scale, content base point
    height = r.bd()
    r.bd(), r.bd()  # arrow size, landing gap
    r.bs(), r.bs(), r.bs(), r.bs()  # attachment and alignment
    for _ in range(2 * lines):
        o.handles.h(o.handle)  # each line's linetype and arrowhead
    e = {"parts": parts, "height": height}
    if r.b():  # text content: the same values an MTEXT holds
        text = {"type": "MTEXT", "text": o.text(), "extrusion": r.bd3(), "point": r.bd3(), "x_axis": r.bd3(), "height": height}
        r.bd()  # rotation
        text["extent"] = (r.bd(), r.bd())
        e["style"] = o.handles.h(o.handle)
        parts.append(text)
    elif r.b():  # block content
        block = {"type": "INSERT", "block": o.handles.h(o.handle), "extrusion": r.bd3(), "point": r.bd3(), "scale": r.bd3(), "rotation": r.bd()}
        # shortcut: the location is used as it stands, which is right for content lying flat (normal along z)
        parts.append(block)
    r.p = o.end  # the settings the style supplies, and the arrowheads, are not read
    return e


def _shape(o, r):
    """SHAPE (AcDbImpShape): a symbol from a compiled shape file, which the drawing names but does not contain."""
    e = {"point": r.bd3(), "size": r.bd(), "rotation": r.bd()}
    r.bd(), r.bd()  # width factor, oblique angle
    _ = r.bd()  # thickness
    e["number"] = r.bs()
    e["extrusion"] = r.bd3()
    e["style"] = o.handles.h(o.handle)
    return e


def _light(o, r):
    """A light source (AcDbImpLight): placed as a light fixture, since IFC has no lamp without a fitting."""
    r.bl()  # version
    name = o.text()
    kind = {1: "distant", 2: "point", 3: "spot"}.get(r.bl(), "web")
    on = bool(r.b())
    _colour(o)
    r.b()  # plot the glyph
    e = {"name": name, "kind": kind, "on": on, "intensity": r.bd(), "point": r.bd3(), "target": r.bd3()}
    r.p = o.end  # attenuation, cone angles and shadow settings
    return e


_DECODERS = {
    "LINE": _line,
    "CIRCLE": _circle,
    "ARC": _arc,
    "POINT": _point,
    "ELLIPSE": _ellipse,
    "LWPOLYLINE": _lwpolyline,
    "POLYLINE_2D": _polyline_2d,
    "POLYLINE_3D": _polyline_3d,
    "POLYLINE_PFACE": _polyline_pface,
    "POLYLINE_MESH": _polyline_mesh,
    "VERTEX_2D": _vertex_2d,
    "VERTEX_3D": _vertex_3d,
    "VERTEX_MESH": _vertex_3d,
    "VERTEX_PFACE": _vertex_3d,
    "VERTEX_PFACE_FACE": _vertex_pface_face,
    "INSERT": _insert,
    "MINSERT": _insert,
    "TEXT": _text,
    "ATTRIB": _attrib,
    "MTEXT": _mtext,
    "SPLINE": _spline,
    "SOLID": _solid,
    "TRACE": _solid,
    "3DFACE": _face3d,
    "LEADER": _leader,
    "TOLERANCE": _tolerance,
    "RAY": _ray,
    "XLINE": _ray,
    "MLINE": _mline,
    "HATCH": _hatch,
    "IMAGE": _image,
    "WIPEOUT": _image,
    "HELIX": _helix,
    "MESH": _mesh,
    "ACAD_TABLE": _table,
    "LIGHT": _light,
    "SHAPE": _shape,
    "MULTILEADER": _mleader,
    "ARC_DIMENSION": _dimension,
    "LARGE_RADIAL_DIMENSION": _dimension,
} | {
    name: _acis
    for name in ("3DSOLID", "REGION", "BODY", "SURFACE", "PLANESURFACE", "EXTRUDEDSURFACE", "REVOLVEDSURFACE",
                 "SWEPTSURFACE", "LOFTEDSURFACE", "NURBSURFACE")  # fmt: skip
} | {name: _dimension for name in dwg.FIXED_TYPES.values() if name.startswith("DIMENSION")}
# objects that are not drawn but name, group or describe what is: where to keep them, and their decoder
_OBJECTS = {
    "DICTIONARY": ("dictionaries", _dictionary),
    "XRECORD": ("xrecords", _xrecord),
    "GROUP": ("groups", _group),
    "STYLE": ("styles", _style),
    "IMAGEDEF": ("images", _image_file),
    "MATERIAL": ("materials", _material),
    "GEODATA": ("geodata", _geodata),
    "ACSH_HISTORY_CLASS": ("histories", _history),
}
# entities with nothing to draw: block brackets, viewports, and attribute templates (the ATTRIB holds the value)
_STRUCTURAL = {"BLOCK", "ENDBLK", "SEQEND", "VIEWPORT", "ATTDEF"}


def _attach_vertices(drawing: Drawing) -> None:
    """Fold VERTEX entities into the POLYLINE that owns them."""
    by_handle = {e["handle"]: e for e in drawing.entities}
    for e in drawing.entities:
        if not e["type"].startswith("POLYLINE"):
            continue
        handles = e.pop("owned", None)
        if handles is None:  # pre-R2004: walk the entity chain from the first vertex to the last
            handles, at, last = [], e.pop("first"), e.pop("last")
            while at in by_handle and len(handles) < len(by_handle):
                handles.append(at)
                if at == last:
                    break
                at = by_handle[at].get("next")
        vertices = [by_handle[h] for h in handles if h in by_handle and by_handle[h]["type"].startswith("VERTEX")]
        e["faces"] = [v["indices"] for v in vertices if "indices" in v]
        # flag 16 marks a spline frame control point, which is not on the drawn curve
        vertices = [v for v in vertices if "point" in v and not v["flags"] & 16]
        e["points"] = [v["point"] for v in vertices]
        e["bulges"] = [v["bulge"] for v in vertices]
    drawing.entities = [e for e in drawing.entities if not e["type"].startswith("VERTEX")]


def load(data: bytes) -> Drawing:
    drawing = Drawing()
    drawing.release = release = dwg.version(data)
    secs = dwg.sections(data)
    names = dwg.classes(secs["AcDb:Classes"], release, data[0x0B])
    objects = secs["AcDb:AcDbObjects"]
    drawing.metric = secs.get("AcDb:Template", b"")[-2:] == b"\x01\x00"
    drawing.units = header.insunits(secs.get("AcDb:Header", b""), release, data[0x0B])
    for loc in dwg.object_map(secs["AcDb:Handles"]).values():
        kind = "object"
        try:  # one damaged object must not lose the rest of the drawing
            o = dwg.read_object(objects, loc, release, names)
            kind = o.type
            if o.type == "LAYER":
                drawing.layers[o.handle] = _layer(o)
                if o.r.p != o.end:
                    raise ValueError(f"LAYER {o.handle:X} decoded to bit {o.r.p}, expected {o.end}")
            elif o.type == "DIMSTYLE" and since(release, "R2000"):
                try:
                    drawing.dimstyles[o.handle] = _dimstyle(o)
                except ValueError:
                    pass  # sizes fall back to defaults
            elif o.type == "FIELD":
                for target, name, value, number in _field(o):
                    drawing.fields.setdefault(target, []).append((name, value, number))
            elif o.type == "LTYPE":
                try:
                    drawing.linetypes[o.handle] = _linetype(o)
                except ValueError:
                    pass  # drawn as a solid line
            elif o.type == "APPID":
                drawing.apps[o.handle] = _table_name(o)
            elif o.type.startswith("ACSH_") and o.type != "ACSH_HISTORY_CLASS":
                drawing.steps[o.handle] = _step(o)
                if o.r.p != o.end:
                    drawing.steps[o.handle]["sizes"] = {}  # still a step of its solid, with sizes we will not vouch for
            elif o.type in _OBJECTS or (o.type == "ACDBDICTIONARYWDFLT" and since(release, "R2000")):
                store, decode = _OBJECTS.get(o.type, _OBJECTS["DICTIONARY"])
                getattr(drawing, store)[o.handle] = decode(o)
                if o.r.p != o.end:
                    raise ValueError(f"{o.type} {o.handle:X} decoded to bit {o.r.p}, expected {o.end}")
            elif o.type == "BLOCK_CONTROL":
                drawing.spaces = _block_control(o)
            elif o.type == "BLOCK_HEADER":
                drawing.blocks[o.handle] = _block_header(o)
            elif o.type in _DECODERS:
                entity = _entity_common(o)
                graphics = entity.pop("graphics")
                entity |= _DECODERS[o.type](o, o.r)
                if "block" in entity and graphics:
                    entity["picture"] = graphics  # drawn from this if the block turns out to be missing
                if o.type == "MULTILEADER" and graphics:
                    # the picture has the arrowheads too: keep its linework, and our text as one editable piece
                    drawn = proxy.decode(graphics, release)
                    text = [p for p in entity["parts"] if p["type"] == "MTEXT"]
                    entity["native"] = entity["parts"]
                    entity["parts"] = [p for p in drawn if p["type"] != "PROXYTEXT"] + text if text and drawn else drawn or entity["parts"]
                if o.r.p != o.end:
                    raise ValueError(f"{o.type} {o.handle:X} decoded to bit {o.r.p}, expected {o.end}")
                if o.thickness:
                    entity["thickness"] = o.thickness
                drawing.entities.append(entity)
            elif o.is_entity and o.type not in _STRUCTURAL:
                # a type we do not decode: draw it from the picture of itself it carries, if it has one
                entity = _entity_common(o)
                entity["parts"] = proxy.decode(entity.pop("graphics"), release)
                if entity["parts"]:
                    drawing.entities.append(entity)
                else:
                    drawing.skipped[o.type] += 1
        except (ValueError, IndexError, KeyError, OverflowError):
            drawing.skipped[f"{kind} (unreadable)"] += 1

    for style in drawing.styles.values():
        style["font"] = next((face for app, face in style.pop("faces") if face and drawing.apps.get(app) == "ACAD"), style["font"])
    # a solid made in one step has that primitive's sizes; after more steps they no longer describe it
    steps = defaultdict(list)
    for step in drawing.steps.values():
        steps[step["owner"]].append(step)
    for e in drawing.entities:
        links = e.pop("links", ())
        made = steps.get(next((drawing.histories[h] for h in links if h in drawing.histories), None), ())
        links = [*links, *(step.get("material") for step in made)]
        faces = list(dict.fromkeys(drawing.materials[h] for h in links if h in drawing.materials))
        if faces:
            e["materials"] = faces
        made = [step for step in made if not step["kind"].startswith("Subent")]  # colours and materials shape nothing
        if len(made) == 1 and made[0]["sizes"]:
            e["stored"] = {"Primitive": made[0]["kind"]} | made[0]["sizes"]
    _attach_vertices(drawing)
    if any(e.get("bodies", 0) is None for e in drawing.entities):  # R2013+ keeps ACIS data outside the entity
        store = datastore.records(secs.get("AcDb:AcDsPrototype_1b", b""))
        for e in drawing.entities:
            data = store.get(e["handle"], b"") if e.get("bodies", 0) is None else b""
            if data.startswith(acis.SAB_MAGIC):
                try:
                    e["bodies"] = acis.bodies(data)
                except ValueError:
                    drawing.skipped[f"{e['type']} (unreadable)"] += 1
    spaces = {"model": drawing.block_named("*Model_Space"), "paper": drawing.block_named("*Paper_Space")}
    spaces |= {k: h for k, h in drawing.spaces.items() if h in drawing.blocks}  # the block table knows best
    by_handle = {e["handle"]: e for e in drawing.entities}
    for entity in drawing.entities:
        entity["owner"] = spaces.get(entity["owner"], entity["owner"])
    for entity in drawing.entities:
        if entity["type"] == "ATTRIB":  # owned by its INSERT, but drawn in the INSERT's space
            entity["insert"] = entity["owner"]
            if entity.get("hidden"):
                entity["invisible"] = 1
            entity["owner"] = by_handle.get(entity["owner"], {}).get("owner")
    return drawing

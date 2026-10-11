"""Decodes DWG objects into plain dicts (spec chapter 20.4)."""

import math
from collections import Counter

from dwg2ifc import acis, datastore, dwg, header, proxy
from dwg2ifc.dwg import since


class Drawing:
    def __init__(self) -> None:
        self.release = ""
        self.units = 0  # INSUNITS code; 0 means the drawing declares none
        self.metric = False  # MEASUREMENT: fallback when the drawing declares no units
        self.layers: dict[int, dict] = {}  # handle -> {name, color, lineweight, off, frozen, locked}
        self.blocks: dict[int, dict] = {}  # block header handle -> {name, base, units}
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


def _object_common(o: dwg.Obj) -> None:
    r = o.r
    if not since(o.release, "R2000"):
        o.set_size(r.rl())
    r.bl()  # reactor count
    if since(o.release, "R2004"):
        r.b()  # xdictionary missing
    if since(o.release, "R2013"):
        r.b()  # has data-store data


def _table_name(o: dwg.Obj) -> str:
    """Start of every table record (LAYER, BLOCK_HEADER, ...): its name and xref flags."""
    _object_common(o)
    name = o.text()
    if since(o.release, "R2007"):
        o.r.bs()  # xref index, with the 64-flag and xdep bits folded in
    else:
        o.r.b(), o.r.bs(), o.r.b()  # 64-flag, xref index, xdep
    return name


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
    name = _table_name(o)
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
    return {"name": name, "color": color or 7, "lineweight": lineweight, "off": off, "frozen": frozen, "locked": locked}


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
    o.text()  # xref path
    if since(o.release, "R2000"):
        while r.rc():  # one non-zero byte per insert, zero-terminated
            pass
        o.text()  # description
        preview_size = r.bl()
        r.p += preview_size * 8
    return {"name": name, "base": base, "units": r.bs() if since(o.release, "R2007") else 0}


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
    e["owner"] = hr.h(o.handle) if entmode == 0 else {1: "paper", 2: "model"}.get(entmode)
    for _ in range(reactors):
        hr.h()
    if not xdic_missing:
        hr.h()
    links = not since(release, "R2004")  # older files chain entities; sub-entity lists (vertices) rely on it
    if links:
        e["next"] = o.handle + 1
    if not since(release, "R2000"):
        e["layer"] = hr.h(o.handle)
        if ltype == 3:
            hr.h()
    if links and not no_links:
        hr.h(o.handle)
        e["next"] = hr.h(o.handle)
    if color_flags & 0x40:
        hr.h()  # colour book reference
    if since(release, "R2000"):
        e["layer"] = hr.h(o.handle)
        for present in (ltype == 3, material == 3, plotstyle == 3):
            if present:
                hr.h()
    for _ in range(visual_styles):
        hr.h()
    return e


def _thickness(o) -> float:
    return o.r.bt() if since(o.release, "R2000") else o.r.bd()


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
        r.bd()  # thickness
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
    r.bt()
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
    if flag & 4:
        r.bd()  # constant width
    elevation = r.bd() if flag & 8 else 0.0
    if flag & 2:
        r.bd()  # thickness
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
    for _ in range(n_widths):
        r.bd(), r.bd()
    return {"points": points, "bulges": bulges, "closed": bool(flag & 512), "elevation": elevation, "extrusion": extrusion}


def _polyline_2d(o, r):
    flags = r.bs()
    r.bs(), r.bd(), r.bd(), _thickness(o)  # curve type, start and end width, thickness
    return {"closed": bool(flags & 1), "elevation": r.bd(), "extrusion": _extrusion(o)} | _owned(o)


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


def _dimension(o, r):
    """Every DIMENSION kind: AutoCAD keeps the drawn dimension in an anonymous block, which is all we place."""
    release = o.release
    if since(release, "R2010"):
        r.rc()  # version
    extrusion = r.bd3()
    r.rd2()  # text midpoint
    elevation = r.bd()
    r.rc()  # flags
    o.text()  # user text
    r.bd(), r.bd()  # text rotation, horizontal direction
    scale = tuple(s or 1.0 for s in r.bd3())
    rotation = r.bd()
    if since(release, "R2000"):
        r.bs(), r.bs(), r.bd(), r.bd()  # attachment, line spacing style and factor, measurement
    if since(release, "R2007"):
        r.b(), r.b(), r.b()
    point = (*r.rd2(), elevation)
    r.p = o.end  # definition points differ per kind and are not needed to place the block
    o.handles.h()  # dimension style
    return {"point": point, "scale": scale, "rotation": rotation, "extrusion": extrusion, "block": o.handles.h(o.handle)}


def _text(o, r):
    if not since(o.release, "R2000"):
        elevation, point = r.bd(), r.rd2()
        r.rd2()  # alignment point
        extrusion = r.bd3()
        r.bd(), r.bd()  # thickness, oblique angle
        rotation, height = r.bd(), r.bd()
        r.bd()  # width factor
        text = o.text()
        r.bs(), r.bs(), r.bs()  # generation, horizontal and vertical alignment
        return {"point": (*point, elevation), "rotation": rotation, "height": height, "extrusion": extrusion, "text": text}
    flags = r.rc()
    elevation = 0.0 if flags & 1 else r.rd()
    point = r.rd2()
    if not flags & 2:
        r.dd(point[0]), r.dd(point[1])  # alignment point
    extrusion = r.be()
    r.bt()
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
    return {"point": (*point, elevation), "rotation": rotation, "height": height, "extrusion": extrusion, "text": text}


def _attrib(o, r):
    e = _text(o, r)
    o.r.p = o.end  # tag, prompt and flags are not needed to draw the value
    return e


def _mtext(o, r):
    e = {"point": r.bd3(), "extrusion": r.bd3(), "x_axis": r.bd3()}
    r.bd()  # rectangle width
    if since(o.release, "R2007"):
        r.bd()  # rectangle height
    e["height"] = r.bd()
    r.bs(), r.bs(), r.bd(), r.bd()  # attachment, drawing direction, extents
    e["text"] = o.text()
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
    return {"bodies": acis.bodies(data) if data else None}


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
    "HATCH": _hatch,
} | {
    name: _acis
    for name in ("3DSOLID", "REGION", "BODY", "SURFACE", "PLANESURFACE", "EXTRUDEDSURFACE", "REVOLVEDSURFACE",
                 "SWEPTSURFACE", "LOFTEDSURFACE", "NURBSURFACE")  # fmt: skip
} | {name: _dimension for name in dwg.FIXED_TYPES.values() if name.startswith("DIMENSION")}
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
            elif o.type == "BLOCK_HEADER":
                drawing.blocks[o.handle] = _block_header(o)
            elif o.type in _DECODERS:
                entity = _entity_common(o)
                del entity["graphics"]
                entity |= _DECODERS[o.type](o, o.r)
                if o.r.p != o.end:
                    raise ValueError(f"{o.type} {o.handle:X} decoded to bit {o.r.p}, expected {o.end}")
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
    by_handle = {e["handle"]: e for e in drawing.entities}
    for entity in drawing.entities:
        entity["owner"] = spaces.get(entity["owner"], entity["owner"])
    for entity in drawing.entities:
        if entity["type"] == "ATTRIB":  # owned by its INSERT, but drawn in the INSERT's space
            entity["owner"] = by_handle.get(entity["owner"], {}).get("owner")
    return drawing

"""Maps a decoded DWG drawing to an IFC4X3 model.

Model space becomes one IfcAnnotation per entity, blocks become IfcRepresentationMaps placed by
IfcMappedItem, and DWG layers become IfcPresentationLayerAssignments.
"""

import math
import re
import statistics
from collections import defaultdict

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.georeference
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.unit
import ifcopenshell.guid

from dwg2ifc import derive, proxy
from dwg2ifc.classify import classify, is_plan
from dwg2ifc.entities import Drawing, bspline_points, fit_spline

# DWG INSUNITS code -> (SI prefix, None) or (None, conversion unit name)
_UNITS = {1: (None, "inch"), 2: (None, "foot"), 3: (None, "mile"), 4: ("MILLI", None), 5: ("CENTI", None),
          6: (None, None), 7: ("KILO", None), 10: (None, "yard"), 14: ("DECI", None)}  # fmt: skip

# metres per drawing unit, by INSUNITS code
_METRES = {1: 0.0254, 2: 0.3048, 3: 1609.344, 4: 0.001, 5: 0.01, 6: 1.0, 7: 1000.0, 10: 0.9144, 14: 0.1}
_UNIT_NAMES = {1: "inches", 2: "feet", 3: "miles", 4: "millimetres", 5: "centimetres", 6: "metres", 7: "kilometres", 10: "yards", 14: "decimetres"}
# what a caller may name a unit in the request
UNIT_CODES = {"in": 1, "ft": 2, "mi": 3, "mm": 4, "cm": 5, "m": 6, "km": 7, "yd": 10, "dm": 14}
# Viewers hold coordinates in single precision, which is good to about a millimetre 10 km from the origin.
_FAR = 10_000.0

# line weight codes -> hundredths of a millimetre; 29 by layer, 30 by block, 31 default
_LINEWEIGHTS = (0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211)
# AutoCAD's colour index, 256 red-green-blue triples, as shipped in its own libraries (the default
# colour map in libwhiptk_wt.dylib of AutoCAD 2027). Index 0 is "by block" and 256 "by layer".
_PALETTE = bytes.fromhex(
    "000000ff0000ffff0000ff0000ffff0000ffff00ffffffff808080c0c0c0ff0000ff7f7fcc0000cc6666990000994c4c"
    "7f00007f3f3f4c00004c2626ff3f00ff9f7fcc3300cc7f66992600995f4c7f1f007f4f3f4c13004c2f26ff7f00ffbf7f"
    "cc6600cc9966994c0099724c7f3f007f5f3f4c26004c3926ffbf00ffdf7fcc9900ccb26699720099854c7f5f007f6f3f"
    "4c39004c4226ffff00ffff7fcccc00cccc6699990099994c7f7f007f7f3f4c4c004c4c26bfff00dfff7f99cc00b2cc66"
    "72990085994c5f7f006f7f3f394c00424c267fff00bfff7f66cc0099cc664c990072994c3f7f005f7f3f264c00394c26"
    "3fff009fff7f33cc007fcc662699005f994c1f7f004f7f3f134c002f4c2600ff007fff7f00cc0066cc660099004c994c"
    "007f003f7f3f004c00264c2600ff3f7fff9f00cc3366cc7f0099264c995f007f1f3f7f4f004c13264c2f00ff7f7fffbf"
    "00cc6666cc9900994c4c9972007f3f3f7f5f004c26264c3900ffbf7fffdf00cc9966ccb20099724c9985007f5f3f7f6f"
    "004c39264c4200ffff7fffff00cccc66cccc0099994c9999007f7f3f7f7f004c4c264c4c00bfff7fdfff0099cc66b2cc"
    "0072994c8599005f7f3f6f7f00394c26424c007fff7fbfff0066cc6699cc004c994c7299003f7f3f5f7f00264c26394c"
    "003fff7f9fff0033cc667fcc0026994c5f99001f7f3f4f7f00134c262f4c0000ff7f7fff0000cc6666cc0000994c4c99"
    "00007f3f3f7f00004c26264c3f00ff9f7fff3300cc7f66cc2600995f4c991f007f4f3f7f13004c2f264c7f00ffbf7fff"
    "6600cc9966cc4c0099724c993f007f5f3f7f26004c39264cbf00ffdf7fff9900ccb266cc720099854c995f007f6f3f7f"
    "39004c42264cff00ffff7fffcc00cccc66cc990099994c997f007f7f3f7f4c004c4c264cff00bfff7fdfcc0099cc66b2"
    "990072994c857f005f7f3f6f4c00394c2642ff007fff7fbfcc0066cc669999004c994c727f003f7f3f5f4c00264c2639"
    "ff003fff7f9fcc0033cc667f990026994c5f7f001f7f3f4f4c00134c262f3333335b5b5b848484adadadd6d6d6ffffff"
)


def _rgb(color) -> tuple[int, int, int]:
    """AutoCAD Color Index to red, green, blue.

    7 is the exception: AutoCAD draws it white on a dark background and black on paper. The table says
    white; drafting is written black here, as on paper.
    """
    if isinstance(color, tuple):
        return color
    if color == 7 or not 1 <= color <= 255:
        return (0, 0, 0)
    return tuple(_PALETTE[3 * color : 3 * color + 3])


# Classes a block insert may take from its layer alone: things that are placed as objects. A block that merely
# sits on a wall or floor layer is more likely a pattern or a mark than a wall or a slab.
_PLACED = {"IfcDoor", "IfcWindow", "IfcStair", "IfcColumn", "IfcSanitaryTerminal", "IfcLightFixture", "IfcOutlet",
           "IfcFurniture", "IfcGeographicElement"}  # fmt: skip

# DWG entity type -> _Builder method, where the name is not simply the lowercased type
_METHODS = {"POLYLINE_2D": "lwpolyline", "ATTRIB": "text", "TRACE": "solid", "3DFACE": "face3d",
            "3DSOLID": "acis", "REGION": "acis", "BODY": "acis"}  # fmt: skip
_METHODS |= {"IMAGE": "polyline_3d", "WIPEOUT": "polyline_3d", "HELIX": "spline", "MESH": "polyline_pface", "ACAD_TABLE": "insert",
             "ARC_DIMENSION": "dimension", "LARGE_RADIAL_DIMENSION": "dimension", "LIGHT": "point", "SHAPE": "point",
             "PDFUNDERLAY": "underlay", "DWFUNDERLAY": "underlay", "DGNUNDERLAY": "underlay"}  # fmt: skip
_METHODS |= dict.fromkeys(("SURFACE", "PLANESURFACE", "EXTRUDEDSURFACE", "REVOLVEDSURFACE", "SWEPTSURFACE",
                           "LOFTEDSURFACE", "NURBSURFACE"), "acis")  # fmt: skip
# representation types that are model geometry rather than drafting
_BODY_KINDS = {"AdvancedBrep", "SurfaceModel", "Tessellation", "CSG", "SweptSolid"}

_Z = (0.0, 0.0, 1.0)
# a full circle as a quadratic NURBS: nine control points (x, y, weight) on the unit circle's bounding square
_CIRCLE = [
    (math.cos(k * math.pi / 4) * (1 if k % 2 == 0 else math.sqrt(2)), math.sin(k * math.pi / 4) * (1 if k % 2 == 0 else math.sqrt(2)),
     1.0 if k % 2 == 0 else math.sqrt(0.5))
    for k in range(9)
]  # fmt: skip
_CIRCLE_KNOTS = ([3, 2, 2, 2, 3], [0.0, 0.25, 0.5, 0.75, 1.0])  # multiplicities, knots


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(v):
    length = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    return (v[0] / length, v[1] / length, v[2] / length) if length else _Z


def _ocs(extrusion):
    """DWG arbitrary axis algorithm: the X, Y, Z axes of an entity's object coordinate system."""
    z = _unit(extrusion)
    if z == _Z:
        return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), z
    x = _unit(_cross((0.0, 1.0, 0.0) if abs(z[0]) < 1 / 64 and abs(z[1]) < 1 / 64 else _Z, z))
    return x, _cross(z, x), z


def _frame(z, x):
    """Orthonormal X, Y, Z axes from a normal and a reference direction that may be missing or not square to it."""
    z = _unit(z)
    x = tuple(x[i] - sum(a * b for a, b in zip(x, z)) * z[i] for i in range(3))
    x = _unit(x) if sum(c * c for c in x) > 1e-18 else _ocs(z)[0]
    return x, _cross(z, x), z


def _to_wcs(axes, p):
    x, y, z = axes
    return tuple(p[0] * x[i] + p[1] * y[i] + p[2] * z[i] for i in range(3))


def _same(p, q) -> bool:
    """Points too close to make an edge. Edges shorter than the model's precision (1e-5) crash or fail in
    geometry kernels (seen with IfcOpenShell)."""
    return all(math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-5) for a, b in zip(p, q))


def _distinct(points):
    """Drops points that repeat their predecessor."""
    out = []
    for p in points:
        if not out or not _same(out[-1], p):
            out.append(tuple(map(float, p)))
    return out


def _plain_text(mtext: str) -> str:
    """MTEXT content without its inline formatting codes."""
    s = re.sub(r"\\P", "\n", mtext)
    s = re.sub(r"\\[ACcFfHQTWp][^;]*;|\\[LlOoKk]", "", s)
    s = re.sub(r"\\S([^;]*);", lambda m: m.group(1).replace("^", "/").replace("#", "/"), s)
    s = re.sub(r"(?<!\\)[{}]", "", s)
    return s.replace("\\~", " ").replace("\\\\", "\\").replace("\\{", "{").replace("\\}", "}")


def _far_origin(entities: list[dict], metres: float) -> tuple[float, float, float]:
    """Where a drawing placed far from the origin (survey coordinates) sits, or zero if it is near."""
    samples = []
    for e in entities:
        if e.get("extrusion", _Z) != _Z:
            continue  # its coordinates are in a tilted plane, not world axes
        for key in ("start", "point", "center", "points", "control", "fit", "corners"):
            p = e.get(key)
            p = p[0] if p and isinstance(p, list) else p
            if p and len(p) >= 2:
                samples.append((p[0], p[1], p[2] if len(p) > 2 else e.get("elevation", 0.0)))
                break
    if not samples:
        return (0.0, 0.0, 0.0)
    origin = tuple(float(round(statistics.median(axis))) for axis in zip(*samples))
    return origin if math.hypot(*origin) * metres > _FAR else (0.0, 0.0, 0.0)


def _map_position(geo: dict, shift) -> dict:
    """IfcMapConversion values from a drawing's stored geographic location, for a projected coordinate system."""
    if geo.get("kind") != "projected grid" or not geo.get("system"):
        return {}  # shortcut: latitude/longitude locations are not converted, that takes a map projection
    code = re.search(r'<Alias id="(\d+)" type="CoordinateSystem">', geo["system"])
    named = re.search(r'<ProjectedCoordinateSystem id="([^"]+)"', geo["system"])
    nx, ny = _unit((*geo["north"], 0.0))[:2]
    dx, dy, dz = (shift[i] - geo["design"][i] for i in range(3))
    scale = geo["scale"] or 1.0
    # a drawing vector's map coordinates are its parts along the drawing's east and north directions
    return {
        "Name": f"EPSG:{code.group(1)}" if code else named.group(1) if named else "from the drawing",
        "Eastings": geo["reference"][0] + scale * (dx * ny - dy * nx),
        "Northings": geo["reference"][1] + scale * (dx * nx + dy * ny),
        "OrthogonalHeight": geo["reference"][2] + scale * dz,
        "XAxisAbscissa": ny,
        "XAxisOrdinate": nx,
        "Scale": scale,
    }


def _kind(item) -> str:
    """The IfcShapeRepresentation type an item belongs in."""
    for ifc_class, kind in (
        ("IfcMappedItem", "MappedRepresentation"),
        ("IfcTextLiteral", "Text"),
        ("IfcCartesianPoint", "Point"),
        ("IfcAnnotationFillArea", "FillArea"),
        ("IfcTessellatedItem", "Tessellation"),
        ("IfcAdvancedBrep", "AdvancedBrep"),
        ("IfcShellBasedSurfaceModel", "SurfaceModel"),
        ("IfcCsgSolid", "CSG"),
        ("IfcSweptAreaSolid", "SweptSolid"),
    ):
        if item.is_a(ifc_class):
            return kind
    return "Curve3D"


def _halves(degree, knots, counts, poles):
    """A B-spline cut in two at a knot near its middle: ((knots, counts, poles), (knots, counts, poles)).

    Knots are given and returned as ACIS writes them (end knots repeated `degree` times). The cut is made by
    inserting the knot until the curve passes through a pole there, which changes no point of it.
    """
    flat = [k for k, m in zip(knots, counts) for _ in range(m)]
    flat = [flat[0], *flat, flat[-1]]
    middle = (flat[0] + flat[-1]) / 2
    at = min(flat[degree + 1 : -degree - 1], key=lambda k: abs(k - middle), default=middle)
    poles = list(poles)
    while flat.count(at) < degree:
        k = max(i for i, u in enumerate(flat) if u <= at)
        blend = lambda i: (at - flat[i]) / (flat[i + degree] - flat[i])  # noqa: E731
        poles = [
            poles[i] if i <= k - degree else poles[i - 1] if i > k
            else tuple((1 - blend(i)) * p + blend(i) * q for p, q in zip(poles[i - 1], poles[i]))
            for i in range(len(poles) + 1)
        ]  # fmt: skip
        flat.insert(k + 1, at)
    k = max(i for i, u in enumerate(flat) if u <= at)

    def acis(part):
        distinct = sorted(set(part))
        repeats = [part.count(u) for u in distinct]
        repeats[0], repeats[-1] = repeats[0] - 1, repeats[-1] - 1
        return distinct, repeats

    return (*acis([*flat[: k + 1], at]), poles[: k - degree + 1]), (*acis([at, *flat[k - degree + 1 :]]), poles[k - degree :])


def _turning(points) -> tuple:
    """Which way a closed run of points goes round, as a vector (right-hand rule)."""
    total = (0.0, 0.0, 0.0)
    for p, q in zip(points, [*points[1:], points[0]]):
        total = tuple(t + c for t, c in zip(total, _cross(p, q)))
    return total


def _open_tubes(faces) -> list:
    """Faces with every tube cut lengthwise into two patches.

    ACIS bounds a swept tube (a cable, a handrail) as one face on a spline surface that closes on itself: rim,
    seam, rim, seam back. Geometry kernels fail on that seam (IfcOpenShell builds nothing). Cut in two along the
    seam and along the line opposite it, each half is an ordinary four-sided patch on its own surface. Nothing
    moves: the surface is divided by knot insertion, and the new edges are the patches' own boundary curves.
    """
    swaps, made = {}, []
    for face in faces:
        s, loops = face["surface"], face["loops"]
        loop = loops[0] if len(loops) == 1 else ()
        keys = [edge[0] for edge in loop]
        seam = [edge for edge in loop if keys.count(edge[0]) == 2]
        rims = [edge for edge in loop if keys.count(edge[0]) == 1 and edge[1] and _same(edge[1], edge[2])]
        if not (s and s[0] == "bspline" and len(s) > 8 and sum(s[8]) == 1 and len(loop) == 4 and len(seam) == 2 and len(rims) == 2 and len(s[7][0][0]) == 3):
            made.append(face)
            continue
        _, du, dv, ku, mu, kv, mv, grid, closed = s
        if turned := closed[1]:  # make u the direction that goes round
            grid, du, dv, ku, mu, kv, mv = [list(column) for column in zip(*grid)], dv, du, kv, mv, ku, mu
        reach = math.dist(grid[0][0], grid[len(grid) // 2][0]) or 1.0
        ends = {j: rim for j in (0, -1) for rim in rims if math.dist(rim[1], grid[0][j]) < 0.05 * reach}
        if len(ends) != 2 or ends[0] is ends[-1]:
            made.append(face)  # trimmed short of the surface's own ends: not the plain tube this handles
            continue
        (lk, lm, left), (rk, rm, right) = _halves(du, ku, mu, [tuple(c for p in row for c in p) for row in grid])
        left, right = ([[row[i : i + 3] for i in range(0, len(row), 3)] for row in half] for half in (left, right))
        halves = {}
        for j, (key, _, _, curve, same_sense, forward) in ends.items():
            corner = grid[0][j]  # the surface's own corner, which the stored vertex only approximates
            ours = _turning([row[j] for row in grid])
            theirs = curve[2] if curve[0] == "ellipse" else _turning([p[:3] for p in curve[4]])
            along = (sum(a * b for a, b in zip(ours, theirs)) > 0) == (same_sense == forward)  # walked with u, as the tube's loop has it
            halves[j] = (
                ((key, "left"), corner, left[-1][j], ("bspline", du, lk, lm, [row[j] for row in left]), True),
                ((key, "right"), right[0][j], corner, ("bspline", du, rk, rm, [row[j] for row in right]), True),
            )
            swaps[key] = (forward == along, *halves[j])
        start, end = grid[0][0], grid[0][-1]
        joint = ((seam[0][0], "seam"), start, end, ("bspline", dv, kv, mv, left[0]), True)
        middle = ((seam[0][0], "middle"), left[-1][0], left[-1][-1], ("bspline", dv, kv, mv, left[-1]), True)
        # each patch walked round its own surface: low rim, far side, high rim back, near side back
        walks = (
            [(*halves[0][0], True), (*middle, True), (*halves[-1][0], False), (*joint, False)],
            [(*halves[0][1], True), (*joint, True), (*halves[-1][1], False), (*middle, False)],
        )
        if not swaps[ends[0][0]][0] == ends[0][5]:  # the tube's own loop went the other way round
            walks = [[(*edge[:5], not edge[5]) for edge in reversed(walk)] for walk in walks]
        for (knots, repeats, half), walk in zip(((lk, lm, left), (rk, rm, right)), walks):
            made.append(dict(face, surface=("bspline", du, dv, knots, repeats, kv, mv, half, (False, False)), loops=[walk], same_sense=face["same_sense"] != turned))
    for face in made if swaps else ():
        for loop in face["loops"]:
            for i in reversed(range(len(loop))):
                if loop[i][0] in swaps:  # a neighbour bounded by a whole rim now meets its two halves
                    with_u, first, second = swaps[loop[i][0]]
                    loop[i : i + 1] = [(*first, True), (*second, True)] if with_u == loop[i][5] else [(*second, False), (*first, False)]
    return made


class _Builder:
    def __init__(self, drawing: Drawing, name: str, heights: dict | None = None) -> None:
        self.d = drawing
        self.heights = heights or {}  # heights the caller supplies, in metres: storey, door, window, sill
        self.f = f = ifcopenshell.api.project.create_file(version="IFC4X3")
        self.by_owner = defaultdict(list)
        self.skipped = drawing.skipped.copy()
        for e in drawing.entities:
            layer = drawing.layers.get(e["layer"], {})
            if layer.get("off") or layer.get("frozen"):
                # AutoCAD does not show these, and IFC4X3 has no way to mark a layer as hidden
                self.skipped["on a layer that is off or frozen"] += 1
            elif e["invisible"]:
                self.skipped["marked invisible"] += 1
            else:
                self.by_owner[e["owner"]].append(e)
        # the drawing's named object dictionary: groups, layouts, materials and so on by name
        self.root = next((entries for entries in drawing.dictionaries.values() if "ACAD_GROUP" in entries), {})
        self.layer_items = defaultdict(list)
        self.maps: dict[int, object] = {}  # block handle -> IfcRepresentationMap, or None if empty
        self.styles: dict[tuple, object] = {}  # (kind, colour, width) -> shared style entity
        self.values: dict[tuple, object] = {}  # (name, value) -> shared IfcPropertySingleValue
        self.types: dict[tuple, object] = {}  # (block handle, IFC class) -> its IfcTypeProduct
        self.typed = defaultdict(list)  # type entity -> occurrences
        self.with_material = defaultdict(list)  # material names -> elements made of them
        self.by_handle: dict[int, object] = {}  # DWG handle -> the product written for it
        self.described = defaultdict(list)  # (layer, entity type, colour) -> products with those properties
        self.attributes = defaultdict(list)  # insert handle -> [(tag, value)] from its block attributes
        for e in drawing.entities:
            if e["type"] == "ATTRIB" and e.get("tag"):
                self.attributes[e.get("insert")].append((e["tag"], _plain_text(e["text"]) if e.get("multiline") else e["text"]))

        project = ifcopenshell.api.root.create_entity(f, ifc_class="IfcProject", name=name)
        model_space = drawing.block_named("*Model_Space")
        units, source = self.unit(model_space)
        prefix, conversion = _UNITS[units]
        metres = _METRES[units]
        self.unit_note = (_UNIT_NAMES[units], source)
        self.millimetre = 0.001 / metres  # in drawing units, for line widths
        self.metres = metres
        layouts = {handle: name for name, handle in drawing.dictionaries.get(self.root.get("ACAD_LAYOUT"), {}).items()}
        # A drawing is its model space. One with nothing there, such as a title block, is its paper-space layouts.
        spaces = [("Model Space", model_space)] if self.by_owner.get(model_space) else [
            (layouts.get(drawing.blocks[handle]["layout"], f"Layout {n}"), handle)
            for n, handle in enumerate((h for h, b in drawing.blocks.items() if b["name"].lower().startswith("*paper_space") and self.by_owner.get(h)), 1)
        ]  # fmt: skip
        written = {handle for _, handle in spaces}
        for handle, block in drawing.blocks.items():
            if block["name"].lower().startswith("*paper_space") and handle not in written and self.by_owner.get(handle):
                self.skipped["on a paper-space layout"] += len(self.by_owner[handle])
        # each space is written relative to this point; block definitions keep their own coordinates
        self.origin_shift = _far_origin([e for _, handle in spaces for e in self.by_owner[handle]], metres)
        self.shift = (0.0, 0.0, 0.0)
        length = (
            ifcopenshell.api.unit.add_conversion_based_unit(f, name=conversion)
            if conversion
            else ifcopenshell.api.unit.add_si_unit(f, unit_type="LENGTHUNIT", prefix=prefix)
        )
        # angles stay in radians, as DWG stores them; areas are given in square metres whatever the drawing unit
        ifcopenshell.api.unit.assign_unit(
            f,
            units=[length, ifcopenshell.api.unit.add_si_unit(f, unit_type="PLANEANGLEUNIT"), ifcopenshell.api.unit.add_si_unit(f, unit_type="AREAUNIT")],
        )
        model = ifcopenshell.api.context.add_context(f, context_type="Model")
        self.context = ifcopenshell.api.context.add_context(
            f, context_type="Model", context_identifier="Annotation", target_view="MODEL_VIEW", parent=model
        )
        self.body_context = ifcopenshell.api.context.add_context(
            f, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=model
        )
        self.footprint_context = ifcopenshell.api.context.add_context(
            f, context_type="Model", context_identifier="FootPrint", target_view="MODEL_VIEW", parent=model
        )
        self.axis_context = ifcopenshell.api.context.add_context(
            f, context_type="Model", context_identifier="Axis", target_view="GRAPH_VIEW", parent=model
        )
        self.origin = f.createIfcAxis2Placement3D(f.createIfcCartesianPoint((0.0, 0.0, 0.0)))
        self.identity = f.createIfcCartesianTransformationOperator3D(None, None, self.origin.Location)

        site = ifcopenshell.api.root.create_entity(f, ifc_class="IfcSite", name="Site")
        building = ifcopenshell.api.root.create_entity(f, ifc_class="IfcBuilding", name="Building")
        ifcopenshell.api.aggregate.assign_object(f, products=[site], relating_object=project)
        ifcopenshell.api.aggregate.assign_object(f, products=[building], relating_object=site)

        if any(self.origin_shift):
            # keep the real position as georeferencing instead of in every coordinate
            ifcopenshell.api.georeference.add_georeferencing(f)
            east, north, height = self.origin_shift
            ifcopenshell.api.georeference.edit_georeferencing(
                f,
                projected_crs={"Name": "DWG world coordinates", "MapUnit": length},
                coordinate_operation={"Eastings": east, "Northings": north, "OrthogonalHeight": height},
            )
        located = _map_position(next(iter(drawing.geodata.values()), {}), self.origin_shift)
        if located:  # the drawing says where on Earth it is: that replaces the plain offset above
            if not any(self.origin_shift):
                ifcopenshell.api.georeference.add_georeferencing(f)
            name = located.pop("Name")
            # shortcut: the map unit is left to mean the project's length unit; Scale converts when they differ
            ifcopenshell.api.georeference.edit_georeferencing(f, projected_crs={"Name": name, "MapUnit": None}, coordinate_operation=located)
        # placements and containment are written directly: the API's assign_container costs 15 ms per product
        for space_name, handle in spaces or [("Model Space", None)]:
            storey = ifcopenshell.api.root.create_entity(f, ifc_class="IfcBuildingStorey", name=space_name)
            ifcopenshell.api.aggregate.assign_object(f, products=[storey], relating_object=building)
            storey.ObjectPlacement = f.createIfcLocalPlacement(None, self.origin)
            storey.Elevation = 0.0
            products = []
            for e in self.by_owner.get(handle, []):
                items = self.items(e, ())
                if not items:
                    continue
                ifc_class = self.element_class(e, items)
                representation = self.representation(items, footprint=ifc_class != "IfcAnnotation")
                product = f.create_entity(
                    ifc_class,
                    GlobalId=ifcopenshell.guid.new(),
                    Name=f"{e['type']}:{e['handle']:X}",
                    ObjectType=e["type"],
                    ObjectPlacement=f.createIfcLocalPlacement(storey.ObjectPlacement, self.origin),
                    Representation=f.createIfcProductDefinitionShape(None, None, [representation]),
                )
                products.append(product)
                self.by_handle[e["handle"]] = product
                self.describe(e, product)
                self.give_type(e, product, items)
            if products:
                f.createIfcRelContainedInSpatialStructure(ifcopenshell.guid.new(), None, None, None, products, storey)
            if handle == model_space:
                self.spaces(storey, handle)
                # real wall objects make guessing walls from linework pointless, and the guesses would double them
                if not any(e["type"] == "AEC_WALL" and "aec" in e for e in self.by_owner.get(handle, [])):
                    self.walls(storey, handle)
                self.architecture(storey, handle)
                self.columns(storey, handle)
        self.groups()
        # which unit the coordinates are in, and how that was decided: the one choice everything else scales by
        self.property_set("DWG_Units", {"Unit": self.unit_note[0], "Source": self.unit_note[1]}, [project])
        made = {}
        for names, made_of in self.with_material.items():
            listed = [made.setdefault(name, f.createIfcMaterial(name, None, None)) for name in names]
            material = listed[0] if len(listed) == 1 else f.createIfcMaterialList(listed)  # several: one per face
            f.createIfcRelAssociatesMaterial(ifcopenshell.guid.new(), None, None, None, made_of, material)
        for kind, occurrences in self.typed.items():
            f.createIfcRelDefinesByType(ifcopenshell.guid.new(), None, None, None, occurrences, kind)
        for (layer, kind, colour, linetype), described in self.described.items():
            self.property_set("DWG_Entity", {"Layer": layer, "EntityType": kind, "Colour": colour, "Linetype": linetype}, described)
        for name, items in self.layer_items.items():
            if items:  # a layer whose entities all came out empty has nothing to assign
                f.createIfcPresentationLayerAssignment(name, None, items, None)

    def property_set(self, name, values: dict, products) -> None:
        f = self.f
        properties = []
        for key, value in values.items():
            if (key, value) not in self.values:
                self.values[key, value] = f.createIfcPropertySingleValue(key, None, f.createIfcText(str(value)), None)
            properties.append(self.values[key, value])
        if properties:
            pset = f.createIfcPropertySet(ifcopenshell.guid.new(), None, name, None, properties)
            f.createIfcRelDefinesByProperties(ifcopenshell.guid.new(), None, None, None, products, pset)

    def stored_set(self, name, description, stored, measure="IfcReal"):
        """A property set of stored values in their own types, so that a BIM tool can edit them as numbers."""
        f = self.f
        kinds = {bool: "IfcBoolean", int: "IfcInteger", float: measure, str: "IfcText"}
        listed = [f.createIfcPropertySingleValue(key, None, f.create_entity(kinds[type(value)], value), None)
                  for key, value in stored.items() if type(value) in kinds]  # fmt: skip
        return f.createIfcPropertySet(ifcopenshell.guid.new(), None, name, description or None, listed) if listed else None

    def describe(self, e, product) -> None:
        """What the DWG knows about an entity beyond its shape, as IFC properties."""
        layer = self.d.layers.get(e["layer"], {})
        colour = e["color"] or layer.get("color", 7)
        colour = "by block" if e["color"] == 0 else ",".join(map(str, _rgb(colour)))
        # shared by every entity with the same layer, type and colour; the handle is in the product's name
        self.described[layer.get("name", "0"), e["type"], colour, self.linetype(e, layer).get("name", "Continuous")].append(product)
        if product.is_a("IfcElement"):
            product.Tag = f"{e['handle']:X}"
        block = self.named(e)
        if block and not block["name"].startswith("*"):
            self.property_set("DWG_Block", {"BlockName": block["name"]} | ({"ExternalFile": block["xref"]} if block["xref"] else {}), [product])
        if parameters := self.stored_set("DWG_BlockParameters", "What this insert's dynamic block parameters are set to", e.get("parameters", {}), "IfcLengthMeasure"):
            self.f.createIfcRelDefinesByProperties(ifcopenshell.guid.new(), None, None, None, [product], parameters)
        tags = {}
        for tag, value in self.attributes.get(e["handle"], ()):
            n = sum(1 for k in tags if k == tag or k.startswith(tag + "_"))
            tags[tag if not n else f"{tag}_{n + 1}"] = value  # a block may repeat a tag
        if tags:
            self.property_set("DWG_Attributes", tags, [product])
        stored = {f"Stored{name}": value for name, value, _ in self.d.fields.get(e["handle"], ())}
        stored |= e.get("stored", {})
        if e.get("thickness"):
            stored["Thickness"] = e["thickness"]
        if e.get("width", 0) > 0:
            stored["Width"] = e["width"]
        if stored:  # values the drawing holds for this entity that its shape alone does not show
            self.property_set("DWG_Stored", stored, [product])
        # a solid's faces name their materials; otherwise the entity's own, or else its layer's
        materials = e.get("materials") or [self.d.materials.get(e.get("material") or layer.get("material"), "")]
        materials = [m for m in materials if m and m not in ("ByLayer", "ByBlock", "Global")]
        if product.is_a("IfcElement") and materials:
            self.with_material[tuple(materials)].append(product)
        if e["type"] == "LIGHT":
            product.Name = e["name"] or product.Name
            self.property_set("DWG_Light", {"Kind": e["kind"], "On": e["on"], "Intensity": e["intensity"], "Target": e["target"]}, [product])
        if e["type"] == "SHAPE":  # shortcut: placed as a point; drawing the symbol needs the shape file itself
            file = self.d.styles.get(e["style"], {}).get("font", "")
            self.property_set("DWG_Shape", {"File": file, "Number": e["number"], "Size": e["size"], "Rotation": e["rotation"]}, [product])
        if e.get("definition") in self.d.underlays:
            shown = self.d.underlays[e["definition"]]
            self.property_set("DWG_Underlay", {"File": shown["file"], "Page": shown["page"], "Scale": e["scale"][0], "Rotation": e["rotation"]}, [product])
        if e.get("definition") in self.d.images:
            self.property_set("DWG_Image", {"File": self.d.images[e["definition"]]}, [product])
        self.xdata(e, product)
        self.records(e, product)

    def xdata(self, e, product) -> None:
        for app, items in e.get("xdata", ()):
            name = self.d.apps.get(app, "")
            # shortcut: AutoCAD's own bookkeeping apps are left out; only other programs' data is carried over
            if name and not name.upper().startswith(("ACAD", "ACDB", "GRADIENTCOLOR")):
                listed = "; ".join(f"{code}={value}" for code, value in items)
                self.property_set(f"DWG_XData_{name}", {"Items": listed}, [product])  # set names must be unique per object

    def records(self, e, product) -> None:
        """Data other programs filed under the entity (its extension dictionary), one property set per record."""
        d = self.d
        entries = dict(d.dictionaries.get(e.get("dictionary"), {}))
        for name, handle in list(entries.items()):
            entries |= d.dictionaries.get(handle, {})  # one level down: a program's folder of records
        for name, handle in entries.items():
            # shortcut: AutoCAD's own bookkeeping records are left out, as with xdata
            if d.xrecords.get(handle) and not name.upper().startswith(("ACAD", "ACDB")):
                listed = "; ".join(f"{code}={value}" for code, value in d.xrecords[handle])
                self.property_set(f"DWG_Record_{name}", {"Items": listed}, [product])

    def groups(self) -> None:
        """AutoCAD groups: named sets of entities, kept as IfcGroup."""
        f = self.f
        names = {handle: name for name, handle in self.d.dictionaries.get(self.root.get("ACAD_GROUP"), {}).items()}
        for handle, group in self.d.groups.items():
            members = [self.by_handle[m] for m in group["members"] if m in self.by_handle]
            if members:
                made = f.createIfcGroup(ifcopenshell.guid.new(), None, names.get(handle, f"{handle:X}"), group["description"] or None)
                f.createIfcRelAssignsToGroup(ifcopenshell.guid.new(), None, None, None, members, None, made)

    def unit(self, model_space) -> tuple[int, str]:
        """The drawing's length unit as an INSUNITS code, and where it comes from.

        In order: the caller's; the unit an AutoCAD Architecture drawing was set up in; the declared
        INSUNITS; the model-space block's own; and last the measurement setting, which says only
        imperial or metric and so is a guess between inch and millimetre.
        """
        d = self.d
        declared = d.units if d.units in _UNITS else 0
        named = _UNIT_NAMES.get(declared, "none")
        asked = UNIT_CODES.get(self.heights.get("units"))
        setup = next((u for u in d.aec_units.values() if u in _UNITS), 0)
        block = d.blocks[model_space]["units"] if model_space else 0
        if asked:
            return asked, f"given in the request; the drawing declares {named}"
        if setup:
            clash = "" if setup == declared or not declared else f"; its INSUNITS says {named}, which is not used"
            return setup, f"the drawing's AutoCAD Architecture setup{clash}"
        if declared:
            return declared, "declared by the drawing as INSUNITS"
        if block in _UNITS:
            return block, "the model-space block's own units; the drawing declares none"
        metric = "metric" if d.metric else "imperial"
        return (4 if d.metric else 1), f"not declared; guessed from the drawing's measurement setting, which says only {metric}"

    def tall(self, stored: float, key: str = "storey"):
        """A height for an element read out of flat linework, and where it comes from. None when there is none:
        the drawing's own (the thickness its lines were drawn with) first, then the caller's, and never a guess."""
        if stored > 0:
            return stored, "stored in the drawing, as the thickness of its lines"
        if self.heights.get(key):
            return self.heights[key] / self.metres, "given in the request"
        return None, "not in the drawing and not given in the request, so none is written"

    def outline(self, curve, kind="Curve2D"):
        return self.f.createIfcShapeRepresentation(self.footprint_context, "FootPrint", kind, [curve])

    def spaces(self, storey, owner) -> None:
        """Rooms the drawing outlines and labels, as IfcSpace: a footprint, extruded when a height is known."""
        f = self.f
        shift = self.origin_shift
        made = []
        for room in derive.spaces(self.d, owner, 0.25 / self.metres**2):
            # the outline as drawn, arcs included, in its own plane; the room is placed at the outline's elevation
            curve = self.polycurve([(x - shift[0], y - shift[1]) for x, y in room["points"]], room["bulges"], True, 0.0, _Z, flat=True)
            if curve is None or len(room["outline"]) < 3:
                continue
            height, source = self.tall(room["height"])
            floor = f.createIfcAxis2Placement3D(f.createIfcCartesianPoint((0.0, 0.0, float(room["elevation"] - shift[2]))))
            if height:
                solid = f.createIfcExtrudedAreaSolid(f.createIfcArbitraryClosedProfileDef("AREA", None, curve), None, self.direction(_Z), height)
                shown = f.createIfcShapeRepresentation(self.body_context, "Body", "SweptSolid", [solid])
            else:
                shown = self.outline(curve)
            space = f.create_entity(
                "IfcSpace",
                GlobalId=ifcopenshell.guid.new(),
                Name=room["name"] or None,
                LongName=next((t.split("\n")[0] for t in room["labels"] if t != room["name"]), None),
                ObjectType="ROOM",
                ObjectPlacement=f.createIfcLocalPlacement(storey.ObjectPlacement, floor),
                Representation=f.createIfcProductDefinitionShape(None, None, [shown]),
                CompositionType="ELEMENT",
            )
            # the area AutoCAD itself stored for the outline, when there is one; otherwise ours, measured from it
            stored = next((number for name, _, number in room["stored"] if name == "Area" and number), None)
            square_metres = (stored or room["area"]) * self.metres**2
            quantity = f.createIfcQuantityArea("NetFloorArea", None, None, square_metres, None)
            sizes = f.createIfcElementQuantity(ifcopenshell.guid.new(), None, "Qto_SpaceBaseQuantities", None, None, [quantity])
            f.createIfcRelDefinesByProperties(ifcopenshell.guid.new(), None, None, None, [space], sizes)
            layer = self.d.layers.get(room["layer"], {}).get("name", "")
            self.property_set(
                "DWG_Room",
                {"Labels": " | ".join(room["labels"]), "OutlineLayer": layer, "OutlineHandle": f"{room['handle']:X}",
                 "Height": source}
                # values AutoCAD itself computed and stored in the drawing for this outline, as text in its format
                | {"AreaSource": "stored in the drawing by AutoCAD" if stored else "measured from the outline"}
                | {f"Stored{name}": value for name, value, _ in room["stored"]},
                [space],
            )  # fmt: skip
            self.xdata(room, space)
            self.records(room, space)
            made.append(space)
        if made:
            f.createIfcRelAggregates(ifcopenshell.guid.new(), None, None, None, storey, made)

    def walls(self, storey, owner) -> None:
        """Walls read from pairs of parallel lines on wall layers, as IfcWall: an axis, and a box when a height is known.

        Laid out the standard way (placed at the start of the axis, a rectangle extruded upwards, a material
        layer of the wall's thickness, a type per thickness) so that tools can treat them as real walls.
        Without a height the wall is its axis and its outline in plan.
        """
        f = self.f
        shift = self.origin_shift
        sources = defaultdict(list)
        wanted = lambda name: classify(name) == "IfcWall" and is_plan(name)  # noqa: E731
        layer_name = lambda wall: self.d.layers.get(wall["layer"], {}).get("name", "")  # noqa: E731
        by_thickness = defaultdict(list)
        made = []
        # 7 to 65 cm thick and at least 30 cm long: thinner pairs are frames and glazing, shorter ones jambs
        pieces = derive.walls(self.d, owner, wanted, 0.07 / self.metres, 0.65 / self.metres, 0.3 / self.metres)
        # anything drawn for a door or a window marks the gap it sits in as an opening rather than the end of a wall
        evidence = []
        for e in self.by_owner.get(owner, []):
            block = (self.named(e) or {}).get("name", "") if e["type"] == "INSERT" else ""
            kind = classify(block, self.d.layers.get(e["layer"], {}).get("name", ""))
            if kind in ("IfcDoor", "IfcWindow") and e.get("extrusion", _Z) == _Z:
                points = [e[k] for k in ("point", "start", "end", "center") if k in e] + list(e.get("points", ())[:1])
                evidence += [(p[0], p[1], (kind, e["handle"])) for p in points]
        used = set()
        for wall in derive.join(pieces, evidence, 0.02 / self.metres, 4.0 / self.metres):
            (x1, y1), (x2, y2) = wall["start"], wall["end"]
            length = math.hypot(x2 - x1, y2 - y1)
            along = ((x2 - x1) / length, (y2 - y1) / length, 0.0)
            thickness = wall["thickness"]
            millimetres = round(thickness * self.metres * 1000)  # walls that agree to the millimetre share a type
            start = f.createIfcAxis2Placement3D(
                f.createIfcCartesianPoint((float(x1 - shift[0]), float(y1 - shift[1]), float(-shift[2]))), self.direction(_Z), self.direction(along)
            )
            axis = f.createIfcPolyline([f.createIfcCartesianPoint((0.0, 0.0)), f.createIfcCartesianPoint((length, 0.0))])
            middle = f.createIfcAxis2Placement2D(f.createIfcCartesianPoint((length / 2, 0.0)))
            height, source = self.tall(wall["height"])
            if height:
                solid = f.createIfcExtrudedAreaSolid(f.createIfcRectangleProfileDef("AREA", None, middle, length, thickness), None, self.direction(_Z), height)
                shown = f.createIfcShapeRepresentation(self.body_context, "Body", "SweptSolid", [solid])
            else:
                half = thickness / 2
                ring = [f.createIfcCartesianPoint(p) for p in ((0.0, -half), (length, -half), (length, half), (0.0, half))]
                shown = self.outline(f.createIfcPolyline([*ring, ring[0]]))
            product = f.create_entity(
                "IfcWall",
                GlobalId=ifcopenshell.guid.new(),
                Name=f"Wall {millimetres} mm",
                ObjectType="derived from plan linework",
                ObjectPlacement=f.createIfcLocalPlacement(storey.ObjectPlacement, start),
                Representation=f.createIfcProductDefinitionShape(
                    None,
                    None,
                    # body first: some readers draw whichever representation is listed first
                    [shown, f.createIfcShapeRepresentation(self.axis_context, "Axis", "Curve2D", [axis])],
                ),
            )
            by_thickness[millimetres].append((product, thickness))
            for distance, width, (kind, handle) in wall["openings"]:
                # a plan gives an opening's width only: it is cut when the caller says how high it is, and where
                tall = self.heights.get("door" if kind == "IfcDoor" else "window")
                sill = 0.0 if kind == "IfcDoor" else self.heights.get("sill")
                if not height or not tall or sill is None:
                    continue
                sill, tall = sill / self.metres, tall / self.metres

                def box(depth, width=width, tall=tall):
                    solid = f.createIfcExtrudedAreaSolid(f.createIfcRectangleProfileDef("AREA", None, None, width, depth), None, self.direction(_Z), tall)
                    return f.createIfcProductDefinitionShape(None, None, [f.createIfcShapeRepresentation(self.body_context, "Body", "SweptSolid", [solid])])

                def in_wall(distance=distance, width=width, sill=sill, product=product):
                    at = f.createIfcAxis2Placement3D(f.createIfcCartesianPoint((distance + width / 2, 0.0, sill)))
                    return f.createIfcLocalPlacement(product.ObjectPlacement, at)

                opening = f.create_entity(
                    "IfcOpeningElement", GlobalId=ifcopenshell.guid.new(), Name="Opening", ObjectPlacement=in_wall(),
                    Representation=box(thickness), PredefinedType="OPENING",
                )  # fmt: skip
                f.createIfcRelVoidsElement(ifcopenshell.guid.new(), None, None, None, product, opening)
                filler = self.by_handle.get(handle)
                if filler is None or not filler.is_a(kind) or handle in used:
                    filler = f.create_entity(
                        kind, GlobalId=ifcopenshell.guid.new(), Name=kind[3:], ObjectType="derived from plan linework",
                        ObjectPlacement=in_wall(), Representation=box(thickness),  # a block filling the opening
                    )  # fmt: skip
                    made.append(filler)
                    self.described[layer_name(wall), f"derived {kind[3:].lower()}", "by layer", "by layer"].append(filler)
                used.add(handle)
                filler.OverallWidth, filler.OverallHeight = width, tall
                f.createIfcRelFillsElement(ifcopenshell.guid.new(), None, None, None, opening, filler)
            layer = self.d.layers.get(wall["layer"], {}).get("name", "")
            self.described[layer, "derived wall", "by layer", "by layer"].append(product)
            made.append(product)
            sources[source].append(product)
        if not made:
            return
        f.createIfcRelContainedInSpatialStructure(ifcopenshell.guid.new(), None, None, None, made, storey)
        source = "two parallel lines on a wall layer, taken as the faces of a wall; gaps that hold a door or window are openings"
        fillers = [product for product in made if not product.is_a("IfcWall")]
        if fillers:
            self.property_set("DWG_Derived", {"Source": source, "Height": "given in the request"}, fillers)
        for height_source, group in sources.items():
            self.property_set("DWG_Derived", {"Source": source, "Height": height_source}, group)
        material = f.createIfcMaterial("Unspecified", None, None)
        for millimetres, group in by_thickness.items():
            name = f"Wall {millimetres} mm"
            occurrences, thickness = [product for product, _ in group], group[0][1]
            kind = ifcopenshell.api.root.create_entity(f, ifc_class="IfcWallType", name=name)
            f.createIfcRelDefinesByType(ifcopenshell.guid.new(), None, None, None, occurrences, kind)
            layers = f.createIfcMaterialLayerSet([f.createIfcMaterialLayer(material, thickness, None)], name, None)
            f.createIfcRelAssociatesMaterial(ifcopenshell.guid.new(), None, None, None, [kind], layers)
            # the layer set is centred on the axis
            usage = f.createIfcMaterialLayerSetUsage(layers, "AXIS2", "POSITIVE", -thickness / 2, None)
            f.createIfcRelAssociatesMaterial(ifcopenshell.guid.new(), None, None, None, occurrences, usage)

    def architecture(self, storey, owner) -> None:
        """Walls, doors, windows and openings kept as AutoCAD Architecture objects, with the sizes the drawing stores.

        Nothing here is assumed or measured from linework: height, width, length and position are the object's own.
        Laid out like the derived walls (placed at the start of the axis, a rectangle extruded upwards).
        """
        f, shift, d = self.f, self.origin_shift, self.d
        dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
        styles = {handle: name for key, entries in self.root.items() if key.startswith("AEC_") and key.endswith("_STYLES")
                  for name, handle in d.dictionaries.get(entries, {}).items()}  # fmt: skip
        named = {handle: name for key, entries in self.root.items() if key in ("AEC_MATERIAL_DEFS", "AEC_PROPERTY_SET_DEFS")
                 for name, handle in d.dictionaries.get(entries, {}).items()}  # fmt: skip
        mine = [e for e in self.by_owner.get(owner, []) if "aec" in e]
        walls, made, typed = {}, [], defaultdict(list)
        values = self.stored_set

        def schedule(handle):
            """The Architecture property sets attached to an object or style, named by their definitions."""
            for stored in d.property_sets.values():
                definition = d.property_set_defs.get(stored["definition"], {})
                if stored["on"] == handle and stored["definition"] in named:
                    names = definition.get("names", {})
                    filled = {names.get(number, f"Property {number}"): value for number, value in stored["values"].items()}
                    if made_set := values(named[stored["definition"]], definition.get("description"), filled):
                        yield made_set

        def scaled(size, width):
            """A wall component's size, by the rule AutoCAD applies (AecInstanceBasedValue::calcActualValue)."""
            base, operand, operator = size["base"], size["operand"], size["operator"]
            if not size["follows"] or operator == 2 and not operand:
                return base
            return base + (operand + width, width - operand, width / (operand or 1), operand * width)[operator] if operator in range(4) else base

        def shape(*representations):
            return f.createIfcProductDefinitionShape(None, None, list(representations))

        def box(length, depth, tall, centre=(0.0, 0.0)):
            at = f.createIfcAxis2Placement2D(f.createIfcCartesianPoint(tuple(map(float, centre))))
            solid = f.createIfcExtrudedAreaSolid(f.createIfcRectangleProfileDef("AREA", None, at, float(length), float(depth)), None, self.direction(_Z), float(tall))
            return f.createIfcShapeRepresentation(self.body_context, "Body", "SweptSolid", [solid])

        def describe(e, product, sizes):
            a = e["aec"]
            style = styles.get(a["style"], "")
            layer = d.layers.get(e["layer"], {}).get("name", "0")
            self.described[layer, e["type"], "by layer", "by layer"].append(product)
            self.property_set("DWG_Architecture", {"Style": style, "Source": "AutoCAD Architecture object; sizes as stored in the drawing"} | sizes, [product])
            typed[product.is_a(), a["style"]].append(product)
            made.append(product)
            for stored in schedule(e["handle"]):
                f.createIfcRelDefinesByProperties(ifcopenshell.guid.new(), None, None, None, [product], stored)

        for e in mine:
            a = e["aec"]
            if e["type"] != "AEC_WALL" or "length" not in a or min(a["length"], a["width"], a["height"]) <= 0:
                continue
            length, width, height = a["length"], a["width"], a["height"]
            # where the wall's body lies across its baseline: the stored box says, else the justification (0.5 is centred)
            side = a["bounds"][0][1] if "bounds" in a else -width * a["justification"]
            x, _, z = a["axes"]
            start = f.createIfcAxis2Placement3D(f.createIfcCartesianPoint(tuple(float(c - s) for c, s in zip(a["origin"], shift))), self.direction(z), self.direction(x))
            axis = f.createIfcPolyline([f.createIfcCartesianPoint((0.0, 0.0)), f.createIfcCartesianPoint((float(length), 0.0))])
            wall = f.create_entity(
                "IfcWall", GlobalId=ifcopenshell.guid.new(), Name=f"Wall {styles.get(a['style'], '')}".strip(), ObjectType="AEC_WALL", Tag=f"{e['handle']:X}",
                ObjectPlacement=f.createIfcLocalPlacement(storey.ObjectPlacement, start),
                Representation=shape(box(length, width, height, (length / 2, side + width / 2)), f.createIfcShapeRepresentation(self.axis_context, "Axis", "Curve2D", [axis])),
            )  # fmt: skip
            walls[e["handle"]] = (wall, a, side)
            describe(e, wall, {"Length": length, "Width": width, "Height": height, "Justification": a["justification"]})

        for e in mine:
            a = e["aec"]
            if e["type"] == "AEC_WALL" or min(a["width"], a["height"]) <= 0:
                continue
            host = walls.get(d.anchors.get(a["anchor"]))
            corner, edges = (a["bounds"][0], a["bounds"][1:]) if "bounds" in a else ((0.0, 0.0, 0.0), ((a["width"], 0, 0), (0, 0, 0), (0, 0, a["height"])))
            across, deep, tall = edges[0][0], edges[1][1], edges[2][2]
            kind = {"AEC_DOOR": "IfcDoor", "AEC_WINDOW": "IfcWindow"}.get(e["type"])
            if host is None:
                if kind:  # not in a wall: stands where its own placement puts it
                    x, _, z = a["axes"]
                    at = f.createIfcAxis2Placement3D(f.createIfcCartesianPoint(tuple(float(c - s) for c, s in zip(a["origin"], shift))), self.direction(z), self.direction(x))
                    placement = f.createIfcLocalPlacement(storey.ObjectPlacement, at)
                    body = box(across, deep or a["width"] / 20, tall, (corner[0] + across / 2, corner[1] + deep / 2))
            else:
                wall, w, side = host
                offset = tuple(p - q for p, q in zip(a["origin"], w["origin"]))
                along, up = dot(offset, w["axes"][0]), dot(offset, w["axes"][2])
                ends = sorted(along + dot(a["axes"][0], w["axes"][0]) * c for c in (corner[0], corner[0] + across))
                middle = ((ends[0] + ends[1]) / 2, side + w["width"] / 2, up + corner[2])
                placement = f.createIfcLocalPlacement(wall.ObjectPlacement, f.createIfcAxis2Placement3D(f.createIfcCartesianPoint(tuple(map(float, middle)))))
                opening = f.create_entity(
                    "IfcOpeningElement", GlobalId=ifcopenshell.guid.new(), Name="Opening", ObjectPlacement=placement,
                    Representation=shape(box(ends[1] - ends[0], w["width"], tall)), PredefinedType="OPENING",
                )  # fmt: skip
                f.createIfcRelVoidsElement(ifcopenshell.guid.new(), None, None, None, wall, opening)
            if not kind:
                continue  # a bare opening: the hole is all there is
            if host is not None:
                # the filling is drawn as a block as deep as its stored box, but no deeper than the wall it is in
                body = box(ends[1] - ends[0], min(deep, w["width"]) or w["width"], tall)
            filler = f.create_entity(
                kind, GlobalId=ifcopenshell.guid.new(), Name=f"{kind[3:]} {styles.get(a['style'], '')}".strip(), ObjectType=e["type"], Tag=f"{e['handle']:X}",
                ObjectPlacement=placement, Representation=shape(body), OverallWidth=float(a["width"]), OverallHeight=float(a["height"]),
            )  # fmt: skip
            if host is not None:
                f.createIfcRelFillsElement(ifcopenshell.guid.new(), None, None, None, opening, filler)
            describe(e, filler, {"Width": a["width"], "Height": a["height"], "Rise": a["rise"]})
        if made:
            f.createIfcRelContainedInSpatialStructure(ifcopenshell.guid.new(), None, None, None, made, storey)
        materials = {}
        for (ifc_class, handle), occurrences in typed.items():
            if handle not in styles:
                continue
            # the style is the type: every wall of style "Brick-X" is an occurrence of it
            kind = ifcopenshell.api.root.create_entity(f, ifc_class=ifc_class + "Type", name=styles[handle])
            f.createIfcRelDefinesByType(ifcopenshell.guid.new(), None, None, None, occurrences, kind)
            style = d.aec_styles.get(handle, {})
            kind.Description = style.get("description") or None
            sizes = values("DWG_ArchitectureStyle", "Sizes stored in the AutoCAD Architecture style", style.get("sizes", {}), "IfcLengthMeasure")
            kind.HasPropertySets = [stored for stored in (sizes, *schedule(handle)) if stored] or None
            by_width = defaultdict(list)
            for product in occurrences if style.get("components") else ():
                _, a, side = walls[int(product.Tag, 16)]
                by_width[a["width"], round(side, 6)].append(product)  # rounded: stored boxes differ in the last digits
            for (width, side), group in by_width.items():
                # the style's layers at this wall width, in the order they lie across the wall
                parts = sorted(((scaled(c["offset"], width), scaled(c["width"], width), c) for c in style["components"]), key=lambda part: part[0])
                if any(thick <= 0 for _, thick, _ in parts):
                    continue
                layers = f.createIfcMaterialLayerSet([
                    f.createIfcMaterialLayer(materials.setdefault(c["material"], f.createIfcMaterial(named.get(c["material"], "Unnamed"), None, None)), float(thick), None, c["name"])
                    for _, thick, c in parts], styles[handle], None)  # fmt: skip
                # the layers start at the side of the wall's own stored box; the style only gives their order and widths
                usage = f.createIfcMaterialLayerSetUsage(layers, "AXIS2", "POSITIVE", float(side), None)
                f.createIfcRelAssociatesMaterial(ifcopenshell.guid.new(), None, None, None, group, usage)
                if len(by_width) == 1:
                    f.createIfcRelAssociatesMaterial(ifcopenshell.guid.new(), None, None, None, [kind], layers)

    def columns(self, storey, owner) -> None:
        """Columns: closed shapes on column layers, extruded when a height is known."""
        f = self.f
        shift = self.origin_shift
        wanted = lambda name: classify(name) == "IfcColumn" and is_plan(name)  # noqa: E731
        made, sources = [], defaultdict(list)
        # between 10 cm and 2 m across: smaller is a mark, larger is not a column
        for shape in derive.footprints(self.d, owner, wanted, (0.1 / self.metres) ** 2, (2.0 / self.metres) ** 2):
            if "radius" in shape:
                cx, cy = shape["center"]
                profile = f.createIfcCircleProfileDef("AREA", None, None, float(shape["radius"]))
                flat = f.createIfcCircle(f.createIfcAxis2Placement2D(f.createIfcCartesianPoint((0.0, 0.0))), float(shape["radius"]))
            else:
                outline = shape["outline"] if derive.area(shape["outline"]) > 0 else shape["outline"][::-1]
                cx, cy = outline[0]
                corners = [f.createIfcCartesianPoint(p) for p in _distinct([(x - cx, y - cy) for x, y in outline])]
                if len(corners) < 3:
                    continue
                flat = f.createIfcPolyline([*corners, corners[0]])
                profile = f.createIfcArbitraryClosedProfileDef("AREA", None, flat)
            base = f.createIfcAxis2Placement3D(
                f.createIfcCartesianPoint((float(cx - shift[0]), float(cy - shift[1]), float(shape["elevation"] - shift[2])))
            )
            height, source = self.tall(shape["height"])
            if height:
                shown = f.createIfcShapeRepresentation(self.body_context, "Body", "SweptSolid", [f.createIfcExtrudedAreaSolid(profile, None, self.direction(_Z), height)])
            else:
                shown = self.outline(flat)
            product = f.create_entity(
                "IfcColumn",
                GlobalId=ifcopenshell.guid.new(),
                Name=f"COLUMN:{shape['handle']:X}",
                ObjectType="derived from plan linework",
                Tag=f"{shape['handle']:X}",
                ObjectPlacement=f.createIfcLocalPlacement(storey.ObjectPlacement, base),
                Representation=f.createIfcProductDefinitionShape(None, None, [shown]),
            )
            sources[source].append(product)
            self.described[self.d.layers.get(shape["layer"], {}).get("name", ""), "derived column", "by layer", "by layer"].append(product)
            made.append(product)
        if made:
            f.createIfcRelContainedInSpatialStructure(ifcopenshell.guid.new(), None, None, None, made, storey)
            for source, group in sources.items():
                self.property_set("DWG_Derived", {"Source": "a closed shape on a column layer, taken as the column's section", "Height": source}, group)

    def element_class(self, e, items) -> str:
        """Solids and meshes are things in the model, and so is a block whose name or layer says what it is.
        Everything else is drafting."""
        layer = self.d.layers.get(e["layer"], {}).get("name", "")
        if e["type"] == "LIGHT":
            return "IfcLightFixture"
        if e.get("thickness") and "bodies" not in e and any(_kind(i) in _BODY_KINDS for i in items):
            # a line drawn with a thickness is one face of something, not the thing: two of them make one wall
            return "IfcBuildingElementProxy"
        if any(_kind(i) in _BODY_KINDS or self.is_body(i) for i in items):
            return classify((self.named(e) or {}).get("name", ""), layer) or "IfcBuildingElementProxy"
        if e["type"] in ("INSERT", "MINSERT"):
            by_layer = classify(layer)
            return classify((self.named(e) or {}).get("name", "")) or (by_layer if by_layer in _PLACED else None) or "IfcAnnotation"
        return "IfcAnnotation"

    def named(self, e):
        """The block an insert is called by: for an adjusted dynamic block, the block the user chose, not its `*U` copy."""
        return self.d.blocks.get(e.get("named", e.get("block")))

    def is_body(self, item) -> bool:
        return item.is_a("IfcMappedItem") and item.MappingSource.MappedRepresentation.RepresentationIdentifier == "Body"

    def give_type(self, e, product, items) -> None:
        """A named block is a type and each insert of it an occurrence, as a family and its instances."""
        block = self.named(e)
        if not product.is_a("IfcElement") or not block or block["name"].startswith("*") or e["type"] != "INSERT":
            return
        key = (e.get("named", e["block"]), product.is_a())
        if key not in self.types:
            self.types[key] = ifcopenshell.api.root.create_entity(self.f, ifc_class=product.is_a() + "Type", name=block["name"])
            if "named" not in e:  # inserts of a dynamic block each have their own shape, so the type has none
                self.types[key].RepresentationMaps = [self.maps[e["block"]]]
        self.typed[self.types[key]].append(product)

    def representation(self, items, footprint=False):
        groups = defaultdict(list)
        for item in items:
            groups[_kind(item)].append(item)
        if len(groups) > 1:
            # IFC has no representation type for mixed content, so each kind of geometry goes into
            # its own nested map and the representation holds mapped items only
            items = groups.pop("MappedRepresentation", [])
            for same_kind in groups.values():
                nested = self.f.createIfcRepresentationMap(self.origin, self.representation(same_kind))
                items.append(self.f.createIfcMappedItem(nested, self.identity))
            groups = {"MappedRepresentation": items}
        ((kind, items),) = groups.items()
        if kind in _BODY_KINDS or any(self.is_body(i) for i in items):
            return self.f.createIfcShapeRepresentation(self.body_context, "Body", kind, items)
        if footprint:  # an element known only by its plan symbol
            return self.f.createIfcShapeRepresentation(self.footprint_context, "FootPrint", kind, items)
        return self.f.createIfcShapeRepresentation(self.context, "Annotation", kind, items)

    def xyz(self, p):
        return tuple(float(c) - s for c, s in zip(p, self.shift))

    def point(self, p):
        return self.f.createIfcCartesianPoint(self.xyz(p))

    def direction(self, v):
        return self.f.createIfcDirection(tuple(map(float, v)))

    def placement(self, axes, origin):
        return self.f.createIfcAxis2Placement3D(self.point(origin), self.direction(axes[2]), self.direction(axes[0]))

    def block(self, handle, parents):
        """The IfcRepresentationMap for a block, built on first use. None if it has nothing to draw."""
        if handle in self.maps:
            return self.maps[handle]
        if handle in parents:  # a block that contains itself
            return None
        items = [i for e in self.by_owner.get(handle, []) for i in self.items(e, (*parents, handle))]
        result = self.f.createIfcRepresentationMap(self.origin, self.representation(items)) if items else None
        self.maps[handle] = result
        return result

    def items(self, e, parents) -> list:
        """One DWG entity as IFC representation items, in its block's coordinates."""
        kind = e["type"]
        name = "parts" if "parts" in e else "dimension" if kind.startswith("DIMENSION") else _METHODS.get(kind, kind.lower())
        outer, self.shift = self.shift, (self.origin_shift if not parents else (0.0, 0.0, 0.0))
        try:
            found = self.standing(e) if e.get("thickness") and "parts" not in e else None
            if found is None:
                found = getattr(self, "_" + name)(e, parents)
        except (ValueError, ZeroDivisionError, OverflowError):  # geometry too broken to express
            found = None
        finally:
            self.shift = outer
        found = [] if found is None else found if isinstance(found, list) else [found]
        if not found and e.get("picture"):  # its block is missing: draw the picture of itself the entity carries
            return self.items({k: v for k, v in e.items() if k != "picture"} | {"parts": proxy.decode(e["picture"], self.d.release)}, parents)
        layer = self.d.layers.get(e["layer"], {})
        self.layer_items[layer.get("name", "0")] += found
        self.style(e, layer, found)
        return found

    def linetype(self, e, layer) -> dict:
        """The entity's linetype, or its layer's: {name, dashes}. Empty for a plain solid line."""
        own = e.get("linetype")
        return self.d.linetypes.get(own if isinstance(own, int) else layer.get("linetype") if own is None else None, {})

    def curve_font(self, linetype, scale=1.0):
        """Dashes and gaps as an IfcCurveStyleFont, or None for a solid line. `scale` is the drawing's linetype
        scale times the entity's own: the pattern is drawn that many times its defined size."""
        dashes = [d * scale for d in linetype.get("dashes") or []]
        key = ("font", linetype.get("name"), tuple(dashes))
        if key not in self.styles:
            pairs = [(max(d, 0.0), -gap) for d, gap in zip(dashes, dashes[1:] + dashes[:1]) if d >= 0 and gap < 0]
            pattern = [self.f.createIfcCurveStyleFontPattern(float(visible), float(invisible)) for visible, invisible in pairs]
            self.styles[key] = self.f.createIfcCurveStyleFont(linetype.get("name"), pattern) if pattern else None
        return self.styles[key]

    def style(self, e, layer, items) -> None:
        """Colour, line weight and linetype, from the entity or else its layer."""
        # shortcut: "by block" also takes the layer's value, since one shared block definition cannot vary per insert
        color = e["color"] or layer.get("color", 7)
        code = e["lineweight"] if e["lineweight"] < 29 else layer.get("lineweight", 31)
        width = _LINEWEIGHTS[code] / 100 * self.millimetre if code < len(_LINEWEIGHTS) and _LINEWEIGHTS[code] else None
        if e.get("width", 0) > 0:
            width = e["width"]  # a wide polyline: its drawn width, in drawing units, rather than a plotting weight
        font = self.curve_font(self.linetype(e, layer), self.d.ltscale * e.get("linetype_scale", 1.0))
        f = self.f
        for item in items:
            kind = _kind(item)
            if kind == "Text":
                self.text_style(e, _rgb(color), item)
            if kind in ("MappedRepresentation", "Text", "Point"):
                continue  # a block's contents are styled where the block is defined
            surface = kind in _BODY_KINDS
            rgb = (255, 255, 255) if surface and color == 7 else _rgb(color)  # 7 on a solid: white, so it can be shaded
            curve = not surface and kind != "FillArea"
            key = ("Curve" if curve else kind, rgb, width if curve else None, font.id() if curve and font else None)
            pattern = e.get("pattern") if kind == "FillArea" else None
            if pattern:  # a hatch drawn with lines rather than filled: the families of lines it is drawn with
                key = ("Hatch", rgb, tuple((line["angle"], line["origin"], line["offset"], tuple(line["dashes"])) for line in pattern))
            if key not in self.styles:
                colour = f.createIfcColourRgb(None, *(c / 255 for c in rgb))
                if pattern:
                    families = []
                    for line in pattern:
                        step = math.hypot(*line["offset"])
                        if step <= 0:
                            continue
                        drawn = f.createIfcCurveStyle(None, self.curve_font({"name": None, "dashes": line["dashes"]}), None, colour, None)
                        apart = f.createIfcVector(f.createIfcDirection(tuple(c / step for c in line["offset"])), step)
                        start = f.createIfcCartesianPoint(tuple(map(float, line["origin"])))
                        families.append(f.createIfcFillAreaStyleHatching(drawn, apart, None, start, float(line["angle"])))
                    self.styles[key] = f.createIfcFillAreaStyle(e.get("pattern_name") or None, families or [colour], None)
                elif surface:
                    self.styles[key] = f.createIfcSurfaceStyle(None, "BOTH", [f.createIfcSurfaceStyleShading(colour, None)])
                elif kind == "FillArea":
                    self.styles[key] = f.createIfcFillAreaStyle(None, [colour], None)
                else:
                    thickness = f.createIfcPositiveLengthMeasure(width) if width else None
                    self.styles[key] = f.createIfcCurveStyle(None, font, thickness, colour, None)
            f.createIfcStyledItem(item, [self.styles[key]], None)

    def text_style(self, e, rgb, item) -> None:
        """The colour, font and size text is drawn with, from the entity and its text style."""
        f = self.f
        named = self.d.styles.get(e.get("style"), {})
        height = float(e.get("height") or item.Extent.SizeInY)  # a part of a custom entity has only its box
        key = ("Text", rgb, named.get("name"), height)
        if key not in self.styles:
            colour = f.createIfcColourRgb(None, *(c / 255 for c in rgb))
            # shortcut: text with no style of its own (a dimension's, a proxy's) is given AutoCAD's default font
            name = named.get("font") or "txt"
            slant = "oblique" if named.get("oblique") else None
            font = f.createIfcTextStyleFontModel(name, [name], slant, None, None, f.createIfcLengthMeasure(height))
            self.styles[key] = f.createIfcTextStyle(named.get("name"), f.createIfcTextStyleForDefinedFont(colour, None), None, font, None)
        f.createIfcStyledItem(item, [self.styles[key]], None)

    def polyline(self, points):
        points = _distinct(points)
        return self.f.createIfcPolyline([self.point(p) for p in points]) if len(points) > 1 else None

    def polycurve(self, points, bulges, closed, elevation, extrusion, flat=False):
        """A 2D polyline with bulge arcs, as LWPOLYLINE, POLYLINE_2D and hatch loops store it.
        `flat` leaves it in its own plane, as a 2D curve, for use as a profile."""
        if len(points) < 2:
            return None
        axes = _ocs(extrusion)
        bulges = list(bulges) + [0.0] * (len(points) - len(bulges))
        coords, segments = [points[0]], []
        for i in range(len(points) if closed else len(points) - 1):
            (x1, y1), (x2, y2) = coords[-1], points[(i + 1) % len(points)]
            if _same((x1, y1), (x2, y2)):
                continue
            start = len(coords)
            if bulges[i]:
                # arc through the point where the bulge is deepest: half the chord times the bulge, off the midpoint
                half = bulges[i] / 2
                coords.append(((x1 + x2) / 2 + half * (y2 - y1), (y1 + y2) / 2 - half * (x2 - x1)))
                segments.append(self.f.createIfcArcIndex((start, start + 1, start + 2)))
            else:
                segments.append(self.f.createIfcLineIndex((start, start + 1)))
            coords.append((x2, y2))
        if not segments:
            return None
        if flat:
            listed = self.f.createIfcCartesianPointList2D([tuple(map(float, p)) for p in coords])
            return self.f.createIfcIndexedPolyCurve(listed, segments, False)
        listed = self.f.createIfcCartesianPointList3D([self.xyz(_to_wcs(axes, (x, y, elevation))) for x, y in coords])
        return self.f.createIfcIndexedPolyCurve(listed, segments, False)

    def standing(self, e):
        """An entity drawn with a thickness: the surface that stands on its curve, as high as the thickness.

        This is how a flat drawing stores a height, so it is written as the 3D it describes: a flat
        panel on a line, part of a cylinder on an arc, a solid block on a filled shape. Written the way
        solids are (faces on exact surfaces, bounded by edges), which geometry kernels handle; a bare
        swept surface makes IfcOpenShell hang. None for entity types that have no such form here.
        """
        f, tall, kind = self.f, e["thickness"], e["type"]
        normal = _unit(e.get("extrusion", _Z))
        axes = _ocs(normal)
        world = lambda x, y, z: _to_wcs(axes, (x, y, z))  # noqa: E731
        lift = lambda p: tuple(c + tall * n for c, n in zip(p, normal))  # noqa: E731
        pieces = []  # (start, end, None) for a straight run; (start, end, centre, radius) for an arc, anticlockwise
        if kind == "LINE":
            pieces.append((e["start"], e["end"], None))
        elif kind in ("CIRCLE", "ARC") and e["radius"] > 0:
            (cx, cy, cz), radius = e["center"], e["radius"]
            from_, to = (e["start_angle"], e["end_angle"]) if kind == "ARC" else (0.0, 0.0)
            on = lambda angle: world(cx + radius * math.cos(angle), cy + radius * math.sin(angle), cz)  # noqa: E731
            pieces.append((on(from_), on(to), world(cx, cy, cz), radius))
        elif kind in ("SOLID", "TRACE"):  # a filled shape with a thickness is a solid block
            a, b, c, d = e["corners"]
            outline = self.polycurve([a, b, d, c], (), True, 0.0, _Z, flat=True)
            base = self.placement(axes, world(0.0, 0.0, e["elevation"] + min(tall, 0.0)))
            return outline and f.createIfcExtrudedAreaSolid(f.createIfcArbitraryClosedProfileDef("AREA", None, outline), base, self.direction(_Z), abs(tall))
        elif kind in ("LWPOLYLINE", "POLYLINE_2D"):
            points, height = [p[:2] for p in e["points"]], e["elevation"]
            bulges = list(e["bulges"]) + [0.0] * len(points)
            for i in range(len(points) if e["closed"] else len(points) - 1):
                (x1, y1), (x2, y2), bulge = points[i], points[(i + 1) % len(points)], bulges[i]
                if _same((x1, y1), (x2, y2)):
                    continue
                if not bulge:
                    pieces.append((world(x1, y1, height), world(x2, y2, height), None))
                    continue
                # the bulge is the tangent of a quarter of the arc's angle; its centre lies off the chord's middle
                back = (1 - bulge * bulge) / (4 * bulge)
                cx, cy = (x1 + x2) / 2 - back * (y2 - y1), (y1 + y2) / 2 + back * (x2 - x1)
                ends = ((x1, y1), (x2, y2)) if bulge > 0 else ((x2, y2), (x1, y1))  # edges run anticlockwise
                pieces.append((*(world(x, y, height) for x, y in ends), world(cx, cy, height), math.hypot(x1 - cx, y1 - cy)))
        up = tuple(n * math.copysign(1.0, tall) for n in normal)
        made, key = [], 0
        for start, end, *arc in pieces:
            if arc[0] is None:
                along = _unit(tuple(q - p for p, q in zip(start, end)))
                surface, bottom, top = ("plane", start, _unit(_cross(along, normal)), along), ("line", start, along), ("line", lift(start), along)
            else:
                centre, major = arc[0], tuple(arc[1] * c for c in axes[0])
                surface = ("cone", centre, normal, major, 1.0, 0.0, 1.0)  # a cylinder: a cone that does not narrow
                bottom, top = ("ellipse", centre, normal, major, 1.0), ("ellipse", lift(centre), normal, major, 1.0)
            if _same(start, end):  # a whole circle: its two rims bound the face
                loops = [[(key, start, start, bottom, True, True)], [(key + 1, lift(start), lift(start), top, True, False)]]
            else:
                loops = [[(key, start, end, bottom, True, True), (key + 1, end, lift(end), ("line", end, up), True, True),
                          (key + 2, lift(start), lift(end), top, True, False), (key + 3, start, lift(start), ("line", start, up), True, False)]]  # fmt: skip
            made.append({"surface": surface, "same_sense": True, "double": True, "loops": loops})
            key += 4
        return self.acis_body({"faces": made}, kind) if made else None

    def faces(self, points, faces):
        """A face set from points and 1-based index loops; loops that collapse to fewer than 3 corners are dropped."""
        loops = []
        for face in faces:
            loop = [i for n, i in enumerate(face) if 1 <= i <= len(points) and i not in face[:n]]
            if len(loop) >= 3:
                loops.append(self.f.createIfcIndexedPolygonalFace(loop))
        if not loops:
            return None
        listed = self.f.createIfcCartesianPointList3D([self.xyz(p) for p in points])
        return self.f.createIfcPolygonalFaceSet(listed, None, loops)

    def _line(self, e, _):
        return self.polyline([e["start"], e["end"]])

    def _ray(self, e, _):
        """A construction line, which has no end. IFC's IfcLine is likewise unbounded; a ray is written the same
        way, from its start point, since IFC has no half-line."""
        if not any(e["direction"]):
            return None
        return self.f.createIfcLine(self.point(e["point"]), self.f.createIfcVector(self.direction(_unit(e["direction"])), 1.0))

    _xline = _ray

    def _leader(self, e, _):
        return self.polyline(e["points"])

    def _point(self, e, _):
        return self.point(e["point"])

    def _underlay(self, e, _):
        """Its clip outline, or just its insertion point: the page's size is in the other file, not the drawing."""
        if len(e["points"]) > 1:
            return self.polycurve(e["points"], (), True, e["elevation"], e["extrusion"])
        return self.point(_to_wcs(_ocs(e["extrusion"]), e["point"]))

    def _circle(self, e, _):
        axes = _ocs(e["extrusion"])
        if e["radius"] <= 0:
            return None
        return self.f.createIfcCircle(self.placement(axes, _to_wcs(axes, e["center"])), e["radius"])

    def trimmed(self, curve, start, end):
        value = self.f.createIfcParameterValue
        return self.f.createIfcTrimmedCurve(curve, [value(start)], [value(end)], True, "PARAMETER")

    def _arc(self, e, _):
        circle = self._circle(e, _)
        return circle and self.trimmed(circle, e["start_angle"], e["end_angle"])

    def _ellipse(self, e, _):
        major = math.sqrt(sum(c * c for c in e["major"]))
        if not major or e["ratio"] <= 0:
            return None
        placement = self.placement((_unit(e["major"]), None, _unit(e["extrusion"])), e["center"])
        if e["ratio"] == 1.0:
            ellipse = self.f.createIfcCircle(placement, major)
        else:
            ellipse = self.f.createIfcEllipse(placement, major, major * e["ratio"])
        if abs(e["end_angle"] - e["start_angle"] - 2 * math.pi) < 1e-9:
            return ellipse
        return self.trimmed(ellipse, e["start_angle"], e["end_angle"])

    def _lwpolyline(self, e, _):
        points = [p[:2] for p in e["points"]]  # POLYLINE_2D vertices carry a z that is always 0
        return self.polycurve(points, e["bulges"], e["closed"], e["elevation"], e["extrusion"])

    def _polyline_3d(self, e, _):
        return self.polyline(e["points"] + e["points"][:1] if e["closed"] else e["points"])

    def _polyline_pface(self, e, _):
        return self.faces(e["points"], [[abs(i) for i in face] for face in e["faces"]])

    def _polyline_mesh(self, e, _):
        m, n = e["m"], e["n"]
        if m * n > len(e["points"]):
            return None
        at = lambda i, j: (i % m) * n + (j % n) + 1  # noqa: E731
        quads = [
            [at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1)]
            for i in range(m if e["closed_m"] else m - 1)
            for j in range(n if e["closed_n"] else n - 1)
        ]
        return self.faces(e["points"], quads)

    def _face3d(self, e, _):
        return self.faces(_distinct(e["corners"]), [[1, 2, 3, 4]])

    def axes(self, origin, z, x):
        """A placement from a normal and a reference direction that may be missing or not square to it."""
        return self.placement(_frame(z, x), origin)

    def nurbs_knots(self, knots, counts, degree):
        """ACIS repeats end knots `degree` times; IFC wants one more. Returns None if the result is not valid."""
        distinct, total = [], []
        for k, m in zip(knots, counts):
            if distinct and math.isclose(k, distinct[-1], rel_tol=1e-12, abs_tol=1e-12):
                total[-1] += m
            else:
                distinct.append(float(k))
                total.append(int(m))
        if len(distinct) < 2 or distinct != sorted(distinct):
            return None
        total[0] += 1
        total[-1] += 1
        if max(total[0], total[-1]) > degree + 1 or any(m > degree for m in total[1:-1]):
            return None
        return total, distinct

    def acis_curve(self, curve):
        if curve[0] == "line":
            if not any(curve[2]):
                return None
            return self.f.createIfcLine(self.point(curve[1]), self.f.createIfcVector(self.direction(_unit(curve[2])), 1.0))
        if curve[0] == "ellipse":
            _, center, normal, major, ratio = curve
            radius = math.sqrt(sum(c * c for c in major))
            if not radius or ratio <= 0:
                return None
            placement = self.axes(center, normal, major)
            if math.isclose(ratio, 1.0, rel_tol=1e-12):
                return self.f.createIfcCircle(placement, radius)
            return self.f.createIfcEllipse(placement, radius, radius * ratio)
        _, degree, knots, counts, points = curve
        fixed = self.nurbs_knots(knots, counts, degree)
        if fixed is None or len(points) <= degree:
            return None
        args = [degree, [self.point(p[:3]) for p in points], "UNSPECIFIED", False, False, *fixed, "UNSPECIFIED"]
        if len(points[0]) == 4:
            return self.f.createIfcRationalBSplineCurveWithKnots(*args, [float(p[3]) for p in points])
        return self.f.createIfcBSplineCurveWithKnots(*args)

    def revolution(self, center, axis, reference, profile):
        """IfcSurfaceOfRevolution of a 2D (radius, height) curve about an axis."""
        x, _, z = _frame(axis, reference)
        f = self.f
        position = self.placement((x, None, _cross(x, z)), center)  # profile plane: X is the radius, Y the axis
        # the axis is given in the surface's own space, not inside the profile's placement (as IfcOpenShell reads it)
        around = f.createIfcAxis1Placement(self.point(center), self.direction(z))
        return f.createIfcSurfaceOfRevolution(f.createIfcArbitraryOpenProfileDef("CURVE", None, profile), position, around)

    def revolved(self, center, axis, reference, profile, degree, counts, knots, ratio=1.0):
        """Exact NURBS surface made by turning a profile of (radius, height, weight) points about an axis.

        `ratio` squashes the circle into an ellipse, which a NURBS surface follows exactly.
        """
        x, y, z = _frame(axis, reference)
        grid = [
            [tuple(center[i] + r * (cx * x[i] + cy * ratio * y[i]) + h * z[i] for i in range(3)) for r, h, _ in profile]
            for cx, cy, _ in _CIRCLE
        ]
        weights = [[w * cw for _, _, w in profile] for _, _, cw in _CIRCLE]
        return self.f.createIfcRationalBSplineSurfaceWithKnots(
            2, degree, [[self.point(p) for p in row] for row in grid], "UNSPECIFIED", False, False, False,
            _CIRCLE_KNOTS[0], counts, _CIRCLE_KNOTS[1], knots, "UNSPECIFIED", weights,
        )  # fmt: skip

    def acis_surface(self, surface, face):
        """(IFC surface, flipped): ACIS marks inward-facing cylinders, spheres and tori by a sign; IFC's always face out."""
        kind = surface[0]
        if kind == "plane":
            return self.f.createIfcPlane(self.axes(surface[1], surface[2], surface[3])), False
        if kind == "cone":
            _, center, axis, major, ratio, sine, cosine = surface
            radius = math.sqrt(sum(c * c for c in major))
            if not radius or not cosine or ratio <= 0:
                return None, False
            if abs(sine) < 1e-12 and math.isclose(ratio, 1.0, rel_tol=1e-12):
                return self.f.createIfcCylindricalSurface(self.axes(center, axis, major), radius), cosine < 0
            # IFC has no cone and no elliptical cylinder: revolve a straight profile that spans the face.
            # The radius changes by sine/cosine per unit of height (checked against AutoCAD's cones).
            z = _unit(axis)
            heights = [sum((p[i] - center[i]) * z[i] for i in range(3)) for loop in face["loops"] for _, a, b, *_ in loop for p in (a, b) if p]
            low, high = (min(heights), max(heights)) if heights else (-radius, radius)
            margin = 0.05 * (high - low) or radius
            low, high = low - margin, high + margin
            slope = sine / cosine
            if slope:  # stop at the apex: beyond it lies the mirror cone
                apex = -radius / slope
                low, high = (max(low, apex), high) if slope > 0 else (low, min(high, apex))
            if high <= low:
                return None, False
            ends = [(radius + slope * h, h, 1.0) for h in (low, high)]
            if math.isclose(ratio, 1.0, rel_tol=1e-12):
                line = self.f.createIfcPolyline([self.f.createIfcCartesianPoint((float(r), float(h))) for r, h, _ in ends])
                return self.revolution(center, axis, major, line), cosine < 0
            return self.revolved(center, axis, major, ends, 1, [2, 2], [0.0, 1.0], ratio), cosine < 0
        if kind == "sphere":
            _, center, radius, u, pole = surface
            if not radius:
                return None, False
            reach = [tuple(p[i] - center[i] for i in range(3)) for loop in face["loops"] for _, a, b, *_ in loop for p in (a, b) if p]
            middle = tuple(map(sum, zip(*reach))) if reach else (0.0, 0.0, 0.0)
            if math.hypot(*middle) <= 1e-6 * abs(radius):
                return self.f.createIfcSphericalSurface(self.axes(center, pole, u), abs(radius)), radius < 0
            # A patch of a sphere, such as the rounded corner of a box. IfcOpenShell does not convert
            # IfcSphericalSurface at all: the face goes missing, and on some it crashes. The same sphere is
            # therefore written as an arc turned about an axis, which it does build. A sphere is the same
            # whichever way its poles point, so they are turned away from the patch, and the arc covers
            # only the band of latitudes the patch lies in, never reaching a pole.
            middle = _unit(middle)
            pole = _unit(_cross(middle, _Z if abs(middle[2]) < 0.9 else (1.0, 0.0, 0.0)))
            heights = [sum(a * b for a, b in zip(_unit(v), pole)) for v in reach]
            low, high = (math.asin(max(-1.0, min(1.0, h))) for h in (min(heights), max(heights)))
            margin = 0.1 * (high - low) + 0.02
            low, high = max(low - margin, -1.55), min(high + margin, 1.55)
            circle = self.f.createIfcCircle(self.f.createIfcAxis2Placement2D(self.f.createIfcCartesianPoint((0.0, 0.0))), abs(radius))
            return self.revolution(center, pole, tuple(-c for c in middle), self.trimmed(circle, low, high)), radius < 0
        if kind == "torus":
            _, center, axis, major, minor, u = surface
            if not minor:
                return None, False
            if 0 < abs(minor) < major:
                return self.f.createIfcToroidalSurface(self.axes(center, axis, u), major, abs(minor)), minor < 0
            # IfcToroidalSurface cannot describe a torus that crosses its own axis (the rounded end of a pin, say),
            # so the tube circle is revolved instead. IfcOpenShell does not build this face; revolving only the
            # part of the circle on its own side of the axis, which would be tidier, makes it hang (tried 2026-10-12).
            origin = self.f.createIfcAxis2Placement2D(self.f.createIfcCartesianPoint((float(major), 0.0)))
            tube = self.trimmed(self.f.createIfcCircle(origin, abs(minor)), 0.0, 2 * math.pi)
            return self.revolution(center, axis, u, tube), minor < 0
        _, degree_u, degree_v, ku, mu, kv, mv, grid, *closed = surface
        closed_u, closed_v = closed[0] if closed else (False, False)
        fixed_u, fixed_v = self.nurbs_knots(ku, mu, degree_u), self.nurbs_knots(kv, mv, degree_v)
        if fixed_u is None or fixed_v is None or not grid or not grid[0]:
            return None, False
        args = [degree_u, degree_v, [[self.point(p[:3]) for p in row] for row in grid], "UNSPECIFIED", closed_u, closed_v, False,
                fixed_u[0], fixed_v[0], fixed_u[1], fixed_v[1], "UNSPECIFIED"]  # fmt: skip
        if len(grid[0][0]) == 4:
            return self.f.createIfcRationalBSplineSurfaceWithKnots(*args, [[float(p[3]) for p in row] for row in grid]), False
        return self.f.createIfcBSplineSurfaceWithKnots(*args), False

    def whole_cone(self, body):
        """A plain pointed cone as a revolved triangle, or None.

        As a B-rep its side is bounded by one circle and the apex point, which IfcOpenShell fails to build,
        and it does not build IfcRightCircularCone either.
        """
        kinds = sorted(face["surface"][0] if face["surface"] else "" for face in body["faces"])
        if kinds != ["cone", "plane"] or any(face["double"] for face in body["faces"]):
            return None
        side = next(face for face in body["faces"] if face["surface"][0] == "cone")
        rims = [edge[3] for loop in side["loops"] for edge in loop if edge[3] not in (None, "point")]
        tips = [edge[1] for loop in side["loops"] for edge in loop if edge[3] == "point"]
        if len(rims) != 1 or len(tips) != 1 or rims[0][0] != "ellipse" or not math.isclose(rims[0][4], 1.0, rel_tol=1e-12):
            return None
        center, radius = rims[0][1], math.sqrt(sum(c * c for c in rims[0][3]))
        up = tuple(tips[0][i] - center[i] for i in range(3))
        height = math.sqrt(sum(c * c for c in up))
        if not height or not radius:
            return None
        f = self.f
        x, _, z = _frame(up, rims[0][3])
        corners = [f.createIfcCartesianPoint(p) for p in ((0.0, 0.0), (radius, 0.0), (0.0, height), (0.0, 0.0))]
        profile = f.createIfcArbitraryClosedProfileDef("AREA", None, f.createIfcPolyline(corners))
        return self.turned(profile, center, x, z)

    def turned(self, profile, center, x, z):
        """IfcRevolvedAreaSolid: a profile in the plane of the radius (X) and the axis (Y), turned fully about the axis."""
        f = self.f
        around = f.createIfcAxis1Placement(f.createIfcCartesianPoint((0.0, 0.0, 0.0)), self.direction((0.0, 1.0, 0.0)))
        return f.createIfcRevolvedAreaSolid(profile, self.placement((x, None, _cross(x, z)), center), around, 2 * math.pi)

    def acis_body(self, body, kind):
        """One ACIS body as an IfcAdvancedBrep, or as an open surface model if it is a sheet or has faces we cannot map."""
        f = self.f
        vertices, edges, faces = {}, {}, []
        cone = self.whole_cone(body)
        if cone is not None:
            return cone
        only = body["faces"][0]
        if len(body["faces"]) == 1 and not only["loops"] and not only["double"] and only["surface"]:
            # a whole sphere or ring torus has no edges to bound a B-rep face, and IFC has primitives for both
            s = only["surface"]
            if s[0] == "sphere" and s[2]:
                return f.createIfcCsgSolid(f.createIfcSphere(self.axes(s[1], s[4], s[3]), abs(s[2])))
            if s[0] == "torus" and 0 < abs(s[4]) < s[3]:
                x, _, z = _frame(s[2], s[5])
                profile = f.createIfcCircleProfileDef("AREA", None, f.createIfcAxis2Placement2D(f.createIfcCartesianPoint((float(s[3]), 0.0))), abs(s[4]))
                return self.turned(profile, s[1], x, z)

        def vertex(p):
            key = tuple(round(c, 9) for c in p)
            if key not in vertices:
                vertices[key] = f.createIfcVertexPoint(self.point(p))
            return vertices[key]

        closed = True
        for face in _open_tubes(body["faces"]):
            surface, flipped = self.acis_surface(face["surface"], face) if face["surface"] else (None, False)
            loops, ok = [], surface is not None
            for loop in face["loops"] if ok else ():
                oriented = []
                for key, start, end, curve, same_sense, forward in loop:
                    if curve == "point":
                        continue  # the neighbouring edges already meet at this vertex
                    if key not in edges:
                        geometry = self.acis_curve(curve) if curve and start and end else None
                        edges[key] = geometry and f.createIfcEdgeCurve(vertex(start), vertex(end), geometry, same_sense)
                    if edges[key] is None:
                        ok = False
                        break
                    oriented.append(f.createIfcOrientedEdge(None, None, edges[key], forward))
                if oriented:
                    points = [p for _, start, end, *_ in loop for p in (start, end)]
                    extent = sum((max(c) - min(c)) ** 2 for c in zip(*points))
                    # a whole circle starts and ends at one point: its size is its diameter
                    rims = [4 * sum(c * c for c in curve[3]) for _, start, end, curve, *_ in loop if curve[0] == "ellipse" and _same(start, end)]
                    extent = max([extent, *rims])
                    loops.append((extent, f.createIfcEdgeLoop(oriented)))
                elif ok and loop and loop[0][1]:  # a loop that is one point: the apex of a cone
                    loops.append((0, f.createIfcVertexLoop(vertex(loop[0][1]))))
            if not ok:
                closed = False
                self.skipped[f"{kind} face (unsupported geometry)"] += 1
                continue
            if not loops:  # a whole sphere or torus has no edges; IFC still wants a bound, so use a point on it
                s = face["surface"]
                on_surface = [s[1][i] + s[2] * _unit(s[4])[i] for i in range(3)] if s[0] == "sphere" else s[1]
                loops.append((0, f.createIfcVertexLoop(vertex(on_surface))))
            outer = max(range(len(loops)), key=lambda i: loops[i][0])
            bounds = [(f.createIfcFaceOuterBound if i == outer else f.createIfcFaceBound)(loop, True) for i, (_, loop) in enumerate(loops)]
            faces.append(f.createIfcAdvancedFace(bounds, surface, face["same_sense"] != flipped))
            closed = closed and not face["double"]
        if not faces:
            return None
        if closed:
            return f.createIfcAdvancedBrep(f.createIfcClosedShell(faces))
        return f.createIfcShellBasedSurfaceModel([f.createIfcOpenShell(faces)])

    def _acis(self, e, _):
        if e["bodies"] is None:
            return None
        return [item for body in e["bodies"] if (item := self.acis_body(body, e["type"])) is not None]

    def fill(self, outer, inner=()):
        return self.f.createIfcAnnotationFillArea(outer, list(inner) or None)

    def _solid(self, e, _):
        a, b, c, d = e["corners"]  # stored in a Z pattern: the last two are swapped relative to the outline
        outline = self.polycurve([a, b, d, c], [], True, e["elevation"], e["extrusion"])
        return outline and self.fill(outline)

    def _hatch(self, e, _):
        # shortcut: a loop flagged external starts a new filled area and the loops after it are its holes;
        # nest by containment instead if drawings turn up whose flags do not follow that order
        areas = []
        for path in e["paths"]:
            curve = self.polycurve(path["points"], path["bulges"], True, e["elevation"], e["extrusion"])
            if curve is None:
                continue
            if path["flags"] & 1 or not areas:
                areas.append([curve])
            else:
                areas[-1].append(curve)
        return [self.fill(outer, holes) for outer, *holes in areas]

    def _spline(self, e, _):
        degree, control, knots, weights = e["degree"], e["control"], e["knots"], e["weights"]
        if not control:  # only fit points are stored; AutoCAD derives the curve from them
            # shortcut: end tangents and closed fit splines are treated as open with natural ends
            knots, control = fit_spline(e["fit"], e["knot_parameter"])
            degree, weights = 3, []
            if not control:
                return self.polyline(e["fit"])
        distinct, counts = [], []
        for k in knots:
            if distinct and math.isclose(k, distinct[-1], rel_tol=1e-12, abs_tol=1e-12):
                counts[-1] += 1
            else:
                distinct.append(float(k))
                counts.append(1)
        valid = (
            degree >= 1
            and len(control) > degree
            and len(knots) == len(control) + degree + 1
            and len(distinct) >= 2
            and distinct == sorted(distinct)
            and max(counts[0], counts[-1]) <= degree + 1
            and all(c <= degree for c in counts[1:-1])
            and (not weights or (len(weights) == len(control) and min(weights) > 0))
        )
        if not valid:  # IFC would reject it, so keep the shape as a polyline
            return self.polyline(bspline_points(degree, knots, control, weights))
        args = [degree, [self.point(p) for p in control], "UNSPECIFIED", e["closed"], False, counts, distinct, "UNSPECIFIED"]
        if weights:
            return self.f.createIfcRationalBSplineCurveWithKnots(*args, [float(w) for w in weights])
        return self.f.createIfcBSplineCurveWithKnots(*args)

    def text(self, e, text, point, x_axis, alignment):
        lines = text.split("\n")
        if not text.strip() or e["height"] <= 0:
            return None
        width, tall = e.get("extent", (0.0, 0.0))
        if width <= 0 or tall <= 0:
            # shortcut: single-line text stores no size, so its box is estimated at 0.6 x height per character
            width, tall = 0.6 * e["height"] * max(map(len, lines)), 1.5 * e["height"] * len(lines)
        extent = self.f.createIfcPlanarExtent(float(width), float(tall))
        placement = self.placement((x_axis, None, _unit(e["extrusion"])), point)
        return self.f.createIfcTextLiteralWithExtent(text, placement, "RIGHT", extent, alignment)

    def _text(self, e, _):
        axes = _ocs(e["extrusion"])
        x_axis = _to_wcs(axes, (math.cos(e["rotation"]), math.sin(e["rotation"]), 0.0))
        if e.get("multiline"):  # a multi-line attribute: written from its first line down
            return self.text(e, _plain_text(e["text"]), _to_wcs(axes, e["point"]), x_axis, "top-left")
        across, upright = e.get("align", (0, 0))
        if (across, upright) == (0, 0) or across in (3, 5):  # plain, or stretched between two points: from its start
            return self.text(e, e["text"], _to_wcs(axes, e["point"]), x_axis, "bottom-left")
        # otherwise the drawing stores the point the text is hung from, and which point of the text that is
        row = {2: "middle", 3: "top"}.get(upright, "bottom") if across != 4 else "middle"
        column = {1: "middle", 2: "right", 4: "middle"}.get(across, "left")
        return self.text(e, e["text"], _to_wcs(axes, e["anchor"]), x_axis, "center" if row == column else f"{row}-{column}")

    def _parts(self, e, parents):
        """An entity drawn from its proxy graphics: each part is built like the entity it resembles."""
        out = []
        for part in e["parts"]:
            found = getattr(self, "_" + part["type"].lower())(part | {"layer": e["layer"]}, parents)
            out += [] if found is None else found if isinstance(found, list) else [found]
        return out

    def _proxytext(self, e, _):
        return self.text(e, e["text"], e["point"], _unit(e["x_axis"]), "bottom-left")

    def _mtext(self, e, _):  # insertion point and x axis are already in world coordinates
        hung = ("top-left", "top-middle", "top-right", "middle-left", "center", "middle-right", "bottom-left", "bottom-middle", "bottom-right")
        attachment = e.get("attachment", 1)  # which point of its box the text is placed by, as stored
        return self.text(e, _plain_text(e["text"]), e["point"], _unit(e["x_axis"]), hung[attachment - 1] if 1 <= attachment <= 9 else "top-left")

    def _insert(self, e, parents, offset=(0.0, 0.0)):
        block = self.d.blocks.get(e["block"])
        source = self.block(e["block"], parents) if block else None
        sx, sy, sz = e["scale"]
        if source is None or not (sx and sy and sz):
            return None
        x, y, z = _ocs(e["extrusion"])
        cos, sin = math.cos(e["rotation"]), math.sin(e["rotation"])
        # the insert's own axes: the object coordinate system turned by the rotation
        rx = tuple(cos * x[i] + sin * y[i] for i in range(3))
        ry = tuple(cos * y[i] - sin * x[i] for i in range(3))
        # a negative scale mirrors by flipping its axis, since IFC scales must be positive
        ax = tuple(math.copysign(1, sx) * c for c in rx)
        ay = tuple(math.copysign(1, sy) * c for c in ry)
        az = tuple(math.copysign(1, sz) * c for c in z)
        sx, sy, sz = abs(sx), abs(sy), abs(sz)
        bx, by, bz = block["base"]
        origin = _to_wcs((x, y, z), e["point"])
        origin = tuple(
            origin[i] + offset[0] * rx[i] + offset[1] * ry[i] - sx * bx * ax[i] - sy * by * ay[i] - sz * bz * az[i]
            for i in range(3)
        )
        target = self.f.createIfcCartesianTransformationOperator3DnonUniform(
            self.direction(ax), self.direction(ay), self.point(origin), sx, self.direction(az), sy, sz
        )
        return self.f.createIfcMappedItem(source, target)

    def _dimension(self, e, parents):
        """The block AutoCAD drew for the dimension, or, when the drawing has none, a plain version of our own."""
        block = self._insert(e, parents)
        if block is not None:
            return block
        # shortcut: lines and centred text only; no arrowheads, and angular dimensions get just their text
        kind, defs = e["type"], e["defs"]
        axes = _ocs(e["extrusion"])
        style = self.d.dimstyles.get(e["style"], {})
        height = style.get("text", 0) * style.get("scale", 1) or 2.5 * self.millimetre
        along = axes[0]
        lines = []
        if kind in ("DIMENSION_LINEAR", "DIMENSION_ALIGNED"):
            along = _to_wcs(axes, (math.cos(e["angle"]), math.sin(e["angle"]), 0.0))
            if kind == "DIMENSION_ALIGNED":
                along = _unit(tuple(b - a for a, b in zip(defs[13], defs[14])))
            # the dimension line runs through point 10; each extension line drops onto it from its origin
            feet = [
                tuple(defs[10][i] + along[i] * sum((origin[k] - defs[10][k]) * along[k] for k in range(3)) for i in range(3))
                for origin in (defs[13], defs[14])
            ]
            lines = [[defs[13], feet[0]], [defs[14], feet[1]], feet]
        elif kind in ("DIMENSION_RADIUS", "DIMENSION_DIAMETER"):
            lines = [[defs[10], defs[15]]]
        elif kind == "DIMENSION_ORDINATE":
            lines = [[defs[13], defs[14]]]
        value = e["measurement"]
        if value is None and len(lines) == 3:
            value = math.dist(*lines[2])
        if value is not None and kind in ("DIMENSION_ANG3PT", "DIMENSION_ANG2LN"):
            value = math.degrees(value)
        measured = "" if value is None else f"{value:.4f}".rstrip("0").rstrip(".")
        text = _plain_text(e["text"]).replace("<>", measured) if e["text"].strip() else measured
        if along[0] < 0 or (along[0] == 0 and along[1] < 0):
            along = tuple(-c for c in along)  # keep the text reading left to right
        label = self.text({"height": height, "extrusion": e["extrusion"]}, text, _to_wcs(axes, e["text_point"]), along, "center")
        return [item for item in (*map(self.polyline, lines), label) if item is not None]

    def _tolerance(self, e, _):
        style = self.d.dimstyles.get(e["style"], {})
        height = style.get("text", 0) * style.get("scale", 1) or 2.5 * self.millimetre
        # shortcut: the feature control frame is written as plain text, its boxes as bars
        text = _plain_text(e["text"]).replace("%%v", " | ").replace("^J", "\n")
        return self.text({"height": height, "extrusion": e["extrusion"]}, text, e["point"], _unit(e["x_axis"]), "bottom-left")

    def _mline(self, e, _):
        return [line for points in e["elements"] if (line := self.polyline(points + points[:1] if e["closed"] else points)) is not None]

    def _minsert(self, e, parents):
        cells = [
            self._insert(e, parents, (column * e["column_spacing"], row * e["row_spacing"]))
            for row in range(max(1, min(e["rows"], 1000)))
            for column in range(max(1, min(e["columns"], 1000)))
        ]
        return [c for c in cells if c is not None]


def build(drawing: Drawing, name: str, heights: dict | None = None) -> tuple[ifcopenshell.file, dict[str, int]]:
    """Returns the IFC model and a count of what could not be converted, by entity type.

    `heights` are the caller's, in metres, for elements read out of flat linework that stores none:
    "storey" (walls, rooms, columns), "door", "window" and "sill". Nothing is assumed in their place."""
    builder = _Builder(drawing, name, heights)
    return builder.f, dict(builder.skipped)

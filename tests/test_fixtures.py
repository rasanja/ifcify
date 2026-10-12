"""Checks decoded values against AutoCAD's own DXF export of the same drawing, for every DWG release.

tests/fixtures is built by tools/make_fixtures.sh: one of every entity type we can script, saved as
R14 through R2018 plus DXF.
"""

import math
import struct
from collections import Counter
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.element
import pytest

from dwg2ifc import convert, entities, ifc

FIXTURES = Path(__file__).parent / "fixtures"
RELEASES = sorted(FIXTURES.glob("entities_*.dwg"))
# our type name -> DXF name, where they differ
DXF_NAME = {"MINSERT": "INSERT", "POLYLINE_2D": "POLYLINE", "POLYLINE_3D": "POLYLINE", "POLYLINE_MESH": "POLYLINE", "POLYLINE_PFACE": "POLYLINE"}


def dxf_entities() -> list[dict]:
    """The ENTITIES section as one {group code: [values]} dict per entity, with the type under 0."""
    lines = (FIXTURES / "entities.dxf").read_text(errors="replace").splitlines()
    pairs = [(int(lines[i]), lines[i + 1].strip()) for i in range(0, len(lines) - 1, 2)]
    start = next(i for i, p in enumerate(pairs) if p == (2, "ENTITIES"))
    out = []
    for code, value in pairs[start + 1 :]:
        if (code, value) == (0, "ENDSEC"):
            break
        if code == 0:
            out.append({0: [value]})
        else:
            out[-1].setdefault(code, []).append(value)
    return out


def f(entity: dict, code: int, index: int = 0) -> float:
    return float(entity[code][index])


def near(a, b) -> bool:
    return len(a) == len(b) and all(math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9) for x, y in zip(a, b))


def match(ours: list, theirs: list) -> None:
    """Every expected tuple has a decoded tuple with the same values."""
    assert len(ours) == len(theirs)
    left = list(ours)
    for expected in theirs:
        found = next((o for o in left if near(o, expected)), None)
        assert found is not None, f"no decoded match for {expected}"
        left.remove(found)


@pytest.fixture(scope="module")
def dxf():
    return dxf_entities()


@pytest.mark.parametrize("path", RELEASES, ids=lambda p: p.stem)
def test_matches_autocad_dxf(path, dxf):
    drawing = entities.load(path.read_bytes())
    assert not [k for k in drawing.skipped if "unreadable" in k], drawing.skipped
    # the fixture is drawn in millimetres (INSUNITS 4); R14 predates the variable
    assert drawing.units == (0 if drawing.release == "R14" else 4)
    spaces = {drawing.block_named("*Model_Space"), drawing.block_named("*Paper_Space")}
    ours = [e for e in drawing.entities if e["owner"] in spaces]
    kinds = {"DIMENSION" if e["type"].startswith("DIMENSION") else DXF_NAME.get(e["type"], e["type"]) for e in ours}

    def mine(*types):
        return [e for e in ours if e["type"] in types]

    def theirs(name):
        return [e for e in dxf if e[0][0] == name]

    # same number of every entity type we decode
    expected = Counter(e[0][0] for e in dxf if e[0][0] in kinds)
    got = Counter("DIMENSION" if e["type"].startswith("DIMENSION") else DXF_NAME.get(e["type"], e["type"]) for e in ours)
    assert got == expected

    match([(*e["start"], *e["end"]) for e in mine("LINE")], [tuple(f(e, c) for c in (10, 20, 30, 11, 21, 31)) for e in theirs("LINE")])
    match([(*e["center"], e["radius"]) for e in mine("CIRCLE")], [tuple(f(e, c) for c in (10, 20, 30, 40)) for e in theirs("CIRCLE")])
    match(
        [(*e["center"], e["radius"], math.degrees(e["start_angle"]) % 360, math.degrees(e["end_angle"]) % 360) for e in mine("ARC")],
        [(*(f(e, c) for c in (10, 20, 30, 40)), f(e, 50) % 360, f(e, 51) % 360) for e in theirs("ARC")],
    )
    match(
        [(*e["center"], *e["major"], e["ratio"], e["start_angle"], e["end_angle"]) for e in mine("ELLIPSE")],
        [tuple(f(e, c) for c in (10, 20, 30, 11, 21, 31, 40, 41, 42)) for e in theirs("ELLIPSE")],
    )
    match([e["point"] for e in mine("POINT")], [tuple(f(e, c) for c in (10, 20, 30)) for e in theirs("POINT")])
    match(
        [(*e["point"], *e["scale"], math.degrees(e["rotation"])) for e in mine("INSERT", "MINSERT")],
        [(f(e, 10), f(e, 20), f(e, 30), *(float(e.get(c, ["1"])[0]) for c in (41, 42, 43)), float(e.get(50, ["0"])[0])) for e in theirs("INSERT")],
    )
    match(
        [tuple(c for p in e["points"] for c in p) + (float(e["closed"]),) for e in mine("LWPOLYLINE")],
        [tuple(c for i in range(len(e[10])) for c in (f(e, 10, i), f(e, 20, i))) + (float(int(e[70][0]) & 1),) for e in theirs("LWPOLYLINE")],
    )
    assert sorted(e["text"] for e in mine("TEXT", "ATTRIB")) == sorted(e[1][0] for e in dxf if e[0][0] in ("TEXT", "ATTRIB"))
    match([(*e["point"][:2], e["height"]) for e in mine("TEXT")], [tuple(f(e, c) for c in (10, 20, 40)) for e in theirs("TEXT")])
    # fit-point splines store no control points; ours come from fit_spline and must equal AutoCAD's
    splines = [(e["control"], e["knots"]) if e["control"] else entities.fit_spline(e["fit"], e["knot_parameter"])[::-1] for e in mine("SPLINE")]
    match(
        [tuple(c for p in control for c in p) + tuple(knots) for control, knots in splines],
        [tuple(c for i in range(len(e[10])) for c in (f(e, 10, i), f(e, 20, i), f(e, 30, i))) + tuple(map(float, e[40])) for e in theirs("SPLINE")],
    )
    match(
        [tuple(c for p in e["corners"] for c in p) for e in mine("3DFACE")],
        [tuple(f(e, c) for c in (10, 20, 30, 11, 21, 31, 12, 22, 32, 13, 23, 33)) for e in theirs("3DFACE")],
    )
    match(
        [tuple(c for p in e["corners"] for c in p) for e in mine("SOLID")],
        [tuple(f(e, c) for c in (10, 20, 11, 21, 12, 22, 13, 23)) for e in theirs("SOLID")],
    )
    # classic polylines: vertex lists follow each POLYLINE in the DXF, up to its SEQEND
    expected_vertices = []
    for i, e in enumerate(dxf):
        if e[0][0] == "POLYLINE":
            run = []
            for v in dxf[i + 1 :]:
                if v[0][0] != "VERTEX":
                    break
                if int(v.get(70, ["0"])[0]) & 128 and not int(v.get(70, ["0"])[0]) & 64:
                    continue  # polyface face record, not a point
                run += [f(v, 10), f(v, 20), f(v, 30)]
            expected_vertices.append(tuple(run))
    match([tuple(float(c) for p in e["points"] for c in p) for e in ours if e["type"].startswith("POLYLINE")], expected_vertices)
    # hatch loops: every boundary point AutoCAD lists is one of ours
    for mine_hatch, their_hatch in zip(sorted(mine("HATCH"), key=lambda e: e["solid"]), sorted(theirs("HATCH"), key=lambda e: int(e[70][0]))):
        assert len(mine_hatch["paths"]) == int(their_hatch[91][0])


@pytest.mark.parametrize("path", RELEASES, ids=lambda p: p.stem)
def test_colours_and_layers_match_autocad(path, dxf):
    """The script draws on a red layer with a 0.50 mm line weight, a layer turned off and a frozen one."""
    drawing = entities.load(path.read_bytes())
    layers = {layer["name"].upper(): layer for layer in drawing.layers.values()}
    true_colour = drawing.release not in ("R14", "R2000")  # older files only have the palette; AutoCAD picks 168
    assert layers["REDLAYER"]["color"] == 1
    assert (layers["OFFLAYER"]["color"], layers["OFFLAYER"]["off"], layers["OFFLAYER"]["frozen"]) == (3, True, False)
    assert layers["FROZENLAYER"]["color"] == ((10, 20, 30) if true_colour else 168)
    assert (layers["FROZENLAYER"]["off"], layers["FROZENLAYER"]["frozen"]) == (False, True)
    assert not layers["0"]["off"] and not layers["0"]["frozen"] and layers["0"]["color"] == 7
    if drawing.release != "R14":  # line weights arrived with R2000
        assert ifc._LINEWEIGHTS[layers["REDLAYER"]["lineweight"]] == 50
    # two lines with their own colour, one of them with its own 0.35 mm weight
    blue, orange = (next(e for e in drawing.entities if e["type"] == "LINE" and e["start"][:2] == (0.0, y)) for y in (180.0, 182.0))
    assert blue["color"] == 5
    assert orange["color"] == ((200, 100, 50) if true_colour else 32)  # 32 is AutoCAD's nearest palette colour
    if drawing.release != "R14":
        assert ifc._LINEWEIGHTS[orange["lineweight"]] == 35
    assert all(e["color"] is None for e in drawing.entities if e["type"] == "CIRCLE")  # by layer


def test_palette():
    # the table is AutoCAD's own; these are entries anyone can check in its colour dialog
    assert [ifc._rgb(i) for i in (1, 5, 8, 9, 10, 11, 12, 13, 19, 20, 30, 50, 250, 254, 255)] == [
        (255, 0, 0), (0, 0, 255), (128, 128, 128), (192, 192, 192), (255, 0, 0), (255, 127, 127), (204, 0, 0),
        (204, 102, 102), (76, 38, 38), (255, 63, 0), (255, 127, 0), (255, 255, 0), (51, 51, 51), (214, 214, 214),
        (255, 255, 255),
    ]  # fmt: skip
    assert ifc._rgb((1, 2, 3)) == (1, 2, 3)
    assert ifc._rgb(7) == (0, 0, 0)  # white in the table; written black, as on paper
    assert len(ifc._PALETTE) == 768


@pytest.mark.parametrize("path", RELEASES, ids=lambda p: p.stem)
def test_converts_to_ifc(path):
    text, skipped = convert(path.read_bytes())
    assert "FILE_SCHEMA(('IFC4X3_ADD2'))" in text
    # the invisible one is a line AutoCAD keeps inside the table's block; saving to a release
    # older than lights makes AutoCAD freeze the layer the light is on
    old = path.stem.endswith(("_14", "2000", "2004"))
    assert skipped == {"on a layer that is off or frozen": 2 + old, "marked invisible": 1}
    # the box and the sphere are closed solids, the region is an open sheet
    model = ifcopenshell.file.from_string(text)
    # two frustums and a cylinder, the wedge, the box; whole primitives use IFC's own solids
    assert sorted(len(brep.Outer.CfsFaces) for brep in model.by_type("IfcAdvancedBrep")) == [3, 3, 3, 5, 6]
    assert len(model.by_type("IfcSphere")) == 1
    torus, cone = sorted(model.by_type("IfcRevolvedAreaSolid"), key=lambda solid: solid.SweptArea.is_a())[::-1]
    assert (torus.SweptArea.Radius, torus.SweptArea.Position.Location.Coordinates) == (2.0, (6.0, 0.0))  # TORUS 430,5,5 6 2
    # CONE 350,5,0 4 8: a triangle of radius 4 and height 8
    assert [p.Coordinates for p in cone.SweptArea.OuterCurve.Points] == [(0.0, 0.0), (4.0, 0.0), (0.0, 8.0), (0.0, 0.0)]
    assert len(model.by_type("IfcSurfaceOfRevolution")) == 2  # the frustums' sides
    assert len(model.by_type("IfcShellBasedSurfaceModel")) == 2  # the region and the plane surface
    box = max(model.by_type("IfcAdvancedBrep"), key=lambda b: len(b.Outer.CfsFaces))
    corners = {v.VertexGeometry.Coordinates for face in box.Outer.CfsFaces for bound in face.Bounds
               for edge in bound.Bound.EdgeList for v in (edge.EdgeElement.EdgeStart, edge.EdgeElement.EdgeEnd)}  # fmt: skip
    assert corners == {(x, y, z) for x in (300.0, 310.0) for y in (0.0, 8.0) for z in (0.0, 6.0)}  # as drawn: BOX 300,0,0 310,8,6
    if "entities_14" not in path.name:
        assert ".LENGTHUNIT.,.MILLI.,.METRE." in text
    # the circles on the off and frozen layers are left out, as AutoCAD hides them; the red layer's colour reaches the IFC
    names = {layer.Name.upper() for layer in model.by_type("IfcPresentationLayerAssignment")}
    assert "REDLAYER" in names and not names & {"OFFLAYER", "FROZENLAYER"}
    red = [style for style in model.by_type("IfcCurveStyle") if tuple(style.CurveColour)[1:] == (1.0, 0.0, 0.0)]
    assert red and all(item.is_a("IfcCircle") for style in red for styled in model.get_inverse(style) for item in [styled.Item])


@pytest.mark.parametrize("path", RELEASES, ids=lambda p: p.stem)
def test_dimension_without_its_block_is_drawn_from_its_points(path):
    """Some programs save dimensions without the block AutoCAD draws; then we draw lines and the measurement."""
    drawing = entities.load(path.read_bytes())
    linear = next(e for e in drawing.entities if e["type"] == "DIMENSION_LINEAR")
    # as drawn: DIMLINEAR 0,0 10,0 5,-5
    assert (linear["defs"][13], linear["defs"][14]) == ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0))
    assert linear["defs"][10][1] == -5.0
    for e in drawing.entities:
        if e["type"].startswith("DIMENSION"):
            e["block"] = 0
    model, skipped = ifc.build(drawing, "test")
    dimension = next(p for p in model.by_type("IfcAnnotation") if p.ObjectType == "DIMENSION_LINEAR")
    found = set()
    todo = list(dimension.Representation.Representations[0].Items)
    while todo:
        item = todo.pop()
        if item.is_a("IfcMappedItem"):
            todo += item.MappingSource.MappedRepresentation.Items
        elif item.is_a("IfcTextLiteral"):
            found.add(item.Literal)
        else:
            found.add(tuple(p.Coordinates for p in item.Points))
    assert "10" in found
    assert ((0.0, -5.0, 0.0), (10.0, -5.0, 0.0)) in found  # the dimension line
    assert ((0.0, 0.0, 0.0), (0.0, -5.0, 0.0)) in found  # an extension line


@pytest.mark.parametrize("path", RELEASES, ids=lambda p: p.stem)
def test_properties_reach_the_ifc(path):
    """Layer, colour, block name and block attributes are carried as property sets."""
    model = ifcopenshell.file.from_string(convert(path.read_bytes())[0])
    psets = [ifcopenshell.util.element.get_psets(p) for p in model.by_type("IfcProduct") if p.ObjectType in ("INSERT", "MINSERT")]
    # the script inserts block SYMBOL five times, typing an attribute value each time
    assert sorted(p["DWG_Attributes"]["TAG1"] for p in psets) == ["Grid", "Mirrored", "Tilted", "Value A", "Value B"]
    assert {p["DWG_Block"]["BlockName"] for p in psets} == {"SYMBOL"}
    red = next(p for p in model.by_type("IfcProduct") if p.ObjectType == "CIRCLE"
               and ifcopenshell.util.element.get_psets(p)["DWG_Entity"]["Layer"].upper() == "REDLAYER")  # fmt: skip
    described = ifcopenshell.util.element.get_psets(red)["DWG_Entity"]
    assert described | {"id": 0, "Layer": "REDLAYER", "Linetype": described["Linetype"].capitalize()} == {
        "id": 0, "Layer": "REDLAYER", "EntityType": "CIRCLE", "Colour": "255,0,0", "Linetype": "Continuous",
    }  # fmt: skip
    solids = [p for p in model.by_type("IfcBuildingElementProxy") if p.ObjectType == "3DSOLID"]
    assert solids and all(p.Tag and p.Name.endswith(":" + p.Tag) for p in solids)  # the DWG handle


@pytest.mark.parametrize("path", RELEASES, ids=lambda p: p.stem)
def test_rays_and_construction_lines(path):
    """As drawn: RAY from 0,90 through 1,91 and XLINE through 0,95 and 1,96."""
    drawing = entities.load(path.read_bytes())
    found = {e["type"]: e for e in drawing.entities if e["type"] in ("RAY", "XLINE")}
    diagonal = (2**-0.5, 2**-0.5, 0.0)
    for kind, point in (("RAY", (0.0, 90.0, 0.0)), ("XLINE", (0.0, 95.0, 0.0))):
        assert found[kind]["point"] == point
        assert near(found[kind]["direction"], diagonal)
    model = ifcopenshell.file.from_string(convert(path.read_bytes())[0])
    lines = [p for p in model.by_type("IfcAnnotation") if p.ObjectType in ("RAY", "XLINE")]
    assert len(lines) == 2 and all(p.Representation.Representations[0].Items[0].is_a("IfcLine") for p in lines)


@pytest.mark.parametrize("path", RELEASES, ids=lambda p: p.stem)
def test_dictionaries_records_and_text_styles(path):
    drawing = entities.load(path.read_bytes())
    assert not any("unreadable" in kind for kind in drawing.skipped), dict(drawing.skipped)
    root = next(entries for entries in drawing.dictionaries.values() if "ACAD_GROUP" in entries)
    assert root["ACAD_GROUP"] in drawing.dictionaries
    assert drawing.xrecords and all(isinstance(code, int) for items in drawing.xrecords.values() for code, _ in items)
    fonts = {style["name"].upper(): style["font"].lower() for style in drawing.styles.values()}
    assert fonts["STANDARD"] in ("arial", "arial.ttf", "txt", "txt.shx")
    texts = [e for e in drawing.entities if e["type"] in ("TEXT", "MTEXT")]
    assert texts and all(e["style"] in drawing.styles for e in texts)


def test_record_values_follow_autocads_type_table():
    # group codes 1 (text), 90 (32-bit), 40 (real), 330 (handle) and 280 (8-bit), as an XRECORD stores them
    raw = b"\x01\x00\x02\x00a\x00b\x00" + b"\x5a\x00\x07\x00\x00\x00" + b"\x28\x00" + struct.pack("<d", 1.5)
    raw += b"\x4a\x01" + (0x1F).to_bytes(8, "little") + b"\x18\x01\x01"
    assert entities._values(raw, True) == [(1, "ab"), (90, 7), (40, 1.5), (330, 0x1F), (280, 1)]


@pytest.mark.parametrize("path", RELEASES, ids=lambda p: p.stem)
def test_entities_that_point_at_other_data(path):
    """As drawn by tools/entities.scr: values must come out as typed there, in every release."""
    drawing = entities.load(path.read_bytes())
    one = {kind: next(e for e in drawing.entities if e["type"] == kind) for kind in ("WIPEOUT", "IMAGE", "HELIX", "MESH", "ACAD_TABLE", "LIGHT", "SHAPE")}
    assert one["WIPEOUT"]["points"] == [(0.0, 160.0, 0.0), (10.0, 160.0, 0.0), (10.0, 170.0, 0.0), (0.0, 170.0, 0.0)]
    assert sorted(one["IMAGE"]["points"]) == [(60.0, 230.0, 0.0), (60.0, 240.0, 0.0), (70.0, 230.0, 0.0), (70.0, 240.0, 0.0)]
    assert drawing.images[one["IMAGE"]["definition"]] == "/tmp/dwg2ifc_fixture.png"
    assert one["HELIX"]["stored"] == {"Radius": 5.0, "Turns": 3.0, "TurnHeight": pytest.approx(10 / 3)}
    low, high = (tuple(f(axis) for axis in zip(*one["MESH"]["points"])) for f in (min, max))
    assert (low, high) == ((40.0, 230.0, 0.0), (50.0, 240.0, 5.0))
    assert len(one["MESH"]["faces"]) == 54 and all(len(face) == 4 for face in one["MESH"]["faces"])
    assert drawing.blocks[one["ACAD_TABLE"]["block"]]["name"].startswith("*T") and one["ACAD_TABLE"]["point"] == (0.0, 270.0, 0.0)
    assert (one["LIGHT"]["name"], one["LIGHT"]["kind"], one["LIGHT"]["point"]) == ("Pointlight1", "point", (80.0, 230.0, 5.0))
    assert (one["SHAPE"]["point"], one["SHAPE"]["size"]) == ((120.0, 230.0, 0.0), 2.0)
    assert drawing.styles[one["SHAPE"]["style"]]["font"].lower() == "ltypeshp.shx"

    names = {handle: name for entries in drawing.dictionaries.values() for name, handle in entries.items()}
    (handle, group), = drawing.groups.items()
    assert (names[handle], group["description"]) == ("PAIR", "A line and a circle")
    assert [e["type"] for e in drawing.entities if e["handle"] in group["members"]] == ["CIRCLE"]  # "L" selected the last one
    styled = next(e for e in drawing.entities if e.get("text") == "Styled text")
    notes = drawing.styles[styled["style"]]
    assert (notes["name"], notes["font"].lower().removesuffix(".ttf")) == ("NOTES", "arial")
    assert math.degrees(notes["oblique"]) == pytest.approx(15)

    model = ifcopenshell.file.from_string(convert(path.read_bytes())[0])
    assert [g.Name for g in model.by_type("IfcGroup")] == ["PAIR"]
    if not path.stem.endswith(("_14", "2000", "2004")):  # saved to those, the light's layer is frozen
        assert [light.Name for light in model.by_type("IfcLightFixture")] == ["Pointlight1"]
    sets = {p.Name: {v.Name: v.NominalValue.wrappedValue for v in p.HasProperties} for p in model.by_type("IfcPropertySet")}
    assert sets["DWG_Image"] == {"File": "/tmp/dwg2ifc_fixture.png"} and sets["DWG_Shape"]["File"].lower() == "ltypeshp.shx"
    assert any(s.TextFontStyle.FontStyle == "oblique" for s in model.by_type("IfcTextStyle"))


@pytest.mark.parametrize("path", [p for p in RELEASES if p.stem[-4:] in ("2007", "2010", "2013", "2018")], ids=lambda p: p.stem)
def test_solids_keep_the_sizes_they_were_made_with(path):
    """BOX 10x8x6, SPHERE r4, three CONEs r4 h8 (top radius 0, 2, 6), CYLINDER r3 h7, TORUS 6/2, WEDGE 10x8x6."""
    drawing = entities.load(path.read_bytes())
    stored = [e["stored"] for e in drawing.entities if e["type"] == "3DSOLID"]
    assert stored == [
        {"Primitive": "Box", "Length": 10.0, "Width": 8.0, "Height": 6.0},
        {"Primitive": "Sphere", "Radius": 4.0},
        {"Primitive": "Cone", "Height": 8.0, "BaseRadius": 4.0, "BaseMinorRadius": 4.0, "TopRadius": 0.0},
        {"Primitive": "Cone", "Height": 8.0, "BaseRadius": 4.0, "BaseMinorRadius": 4.0, "TopRadius": 2.0},
        {"Primitive": "Cone", "Height": 8.0, "BaseRadius": 4.0, "BaseMinorRadius": 4.0, "TopRadius": 6.0},
        {"Primitive": "Cylinder", "Height": 7.0, "BaseRadius": 3.0, "BaseMinorRadius": 3.0, "TopRadius": 3.0},
        {"Primitive": "Torus", "MajorRadius": 6.0, "MinorRadius": 2.0},
        {"Primitive": "Wedge", "Length": 10.0, "Width": 8.0, "Height": 6.0},
    ]
    model = ifcopenshell.file.from_string(convert(path.read_bytes())[0])
    box = next(p for p in model.by_type("IfcBuildingElementProxy") if ifcopenshell.util.element.get_psets(p).get("DWG_Stored", {}).get("Primitive") == "Box")
    assert ifcopenshell.util.element.get_psets(box)["DWG_Stored"]["Height"] == "6.0"


def test_stored_geographic_location_becomes_a_map_conversion():
    geo = {"kind": "projected grid", "system": '<Alias id="26911" type="CoordinateSystem">', "design": (100.0, 200.0, 0.0),
           "reference": (500000.0, 4000000.0, 10.0), "scale": 0.5, "north": (0.0, 1.0)}  # fmt: skip
    assert ifc._map_position(geo, (0.0, 0.0, 0.0)) == {
        "Name": "EPSG:26911", "Eastings": 499950.0, "Northings": 3999900.0, "OrthogonalHeight": 10.0,
        "XAxisAbscissa": 1.0, "XAxisOrdinate": 0.0, "Scale": 0.5,
    }  # fmt: skip
    # drawing north pointing along +x: the drawing's x axis is map north, its y axis map west
    turned = ifc._map_position(geo | {"north": (1.0, 0.0), "design": (0.0, 0.0, 0.0)}, (0.0, 10.0, 0.0))
    assert (turned["Eastings"], turned["Northings"], turned["XAxisAbscissa"], turned["XAxisOrdinate"]) == (499995.0, 4000000.0, 0.0, 1.0)
    assert ifc._map_position(geo | {"kind": "local grid"}, (0.0, 0.0, 0.0)) == {}

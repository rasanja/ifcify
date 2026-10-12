import ifcopenshell.util.element

from dwg2ifc import derive, ifc
from dwg2ifc.entities import Drawing

Z = (0.0, 0.0, 1.0)
# heights a caller supplies, in metres; flat linework stores none
GIVEN = {"storey": 3.0, "door": 2.1, "window": 1.2, "sill": 0.9}


def plan() -> Drawing:
    """Two rooms outlined on a room layer, one of them drawn again as a hatch, inside a gross-area outline."""
    d = Drawing()
    d.units = 6  # metres
    d.blocks = {1: {"name": "*Model_Space", "base": (0, 0, 0), "units": 6}}
    d.spaces = {"model": 1}
    d.layers = {10: {"name": "A-AREA-ROOM", "color": 7}, 11: {"name": "A-ANNO", "color": 7}, 12: {"name": "A-WALL", "color": 7}}
    common = {"owner": 1, "invisible": 0, "color": None, "lineweight": 29}

    def outline(handle, points, layer=10):
        return common | {"type": "LWPOLYLINE", "handle": handle, "layer": layer, "points": points, "bulges": [], "closed": True,
                         "elevation": 0.0, "extrusion": Z}  # fmt: skip

    def text(handle, value, x, y):
        return common | {"type": "TEXT", "handle": handle, "layer": 11, "text": value, "point": (x, y, 0.0), "height": 0.2,
                         "rotation": 0.0, "extrusion": Z}  # fmt: skip

    a = [(0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0)]
    b = [(4.0, 0.0), (9.0, 0.0), (9.0, 3.0), (4.0, 3.0)]
    c = [(9.0, 0.0), (12.0, 0.0), (12.0, 3.0), (9.0, 3.0)]
    d.entities = [
        outline(100, a), outline(101, b), outline(104, c),
        outline(102, [(0.0, 0.0), (12.0, 0.0), (12.0, 3.0), (0.0, 3.0)]),  # the whole floor
        outline(103, a, layer=12),  # not on a room layer
        common | {"type": "HATCH", "handle": 110, "layer": 10, "elevation": 0.0, "extrusion": Z, "solid": True,
                  "paths": [{"flags": 1, "points": a, "bulges": [], "closed": True}]},
        text(200, "101", 2.0, 1.5), text(201, "OFFICE", 2.0, 1.0), text(202, "102", 6.0, 1.5), text(203, "STORE", 10.0, 1.5),
    ]  # fmt: skip
    return d


def test_rooms_come_from_outlines_and_the_text_inside_them():
    rooms = derive.spaces(plan(), 1, smallest=0.25)
    assert [(r["name"], r["area"]) for r in rooms] == [("STORE", 9.0), ("101", 12.0), ("102", 15.0)]
    assert rooms[1]["labels"] == ["101", "OFFICE"]


def test_rooms_become_ifc_spaces():
    model, _ = ifc.build(plan(), "plan", GIVEN)
    spaces = {s.Name: s for s in model.by_type("IfcSpace")}
    assert set(spaces) == {"101", "102", "STORE"}
    office = spaces["101"]
    assert office.LongName == "OFFICE"
    assert ifcopenshell.util.element.get_psets(office)["Qto_SpaceBaseQuantities"]["NetFloorArea"] == 12.0
    solid = office.Representation.Representations[0].Items[0]
    assert solid.Depth == 3.0 and len(solid.SweptArea.OuterCurve.Points.CoordList) == 5
    assert office.Decomposes[0].RelatingObject.is_a("IfcBuildingStorey")


def test_bulged_outline_area():
    # a circle of radius 1 drawn as two half-circle bulges
    outline = derive._outline([(1.0, 0.0), (-1.0, 0.0)], [1.0, 1.0])
    assert abs(abs(derive.area(outline)) - 3.14159) < 0.05  # the drawn outline is chords, about 1 percent short
    assert abs(derive.exact_area([(1.0, 0.0), (-1.0, 0.0)], [1.0, 1.0]) - 3.141592653589793) < 1e-12
    # a 2 x 2 square with one side bulged outwards as a half circle of radius 1, drawn in either direction
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
    assert abs(derive.exact_area(square, [0.0, 1.0, 0.0, 0.0]) - (4 + 3.141592653589793 / 2)) < 1e-12
    assert abs(derive.exact_area(square[::-1], [0.0, -1.0, 0.0, 0.0]) - (4 + 3.141592653589793 / 2)) < 1e-12


def double_line_room() -> Drawing:
    """A 4 x 3 m room drawn the usual way: an outer and an inner rectangle 0.2 m apart, on a wall layer."""
    d = plan()
    common = {"owner": 1, "invisible": 0, "color": None, "lineweight": 29, "layer": 12, "extrusion": Z}

    def rectangle(handle, x1, y1, x2, y2):
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        return [common | {"type": "LINE", "handle": handle + i, "start": (*p, 0.0), "end": (*q, 0.0)}
                for i, (p, q) in enumerate(zip(corners, corners[1:] + corners[:1]))]  # fmt: skip

    far = common | {"type": "LINE", "handle": 399, "start": (-0.2, 5.0, 0.0), "end": (4.2, 5.0, 0.0)}  # 2 m away: no wall
    d.entities = rectangle(300, -0.2, -0.2, 4.2, 3.2) + rectangle(310, 0.0, 0.0, 4.0, 3.0) + [far]
    return d


def test_walls_come_from_parallel_line_pairs():
    walls = derive.walls(double_line_room(), 1, lambda name: name == "A-WALL", thinnest=0.04, thickest=0.65)
    assert sorted((round(w["thickness"], 6), round(__import__("math").dist(w["start"], w["end"]), 6)) for w in walls) == [
        (0.2, 3.0), (0.2, 3.0), (0.2, 4.0), (0.2, 4.0),
    ]  # fmt: skip
    south = next(w for w in walls if w["start"][1] == w["end"][1] and w["start"][1] < 0)
    assert south["start"][1] == -0.1  # the centre line, halfway between the two faces


def test_a_line_between_two_others_makes_two_walls():
    d = double_line_room()
    d.entities = [e | {"start": (0.0, y, 0.0), "end": (5.0, y, 0.0), "handle": 500 + i} for i, y in enumerate((0.0, 0.1, 0.3)) for e in d.entities[:1]]
    walls = derive.walls(d, 1, lambda name: True, thinnest=0.04, thickest=0.65)
    assert sorted(round(w["thickness"], 6) for w in walls) == [0.1, 0.2]


def test_walls_become_ifc_walls_with_types():
    model, _ = ifc.build(double_line_room(), "room", GIVEN)
    walls = model.by_type("IfcWall")
    assert len(walls) == 4
    kind = model.by_type("IfcWallType")[0]
    assert kind.Name == "Wall 200 mm" and set(kind.Types[0].RelatedObjects) == set(walls)
    wall = walls[0]
    body, axis = wall.Representation.Representations
    assert (axis.RepresentationIdentifier, body.RepresentationType) == ("Axis", "SweptSolid")
    box = body.Items[0]
    assert (box.SweptArea.YDim, box.Depth) == (0.2, 3.0) and box.SweptArea.XDim in (3.0, 4.0)
    usage = ifcopenshell.util.element.get_material(wall)
    assert usage.is_a("IfcMaterialLayerSetUsage") and usage.ForLayerSet.MaterialLayers[0].LayerThickness == 0.2


def test_columns_come_from_closed_shapes_on_column_layers():
    d = plan()
    d.layers[13] = {"name": "S-COLS", "color": 7}
    common = {"owner": 1, "invisible": 0, "color": None, "lineweight": 29, "layer": 13, "extrusion": Z}
    square = [(1.0, 1.0), (1.4, 1.0), (1.4, 1.4), (1.0, 1.4)]
    d.entities = [
        common | {"type": "LWPOLYLINE", "handle": 600, "points": square, "bulges": [], "closed": True, "elevation": 0.0},
        common | {"type": "HATCH", "handle": 601, "elevation": 0.0, "solid": True,
                  "paths": [{"flags": 1, "points": square, "bulges": [], "closed": True}]},  # the same column, hatched
        common | {"type": "CIRCLE", "handle": 602, "center": (5.0, 1.0, 0.0), "radius": 0.25},
        common | {"type": "CIRCLE", "handle": 603, "center": (8.0, 1.0, 0.0), "radius": 0.01},  # a mark, not a column
    ]  # fmt: skip
    model, _ = ifc.build(d, "columns", GIVEN)
    columns = model.by_type("IfcColumn")
    profiles = sorted(c.Representation.Representations[0].Items[0].SweptArea.is_a() for c in columns)
    assert profiles == ["IfcArbitraryClosedProfileDef", "IfcCircleProfileDef"]
    assert all(c.Representation.Representations[0].Items[0].Depth == 3.0 for c in columns)
    assert model.by_type("IfcBuildingStorey")[0].Elevation == 0.0


def wall_with_a_door() -> Drawing:
    """A 0.2 m wall along x from 0 to 5 m, broken between 2.0 and 2.9 m, with a door swing drawn in the gap."""
    d = plan()
    d.layers[14] = {"name": "A-DOOR", "color": 7}
    common = {"owner": 1, "invisible": 0, "color": None, "lineweight": 29, "layer": 12, "extrusion": Z}
    lines = [((0.0, y), (2.0, y)) for y in (0.0, 0.2)] + [((2.9, y), (5.0, y)) for y in (0.0, 0.2)]
    d.entities = [common | {"type": "LINE", "handle": 700 + i, "start": (*p, 0.0), "end": (*q, 0.0)} for i, (p, q) in enumerate(lines)]
    d.entities.append(common | {"type": "ARC", "handle": 710, "layer": 14, "center": (2.0, 0.2, 0.0), "radius": 0.9,
                                "start_angle": 0.0, "end_angle": 1.57})  # fmt: skip
    return d


def test_a_gap_with_a_door_in_it_is_an_opening_not_two_walls():
    model, _ = ifc.build(wall_with_a_door(), "door", GIVEN)
    (wall,) = model.by_type("IfcWall")
    assert wall.Representation.Representations[0].Items[0].SweptArea.XDim == 5.0
    (opening,) = model.by_type("IfcOpeningElement")
    assert opening.VoidsElements[0].RelatingBuildingElement == wall
    assert round(opening.Representation.Representations[0].Items[0].SweptArea.XDim, 6) == 0.9
    (door,) = model.by_type("IfcDoor")
    assert opening.HasFillings[0].RelatedBuildingElement == door
    assert (round(door.OverallWidth, 6), door.OverallHeight) == (0.9, 2.1)
    # the opening sits 2.0 m along the wall, so its middle is at 2.45 m
    assert round(opening.ObjectPlacement.RelativePlacement.Location.Coordinates[0], 6) == 2.45


def test_a_gap_with_nothing_in_it_stays_a_gap():
    d = wall_with_a_door()
    d.entities = [e for e in d.entities if e["type"] != "ARC"]
    model, _ = ifc.build(d, "gap")
    assert len(model.by_type("IfcWall")) == 2 and not model.by_type("IfcOpeningElement")


def test_without_a_height_nothing_is_invented():
    """No height in the drawing and none from the caller: walls, rooms and columns are flat outlines, openings are not cut."""
    d = wall_with_a_door()
    d.layers[13] = {"name": "S-COLS", "color": 7}
    d.entities += plan().entities[:1]
    d.entities.append({"owner": 1, "invisible": 0, "color": None, "lineweight": 29, "layer": 13, "extrusion": Z,
                       "type": "CIRCLE", "handle": 602, "center": (5.0, 1.0, 0.0), "radius": 0.25})  # fmt: skip
    model, _ = ifc.build(d, "flat")
    (wall,), (space,), (column,) = model.by_type("IfcWall"), model.by_type("IfcSpace"), model.by_type("IfcColumn")
    for product in (wall, space, column):
        shown = product.Representation.Representations[0]
        assert shown.RepresentationIdentifier == "FootPrint" and not shown.Items[0].is_a("IfcSolidModel")
    assert not model.by_type("IfcExtrudedAreaSolid") and not model.by_type("IfcOpeningElement")
    # the wall is still one wall across the door, 5 m long and 0.2 m wide in plan
    xs, ys = zip(*(p.Coordinates for p in wall.Representation.Representations[0].Items[0].Points))
    assert (min(xs), max(xs), min(ys), max(ys)) == (0.0, 5.0, -0.1, 0.1)
    said = ifcopenshell.util.element.get_psets(wall)["DWG_Derived"]["Height"]
    assert said.startswith("not in the drawing") and "assum" not in said


def test_a_height_stored_in_the_drawing_is_used():
    """Lines drawn with a thickness of 2.7 store the wall's height; that wins over what the caller gives."""
    d = double_line_room()
    for e in d.entities:
        e["thickness"] = 2.7
    model, _ = ifc.build(d, "thick", GIVEN)
    walls = model.by_type("IfcWall")
    assert len(walls) == 4 and {w.Representation.Representations[0].Items[0].Depth for w in walls} == {2.7}
    assert ifcopenshell.util.element.get_psets(walls[0])["DWG_Derived"]["Height"].startswith("stored in the drawing")


def test_a_room_outline_keeps_its_arcs():
    """A room with a half-round end: the outline is written with the arc itself, not straight pieces."""
    d = plan()
    d.entities = [e for e in d.entities if e["handle"] in (100, 200)]
    d.entities[0] = d.entities[0] | {"bulges": [0.0, 1.0, 0.0, 0.0]}  # the side from (4,0) to (4,3) bulges out
    model, _ = ifc.build(d, "round", GIVEN)
    (space,) = model.by_type("IfcSpace")
    outline = space.Representation.Representations[0].Items[0].SweptArea.OuterCurve
    assert [segment.is_a() for segment in outline.Segments] == ["IfcLineIndex", "IfcArcIndex", "IfcLineIndex", "IfcLineIndex"]
    assert (5.5, 1.5) in outline.Points.CoordList  # the arc's outermost point, 1.5 beyond the wall line
    # and its area is the rectangle plus the half disc, exactly
    area = ifcopenshell.util.element.get_psets(space)["Qto_SpaceBaseQuantities"]["NetFloorArea"]
    assert abs(area - (12 + 3.141592653589793 * 1.5**2 / 2)) < 1e-9

import ifcopenshell.util.element

from dwg2ifc import derive, ifc
from dwg2ifc.entities import Drawing

Z = (0.0, 0.0, 1.0)


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
    model, _ = ifc.build(plan(), "plan")
    spaces = {s.Name: s for s in model.by_type("IfcSpace")}
    assert set(spaces) == {"101", "102", "STORE"}
    office = spaces["101"]
    assert office.LongName == "OFFICE"
    assert ifcopenshell.util.element.get_psets(office)["Qto_SpaceBaseQuantities"]["NetFloorArea"] == 12.0
    solid = office.Representation.Representations[0].Items[0]
    assert solid.Depth == 3.0 and len(solid.SweptArea.OuterCurve.Points) == 5
    assert office.Decomposes[0].RelatingObject.is_a("IfcBuildingStorey")


def test_bulged_outline_area():
    # a circle of radius 1 drawn as two half-circle bulges
    outline = derive._outline([(1.0, 0.0), (-1.0, 0.0)], [1.0, 1.0])
    assert abs(abs(derive.area(outline)) - 3.14159) < 0.05

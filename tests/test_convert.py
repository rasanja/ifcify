from collections import Counter
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.validate
import pytest

from dwg2ifc import convert, entities

ASSETS = Path(__file__).parent.parent / "sample-assets"
# the sample drawings are not in the repository; these tests run only where they have been copied in
pytestmark = pytest.mark.skipif(not list(ASSETS.glob("*.dwg")), reason="sample-assets/ is not present")


def test_floor_plan_entities_decode_exactly():
    # every decoder checks it consumed exactly the object's bits, so a count match means no field was misread
    drawing = entities.load((ASSETS / "building001-0_floor1.dwg").read_bytes())
    assert Counter(e["type"] for e in drawing.entities) == {
        "LINE": 1447, "TEXT": 286, "HATCH": 284, "LWPOLYLINE": 198, "MTEXT": 72, "INSERT": 17, "ATTRIB": 12, "CIRCLE": 1,
    }  # fmt: skip
    assert not drawing.skipped
    assert all(e["layer"] in drawing.layers for e in drawing.entities)
    lobby = next(e for e in drawing.entities if e.get("text") == "LOBBY")
    assert drawing.layers[lobby["layer"]] and lobby["height"] > 0


def test_every_sample_decodes_without_unreadable_objects():
    for sample in ASSETS.glob("*.dwg"):
        skipped = entities.load(sample.read_bytes()).skipped
        assert not [kind for kind in skipped if "unreadable" in kind], sample.name


def test_bspline_points_follow_the_curve():
    # a clamped quadratic through (0,0) (1,2) (2,0) starts and ends on its end control points and peaks at (1,1)
    points = entities.bspline_points(2, [0, 0, 0, 1, 1, 1], [(0.0, 0.0), (1.0, 2.0), (2.0, 0.0)], [])
    assert points[0] == (0.0, 0.0) and points[-1] == (2.0, 0.0)
    assert points[len(points) // 2] == (1.0, 1.0)


class _Errors:
    def __init__(self):
        self.errors = []

    def error(self, message, *args):
        # ifcopenshell cannot evaluate IfcSurfaceWeightsPositive and reports its own TypeError as a violation
        text = str(message) % args if args else str(message)
        if "unsupported operand type" not in text:
            self.errors.append(text)

    def __getattr__(self, name):  # warning, info, set_state, ...
        return lambda *args, **kwargs: None


def test_sample_ifc_passes_schema_rules():
    for sample in ASSETS.glob("*.dwg"):
        text, skipped = convert(sample.read_bytes())
        model = ifcopenshell.file.from_string(text)
        log = _Errors()
        ifcopenshell.validate.validate(model, log, express_rules=True)
        assert log.errors == [], sample.name


def test_floor_plan_ifc_content():
    text, skipped = convert((ASSETS / "building001-0_floor1.dwg").read_bytes())
    model = ifcopenshell.file.from_string(text)
    # 4 of its layers are off, the grid among them
    assert skipped == {"on a layer that is off or frozen": 113}
    assert len(model.by_type("IfcAnnotation")) == 2152
    assert {layer.Name for layer in model.by_type("IfcPresentationLayerAssignment")} >= {"A-WALL", "A-DOOR", "A-GLAZ"}
    assert len(model.by_type("IfcStyledItem")) > 1700  # everything but text
    assert len(model.by_type("IfcMappedItem")) >= 17
    # the drawing sits 1,758 km from the origin; geometry is written near zero and the position kept as georeferencing
    conversion = model.by_type("IfcMapConversion")[0]
    assert abs(conversion.Eastings - 69221645) < 50 and abs(conversion.Northings - 27676200) < 50
    assert max(abs(c) for point in model.by_type("IfcCartesianPoint") for c in point.Coordinates) < 5000
    assert len(model.by_type("IfcAnnotationFillArea")) >= 284
    # zero-length edges crash geometry kernels, so none may be written
    for curve in model.by_type("IfcIndexedPolyCurve"):
        points = curve.Points.CoordList
        assert all(points[s[0][0] - 1] != points[s[0][-1] - 1] for s in curve.Segments)


def test_conference_room_solids_become_breps():
    text, skipped = convert((ASSETS / "visualization_-_conference_room.dwg").read_bytes())
    model = ifcopenshell.file.from_string(text)
    # 327 ACIS faces; the 6 on self-intersecting tori have no IFC surface and are reported instead
    assert len(model.by_type("IfcAdvancedFace")) == 327
    assert skipped == {"LIGHT": 8}
    assert len(model.by_type("IfcBuildingElementProxy")) == 32  # 30 solids, 2 surfaces
    # the block "Fluorescent Fixture" is a type, and its six inserts are light fixtures of that type
    fixtures = model.by_type("IfcLightFixture")
    kind = model.by_type("IfcLightFixtureType")[0]
    assert len(fixtures) == 6 and kind.Name == "Fluorescent Fixture"
    assert set(model.by_type("IfcRelDefinesByType")[0].RelatedObjects) == set(fixtures)
    assert all(f.Representation.Representations[0].Items[0].MappingSource in kind.RepresentationMaps for f in fixtures)
    assert model.by_type("IfcBSplineSurfaceWithKnots") and model.by_type("IfcToroidalSurface")


def test_unknown_entities_are_drawn_from_their_proxy_graphics():
    # MULTILEADER has no decoder; each one carries a drawing of itself (leader lines, arrowheads, text)
    drawing = entities.load((ASSETS / "architectural_-_annotation_scaling_and_multileaders.dwg").read_bytes())
    leaders = [e for e in drawing.entities if e["type"] == "MULTILEADER"]
    assert len(leaders) == 36 and "MULTILEADER" not in drawing.skipped
    kinds = Counter(part["type"] for e in leaders for part in e["parts"])
    assert kinds == {"POLYLINE_3D": 111, "PROXYTEXT": 62, "ELLIPSE": 3}
    assert "EPDM ROOF CONSTRUCTION:" in {part.get("text") for e in leaders for part in e["parts"]}
    text, skipped = convert((ASSETS / "architectural_-_annotation_scaling_and_multileaders.dwg").read_bytes())
    assert "MULTILEADER" not in skipped and "'EPDM ROOF CONSTRUCTION:'" in text


def test_title_block_drawings_export_their_layouts():
    # nothing in model space: the drawing is its paper-space layouts, one storey each
    sample = ASSETS / "LACPW_LDD_SEWER_BORDER.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    model = ifcopenshell.file.from_string(convert(sample.read_bytes())[0])
    assert [storey.Name for storey in model.by_type("IfcBuildingStorey")] == ["Layout 1", "Layout 2"]
    assert len(model.by_type("IfcAnnotation")) > 500


def test_no_sample_reports_unconverted_drawing_entities():
    # across all samples only lights (nothing to draw) and things AutoCAD itself would not show in the model
    allowed = {"LIGHT", "on a layer that is off or frozen", "marked invisible", "on a paper-space layout"}
    for sample in ASSETS.glob("*.dwg"):
        skipped = convert(sample.read_bytes())[1]
        assert set(skipped) <= allowed, sample.name


def test_floor_plan_rooms_become_spaces():
    model = ifcopenshell.file.from_string(convert((ASSETS / "building001-0_floor1.dwg").read_bytes())[0])
    spaces = {s.Name: s for s in model.by_type("IfcSpace")}
    assert len(spaces) == 71  # the drawing numbers 71 rooms
    psets = ifcopenshell.util.element.get_psets(spaces["126A"])
    # the drawing labels this room "405 SF"
    assert abs(psets["Qto_SpaceBaseQuantities"]["NetFloorArea"] / 0.3048**2 - 405) < 1
    assert "405 SF" in psets["DWG_Room"]["Labels"]
    # the drawing stores each outline's area in a field object; that value is carried as it is
    assert psets["DWG_Room"]["StoredArea"] == "405 SF"
    stored = [s for s in spaces.values() if "StoredArea" in ifcopenshell.util.element.get_psets(s)["DWG_Room"]]
    assert len(stored) == 66  # five rooms have no area field
    for space in stored:  # the quantity is that stored number, which rounds to the text the drawing shows
        values = ifcopenshell.util.element.get_psets(space)
        assert values["DWG_Room"]["AreaSource"] == "stored in the drawing by AutoCAD"
        assert abs(values["Qto_SpaceBaseQuantities"]["NetFloorArea"] / 0.3048**2 - float(values["DWG_Room"]["StoredArea"].split()[0].replace(",", ""))) <= 0.5
    measured = [s for s in spaces.values() if s not in stored]
    assert all(ifcopenshell.util.element.get_psets(s)["DWG_Room"]["AreaSource"] == "measured from the outline" for s in measured)


def test_stored_field_values_match_our_own_measurement():
    # AutoCAD's stored area for each room outline and the area we measure from the same outline: within 0.01 percent
    from dwg2ifc import derive

    drawing = entities.load((ASSETS / "building001-0_floor1.dwg").read_bytes())
    by_handle = {e["handle"]: e for e in drawing.entities}
    assert len(drawing.fields) == 66
    for handle, [(name, text, number)] in drawing.fields.items():
        outline = by_handle[handle]
        assert name == "Area" and text.endswith(" SF")
        assert abs(derive.exact_area(outline["points"], outline["bulges"]) - number) / number < 1e-4


def test_plan_walls_become_ifc_walls():
    sample = ASSETS / "architectural_example-imperial.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    model = ifcopenshell.file.from_string(convert(sample.read_bytes())[0])
    # the drawing's own parameter says bldWALL=6": every wall is 6 inches, 152 mm, thick
    assert [t.Name for t in model.by_type("IfcWallType")] == ["Wall 152 mm"]
    assert len(model.by_type("IfcWall")) == 14
    assert {round(w.Representation.Representations[0].Items[0].SweptArea.YDim, 6) for w in model.by_type("IfcWall")} == {6.0}


def test_linetypes_reach_the_ifc():
    sample = ASSETS / "carriage.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    model = ifcopenshell.file.from_string(convert(sample.read_bytes())[0])
    fonts = {font.Name: [(p.VisibleSegmentLength, p.InvisibleSegmentLength) for p in font.PatternList] for font in model.by_type("IfcCurveStyleFont")}
    # AutoCAD's standard centre line: long dash, gap, short dash, gap
    assert fonts["CENTER"] == [(1.25, 0.25), (0.25, 0.25)]
    assert fonts["HIDDEN"] == [(0.25, 0.125)]
    assert any(style.CurveFont for style in model.by_type("IfcCurveStyle"))

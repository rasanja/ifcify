from collections import Counter
from pathlib import Path

import ifcopenshell
import ifcopenshell.validate

from dwg2ifc import convert, entities

ASSETS = Path(__file__).parent.parent / "sample-assets"


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
        if "unsupported operand type" not in str(message):
            self.errors.append(str(message))

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
    assert len(model.by_type("IfcAnnotation")) > 2200
    assert {layer.Name for layer in model.by_type("IfcPresentationLayerAssignment")} >= {"A-WALL", "A-DOOR", "A-GLAZ"}
    assert len(model.by_type("IfcMappedItem")) >= 17
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
    assert len(model.by_type("IfcBuildingElementProxy")) == 38  # 30 solids, 2 surfaces, 6 light fixtures
    assert model.by_type("IfcBSplineSurfaceWithKnots") and model.by_type("IfcToroidalSurface")

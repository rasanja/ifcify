from collections import Counter
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.element
import ifcopenshell.util.unit
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
    assert skipped == {}
    lights = model.by_type("IfcLightFixture")
    # six point lights, and six inserts of the block "Fluorescent Fixture" (which holds two lights of its own)
    assert len(lights) == 12 and {light.Name for light in lights} >= {"Pointlight1", "Pointlight6"}
    # sizes AutoCAD keeps for solids made in one step, and the material given to faces in a solid's history
    stored = [ifcopenshell.util.element.get_psets(p).get("DWG_Stored", {}) for p in model.by_type("IfcElement")]
    assert sum(1 for s in stored if s.get("Primitive") == "Cylinder" and float(s["BaseRadius"]) == 3.298655103182199) == 6
    assert [m.Name for m in model.by_type("IfcMaterial")] == ["Blue"]
    assert len(model.by_type("IfcBuildingElementProxy")) == 32  # 30 solids, 2 surfaces
    # the block "Fluorescent Fixture" is a type, and its six inserts are light fixtures of that type
    fixtures = [light for light in lights if light.ObjectType == "INSERT"]
    kind = model.by_type("IfcLightFixtureType")[0]
    assert len(fixtures) == 6 and kind.Name == "Fluorescent Fixture"
    assert set(model.by_type("IfcRelDefinesByType")[0].RelatedObjects) == set(fixtures)
    assert all(f.Representation.Representations[0].Items[0].MappingSource in kind.RepresentationMaps for f in fixtures)
    assert model.by_type("IfcBSplineSurfaceWithKnots") and model.by_type("IfcToroidalSurface")


def test_multileaders_keep_their_text_whole_and_their_drawn_linework():
    drawing = entities.load((ASSETS / "architectural_-_annotation_scaling_and_multileaders.dwg").read_bytes())
    leaders = [e for e in drawing.entities if e["type"] == "MULTILEADER"]
    assert len(leaders) == 36 and "MULTILEADER" not in drawing.skipped
    # 33 hold text, read as one paragraph each; 3 hold a block, drawn from the picture they carry (6 attribute texts)
    kinds = Counter(part["type"] for e in leaders for part in e["parts"])
    assert kinds == {"POLYLINE_3D": 111, "MTEXT": 33, "PROXYTEXT": 6, "ELLIPSE": 3}
    assert any(part.get("text", "").startswith("EPDM ROOF CONSTRUCTION:\\PEPDM ROOFING MEMBRANE") for e in leaders for part in e["parts"])
    # our own reading of the leader lines agrees with the picture: each line's corners are drawn there
    for e in leaders:
        drawn = {tuple(round(c, 6) for c in p) for part in e["parts"] if part["type"] == "POLYLINE_3D" for p in part["points"]}
        for line in (part for part in e["native"] if part["type"] == "POLYLINE_3D"):
            assert {tuple(round(c, 6) for c in p) for p in line["points"][-2:]} <= drawn
    text, skipped = convert((ASSETS / "architectural_-_annotation_scaling_and_multileaders.dwg").read_bytes())
    assert "MULTILEADER" not in skipped and "EPDM ROOF CONSTRUCTION:" in text


def test_title_block_drawings_export_their_layouts():
    # nothing in model space: the drawing is its paper-space layouts, one storey each
    sample = ASSETS / "LACPW_LDD_SEWER_BORDER.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    model = ifcopenshell.file.from_string(convert(sample.read_bytes())[0])
    assert [storey.Name for storey in model.by_type("IfcBuildingStorey")] == ["TS-SS-DL", "PPS-SS-DL"]  # the layouts' own names
    assert len(model.by_type("IfcAnnotation")) > 500


def test_no_sample_reports_unconverted_drawing_entities():
    # across all samples only things AutoCAD itself would not show in the model
    allowed = {"on a layer that is off or frozen", "marked invisible", "on a paper-space layout"}
    # known gaps, by file: two corner blends ("VBL_SURF") stored as a recipe with no surface to read
    known = {"visualization_-_condominium_with_skylight.dwg": {"3DSOLID face (unsupported geometry)": 2}}
    for sample in ASSETS.glob("*.dwg"):
        skipped = convert(sample.read_bytes())[1]
        extra = {reason: count for reason, count in skipped.items() if reason not in allowed}
        assert extra == known.get(sample.name, {}), sample.name


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
    model = ifcopenshell.file.from_string(convert(sample.read_bytes(), heights={"storey": 3.0})[0])
    # the drawing's own parameter says bldWALL=6": every wall is 6 inches, 152 mm, thick
    assert [t.Name for t in model.by_type("IfcWallType")] == ["Wall 152 mm"]
    assert len(model.by_type("IfcWall")) == 14
    assert {round(w.Representation.Representations[0].Items[0].SweptArea.YDim, 6) for w in model.by_type("IfcWall")} == {6.0}


def test_linetypes_reach_the_ifc():
    sample = ASSETS / "carriage.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    model = ifcopenshell.file.from_string(convert(sample.read_bytes())[0])
    fonts = {(font.Name, tuple((p.VisibleSegmentLength, p.InvisibleSegmentLength) for p in font.PatternList)) for font in model.by_type("IfcCurveStyleFont")}
    # AutoCAD's standard centre line (long dash, gap, short dash, gap), at this drawing's linetype scale of 4
    assert entities.load(sample.read_bytes()).ltscale == 4.0
    assert ("CENTER", ((5.0, 1.0), (1.0, 1.0))) in fonts and ("HIDDEN", ((1.0, 0.5),)) in fonts
    # three entities carry their own scale of 0.75 on top of that
    assert ("HIDDEN", ((0.75, 0.375),)) in fonts
    assert any(style.CurveFont for style in model.by_type("IfcCurveStyle"))


def test_groups_fonts_and_attached_records_reach_the_ifc():
    model = ifcopenshell.file.from_string(convert((ASSETS / "building.dwg").read_bytes())[0])
    groups = model.by_type("IfcGroup")
    assert groups and all(rel.RelatedObjects for group in groups for rel in group.IsGroupedBy)
    assert {style.TextFontStyle.Name for style in model.by_type("IfcTextStyle")} >= {"Arial", "txt.shx"}
    # a facilities program tagged the room outlines of the floor plan; its record travels with each room
    model = ifcopenshell.file.from_string(convert((ASSETS / "building001-0_floor1.dwg").read_bytes())[0])
    tagged = [p for p in model.by_type("IfcPropertySet") if p.Name == "DWG_Record_Planon_Space"]
    assert len(tagged) >= 70 and "1000=Space" in tagged[0].HasProperties[0].NominalValue.wrappedValue


def test_solids_saved_without_a_ready_made_surface():
    """ACIS 21500: rational surfaces of revolution, and extrusions stored as a curve and a direction."""
    sample = ASSETS / "visualization_-_condominium_with_skylight.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    drawing = entities.load(sample.read_bytes())
    assert not any("unreadable" in reason for reason in drawing.skipped)
    surfaces = [f["surface"] for e in drawing.entities for b in e.get("bodies") or () for f in b["faces"]]
    # still without a surface: two corner blends, and two offsets of an extrusion in blocks the drawing never places
    assert sum(1 for s in surfaces if s is None) == 4
    rational = [s for s in surfaces if s and s[0] == "bspline" and len(s[7][0][0]) == 4]
    assert len(rational) == 10 and all(0 < p[3] <= 1 for s in rational for row in s[7] for p in row)
    # an extrusion: two rows of control points, the second the first moved along x by the stored length
    extruded = [s for s in surfaces if s and s[0] == "bspline" and s[2] == 1 and s[5] == [0.0, 2.9363615069628164]]
    assert len(extruded) >= 2
    for low, high in extruded[0][7]:
        assert (high[1], high[2]) == (low[1], low[2]) and high[0] - low[0] == pytest.approx(2.9363615069628164)


def test_architecture_objects_become_elements_with_their_stored_sizes():
    """AutoCAD Architecture walls, doors, windows and openings: sizes are the objects' own, nothing is assumed."""
    sample = ASSETS / "THREE BEDROOM HOUSE PLAN.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    drawing = entities.load(sample.read_bytes())
    assert not drawing.skipped
    found = Counter((e["type"], e["aec"]["height"], e["aec"]["width"]) for e in drawing.entities if "aec" in e)
    assert found[("AEC_WALL", 3200.0, 230.0)] == 46 and found[("AEC_WALL", 3200.0, 300.0)] == 2
    assert found[("AEC_DOOR", 2500.0, 900.0)] == 8 and found[("AEC_WINDOW", 1500.0, 1500.0)] == 4
    assert sum(found.values()) == 80
    # every door, window and opening is tied to a wall, and placed square to the world's vertical
    walls = {e["handle"] for e in drawing.entities if e["type"] == "AEC_WALL"}
    hosted = [e["aec"] for e in drawing.entities if "aec" in e and e["type"] != "AEC_WALL"]
    assert len(hosted) == 32 and all(drawing.anchors[a["anchor"]] in walls and a["axes"][2] == (0.0, 0.0, 1.0) for a in hosted)

    model = ifcopenshell.file.from_string(convert(sample.read_bytes())[0])
    # only the real walls: none guessed from linework next to them
    assert Counter(p.is_a() for p in model.by_type("IfcElement")) == {"IfcWall": 48, "IfcOpeningElement": 32, "IfcWindow": 12, "IfcDoor": 10}
    assert len(model.by_type("IfcRelVoidsElement")) == 32 and len(model.by_type("IfcRelFillsElement")) == 22
    types = {(t.is_a(), t.Name): len(t.Types[0].RelatedObjects) for t in model.by_type("IfcTypeProduct") if t.Types}
    assert types == {("IfcWallType", "Brick-X"): 48, ("IfcDoorType", "Standard"): 9, ("IfcDoorType", "Hinged - Double"): 1, ("IfcWindowType", "Standard"): 12}
    wall = model.by_type("IfcWall")[0]
    stored = ifcopenshell.util.element.get_psets(wall)["DWG_Architecture"]
    assert (stored["Style"], stored["Height"], stored["Width"]) == ("Brick-X", "3200.0", "230.0")
    body = wall.Representation.Representations[0].Items[0]
    assert (body.Depth, body.SweptArea.YDim, body.SweptArea.XDim) == (3200.0, 230.0, pytest.approx(float(stored["Length"])))
    # each opening sits inside its wall: along its length, and between its floor and its top
    for rel in model.by_type("IfcRelVoidsElement"):
        host, hole = rel.RelatingBuildingElement.Representation.Representations[0].Items[0], rel.RelatedOpeningElement
        x, _, z = hole.ObjectPlacement.RelativePlacement.Location.Coordinates
        cut = hole.Representation.Representations[0].Items[0]
        assert -1e-6 <= x - cut.SweptArea.XDim / 2 and x + cut.SweptArea.XDim / 2 <= host.SweptArea.XDim + 1e-6
        assert -1e-6 <= z and z + cut.Depth <= host.Depth + 1e-6
    door = next(p for p in model.by_type("IfcDoor"))
    assert (door.OverallWidth, door.OverallHeight) in {(900.0, 2500.0), (800.0, 2500.0), (1200.0, 2500.0)}


def test_an_architecture_drawing_is_in_the_unit_it_was_set_up_in():
    """The house plan declares inches (INSUNITS) but was set up, and drawn, in millimetres."""
    sample = ASSETS / "THREE BEDROOM HOUSE PLAN.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    drawing = entities.load(sample.read_bytes())
    assert drawing.units == 1 and list(drawing.aec_units.values()) == [4]
    model = ifcopenshell.file.from_string(convert(sample.read_bytes())[0])
    told = {p.Name: p.NominalValue.wrappedValue for s in model.by_type("IfcPropertySet") if s.Name == "DWG_Units" for p in s.HasProperties}
    assert told["Unit"] == "millimetres" and "INSUNITS says inches" in told["Source"]
    assert ifcopenshell.util.unit.calculate_unit_scale(model) == pytest.approx(0.001)  # a 3200-high wall is 3.2 m
    # where the two agree nothing is said about a clash
    other = ASSETS / "building001-0_floor1.dwg"
    if other.exists():
        drawing = entities.load(other.read_bytes())
        assert drawing.units == 1 and list(drawing.aec_units.values()) == [1]


def test_rounded_corners_survive_the_geometry_kernel():
    """A box with rounded corners has eight patches of a sphere. IfcOpenShell does not convert
    IfcSphericalSurface (the patches went missing, and one drawing crashed it), so they are written as
    an arc turned about an axis. Built in a separate process: a crash must fail the test, not end it."""
    import subprocess
    import sys

    sample = ASSETS / "visualization_-_condominium_with_skylight.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    text = convert(sample.read_bytes())[0]
    model = ifcopenshell.file.from_string(text)
    solid = next(p for p in model.by_type("IfcBuildingElementProxy") if p.Tag == "40D")
    faces = solid.Representation.Representations[0].Items[0].Outer.CfsFaces
    assert not model.by_type("IfcSphericalSurface") and sum(1 for f in faces if f.FaceSurface.is_a("IfcSurfaceOfRevolution")) >= 8
    script = (
        "import sys, ifcopenshell, ifcopenshell.geom\n"
        "m = ifcopenshell.file.from_string(sys.stdin.read())\n"
        "p = next(p for p in m.by_type('IfcBuildingElementProxy') if p.Tag == '40D')\n"
        "s = ifcopenshell.geom.settings(); s.set('use-world-coords', True)\n"
        "v = ifcopenshell.geom.create_shape(s, p).geometry.verts\n"
        "print(max(v[0::3]) - min(v[0::3]))\n"
    )
    done = subprocess.run([sys.executable, "-c", script], input=text, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-300:]
    # the solid's extent along x, as AutoCAD lists it: 368.6623 to 454.4151 inches
    assert float(done.stdout) * 39.37007874 == pytest.approx(454.41510167 - 368.66230971, abs=0.01)


def test_tubes_are_marked_as_closing_on_themselves():
    sample = ASSETS / "visualization_-_conference_room.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    model = ifcopenshell.file.from_string(convert(sample.read_bytes())[0])
    closed = [(s.UClosed, s.VClosed) for s in model.by_type("IfcBSplineSurfaceWithKnots")]
    assert closed.count((True, False)) + closed.count((False, True)) >= 3 and (False, False) in closed

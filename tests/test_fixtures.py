"""Checks decoded values against AutoCAD's own DXF export of the same drawing, for every DWG release.

tests/fixtures is built by tools/make_fixtures.sh: one of every entity type we can script, saved as
R14 through R2018 plus DXF.
"""

import math
from collections import Counter
from pathlib import Path

import ifcopenshell
import pytest

from dwg2ifc import convert, entities

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
def test_converts_to_ifc(path):
    text, skipped = convert(path.read_bytes())
    assert "FILE_SCHEMA(('IFC4X3_ADD2'))" in text
    assert set(skipped) <= {"RAY", "XLINE", "WIPEOUT"}
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
    assert len(model.by_type("IfcShellBasedSurfaceModel")) == 1
    box = max(model.by_type("IfcAdvancedBrep"), key=lambda b: len(b.Outer.CfsFaces))
    corners = {v.VertexGeometry.Coordinates for face in box.Outer.CfsFaces for bound in face.Bounds
               for edge in bound.Bound.EdgeList for v in (edge.EdgeElement.EdgeStart, edge.EdgeElement.EdgeEnd)}  # fmt: skip
    assert corners == {(x, y, z) for x in (300.0, 310.0) for y in (0.0, 8.0) for z in (0.0, 6.0)}  # as drawn: BOX 300,0,0 310,8,6
    if "entities_14" not in path.name:
        assert ".LENGTHUNIT.,.MILLI.,.METRE." in text

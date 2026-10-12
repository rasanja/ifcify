"""Our output against AutoCAD's own figures for the same drawing.

tests/fixtures/entities_autocad.txt is what AutoCAD printed for tests/fixtures/entities_2018.dwg: every
object's length, area or bounding box (LIST), each solid's volume and surface area (MASSPROP and AREA,
one solid at a time), and the solids' total. `tools/accuracy.py --save-log` rewrites it; that needs
AutoCAD, this test does not.
"""

import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import accuracy  # noqa: E402


def test_fixture_agrees_with_autocad():
    rows = accuracy.compare(FIXTURES / "entities_2018.dwg", (FIXTURES / "entities_autocad.txt").read_text())
    worst = {}
    for source, kind, _, quantity, _, _, difference in rows:
        worst[source, kind, quantity] = max(worst.get((source, kind, quantity), 0.0), difference)
    # what we read from the drawing: AutoCAD prints eight decimals, and we agree to all of them
    exact = {key: value for key, value in worst.items() if key[0] == "decoded" and key[1] != "HELIX"}
    assert len(exact) >= 11 and max(exact.values()) < 1e-6, exact
    # a helix is written as the spline the drawing stores for it, which follows the true helix this closely
    assert worst["decoded", "HELIX", "length"] < 1e-3
    # the finished IFC, built by a geometry kernel: curved faces are tessellated, hence the looser bound
    built = {key: value for key, value in worst.items() if key[0] == "ifc"}
    assert {("ifc", "3DSOLID", "volume"), ("ifc", "3DSOLID", "surface area"), ("ifc", "all 3DSOLIDs", "centroid")} <= set(built)
    assert max(built.values()) < 3e-3, built
    # every one of the eight solids was measured on its own
    assert sum(1 for row in rows if row[1] == "3DSOLID" and row[3] == "volume") == 8


def test_a_spline_is_cut_in_two_without_moving_it():
    from dwg2ifc import ifc

    # one cubic segment, cut at its middle: the two halves are the ones de Casteljau's construction gives
    (knots, counts, left), (_, _, right) = ifc._halves(3, [0.0, 1.0], [3, 3], [(0.0, 0.0), (0.0, 8.0), (8.0, 8.0), (8.0, 0.0)])
    assert (knots, counts) == ([0.0, 0.5], [3, 3])
    assert left == [(0.0, 0.0), (0.0, 4.0), (2.0, 6.0), (4.0, 6.0)] and right == [(4.0, 6.0), (6.0, 6.0), (8.0, 4.0), (8.0, 0.0)]


def test_a_swept_tube_is_built_whole():
    """A cable swept along a path: one face on a surface closed on itself. Cut lengthwise, a kernel builds it."""
    import copy

    import pytest

    from dwg2ifc import entities, ifc

    sample = Path(__file__).parent.parent / "sample-assets" / "visualization_-_conference_room.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    drawing = entities.load(sample.read_bytes())
    one = copy.copy(drawing)
    one.entities = [e for e in drawing.entities if e["handle"] == 0x7D5]
    faces = ifc._open_tubes(one.entities[0]["bodies"][0]["faces"])
    assert len(one.entities[0]["bodies"][0]["faces"]) == 3 and len(faces) == 4  # the tube in two halves, and its two ends
    assert all(len(loop) == 4 for face in faces[:2] for loop in face["loops"]) and all(len(face["loops"][0]) == 2 for face in faces[2:])
    built, _ = accuracy.built(ifc.build(one, "tube")[0])
    assert built[0x7D5]["area"] == pytest.approx(11.7376, rel=2e-3)  # AutoCAD's own figure for this solid

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

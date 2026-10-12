from pathlib import Path

import ifcopenshell
import pytest
from fastapi.testclient import TestClient

from dwg2ifc.api import app

ASSETS = Path(__file__).parent.parent / "sample-assets"
SAMPLES = sorted(ASSETS.glob("*.dwg"))

client = TestClient(app)


@pytest.fixture(autouse=True)
def root(monkeypatch):
    monkeypatch.setenv("DWG2IFC_ROOT", str(ASSETS))


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda p: p.name)
def test_bytes_and_path_give_ifc4x3(sample):
    by_bytes = client.post("/convert", content=sample.read_bytes())
    by_path = client.post("/convert", params={"path": sample.name})
    for r in (by_bytes, by_path):
        assert r.status_code == 200, r.text
        model = ifcopenshell.file.from_string(r.text)
        assert model.schema == "IFC4X3"
        assert model.by_type("IfcAnnotation")
    assert by_bytes.headers["X-DWG-Version"] == by_path.headers["X-DWG-Version"]
    assert "unreadable" not in by_bytes.headers["X-DWG-Skipped"]


def test_rejects_non_dwg():
    assert client.post("/convert", content=b"not a drawing").status_code == 422


def test_rejects_password_protected_drawings():
    for release in ("2004", "2007", "2018"):
        data = bytearray((Path(__file__).parent / "fixtures" / f"entities_{release}.dwg").read_bytes())
        data[0x18] |= 1  # the "data is encrypted" flag
        response = client.post("/convert", content=bytes(data))
        assert response.status_code == 422 and "password" in response.text


def test_missing_path_is_404():
    assert client.post("/convert", params={"path": "no-such-drawing.dwg"}).status_code == 404


def test_rejects_truncated_dwg():
    fixture = Path(__file__).parent / "fixtures" / "entities_2018.dwg"
    assert client.post("/convert", content=fixture.read_bytes()[:5000]).status_code == 422


def test_refuses_oversized_drawings(monkeypatch):
    monkeypatch.setenv("DWG2IFC_MAX_MB", "0.001")
    fixture = Path(__file__).parent / "fixtures" / "entities_2018.dwg"
    assert client.post("/convert", content=fixture.read_bytes()).status_code == 413
    monkeypatch.setenv("DWG2IFC_ROOT", str(fixture.parent))
    assert client.post("/convert", params={"path": fixture.name}).status_code == 413


def test_path_cannot_escape_root():
    assert client.post("/convert", params={"path": "../pyproject.toml"}).status_code == 403


def test_docs_work_offline():
    page = client.get("/docs").text
    assert "/static/swagger-ui-bundle.js" in page
    assert "http://" not in page and "https://" not in page
    assert client.get("/static/swagger-ui-bundle.js").status_code == 200
    assert "/convert" in client.get("/openapi.json").json()["paths"]


def test_parallel_requests_run_in_separate_processes():
    """Six requests at once: all answered correctly, by a pool of worker processes rather than one after another."""
    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

    from dwg2ifc import api

    fixtures = sorted((Path(__file__).parent / "fixtures").glob("entities_20*.dwg"))
    with ThreadPoolExecutor(6) as threads:
        answers = list(threads.map(lambda path: (path, client.post("/convert", content=path.read_bytes())), fixtures))
    assert len(answers) == 6 and all(r.status_code == 200 for _, r in answers)
    # each caller got its own drawing back, not a neighbour's
    assert [r.headers["X-DWG-Version"] for _, r in answers] == ["R" + path.stem[-4:] for path, _ in answers]
    assert isinstance(api._executor(), ProcessPoolExecutor) and api._workers() >= 1
    assert len({process.pid for process in api._executor()._processes.values()}) >= min(2, api._workers())


def test_heights_come_from_the_request_or_not_at_all():
    sample = ASSETS / "architectural_example-imperial.dwg"
    if not sample.exists():
        pytest.skip("sample not present")
    flat = ifcopenshell.file.from_string(client.post("/convert", content=sample.read_bytes()).text)
    assert flat.by_type("IfcWall") and not any(w.Representation.Representations[0].RepresentationIdentifier == "Body" for w in flat.by_type("IfcWall"))
    tall = ifcopenshell.file.from_string(client.post("/convert", content=sample.read_bytes(), params={"storey_height": 2.5}).text)
    depths = {w.Representation.Representations[0].Items[0].Depth for w in tall.by_type("IfcWall")}
    assert len(depths) == 1 and depths.pop() == pytest.approx(2.5 / 0.0254)  # the drawing is in inches
    assert client.post("/convert", content=sample.read_bytes(), params={"storey_height": -1}).status_code == 422
    assert {"storey_height", "door_height", "window_height", "window_sill"} <= {p["name"] for p in client.get("/openapi.json").json()["paths"]["/convert"]["post"]["parameters"]}

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

# dwg2ifc

A local web service that converts AutoCAD DWG drawings to IFC4X3.

It reads the DWG binary format directly. It does not need AutoCAD, accoreconsole or any other Autodesk
software, makes no network calls, and runs headless.

## What it converts

DWG releases R14, R2000, R2004, R2007, R2010, R2013 and R2018.

| DWG | IFC |
|-----|-----|
| Lines, arcs, circles, ellipses, polylines, splines, leaders | curves in an `IfcAnnotation` |
| Text, multiline text, attributes | `IfcTextLiteralWithExtent` |
| Hatches, 2D solids | `IfcAnnotationFillArea` |
| Blocks and their inserts, dimensions | `IfcRepresentationMap` placed by `IfcMappedItem` |
| 3D solids, regions, surfaces | `IfcAdvancedBrep` or a surface model, in an `IfcBuildingElementProxy` |
| Meshes, polyface meshes, 3D faces | `IfcPolygonalFaceSet` |
| Multileaders, wipeouts and custom entities | drawn from the picture of themselves they carry, when they have one |
| Layers | `IfcPresentationLayerAssignment` |
| Drawing units | IFC length unit |

Everything goes into one storey: `IfcProject > IfcSite > IfcBuilding > IfcBuildingStorey`.

A drawing placed far from the origin, as survey drawings are, is written relative to a local origin so
viewers can display it. Its real position is kept in the file as `IfcMapConversion`.

Not converted yet: rays and construction lines, lights, custom entities that carry no picture of
themselves, colours and line weights, and paper space. Anything left out is counted in the
`X-DWG-Skipped` response header, so nothing is dropped silently.

## Install

Needs Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
```

## Run

```sh
uv run dwg2ifc
```

The service listens on http://127.0.0.1:8000. Interactive API docs are at http://127.0.0.1:8000/docs
and work offline.

| Environment variable | Default | Meaning |
|----------------------|---------|---------|
| `DWG2IFC_PORT` | `8000` | Port to listen on |
| `DWG2IFC_ROOT` | current directory | Directory that `?path=` references are read from |

## Use

There is one endpoint, `POST /convert`. Give it the drawing in either of two ways.

Send the DWG bytes as the request body:

```sh
curl --data-binary @sample-assets/building001-0_floor1.dwg \
     -o floor1.ifc http://127.0.0.1:8000/convert
```

Or name a file under `DWG2IFC_ROOT`:

```sh
DWG2IFC_ROOT=sample-assets uv run dwg2ifc
curl -X POST -o floor1.ifc "http://127.0.0.1:8000/convert?path=building001-0_floor1.dwg"
```

The response body is the IFC file (STEP text, `application/x-step`). Two headers describe the result:

| Header | Example | Meaning |
|--------|---------|---------|
| `X-DWG-Version` | `R2013` | DWG release of the input |
| `X-DWG-Skipped` | `LIGHT=8` | Entities left out, counted by type. Empty when everything converted |

Add `-D -` to `curl` to see them.

| Status | Meaning |
|--------|---------|
| 200 | Converted |
| 403 | `path` points outside `DWG2IFC_ROOT` |
| 404 | `path` does not exist |
| 422 | Not a DWG file, an unsupported release, or a corrupt file |

From Python, without the web service:

```python
from pathlib import Path
from dwg2ifc import convert

ifc_text, skipped = convert(Path("drawing.dwg").read_bytes())
Path("drawing.ifc").write_text(ifc_text)
```

## Limits

- No request size limit or timeout yet; run it on a trusted machine.
- Password-protected drawings are rejected.
- DWG R13 uses the R14 code path and is untested.
- Solids saved in R2004 or earlier lose their rounded (filleted) faces; the rest of the solid converts.
- IFC4X3 has no cone surface, so cone faces are written as surfaces of revolution. Viewer support for that varies.
- Text is positioned and sized, but fonts and exact alignment are not carried over.

## Development

```sh
uv run pytest -q
```

Tests compare decoded values with AutoCAD's own DXF export of the same drawing in every supported
release, and validate the IFC output against the schema.

`tools/make_fixtures.sh` rebuilds the test drawings in `tests/fixtures`. It is the only part that needs
AutoCAD (2027, on macOS) and is not needed to run the service or the tests.

`CLAUDE.md` holds the design notes: how correctness is checked, the IFC mapping, known gaps, the
roadmap, and the places where the published DWG specification turned out to be wrong.

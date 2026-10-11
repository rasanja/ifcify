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
| Tolerances, multilines | text and curves |
| 3D solids, regions, surfaces | `IfcAdvancedBrep` or a surface model, in an `IfcBuildingElementProxy` |
| Meshes, polyface meshes, 3D faces | `IfcPolygonalFaceSet` |
| Multileaders, wipeouts and custom entities | drawn from the picture of themselves they carry, when they have one |
| Layers | `IfcPresentationLayerAssignment` |
| Colours and line weights | `IfcStyledItem` with curve, fill or surface styles |
| Drawing units | IFC length unit |

## Building elements

A DWG holds lines on layers, not walls and rooms. The converter adds building information in three ways:

- **Properties.** Every object carries its layer, entity type and colour. Block inserts carry their block name
  and their attributes (door numbers, room data). Data attached by other programs is carried over too.
- **Classes and types from names.** A layer or block whose name says what it is (`A-DOOR`, `Windows`,
  `PRTA0001`, `Fluorescent Fixture`) makes its solids and block inserts IFC doors, windows, light fixtures,
  furniture and so on. Each named block becomes a type and its inserts occurrences of it.
- **Elements derived from plans.** Room outlines become `IfcSpace` with a name and floor area. Pairs of
  parallel lines on wall layers become `IfcWall` with a thickness and a wall type. Closed shapes on column
  layers become `IfcColumn`. The original linework is kept alongside.

Derived elements are an interpretation and are marked as such. Their height is an assumed 3 m, because a plan
does not give one. Recognition depends on layer and block naming, so a drawing with meaningless names
produces drafting and generic objects only. Import into Revit has not been tested.

Model space goes into one storey: `IfcProject > IfcSite > IfcBuilding > IfcBuildingStorey`. A drawing
with nothing in model space, such as a title block, is exported as its paper-space layouts instead,
one storey per layout.

A drawing placed far from the origin, as survey drawings are, is written relative to a local origin so
viewers can display it. Its real position is kept in the file as `IfcMapConversion`.

Entities on layers that are switched off or frozen are left out, as AutoCAD hides them.

Not converted yet: rays and construction lines, lights, custom entities that carry no picture of
themselves, linetypes, text fonts, and paper-space layouts of drawings that also have a model. Anything left out is counted in the
`X-DWG-Skipped` response header, so nothing is dropped silently.

## Run with Docker

```sh
docker compose up
```

That builds the image on first use (this step needs the network; running does not) and serves on
http://127.0.0.1:8000, reachable from your machine only. API docs are at http://127.0.0.1:8000/docs.

Put drawings in `./drawings` to refer to them with `?path=`; the folder is mounted read-only. Sending
the drawing in the request body needs no setup. Stop with `Ctrl+C`, or run `docker compose up -d` and
`docker compose down`.

## Install without Docker

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
| `DWG2IFC_HOST` | `127.0.0.1` | Address to listen on. The Docker image sets `0.0.0.0` |
| `DWG2IFC_PORT` | `8000` | Port to listen on |
| `DWG2IFC_ROOT` | current directory | Directory that `?path=` references are read from |

## Use

There is one endpoint, `POST /convert`. Give it the drawing in either of two ways.

Send the DWG bytes as the request body:

```sh
curl --data-binary @drawing.dwg -o drawing.ifc http://127.0.0.1:8000/convert
```

Or name a file under `DWG2IFC_ROOT`:

```sh
DWG2IFC_ROOT=/path/to/drawings uv run dwg2ifc
curl -X POST -o drawing.ifc "http://127.0.0.1:8000/convert?path=drawing.dwg"
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

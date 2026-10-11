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
| Colours, line weights, linetypes | `IfcStyledItem` with curve, fill or surface styles |
| Drawing units | IFC length unit |

## Building elements

A DWG holds lines on layers, not walls and rooms. The converter adds building information in three ways:

- **Properties.** Every object carries its layer, entity type and colour. Block inserts carry their block name
  and their attributes (door numbers, room data). Data attached by other programs is carried over too.
- **Classes and types from names.** A layer or block whose name says what it is (`A-DOOR`, `Windows`,
  `PRTA0001`, `Fluorescent Fixture`) makes its solids and block inserts IFC doors, windows, light fixtures,
  furniture and so on. Each named block becomes a type and its inserts occurrences of it.
- **Elements derived from plans.** Room outlines become `IfcSpace` with a name and floor area. Pairs of
  parallel lines on wall layers become `IfcWall` with a thickness and a wall type; a gap with a door or window
  drawn in it becomes an opening in the wall, filled by an `IfcDoor` or `IfcWindow`. Closed shapes on column
  layers become `IfcColumn`. The original linework is kept alongside.

Derived elements are an interpretation and are marked as such. Recognition depends on layer and block
naming, so a drawing with meaningless names produces drafting and generic objects only. Import into Revit
has not been tested.

### Assumed sizes: read this before using the 3D model

**Every vertical size of a derived element is an assumption, not data from the drawing.** A 2D plan shows
where a wall is and how thick it is; nothing in it says how tall. To write a 3D element at all, the
converter puts in fixed values:

| Size | Where it comes from |
|------|---------------------|
| Wall length, position and thickness | measured from the drawing |
| Door and window width | measured from the gap in the wall |
| Room outline and floor area | measured from the drawing |
| Column cross-section | measured from the drawing |
| **Wall, room and column height** | **assumed: 3 m** |
| **Door height** | **assumed: 2.1 m** |
| **Window height and sill** | **assumed: 1.2 m high, 0.9 m above the floor** |
| **Derived door and window thickness** | **assumed: 5 cm** (a placeholder slab in the opening) |
| **Storey elevation** | **assumed: 0** |

What this means in practice:

- Everything measured in plan is real: lengths, widths, thicknesses, and floor areas, which are length
  times width.
- Only what involves the vertical direction is not: heights, volumes, and the area of a wall's face
  (its length times its assumed height). Do not take those out of the IFC.
- Each derived element says so in its properties: `DWG_Derived.Height` or `DWG_Room.Height` reads
  "assumed ...". Elements converted from real 3D solids in the drawing are not affected; their sizes are
  the drawing's.
- The values are fixed in the code for now and cannot be set per request.
- Some drawings do record a height, as the "thickness" of a line or the elevation it is drawn at. The
  converter does not use either yet, so even those drawings get the assumed values.

Model space goes into one storey: `IfcProject > IfcSite > IfcBuilding > IfcBuildingStorey`. A drawing
with nothing in model space, such as a title block, is exported as its paper-space layouts instead,
one storey per layout.

A drawing placed far from the origin, as survey drawings are, is written relative to a local origin so
viewers can display it. Its real position is kept in the file as `IfcMapConversion`.

Entities on layers that are switched off or frozen are left out, as AutoCAD hides them.

Not converted yet: rays and construction lines, lights, custom entities that carry no picture of
themselves, text fonts, and paper-space layouts of drawings that also have a model. Anything left out is counted in the
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
| `DWG2IFC_MAX_MB` | `100` | Largest drawing accepted, in megabytes |

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
| 413 | The drawing is larger than `DWG2IFC_MAX_MB` |
| 422 | Not a DWG file, an unsupported release, or a corrupt file |

From Python, without the web service:

```python
from pathlib import Path
from dwg2ifc import convert

ifc_text, skipped = convert(Path("drawing.dwg").read_bytes())
Path("drawing.ifc").write_text(ifc_text)
```

## Limits

- There is a size limit but no time limit on a conversion; run it on a trusted machine.
- Password-protected drawings are rejected; they are out of scope.
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

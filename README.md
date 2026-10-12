# dwg2ifc

A local web service that converts AutoCAD DWG drawings to IFC4X3.

It reads the DWG binary format directly. It does not need AutoCAD, accoreconsole or any other Autodesk
software, makes no network calls, and runs headless.

## What it converts

DWG releases R14, R2000, R2004, R2007, R2010, R2013 and R2018.

| DWG | IFC |
|-----|-----|
| Lines, arcs, circles, ellipses, polylines, splines, leaders | curves in an `IfcAnnotation` |
| Text, multiline text, attributes | `IfcTextLiteralWithExtent`, with its colour, font and height as an `IfcTextStyle` |
| Hatches, 2D solids | `IfcAnnotationFillArea` |
| Blocks and their inserts, dimensions, tables | `IfcRepresentationMap` placed by `IfcMappedItem` |
| Tolerances, multilines | text and curves |
| Rays, construction lines | `IfcLine`, which is unbounded like they are |
| 3D solids, regions, surfaces | `IfcAdvancedBrep` or a surface model, in an `IfcBuildingElementProxy` |
| Meshes, polyface meshes, 3D faces, subdivision meshes | `IfcPolygonalFaceSet` |
| Helices | `IfcBSplineCurveWithKnots`, with the radius, turns and turn height as properties |
| Multileaders | leader lines and one text (or the block they hold) |
| Images, wipeouts | the outline of the frame; an image's file path as a property (the picture is not in the DWG) |
| Lights | `IfcLightFixture` at the light's position, with its name, kind and intensity |
| Shapes | a point, with the shape file and number as properties (the symbol is not in the DWG) |
| Other custom entities | drawn from the picture of themselves they carry, when they have one |
| Groups | `IfcGroup` |
| Materials on solids | `IfcMaterial` |
| Geographic location in a projected coordinate system | `IfcMapConversion` and `IfcProjectedCRS` |
| Layers | `IfcPresentationLayerAssignment` |
| Colours, line weights, linetypes | `IfcStyledItem` with curve, fill or surface styles |
| Drawing units | IFC length unit |

## Building elements

A DWG holds lines on layers, not walls and rooms. The converter adds building information in three ways:

- **Properties.** Every converted entity carries its layer, entity type, colour and linetype. Block inserts
  carry their block name and their attributes (door numbers, room data). Data attached by other programs
  is carried over too, both kinds a DWG has: extended data (`DWG_XData_<program>`) and records in the
  entity's extension dictionary (`DWG_Record_<name>`). Derived rooms carry the records of their outline.
- **Classes and types from names.** A layer or block whose name says what it is (`A-DOOR`, `Windows`,
  `PRTA0001`, `Fluorescent Fixture`) makes its solids and block inserts IFC doors, windows, light fixtures,
  furniture and so on. A named block recognised this way becomes a type and its inserts occurrences of it;
  blocks whose names are not recognised stay plain drafting, without a type.
- **Elements derived from plans.** Room outlines become `IfcSpace` with a floor area, and a name when there
  is text inside the outline. Pairs of
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

- What is measured in plan comes from the drawing: lengths, widths, thicknesses and floor areas. See the
  next section for what "measured" does and does not guarantee.
- Only what involves the vertical direction is not: heights, volumes, and the area of a wall's face
  (its length times its assumed height). Do not take those out of the IFC.
- Each derived element says so in its properties: `DWG_Derived.Height` or `DWG_Room.Height` reads
  "assumed ...". Elements converted from real 3D solids in the drawing are not affected; their sizes are
  the drawing's.
- The values are fixed in the code for now and cannot be set per request.
- Some drawings do record a height, as the "thickness" of a line or the elevation it is drawn at. The
  thickness is carried as a property but neither is used for the derived elements yet, so even those
  drawings get the assumed values.

### What to trust in a flat 2D drawing

Nothing is assumed about plan geometry, but not everything in the output is exact, and some of it is a
reading of the drawing rather than a fact in it.

**Exact.** Positions and sizes of lines, arcs, circles, ellipses, polylines, points, splines with control
points, solid fills, block placements (position, scale, rotation, mirroring), and text content and
position. These are checked value for value against AutoCAD's own export of a test drawing, in each
supported release. Layers, colours, line weights and linetype patterns are read directly from the file;
colours given as a palette number are looked up in AutoCAD's own colour table.

**Stored by AutoCAD in the drawing, and carried over as stored.** Where a drawing holds a value, it is
used rather than recomputed:

- Field values. A drawing can keep a computed property next to an object, most often the area of a room
  outline. Both forms AutoCAD stores are read: the text it displays (`405 SF`, written as `StoredArea`)
  and the unrounded number behind it, which becomes the room's floor area. Rooms without a stored area
  are measured from their outline, and `AreaSource` says which it was. On the sample plan the stored and
  measured areas agree to 0.01 percent.
- The measured size of multiline text.
- The width of a wide polyline, when it is the same along its length.
- The thickness (extrusion height) of a 2D entity, kept as a property. It is not yet used as a height.
- A dimension's measurement.
- The sizes a solid was made with. AutoCAD keeps the length, width and height of a box or wedge, the
  radius of a sphere, the height and radii of a cylinder or cone, and the two radii of a torus, in the
  solid's history. They are written to `DWG_Stored` when that primitive is the solid's only modelling
  step; after a fillet, a cut or a union they no longer describe the solid and are left out. Drawings
  saved as R2004 or older have no history.
- A helix's radius, number of turns and turn height.
- Text style: the font each text is drawn with, and whether it is slanted.
- Layout names, group names and descriptions, and material names.
- The value and tag of a multi-line block attribute (R2018 files; saved to an older release, AutoCAD
  itself splits it into one plain attribute per line, tagged `NAME_001`, and that is what the file holds).

**Exact only if the drawing declares its unit.** Coordinates are written in the drawing's own unit. Real
sizes, and floor areas in square metres, depend on which unit that is. A drawing that declares none is
taken as inches or millimetres according to its measurement setting, which is a guess; if it is wrong,
every real-world size is wrong by the same factor.

**Approximate.**

- Room footprints: an outline with arcs is written with the arcs broken into short straight pieces. The
  floor area reported for the room is computed from the true arcs and is exact.
- Splines drawn through fit points are recomputed; end tangents and closed splines are not honoured.
- "By block" colour and linetype take the layer's. Palette colour 7, which AutoCAD shows white on a dark
  screen and black on paper, is written black for linework and white on solids.
- Text: single-line text stores no size, so the box around it is estimated from the character count.
  The font is named but the width factor and alignment are not carried over, so text may sit slightly
  differently. Multiline text loses its inline formatting. An SHX font is named by its file (`romans.shx`).
- Subdivision meshes are written as the cage they are smoothed from; a mesh with smoothing levels comes
  out faceted.
- A solid with different materials on different faces lists them all; which face has which is not kept.
- Dimensions saved without their drawn form are redrawn as plain lines and a number, without arrowheads
  and without the drawing's unit format (a length shows as `144`, not `12'-0"`).

**Present in the drawing but not carried over.**

- The width of a polyline that tapers or changes width along its length; it comes out with no width.
- Hatch patterns; a hatch becomes a plain fill of its boundary, and holes rely on the order of its loops.
- Linetype scale, and shapes or text inside a linetype.
- Arrowheads of a multileader saved without its picture, and the frame of a wipeout as a mask: it is an
  outline only and hides nothing.
- A geographic location given as latitude and longitude (it needs a map projection); only locations in a
  projected coordinate system are written. The written form has not been checked against a geolocated
  drawing, as none was available.

**A reading, not a fact.** The derived elements are found by rules, and a rule can pick the wrong lines:

- A wall is any two parallel lines on a wall layer that are 7 to 65 cm apart. The thickness is the true
  distance between those two lines, but nothing guarantees they are the two faces of one wall.
- A wall's length is the stretch along which its two lines run side by side. Corners are not joined, so
  it is not the wall's length as an architect would give it, and lengths do not add up to a perimeter.
- A door or window width is the gap in the wall lines, frame included.
- A room's name is whatever short text with a digit lies inside its outline.
- What something is (door, window, column) comes from layer and block names.

Model space goes into one storey: `IfcProject > IfcSite > IfcBuilding > IfcBuildingStorey`. A drawing
with nothing in model space, such as a title block, is exported as its paper-space layouts instead,
one storey per layout, named after the layout.

A drawing placed far from the origin, as survey drawings are, is written relative to a local origin so
viewers can display it. Its real position is kept in the file as `IfcMapConversion`.

Entities on layers that are switched off or frozen, and entities marked invisible, are left out, as
AutoCAD hides them. Room outlines are the exception: they are read from hidden layers too, because
offices often keep them there.

Not converted: custom entities that carry no picture of themselves, and paper-space layouts of drawings
that also have a model. PDF, DWF and DGN underlays and embedded OLE objects are in the first group too:
AutoCAD's console cannot create them, so there was nothing to check a decoder against. AutoCAD Architecture objects (walls, doors, windows as objects rather than
linework) are in the first group: they are drawn from their picture and their heights and styles are
not read yet, because no drawing containing them was available to check a decoder against. Every entity left out, for any of these
reasons, is counted by reason in the `X-DWG-Skipped` response header. The properties listed above as not
carried over are dropped without a count.

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
| `X-DWG-Skipped` | `LIGHT=8` | Entities left out, counted by type or reason. Empty when none were |

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
- Password-protected drawings are rejected with a 422; they are out of scope.
- DWG R13 uses the R14 code path and is untested.
- Solids in drawings saved as R2000 or R14 lose their rounded (filleted) faces; the rest of the solid converts.
- IFC4X3 has no cone surface, so cone faces are written as surfaces of revolution. Viewer support for that varies.
- Text is positioned, sized and given its font, but exact alignment and width factor are not carried over.
- Arc-length and jogged-radius dimensions are placed by the block AutoCAD drew for them; this path could
  not be checked against AutoCAD, whose console cannot create them without a mouse pick.

## Development

```sh
uv run pytest -q
```

Tests compare decoded values with AutoCAD's own DXF export of the same drawing in every supported
release, and validate the IFC output against the schema.

`tools/make_fixtures.sh` rebuilds the test drawings in `tests/fixtures`. It is the only part that needs
AutoCAD (2027, on macOS) and is not needed to run the service or the tests.

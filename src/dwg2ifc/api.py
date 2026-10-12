import asyncio
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.staticfiles import StaticFiles

from dwg2ifc import convert, dwg

_pool: ProcessPoolExecutor | None = None


def _workers() -> int:
    """How many drawings are converted at once: DWG2IFC_WORKERS, or one per processor."""
    return int(os.environ.get("DWG2IFC_WORKERS") or 0) or os.cpu_count() or 1


def _executor() -> ProcessPoolExecutor:
    # Conversion is pure computation, so threads would take turns; separate processes run side by side.
    # "spawn" starts each worker clean, whatever threads the server itself is running.
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(_workers(), mp_context=multiprocessing.get_context("spawn"))
    return _pool


@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    if _pool is not None:
        _pool.shutdown(cancel_futures=True)


# docs_url/redoc_url off: FastAPI's defaults load their UI from a CDN, which breaks offline
app = FastAPI(
    title="dwg2ifc",
    description="Headless DWG to IFC4X3 conversion. Needs no AutoCAD and no network.",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=_lifespan,
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/docs", include_in_schema=False)
def docs():
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="dwg2ifc",
        swagger_js_url="/static/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css",
        swagger_favicon_url="data:,",
    )


def _root() -> Path:
    return Path(os.environ.get("DWG2IFC_ROOT", ".")).resolve()


def _limit() -> int:
    """Largest drawing accepted, in bytes. A converted drawing takes many times its size in memory."""
    return int(float(os.environ.get("DWG2IFC_MAX_MB", "100")) * 1024 * 1024)


async def _body(request: Request) -> bytes:
    """The request body, refused as soon as it is known to be too large."""
    limit = _limit()
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(413, "drawing is larger than DWG2IFC_MAX_MB")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, "drawing is larger than DWG2IFC_MAX_MB")
    return bytes(data)


def _read_ref(ref: str) -> bytes:
    # path references are confined to DWG2IFC_ROOT so callers cannot read arbitrary files
    path = (_root() / ref).resolve()
    if not path.is_relative_to(_root()):
        raise HTTPException(403, "path is outside DWG2IFC_ROOT")
    if not path.is_file():
        raise HTTPException(404, "no such file")
    if path.stat().st_size > _limit():
        raise HTTPException(413, "drawing is larger than DWG2IFC_MAX_MB")
    return path.read_bytes()


@app.post(
    "/convert",
    summary="Convert a DWG file to IFC4X3",
    response_class=Response,
    responses={
        200: {
            "description": "The IFC4X3 model as STEP text.",
            "content": {"application/x-step": {"schema": {"type": "string"}}},
            "headers": {
                "X-DWG-Version": {"description": "Detected DWG release, e.g. R2013.", "schema": {"type": "string"}},
                "X-DWG-Skipped": {"description": "Entities left out of the IFC, as counts by DWG type, e.g. `HATCH=284`. Empty when nothing was skipped.", "schema": {"type": "string"}},
            },
        },
        403: {"description": "`path` points outside DWG2IFC_ROOT."},
        404: {"description": "`path` does not exist."},
        413: {"description": "The drawing is larger than DWG2IFC_MAX_MB (default 100)."},
        422: {"description": "The input is not a DWG file, is a release not supported yet, or is corrupt."},
        500: {"description": "The conversion process died on this drawing. Other requests are not affected beyond a retry."},
    },
    openapi_extra={
        "requestBody": {
            "description": "Raw DWG bytes. Leave empty when using `path`.",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def convert_dwg(
    request: Request,
    path: str | None = Query(None, description="DWG file to read instead of the body, relative to the server's DWG2IFC_ROOT directory."),
    storey_height: float | None = Query(None, gt=0, description="Height in metres for walls, rooms and columns read out of flat linework. A plan stores none; without this they are written as flat outlines. Not used where the drawing itself holds a height."),
    door_height: float | None = Query(None, gt=0, description="Height in metres of door openings found in such walls. Without it they are not cut."),
    window_height: float | None = Query(None, gt=0, description="Height in metres of window openings found in such walls. Needs `window_sill` too."),
    window_sill: float | None = Query(None, ge=0, description="Height in metres of the window sill above the floor."),
) -> Response:
    """Send the DWG either as the raw request body or by reference with `path`.

    Requests are converted in parallel, one worker process each, up to the server's `DWG2IFC_WORKERS`
    (default: one per processor). Further requests wait their turn."""
    data = _read_ref(path) if path else await _body(request)
    global _pool
    try:
        # in a worker process: requests are converted in parallel, up to DWG2IFC_WORKERS at a time
        name = Path(path).stem if path else "drawing"
        heights = {"storey": storey_height, "door": door_height, "window": window_height, "sill": window_sill}
        ifc, skipped = await asyncio.get_running_loop().run_in_executor(_executor(), convert, data, name, heights)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except BrokenProcessPool:
        _pool = None  # a worker crashed outright; the next request starts a fresh set
        raise HTTPException(500, "the conversion process died on this drawing") from None
    headers = {"X-DWG-Version": dwg.version(data), "X-DWG-Skipped": ", ".join(f"{k}={v}" for k, v in sorted(skipped.items()))}
    return Response(ifc, media_type="application/x-step", headers=headers)


def main() -> None:
    # localhost unless told otherwise; the container image sets DWG2IFC_HOST=0.0.0.0
    uvicorn.run(app, host=os.environ.get("DWG2IFC_HOST", "127.0.0.1"), port=int(os.environ.get("DWG2IFC_PORT", "8000")))

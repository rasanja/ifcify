import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.staticfiles import StaticFiles

from dwg2ifc import convert, dwg

# docs_url/redoc_url off: FastAPI's defaults load their UI from a CDN, which breaks offline
app = FastAPI(
    title="dwg2ifc",
    description="Headless DWG to IFC4X3 conversion. Needs no AutoCAD and no network.",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
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
) -> Response:
    """Send the DWG either as the raw request body or by reference with `path`."""
    data = _read_ref(path) if path else await _body(request)
    try:
        # off the event loop, so one large drawing does not stall other requests
        ifc, skipped = await run_in_threadpool(convert, data, Path(path).stem if path else "drawing")
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    headers = {"X-DWG-Version": dwg.version(data), "X-DWG-Skipped": ", ".join(f"{k}={v}" for k, v in sorted(skipped.items()))}
    return Response(ifc, media_type="application/x-step", headers=headers)


def main() -> None:
    # localhost unless told otherwise; the container image sets DWG2IFC_HOST=0.0.0.0
    uvicorn.run(app, host=os.environ.get("DWG2IFC_HOST", "127.0.0.1"), port=int(os.environ.get("DWG2IFC_PORT", "8000")))

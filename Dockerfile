# dwg2ifc: DWG to IFC4X3 conversion service. The build needs the network; the running container does not.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /bin/uv
WORKDIR /app

# dependencies first, so editing the source does not reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# inside a container the service must listen on all interfaces; compose.yaml publishes it to localhost only
ENV PATH=/app/.venv/bin:$PATH \
    DWG2IFC_HOST=0.0.0.0 \
    DWG2IFC_ROOT=/data
RUN mkdir /data
USER nobody
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/openapi.json')"
CMD ["dwg2ifc"]

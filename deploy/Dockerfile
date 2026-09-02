# grag server image: REST + UI + MCP on one live .lbdb, embeddings on-box.
#
#   docker build -f deploy/Dockerfile -t grag .
#   docker run -p 47832:47832 -v grag-data:/data -e GRAG_API_TOKEN=... grag
#
# Build the UI first (cd ui && npm ci && npm run build) — the wheel's
# force-include needs src/grag/api/static to exist (see pyproject.toml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Single-writer contract: exactly one server process owns /data/grag.lbdb.
    GRAG_DB_PATH=/data/grag.lbdb \
    # Semantic search on the server (ONNX, no PyTorch); model cached in /data.
    GRAG_EMBED_PROVIDER=fastembed \
    FASTEMBED_CACHE_PATH=/data/fastembed \
    # No TTY under a supervisor: recover a corrupt WAL instead of crash-looping.
    GRAG_WAL_AUTO_RECOVER=1 \
    HOME=/data

WORKDIR /app
COPY . /app
RUN pip install ".[embed-local,code]"

RUN useradd --uid 10001 --create-home --home-dir /data grag \
    && chown -R grag:grag /data
USER grag
VOLUME ["/data"]
EXPOSE 47832

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:47832/api/health', timeout=3).status==200 else 1)"

# GRAG_API_TOKEN is REQUIRED: grag refuses a non-loopback bind without it.
CMD ["grag", "serve", "--host", "0.0.0.0", "--port", "47832", "--with-mcp"]

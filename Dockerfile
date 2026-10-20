# syntax=docker/dockerfile:1

# Build the browser bundle in an isolated Node image.  The source tree stays
# out of the runtime image; only the compiled Studio assets are copied below.
FROM node:22-bookworm-slim AS frontend-build

WORKDIR /frontend
COPY studio-frontend/package.json studio-frontend/package-lock.json ./
# Vulnerability scanning is a separate release gate. Disabling npm's network
# audit here keeps production image builds deterministic when the audit endpoint
# is slow or unavailable.
RUN npm ci --ignore-scripts --no-audit
COPY studio-frontend/ ./
RUN npm run typecheck && npm run build

# Install the Python application and keep the migration command in the same
# image as the runtime.  Migrations are still an explicit deploy step in
# Compose; the web/worker processes never run them implicitly.
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
# Cache the local tokenizer at build time; model requests never download it.
RUN python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")' \
    && chmod -R a+rX /opt/tiktoken
# Fetch the pinned local embedding model (revision and sha256 checked); the
# running app never downloads it and no text leaves the machine.
COPY scripts/fetch_embedding_model.py /tmp/fetch_embedding_model.py
RUN python /tmp/fetch_embedding_model.py /opt/models/paraphrase-multilingual-MiniLM-L12-v2 \
    && chmod -R a+rX /opt/models && rm /tmp/fetch_embedding_model.py

COPY app ./app
COPY scripts ./scripts
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY --from=frontend-build /app/web/static/studio-dist ./app/web/static/studio-dist

# Local cookie signing state lives here; mount a volume on this path.
# PostgreSQL data is stored in its own persistent volume, outside this image.
RUN useradd -r -u 10001 appuser && mkdir -p /app/data && chown -R appuser:appuser /app/data
VOLUME ["/app/data"]

EXPOSE 8080

USER appuser

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5)" || exit 1

CMD ["python", "-m", "app.main"]

# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12
ARG GNPARSER_VERSION=1.15.0
# Test tooling ships in the image by default: the container is the only Python
# environment, so tests have nowhere else to run. Set to "0" for a lean deploy.
ARG INSTALL_DEV=1

# ---------------------------------------------------------------- gnparser
# Fetched in its own stage so the runtime image never carries curl/tar layers.
FROM debian:bookworm-slim AS gnparser
ARG GNPARSER_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/gnparser.tar.gz \
      "https://github.com/gnames/gnparser/releases/download/v${GNPARSER_VERSION}/gnparser-v${GNPARSER_VERSION}-linux-x86.tar.gz" \
    && tar -xzf /tmp/gnparser.tar.gz -C /usr/local/bin gnparser \
    && chmod +x /usr/local/bin/gnparser \
    && /usr/local/bin/gnparser --version

# ---------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim AS builder
ARG INSTALL_DEV
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml README.md ./
COPY checklistdiff ./checklistdiff
# Build a wheel, then resolve it into a self-contained venv we can copy over.
RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip setuptools wheel \
    && if [ "$INSTALL_DEV" = "1" ]; then /venv/bin/pip install ".[dev]"; \
       else /venv/bin/pip install .; fi

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    # /app precedes the venv's site-packages so the bind-mounted source in
    # compose.yml is what actually runs. Without this the console script would
    # silently import the copy baked into the image at build time, and every
    # edit would appear to have no effect until a rebuild.
    PYTHONPATH=/app

COPY --from=gnparser /usr/local/bin/gnparser /usr/local/bin/gnparser
COPY --from=builder /venv /venv

# Non-root, and it must own /data since SQLite needs to write the db plus its
# -wal and -shm sidecars into that directory.
RUN useradd --create-home --uid 1000 ckdiff \
    && mkdir -p /data \
    && chown -R ckdiff:ckdiff /data

WORKDIR /app
COPY --chown=ckdiff:ckdiff . /app
USER ckdiff

ENV CKDIFF_DB_PATH=/data/checklistdiff.sqlite \
    CKDIFF_GNPARSER_BIN=/usr/local/bin/gnparser

EXPOSE 8000
CMD ["uvicorn", "checklistdiff.api:app", "--host", "0.0.0.0", "--port", "8000"]

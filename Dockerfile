# Phase 3 image: RDKit + OPSIN. `default-jre-headless` is required at
# runtime by `app/opsin_utils.py:_call_local`, which invokes the OPSIN
# CLI JAR shipped inside the py2opsin wheel via `subprocess.run`.
# `--no-install-recommends` keeps the JRE pull lean by avoiding the
# desktop bundle pulled in by `default-jre`. Measure the resulting
# image size before claiming it (see Artifact Registry cleanup note in
# DEPLOY.md §5.5).
#
# The Cloud Build pipeline (cloudbuild.yaml) uses the legacy (non-
# BuildKit) `gcr.io/cloud-builders/docker` daemon, so no BuildKit
# parser directive sits at the top of this file. If you ever need
# BuildKit features (e.g. `RUN --mount=type=cache`), restore the
# `# syntax=docker/dockerfile:1` line AND switch cloudbuild.yaml's
# docker step to set `DOCKER_BUILDKIT=1` and pass
# `--build-arg BUILDKIT_INLINE_CACHE=1`.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        default-jre-headless \
        libxrender1 \
        libxext6 \
        libexpat1 \
        libfontconfig1 \
        libfreetype6 \
        libcairo2 \
    && rm -rf /var/lib/apt/lists/*
# ``rdkit.Chem.Draw.rdMolDraw2D`` loads the full cairo/freetype/font
# stack at import time on Linux, even when only the pure-SVG renderer
# (``MolDraw2DSVG``) is used. python:3.11-slim ships none of these.
# The cascade we hit on first deploy was:
#   ImportError: libXrender.so.1: cannot open shared object file
#   ImportError: libexpat.so.1: cannot open shared object file
# All five extra packages are needed together; adding them piecewise
# costs one ~6-min Cloud Build per iteration. libcairo2 transitively
# pulls fontconfig + freetype + expat, but listing them explicitly
# keeps the dependency intent obvious in this file.

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/

# Cloud Run injects PORT; fall back to 8080 for local `docker run`.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

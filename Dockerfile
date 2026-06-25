# syntax=docker/dockerfile:1
#
# Phase 3 image: RDKit + OPSIN. `default-jre-headless` is required at
# runtime by `app/opsin_utils.py:_call_local`, which invokes the OPSIN
# CLI JAR shipped inside the py2opsin wheel via `subprocess.run`.
# `--no-install-recommends` keeps the JRE pull lean by avoiding the
# desktop bundle pulled in by `default-jre`. Measure the resulting
# image size before claiming it (see Artifact Registry cleanup note in
# DEPLOY.md §5.5).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/

# Cloud Run injects PORT; fall back to 8080 for local `docker run`.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

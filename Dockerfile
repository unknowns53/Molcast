# syntax=docker/dockerfile:1
#
# Phase 1 image: RDKit only, no OPSIN. JRE and py2opsin are removed to
# keep the image under the Artifact Registry 0.5 GB free tier. For Phase
# 3 (which calls OPSIN through py2opsin), restore:
#   - `default-jre-headless` in the apt-get install line below
#   - `py2opsin>=1.0.1` in requirements.txt
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/

# Cloud Run injects PORT; fall back to 8080 for local `docker run`.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

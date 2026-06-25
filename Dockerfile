# syntax=docker/dockerfile:1
#
# §11.1 recommended path: py2opsin variant. JRE is installed up-front so
# that Phase 3 (which calls OPSIN through py2opsin) does not need a
# second image rebuild path.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        default-jre-headless \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/

# Cloud Run injects PORT; fall back to 8080 for local `docker run`.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]

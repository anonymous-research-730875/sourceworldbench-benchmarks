# syntax=docker/dockerfile:1.7

FROM python:3.9-slim

ARG REPO_URL=https://github.com/Fatal1ty/mashumaro.git
ARG BASE_COMMIT=73a937aebf8383da558cac3e71978cc1f8e6a0c7

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY environments/Fatal1ty__mashumaro-127__base/patch.diff /tmp/patch.diff
COPY environments/Fatal1ty__mashumaro-127__base/test_patch.diff /tmp/test_patch.diff

WORKDIR /app
RUN git clone "$REPO_URL" . \
    && git checkout "$BASE_COMMIT" \
    && git apply /tmp/patch.diff \
    && git apply /tmp/test_patch.diff \
    && rm -f /tmp/patch.diff /tmp/test_patch.diff \
    && python -m venv .venv \
    && .venv/bin/pip install --no-cache-dir --upgrade pip \
    && .venv/bin/pip install --no-cache-dir -e . \
    && .venv/bin/pip install --no-cache-dir -r requirements-dev.txt \
    && mkdir -p /results

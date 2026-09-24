# syntax=docker/dockerfile:1.7

FROM python:3.13-slim-bookworm

ARG TENACITY_REPO_URL=https://github.com/jd/tenacity.git
ARG TENACITY_REF=8a171aa5422efac9bd144d6dcd33d703359647d6

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$TENACITY_REPO_URL" /app \
    && git checkout "$TENACITY_REF" \
    && python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /app/.venv/bin/pip install --no-cache-dir \
         "pytest" "tornado>=4.5" "typeguard" "trio" \
    && /app/.venv/bin/pip install --no-cache-dir -e . \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

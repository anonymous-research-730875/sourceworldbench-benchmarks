# syntax=docker/dockerfile:1.7

FROM python:3.7-slim-bullseye

ARG TENACITY_REPO_URL=https://github.com/jd/tenacity.git
ARG TENACITY_REF=a7b83aeb487bf8de6b8f601d5b2e35f7141e8a4e

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
         "pbr" "six>=1.9.0" "pytest" "tornado>=4.5,<6" \
    && /app/.venv/bin/pip install --no-cache-dir --no-deps -e . \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

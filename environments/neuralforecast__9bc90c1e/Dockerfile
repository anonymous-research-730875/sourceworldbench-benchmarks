# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG NEURALFORECAST_REPO_URL=https://github.com/Nixtla/neuralforecast.git
ARG NEURALFORECAST_REF=9bc90c1eac5b717a46d6152106e38932fb9c3dc6

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$NEURALFORECAST_REPO_URL" /app \
    && git checkout "$NEURALFORECAST_REF" \
    && uv sync --group dev --group aws --extra spark \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ARG FLASK_WTF_REPO_URL=https://github.com/pallets-eco/flask-wtf.git
ARG FLASK_WTF_REF=de1faa6ba9ab17a0c353d3a211655d42b8d11bb3

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$FLASK_WTF_REPO_URL" /app \
    && git checkout "$FLASK_WTF_REF" \
    && uv sync --group test --group test-babel \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

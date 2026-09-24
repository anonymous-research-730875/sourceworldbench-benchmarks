# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:python3.8-bookworm-slim

ARG REPO_URL=https://github.com/RocketPy-Team/RocketPy.git
ARG REPO_REF=3ce20f06f2709d1058fb4317304b8c4246a4b248

ENV MPLBACKEND=Agg

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$REPO_URL" /app \
    && git checkout "$REPO_REF"

ENV VIRTUAL_ENV=/app/.venv
RUN uv venv --python 3.8 /app/.venv \
    && uv pip install -r requirements.txt -r requirements_test.txt \
    && uv pip install -e . \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:python3.10-bookworm-slim

ARG REPO_URL=https://github.com/RocketPy-Team/RocketPy.git
ARG REPO_REF=7452b6145a8ab1df02310cdd370eda1138294f6b

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
RUN uv venv /app/.venv \
    && uv pip install -e ".[all]" -r requirements-tests.txt \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

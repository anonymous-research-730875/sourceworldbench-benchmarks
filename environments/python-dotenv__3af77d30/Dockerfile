# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ARG DOTENV_REPO_URL=https://github.com/theskumar/python-dotenv.git
ARG DOTENV_REF=3af77d3029eb717aeec0a3c25f751b6a614a6d3c

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$DOTENV_REPO_URL" /app \
    && git checkout "$DOTENV_REF" \
    && uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python -e ".[cli]" \
    && uv pip install --python /app/.venv/bin/python pytest pytest-cov "sh>=2.0.2,<3" click ipython \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

ENV PATH="/app/.venv/bin:${PATH}"

USER runner

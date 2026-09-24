# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ARG DOTENV_REPO_URL=https://github.com/theskumar/python-dotenv.git
ARG DOTENV_REF=fa4e6a90b45428212452afc6ee0d5c8103b9301d

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
    && uv pip install --python /app/.venv/bin/python pytest pytest-cov ipython \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

ENV PATH="/app/.venv/bin:${PATH}"

USER runner

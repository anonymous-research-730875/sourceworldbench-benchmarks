# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.8-bookworm-slim

ARG DOTENV_REPO_URL=https://github.com/theskumar/python-dotenv.git
ARG DOTENV_REF=d0a219d180ec62bfb1bead5849aecca91eead3d9

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$DOTENV_REPO_URL" /app \
    && git checkout "$DOTENV_REF" \
    && uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python -e . \
    && uv pip install --python /app/.venv/bin/python mock pytest "click==7.0" "sh<2" "ipython<8" "jedi<0.18" \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

ENV PATH="/app/.venv/bin:${PATH}"

USER runner

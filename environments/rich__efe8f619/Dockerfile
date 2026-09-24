# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ARG RICH_REPO_URL=https://github.com/Textualize/rich.git
ARG RICH_REF=efe8f619138327dd3e8fe490289d1b0e6da4a0e8

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_NO_INTERACTION=1 \
    PATH="/root/.local/bin:$PATH"

# poetry.lock at this commit is lock-version 2.1, which requires Poetry >= 2.0.
RUN uv tool install "poetry==2.1.3"

WORKDIR /app
RUN git clone "$RICH_REPO_URL" /app \
    && git checkout "$RICH_REF" \
    && poetry install

# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ARG RICH_REPO_URL=https://github.com/Textualize/rich.git
ARG RICH_REF=a34914be5d1cc9dc298ca92e5c9af757157a0bb7

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_NO_INTERACTION=1 \
    PATH="/root/.local/bin:$PATH"

RUN uv tool install "poetry==1.8.5"

WORKDIR /app
RUN git clone "$RICH_REPO_URL" /app \
    && git checkout "$RICH_REF" \
    && poetry lock --no-update \
    && poetry install

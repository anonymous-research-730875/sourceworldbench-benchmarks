# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.10-bookworm-slim

ARG HVAC_REPO_URL=https://github.com/hvac/hvac.git
ARG HVAC_REF=f0b44cc757179d088780bbd4c5fd87cc6e264766


RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_NO_INTERACTION=1 \
    PATH="/root/.local/bin:$PATH"

RUN uv tool install "poetry==1.4.2"

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$HVAC_REPO_URL" /app \
    && git checkout "$HVAC_REF" \
    && poetry install \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

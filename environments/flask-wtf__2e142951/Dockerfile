# syntax=docker/dockerfile:1.7

# Flask-WTF v1.2.2 (commit 2e142951, 2024-10-20). Modern pytest project,
# requires-python >=3.9; CI matrix runs py3.9-3.13. tox testenv installs the
# pinned test deps plus Flask-Babel + flask-reuploaded (needed for the i18n and
# file test modules), which we bake into the image so the container runs sealed.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG FLASK_WTF_REPO_URL=https://github.com/pallets-eco/flask-wtf.git
ARG FLASK_WTF_REF=2e142951788d8209815fd4797c93f6f3c275ed71

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$FLASK_WTF_REPO_URL" /app \
    && git checkout "$FLASK_WTF_REF" \
    && uv venv \
    && uv pip install -r requirements/tests.txt Flask-Babel flask-reuploaded \
    && uv pip install -e . \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

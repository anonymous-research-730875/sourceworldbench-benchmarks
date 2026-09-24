# syntax=docker/dockerfile:1.7

FROM python:3.13-slim-bookworm

ARG TENACITY_REPO_URL=https://github.com/jd/tenacity.git
ARG TENACITY_REF=e8d5f3b91b4da34e799c9aeff29e3ee3631047c4

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$TENACITY_REPO_URL" /app \
    && git checkout "$TENACITY_REF" \
    && python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip setuptools setuptools_scm wheel \
    && /app/.venv/bin/pip install --no-cache-dir -e ".[test]" \
    # trio is an optional test dep (CI installs it only in the py314-trio tox env);
    # pin it so tests/test_asyncio.py::TestTrio runs and stays reproducible across trio releases.
    && /app/.venv/bin/pip install --no-cache-dir "trio==0.33.0" \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

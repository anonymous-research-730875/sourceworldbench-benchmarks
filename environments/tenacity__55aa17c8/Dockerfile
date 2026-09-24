# syntax=docker/dockerfile:1.7

FROM python:3.7-slim-bullseye

ARG TENACITY_REPO_URL=https://github.com/jd/tenacity.git
ARG TENACITY_REF=55aa17c8cc018aecda1828e8fbc64840165549fe

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

# This commit's setup.py uses setuptools_scm (use_scm_version=True), not pbr, and
# ships a top-level `releasenotes/` dir. setuptools >= 61 flat-layout autodiscovery
# refuses to build with multiple top-level packages, so pin setuptools < 61 and
# provide a pretend version so the editable install does not need git-tag resolution.
WORKDIR /app
RUN git clone "$TENACITY_REPO_URL" /app \
    && git checkout "$TENACITY_REF" \
    && python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip "setuptools<61" wheel \
    && /app/.venv/bin/pip install --no-cache-dir \
         "setuptools_scm" "six>=1.9.0" "pytest" "tornado>=4.5,<6" \
    && SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 /app/.venv/bin/pip install --no-cache-dir --no-deps -e . \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

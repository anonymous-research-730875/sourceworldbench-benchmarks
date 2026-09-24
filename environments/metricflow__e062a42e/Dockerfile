# syntax=docker/dockerfile:1.7

# duckdb 0.3.4 (Jan 2022) ships no arm64 wheel; force linux/amd64 (matching CI's
# x86_64 runners) so pip installs prebuilt manylinux wheels instead of compiling.
FROM --platform=linux/amd64 python:3.9-slim-bookworm

ARG REPO_URL=https://github.com/dbt-labs/metricflow.git
ARG REPO_REF=e062a42e581ac73004bb0312ac33c7ec31dd0d6d

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates graphviz build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && dot -c

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$REPO_URL" /app \
    && git checkout "$REPO_REF"

# poetry.lock is lock-version 2.0; pin a contemporaneous poetry release.
RUN pip install --no-cache-dir "poetry==1.5.1"

# Keep the virtualenv inside the project so the runner can invoke /app/.venv/bin/pytest.
RUN poetry config virtualenvs.in-project true \
    && poetry install -E dbt-cloud \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

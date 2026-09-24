# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm

ARG REPO_URL=https://github.com/dbt-labs/metricflow.git
ARG REPO_REF=2b49b34f34a247173569e2d6e59620ec74dd32a1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates graphviz \
    && rm -rf /var/lib/apt/lists/* \
    && dot -c

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$REPO_URL" /app \
    && git checkout "$REPO_REF"

RUN pip install --no-cache-dir "virtualenv<20.40" "hatch"

# Hatch's default data dir is under /root, unreadable for non-root runtime users.
ENV HATCH_DATA_DIR=/opt/hatch

# This commit is a hatch monorepo with two projects, each with its own `dev-env`:
#   - root                 -> runs tests_metricflow/
#   - metricflow-semantics -> runs tests_metricflow_semantics/
# Create both and symlink each venv next to its project so tests can invoke the
# venv's pytest binary directly (no runtime dependency resolution).
RUN hatch env create dev-env \
    && ln -s "$(hatch env find dev-env)" /app/.venv \
    && cd /app/metricflow-semantics \
    && hatch env create dev-env \
    && ln -s "$(hatch env find dev-env)" /app/metricflow-semantics/.venv \
    && mkdir -p /results \
    && chown -R runner:runner /app /opt/hatch /results

# The project pins tabulate loosely (`tabulate>=0.8.9`). tabulate 0.10.0 (released
# 2026-03-04, AFTER this 2024-12-09 commit) changed column alignment and can break the
# repo's table-formatting snapshots. Pin to the commit-era 0.9.x in both venvs so the
# environment reflects the dependencies resolvable when the commit was authored.
RUN /app/.venv/bin/pip install --no-cache-dir "tabulate<0.10" \
    && /app/metricflow-semantics/.venv/bin/pip install --no-cache-dir "tabulate<0.10"

USER runner

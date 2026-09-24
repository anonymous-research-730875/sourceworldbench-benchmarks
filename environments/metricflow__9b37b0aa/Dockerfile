# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm

ARG REPO_URL=https://github.com/dbt-labs/metricflow.git
ARG REPO_REF=9b37b0aae4d1052b826b17f4f95734ef5fa14a78

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

# Two dependencies are pinned loosely by the project and resolve to releases that
# postdate this 2025-04-03 commit, breaking snapshot tests:
#   - tabulate ("tabulate>=0.8.9"): 0.10.0 (2026-03-04) changed column alignment.
#   - click ("click>=8.1.6"): 8.2.0 (2025-05-10) reworked usage/parameter-error output,
#     breaking the CLI error snapshot (tests_metricflow/cli/test_output_format.py).
# Pin both to the newest release available when the commit was authored (tabulate 0.9.x,
# click 8.1.8) in both venvs so the environment reflects that dependency set.
RUN /app/.venv/bin/pip install --no-cache-dir "tabulate<0.10" "click<8.2" \
    && /app/metricflow-semantics/.venv/bin/pip install --no-cache-dir "tabulate<0.10" "click<8.2"

USER runner

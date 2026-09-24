# syntax=docker/dockerfile:1.7

FROM python:3.10-slim-bookworm

ARG REPO_URL=https://github.com/dbt-labs/metricflow.git
ARG REPO_REF=a6ac795fd06dbc630c2cae3a3093417ba0fde530

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
RUN hatch env create dev-env \
    && ln -s "$(hatch env find dev-env)" /app/.venv \
    && mkdir -p /results \
    && chown -R runner:runner /app /opt/hatch /results

USER runner

# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG NEURALFORECAST_REPO_URL=https://github.com/Nixtla/neuralforecast.git
ARG NEURALFORECAST_REF=a788a8b4b2434e55524bee2a3c0e72b3b0c1dba2

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
RUN git clone "$NEURALFORECAST_REPO_URL" /app \
    && git checkout "$NEURALFORECAST_REF" \
    # Do NOT use `uv sync` here: the committed uv.lock resolves universally across
    # requires-python >=3.9, and the `docs` extra's mkdocstrings-parser needs >=3.10,
    # so the 3.9 split is unsatisfiable. CI sidesteps this with `uv pip install` (which
    # resolves only for the active interpreter), so mirror CI's install into a venv.
    # Pin resolution to the base-commit date (2026-01-13): the loose deps otherwise
    # resolve pandas 3.x, which removed the 'M' month-end offset alias the tests use
    # ("'M' is no longer supported ... use 'ME'"). The commit's uv.lock pins pandas
    # 2.3.2, which `--exclude-newer` reproduces.
    && uv venv \
    && uv pip install --exclude-newer 2026-01-14T00:00:00Z ".[dev,aws,spark]" --torch-backend cpu \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

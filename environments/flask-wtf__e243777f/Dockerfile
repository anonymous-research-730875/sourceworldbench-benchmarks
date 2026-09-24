# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:python3.10-bookworm-slim

ARG FLASK_WTF_REPO_URL=https://github.com/pallets-eco/flask-wtf.git
ARG FLASK_WTF_REF=e243777fed50915086326297fbfe789a935afffa

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

WORKDIR /app
# setup.cfg sets `filterwarnings = error`, so deprecation warnings from newer
# dependencies fail otherwise-passing tests. Pin Flask/Werkzeug/WTForms/Babel to
# the versions current at the base commit (Dec 2022) to match the CI-era suite.
RUN git clone "$FLASK_WTF_REPO_URL" /app \
    && git checkout "$FLASK_WTF_REF" \
    && uv venv \
    && uv pip install -r requirements/tests.txt \
        "Flask==2.2.2" "Werkzeug==2.2.2" "WTForms==3.0.1" "itsdangerous==2.1.2" \
        "Jinja2==3.1.2" "MarkupSafe==2.1.1" "click==8.1.3" \
        "Flask-Babel==2.0.0" flask-reuploaded \
    && uv pip install -e . \
    && mkdir -p /results \
    && chown -R runner:runner /app /results

USER runner

# syntax=docker/dockerfile:1.7
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates patch \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY environments/CS-SI__eodag-490__base/test_patch.diff /tmp/test_patch.diff

RUN git clone https://github.com/CS-SI/eodag . \
    && git checkout 0a782d7f476a993170f7d60c006be09b4cfa2b47 \
    && git apply /tmp/test_patch.diff \
    && rm -f /tmp/test_patch.diff

RUN pip install --no-cache-dir \
    "python-dateutil<3.0.0,>=2.1" \
    "shapely<2.0.0" \
    "jsonpath-ng<1.6.0" \
    "flask<2.0.0" \
    "click<8.0" \
    "werkzeug<2.0.0" \
    "jinja2<3.0.0" \
    "markupsafe<2.1.0" \
    -e ".[dev]" \
    && mkdir -p /results

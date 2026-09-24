# syntax=docker/dockerfile:1.7

FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates patch \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY environments/NCAR__geocat-comp-571__base/test_patch.diff /tmp/test_patch.diff

RUN git clone https://github.com/NCAR/geocat-comp . \
    && git checkout 9ccf3e0cb4575c11f41288cdc95a0a8f6f3875ac \
    && git apply /tmp/test_patch.diff \
    && rm -f /tmp/test_patch.diff

RUN pip install --no-cache-dir -e "." \
    pytest netcdf4 geocat-datafiles parameterized pytest-cov "dask[distributed]" \
    && mkdir -p /results

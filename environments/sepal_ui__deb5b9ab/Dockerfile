# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 swerebench/sweb.eval.x86_64.12rambau_1776_sepal_ui-411:latest

RUN ln -s /testbed /app

WORKDIR /app
RUN (git cat-file -e deb5b9abc535aa8b847f62057e3121023ebe1458 2>/dev/null || git fetch --no-tags https://github.com/12rambau/sepal_ui deb5b9abc535aa8b847f62057e3121023ebe1458) \
    && git checkout -f deb5b9abc535aa8b847f62057e3121023ebe1458 \
    && . /opt/conda/etc/profile.d/conda.sh && conda run -n testbed pip install -e . -q \
    && mkdir -p /results

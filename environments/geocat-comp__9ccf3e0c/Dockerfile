# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 swerebench/sweb.eval.x86_64.ncar_1776_geocat-comp-571:latest

RUN ln -s /testbed /app

WORKDIR /app
RUN . /opt/conda/etc/profile.d/conda.sh && conda run -n testbed pip install -e . -q \
    && mkdir -p /results

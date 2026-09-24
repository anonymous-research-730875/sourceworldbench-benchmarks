# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 swerebench/sweb.eval.x86_64.ncar_1776_geocat-comp-571:latest

RUN ln -s /testbed /app

WORKDIR /app
RUN (git cat-file -e d5c31c831135bde0d24108d4d113331be2469c99 2>/dev/null || git fetch --no-tags https://github.com/NCAR/geocat-comp d5c31c831135bde0d24108d4d113331be2469c99) \
    && git checkout -f d5c31c831135bde0d24108d4d113331be2469c99 \
    && . /opt/conda/etc/profile.d/conda.sh && conda run -n testbed pip install -e . -q \
    && mkdir -p /results

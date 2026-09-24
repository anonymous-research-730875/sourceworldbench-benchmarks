# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 swerebench/sweb.eval.x86_64.beeware_1776_briefcase-1972:latest

COPY environments/beeware__briefcase-1972__basecommit/test_patch.diff /tmp/test_patch.diff

RUN ln -s /testbed /app

WORKDIR /app
RUN git apply /tmp/test_patch.diff \
    && rm -f /tmp/test_patch.diff \
    && . /opt/conda/etc/profile.d/conda.sh && conda run -n testbed pip install -e . -q \
    && mkdir -p /results

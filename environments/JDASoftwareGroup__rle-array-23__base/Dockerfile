# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 swerebench/sweb.eval.x86_64.jdasoftwaregroup_1776_rle-array-23:latest

COPY environments/JDASoftwareGroup__rle-array-23__base/patch.diff /tmp/patch.diff
COPY environments/JDASoftwareGroup__rle-array-23__base/test_patch.diff /tmp/test_patch.diff

RUN ln -s /testbed /app

WORKDIR /app
RUN git apply /tmp/patch.diff \
    && git apply /tmp/test_patch.diff \
    && rm -f /tmp/patch.diff /tmp/test_patch.diff \
    && . /opt/conda/etc/profile.d/conda.sh && conda run -n testbed pip install -e . -q \
    && mkdir -p /results

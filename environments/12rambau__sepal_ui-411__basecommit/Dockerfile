# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 swerebench/sweb.eval.x86_64.12rambau_1776_sepal_ui-411:latest

COPY environments/12rambau__sepal_ui-411__basecommit/test_patch.diff /tmp/test_patch.diff

RUN mv /testbed /app

WORKDIR /app
RUN git apply /tmp/test_patch.diff \
    && rm -f /tmp/test_patch.diff \
    && mkdir -p /results

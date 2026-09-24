# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 ghcr.io/swefficiency/swefficiency-images:numpy__numpy-18203

RUN groupadd --gid 65532 runner \
    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner

RUN ln -s /testbed /app

WORKDIR /app
RUN mkdir -p /results \
    && chown -R runner:runner /testbed /results

USER runner

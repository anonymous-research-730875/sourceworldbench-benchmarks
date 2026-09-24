# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 swerebench/sweb.eval.x86_64.mit-ll-responsible-ai_1776_hydra-zen-73:latest

RUN ln -s /testbed /app

WORKDIR /app
RUN (git cat-file -e 5c240019f1f183a5e0043f035ad305feee606a4d 2>/dev/null || git fetch --no-tags https://github.com/mit-ll-responsible-ai/hydra-zen 5c240019f1f183a5e0043f035ad305feee606a4d) \
    && git checkout -f 5c240019f1f183a5e0043f035ad305feee606a4d \
    && . /opt/conda/etc/profile.d/conda.sh && conda run -n testbed pip install -e . -q \
    && mkdir -p /results

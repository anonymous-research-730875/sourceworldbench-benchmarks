# syntax=docker/dockerfile:1.7

FROM --platform=linux/amd64 swerebench/sweb.eval.x86_64.azure_1776_azure-cli-4265:latest

COPY environments/Azure__azure-cli-4265__base/patch.diff /tmp/patch.diff
COPY environments/Azure__azure-cli-4265__base/test_patch.diff /tmp/test_patch.diff

RUN mv /testbed /app && for PTH in $(find /opt/conda -name easy-install.pth 2>/dev/null); do for PREFIX in $(grep '^/' "$PTH" | cut -d/ -f2 | sort -u); do [ -e "/$PREFIX" ] || ln -sf /app "/$PREFIX"; done; done

WORKDIR /app
RUN git apply /tmp/patch.diff \
    && git apply /tmp/test_patch.diff \
    && rm -f /tmp/patch.diff /tmp/test_patch.diff \
    && mkdir -p /results

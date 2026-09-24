#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results
cd /app

# Run each test file in its own pytest process so memory is fully released between
# files. The suite trains many torch/pytorch-lightning models; run as a single
# `pytest tests` process it transiently peaks ~7GiB (esp. test_model_checks[TimeLLM],
# which loads a large LM backbone) and OOM-kills on a 7.65GiB Docker VM, whereas
# GitHub CI runs it on ~16GiB. Per-file isolation caps peak memory (~5GiB). Each file
# writes its own JUnit XML; parse_junit_xml globs every *.xml under /results.
#
# TimeLLM is deselected: it loads a large pretrained language-model backbone that both
# spikes memory over the ceiling and is unsuitable for a sealed run.
for f in $(/app/.venv/bin/pytest tests --co -q --no-cov 2>/dev/null | grep :: | sed 's/::.*//' | sort -u); do
    /app/.venv/bin/pytest "$f" \
        --no-cov \
        --deselect 'tests/test_common/test_model_checks.py::test_model_checks[TimeLLM]' \
        --junitxml="/results/junit_$(echo "$f" | tr '/.' '__').xml" \
        -q --continue-on-collection-errors
done

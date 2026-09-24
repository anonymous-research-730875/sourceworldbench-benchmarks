from pathlib import Path

import pytest

from sourceworldbench_benchmarks.k8s import K8sRunConfig, K8sRunError
from sourceworldbench_benchmarks.k8s.config import LABEL_VALUE_MAX_LENGTH, _required_or_default_run_name


def test_config_rejects_non_gs_remote_root() -> None:
    with pytest.raises(K8sRunError, match="gs://"):
        K8sRunConfig(remote_root="/mnt/experiments/sourceworldbench/collect-runs")


def test_default_run_name_stays_unique_for_long_filenames() -> None:
    """A long filename stem must not crowd out the timestamp+uuid uniqueness suffix.

    The stem is truncated before the suffix is appended, so the suffix survives the
    label cap and same-day runs of one file get distinct run names.
    """
    long_path = Path("data/sourceworldbench-benchmarks-poc.nonroot.partial.no-test-scope-mods.jsonl")
    config = K8sRunConfig()

    first = _required_or_default_run_name(config, long_path)
    second = _required_or_default_run_name(config, long_path)

    assert first != second
    assert len(first) <= LABEL_VALUE_MAX_LENGTH
    assert len(second) <= LABEL_VALUE_MAX_LENGTH

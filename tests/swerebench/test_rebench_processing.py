"""Tests for ``swerebench.rebench_processing``: the CLI instance-selection helper
``select_rebench_ids`` and the ``process_rebench_instance`` edge-case fallback routing.

``select_rebench_ids`` resolves the CLI selectors (``--rebench-id`` / ``--index`` /
neither) into an ordered list of bare rebench instance ids, with ``--limit`` applied
last. A plain list of dicts stands in for a HF dataset (it supports both iteration and
positional indexing).

The fallback tests check that an unidentifiable golden commit or an environment patch no
longer aborts the instance: instead the failure is recorded in ``integration_failures``
and the golden_base build mode (``create_base_datapoints``' ``golden_mode``) is chosen
accordingly.
"""

import pytest

from sourceworldbench_benchmarks.swerebench import rebench_processing as rp
from sourceworldbench_benchmarks.swerebench.environment_patch import EnvironmentPatchError
from sourceworldbench_benchmarks.swerebench.golden_commit import GoldenCommitError
from sourceworldbench_benchmarks.swerebench.rebench_processing import process_rebench_instance, select_rebench_ids

DS = [
    {"instance_id": "repo__a"},
    {"instance_id": "repo__b"},
    {"instance_id": "repo__c"},
]


def test_no_selector_returns_whole_split_in_order():
    assert select_rebench_ids(DS) == ["repo__a", "repo__b", "repo__c"]


def test_rebench_ids_taken_verbatim():
    assert select_rebench_ids(DS, rebench_ids=["repo__c", "repo__a"]) == ["repo__c", "repo__a"]


def test_rebench_ids_unknown_raises():
    with pytest.raises(KeyError):
        select_rebench_ids(DS, rebench_ids=["repo__a", "repo__missing"])


def test_indices_select_positional_rows():
    assert select_rebench_ids(DS, indices=[2, 0]) == ["repo__c", "repo__a"]


def test_limit_applied_after_selection():
    assert select_rebench_ids(DS, limit=2) == ["repo__a", "repo__b"]
    assert select_rebench_ids(DS, rebench_ids=["repo__c", "repo__b", "repo__a"], limit=1) == ["repo__c"]


# --------------------------------------------------------------------------- #
# process_rebench_instance edge-case fallback routing
# --------------------------------------------------------------------------- #

GOLDEN = "0aa40c5126c7c4f88e9291eb6fb81eb3b880fe59"


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub the pipeline's external steps; capture how create_base_datapoints was called."""
    captured: dict[str, object] = {}
    dp = {"instance_id": "astropy__astropy-17642", "repo": "astropy/astropy", "docker_image": "swerebench/img"}

    monkeypatch.setattr(rp, "load_rebench_datapoint", lambda rebench_id: dp)
    monkeypatch.setattr(rp, "identify_golden_commit", lambda d: GOLDEN)
    monkeypatch.setattr(rp, "detect_environment_patch", lambda img: None)

    def fake_build(rebench_dp, *, golden_commit, golden_mode, integration_failures, timeout=600):
        captured["golden_commit"] = golden_commit
        captured["golden_mode"] = golden_mode
        captured["integration_failures"] = integration_failures
        captured["timeout"] = timeout
        # golden_base "failed" so trajectory generation is not reached.
        return ({"golden_base_test_run_status": "failed", "golden_base_instance_id": None}, {})

    monkeypatch.setattr(rp, "create_base_datapoints", fake_build)
    return captured, monkeypatch


def test_clean_image_uses_checkout_mode(stub_pipeline):
    captured, _ = stub_pipeline
    result = process_rebench_instance("astropy__astropy-17642")
    assert captured["golden_commit"] == GOLDEN
    assert captured["golden_mode"] == rp.GoldenBaseMode.CHECKOUT
    assert result["integration_failures"] == []
    assert result["trajectories"] is None


def test_timeout_is_threaded_into_base_datapoints(stub_pipeline):
    captured, _ = stub_pipeline
    process_rebench_instance("astropy__astropy-17642", timeout=1800)
    assert captured["timeout"] == 1800


def test_golden_commit_error_falls_back_to_patches_with_null_commit(stub_pipeline):
    captured, monkeypatch = stub_pipeline
    monkeypatch.setattr(rp, "identify_golden_commit", lambda d: GoldenCommitError.PR_NOT_MERGED)

    result = process_rebench_instance("astropy__astropy-17642")
    assert captured["golden_commit"] is None
    assert captured["golden_mode"] == rp.GoldenBaseMode.PATCHES
    assert result["golden_commit"] is None
    assert "pr_not_merged" in result["integration_failures"]


def test_environment_patch_present_falls_back_to_patches(stub_pipeline):
    captured, monkeypatch = stub_pipeline
    monkeypatch.setattr(rp, "detect_environment_patch", lambda img: EnvironmentPatchError.ENVIRONMENT_PATCH_PRESENT)

    result = process_rebench_instance("astropy__astropy-17642")
    assert captured["golden_commit"] == GOLDEN  # still identified; only the build mode changes
    assert captured["golden_mode"] == rp.GoldenBaseMode.PATCHES
    assert result["integration_failures"] == ["environment_patch_present"]


def test_inspect_failure_skips_golden_base(stub_pipeline):
    captured, monkeypatch = stub_pipeline
    monkeypatch.setattr(rp, "detect_environment_patch", lambda img: EnvironmentPatchError.INSPECT_FAILED)

    result = process_rebench_instance("astropy__astropy-17642")
    assert captured["golden_mode"] == rp.GoldenBaseMode.SKIP
    assert result["integration_failures"] == ["inspect_failed"]


def test_both_failures_recorded_and_golden_error_wins_the_build(stub_pipeline):
    captured, monkeypatch = stub_pipeline
    monkeypatch.setattr(rp, "identify_golden_commit", lambda d: GoldenCommitError.DIFF_MISMATCH)
    monkeypatch.setattr(rp, "detect_environment_patch", lambda img: EnvironmentPatchError.ENVIRONMENT_PATCH_PRESENT)

    result = process_rebench_instance("astropy__astropy-17642")
    # Golden-unknown (case A) wins the build (its patch build also covers the env patch)...
    assert captured["golden_commit"] is None
    assert captured["golden_mode"] == rp.GoldenBaseMode.PATCHES
    # ...but both detected failures are reported.
    assert result["integration_failures"] == ["diff_mismatch", "environment_patch_present"]


@pytest.mark.parametrize(
    "golden, env, expect_reverse",
    [
        (GOLDEN, None, False),  # main path (checkout) → checkout-restore trajectories
        (GoldenCommitError.DIFF_MISMATCH, None, True),  # fallback (patches) → reverse-patch
    ],
)
def test_trajectory_restore_mode_matches_build_mode(monkeypatch, golden, env, expect_reverse):
    from sourceworldbench_benchmarks.schema import StateDatapoint

    dp = {"instance_id": "astropy__astropy-17642", "repo": "astropy/astropy", "docker_image": "swerebench/img"}
    golden_base = StateDatapoint(
        instance_id="astropy__none_20260101-000000__base",
        repo="astropy/astropy",
        base_commit=None,
        container="img",
        command="c",
        test_scope=["tests/"],
    )

    monkeypatch.setattr(rp, "load_rebench_datapoint", lambda rebench_id: dp)
    monkeypatch.setattr(rp, "identify_golden_commit", lambda d: golden)
    monkeypatch.setattr(rp, "detect_environment_patch", lambda img: env)
    monkeypatch.setattr(
        rp,
        "create_base_datapoints",
        lambda rebench_dp, **kw: (
            {"golden_base_test_run_status": "ok", "golden_base_instance_id": golden_base.instance_id},
            {golden_base.instance_id: golden_base},
        ),
    )

    captured: dict[str, object] = {}

    def fake_trajectories(rebench_dp, golden_base_row, *, use_reverse_patch, **kw):
        captured["use_reverse_patch"] = use_reverse_patch
        return []

    monkeypatch.setattr(rp, "create_trajectory_augmentations", fake_trajectories)

    process_rebench_instance("astropy__astropy-17642")
    assert captured["use_reverse_patch"] is expect_reverse

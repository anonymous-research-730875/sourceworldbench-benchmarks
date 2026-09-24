"""Tests for ``swerebench.from_trajectory``: the two trajectory-diff restore strategies
(checkout for the main path, reverse-patch for the PR #50 fallback), the mutual-exclusion
guard, and the nested ``rebench_integration`` metadata on augmentation rows.

The diff itself runs in a container, so these cover only the pure logic: the emitted
shell scripts, the argument guard, and the row builder.
"""

import pytest

from sourceworldbench_benchmarks.swerebench import from_trajectory as ft


def test_checkout_script_rewinds_then_applies_traj_and_test():
    base = "0" * 40
    script = ft._diff_script_checkout(base)
    assert f"git checkout -f {base}" in script
    # main path re-applies test_patch (checkout wiped it); order: checkout < traj < test.
    assert script.index("git checkout") < script.index("/sourceworldbench_patches/traj.diff")
    assert script.index("/sourceworldbench_patches/traj.diff") < script.index("/sourceworldbench_patches/test.diff")
    # it does not reverse a fix patch.
    assert "fix.diff" not in script


def test_reverse_patch_script_reverses_fix_without_checkout():
    script = ft._diff_script_reverse_patch()
    # fallback restores the base tree by reverse-applying the fix — never a checkout,
    # so the environment patch survives.
    assert "git checkout" not in script
    assert "-R /sourceworldbench_patches/fix.diff" in script
    # test_patch is not re-applied here (it survives in the working tree).
    assert "test.diff" not in script
    assert "/sourceworldbench_patches/traj.diff" in script


def test_compute_trajectory_patch_requires_exactly_one_restore_strategy():
    # neither
    with pytest.raises(ValueError):
        ft.compute_trajectory_patch(golden_base_image="img", model_patch="d")
    # both
    with pytest.raises(ValueError):
        ft.compute_trajectory_patch(
            golden_base_image="img", model_patch="d", base_commit="0" * 40, fix_patch="diff"
        )


def _golden_base_row(**over) -> dict:
    row = {
        "instance_id": "sepal_ui__deb5b9ab__base",
        "repo": "12rambau/sepal_ui",
        "base_commit": "deb5b9ab1234567890abcdef1234567890abcdef",
        "container": "img",
        "command": "cmd",
        "test_scope": ["tests/"],
        "metadata": {
            "rebench_integration": {
                "dataset": "nebius/SWE-rebench",
                "golden_commit": "deb5b9ab1234567890abcdef1234567890abcdef",
                "rebench_base_commit": "0" * 40,
                "integration_failures": ["environment_patch_present"],
            }
        },
    }
    row.update(over)
    return row


def test_augmentation_row_nests_and_inherits_rebench_integration():
    traj = {"trajectory_id": "t7", "instance_id": "12rambau__sepal_ui-411"}
    row = ft.trajectory_augmentation_row(_golden_base_row(), traj, patch="some diff")

    assert row["instance_id"] == "sepal_ui__deb5b9ab__rebench_trajectory_t7"
    assert row["patch"] == "some diff"
    ri = row["metadata"]["rebench_integration"]
    assert ri["parent"] == "sepal_ui__deb5b9ab__base"
    assert ri["trajectory_id"] == "t7"
    assert ri["rebench_instance_id"] == "12rambau__sepal_ui-411"
    assert ri["trajectory_dataset"] == ft.TRAJECTORY_DATASET
    # inherited from the golden_base row.
    assert ri["dataset"] == "nebius/SWE-rebench"
    assert ri["golden_commit"] == "deb5b9ab1234567890abcdef1234567890abcdef"
    assert ri["integration_failures"] == ["environment_patch_present"]


def test_augmentation_row_carries_null_base_commit_from_fallback_parent():
    # Case A golden_base has base_commit None + none_<ts> base-id; augmentations inherit it.
    parent = _golden_base_row(
        instance_id="sepal_ui__none_20260101-000000__base",
        base_commit=None,
        metadata={"rebench_integration": {"golden_commit": None, "integration_failures": ["diff_mismatch"]}},
    )
    traj = {"trajectory_id": "t1", "instance_id": "12rambau__sepal_ui-411"}
    row = ft.trajectory_augmentation_row(parent, traj, patch="d")

    assert row["base_commit"] is None
    assert row["instance_id"] == "sepal_ui__none_20260101-000000__rebench_trajectory_t1"
    # base_commit is None, so the augmentation must carry its (parent-matching) base-id.
    assert row["metadata"]["base_id"] == "none_20260101-000000"
    assert row["metadata"]["rebench_integration"]["integration_failures"] == ["diff_mismatch"]

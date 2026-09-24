"""Tests for the rebench → StateDatapoint row builder (``swerebench.to_datapoint``).

Covers the naming and ``base_commit`` conventions (instance ids anchor on the golden
commit, with the original rebench id + pre-fix commit kept in ``metadata``), the nested
``rebench_integration`` metadata block, and the edge-case fallback where the golden
commit is unidentified (``base_commit=None``, ``none_<timestamp>`` base-id).
"""

from sourceworldbench_benchmarks.schema import BASE_ID_METADATA_KEY
from sourceworldbench_benchmarks.swerebench.to_datapoint import (
    rebench_base_id,
    rebench_base_stem,
    rebench_datapoint_row,
)

GOLDEN = "deb5b9ab1234567890abcdef1234567890abcdef"
REBENCH_BASE = "0" * 40


def _rebench_datapoint(**over) -> dict:
    dp = {
        "instance_id": "12rambau__sepal_ui-411",
        "repo": "12rambau/sepal_ui",
        "base_commit": REBENCH_BASE,
        "install_config": {"test_cmd": "python -m pytest tests/"},
    }
    dp.update(over)
    return dp


def _ri(row: dict) -> dict:
    """The nested rebench_integration metadata block."""
    return row["metadata"]["rebench_integration"]


def test_rebench_base_stem_anchors_on_short_golden_commit():
    # <repo-leaf>__<golden_commit[:8]>, matching the repo-wide id convention.
    assert rebench_base_stem("12rambau/sepal_ui", GOLDEN) == f"sepal_ui__{GOLDEN[:8]}"


def test_rebench_base_stem_uses_repo_leaf_only():
    assert rebench_base_stem("owner/nested/name", GOLDEN).startswith("name__")


def test_rebench_base_stem_anchors_on_any_commit():
    assert rebench_base_stem("12rambau/sepal_ui", REBENCH_BASE) == f"sepal_ui__{REBENCH_BASE[:8]}"


def test_rebench_base_id_known_commit_is_short_sha():
    assert rebench_base_id(GOLDEN) == GOLDEN[:8]


def test_rebench_base_id_none_is_timestamp_placeholder():
    # An opaque, filesystem-safe `none_<UTC timestamp>` token when the commit is unknown.
    base_id = rebench_base_id(None)
    assert base_id.startswith("none_")
    ts = base_id.removeprefix("none_")
    date, _, time = ts.partition("-")
    assert len(date) == 8 and date.isdigit()
    assert len(time) == 6 and time.isdigit()


def test_row_base_commit_is_golden_commit():
    row = rebench_datapoint_row(
        _rebench_datapoint(),
        instance_id=f"sepal_ui__{GOLDEN[:8]}__base",
        base_commit=GOLDEN,
        golden_commit=GOLDEN,
    )
    assert row["base_commit"] == GOLDEN
    assert row["patch"] is None


def test_row_rebench_base_anchors_on_rebench_base_commit():
    # The standalone rebench_base row anchors on the rebench base commit, not golden. It is
    # a bare base state, so it takes the reserved `__base` suffix just like the golden_base row.
    instance_id = f"sepal_ui__{REBENCH_BASE[:8]}__base"
    row = rebench_datapoint_row(
        _rebench_datapoint(), instance_id=instance_id, base_commit=REBENCH_BASE, golden_commit=GOLDEN
    )
    assert row["base_commit"] == REBENCH_BASE
    assert row["patch"] is None
    # golden provenance is still recorded in metadata.
    assert _ri(row)["golden_commit"] == GOLDEN
    assert _ri(row)["rebench_instance_id"] == "12rambau__sepal_ui-411"


def test_row_unidentified_golden_commit_has_null_base_commit():
    # Case A fallback: golden commit unknown → base_commit None + none_<ts> base-id.
    instance_id = f"sepal_ui__{rebench_base_id(None)}__base"
    row = rebench_datapoint_row(
        _rebench_datapoint(),
        instance_id=instance_id,
        base_commit=None,
        golden_commit=None,
        integration_failures=["diff_mismatch"],
    )
    assert row["base_commit"] is None
    # base_commit is None, so metadata carries the base-id that keeps the state keyable.
    assert row["metadata"][BASE_ID_METADATA_KEY] == instance_id.split("__")[1]
    assert row["metadata"][BASE_ID_METADATA_KEY].startswith("none_")
    assert _ri(row)["golden_commit"] is None
    assert _ri(row)["integration_failures"] == ["diff_mismatch"]


def test_row_metadata_preserves_rebench_provenance():
    instance_id = f"sepal_ui__{GOLDEN[:8]}__base"
    row = rebench_datapoint_row(
        _rebench_datapoint(), instance_id=instance_id, base_commit=GOLDEN, golden_commit=GOLDEN
    )
    ri = _ri(row)
    assert ri["rebench_instance_id"] == "12rambau__sepal_ui-411"
    assert ri["rebench_base_commit"] == REBENCH_BASE
    assert ri["golden_commit"] == GOLDEN
    # commit is the trailing id segment (the suffix); failures default to an empty list.
    assert ri["commit"] == "base"
    assert ri["integration_failures"] == []


def test_row_instance_id_and_repo_are_carried_through():
    instance_id = f"sepal_ui__{GOLDEN[:8]}__base"
    row = rebench_datapoint_row(
        _rebench_datapoint(), instance_id=instance_id, base_commit=GOLDEN, golden_commit=GOLDEN
    )
    assert row["instance_id"] == instance_id
    assert row["repo"] == "12rambau/sepal_ui"


def test_row_default_container_keyed_by_base_stem_not_instance_id():
    # The default container is the local tag of the `<repo>__<base-id>` stem; the `__base`
    # suffix is carried only by the instance_id.
    instance_id = f"sepal_ui__{GOLDEN[:8]}__base"
    row = rebench_datapoint_row(
        _rebench_datapoint(), instance_id=instance_id, base_commit=GOLDEN, golden_commit=GOLDEN
    )
    assert row["instance_id"] == instance_id
    assert "__base" not in row["container"]
    assert row["container"] == f"sourceworldbench/sepal_ui__{GOLDEN[:8]}:dev"


def test_row_default_command_rendered_from_test_cmd():
    row = rebench_datapoint_row(
        _rebench_datapoint(),
        instance_id=f"sepal_ui__{GOLDEN[:8]}__base",
        base_commit=GOLDEN,
        golden_commit=GOLDEN,
    )
    assert "python -m pytest tests/" in row["command"]


def test_row_explicit_overrides_win():
    row = rebench_datapoint_row(
        _rebench_datapoint(),
        instance_id=f"sepal_ui__{GOLDEN[:8]}__base",
        base_commit=GOLDEN,
        golden_commit=GOLDEN,
        container="registry.example/img:tag",
        command="custom-cmd",
        test_scope=["pkg/"],
    )
    assert row["container"] == "registry.example/img:tag"
    assert row["command"] == "custom-cmd"
    assert row["test_scope"] == ["pkg/"]


def test_row_default_test_scope():
    row = rebench_datapoint_row(
        _rebench_datapoint(),
        instance_id=f"sepal_ui__{GOLDEN[:8]}__base",
        base_commit=GOLDEN,
        golden_commit=GOLDEN,
    )
    assert row["test_scope"] == ["tests/"]

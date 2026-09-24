"""Instance-id / pair-id format contract tests.

The `<base-id>` segment (the second `__`-segment) is an opaque token: the short
base_commit SHA when the commit is known, or a `none_<timestamp>` placeholder
when it is not. These tests pin that it is accepted and never validated as a SHA.
"""

import pytest
from pydantic import ValidationError

from sourceworldbench_benchmarks.schema import BASE_ID_METADATA_KEY, StateDatapoint, base_key
from sourceworldbench_benchmarks.schema_pair import PairDatapoint, make_pair_id

_ROW = {
    "repo": "anonymous-research-730875/sourceworldbench-toy-repo",
    "base_commit": "a" * 40,
    "container": "xxx-docker.pkg.dev/xxx/ml/sourceworldbench/example:dev",
    "command": "true",
    "test_scope": ["tests/"],
}


@pytest.mark.parametrize(
    "instance_id",
    [
        "hvac__09902dea__base",  # known commit (short SHA base-id)
        "hvac__09902dea__ruff__format",
        "hvac__none_20260723-120000__base",  # unknown commit (placeholder base-id)
        "hvac__none_1721745600__ruff__format",
    ],
)
def test_instance_id_accepts_sha_and_placeholder_base_ids(instance_id: str) -> None:
    assert StateDatapoint(instance_id=instance_id, **_ROW).instance_id == instance_id


@pytest.mark.parametrize(
    "instance_id",
    [
        "hvac",  # single segment: no base-id at all
        "hvac__",  # empty base-id segment
        "hvac__none 1721745600__base",  # space is outside the segment alphabet
    ],
)
def test_instance_id_rejects_malformed_ids(instance_id: str) -> None:
    with pytest.raises(ValidationError):
        StateDatapoint(instance_id=instance_id, **_ROW)


def test_base_key_uses_commit_when_known() -> None:
    dp = StateDatapoint(instance_id="hvac__09902dea__base", **{**_ROW, "base_commit": "a" * 40})
    assert base_key(dp) == ("anonymous-research-730875/sourceworldbench-toy-repo", "a" * 40)


def test_base_key_falls_back_to_base_id_when_commit_is_none() -> None:
    # Two unidentified-commit states of the same repo must stay distinct: the key uses
    # their metadata base-id, not a shared None.
    row = {**_ROW, "base_commit": None}
    dp1 = StateDatapoint(
        instance_id="hvac__none_20260723-120000__base",
        metadata={BASE_ID_METADATA_KEY: "none_20260723-120000"},
        **row,
    )
    dp2 = StateDatapoint(
        instance_id="hvac__none_20260723-130000__base",
        metadata={BASE_ID_METADATA_KEY: "none_20260723-130000"},
        **row,
    )
    assert base_key(dp1) == ("anonymous-research-730875/sourceworldbench-toy-repo", "none_20260723-120000")
    assert base_key(dp2) == ("anonymous-research-730875/sourceworldbench-toy-repo", "none_20260723-130000")
    assert base_key(dp1) != base_key(dp2)


def test_base_key_raises_when_commit_none_and_no_base_id() -> None:
    dp = StateDatapoint(instance_id="hvac__none_20260723-120000__base", **{**_ROW, "base_commit": None})
    with pytest.raises(ValueError):
        base_key(dp)


def test_pair_id_roundtrips_placeholder_base_id() -> None:
    a = "hvac__none_20260723-120000__base"
    b = "hvac__none_20260723-120000__ruff__format"
    pair_id = make_pair_id(a, b)
    assert pair_id == "hvac__none_20260723-120000__base__::__ruff__format"
    # A pair_id built from placeholder-base-id states validates.
    PairDatapoint(
        pair_id=pair_id,
        repo="h/hvac",
        base_commit="a" * 40,
        container="c",
        command="pytest",
        is_base_healthy=True,
        instance_id_A=a,
        instance_id_B=b,
        patch_A=None,
        patch_B="diff",
        passed_tests_A=[],
        passed_tests_B=[],
        failed_tests_A=[],
        failed_tests_B=[],
        skipped_tests_A=[],
        skipped_tests_B=[],
        errored_tests_A=[],
        errored_tests_B=[],
        discovery_errors_A=[],
        discovery_errors_B=[],
        is_clone=None,
        num_flipped_tests=0,
        flipped_ratio=0.0,
    )

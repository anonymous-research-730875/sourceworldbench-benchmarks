import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# `instance_id` convention: `__`-separated, filesystem-safe segments. The first two are
# `<repo>__<base-id>` (the base stem). `<base-id>` uniquely identifies the base state: it is the
# `base_commit`'s short SHA when the commit is known (`hvac__09902dea__base`), or a
# `none_<timestamp>` placeholder when it is not (`hvac__none_20260723-120000__base`). Either way it
# is an opaque token — nothing parses it back into a commit. A base state appends the reserved
# `base` segment, an augmented state appends augmentation segments instead
# (e.g. `hvac__09902dea__ruff__format`). A segment never contains the `__` separator itself,
# so `instance_id.split("__")` recovers the components.
_INSTANCE_ID_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")

BaseKey = tuple[str, str]

# Metadata key holding the `<base-id>` — the second `instance_id` segment. It is the
# stable identifier of a base state when `base_commit` is unknown (`None`): unlike a
# shared `None`, it distinguishes base states of the same repo whose commit could not be
# identified. See `base_key`.
BASE_ID_METADATA_KEY = "base_id"


def base_key(datapoint: "StateDatapoint") -> BaseKey:
    """The registry key shared by a base state and all its augmentations.

    `(repo, base_commit)` when the commit is known. When `base_commit` is `None`, the
    commit cannot identify the state, so the key falls back to the `<base-id>` recorded in
    `metadata[BASE_ID_METADATA_KEY]` — `(repo, base_id)` — which keeps distinct base states
    of the same repo apart. Raises `ValueError` when neither is available.
    """
    if datapoint.base_commit is not None:
        return (datapoint.repo, datapoint.base_commit)
    base_id = datapoint.metadata.get(BASE_ID_METADATA_KEY)
    if not base_id:
        raise ValueError(
            f"cannot key {datapoint.instance_id!r}: base_commit is None and "
            f"metadata[{BASE_ID_METADATA_KEY!r}] is missing"
        )
    return (datapoint.repo, str(base_id))


class StateDatapoint(BaseModel):
    """One benchmark example as a single repository state and its own outcomes.

    A state is `base_commit` plus an optional `patch`: the base state has
    `patch=None`; an augmented state has `patch=<diff>`. When present, the patch
    is `git apply`'d before `command` runs inside `container`.

    The test-outcome fields hold this one state's own results. They are `None` on 
    partial rows and lists on filled rows. `discovery_errors` is kept separate 
    from `errored_tests`: it tracks module-level collection failures (reported 
    outside any testcase), not per-test setup/teardown errors.
    """

    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(
        ...,
        description=(
            "Stable, filesystem-safe identifier following `<repo>__<base-id>__base` for a "
            "base state, with `__`-separated augmentation segments in place of `base` for an "
            "augmented state. `<base-id>` is the base_commit's short SHA when known, else a "
            "`none_<timestamp>` placeholder; it is an opaque token, not parsed as a commit."
        ),
    )
    repo: str = Field(..., min_length=1, description="`<owner>/<repo>` short form.")

    @field_validator("instance_id")
    @classmethod
    def _validate_instance_id(cls, value: str) -> str:
        """Enforce the `<repo>__<base-id>[__(base|<augmentation>...)]` convention.

        Requires the `<repo>__<base-id>` stem, optionally followed by suffix
        segments — `base` for a base state, augmentation names otherwise — with every
        `__`-separated segment non-empty and filesystem-safe. `augment_hf` relies on
        this shape to derive an augmented id from its base by replacing the trailing
        `base` segment. `<base-id>` is deliberately not validated as a SHA: it is the
        base_commit's short SHA when the commit is known, or a `none_<timestamp>`
        placeholder otherwise. Legacy rows use the bare two-segment stem as their base state
        id; they still exist in the published datasets, so the stem alone is accepted.
        """
        segments = value.split("__")
        # TODO: fix datapoints that don't follow this format (e.g. derivatives of flask-wtf__8849b06)
        # TODO: migrate legacy two-segment datapoints (e.g. flask-wtf__8849b06) to the __base suffix
        if len(segments) < 2 or any(not _INSTANCE_ID_SEGMENT.fullmatch(seg) for seg in segments):
            raise ValueError(
                f"instance_id {value!r} must follow '<repo>__<base-id>__(base|<augmentation>...)' "
                "with non-empty, filesystem-safe `__`-separated segments"
            )

        return value

    base_commit: str | None = Field(
        default=...,
        description=(
            "40-character SHA-1 of the base state, or None when the base commit is "
            "unknown (its `<base-id>` is then a `none_<timestamp>` placeholder)."
        ),
    )
    patch: str | None = Field(
        default=None,
        min_length=1,
        description="Unified diff applied on top of base_commit; None for the bare base state.",
    )

    container: str = Field(
        ...,
        min_length=1,
        description="Full container image reference (registry/repo/name:tag).",
    )
    command: str = Field(..., min_length=1, description="Evaluation command to run inside the container.")
    command_workload: str | None = Field(
        default=None,
        description=(
            "Alternative command that runs a workload instead of the full test suite. "
        ),
    )
    command_workload_amplified: str | None = Field(
        default=None,
        description=(
            "LLM-generated memory-profiling workload: three tests at light/medium/heavy "
            "input scales, amplified from `command_workload` where possible, otherwise "
            "written from scratch against the pair's patch."
        ),
    )
    test_scope: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Repo-relative path(s) to files or directories that hold the tests this row evaluates. "
            "Typically ['tests/']. Used to verify that a patch does not modify the test suite itself."
        ),
    )

    @field_validator("test_scope")
    @classmethod
    def _validate_test_scope(cls, value: list[str]) -> list[str]:
        """Reject empty entries, absolute paths, and `..` segments.

        We need this to be a list of repo-relative paths so it can later be used
        as a prefix-match scope for patch validation. Absolute paths and `..`
        escape the repo and would make that check meaningless.
        """
        for entry in value:
            if not entry or not entry.strip():
                raise ValueError("test_scope entries must be non-empty strings")
            if entry.startswith("/"):
                raise ValueError(f"test_scope entry {entry!r} must be repo-relative, not absolute")
            if ".." in entry.split("/"):
                raise ValueError(f"test_scope entry {entry!r} must not contain '..' segments")
        return value

    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary JSON-serializable metadata; empty on base rows from add-base, filled during collect.",
    )

    @field_validator("metadata", mode="before")
    @classmethod
    def _decode_metadata(cls, value: Any) -> Any:
        """Accept `metadata` as a JSON-encoded string.

        This form it takes when rows are stored as a flat string column,
        e.g., when exported from a HF dataset.
        """
        if isinstance(value, str):
            return json.loads(value) if value else {}
        return value

    passed_tests: list[str] | None = Field(
        default=None, description="Tests passing in this state, or None if not filled yet."
    )
    failed_tests: list[str] | None = Field(
        default=None, description="Tests failing in this state, or None if not filled yet."
    )
    skipped_tests: list[str] | None = Field(
        default=None, description="Tests skipped in this state, or None if not filled yet."
    )
    errored_tests: list[str] | None = Field(
        default=None, description="Tests erroring in this state, or None if not filled yet."
    )
    discovery_errors: list[str] | None = Field(
        default=None,
        description=(
            "Discovery errors reported outside this state's testcases, "
            "for example a Python module that failed pytest collection, or None if not filled yet."
        ),
    )
    execution_data: dict[str, str] | None = Field(
        default=None,
        description=(
            "Map from tracer name ('trace', 'walltime', 'memprof', 'cprofile') to a that tracer's output artifact."
            "None on rows that have not been traced yet."
        ),
    )


# The outcome fields filled by the collector. A row is "filled" when all of these
# are non-None. Single source of truth shared by the collector's completeness check
# and the Hugging Face `hf` command.
STATE_OUTCOME_FIELDS: tuple[str, ...] = (
    "passed_tests",
    "failed_tests",
    "skipped_tests",
    "errored_tests",
    "discovery_errors",
)

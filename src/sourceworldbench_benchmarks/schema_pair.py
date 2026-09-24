import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_INSTANCE_ID_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
INSTANCE_ID_SEGMENT = _INSTANCE_ID_SEGMENT


def is_base_state(patch: str | None) -> bool:
    """True when the state is the bare base commit (no patch applied)."""
    return patch is None

# `pair_id` joins the two states' `instance_id` suffixes with `__::__`. The `::` is outside
# the `instance_id` segment alphabet `[A-Za-z0-9._-]` (see `repo_states.schema`), so it can
# never appear inside a repo name, base id, or augmentation suffix — which makes the
# delimiter collision-proof without reserving a keyword, and `pair_id.split(PAIR_ID_DELIMITER)`
# always recovers exactly the two halves. A `pair_id` therefore looks like
# `<repo>__<base-id>__<suffixA>__::__<suffixB>`, e.g.
# `flask-wtf__de1faa6b__base__::__ruff__format` (`<base-id>` is the base_commit's short SHA when
# known, else a `none_<timestamp>` placeholder — always an opaque token, never parsed).
PAIR_ID_DELIMITER = "__::__"


def make_pair_id(instance_id_a: str, instance_id_b: str) -> str:
    """Construct the `pair_id` for two states of the same base (the inverse of splitting on `__::__`).

    `instance_id_a` already carries the shared `<repo>__<base-id>` stem plus its own suffix
    segment (the reserved `base` for the bare base state), so only state B's suffix (its segments
    after that stem) is appended after the delimiter, e.g.
    `(flask-wtf__de1faa6b__base, flask-wtf__de1faa6b__ruff__format)` ->
    `flask-wtf__de1faa6b__base__::__ruff__format`.
    """
    suffix_b = "__".join(instance_id_b.split("__")[2:])
    return f"{instance_id_a}{PAIR_ID_DELIMITER}{suffix_b}"


class PairDatapoint(BaseModel):
    """One pairwise benchmark example built from two single repository states.

    A pair joins two `repo_states.schema.StateDatapoint`s — state A and state B — that share
    the same `repo` and `base_commit` (and therefore the same `container` / `command`). Each state
    carries its own `patch` (a diff against the base commit, *not* against the other state)
    and its own test outcomes; the pair lays both states' fields out alongside each other and
    adds the cross-state comparison (`is_clone`, `num_flipped_tests`, `flipped_ratio`).

    It is an intermediate artifact derived from two **already-filled** single-states, with no
    container run of its own; the downstream task is derived from it with simple deterministic
    logic.

    Because both states are always filled when a pair is built, the per-state outcome lists
    are required (unlike the `None`-until-filled lists on `StateDatapoint`). Only `is_clone`
    is nullable: cloneness is undefined when the base state is unhealthy.
    """

    model_config = ConfigDict(extra="forbid")

    pair_id: str = Field(
        ...,
        description=(
            "Stable, filesystem-safe identifier `<repo>__<base-id>__<suffixA>__::__<suffixB>`, "
            "joining the two states' instance_id suffixes with the `__::__` delimiter."
        ),
    )
    repo: str = Field(..., min_length=1, description="`<owner>/<repo>` short form, shared by both states.")
    base_commit: str = Field(..., description="40-character SHA-1 of the base state shared by both states.")

    container: str = Field(
        ...,
        min_length=1,
        description="Full container image reference (registry/repo/name:tag), shared by both states.",
    )
    command: str = Field(
        ..., min_length=1, description="Evaluation command run inside the container, shared by both states."
    )
    is_base_healthy: bool = Field(
        ...,
        description=(
            "True when the bare base state of this base_commit (the stem's `__base` state) has none of: "
            "failed tests, errored tests, or discovery errors. It is a property of the base, shared by "
            "every pair of that base — not of state A or B."
        ),
    )

    instance_id_A: str = Field(..., min_length=1, description="Full single-state `instance_id` of state A.")
    instance_id_B: str = Field(..., min_length=1, description="Full single-state `instance_id` of state B.")
    patch_A: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Unified diff that produces state A from base_commit (a diff against the base, not against "
            "state B); None when state A is the bare base state."
        ),
    )
    patch_B: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Unified diff that produces state B from base_commit (a diff against the base, not against "
            "state A); None when state B is the bare base state."
        ),
    )

    passed_tests_A: list[str] = Field(..., description="Tests passing in state A.")
    passed_tests_B: list[str] = Field(..., description="Tests passing in state B.")
    failed_tests_A: list[str] = Field(..., description="Tests failing in state A.")
    failed_tests_B: list[str] = Field(..., description="Tests failing in state B.")
    skipped_tests_A: list[str] = Field(..., description="Tests skipped in state A.")
    skipped_tests_B: list[str] = Field(..., description="Tests skipped in state B.")
    errored_tests_A: list[str] = Field(..., description="Tests erroring in state A.")
    errored_tests_B: list[str] = Field(..., description="Tests erroring in state B.")
    discovery_errors_A: list[str] = Field(
        ...,
        description="Discovery errors reported outside state A's testcases (e.g. a module that failed collection).",
    )
    discovery_errors_B: list[str] = Field(
        ...,
        description="Discovery errors reported outside state B's testcases (e.g. a module that failed collection).",
    )

    metadata_A: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arbitrary JSON-serializable metadata about state A (copied from state A's `metadata`). "
            "Encoded as a JSON string in the Hub parquet schema — an empty struct cannot be written to "
            "Parquet (and so breaks the Hugging Face push), whereas a JSON string round-trips cleanly."
        ),
    )
    metadata_B: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arbitrary JSON-serializable metadata about state B (copied from state B's `metadata`). See `metadata_A`."
        ),
    )

    is_clone: bool | None = Field(
        ...,
        description=(
            "Whether the two states are clones (observable test behaviour unchanged): with a "
            "healthy base, True when neither state has any errored tests and their passed, "
            "failed, and skipped test sets are each identical. None when the base state is "
            "unhealthy, where cloneness is undefined."
        ),
    )
    num_flipped_tests: int = Field(..., ge=0, description="Number of tests whose status differs across the two states.")
    flipped_ratio: float = Field(
        ..., ge=0.0, le=1.0, description="Flipped tests over all observed tests (0.0 when no tests are observed)."
    )
    pair_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Arbitrary JSON-serializable metadata about the pair itself (as opposed to the per-state "
            "`metadata_A` / `metadata_B`). Encoded as a JSON string in the Hub parquet schema; see `metadata_A`."
        ),
    )

    @field_validator("metadata_A", "metadata_B", "pair_metadata", mode="before")
    @classmethod
    def _decode_metadata(cls, value: Any) -> Any:
        """Accept metadata as a JSON-encoded string (the form it takes in the Hub parquet schema)."""
        if isinstance(value, str):
            return json.loads(value) if value else {}
        return value

    @field_validator("pair_id")
    @classmethod
    def _validate_pair_id(cls, value: str) -> str:
        """Enforce `<repo>__<base-id>__<suffixA>__::__<suffixB>` with a single `__::__` delimiter.

        Both halves around the delimiter must be non-empty and `__`-segment-safe (the only
        non-alphabet token allowed is the `__::__` delimiter itself), so
        `value.split(PAIR_ID_DELIMITER)` recovers exactly the two `instance_id`-shaped halves.
        """
        halves = value.split(PAIR_ID_DELIMITER)
        if len(halves) != 2 or not all(
            half and all(INSTANCE_ID_SEGMENT.fullmatch(seg) for seg in half.split("__")) for half in halves
        ):
            raise ValueError(
                f"pair_id {value!r} must follow '<repo>__<base-id>__<suffixA>{PAIR_ID_DELIMITER}<suffixB>' "
                f"with a single {PAIR_ID_DELIMITER!r} delimiter and non-empty, filesystem-safe `__`-separated segments"
            )
        return value

    @model_validator(mode="after")
    def _validate_state_pairing(self) -> "PairDatapoint":
        """Enforce the two-state pairing invariants.

        - The two states must be distinct (`instance_id_A != instance_id_B`).
        - If the pair includes the bare base state, it must be state A. Pinning the base to A
          gives every (base, augmented) pair one canonical ordering, which keeps the dataset
          free of duplicate rows that differ only by swapping A and B and prevents the
          inconsistency of a base state appearing on either position.
        """
        if self.instance_id_A == self.instance_id_B:
            raise ValueError(f"instance_id_A and instance_id_B must differ; both are {self.instance_id_A!r}")
        if is_base_state(self.patch_B):
            raise ValueError(
                f"the base state must be state A, not state B: instance_id_B {self.instance_id_B!r} is a base state"
            )
        return self
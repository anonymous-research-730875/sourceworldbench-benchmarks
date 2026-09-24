"""Storage layer for the benchmark dataset.

Loads and pushes the Hugging Face dataset, reads and writes local JSONL, resolves
a ``--in`` value (local file or HF repo) to its rows and push target, and merges
rows by instance_id. Commands depend on this module; it depends only on the
schema and the HF libraries, so it never imports a command module.
"""

import json
from pathlib import Path
from types import UnionType
from typing import Any, get_args, get_origin

from datasets import Dataset, Features, Sequence, Value, load_dataset
from datasets.info import DatasetInfosDict
from huggingface_hub import DatasetCard, HfApi, hf_hub_download
from huggingface_hub.errors import RepositoryNotFoundError

from sourceworldbench_benchmarks.schema import StateDatapoint

DEFAULT_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-single-state"
DEFAULT_PAIRWISE_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-pairwise"
DEFAULT_CLONE_DETECTION_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-clone-detection"


def _state_features() -> Features:
    """Return the Hugging Face storage schema for `StateDatapoint`.

    Hugging Face infers `null` for columns whose uploaded values are all `None`,
    and `list<null>` for list columns whose uploaded values are all empty. The
    benchmark schema allows those fields to receive strings later, so pushes and
    loads use explicit features instead of relying on per-batch inference.
    """
    return Features(
        {name: _feature_from_annotation(field.annotation) for name, field in StateDatapoint.model_fields.items()}
    )


def _feature_from_annotation(annotation: Any) -> Any:
    annotation = _remove_none(annotation)
    if annotation is str:
        return Value("string")
    if get_origin(annotation) is list and get_args(annotation) == (str,):
        return Sequence(Value("string"))
    if get_origin(annotation) is dict and get_args(annotation) == (str, Any):
        # Arbitrary dicts are JSON-encoded strings in the Hub parquet schema.
        return Value("string")
    if get_origin(annotation) is dict and get_args(annotation) == (str, str):
        # execution_data: dict[str, str] — also JSON-encoded.
        return Value("string")
    raise TypeError(f"Unsupported StateDatapoint field annotation: {annotation!r}")


def _remove_none(annotation: Any) -> Any:
    if get_origin(annotation) is not UnionType:
        return annotation

    non_none_args = tuple(arg for arg in get_args(annotation) if arg is not type(None))
    if len(non_none_args) == 1:
        return non_none_args[0]
    return annotation


def _row_for_hub(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    metadata = out.get("metadata")
    if isinstance(metadata, dict):
        out["metadata"] = json.dumps(metadata, sort_keys=True, default=str)
    elif metadata is None:
        out["metadata"] = "{}"
    execution_data = out.get("execution_data")
    if isinstance(execution_data, dict):
        out["execution_data"] = json.dumps(execution_data, sort_keys=True)
    # execution_data=None stays None (field is optional)
    return out


def _row_from_hub(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    metadata = out.get("metadata")
    if metadata is None or metadata == "":
        out["metadata"] = {}
    elif isinstance(metadata, str):
        out["metadata"] = json.loads(metadata)
    execution_data = out.get("execution_data")
    if isinstance(execution_data, str) and execution_data:
        out["execution_data"] = json.loads(execution_data)
    # execution_data=None stays None
    return out


def read_jsonl(path: Path) -> list[StateDatapoint]:
    """Read a JSONL file into a list of validated `StateDatapoint` objects."""
    rows: list[StateDatapoint] = []
    with path.open("r", encoding="utf-8") as handle:
        for i, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(StateDatapoint.model_validate_json(stripped))
            except Exception as exc:
                raise ValueError(f"{path}:{i}: invalid StateDatapoint: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def load_existing_rows(
    repo_id: str,
    *,
    instance_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Load the existing dataset rows from the Hub, or return [] if the repo is empty/new.

    When ``instance_ids`` is provided, only rows whose ``instance_id`` matches are
    materialized. Filtering happens at the Arrow level before per-row Python
    conversion, which avoids pulling the whole dataset into Python when only a
    handful of rows are needed.
    """
    try:
        ds = load_dataset(repo_id, split="train")
    except (FileNotFoundError, RepositoryNotFoundError, ValueError):
        return []
    if instance_ids:
        wanted = set(instance_ids)
        ds = ds.filter(lambda iid: iid in wanted, input_columns=["instance_id"])
    return [_row_from_hub(dict(row)) for row in ds]


def push_rows(
    repo_id: str,
    rows: list[dict[str, Any]],
    *,
    commit_message: str,
    commit_description: str | None = None,
) -> None:
    hub_rows = [_row_for_hub(row) for row in rows]
    Dataset.from_list(hub_rows, features=_state_features()).push_to_hub(
        repo_id,
        commit_message=commit_message,
        commit_description=commit_description,
    )
    _sync_dataset_card_features(repo_id)


def _sync_dataset_card_features(repo_id: str) -> None:
    readme_path = hf_hub_download(repo_id, "README.md", repo_type="dataset")
    card = DatasetCard(Path(readme_path).read_text(encoding="utf-8"))
    dataset_infos = DatasetInfosDict.from_dataset_card_data(card.data)
    if "default" not in dataset_infos:
        return

    dataset_info = dataset_infos["default"]
    features = _state_features()
    if dataset_info.features == features:
        return

    dataset_info.features = features
    DatasetInfosDict({"default": dataset_info}).to_dataset_card_data(card.data)
    HfApi().upload_file(
        repo_id=repo_id,
        repo_type="dataset",
        path_in_repo="README.md",
        path_or_fileobj=str(card).encode("utf-8"),
        commit_message="Update dataset schema",
    )


def push_repo_for(in_path: str) -> str:
    """HF repo a ``--in`` value pushes to: itself if an HF repo, else the default dataset."""
    return DEFAULT_DATASET_REPO_ID if Path(in_path).is_file() else in_path


def resolve_source(
    in_path: str,
    *,
    instance_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Resolve a ``--in`` value to ``(rows, push_repo)``.

    An existing file is read as a local JSONL; anything else is treated as an HF
    dataset repo id. The push target is the same HF repo for an HF source, and
    ``DEFAULT_DATASET_REPO_ID`` for a local source.

    When ``instance_ids`` is provided, HF loads are filtered at the Arrow level
    so only matching rows are materialized. Local JSONL still reads the full
    file (callers filter afterwards).
    """
    path = Path(in_path)
    if path.is_file():
        return [row.model_dump() for row in read_jsonl(path)], DEFAULT_DATASET_REPO_ID
    return load_existing_rows(in_path, instance_ids=instance_ids), in_path


def upsert_rows(base_rows: list[dict[str, Any]], changed: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace/insert ``changed`` rows into ``base_rows`` by instance_id, preserving order."""
    by_id = {row["instance_id"]: row for row in base_rows}
    by_id.update(changed)
    return list(by_id.values())


def commit_description(instance_ids: list[str]) -> str:
    return "\n".join(f"- {instance_id}" for instance_id in sorted(instance_ids))

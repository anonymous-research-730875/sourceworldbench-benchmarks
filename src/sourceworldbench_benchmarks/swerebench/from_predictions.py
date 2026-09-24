"""Read swefficiency model-prediction patches and build prediction StateDatapoints.

A swefficiency *prediction* is one model-harness run's attempt at an instance. Its
``model_patch`` is a diff against the instance's base commit, so it becomes a
single-state augmentation of that instance's base state: the base commit with the
model's patch applied on top, rebuilt, run against the same baked-in test suite.

The prediction rows, and the per-patch facts joined onto them, are read from the
swefficiency repo on GitHub at the pinned commit :data:`REF`, through a disk cache.
Each row is joined, on ``instance_id``, against:
  - the prediction file it came from (``predictions/converted/<run>.jsonl``);
  - ``eval_reports/eval_report_<model_name_or_path>.csv`` — this patch's score and
    the timings behind it;
  - ``eval_reports/eval_report_gold.csv`` — the same instance under the expert patch.

Fields
------
Each prediction carries the keys below, recorded under
``metadata["swefficiency"]["prediction"]`` on the augmentation (``instance_id`` and
``model_patch`` are represented by the datapoint's own ``instance_id`` / ``patch``
instead). The join is sparse: a key whose value is null is omitted, so a run without
an eval report carries only the identity keys.

Identity (present on every prediction):
  - ``model_name_or_path`` — the model/config the run used.
  - ``harness`` — ``OpenHands`` / ``SWE-agent`` / ``Cursor CLI``, from the file-name
    prefix; every prediction file uses one of these three prefixes.
  - ``source_path`` — the ``predictions/converted/<run>.jsonl`` the row came from.
  - ``trajectory_length`` — present only in the deepseekv31 runs that record it.

Measured outcome of this patch (present when the run has an eval-report row):
  - ``speedup_ratio`` — the benchmark's instance score: gated speedup / gold speedup,
    where the gate drops the speedup to 1.0 unless every PASS_TO_PASS test passes;
    1.0 means "matched the expert".
  - ``lm_speedup`` — the ungated timed speedup, large even for an incorrect patch.
  - ``eval_correctness_pct`` — fraction of PASS_TO_PASS tests the patch keeps passing.

The same instance under the expert patch, and the instance itself:
  - ``gold_speedup`` — the expert patch's speedup.
  - ``gold_correctness_pct`` — the expert patch's correctness (must always be 1.0).
  - ``pre_edit_runtime`` — the unpatched workload's runtime.

Every value is read from a file, never computed. Excluded on purpose:
  - anything derivable from a present field: statistics of ``model_patch``, and the
    report's ``pred_speedup_ratio`` / ``correctness`` intermediates.
  - run-level aggregates that describe all instances rather than one row.
  - another model's numbers — join two prediction sets on ``instance_id`` instead.
  - per-test pass/fail results, which need a re-run.
  - trajectory cost, tokens, and step counts.
  - the expert patch itself and other HF-dataset fields.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sourceworldbench_benchmarks.execution import REBUILD_COMMAND_METADATA_KEY
from sourceworldbench_benchmarks.schema import BASE_ID_METADATA_KEY, StateDatapoint

REPO = "swefficiency/swefficiency"

# The one repo state read here. An exact commit, not a branch, so every run sees
# identical data: the tip of `swefficiency_base` (the repo's default branch; there is
# no `main`) as of the last time this was validated against the data.
REF = "12d32a2d6800824a7d84bdb6797b5708e7b7957f"

RAW_URL = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"

# GitHub contents API, used only to enumerate the prediction files in a directory
# (raw.githubusercontent cannot list directories). Unauthenticated; one call per run.
CONTENTS_URL = "https://api.github.com/repos/{repo}/contents/{path}?ref={ref}"

PREDICTIONS_DIR = "predictions/converted"

GOLD_REPORT = "eval_reports/eval_report_gold.csv"

# Root of the on-disk fetch cache, and the subdirectory under it holding the fetched
# swefficiency files. The enriched output is never cached — it is recomputed from
# these fetched inputs on every run.
DEFAULT_CACHE_DIR = Path("data") / "cache"
CACHE_SUBDIR = "swefficiency_patches"

HARNESS_BY_PREFIX = (
    ("oh_", "OpenHands"),
    ("sweagent_", "SWE-agent"),
    ("cursor_", "Cursor CLI"),
)


# ---------------------------------------------------------------------------
# File access: GitHub raw at REF, through a disk cache.
# ---------------------------------------------------------------------------


class Source:
    """Reads repo-relative paths from GitHub at REF, caching under ``<cache_dir>/<CACHE_SUBDIR>``."""

    def __init__(self, cache_dir: str | Path | None = DEFAULT_CACHE_DIR, verbose: bool = True) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.verbose = verbose

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr)

    def read_text(self, path: str) -> str | None:
        """Return file contents, or None if the file does not exist."""
        cached = self.cache_dir / CACHE_SUBDIR / path if self.cache_dir else None
        if cached and cached.exists():
            return cached.read_text()

        url = RAW_URL.format(repo=REPO, ref=REF, path=path)
        self._log(f"  fetching: {path}")
        try:
            with urllib.request.urlopen(url) as resp:
                text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self._log(f"  missing: {path}")
                return None
            raise
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(text)
        return text

    def read_csv(self, path: str, key: str = "instance_id") -> dict[str, dict[str, Any]]:
        """Read a CSV into {key: row}, coercing numeric cells. Empty if absent."""
        text = self.read_text(path)
        if text is None:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in csv.DictReader(io.StringIO(text)):
            out[row[key]] = {k: _coerce(v) for k, v in row.items() if k != key}
        return out


def _coerce(value: Any) -> Any:
    """Turn a CSV cell into a float/None where that is unambiguous."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return value


# ---------------------------------------------------------------------------
# Assembly.
# ---------------------------------------------------------------------------


def harness_from_filename(stem: str) -> str | None:
    for prefix, name in HARNESS_BY_PREFIX:
        if stem.startswith(prefix):
            return name
    return None


def resolve_prediction_path(source: Source, name: str) -> tuple[str, str]:
    """Accept a repo path, a bare file name, or a bare stem. Returns ``(path, stem)``."""
    if "/" in name:
        return name, Path(name).stem
    stem = name[:-6] if name.endswith(".jsonl") else name
    path = f"{PREDICTIONS_DIR}/{stem}.jsonl"
    if source.read_text(path) is None:
        raise FileNotFoundError(f"no prediction file found for {name!r} under {PREDICTIONS_DIR} at {REF[:12]}")
    return path, stem


def list_prediction_stems(source: Source) -> list[str]:
    """Return every prediction-file stem under :data:`PREDICTIONS_DIR` at :data:`REF`.

    Uses the GitHub contents API (raw.githubusercontent cannot list directories).
    Not cached — the listing is small and re-read once per run.
    """
    url = CONTENTS_URL.format(repo=REPO, path=PREDICTIONS_DIR, ref=REF)
    source._log(f"  listing: {PREDICTIONS_DIR}")
    try:
        with urllib.request.urlopen(url) as resp:
            items = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = "GitHub API rate limit — retry later or set an authenticated request" if exc.code == 403 else str(exc)
        raise RuntimeError(f"could not list {PREDICTIONS_DIR} at {REF[:12]}: {detail}") from exc
    return sorted(
        item["name"][: -len(".jsonl")]
        for item in items
        if item.get("type") == "file" and item["name"].endswith(".jsonl")
    )


class EvalReports:
    """Lazily loads the eval report matching a row's `model_name_or_path`.

    A prediction file usually holds one model name, but `oh_claude37sonnet.jsonl`
    mixes two (a bedrock `us.anthropic.*` variant and a plain one), so the lookup
    is per row rather than per file.
    """

    def __init__(self, source: Source) -> None:
        self.source = source
        self._cache: dict[str, dict[str, dict[str, Any]]] = {}

    def rows(self, model_name_or_path: str) -> dict[str, dict[str, Any]]:
        if model_name_or_path not in self._cache:
            self._cache[model_name_or_path] = self.source.read_csv(f"eval_reports/eval_report_{model_name_or_path}.csv")
        return self._cache[model_name_or_path]


_TIMESTAMP_FILE_ENTRY = re.compile(r"diff --git a/\d{4}-\d{2}-\d{2} ")


def normalize_model_patch(patch: str) -> str:
    """Repair the two conversion artifacts that make `git apply` reject a patch.

    Restores the missing final newline and drops the stray `diff --git a/<timestamp>`
    file entry.
    """
    file_entries = [s for s in re.split(r"(?m)(?=^diff --git )", patch) if s]
    kept = "".join(s for s in file_entries if not _TIMESTAMP_FILE_ENTRY.match(s))
    if kept and not kept.endswith("\n"):
        kept += "\n"
    return kept


def enrich_row(
    row: dict[str, Any],
    source_path: str,
    harness: str | None,
    gold_rows: dict[str, dict[str, Any]],
    reports: EvalReports,
) -> dict[str, Any]:
    """Join one prediction row against its eval report and the gold report.

    Returns the flat, sparse field set documented in the module docstring (null values
    omitted).
    """
    instance_id = row["instance_id"]
    model_name_or_path = row.get("model_name_or_path")
    eval_row = reports.rows(model_name_or_path).get(instance_id) or {}
    gold_row = gold_rows.get(instance_id) or {}

    out = {
        # --- identity ---
        "instance_id": instance_id,
        "model_patch": normalize_model_patch(row["model_patch"]),
        "model_name_or_path": model_name_or_path,
        "harness": harness,
        "source_path": source_path,
        "trajectory_length": row.get("trajectory_length"),
        # --- this patch's measured outcome ---
        "speedup_ratio": eval_row.get("human_speedup_ratio"),
        "lm_speedup": eval_row.get("raw_pred_speedup_ratio"),
        "eval_correctness_pct": eval_row.get("correctness_pct"),
        # --- the same instance under the gold patch, and the instance itself ---
        "gold_speedup": eval_row.get("gold_speedup_ratio") or gold_row.get("gold_speedup_ratio"),
        "gold_correctness_pct": gold_row.get("correctness_pct"),
        "pre_edit_runtime": eval_row.get("pre_edit_runtime") or gold_row.get("pre_edit_runtime"),
    }

    # Sparse output: a null carries no information the absent key does not.
    return {key: value for key, value in out.items() if value is not None}


def keep_row(row: dict[str, Any]) -> tuple[bool, str | None]:
    """Skip rows whose run failed outright, rather than emitting empty patches."""
    if row.get("status") == "error":
        return False, "status=error"
    if not normalize_model_patch(row.get("model_patch") or "").strip():
        return False, "empty model_patch"
    return True, None


# ---------------------------------------------------------------------------
# Prediction context: the shared inputs (gold report + per-model reports) plus
# per-instance iteration over one or more prediction files.
# ---------------------------------------------------------------------------


class PredictionContext:
    """Bundles the enrichment inputs for a fixed set of prediction files.

    Loads the gold report once and shares the lazily-loaded per-model reports, so
    iterating the same set of files for many instances re-reads only the (cached)
    prediction files themselves.
    """

    def __init__(self, source: Source, prediction_files: list[tuple[str, str]]) -> None:
        self.source = source
        # (repo path, stem) for each prediction file, resolved once.
        self.prediction_files = prediction_files
        self.gold_rows = source.read_csv(GOLD_REPORT)
        self.reports = EvalReports(source)

    @classmethod
    def create(
        cls,
        prediction_files: list[str],
        *,
        cache_dir: str | Path | None = DEFAULT_CACHE_DIR,
        verbose: bool = True,
        include_all: bool = False,
    ) -> "PredictionContext":
        """Resolve the selected prediction files against the source.

        ``prediction_files`` are stems, names, or repo paths. With ``include_all`` the
        full set of files under :data:`PREDICTIONS_DIR` is added too. Files that resolve
        to the same repo path are de-duplicated, so mixing explicit names with
        ``include_all`` never yields a file twice. Raises ``ValueError`` if the selection
        is empty.
        """
        source = Source(cache_dir=cache_dir, verbose=verbose)
        names = list(prediction_files)
        if include_all:
            names += list_prediction_stems(source)
        resolved: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name in names:
            path, stem = resolve_prediction_path(source, name)
            if path in seen:
                continue
            seen.add(path)
            resolved.append((path, stem))
        if not resolved:
            raise ValueError("no prediction files selected")
        return cls(source, resolved)

    def iter_predictions(self, instance_id: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield ``(source_stem, enriched_row)`` for every kept prediction of ``instance_id``.

        One entry per prediction file that carries a row for the instance, across all
        files this context was created with.
        """
        for path, stem in self.prediction_files:
            harness = harness_from_filename(stem)
            text = self.source.read_text(path)
            if text is None:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("instance_id") != instance_id:
                    continue
                ok, _ = keep_row(row)
                if not ok:
                    continue
                yield stem, enrich_row(row, path, harness, self.gold_rows, self.reports)


def _sanitize_segment(stem: str) -> str:
    """Make a prediction-file stem safe as a single ``instance_id`` segment.

    Segments are split on the ``__`` separator, so any ``__`` inside the stem is
    collapsed to a single ``_``.
    """
    return stem.replace("__", "_")


def prediction_augmentation_datapoint(
    base_datapoint: StateDatapoint,
    enriched: dict[str, Any],
    *,
    rebuild_command: str,
    source_stem: str,
) -> StateDatapoint:
    """Build a prediction state: the base state with one model's ``model_patch`` applied.

    The prediction state reuses the base state's container, command, and test_scope — it
    is the base commit with the model's patch on top, rebuilt at collect-time. It
    inherits the base state's swefficiency provenance and records the joined per-patch
    facts under ``metadata['swefficiency']['prediction']``.
    """
    # Follow the `<repo>__<base_id>__<suffix>` convention: replace the base state's
    # trailing `base` segment with the prediction suffix.
    base_stem = "__".join(base_datapoint.instance_id.split("__")[:-1])
    instance_id = f"{base_stem}__prediction_{_sanitize_segment(source_stem)}"

    base_sweff = dict((base_datapoint.metadata or {}).get("swefficiency") or {})
    # The enriched row's identity fields are represented by the datapoint itself.
    prediction = {k: v for k, v in enriched.items() if k not in ("instance_id", "model_patch")}

    metadata: dict[str, Any] = {
        BASE_ID_METADATA_KEY: base_datapoint.metadata[BASE_ID_METADATA_KEY],
        "swefficiency": {
            "dataset": base_sweff.get("dataset"),
            "instance_id": base_sweff.get("instance_id"),
            "variant": "prediction",
            "prediction": prediction,
        },
        REBUILD_COMMAND_METADATA_KEY: rebuild_command,
    }

    return StateDatapoint(
        instance_id=instance_id,
        repo=base_datapoint.repo,
        base_commit=base_datapoint.base_commit,
        patch=enriched["model_patch"],
        container=base_datapoint.container,
        command=base_datapoint.command,
        test_scope=list(base_datapoint.test_scope),
        metadata=metadata,
    )

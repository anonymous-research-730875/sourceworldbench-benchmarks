"""Typer CLI for sourceworldbench-benchmarks execution-tracer.

Three subcommands:

  sourceworldbench-benchmarks execution-tracer trace
      --in <partial.jsonl | hf-repo-id> --out-dir <traces/>
      [--instance-id ID ...] [--tracer <name>] [--repo-dir /app] [--timeout 1800]
      [--push/--no-push] [--trace-scope patch|repo]
      (default: run all tracers and merge into trace_output.json;
       --tracer <name> runs a single tracer without merging. Each row is
       traced once — its patch is applied when present, otherwise the bare
       base commit is used.)

  sourceworldbench-benchmarks execution-tracer adapt-swebench
      --dataset SWE-bench/SWE-bench_Verified [--split test]
      [--instance-ids id1,id2,...] --out <partial.jsonl>

  sourceworldbench-benchmarks execution-tracer build-samples
      --in <filled.jsonl> --traces <traces/> --out-dir <samples/>
      [--outcomes auto|datapoint] [--context-strategy smart|oracle]
      [--repo-dir /testbed] [--tasks ...]
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import typer
from pydantic import ValidationError

from sourceworldbench_benchmarks.dataset_io import (
    commit_description,
    load_existing_rows,
    push_rows,
    resolve_source,
    upsert_rows,
)
from sourceworldbench_benchmarks.execution_tracer.runner import BY_NAME as TRACER_BY_NAME
from sourceworldbench_benchmarks.execution_tracer.runner import trace_one
from sourceworldbench_benchmarks.execution_tracer.runner.script import TRACE_SCOPES, TraceScope, resolve_trace_paths
from sourceworldbench_benchmarks.schema import BaseKey, StateDatapoint, base_key

# `--run` choices for trace: the row column that overrides `command` (None = no
# override, the row runs `command` as-is).
RUN_COLUMNS = {
    "tests": None,
    "time": "command_workload",
    "memory": "command_workload_amplified",
}

logger = logging.getLogger("sourceworldbench-benchmarks execution-tracer")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Record and inspect test-execution traces.",
)


def _iter_jsonl_rows(path: Path) -> Iterable[tuple[int, dict]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logger.warning("line %d: invalid JSON (%s); skipping", line_number, exc)
                continue
            if not isinstance(payload, dict):
                logger.warning("line %d: not an object; skipping", line_number)
                continue
            yield line_number, payload


def _load_trace_artifacts(instance_dir: Path) -> dict[str, Any]:
    """Parse every ``<tracer>_output.json`` in an instance dir into ``{stem: contents}``."""
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(instance_dir.glob("*_output.json"))
    }


@app.command()
def trace(
    in_path: str = typer.Option(
        ...,
        "--in",
        help="sourceworldbench-benchmarks-style partial/filled JSONL file, or an HF dataset repo id.",
    ),
    instance_ids: list[str] = typer.Option(
        [],
        "--instance-id",
        help="Trace only specific row(s) by instance_id. Repeatable; default: all rows.",
    ),
    out_dir: Path = typer.Option(..., "--out-dir", help="Where trace JSONs land (one subdir per instance)."),
    tracer: str | None = typer.Option(
        None,
        "--tracer",
        help=(
            "Run a single tracer without merging "
            f"(one of {sorted(TRACER_BY_NAME)}). "
            "Default: run all tracers and merge into trace_output.json."
        ),
    ),
    repo_dir: str = typer.Option("/app", "--repo-dir", help="Container path to the repo root (used to scope tracing)."),
    timeout: int = typer.Option(1800, "--timeout", help="Per-state docker run timeout in seconds."),
    push: bool = typer.Option(
        False,
        "--push/--no-push",
        help=(
            "Embed each row's trace artifacts under metadata['traces'] and push the updated rows "
            "to the HF dataset (the --in repo, or the default dataset for a JSONL input)."
        ),
    ),
    run: str = typer.Option(
        "tests",
        "--run",
        help=(
            "Which command each row runs: 'tests' (default) runs `command` (the test suite), "
            "'time' runs `command_workload` (the timing workload), 'memory' runs "
            "`command_workload_amplified` (the memory workload). Rows whose selected column "
            "is empty are skipped with a warning."
        ),
    ),
    trace_scope: str = typer.Option(
        "repo",
        "--trace-scope",
        help=(
            "Which part of the repository the tracers record: `repo` (default) covers the whole "
            "repository, identically for every row; `patch` covers only the directories the row's "
            "patch touches, so it varies with the patch."
        ),
    ),
) -> None:
    """Trace every single-state row in `--in` and write JSONs into `--out-dir`.

    Each row is traced once into `<out-dir>/<instance_id>/`; the row's patch
    is applied when present, otherwise the bare base commit is used. By
    default runs every tracer (trace, walltime, memprof, cprofile), then
    merges their outputs into a single enriched `trace_output.json` — the
    only file `build-samples` needs. Pass `--tracer <name>` to run just one
    tracer and skip merging.

    With `--push` (off by default), each row's trace artifacts are read back
    from `<out-dir>/<instance_id>/`, embedded under `metadata["traces"]` keyed
    by file stem, and the updated rows are pushed to the HF dataset, mirroring
    `collect`.

    `--trace-scope` selects what the tracers record, with the same values and
    the same default as the `execute-trace` worker, so a row traced here and
    traced on the cluster yield the same function set.
    """
    from sourceworldbench_benchmarks.execution_tracer.traces import merge_trace_outputs

    if run not in RUN_COLUMNS:
        raise typer.BadParameter(f"unknown --run {run!r}; pick from {list(RUN_COLUMNS)}")
    workload_column = RUN_COLUMNS[run]

    if tracer is not None:
        if tracer not in TRACER_BY_NAME:
            raise typer.BadParameter(f"unknown tracer {tracer!r}; pick from {sorted(TRACER_BY_NAME)}")
        specs = [TRACER_BY_NAME[tracer]]
        do_merge = False
    else:
        specs = list(TRACER_BY_NAME.values())
        do_merge = True

    if trace_scope not in TRACE_SCOPES:
        raise typer.BadParameter(f"unknown --trace-scope {trace_scope!r}; pick from {list(TRACE_SCOPES)}")
    scope = cast(TraceScope, trace_scope)

    out_dir.mkdir(parents=True, exist_ok=True)

    rows, push_repo = resolve_source(in_path, instance_ids=instance_ids or None)

    if instance_ids:
        rows_by_id = {row["instance_id"]: row for row in rows}
        missing = [iid for iid in instance_ids if iid not in rows_by_id]
        if missing:
            typer.echo(f"error: instance_id(s) not found in {in_path}: {', '.join(missing)}", err=True)
            raise typer.Exit(code=2)
        rows = [rows_by_id[iid] for iid in instance_ids]

    traced_rows: dict[str, dict[str, Any]] = {}
    ok = fail = 0
    for index, payload in enumerate(rows, start=1):
        try:
            dp = StateDatapoint.model_validate(payload)
        except ValidationError as exc:
            logger.warning("row %d: schema invalid; skipping (%s)", index, exc)
            fail += 1
            continue

        if workload_column and not getattr(dp, workload_column):
            logger.warning("skipping %s: --run %s needs %s but it is None", dp.instance_id, run, workload_column)
            fail += 1
            continue

        row_ok = True
        for spec in specs:
            logger.info("tracing instance=%s tracer=%s", dp.instance_id, spec.name)
            result = trace_one(
                dp,
                tracer_spec=spec,
                out_dir=out_dir,
                timeout=timeout,
                repo_dir=repo_dir,
                trace_paths=resolve_trace_paths(scope, dp.patch),
                command_override=getattr(dp, workload_column) if workload_column else None,
            )
            if result.ok:
                logger.info("ok instance=%s tracer=%s", dp.instance_id, spec.name)
            else:
                row_ok = False
                logger.warning("fail instance=%s tracer=%s %s", dp.instance_id, spec.name, result.error)

        instance_dir = out_dir / dp.instance_id
        if do_merge:
            try:
                n = merge_trace_outputs(instance_dir)
                logger.info("merged %s (%d tests)", dp.instance_id, n)
            except FileNotFoundError as exc:
                logger.warning("skip merge %s: %s", dp.instance_id, exc)

        if push and row_ok:
            artifacts = _load_trace_artifacts(instance_dir)
            if artifacts:
                row = dp.model_dump()
                row["metadata"]["traces"] = artifacts
                traced_rows[dp.instance_id] = row

        if row_ok:
            ok += 1
            logger.info("ok instance=%s", dp.instance_id)
        else:
            fail += 1
            logger.warning("fail instance=%s", dp.instance_id)

    if not push:
        typer.echo("Skipping push (--no-push).", err=True)
    elif not traced_rows:
        typer.echo("No traces produced; dataset unchanged.", err=True)
    else:
        merged = upsert_rows(load_existing_rows(push_repo), traced_rows)
        push_rows(
            push_repo,
            merged,
            commit_message=f"Add traces to metadata ({len(traced_rows)} row(s))",
            commit_description=commit_description(list(traced_rows)),
        )
        typer.echo(f"Pushed {len(traced_rows)} traced row(s) to {push_repo}.", err=True)

    typer.echo(f"done: ok={ok} fail={fail}", err=True)
    raise typer.Exit(code=0 if fail == 0 else 1)


@app.command("adapt-swebench")
def adapt_swebench(
    dataset_name: str = typer.Option(
        "SWE-bench/SWE-bench_Verified",
        "--dataset",
        help="Hugging Face dataset reference.",
    ),
    split: str = typer.Option("test", "--split", help="Dataset split to read."),
    instance_ids: str | None = typer.Option(
        None,
        "--instance-ids",
        help="Comma-separated instance_ids to keep; default: all.",
    ),
    out_path: Path = typer.Option(..., "--out", help="Output sourceworldbench-benchmarks-style JSONL path."),
    namespace: str = typer.Option(
        "swebench",
        "--namespace",
        help="Docker namespace for the eval image (\"swebench\" → Hub image; empty → local build).",
    ),
) -> None:
    """Convert a SWE-bench Verified split into a sourceworldbench-benchmarks JSONL.

    Requires `pip install 'sourceworldbench-benchmarks[swebench]'`.
    """
    try:
        from sourceworldbench_benchmarks.execution_tracer.adapters.swebench import adapt_swebench_dataset
    except ImportError as exc:  # pragma: no cover
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    wanted: list[str] | None = None
    if instance_ids:
        wanted = [s.strip() for s in instance_ids.split(",") if s.strip()]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    ns_arg: str | None = namespace if namespace else None
    with out_path.open("w", encoding="utf-8") as handle:
        for dp in adapt_swebench_dataset(
            dataset_name, split=split, instance_ids=wanted, namespace=ns_arg
        ):
            handle.write(dp.model_dump_json() + "\n")
            count += 1
    typer.echo(f"wrote {count} rows to {out_path}", err=True)


# ── build-samples ──────────────────────────────────────────────────────────


def _resolve_provider(name: str):
    """Pick an OutcomeProvider.

    `auto`/`datapoint` → `FilledDatapointOutcomes`, which reads outcomes off
    the single-state row. `swebench-log` is reserved for a follow-up that
    persists run logs alongside the trace JSON.
    """
    from sourceworldbench_benchmarks.execution_tracer.outcomes import FilledDatapointOutcomes

    if name in ("auto", "datapoint"):
        return FilledDatapointOutcomes()
    raise typer.BadParameter(
        f"--outcomes={name!r} is not supported yet; use 'auto' or 'datapoint' "
        "(swebench-log support requires run-log persistence in `trace`)."
    )


def _locate_trace(traces_dir: Path, instance_id: str) -> Path | None:
    """Find `<traces>/<instance>/<tracer>_output.json[.gz]`. Returns the
    first match for any tracer (build-samples is tracer-agnostic).
    """
    instance_dir = traces_dir / instance_id
    if not instance_dir.is_dir():
        return None
    for entry in sorted(instance_dir.iterdir()):
        if entry.is_file() and (entry.suffix == ".json" or entry.name.endswith(".json.gz")):
            return entry
    return None


def _trace_file_paths(trace: dict) -> set[str]:
    """Collect repo-relative file paths referenced by any test's functions."""
    out: set[str] = set()
    for test in (trace.get("tests") or {}).values():
        for fn in test.get("functions") or []:
            fp = fn.get("file")
            if isinstance(fp, str) and fp:
                out.add(fp)
        # Test files themselves; the builder also wants these in the snapshot.
        for fname in (test.get("lines") or {}).keys():
            if isinstance(fname, str) and fname:
                out.add(fname)
    return out


def _is_base_row(dp: StateDatapoint) -> bool:
    """True when a row is the base state of its `(repo, base_commit)` group.

    Classified by id shape — the instance_id's suffix is the reserved `base`
    segment (`<repo>__<base-id>__base`) — not by patch presence: unlike the
    clone dataset's `is_base_state`, SWE-bench-adapted base rows deliberately
    carry a (test) patch, so `patch is None` would misclassify them.
    """
    return dp.instance_id.split("__")[2:] == ["base"]


def _fail_to_pass(base_dp: StateDatapoint | None, aug_rows: list[StateDatapoint]) -> list[str]:
    """Tests that fail on the base state and pass under some augmentation.

    The single-state analog of SWE-bench's flipped tests: the base row has
    no `flipped_tests` field, so we recompute it from the pair — base
    failures/errors that any augmented sibling turns green. Empty when there
    is no base row (no reference to flip against).
    """
    if base_dp is None or base_dp.failed_tests is None:
        return []
    base_broken = set(base_dp.failed_tests) | set(base_dp.errored_tests or [])
    passed_when_augmented: set[str] = set()
    for aug in aug_rows:
        passed_when_augmented |= set(aug.passed_tests or [])
    return sorted(base_broken & passed_when_augmented)


def _instance_meta(dp: StateDatapoint, fail_to_pass: list[str]) -> dict:
    """Bridge a single-state row → the dict `build_samples_for_trace` expects."""
    return {
        "instance_id": dp.instance_id,
        "repo": dp.repo,
        "base_commit": dp.base_commit,
        "problem_statement": "",
        "FAIL_TO_PASS": list(fail_to_pass),
    }


@app.command("build-samples")
def build_samples(
    in_path: Path = typer.Option(..., "--in", help="Filled sourceworldbench-benchmarks-style JSONL."),
    traces_dir: Path = typer.Option(..., "--traces", help="Directory produced by `trace`."),
    out_dir: Path = typer.Option(..., "--out-dir", help="Where benchmark samples land."),
    outcomes: str = typer.Option(
        "auto",
        "--outcomes",
        help="Outcome provider: auto|datapoint (swebench-log not yet supported here).",
    ),
    context_strategy: str = typer.Option(
        "smart",
        "--context-strategy",
        help="Builder context strategy: smart (default) or oracle.",
    ),
    repo_dir: str = typer.Option(
        "/testbed",
        "--repo-dir",
        help="Container path to the repo root, used to cp source files out.",
    ),
    tasks: str | None = typer.Option(
        None,
        "--tasks",
        help="Comma-separated subset of benchmark tasks; default: all known tasks.",
    ),
    timeout: int = typer.Option(
        600, "--timeout", help="Per-state `fetch_sources` docker run timeout."
    ),
) -> None:
    """Assemble benchmark samples from filled single-state rows + trace JSONs.

    Rows are grouped into base/augmented pairs by `(repo, base_commit)`; each
    group's FAIL_TO_PASS is computed from the pair (base failures that an
    augmented sibling turns green). Then for each row that has a trace:
      1. Pick the trace JSON in `<traces>/<instance>/`.
      2. Merge the row's own per-test outcomes from the chosen provider.
      3. Fetch source files referenced by the trace from the container.
      4. Call `build_samples_for_trace` (`pre` for the base row, `post` for
         augmented) and write the resulting samples.
    """
    import tempfile

    from sourceworldbench_benchmarks.execution_tracer.benchmark.builder import build_samples_for_trace
    from sourceworldbench_benchmarks.execution_tracer.benchmark.samples import TASKS, write_sample
    from sourceworldbench_benchmarks.execution_tracer.outcomes import merge_outcomes_into_trace
    from sourceworldbench_benchmarks.execution_tracer.sources import fetch_sources, materialize_to_dir

    chosen_tasks: list[str] | None = None
    if tasks is not None:
        chosen_tasks = [t.strip() for t in tasks.split(",") if t.strip()]
        unknown = [t for t in chosen_tasks if t not in TASKS]
        if unknown:
            raise typer.BadParameter(
                f"unknown task(s) {unknown!r}; pick from {TASKS}"
            )

    groups: dict[BaseKey, list[StateDatapoint]] = {}
    invalid = 0
    for line_number, payload in _iter_jsonl_rows(in_path):
        try:
            dp = StateDatapoint.model_validate(payload)
        except ValidationError as exc:
            logger.warning("line %d: schema invalid; skipping (%s)", line_number, exc)
            invalid += 1
            continue
        groups.setdefault(base_key(dp), []).append(dp)

    provider = _resolve_provider(outcomes)

    out_dir.mkdir(parents=True, exist_ok=True)
    ok = total_samples = 0
    fail = invalid
    per_task: dict[str, int] = {}

    for group_id, rows in groups.items():
        # The base state of a group is the row with no patch (the bare base
        # commit); the rest are its augmented siblings.
        base_rows = [r for r in rows if _is_base_row(r)]
        aug_rows = [r for r in rows if not _is_base_row(r)]
        if len(base_rows) > 1:
            logger.warning("group %s has %d base rows; using the first", group_id, len(base_rows))
        base_dp = base_rows[0] if base_rows else None
        fail_to_pass = _fail_to_pass(base_dp, aug_rows)

        for dp in rows:
            builder_side = "pre" if _is_base_row(dp) else "post"
            meta = _instance_meta(dp, fail_to_pass)
            row_samples = 0

            with tempfile.TemporaryDirectory(prefix="sourceworldbench_snap_") as tmp_root:
                trace_path = _locate_trace(traces_dir, dp.instance_id)
                if trace_path is None:
                    logger.info("no trace for %s; skipping", dp.instance_id)
                    fail += 1
                    continue

                try:
                    row_outcomes = provider.outcomes_for(dp)
                except ValueError as exc:
                    logger.warning(
                        "%s: provider failed: %s; skipping", dp.instance_id, exc
                    )
                    fail += 1
                    continue

                merge_outcomes_into_trace(trace_path, row_outcomes)

                trace_obj = json.loads(
                    trace_path.read_text(encoding="utf-8")
                ) if trace_path.suffix != ".gz" else None
                if trace_obj is None:
                    import gzip
                    with gzip.open(trace_path, "rt", encoding="utf-8") as handle:
                        trace_obj = json.load(handle)
                paths = _trace_file_paths(trace_obj)

                fetched = fetch_sources(dp, paths, repo_dir=repo_dir, timeout=timeout)
                snapshot_dir = Path(tmp_root) / builder_side
                materialize_to_dir(fetched, snapshot_dir)

                samples = build_samples_for_trace(
                    trace_path,
                    snapshot_dir,
                    meta,
                    tasks=chosen_tasks,
                    side=builder_side,
                    context_strategy=context_strategy,
                )
                for s in samples:
                    write_sample(s, out_dir)
                    per_task[s.task] = per_task.get(s.task, 0) + 1
                row_samples += len(samples)

            total_samples += row_samples
            if row_samples:
                ok += 1
                logger.info("ok instance=%s samples=%d", dp.instance_id, row_samples)
            else:
                fail += 1
                logger.info("no samples produced for instance=%s", dp.instance_id)

    manifest = {
        "input": str(in_path),
        "traces": str(traces_dir),
        "context_strategy": context_strategy,
        "tasks": per_task,
        "total_samples": total_samples,
        "instances_ok": ok,
        "instances_fail": fail,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    typer.echo(
        f"done: ok={ok} fail={fail} total_samples={total_samples}", err=True
    )
    raise typer.Exit(code=0 if total_samples > 0 else 1)


@app.command("merge-traces")
def merge_traces(
    traces_dir: Path = typer.Option(
        ..., "--traces", help="Root traces directory (<traces>/<instance>/)."
    ),
) -> None:
    """Merge walltime + memprof outputs into each trace_output.json in-place.

    Scans every `<instance>/` directory under `--traces`, enriches
    `trace_output.json` with per-test tracemalloc fields (from memprof) and a
    clean `wall_time_s` (from walltime), and overwrites the file. Run this once
    after collecting the tracer passes; `build-samples` then has everything
    it needs for all tasks including `peak_rss` and `hot_methods_alloc`.
    """
    from sourceworldbench_benchmarks.execution_tracer.traces import merge_trace_outputs

    ok = fail = 0
    for instance_dir in sorted(traces_dir.iterdir()):
        if not instance_dir.is_dir():
            continue
        try:
            n = merge_trace_outputs(instance_dir)
            logger.info("merged %s (%d tests)", instance_dir.name, n)
            ok += 1
        except FileNotFoundError as exc:
            logger.warning("skip %s: %s", instance_dir.name, exc)
            fail += 1

    typer.echo(f"done: ok={ok} fail={fail}", err=True)
    raise typer.Exit(code=0 if fail == 0 else 1)


@app.command()
def inspect(
    traces_dir: Path = typer.Option(
        ..., "--traces", help="Root traces directory (<traces>/<instance>/)."
    ),
    out_path: Path = typer.Option(
        None,
        "--out",
        help="Output HTML file (default: <traces>/report.html).",
    ),
    open_browser: bool = typer.Option(
        False, "--open", help="Open the report in a browser when done."
    ),
) -> None:
    """Build a self-contained HTML report for browsing collected traces.

    Walks every `<instance>/trace_output.json` under `--traces`, embeds them
    into a single HTML file, and writes it to `--out`. The report lets you
    navigate by instance, inspect per-test timing/memory/function stats, and
    step through the full call sequence.
    """
    from sourceworldbench_benchmarks.execution_tracer.inspector import write_report

    if not traces_dir.is_dir():
        raise typer.BadParameter(f"{traces_dir} is not a directory")

    output = out_path or (traces_dir / "report.html")
    n_inst, n_tests, warnings = write_report(traces_dir, output)
    for warning in warnings:
        logger.warning(warning)
    if n_inst == 0:
        typer.echo(f"error: no trace_output.json files found under {traces_dir}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"wrote {output} — {n_inst} instance(s), {n_tests} test(s)",
        err=True,
    )
    if open_browser:
        import webbrowser

        webbrowser.open(output.resolve().as_uri())


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app()


if __name__ == "__main__":  # pragma: no cover
    main()

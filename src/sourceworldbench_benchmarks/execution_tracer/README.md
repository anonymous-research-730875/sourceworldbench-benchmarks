# Execution tracer

Runtime tracing for [`sourceworldbench-benchmarks`](https://github.com/anonymous-research-730875/sourceworldbench-benchmarks)
`StateDatapoint`s. Each row is one repository state — a base commit with an
optional patch applied — and is traced once: the tracer records what each
test executed, merges per-test outcomes, and emits benchmark samples for LLM
evaluation. To compare a base state against an augmented one, trace the two
rows that share a `(repo, base_commit)`.

`sourceworldbench-benchmarks` is the source of truth for repository state, patches,
and Docker invocation — this package layers tracing on top via bind-mounted
bootstrap files. There is **no** per-instance Dockerfile here; the tracer
runs inside whatever image the row's `container` field points at.

## Layout

```
sourceworldbench_benchmarks/execution_tracer/
├── bootstrap/                 # sitecustomize.py + sourceworldbench_tracer_plugin.py
│                              #   bind-mounted into the container at /sourceworldbench-tracing
├── tracers/                   # in-container instrumentation, one file each
│   ├── injectable_tracer.py   # sys.settrace; per-test function + line events
│   ├── memory_test_profiler.py
│   ├── cprofile_test_profiler.py
│   └── test_timer.py
├── runner/                    # Docker-side orchestration (StateDatapoint-native)
│   ├── runner.py              # trace_one(dp, …) → TraceResult
│   ├── docker.py              # wraps sourceworldbench_benchmarks.docker_runner.run_state
│   ├── script.py              # builds the in-container shell script
│   └── spec.py                # TracerSpec registry (BY_NAME)
├── adapters/
│   └── swebench.py            # SWE-bench Verified row → base/augmented StateDatapoint pair
├── legacy_swebench_harness/   # legacy SWE-bench harness entrypoints
├── scripts/                   # batch, evaluation, report, and HF scripts
├── outcomes/                  # Test outcome providers + trace merge
│   ├── from_datapoint.py      # reads the row's passed/failed/skipped/errored_tests
│   ├── from_swebench_log.py   # parses swebench eval log (post-trace)
│   └── merge.py               # writes outcome onto trace["tests"][nodeid]
├── sources.py                 # on-demand source-file fetcher (cp out of container)
├── benchmark/
│   ├── builder.py             # trace JSON + source snapshot → BenchmarkSample
│   ├── samples.py             # BenchmarkSample dataclass + on-disk layout
│   └── scoring.py             # benchmark scoring helpers
└── cli.py                     # Typer app: trace / adapt-swebench / build-samples / merge-traces / inspect
```

## Install

```bash
# From the sourceworldbench-benchmarks checkout
uv sync --extra swebench
```

Drop `--extra swebench` if you only need to trace pre-built `StateDatapoint`
JSONLs (no Hugging Face dataset access).

Additional extras are available for workflows that use the legacy harness or
model-evaluation scripts:

```bash
uv sync --extra swebench-harness
uv sync --extra eval
```

## CLI

```bash
uv run sourceworldbench-benchmarks execution-tracer --help
```

### `adapt-swebench` — SWE-bench Verified → sourceworldbench-benchmarks JSONL

```bash
uv run sourceworldbench-benchmarks execution-tracer adapt-swebench \
    --dataset SWE-bench/SWE-bench_Verified \
    --instance-ids astropy__astropy-12907 \
    --out partial.jsonl
```

Writes **two** rows per instance, sharing one `(repo, base_commit)`:

- `<id>__base` — the base state, `patch` = the row's `test_patch` (tests present, fix
  absent).
- `<id>__gold` — the augmented state, `patch` = `test_patch` + the gold patch
  (tests + fix).

Both share `container` (the Docker Hub-published eval image,
`swebench/sweb.eval.x86_64.<id_with_1776>:latest`) and `command` (activates the
`testbed` conda env before running the test body swebench framed between
`>>>>> Start Test Output` / `>>>>> End Test Output`). The base side intentionally
carries the test patch despite the single-state "base = no patch" convention, so
the FAIL_TO_PASS tests exist to fail on base and pass on `__gold`.

Pass `--namespace ""` to skip the Hub-namespace rewrite if you've built the
image locally with swebench.

### `trace` — collect tracer output

```bash
uv run sourceworldbench-benchmarks execution-tracer trace \
    --in partial.jsonl --out-dir traces/ \
    --repo-dir /app --timeout 1800
```

Traces each row once into `<traces>/<instance>/trace_output.json[.gz]`, with
`/sourceworldbench-tracing/` (bootstrap + tracer) bind-mounted in and pytest auto-loading
`sourceworldbench_tracer_plugin`. The row's patch is applied when present, otherwise the bare
base commit is used.

`--repo-dir` is the repo root **inside the container** (read it off the row's
`command` — `/app` for sourceworldbench-benchmarks images, `/testbed` for SWE-bench); the
tracer scopes instrumentation to files under it.

By default every tracer runs (its own container pass) and their outputs are
merged into a single enriched `trace_output.json`. Pass `--tracer <name>` to run
just one and skip merging — `BY_NAME` in `runner/spec.py`: `trace` (line +
function), `memprof`, `cprofile`, `walltime`.

### `build-samples` — traces → benchmark samples

```bash
uv run sourceworldbench-benchmarks execution-tracer build-samples \
    --in filled.jsonl --traces traces/ --out-dir samples/ \
    --outcomes datapoint \
    --context-strategy smart \
    --repo-dir /app
```

Groups rows into base/augmented pairs by `(repo, base_commit)` and computes each
group's FAIL_TO_PASS (base failures that an augmented sibling turns green). Then
per row that has a trace:

1. Locate `<traces>/<instance>/trace_output.json[.gz]`.
2. Resolve an outcome provider (`datapoint` reads the row's own
   `passed/failed/skipped/errored_tests`). Requires a **filled** JSONL.
3. Merge per-test outcomes onto `trace["tests"][nodeid]["outcome"]`.
4. Walk the trace for referenced source files; `cp` them out of the
   container into a tempdir snapshot via `sources.fetch_sources` (uses the
   same patched container the trace ran in).
5. Hand the trace JSON + snapshot to `benchmark.builder.build_samples_for_trace`
   (`pre` for the base row, `post` for augmented) and write each
   `BenchmarkSample` under `<out-dir>/<task>/`.

`--outcomes swebench-log` is reserved for a follow-up that persists run
logs alongside trace JSONs; SWE-bench rows are therefore trace/inspect-only
for now (`build-samples` is native-JSONL only).

### `inspect` — traces → interactive HTML report

```bash
uv run sourceworldbench-benchmarks execution-tracer inspect --traces traces/ [--out report.html] [--open]
```

Walks every `<traces>/<instance>/trace_output.json`, embeds them into a single
self-contained HTML file (default `<traces>/report.html`), and opens it in a
browser with `--open`. The report lets you navigate by instance, inspect per-test
timing/memory/function stats, and step through the full call sequence.

## Apple Silicon

SWE-bench images are published for `linux/amd64`. Export
`DOCKER_DEFAULT_PLATFORM=linux/amd64` before any `docker pull` or CLI
invocation; Docker Desktop's QEMU emulation handles the rest.

## Tests

```bash
uv run pytest tests/execution_tracer -q
```

The whole suite is Docker-free: every container interaction is monkeypatched
at the `sourceworldbench_benchmarks.docker_runner.run_state` seam. Legacy harness tests
have been moved under `tests/execution_tracer/legacy/` and are excluded from
default collection (require `uv sync --extra swebench-harness` to run).

## Tasks the benchmark evaluates

Each sample asks the model to predict, without executing the code, the
runtime behaviour of a single test:

| Sub-task            | Prediction                                                        | Metric                        |
|---------------------|-------------------------------------------------------------------|-------------------------------|
| `outcome`           | `passed` / `failed` (AssertionError) / `error` + exception + line | P/R/F1, exception exact-match |
| `peak_bytes`        | Peak memory above entry baseline, bytes                           | log10 linear fit + log10 MAE  |
| `wall_ms`           | Total test wall-clock time, milliseconds                          | log10 linear fit + log10 MAE  |
| `hot_methods_time`  | Top-20 in-project functions by exclusive wall time                | NDCG@5 + Recall@5             |
| `hot_methods_alloc` | Top-20 in-project functions by exclusive allocation               | NDCG@5 + Recall@5             |
| `hot_lines_time`    | Top-20 `path/file.py:line` strings by wall time                   | NDCG@5 + Recall@5             |
| `hot_lines_alloc`   | Top-20 `path/file.py:line` strings by allocation                  | NDCG@5 + Recall@5             |

Per-level measurement strategy (one pass per task so attached
instrumentation doesn't inflate the very numbers it measures):

|            | **Test-level**                                                                           | **Method-level**                                                               | **Line-level**                                                      |
|------------|------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------|---------------------------------------------------------------------|
| **Time**   | `walltime` pass: `perf_counter`, no profiler                                             | `cprofile` pass: cProfile exclusive self-time per qualified function name      | `trace` pass: deltas between consecutive `sys.settrace` line events |
| **Memory** | `memprof` pass: max of peak tracemalloc rise, peak RSS rise, and largest per-method peak | `memprof` pass: max of summed per-call tracemalloc peaks and summed RSS deltas | `trace` pass: per-line tracemalloc + RSS deltas between line events |

`memprof` calls `tracemalloc.reset_peak()` on every project-frame entry so
per-call exclusive peaks are accurate, ratcheting the test-level high-water
mark before each reset.

## Dependencies

- `sourceworldbench-benchmarks` — `StateDatapoint` schema + `docker_runner`
- `typer` — CLI
- `swebench` (optional, `--extra swebench`) — dataset loader + image spec
  resolution; not needed when consuming pre-built `StateDatapoint` JSONLs
- `docker` (optional, `--extra swebench-harness`) — Python Docker SDK for the
  legacy SWE-bench harness
- `openai` and `anthropic` (optional, `--extra eval`) — model-evaluation scripts

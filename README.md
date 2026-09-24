# sourceworldbench-benchmarks

Benchmark data tooling for the **SourceWorldBench** project.

`sourceworldbench-benchmarks` fills benchmark rows by running one repository state's test
command in a container. The container produces runner output under `/results/`;
a registered parser turns that output into test statuses; the collector writes the
filled JSONL row with that state's own outcomes.

Each row is a single repository state (`base_commit`, optionally plus a `patch`).
**OUTDATED:** Pairwise / cross-state comparison (flipped tests, the clone-detection dataset) is
derived later from two filled rows by the standalone script
[`scripts/build_clone_dataset.py`](scripts/build_clone_dataset.py) — not here. **OUTDATED:**

For the full row lifecycle and contracts, see
[`docs/PIPELINE.md`](docs/PIPELINE.md).

## Install

```bash
uv sync
```

## GitHub token

Golden-commit identification for SWE-rebench datapoints
([`src/sourceworldbench_benchmarks/swerebench/golden_commit.py`](src/sourceworldbench_benchmarks/swerebench/golden_commit.py))
verifies each candidate against the GitHub API. Unauthenticated requests are
capped at ~60/hour, which is far too low for bulk identification; a token raises
the limit to ~5,000/hour.

**Most runs need no token.** Golden commits for the verified part of SWE-rebench
are already precomputed and committed to
[`data/cache/rebench_golden_commits.jsonl`](data/cache/rebench_golden_commits.jsonl);
identification returns a cache hit for those instances and never touches GitHub.
A token is only needed when identifying **new** datapoints not in the cache, or
when recomputing without the cache (`cache_path=None`).

If you do need one, copy the template and paste your token in:

```bash
cp .env.template .env
# then edit .env and set GITHUB_TOKEN=<your token>
```

`.env` is git-ignored ([`.env.template`](.env.template) is committed as the
reference) and is loaded automatically — no `export` needed. `GITHUB_TOKEN` is
checked first, then `GH_TOKEN`; a variable already set in your real environment
always wins over `.env`. For public repositories the token needs no special
scopes. Create one at <https://github.com/settings/tokens>.

## CLI

```bash
uv run sourceworldbench-benchmarks --help
```

Top-level commands:

- `create-partial` writes a partial StateDatapoint row from metadata and a patch file.
- `collect` fills partial StateDatapoint rows (single state: one container).
- `build-image` builds the Docker image for one benchmark instance.
- `augment` creates augmentation patches from target repositories.
- `hf` manages the Hugging Face dataset used to publish benchmark rows.

## Pipeline

Each example is a JSONL row filled in two phases.

**1. Prepare a partial row.** Either write JSONL by hand or use `create-partial` after
generating a patch with `augment`:

```bash
uv run sourceworldbench-benchmarks augment ruff --image sourceworldbench/hvac__09902dea:dev --scope hvac --out ruff__format.diff
uv run sourceworldbench-benchmarks create-partial \
  --base-commit <full-sha> \
  --container <digest-pinned-image> \
  --patch ruff__format.diff
```

Required: `--base-commit`, `--container`, `--patch`.

Defaults for everything else:

- `instance_id` — `<base-from-container>__<patch-stem>__<timestamp>` (override with `--instance-id`)
- `repo` — read from `environments/<base>/Dockerfile` (override with `--repo`)
- `command` — `DEFAULT_RUN_TESTS_COMMAND_REGISTRY` for the state's `(repo, base_commit)` (override with `--command`)
- `out` — `<patch-dir>/<instance-id>.partial.jsonl` (override with `--out`)

**2. Run `sourceworldbench-benchmarks collect`.**

```bash
uv run sourceworldbench-benchmarks collect --in partial.jsonl --out filled.jsonl
```

For each row, the tool:

- Runs the container once (`git apply`'ing `patch` first when present) and reads
  whatever lands in `/results/`.
- Invokes the parser registered for the row's `(repo, base_commit)`
  (`src/sourceworldbench_benchmarks/parsers/`) to turn the `/results/` directory into a
  `ParsedReport` (per-test outcomes plus discovery errors outside individual
  testcases).
- Fills `passed_tests`, `failed_tests`, `skipped_tests`, `errored_tests`, and
  `discovery_errors` (module-level collection failures reported outside any
  testcase, kept separate from per-test `errored_tests`).

Resume (skip `instance_id`s already in the output), pass-through (rows with all
five outcome fields already set), and failure dirs are all handled per row.
Failures land in `<out>.failures/<instance_id>/` with stdout, stderr, and a reason.

## Build an image

```bash
uv run sourceworldbench-benchmarks build-image --instance-id <id> [--push]
```

Reads `environments/<id>/Dockerfile`. With `--push`, pushes to
`xxx-docker.pkg.dev/xxx/ml/sourceworldbench/<id>:<tag>` and prints the
digest-pinned reference.

For Dockerfiles that clone private repositories, forward your SSH agent:

```bash
uv run sourceworldbench-benchmarks build-image --instance-id <id> --ssh default
```

## Build the Kubernetes runner image

Kubernetes collect runs need a runner image containing this CLI at
`/opt/sourceworldbench-runner/bin/sourceworldbench-benchmarks`.

```bash
uv run sourceworldbench-benchmarks k8s build --push
```

The command prints `image=<ref>`. When pushed and inspectable, `<ref>` is
digest-pinned and can be passed directly to `k8s collect --runner-image`.
If you use the default `--push` target, `k8s collect` also defaults to the
matching `sourceworldbench-runner:latest` tag.

Kubernetes collect runs one Kueue-managed worker Job per row and stores run
state in a GCS bucket. The lifecycle is `k8s collect` → `k8s status` →
`k8s download` (failed rows land in `<out>.failures/`). See
[`docs/K8S.md`](docs/K8S.md) for the infra requirements and failure modes.

## Hugging Face dataset

Benchmark rows are stored in
the [anonymous-research-730875/sourceworldbench-benchmarks-poc](https://huggingface.co/datasets/anonymous-research-730875/sourceworldbench-benchmarks-poc)
dataset.

Append rows from a local JSONL file:

```bash
uv run sourceworldbench-benchmarks hf append anonymous-research-730875/sourceworldbench-benchmarks-poc filled.jsonl
```

By default, existing `instance_id`s are skipped. Use `--override` to replace
colliding rows, `--allow-partial` to permit rows with missing test-outcome fields.

List or remove rows:

```bash
uv run sourceworldbench-benchmarks hf list anonymous-research-730875/sourceworldbench-benchmarks-poc
uv run sourceworldbench-benchmarks hf remove anonymous-research-730875/sourceworldbench-benchmarks-poc --instance-id <id>
```

## Execution tracer

Runtime trace collection lives under the `sourceworldbench-benchmarks execution-tracer`
command group. It traces each single-state row once — the row's patch is
applied when present, otherwise the bare base commit is used. By default every
tracer runs (execution trace, wall-time, memory, cProfile) and their outputs
merge into a single per-test `trace_output.json` per instance; a second pass
turns those traces into benchmark samples.

Trace an existing partial/filled JSONL and build samples:

```bash
# Optional: convert a SWE-bench Verified split into a sourceworldbench-benchmarks JSONL
# (emits a base + `__gold` row per instance).
uv run sourceworldbench-benchmarks execution-tracer adapt-swebench \
    --instance-ids astropy__astropy-12907 --out data/examples/partial.jsonl

# 1. Collect traces. Runs all tracers and merges them into a single
#    trace_output.json per instance. Pass --tracer <name> to run a single
#    tracer without merging instead. --repo-dir is the in-container repo root
#    (read it off the row's command: /app for sourceworldbench-benchmarks, /testbed for SWE-bench).
uv run sourceworldbench-benchmarks execution-tracer trace \
    --in data/examples/partial.jsonl --out-dir data/traces/ --repo-dir /app

# 2. Merge outcomes, fetch sources on demand, write benchmark samples.
#    Needs a filled JSONL (rows with outcomes); pairs base/augmented rows by
#    base stem to compute FAIL_TO_PASS.
uv run sourceworldbench-benchmarks execution-tracer build-samples \
    --in filled.jsonl --traces data/traces/ --out-dir samples/ \
    --outcomes datapoint --repo-dir /app
```

See `src/sourceworldbench_benchmarks/execution_tracer/README.md` for the trace JSON schema, sample format, and per-task ground truth.

## Trace inspector

Generate a self-contained HTML report for browsing collected traces:

```bash
uv run sourceworldbench-benchmarks execution-tracer inspect \
    --traces data/traces/ [--out report.html] [--open]
```

The report (default `data/traces/report.html`) embeds all trace data and lets
you navigate by instance, inspect per-test timing, memory, and function-level
stats, and step through the full call sequence.

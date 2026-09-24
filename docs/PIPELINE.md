# Pipeline overview

`sourceworldbench-benchmarks` is a small data-collection utility for SourceWorldBench benchmark rows. It takes a partially filled JSONL row describing **one repository state**, runs that state's test command in a container, parses the runner output, and writes a filled row carrying that state's own test outcomes.

The project is intentionally narrow: it does not discover examples, clone repos, checkout commits, adapt upstream datasets, or decide which tests are important. It only makes one already-prepared row executable and reproducible once the row metadata and container image already exist.

Pairwise / cross-state work (comparing a base state against an augmented one, deriving flipped/non-flipped tests, the clone-detection dataset) is **out of scope here** — it lives in the standalone script [`scripts/build_clone_dataset.py`](../scripts/build_clone_dataset.py), which pairs each base state with its augmentations and derives a clone-detection row from two already-filled single-state rows without re-running any container. That script's module docstring documents the pairing, diff, and projection.

---

## 1. Data model

The schema lives in [`src/sourceworldbench_benchmarks/schema.py`](../src/sourceworldbench_benchmarks/schema.py). One `StateDatapoint` describes one repository state evaluated in one container.

| Field | Role |
|---|---|
| `instance_id` | Stable, filesystem-safe identifier. By convention a base state (no patch) is `<repo>__<base-id>__base` (e.g. `metricflow__a6ac795f__base`); an augmented state appends an augmentation suffix instead (e.g. `metricflow__a6ac795f__ruff__format`). `<base-id>` is the `base_commit`'s short SHA when the commit is known, or a `none_<timestamp>` placeholder when it is not — it is an opaque token, never parsed as a commit. The schema enforces this `__`-separated shape, and it names the `environments/` dir. |
| `repo` | Repository short name, usually `<owner>/<repo>`. |
| `base_commit` | Commit checked out inside the container image. |
| `patch` | Unified diff `git apply`'d on top of `base_commit` before the command, or `null` for the bare base state. |
| `container` | Full image reference. |
| `command` | Shell snippet run inside the container after the patch (if any) is applied. |
| `passed_tests`, `failed_tests`, `skipped_tests`, `errored_tests` | Observed testcase identifiers partitioned by status in this state. |
| `discovery_errors` | Errors reported outside individual testcases (e.g. a module that failed pytest collection). Not test outcomes. |

The five outcome lists start as `null` in a partial row. After `sourceworldbench-benchmarks collect`, they are concrete lists. An empty list means the state was measured and nothing landed in that bucket.

Two concepts are easy to conflate and are kept in separate fields:

- **per-test errors** (`errored_tests`, status `ERROR`) — real setup/teardown/fixture failures of an observed testcase.
- **discovery errors** (`discovery_errors`) — module-level collection failures reported outside any testcase. When a module fails discovery, the tests inside it vanish from all outcome lists for that state.

Invariant on filled rows: every entry in `passed_/failed_/skipped_/errored_tests` is an observed testcase identifier. Discovery failures are surfaced only in `discovery_errors`.

---

## 2. StateDatapoint lifecycle

```
partial JSONL row
      |
      v
sourceworldbench-benchmarks collect
      |
      +-- one run: base_commit (+ patch, if present) -> /results
      |
      v
registered parser(instance_id) -> ParsedReport
      |
      +-- success -> append filled row to output JSONL
      |
      +-- failure -> write <out>.failures/<instance_id>/
```

The collector appends rows incrementally. On startup it scans the output JSONL for existing `instance_id`s and skips those rows, so interrupted runs can be resumed by running the same command again.

Already-filled rows in the input are passed through unchanged (a row is filled when all five outcome fields are non-null). Duplicate `instance_id`s within the same input are skipped after the first occurrence.

---

## 3. Container contract

The collector assumes the image already knows how to run the target project. The contract is deliberately small:

| Requirement | Why it matters |
|---|---|
| The repository is checked out at `base_commit`. | The collector only applies a diff; it does not clone or checkout commits. |
| The image `WORKDIR` is the repository root. | `git apply` runs before `command`, so patch paths must resolve from the current directory. |
| `/bin/sh` and `git` are available. | The collector runs `/bin/sh -c "<patch setup>; <command>"` and applies the patch with `git apply`. |
| The command writes runner output under `/results/`. | `/results` is bind-mounted to a temporary host directory that the parser reads. |
| The output format matches the registered parser. | The collector has no implicit parser fallback. |

The command may exit non-zero. Test commands often do that when tests fail, and that is not itself a row failure. The parser output is the source of truth for test status. Patch application failure is special: `git apply` exits through a sentinel code and the row is marked `patch_apply`.

The current code does not enforce a single result-file format. A row can write JUnit XML, JSON, or another runner artifact into `/results`, as long as the registered parser can normalize it into `TestResult` objects.

---

## 4. Patch behavior

The state gets a single `docker run --rm` invocation.

| State | Wrapper |
|---|---|
| `patch is None` (bare base) | `command`. |
| `patch` present | `git apply /work/patch.diff`, then `command`. |

The patch file is created in a temporary host directory and mounted read-only into the container under `/work/`. The host-side temporary directory also contains the `/results` bind mount and is removed after the row finishes.

The wrapping is built by `execution.wrap_command`; the mount path comes from `docker_runner.CONTAINER_PATCH_PATH`, the single source of truth shared between the two.

---

## 5. Parser contract

Parsers live under [`src/sourceworldbench_benchmarks/parsers/`](../src/sourceworldbench_benchmarks/parsers/). The registry in [`registry.py`](../src/sourceworldbench_benchmarks/parsers/registry.py) maps the **base** `(repo, base_commit)` key to a parser function. `get_parser(repo, base_commit)` looks up that key directly off the row: augmentations carry the same `(repo, base_commit)` as their base and reuse its test command and so its output format, meaning every augmentation shares the base's parser and needs no entry of its own.

```python
TestParser = Callable[[Path], ParsedReport]
```

A parser receives the host-side results directory and returns a `ParsedReport`:

| Value | Meaning |
|---|---|
| `ParsedReport.outcomes` | List of `TestResult(name, status)`. Each entry must correspond to an observed testcase. |
| `ParsedReport.discovery_errors` | Sorted, deduplicated list of errors reported outside individual testcases. |
| `TestResult.name` | Stable test identifier. Pytest-style node ids are preferred when available. |
| `TestResult.status` | One of `PASSED`, `FAILED`, `SKIPPED`, `ERROR`. `ERROR` is only for real per-test errors; discovery failures go to `discovery_errors`. |

Parser failures should use the report error types from [`report.py`](../src/sourceworldbench_benchmarks/report.py):

| Error | Use when |
|---|---|
| `MissingReportError` | The expected runner output is absent. |
| `MalformedReportError` | Output exists but cannot be parsed or is missing required fields. |
| `EmptyReportError` | Output is well-formed but contains no tests. |
| `ConflictingReportError` | The same testcase appears with conflicting statuses across reports. |

Every new **base** state must be registered explicitly (its augmentations inherit the entry). This is intentional: silent parser defaults can turn a bad row into misleading benchmark data.

---

## 6. Failure artifacts

Failures are written next to the output JSONL:

```
filled.jsonl.failures/<instance_id>/
├── reason.txt
├── partial.json
├── error.txt
├── stdout
└── stderr
```

`stdout`/`stderr` hold the container's output and are written only when a container run actually happened (a `schema_invalid` row has neither).

Current failure reasons:

| Reason | Source |
|---|---|
| `schema_invalid` | Input row is not valid JSON or does not match `StateDatapoint`. |
| `patch_apply` | `git apply` failed inside the container. |
| `timeout` | Docker invocation exceeded the configured timeout. |
| `missing_report` | Parser could not find expected output in `/results`. |
| `malformed_report` | Parser found output but could not parse it. |
| `empty_report` | Parser found a valid report with no tests. |
| `conflicting_report` | Parser found the same testcase with conflicting statuses across reports. |
| `docker_error` | Docker itself could not be invoked or returned a build-time error. |

The top-level collect summary counts successes, skips, and failures. The CLI exits non-zero if any row failed.

---

## 7. Image helper

`build-image` is a convenience wrapper around local `docker buildx build`. It expects image definitions under:

```
environments/<instance_id>/Dockerfile
```

Local builds are tagged as:

```
sourceworldbench/<instance_id>:dev
```

With `--push`, images are pushed to the configured Google Artifact Registry prefix and the command prints a digest-pinned reference when `docker buildx imagetools inspect` can resolve one.

Dockerfiles that clone private repositories should use BuildKit SSH mounts. The image helper forwards this through `--ssh`:

```bash
uv run sourceworldbench-benchmarks build-image --instance-id <id> --ssh default
```

The image helper does not define the benchmark contract by itself. The Dockerfile still needs to produce an image satisfying the container contract above.

---

## 8. Adding a new row

1. Create or identify a container image checked out at `base_commit`.
2. Make sure the image can apply a patch from its `WORKDIR`.
3. Write a command that runs the intended tests and leaves parser-readable output under `/results/`.
4. Add a parser if the output format is new.
5. Register the **base** `(repo, base_commit)` key in `parsers/registry.py` — augmentations of that base resolve to the same entry.
6. Add a partial JSONL row with metadata, optional patch, container, and command.
7. Run:

```bash
uv run sourceworldbench-benchmarks collect --in partial.jsonl --out filled.jsonl
```

Instance ids follow `<repo>__<base-id>__base` for a base state, with an augmentation suffix in place of `base` for an augmented state (e.g. `…__ruff__format`); `<base-id>` is the `base_commit`'s short SHA when known, else a `none_<timestamp>` placeholder (an opaque token, never parsed as a commit). The schema validates this `__`-separated, filesystem-safe shape on every row. The base state and each augmentation are still separate rows, but they share one registry entry keyed by `(repo, base_commit)` — every row carries that key directly — so registering the base covers all of its augmentations.

For quick smoke tests, keep the selected test command small and inspect both the filled row and any failure directory before scaling up.

---

## 9. Current boundaries

This is still an early-stage repository. A few constraints are explicit rather than hidden:

| Boundary | Current behavior |
|---|---|
| Parallelism | Local `--workers` exists but only `1` is implemented. Kubernetes runs (`k8s collect`) parallelize via Kueue queue quota, not a CLI flag. |
| Source adapters | The repo does not convert upstream datasets into partial rows automatically. |
| Parser selection | Parsers are keyed by the base `(repo, base_commit)`; augmentations resolve to their base. Not keyed by output format or repository alone. |
| Result caching | Resumability is based on existing output rows, not on cached per-state reports. |
| Provenance | Filled rows do not yet record collector version, host, timestamp, or image digest unless encoded by the input row. |
| Cross-state / pairwise | Out of scope here; derived later from two filled rows by `scripts/build_clone_dataset.py`. |

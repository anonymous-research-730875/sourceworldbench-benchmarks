"""SWE-bench Verified → sourceworldbench-benchmarks single-state Datapoint adapter.

A one-shot conversion: feed a Verified row in, get the *pair* of
`StateDatapoint`s out — a `base` side (tests present, fix absent) and an
`augmented` side (tests + fix). The output JSONL is a first-class
sourceworldbench-benchmarks artifact — the rest of the tracer (runner, CLI, outcome
providers) treats SWE-bench-origin and hand-written rows identically.

Single-state caveat (the test_patch gap): sourceworldbench-benchmarks' single-state
schema has no `test_patch` field, and its convention is that a `base`
state carries no patch. SWE-bench, however, needs the *test* patch on
both sides so the FAIL_TO_PASS tests exist to fail on the base side and
pass on the augmented side. We therefore deviate deliberately: the base
row carries `test_patch` as its `patch` (but keeps the reserved `base`
suffix segment, `<swebench_id>__base`, so it is still classified as the
base side by id shape rather than by patch presence), while the augmented
row carries `test_patch` + the gold patch. This is a pragmatic mapping
that keeps fail→pass observable; confirm with the project owner before
relying on SWE-bench-derived samples.

`swebench` is imported lazily so this module can be referenced from
modules loaded without the `[swebench]` extra; calling any function
here without the extra installed raises a clear error.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from sourceworldbench_benchmarks.schema import StateDatapoint

_SWEBENCH_INSTALL_HINT = (
    "swebench is required for SWE-bench adapter use. "
    "Install with: pip install 'sourceworldbench-benchmarks[swebench]'"
)


def _import_swebench():
    try:
        from swebench.harness import constants as _const
        from swebench.harness.test_spec.test_spec import make_test_spec
    except ImportError as exc:  # pragma: no cover - tested separately via monkeypatch
        raise ImportError(_SWEBENCH_INSTALL_HINT) from exc
    return _const, make_test_spec


def _extract_test_command(eval_script: str, start_marker: str, end_marker: str) -> str:
    """Pull the test invocation out of swebench's `eval_script`.

    swebench frames the test command between literal sentinel lines
    `: '>>>>> Start Test Output'` and `: '>>>>> End Test Output'`. Anything
    before is environment + patch setup; anything after is cleanup. We
    keep the body between markers verbatim — typically one or two lines.
    """
    lines = eval_script.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if start_marker in ln)
        end = next(i for i, ln in enumerate(lines) if i > start and end_marker in ln)
    except StopIteration as exc:
        raise ValueError(
            f"swebench eval_script did not contain expected markers "
            f"({start_marker!r} / {end_marker!r})"
        ) from exc
    body = "\n".join(lines[start + 1 : end]).strip()
    if not body:
        raise ValueError("swebench eval_script: test command body is empty")
    return body


def _instance_image_ref(spec) -> str:
    """Best-effort: pull the canonical eval image ref off a swebench TestSpec."""
    for attr in ("instance_image_key", "instance_image_tag"):
        value = getattr(spec, attr, None)
        if value:
            return value
    raise AttributeError("TestSpec has no instance_image_key / instance_image_tag")


def _concat_patches(*patches: str | None) -> str | None:
    """Concatenate unified diffs into one `git apply`-able blob.

    Empty/None patches are dropped; each kept patch is newline-terminated
    so the diffs don't run together. Returns `None` when nothing is left.
    """
    parts = [p for p in patches if p]
    if not parts:
        return None
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts)


def _test_scope_from_patch(test_patch: str | None) -> list[str]:
    if not test_patch:
        return ["tests/"]
    paths: set[str] = set()
    for line in test_patch.splitlines():
        if line.startswith(("--- a/", "+++ b/")):
            path = line[6:].strip()
            if path != "/dev/null":
                paths.add(path)
    return sorted(paths) or ["tests/"]


def adapt_swebench_row(
    row: dict, *, namespace: str | None = "swebench"
) -> tuple[StateDatapoint, StateDatapoint]:
    """Convert one SWE-bench Verified row into a (base, augmented) state pair.

    Both states share `repo`, `base_commit`, `container`, and `command`.
    `container` is the canonical swebench eval image ref; `command` is the
    body between swebench's Start/End Test Output markers, prefixed with the
    conda activation the eval script assumes.

    The pair shares one `(repo, base_commit)` key:

      * base — instance_id `<swebench_id>__base`, `patch` = the row's `test_patch`
        (tests present, fix absent → FAIL_TO_PASS tests fail).
      * augmented — instance_id `<swebench_id>__gold`, `patch` = test_patch +
        gold patch (tests + fix → those tests pass).

    See the module docstring for why the base side intentionally carries a
    patch despite the single-state "base = no patch" convention.

    `namespace` controls the image form: with `"swebench"` (default) you get
    the Docker Hub-published ref (`swebench/sweb.eval.x86_64.<inst>:latest`);
    pass `None` for the local-build form (`sweb.eval.x86_64.<inst>:latest`).

    Outcome list fields are left as `None`; outcomes for swebench rows are
    produced post-run by `outcomes.from_swebench_log` rather than baked in.
    """
    const, make_test_spec = _import_swebench()
    spec = make_test_spec(row, namespace=namespace)
    test_command = _extract_test_command(
        spec.eval_script, const.START_TEST_OUTPUT, const.END_TEST_OUTPUT
    )
    # SWE-bench eval images ship a `testbed` conda env that the eval
    # script activates before running tests. The body between the
    # Start/End markers assumes that env is active, so we mirror it
    # here when wrapping the command for sourceworldbench-benchmarks.
    # The eval container runs commands under `/bin/sh`, where `source`
    # is not a builtin; use POSIX `.` to load conda's profile.
    command = (
        ". /opt/miniconda3/etc/profile.d/conda.sh && "
        "conda activate testbed && "
        "cd /testbed && " + test_command
    )

    instance_id = row["instance_id"]
    repo = row["repo"]
    base_commit = row["base_commit"]
    container = _instance_image_ref(spec)
    test_patch = row.get("test_patch") or None
    test_scope = _test_scope_from_patch(test_patch)

    base = StateDatapoint(
        instance_id=f"{instance_id}__base",
        repo=repo,
        base_commit=base_commit,
        patch=test_patch,
        container=container,
        command=command,
        test_scope=test_scope,
    )
    augmented = StateDatapoint(
        instance_id=f"{instance_id}__gold",
        repo=repo,
        base_commit=base_commit,
        patch=_concat_patches(test_patch, row["patch"]),
        container=container,
        command=command,
        test_scope=test_scope,
    )
    return base, augmented


def adapt_swebench_dataset(
    dataset_name: str = "SWE-bench/SWE-bench_Verified",
    *,
    split: str = "test",
    instance_ids: Iterable[str] | None = None,
    namespace: str | None = "swebench",
) -> Iterator[StateDatapoint]:
    """Stream `StateDatapoint`s out of a Hugging Face SWE-bench split.

    `instance_ids` (optional) filters the stream; iteration order matches
    the dataset's natural order. Each row yields its base then augmented
    state — the caller writes them line-by-line into a JSONL.
    """
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise ImportError(_SWEBENCH_INSTALL_HINT) from exc

    wanted: set[str] | None = set(instance_ids) if instance_ids is not None else None
    ds = load_dataset(dataset_name, split=split)
    for row in ds:
        if wanted is not None and row["instance_id"] not in wanted:
            continue
        base, augmented = adapt_swebench_row(dict(row), namespace=namespace)
        yield base
        yield augmented

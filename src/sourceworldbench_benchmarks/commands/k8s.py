import json
import tempfile
import time
from pathlib import Path
from typing import cast

import typer

from sourceworldbench_benchmarks.dataset_io import (
    DEFAULT_DATASET_REPO_ID,
    commit_description,
    load_existing_rows,
    push_rows,
    read_jsonl,
    upsert_rows,
    write_jsonl,
)
from sourceworldbench_benchmarks.execution_tracer.runner.script import TRACE_SCOPES, TraceScope
from sourceworldbench_benchmarks.execution_tracer.runner.spec import BY_NAME as TRACER_BY_NAME
from sourceworldbench_benchmarks.image_builder import (
    DEFAULT_PUSH_PLATFORM,
    BuildImageError,
    build_k8s_runner_image,
    local_k8s_runner_tag,
    parse_build_arg,
    remote_k8s_runner_tag,
)
from sourceworldbench_benchmarks.k8s import (
    DEFAULT_NAMESPACE,
    DEFAULT_PRIORITY,
    DEFAULT_QUEUE,
    DEFAULT_REMOTE_ROOT,
    DEFAULT_RUNNER_IMAGE,
    DEFAULT_RUNTIME_CLASS,
    K8sRunConfig,
    K8sRunError,
    cancel_k8s_run,
    download_k8s_collect,
    download_k8s_trace,
    get_k8s_run_status,
    grafana_logs_url,
    kubectl_inspect_command,
    submit_k8s_run,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Run collect on a Kubernetes cluster: submit worker Jobs, then watch, download, or cancel the run.",
)

DEFAULT_K8S_RUNNER_TAG = "latest"
DEFAULT_K8S_RUNNER_IMAGE = remote_k8s_runner_tag(DEFAULT_K8S_RUNNER_TAG)

# `--run` choices for trace: the row column that overrides `command` (None = no
# override, the row runs `command` as-is).
RUN_COLUMNS = {
    "tests": None,
    "time": "command_workload",
    "memory": "command_workload_amplified",
}


@app.command(
    "build",
    help=f"Build the Kubernetes runner image. Default pushed image: {DEFAULT_K8S_RUNNER_IMAGE}.",
)
def build(
    push: bool = typer.Option(False, "--push", help="Push the built runner image to the registry."),
    tag: str = typer.Option(DEFAULT_K8S_RUNNER_TAG, "--tag", help="Image tag to build and optionally push."),
    image: str | None = typer.Option(
        None,
        "--image",
        help="Full image tag to build when --push is set.",
        show_default=DEFAULT_K8S_RUNNER_IMAGE,
    ),
    build_arg: list[str] = typer.Option(
        [],
        "--build-arg",
        metavar="KEY=VALUE",
        help="Forwarded to docker buildx. Repeatable.",
    ),
    platform: str | None = typer.Option(
        None,
        "--platform",
        metavar="PLATFORM",
        help="Forwarded to docker buildx --platform when --push is set.",
        show_default=DEFAULT_PUSH_PLATFORM,
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Capture Docker build output instead of streaming it."),
) -> None:
    """Build the Kubernetes runner image used by collect worker Jobs."""
    try:
        build_args = dict(parse_build_arg(raw) for raw in build_arg)
    except ValueError as exc:
        typer.echo(f"error: invalid --build-arg: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    target_image = _resolve_k8s_runner_build_image(image=image, push=push, tag=tag)
    target_platform = _resolve_k8s_runner_build_platform(platform=platform, push=push)
    try:
        build_k8s_runner_image(
            repo_root=Path.cwd(),
            push=push,
            image=target_image,
            build_args=build_args,
            platform=target_platform,
            capture_output=quiet,
        )
    except BuildImageError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _resolve_k8s_runner_build_image(*, image: str | None, push: bool, tag: str) -> str:
    if image is not None:
        return image
    if push:
        return remote_k8s_runner_tag(tag)
    return local_k8s_runner_tag(tag)


def _parse_node_selector(values: list[str]) -> tuple[tuple[str, str], ...]:
    """Parse repeated `KEY=VALUE` node-label selectors, splitting on the first `=`."""
    pairs: list[tuple[str, str]] = []
    for raw in values:
        key, _, value = raw.partition("=")
        if not key or not value:
            raise typer.BadParameter(f"--node-selector must be KEY=VALUE with both parts (got {raw!r})")
        pairs.append((key, value))
    return tuple(pairs)


def _resolve_k8s_runner_build_platform(*, platform: str | None, push: bool) -> str | None:
    if platform is not None:
        return platform
    if push:
        return DEFAULT_PUSH_PLATFORM
    return None


@app.command("collect")
def collect(
    in_path: str = typer.Option(
        ..., "--in", help="Input JSONL file or HF dataset repo id with partial StateDatapoint rows."
    ),
    timeout: int = typer.Option(3600, "--timeout", help="Seconds per Kubernetes worker command."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", help="Kubernetes namespace."),
    run_name: str | None = typer.Option(
        None,
        "--run-name",
        help=(
            "Name identifying this run; it becomes the remote run directory name and the label "
            "that status/download/cancel select on. Must not match an existing run. "
            "Auto-generated from the input file name and a timestamp when omitted."
        ),
    ),
    runner_image: str | None = typer.Option(
        DEFAULT_RUNNER_IMAGE,
        "--runner-image",
        help=(
            "Image providing the sourceworldbench-benchmarks worker binary, injected into each row's "
            "environment container (build with `k8s build --push` or use `--build` with this command). "
            "Non-default references must be digest-pinned."
        ),
    ),
    remote_root: str = typer.Option(
        DEFAULT_REMOTE_ROOT,
        "--remote-root",
        help="GCS prefix (gs://bucket/prefix) under which the run directory <remote-root>/<run-name>/ is created.",
    ),
    queue: str = typer.Option(
        DEFAULT_QUEUE,
        "--queue",
        help=(
            "Kueue LocalQueue name. Pass an empty string to submit without Kueue: the Jobs "
            "carry no queue label and start as soon as the scheduler can place them, which "
            "skips admission control and puts the whole run on the cluster at once."
        ),
    ),
    priority: str = typer.Option(
        DEFAULT_PRIORITY,
        "--priority",
        help=(
            "Kueue WorkloadPriorityClass for the worker Jobs (controls queueing order and "
            "preemption). Must name a class that exists in the cluster: "
            "`kubectl get workloadpriorityclass`."
        ),
    ),
    runtime_class: str = typer.Option(
        DEFAULT_RUNTIME_CLASS,
        "--runtime-class",
        help=(
            "RuntimeClass for the worker pods. Pass an empty string to run without a "
            "RuntimeClass (no gVisor sandbox) — only for debugging with trusted images, "
            "since workers execute third-party test code."
        ),
    ),
    node_selector: list[str] = typer.Option(
        [],
        "--node-selector",
        metavar="KEY=VALUE",
        help=(
            "Node label every worker pod must match, e.g. "
            "`node.kubernetes.io/instance-type=n2-standard-16`. Repeatable; default: any node. "
            "Pin a machine shape for timing-sensitive runs, since the cluster mixes machine families."
        ),
    ),
    cpu: str = typer.Option("8", "--cpu", help="Worker CPU request and limit."),
    memory: str = typer.Option("32Gi", "--memory", help="Worker memory request and limit."),
    ephemeral_storage: str = typer.Option(
        "20Gi",
        "--ephemeral-storage",
        help="Worker ephemeral-storage request and limit.",
    ),
    allow_local_images: bool = typer.Option(
        False,
        "--allow-local-images",
        help=(
            "Skip submission-time image checks (registry host required, runner image "
            "digest-pinned). Only for smoke tests against a local cluster that can "
            "resolve node-local images."
        ),
    ),
    build: bool = typer.Option(
        False,
        "--build/--no-build",
        help=(
            "Build the runner image and push it to --runner-image before submitting. "
            "The run uses the digest-pinned reference of the pushed image."
        ),
    ),
) -> None:
    """Run collect on the cluster: one Kubernetes worker Job per unfilled input row.

    With --build, first builds the runner image and pushes it to --runner-image,
    then submits the run with the pushed image's digest-pinned reference.
    Validates the input locally, uploads it to the remote run directory, and
    submits one Kueue-queued worker Job per row whose test-outcome fields are
    still null (already-filled rows pass through without a Job). Returns right
    after submission — the workers run unattended. Follow progress with
    `k8s status` and fetch results with `k8s download`, using the printed
    run-name.
    """
    if build:
        if runner_image is None or "@sha256:" in runner_image:
            typer.echo(
                "error: --build needs a tag-based --runner-image to push to, not a digest-pinned reference",
                err=True,
            )
            raise typer.Exit(code=2)
        try:
            runner_image = build_k8s_runner_image(
                repo_root=Path.cwd(),
                push=True,
                image=runner_image,
                build_args={},
                platform=DEFAULT_PUSH_PLATFORM,
            )
        except BuildImageError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    with tempfile.TemporaryDirectory() as tmp:
        local_in = Path(in_path)
        if not local_in.is_file():
            rows = load_existing_rows(in_path)
            if not rows:
                typer.echo(f"error: source {in_path} is empty; nothing to collect", err=True)
                raise typer.Exit(code=2)
            local_in = Path(tmp) / f"{in_path.rsplit('/', 1)[-1]}.jsonl"
            write_jsonl(local_in, rows)
        try:
            submission = submit_k8s_run(
                in_path=local_in,
                config=K8sRunConfig(
                    namespace=namespace,
                    run_name=run_name,
                    runner_image=runner_image,
                    queue=queue,
                    priority=priority,
                    runtime_class=runtime_class or None,
                    cpu=cpu,
                    memory=memory,
                    ephemeral_storage=ephemeral_storage,
                    command_timeout=timeout,
                    remote_root=remote_root,
                    allow_local_images=allow_local_images,
                    node_selector=_parse_node_selector(node_selector),
                ),
            )
        except K8sRunError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    typer.echo(f"run-name: {submission.run_name}")
    typer.echo(f"namespace: {submission.namespace}")
    typer.echo(f"remote-run-dir: {submission.run_dir}")
    typer.echo()
    typer.echo(f"submitted-rows: {submission.submitted_rows}")
    typer.echo(f"passed-through-rows: {submission.skipped_rows}")
    typer.echo()
    typer.echo("Next commands:")
    typer.echo()
    typer.echo("Status:")
    typer.echo(submission.status_command)
    typer.echo()
    typer.echo("Download:")
    typer.echo(submission.download_command)
    typer.echo()
    typer.echo("Inspect:")
    typer.echo(submission.inspect_command)
    typer.echo()
    typer.echo("Logs (Grafana/Loki):")
    typer.echo(submission.logs_url)
    raise typer.Exit(code=0)


def _resolve_run_input(in_path: str, instance_ids: list[str], tmp_dir: Path, *, verb: str) -> Path:
    """Return a local JSONL path for the run, applying an optional --instance-id filter.

    Without a filter, a local file is passed through as-is and an HF dataset id is
    materialized to a temp file (unchanged behavior). With a filter, rows are loaded
    from either source, the subset is selected by exact instance_id (order preserved
    as given), and only those rows are written to a temp file. Exits 2 on an empty
    source or an unknown instance_id.
    """
    local_in = Path(in_path)
    if not instance_ids and local_in.is_file():
        return local_in
    if local_in.is_file():
        with local_in.open("r", encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
    else:
        rows = load_existing_rows(in_path)
    if not rows:
        typer.echo(f"error: source {in_path} is empty; nothing to {verb}", err=True)
        raise typer.Exit(code=2)
    if instance_ids:
        rows_by_id = {row["instance_id"]: row for row in rows}
        missing = [iid for iid in instance_ids if iid not in rows_by_id]
        if missing:
            typer.echo(f"error: instance_id(s) not found in {in_path}: {', '.join(missing)}", err=True)
            raise typer.Exit(code=2)
        rows = [rows_by_id[iid] for iid in instance_ids]
    name = Path(in_path).name
    if not name.endswith(".jsonl"):
        name = f"{name}.jsonl"
    local_in = tmp_dir / name
    write_jsonl(local_in, rows)
    return local_in


@app.command("trace")
def trace(
    in_path: str = typer.Option(
        ..., "--in", help="Input JSONL file or HF dataset repo id with StateDatapoint rows to trace."
    ),
    instance_ids: list[str] = typer.Option(
        [],
        "--instance-id",
        help="Trace only specific row(s) by instance_id. Repeatable; default: all rows in --in.",
    ),
    tracers: list[str] = typer.Option(
        [],
        "--tracer",
        help=(
            f"Run only this tracer (one of {', '.join(sorted(TRACER_BY_NAME))}). "
            "Repeatable; default: all tracers."
        ),
    ),
    timeout: int = typer.Option(3600, "--timeout", help="Seconds per tracer pass command."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", help="Kubernetes namespace."),
    run_name: str | None = typer.Option(
        None,
        "--run-name",
        help=(
            "Name identifying this run; it becomes the remote run directory name and the label "
            "that status/download/cancel select on. Must not match an existing run. "
            "Auto-generated from the input file name and a timestamp when omitted."
        ),
    ),
    runner_image: str | None = typer.Option(
        DEFAULT_RUNNER_IMAGE,
        "--runner-image",
        help=(
            "Image providing the sourceworldbench-benchmarks worker binary (with the tracer data), injected into "
            "each row's environment container. Non-default references must be digest-pinned."
        ),
    ),
    remote_root: str = typer.Option(
        DEFAULT_REMOTE_ROOT,
        "--remote-root",
        help="GCS prefix (gs://bucket/prefix) under which the run directory <remote-root>/<run-name>/ is created.",
    ),
    queue: str = typer.Option(
        DEFAULT_QUEUE,
        "--queue",
        help=(
            "Kueue LocalQueue name. Pass an empty string to submit without Kueue: the Jobs "
            "carry no queue label and start as soon as the scheduler can place them, which "
            "skips admission control and puts the whole run on the cluster at once."
        ),
    ),
    priority: str = typer.Option(DEFAULT_PRIORITY, "--priority", help="Kueue WorkloadPriorityClass for the Jobs."),
    runtime_class: str = typer.Option(
        DEFAULT_RUNTIME_CLASS,
        "--runtime-class",
        help=(
            "RuntimeClass for the worker pods. Pass an empty string to run without a RuntimeClass "
            "(no gVisor sandbox) — only for debugging with trusted images."
        ),
    ),
    node_selector: list[str] = typer.Option(
        [],
        "--node-selector",
        metavar="KEY=VALUE",
        help=(
            "Node label every worker pod must match, e.g. "
            "`node.kubernetes.io/instance-type=n2-standard-16`. Repeatable; default: any node. "
            "Pin a machine shape for timing-sensitive runs, since the cluster mixes machine families."
        ),
    ),
    cpu: str = typer.Option("8", "--cpu", help="Worker CPU request and limit."),
    memory: str = typer.Option("32Gi", "--memory", help="Worker memory request and limit."),
    ephemeral_storage: str = typer.Option("20Gi", "--ephemeral-storage", help="Worker ephemeral-storage req/limit."),
    allow_local_images: bool = typer.Option(
        False,
        "--allow-local-images",
        help="Skip submission-time image checks. Only for smoke tests against a local cluster.",
    ),
    build: bool = typer.Option(
        False,
        "--build/--no-build",
        help="Build the runner image and push it to --runner-image before submitting.",
    ),
    run: str = typer.Option(
        "tests",
        "--run",
        help=(
            "Which command each row runs: 'tests' (default) runs `command` (the test suite), "
            "'time' runs `command_workload` (the timing workload), 'memory' runs "
            "`command_workload_amplified` (the memory workload). Rows whose selected column "
            "is empty are rejected at submission time."
        ),
    ),
    trace_scope: str = typer.Option(
        "repo",
        "--trace-scope",
        help=(
            "Which part of the repository the tracers record: `repo` (default) covers the whole "
            "repository, identically for every row, so profiles stay comparable across rows; "
            "`patch` covers only the directories each row's patch touches, so the scope varies "
            "from row to row."
        ),
    ),
) -> None:
    """Run the execution tracer on the cluster: one worker Job per (row, tracer).

    Mirrors `k8s collect`, but fans each input row out into one Job per tracer,
    each running the row command once with that tracer attached. The per-tracer
    outputs are merged into a single trace_output.json at download. Every input
    row is traced (filled rows too), or only the rows named by repeated
    `--instance-id`. Use `--tracer` (repeatable) to restrict which tracers run;
    default is every tracer. Fetch results with `k8s trace-download`; use `k8s status`
    and `k8s cancel` as for collect.
    """
    unknown = [t for t in tracers if t not in TRACER_BY_NAME]
    if unknown:
        typer.echo(
            f"error: unknown tracer(s) {', '.join(unknown)!r}; expected one of {', '.join(sorted(TRACER_BY_NAME))}",
            err=True,
        )
        raise typer.Exit(code=2)
    if trace_scope not in TRACE_SCOPES:
        typer.echo(
            f"error: unknown --trace-scope {trace_scope!r}; expected one of {', '.join(TRACE_SCOPES)}", err=True
        )
        raise typer.Exit(code=2)
    if run not in RUN_COLUMNS:
        typer.echo(f"error: unknown --run {run!r}; expected one of {', '.join(RUN_COLUMNS)}", err=True)
        raise typer.Exit(code=2)
    workload_column = RUN_COLUMNS[run]
    if workload_column:
        _src = Path(in_path)
        _rows = (
            [json.loads(line) for line in _src.open() if line.strip()]
            if _src.is_file()
            else load_existing_rows(in_path)
        )
        if instance_ids:
            _wanted = set(instance_ids)
            _rows = [r for r in _rows if r.get("instance_id") in _wanted]
        missing_wl = [r["instance_id"] for r in _rows if not r.get(workload_column)]
        if missing_wl:
            typer.echo(
                f"warning: --run {run} needs {workload_column} but {len(missing_wl)} row(s) have none, "
                " and will be skipped:\n"
                + "\n".join(f"  {iid}" for iid in missing_wl[:10])
                + (f"\n  ... and {len(missing_wl) - 10} more" if len(missing_wl) > 10 else ""),
                err=True,
            )
            instance_ids = [r["instance_id"] for r in _rows if r.get(workload_column)]
            if not instance_ids:
                typer.echo(f"error: no rows with {workload_column} remain", err=True)
                raise typer.Exit(code=2)
    if build:
        if runner_image is None or "@sha256:" in runner_image:
            typer.echo(
                "error: --build needs a tag-based --runner-image to push to, not a digest-pinned reference",
                err=True,
            )
            raise typer.Exit(code=2)
        try:
            runner_image = build_k8s_runner_image(
                repo_root=Path.cwd(),
                push=True,
                image=runner_image,
                build_args={},
                platform=DEFAULT_PUSH_PLATFORM,
            )
        except BuildImageError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    with tempfile.TemporaryDirectory() as tmp:
        local_in = _resolve_run_input(in_path, instance_ids, Path(tmp), verb="trace")
        try:
            submission = submit_k8s_run(
                in_path=local_in,
                config=K8sRunConfig(
                    namespace=namespace,
                    run_name=run_name,
                    runner_image=runner_image,
                    queue=queue,
                    priority=priority,
                    runtime_class=runtime_class or None,
                    cpu=cpu,
                    memory=memory,
                    ephemeral_storage=ephemeral_storage,
                    command_timeout=timeout,
                    remote_root=remote_root,
                    allow_local_images=allow_local_images,
                    worker="trace",
                    tracers=tuple(tracers),
                    validate_parsers=False,
                    workload_column=workload_column,
                    trace_scope=cast(TraceScope, trace_scope),
                    node_selector=_parse_node_selector(node_selector),
                ),
            )
        except K8sRunError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    typer.echo(f"run-name: {submission.run_name}")
    typer.echo(f"namespace: {submission.namespace}")
    typer.echo(f"remote-run-dir: {submission.run_dir}")
    typer.echo()
    typer.echo(f"submitted-jobs: {submission.submitted_rows}")
    typer.echo()
    typer.echo("Next commands:")
    typer.echo()
    typer.echo("Status:")
    typer.echo(submission.status_command)
    typer.echo()
    typer.echo("Download:")
    typer.echo(
        f"uv run sourceworldbench-benchmarks k8s trace-download --run-name {submission.run_name} "
        f"--namespace {submission.namespace} --out-dir <dir>"
    )
    typer.echo()
    typer.echo("Inspect:")
    typer.echo(submission.inspect_command)
    typer.echo()
    typer.echo("Logs (Grafana/Loki):")
    typer.echo(submission.logs_url)
    raise typer.Exit(code=0)


@app.command("trace-download")
def trace_download(
    run_name: str = typer.Option(..., "--run-name", help="Run name printed by `k8s trace`."),
    out_dir: Path = typer.Option(..., "--out-dir", help="Local directory; one subdir per traced instance."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", help="Kubernetes namespace."),
    remote_root: str = typer.Option(
        DEFAULT_REMOTE_ROOT,
        "--remote-root",
        help="GCS prefix (gs://bucket/prefix) the run was submitted under (pass the same value as at trace time).",
    ),
) -> None:
    """Assemble a finished trace run's outputs under --out-dir/<instance_id>/.

    Each row contributes the output file of every tracer that ran; when
    trace_output.json is among them the others are merged into it — the same
    layout local `trace` writes, so the result is a drop-in input for a local
    `trace ... --push` or `build-samples`. Rows with no output from any tracer
    are recorded under <out-dir>.failures/. Refuses while any worker Job is
    still pending or active.
    """
    try:
        summary = download_k8s_trace(
            out_dir=out_dir,
            config=K8sRunConfig(namespace=namespace, run_name=run_name, remote_root=remote_root),
        )
    except K8sRunError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"downloaded: {out_dir}")
    typer.echo(f"done: ok={summary.ok} fail={summary.fail}")
    if summary.fail > 0:
        typer.echo(f"failures: {out_dir}.failures/")


@app.command("status")
def status(
    run_name: str = typer.Option(..., "--run-name", help="Run name printed by `k8s collect`."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", help="Kubernetes namespace."),
    watch: bool = typer.Option(False, "--watch", help="Refresh status until interrupted."),
    poll_interval: float = typer.Option(5, "--poll-interval", min=1, help="Seconds between watch refreshes."),
) -> None:
    """Show how far a submitted run's worker Jobs have progressed.

    Counts the run's Jobs and pods by state; pending includes Kueue-suspended
    Jobs still waiting for queue quota. The run is finished once no Jobs are
    pending or active — `k8s download` refuses to assemble results before
    that.
    """
    config = K8sRunConfig(namespace=namespace, run_name=run_name)
    while True:
        try:
            current = get_k8s_run_status(config=config)
        except K8sRunError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        _print_k8s_status(
            current.run_name,
            current.namespace,
            current.job_counts,
            current.pod_counts,
        )
        if not watch:
            break
        time.sleep(poll_interval)


@app.command("download")
def download(
    run_name: str = typer.Option(..., "--run-name", help="Run name printed by `k8s collect`."),
    out_path: Path = typer.Option(..., "--out", help="Local JSONL path to write."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", help="Kubernetes namespace."),
    remote_root: str = typer.Option(
        DEFAULT_REMOTE_ROOT,
        "--remote-root",
        help="GCS prefix (gs://bucket/prefix) the run was submitted under (pass the same value as at collect time).",
    ),
    push: bool = typer.Option(
        False,
        "--push/--no-push",
        help=f"After assembling results, upsert the filled rows into {DEFAULT_DATASET_REPO_ID}.",
    ),
) -> None:
    """Assemble a finished run's results into a local output JSONL.

    Gathers each worker's filled row from the remote run directory and writes
    them to --out in input order. Failed rows are left out of the output;
    their failure.json is written under <out>.failures/<instance_id>/ instead.
    Refuses while any worker Job is still pending or active.
    """
    try:
        summary = download_k8s_collect(
            out_path=out_path,
            config=K8sRunConfig(namespace=namespace, run_name=run_name, remote_root=remote_root),
        )
    except K8sRunError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"collected: {out_path}")
    typer.echo(f"done: ok={summary.ok} fail={summary.fail}")
    if summary.fail > 0:
        typer.echo(f"failures: {out_path}.failures/")

    if not push:
        return
    changed = {row.instance_id: row.model_dump() for row in read_jsonl(out_path)}
    if not changed:
        typer.echo("No rows downloaded; nothing to push.")
        return
    merged = upsert_rows(load_existing_rows(DEFAULT_DATASET_REPO_ID), changed)
    push_rows(
        DEFAULT_DATASET_REPO_ID,
        merged,
        commit_message=f"Fill test outcomes ({len(changed)} row(s))",
        commit_description=commit_description(list(changed)),
    )
    typer.echo(f"Pushed {len(changed)} row(s) to {DEFAULT_DATASET_REPO_ID}.")


@app.command("cancel")
def cancel(
    run_name: str = typer.Option(..., "--run-name", help="Run name printed by `k8s collect`."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", help="Kubernetes namespace."),
) -> None:
    """Stop a run by deleting its worker Jobs (their pods are removed with them).

    Results already written to the remote run directory are kept, so a later
    `k8s download` returns partial output: finished rows are assembled,
    unfinished rows become missing_worker_artifact failures. The remote run
    directory itself is never deleted by this command.
    """
    try:
        cancel_k8s_run(config=K8sRunConfig(namespace=namespace, run_name=run_name))
    except K8sRunError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"cancelled: {run_name}")


def _print_k8s_status(
    run_name: str,
    namespace: str,
    job_counts: dict[str, int],
    pod_counts: dict[str, int],
) -> None:
    typer.echo(f"run-name: {run_name}")
    typer.echo(f"namespace: {namespace}")
    typer.echo()

    typer.echo("Jobs:")
    typer.echo(f"worker_pending={job_counts.get('worker_pending', 0)}")
    typer.echo(f"worker_active={job_counts.get('worker_active', 0)}")
    typer.echo(f"worker_succeeded={job_counts.get('worker_succeeded', 0)}")
    typer.echo(f"worker_failed={job_counts.get('worker_failed', 0)}")
    typer.echo()

    typer.echo("Pods:")
    typer.echo(f"total={pod_counts.get('total', 0)}")
    typer.echo(f"pending={pod_counts.get('pending', 0)}")
    typer.echo(f"running={pod_counts.get('running', 0)}")
    typer.echo(f"succeeded={pod_counts.get('succeeded', 0)}")
    typer.echo(f"failed={pod_counts.get('failed', 0)}")
    typer.echo()

    typer.echo("Inspect:")
    typer.echo(kubectl_inspect_command(namespace=namespace, run_name=run_name))
    typer.echo()
    typer.echo("Logs (Grafana/Loki):")
    typer.echo(grafana_logs_url(namespace=namespace, run_name=run_name))

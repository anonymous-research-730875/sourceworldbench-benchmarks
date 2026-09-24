"""Worker entrypoint logic for `k8s trace`: run one tracer over one row in-pod.

Sibling of the `execute_worker_row*` drivers in `execution.py`. Where `execute`
fills a row, `trace` produces one tracer's output: it runs the row command once
with that tracer activated via `CheckoutExecutionBackend.run(extra_env=…)` and
writes the tracer's JSON straight to the artifacts dir we upload from.

The tracers are mutually exclusive (`settrace`, `setprofile`, clean
`perf_counter`, `tracemalloc` and an RSS measured without tracemalloc cannot
coexist in one process), so `k8s trace` fans a row out into one Job per
tracer; each Job runs this worker once. The
per-tracer outputs are merged at download (`k8s/trace.py`), not in-pod, so a
failed tracer never costs the row the tracers that succeeded.
"""

import gzip
import hashlib
import json
import logging
import os
import platform
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from sourceworldbench_benchmarks.execution import CheckoutExecutionBackend, ExecutionError, select_input_row
from sourceworldbench_benchmarks.execution_tracer.runner.runner import _stage_bundle
from sourceworldbench_benchmarks.execution_tracer.runner.script import TraceScope, resolve_trace_paths
from sourceworldbench_benchmarks.execution_tracer.runner.spec import BY_NAME, TracerSpec
from sourceworldbench_benchmarks.gcs import GcsFiles

logger = logging.getLogger(__name__)

TRACE_MARKER = "trace.json"

# Bumped whenever the `host` block's keys or their meaning change, so a mixed set
# of downloaded runs stays readable: consumers can branch on the version rather
# than guess which shape a file holds.
HOST_SCHEMA_VERSION = 3

CPUINFO_PATH = Path("/proc/cpuinfo")


@dataclass
class TraceWorkerOutcome:
    """Result of running one tracer over one row. `ok` is true iff the tracer wrote output."""

    instance_id: str
    tracer: str
    ok: bool
    marker_path: Path


def stage_all_bundles(dest: Path) -> list[str]:
    """Stage every tracer's bundle under `dest/<name>/`; return the tracer names.

    Raises `FileNotFoundError` if a tracer/bootstrap source file is missing —
    used as the runner-image build self-check that the tracer `.py` files are
    bundled and resolvable from the frozen binary.
    """
    for name, spec in BY_NAME.items():
        _stage_bundle(spec, dest / name)
    return list(BY_NAME)


def _resolve_spec(tracer: str) -> TracerSpec:
    try:
        return BY_NAME[tracer]
    except KeyError:
        raise ValueError(f"unknown tracer {tracer!r}; expected one of {', '.join(BY_NAME)}") from None


def _read_output(output_path: Path) -> dict[str, Any]:
    if output_path.suffix == ".gz":
        return json.loads(gzip.decompress(output_path.read_bytes()))
    return json.loads(output_path.read_text(encoding="utf-8"))


def _write_output(output_path: Path, data: dict[str, Any]) -> None:
    """Rewrite a tracer output in place, compactly: a trace pass can emit millions of events."""
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    output_path.write_bytes(gzip.compress(raw) if output_path.suffix == ".gz" else raw)


def _test_count(data: dict[str, Any]) -> int:
    count = data.get("test_count")
    return int(count) if count is not None else len(data.get("tests") or {})


def _find_output(artifacts_dir: Path, spec: TracerSpec) -> Path | None:
    for name in (spec.output_filename, f"{spec.output_filename}.gz"):
        candidate = artifacts_dir / name
        if candidate.exists():
            return candidate
    return None


def _cpuinfo_fields() -> dict[str, str]:
    """Return the first processor block of `/proc/cpuinfo` as a key -> value mapping."""
    fields: dict[str, str] = {}
    try:
        text = CPUINFO_PATH.read_text(encoding="utf-8")
    except OSError:
        return fields
    for line in text.splitlines():
        if not line.strip():
            # Blank line ends the first processor's block; the rest are its siblings.
            if fields:
                break
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            fields.setdefault(key.strip(), value.strip())
    return fields


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _normalize_family(raw: int | None) -> int | None:
    """Sum a CPUID family that `/proc/cpuinfo` left in its raw encoded form.

    Linux prints `cpu family` as base + extended, but gVisor prints the two
    nibbles packed into one byte instead. A c2d pod reports 175 (0xAF) where
    Linux would print 25: base 0xF, extended 0xA, and 15 + 10 is 25 — Milan,
    which is what the Compute API says those nodes are.

    The x86 rule — extend only when the base nibble is 0xF — unpacks that form
    and leaves an already-summed value alone, so both encodings land on the same
    number. Intel is untouched either way: its base nibble is 6.
    """
    if raw is None:
        return None
    return 15 + (raw >> 4) if raw & 0xF == 0xF else raw


def _microarch(vendor: str | None, family: int | None, model: int | None) -> str | None:
    """Name the silicon from its CPUID vendor/family/model, or None if unrecognised.

    `family` must already be normalized by `_normalize_family`. This is the
    identification that survives gVisor: the sandbox rewrites `model name` to
    "unknown" but passes the CPUID numbers through. Only the platforms Google
    Compute Engine offers are listed. Intel model 85 covers two platforms that a
    sandboxed pod cannot tell apart, because `stepping` — the field that
    separates them — is blanked too.
    """
    return {
        ("GenuineIntel", 6, 45): "Intel Sandy Bridge",
        ("GenuineIntel", 6, 62): "Intel Ivy Bridge",
        ("GenuineIntel", 6, 63): "Intel Haswell",
        ("GenuineIntel", 6, 79): "Intel Broadwell",
        ("GenuineIntel", 6, 85): "Intel Skylake or Cascade Lake",
        ("GenuineIntel", 6, 106): "Intel Ice Lake",
        ("GenuineIntel", 6, 143): "Intel Sapphire Rapids",
        ("GenuineIntel", 6, 207): "Intel Emerald Rapids",
        ("GenuineIntel", 6, 173): "Intel Granite Rapids",
        ("AuthenticAMD", 23, 1): "AMD Naples",
        ("AuthenticAMD", 23, 49): "AMD Rome",
        ("AuthenticAMD", 25, 1): "AMD Milan",
        ("AuthenticAMD", 25, 17): "AMD Genoa",
        ("AuthenticAMD", 26, 2): "AMD Turin",
    }.get((vendor or "", family if family is not None else -1, model if model is not None else -1))


def cpu_info() -> dict[str, Any]:
    """Identify the CPU from `/proc/cpuinfo`, working around gVisor's blanked fields.

    `family` is the summed value Linux would print, not always the one the file
    carries — see `_normalize_family`. `model_name` is None whenever the sandbox
    blanked it, so a real name is never confused with the literal string
    "unknown". `flags_hash` fingerprints the instruction-set feature list: two
    passes with the same hash ran on the same kind of silicon, whether or not
    `microarch` recognised it.

    Only fields that vary with the silicon are reported. The file's `cache size`
    is not one of them — gVisor prints 8192 KB whatever the chip — so the cache
    hierarchy has to come from the named microarchitecture or from a measurement.
    """
    fields = _cpuinfo_fields()
    vendor = fields.get("vendor_id")
    family = _normalize_family(_as_int(fields.get("cpu family")))
    model = _as_int(fields.get("model"))
    model_name = fields.get("model name")
    stepping = fields.get("stepping")
    flags = (fields.get("flags") or "").split()
    return {
        "vendor": vendor,
        "family": family,
        "model": model,
        "stepping": _as_int(stepping),
        "model_name": model_name if model_name and model_name != "unknown" else None,
        "microarch": _microarch(vendor, family, model),
        "mhz": _as_float(fields.get("cpu MHz")),
        "flag_count": len(flags),
        "flags_hash": hashlib.sha256(" ".join(sorted(flags)).encode("utf-8")).hexdigest()[:12] if flags else None,
    }


def host_info() -> dict[str, Any]:
    """Describe the machine a tracer pass ran on, and what that machine was given.

    Everything but `cpu` and `platform` comes from the env vars the worker Job's
    downward API sets (`k8s/manifests.py:_worker_env`), so all of it is None for
    a local run. `cpu_count` is what the process sees; under gVisor that is the
    sandbox's allotment, which is why `cpu_limit_millicores` is recorded
    alongside it — that one is the container's real limit, straight from the API
    server. `platform` describes the process's own view: under gVisor its kernel
    release is the sandbox's fixed 4.4.0, never the node's kernel, so read it for
    the C library version and as a marker that the pass was sandboxed. Machine
    type, CPU platform and node pool are deliberately absent: a pod cannot read
    them, they have to be collected outside the cluster.
    """
    return {
        "schema_version": HOST_SCHEMA_VERSION,
        "node": os.environ.get("SOURCEWORLDBENCH_NODE_NAME"),
        "pod": os.environ.get("SOURCEWORLDBENCH_POD_NAME"),
        "pod_uid": os.environ.get("SOURCEWORLDBENCH_POD_UID"),
        "job": os.environ.get("SOURCEWORLDBENCH_JOB_NAME"),
        "run": os.environ.get("SOURCEWORLDBENCH_RUN_NAME"),
        "container_image": os.environ.get("SOURCEWORLDBENCH_CONTAINER_IMAGE"),
        "runner_image": os.environ.get("SOURCEWORLDBENCH_RUNNER_IMAGE"),
        "cpu": cpu_info(),
        "cpu_count": os.cpu_count(),
        "cpu_limit_millicores": _as_int(os.environ.get("SOURCEWORLDBENCH_CPU_LIMIT_MILLICORES")),
        "memory_limit_bytes": _as_int(os.environ.get("SOURCEWORLDBENCH_MEMORY_LIMIT_BYTES")),
        "platform": platform.platform(),
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sourceworldbench_version() -> str | None:
    try:
        return version("sourceworldbench-benchmarks")
    except PackageNotFoundError:
        # The runner ships as a frozen binary, which need not carry its own metadata.
        return None


def execute_worker_trace(
    *,
    input_path: Path,
    instance_id: str,
    tracer: str,
    repo_dir: Path,
    artifacts_dir: Path,
    timeout: int,
    tempdir: Path,
    workload_column: str | None = None,
    trace_scope: TraceScope = "repo",
) -> TraceWorkerOutcome:
    """Run one tracer over one row, then write the completion marker.

    The tracer JSON lands at `artifacts_dir/<tracer>_output.json`, the command's
    stdout/stderr under `artifacts_dir/logs/` (always, including on failure, so
    an empty or timed-out pass is diagnosable), and the marker at
    `artifacts_dir/trace.json` (written last). `tempdir` holds the staged bundle
    and per-command scratch dirs; the caller owns its lifetime.

    `trace_scope` picks which part of the repository the tracer records — see
    `resolve_trace_paths`. It is echoed into the pass block so a downloaded run
    says which scope produced it.

    Both the tracer JSON and the marker carry a `host` block (the machine) and a
    `pass` block (this run of this tracer), so a profile stays interpretable once
    the pod and its node are gone.

    Bundle-staging failures propagate (infra: the runner image is missing the
    tracer data). A tracer pass that raises is recorded in the marker, not
    fatal — the row keeps whatever other tracers produced.
    """
    spec = _resolve_spec(tracer)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = artifacts_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    _, row = select_input_row(input_path=input_path, instance_id=instance_id)

    if workload_column:
        command = getattr(row, workload_column)
        if not command:
            raise ValueError(f"--workload set but {workload_column} is None for {instance_id!r}")
        row = row.model_copy(update={"command": command})

    bundle_dir = tempdir / "bundle"
    _stage_bundle(spec, bundle_dir)
    backend = CheckoutExecutionBackend(repo_dir=repo_dir, command_results_dir=tempdir / "cmd-results")
    extra_env = {
        "PYTHONPATH": f"{bundle_dir}:{os.environ.get('PYTHONPATH', '')}".rstrip(":"),
        "PYTEST_PLUGINS": "sourceworldbench_tracer_plugin",
        # `trace_scope` has the same values and default in the local
        # `execution-tracer trace` CLI, so an instance traced here and traced
        # locally under the same scope yield the same function set.
        **spec.env_vars(
            output_path_in_container=str(artifacts_dir / spec.output_filename),
            repo_dir=str(repo_dir),
            trace_paths=resolve_trace_paths(trace_scope, row.patch),
        ),
    }
    if workload_column:
        # Keep the one test's stdout: it carries the workload's own `timeit` mean.
        # Not on suite passes, where thousands of tests would balloon the output.
        extra_env[spec.env_prefix + "_KEEP_STDOUT"] = "1"

    started_at = _utc_now()
    started = time.monotonic()
    rebuild_s: float | None = None
    try:
        result = backend.run(row, tempdir / "results", timeout, extra_env=extra_env)
        (logs_dir / "cmd.stdout").write_bytes(result.stdout)
        (logs_dir / "cmd.stderr").write_bytes(result.stderr)
        if result.rebuild_seconds is not None:
            rebuild_s = round(result.rebuild_seconds, 3)
        status = "ok"
    except ExecutionError as exc:
        logger.warning("trace pass %s failed for %s: %s", tracer, instance_id, exc)
        (logs_dir / "cmd.stdout").write_bytes(exc.stdout)
        (logs_dir / "cmd.stderr").write_bytes(exc.stderr)
        status = type(exc).__name__
    # Wall clock over the command alone, staging and upload excluded. Coarse by
    # design: it bounds the pass, the tracers time the tests.
    duration_s = round(time.monotonic() - started, 3)
    finished_at = _utc_now()

    output_path = _find_output(artifacts_dir, spec)
    host = host_info()
    # Identity and wall clock of this pass, kept apart from `host`, which is only
    # about the machine. The timestamps are what join a profile to whatever was
    # recorded about its node outside the cluster.
    pass_info = {
        "instance_id": instance_id,
        "tracer": tracer,
        "workload": workload_column is not None,
        "workload_column": workload_column,
        "trace_scope": trace_scope,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": duration_s,
        # Part of `duration_s`, split out because it can dominate it and no
        # tracer sees it. Null when the row declared no rebuild.
        "rebuild_s": rebuild_s,
        "sourceworldbench_version": _sourceworldbench_version(),
    }
    test_count = 0
    if output_path is not None:
        try:
            data = _read_output(output_path)
            test_count = _test_count(data)
            # Into the output, not only the marker: download skips the marker, and
            # `merge_trace_outputs` writes the trace dict back, so these keys survive.
            data["host"] = host
            data["pass"] = pass_info
            _write_output(output_path, data)
        except (OSError, EOFError, ValueError) as exc:
            # A pass killed mid-write (timeout, OOM) leaves a truncated file;
            # drop it so download never merges unparseable output.
            logger.warning("trace pass %s wrote unreadable output for %s: %s", tracer, instance_id, exc)
            output_path.unlink()
            output_path = None
            if status == "ok":
                status = "invalid_output"
    if status == "ok" and output_path is None:
        status = "no_output"

    ok = output_path is not None
    marker_path = artifacts_dir / TRACE_MARKER
    marker_path.write_text(
        json.dumps(
            {
                **pass_info,
                # `status` is re-read: an unreadable or absent output demotes it
                # after `pass_info` was built.
                "status": status,
                "test_count": test_count,
                # Also here, so a pass with no output still records where it ran.
                "host": host,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return TraceWorkerOutcome(instance_id=instance_id, tracer=tracer, ok=ok, marker_path=marker_path)


def execute_worker_trace_gcs(
    *,
    input_uri: str,
    instance_id: str,
    tracer: str,
    repo_dir: Path,
    artifacts_uri: str,
    timeout: int,
    gcs: GcsFiles,
    workload_column: str | None = None,
    trace_scope: TraceScope = "repo",
) -> TraceWorkerOutcome:
    """Run one tracer over one row with gs:// input/artifacts: stage locally, upload at the end.

    Every artifact uploads first; the marker `trace.json` uploads last, because
    `k8s trace-download` treats its presence as "this tracer unit finished".
    """
    with tempfile.TemporaryDirectory(prefix="sourceworldbench_trace_worker_") as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / "rows.jsonl"
        gcs.download_file(input_uri, input_path)
        artifacts_dir = tmp_dir / "artifacts"
        outcome = execute_worker_trace(
            input_path=input_path,
            instance_id=instance_id,
            tracer=tracer,
            repo_dir=repo_dir,
            artifacts_dir=artifacts_dir,
            timeout=timeout,
            tempdir=tmp_dir / "work",
            workload_column=workload_column,
            trace_scope=trace_scope,
        )
        artifacts_uri = artifacts_uri.rstrip("/")
        marker = outcome.marker_path
        for path in sorted(p for p in artifacts_dir.rglob("*") if p.is_file() and p != marker):
            gcs.upload_file(path, f"{artifacts_uri}/{path.relative_to(artifacts_dir)}")
        gcs.upload_file(marker, f"{artifacts_uri}/{TRACE_MARKER}")
        return outcome

import hashlib
import json
import re
import urllib.parse
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from sourceworldbench_benchmarks.schema import StateDatapoint

DEFAULT_NAMESPACE = "sourceworldbench"
DEFAULT_RUNNER_IMAGE = "xxx-docker.pkg.dev/xxx/ml/sourceworldbench/sourceworldbench-runner:latest"
DEFAULT_QUEUE = "default"
DEFAULT_PRIORITY = "low"
DEFAULT_RUNTIME_CLASS = "gvisor"
DEFAULT_WORKER_SERVICE_ACCOUNT = "gke-cloud-storage"
DEFAULT_IMAGE_PULL_SECRET = "xxx-regcred-ro"
DEFAULT_REMOTE_ROOT = "gs://xxx/sourceworldbench/collect-runs"
# Bound worker Job names so the Job controller's generated pod-name suffix
# still fits within the 63-character Kubernetes name limit.
MAX_WORKER_JOB_NAME_LENGTH = 52
WORKER_JOB_HASH_LENGTH = 8
# Kubernetes label values (and the names we derive from them) are capped at 63 characters.
LABEL_VALUE_MAX_LENGTH = 63

APP_LABEL = "sourceworldbench-benchmarks"
RUN_LABEL = "sourceworldbench-benchmarks/run-name"
COMPONENT_LABEL = "sourceworldbench-benchmarks/component"
EXAMPLE_HASH_LABEL = "sourceworldbench-benchmarks/example-hash"
INSTANCE_LABEL = "sourceworldbench-benchmarks/instance-id"

# Grafana Explore + Loki for streaming worker pod logs
GRAFANA_EXPLORE_URL = "https://grafana.xxx/explore"
LOKI_DATASOURCE_UID = "xxx"
GRAFANA_ORG_ID = "xxx"

JobState = Literal["pending", "active", "succeeded", "failed"]


class K8sRunError(Exception):
    """Kubernetes collect submission or download failed before row execution completed."""


@dataclass(frozen=True)
class K8sRunConfig:
    """Configuration resolved before a Kubernetes collect run is submitted."""

    namespace: str = DEFAULT_NAMESPACE
    run_name: str | None = None
    runner_image: str | None = DEFAULT_RUNNER_IMAGE
    queue: str = DEFAULT_QUEUE
    priority: str = DEFAULT_PRIORITY
    runtime_class: str | None = DEFAULT_RUNTIME_CLASS
    cpu: str = "8"
    memory: str = "32Gi"
    ephemeral_storage: str = "20Gi"
    command_timeout: int = 7200
    # "execute" fills rows (collect); "trace" runs the tracer worker. The worker
    # kind drives which Job args are rendered and whether every row gets a Job.
    worker: Literal["execute", "trace"] = "execute"
    # Subset of tracers to fan out for trace runs; empty tuple means all tracers.
    tracers: tuple[str, ...] = ()
    # Row column the trace worker runs instead of `command` (e.g. "command_workload");
    # None means the worker runs `command`.
    workload_column: str | None = None
    # Which part of the repository the tracers record: "repo" covers the whole
    # repository, "patch" scopes to the row patch's directories (varies per row).
    trace_scope: Literal["patch", "repo"] = "repo"
    # Node labels every worker pod must match, as (key, value) pairs; empty means any
    # node. Pinning a machine shape keeps a timing-sensitive run's profiles comparable.
    node_selector: tuple[tuple[str, str], ...] = ()
    repo_dir: str = "/app"
    remote_root: str = DEFAULT_REMOTE_ROOT
    worker_service_account: str = DEFAULT_WORKER_SERVICE_ACCOUNT
    image_pull_secret: str = DEFAULT_IMAGE_PULL_SECRET
    allow_local_images: bool = False
    validate_parsers: bool = True
    runner_source_path: str = "/opt/sourceworldbench-runner"
    runner_mount_path: str = "/opt/sourceworldbench-runner"
    runner_copy_mount_path: str = "/runner-copy"
    runner_command: str = "/opt/sourceworldbench-runner/bin/sourceworldbench-benchmarks"

    def __post_init__(self) -> None:
        if not self.remote_root.startswith("gs://"):
            raise K8sRunError(f"remote_root must be a gs://bucket/prefix URI, got {self.remote_root!r}")


@dataclass(frozen=True)
class K8sRunStatus:
    """Current Kubernetes status for one collect run."""

    run_name: str
    namespace: str
    job_counts: dict[str, int]
    pod_counts: dict[str, int]


@dataclass(frozen=True)
class WorkerUnit:
    """One worker Job for a row.

    `tracer` is None for collect (the row's single Job) and a tracer name for
    `trace`, which fans a row out into one Job per tracer.
    """

    job_name: str
    example_hash: str
    tracer: str | None = None


@dataclass(frozen=True)
class PreparedRow:
    """One validated input row and its Kubernetes execution metadata.

    `units` is empty exactly for passthrough rows (already filled, no worker
    Job); collect rows have one unit, trace rows have one per tracer.
    """

    original_index: int
    partial: StateDatapoint
    payload: dict[str, Any]
    raw_line: str
    needs_cluster: bool
    units: tuple[WorkerUnit, ...] = ()


@dataclass(frozen=True)
class PreparedK8sRun:
    """Validated input rows and run-path metadata for a Kubernetes collect run."""

    run_name: str
    run_dir: str
    rows: list[PreparedRow]
    skipped: int

    @property
    def total(self) -> int:
        """Return the number of non-blank input rows in the run."""
        return len(self.rows)

    @property
    def submitted(self) -> int:
        """Return the number of worker Jobs across all rows (one per unit)."""
        return sum(len(row.units) for row in self.rows)


@dataclass(frozen=True)
class K8sRunSubmission:
    """Submission metadata printed by the CLI after creating the worker Jobs."""

    run_name: str
    namespace: str
    run_dir: str
    submitted_rows: int
    skipped_rows: int

    @property
    def status_command(self) -> str:
        """Return the command users can run to inspect this submitted run."""
        return f"uv run sourceworldbench-benchmarks k8s status --run-name {self.run_name} --namespace {self.namespace}"

    @property
    def download_command(self) -> str:
        """Return the command prefix users can run to download this submitted run."""
        return (
            f"uv run sourceworldbench-benchmarks k8s download --run-name {self.run_name} "
            f"--namespace {self.namespace} --out <path>"
        )

    @property
    def inspect_command(self) -> str:
        """Return the kubectl command users can run to list this run's Jobs and pods."""
        return kubectl_inspect_command(namespace=self.namespace, run_name=self.run_name)

    @property
    def logs_url(self) -> str:
        """Return a Grafana Explore URL streaming Loki logs for this run's worker pods."""
        return grafana_logs_url(namespace=self.namespace, run_name=self.run_name)


def _required_or_default_run_name(config: K8sRunConfig, in_path: Path) -> str:
    if config.run_name:
        return _sanitize_name(config.run_name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = f"{stamp}-{uuid.uuid4().hex[:6]}"
    # Trim the filename stem, not the whole name, so the timestamp+uuid that makes
    # the name unique always survives the label-length cap.
    budget = LABEL_VALUE_MAX_LENGTH - len(suffix) - 1
    return _trim_name(f"{_sanitize_name(in_path.stem)[:budget]}-{suffix}")


def _required_run_name(config: K8sRunConfig) -> str:
    if not config.run_name:
        raise K8sRunError("--run-name is required for this command")
    return _sanitize_name(config.run_name)


def _replace_config(config: K8sRunConfig, **updates: Any) -> K8sRunConfig:
    values = asdict(config)
    values.update(updates)
    return K8sRunConfig(**values)


def _remote_run_dir(config: K8sRunConfig, run_name: str) -> str:
    return f"{config.remote_root.rstrip('/')}/{run_name}"


def _run_label_selector(run_name: str) -> str:
    return f"{RUN_LABEL}={run_name}"


def kubectl_inspect_command(*, namespace: str, run_name: str) -> str:
    """Return a kubectl command listing a run's Jobs and pods with their instance ids."""
    return f"kubectl -n {namespace} get jobs,pods -l {RUN_LABEL}={run_name} -L {INSTANCE_LABEL}"


def grafana_logs_url(*, namespace: str, run_name: str) -> str:
    """Return a Grafana Explore URL streaming Loki logs for a run's worker pods.

    Filters Loki by namespace and a pod-name regex covering every worker pod in
    the run, so one link follows the whole fan-out. Valid even before any pod
    exists (Kueue may keep Jobs suspended), since it matches by name over a time
    window rather than listing live pods. Loki indexes `namespace`/`pod` but not
    our custom labels, which is why this matches on the pod name.
    """
    expr = f'{{namespace="{namespace}", pod=~"{_worker_job_name_prefix(run_name)}.*"}}'
    panes = {
        "sourceworldbench": {
            "datasource": LOKI_DATASOURCE_UID,
            "queries": [
                {
                    "refId": "A",
                    "expr": expr,
                    "queryType": "range",
                    "datasource": {"type": "loki", "uid": LOKI_DATASOURCE_UID},
                    "editorMode": "code",
                }
            ],
            "range": {"from": "now-24h", "to": "now"},
        }
    }
    query = urllib.parse.urlencode(
        {
            "schemaVersion": "1",
            "panes": json.dumps(panes, separators=(",", ":")),
            "orgId": GRAFANA_ORG_ID,
        }
    )
    return f"{GRAFANA_EXPLORE_URL}?{query}"


def _worker_job_name(run_name: str, example_hash: str) -> str:
    return f"{_worker_job_name_prefix(run_name)}{example_hash[:WORKER_JOB_HASH_LENGTH]}"


def _worker_job_name_prefix(run_name: str) -> str:
    """Return the name prefix shared by every worker Job and pod in a run.

    Worker Job names are this prefix plus the per-row example hash; pod names add
    the Job controller's own suffix. So matching `<prefix>.*` covers all of a
    run's worker pods — which is what the Loki logs query relies on.
    """
    suffix_len = len("-w-") + WORKER_JOB_HASH_LENGTH
    prefix = _bounded_name(
        prefix="sourceworldbench-collect",
        value=run_name,
        max_length=MAX_WORKER_JOB_NAME_LENGTH - suffix_len,
    )
    return f"{prefix}-w-"


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _sanitize_name(value: str) -> str:
    sanitized = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return sanitized or "run"


def _trim_label_value(value: str) -> str:
    return _trim_name(value)


def _trim_name(value: str, max_length: int = LABEL_VALUE_MAX_LENGTH) -> str:
    value = value[:max_length].strip("-")
    return value or "x"


def _bounded_name(*, prefix: str, value: str, max_length: int) -> str:
    suffix = _stable_hash(value)[:6]
    budget = max_length - len(suffix) - 1
    base = _sanitize_name(f"{prefix}-{value}")[:budget]
    return _trim_name(f"{base}-{suffix}", max_length=max_length)


def _job_state(job: dict[str, Any]) -> JobState:
    """Classify a Job; no conditions and `active == 0` means "pending".

    A Kueue-suspended Job looks exactly like that, so queued Jobs count as
    pending — which is what makes download refuse runs still waiting for quota.
    """
    status = job.get("status", {})
    if _has_true_condition(status, {"Complete"}):
        return "succeeded"
    if _has_true_condition(status, {"Failed"}):
        return "failed"
    if int(status.get("active", 0)) > 0:
        return "active"
    return "pending"


def _has_true_condition(status: dict[str, Any], condition_types: set[str]) -> bool:
    for condition in status.get("conditions", []):
        if condition.get("type") in condition_types and condition.get("status") == "True":
            return True
    return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    _write_text_file(path, json.dumps(payload, sort_keys=True) + "\n")


def _write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)

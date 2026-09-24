# Kubernetes Collect

`k8s collect` runs each incomplete input row as its own `batch/v1` Job on the
cluster. The laptop renders one Kueue-labeled Job per row, uploads the run
directory to a GCS bucket, and applies all Jobs in a single `kind: List`. There
is no in-cluster controller and no JobSet: Kueue suspends each Job on creation
and admits Jobs as queue quota frees up, so the LocalQueue quota *is* the
parallelism control, and a failed row affects only itself (`backoffLimit: 0`).

Each worker Job runs the row's own container image, injects the
`sourceworldbench-benchmarks` binary from the runner image via an init container, executes
the hidden `sourceworldbench-benchmarks execute` command, and uploads its output to GCS
over the JSON API (no gcsfuse mount — the FUSE sidecar interacts badly with
the gVisor sandbox). Worker pods always run as uid 65532, overriding the
image's `USER`. The runner image build enforces a glibc floor (bullseye, 2.31)
so the injected binary starts in the oldest environment images.

## Requirements

- The worker ServiceAccount (default `gke-cloud-storage`) bound via Workload
  Identity to a Google service account with object read/write on the remote
  root bucket. No extra RBAC is needed: Jobs are created by your local
  kubeconfig identity, not by anything running in the cluster.
- Laptop GCS access for upload/download: `gcloud auth application-default
  login` with object read/write on the same bucket.
- The `xxx-regcred-ro` image pull secret in the namespace.
- Kueue with the `batch/job` integration enabled, a LocalQueue (default
  `default`), and a WorkloadPriorityClass (default `low`). Verify with a
  labeled sleep Job: it should be created suspended, get a Workload object,
  then unsuspend and run.
- A runner image built by `k8s build --push` (see README), or pass `--build`
  to `k8s collect` to build and push it as part of submission. Workers do not
  need kubectl; the image only provides the standalone `sourceworldbench-benchmarks` binary.

## Remote run directory layout

`k8s collect` creates `<remote-root>/<run-name>/` in GCS (default remote root
`gs://xxx/sourceworldbench/collect-runs`). Runs submitted
before the GCS-API switch used PVC paths, but the data lives in the same
bucket: the old default `/mnt/experiments/sourceworldbench/collect-runs` is today's default
remote root, and pre-rename runs are under `gs://.../sourceworldbench/build-runs` with an
explicit `--remote-root`:

```
<run-dir>/
├── run.json               # submission metadata; download depends on
│                          # run_name, remote_run_dir, and rows[]
├── input/rows.jsonl       # the validated input rows, one per line
└── workers/<instance_id>/
    ├── result.json        # filled row, written on success
    └── artifacts/
        ├── failure.json   # category/stage/reason, written on failure
        ├── logs/          # command stdout/stderr
        └── raw-results/   # whatever the row command wrote to /results
```

The run directory name must be new: submission fails if it already exists.

## Lifecycle

```bash
uv run sourceworldbench-benchmarks k8s collect --in partial.jsonl
uv run sourceworldbench-benchmarks k8s status --run-name <run> [--watch]
uv run sourceworldbench-benchmarks k8s download --run-name <run> --out filled.jsonl
uv run sourceworldbench-benchmarks k8s cancel --run-name <run>
```

`download` refuses while any worker Job is pending or active, then assembles
the output from `run.json` plus the per-worker files, preserving input order.
Failed rows are omitted from the output JSONL; their failure details are
written locally under `filled.jsonl.failures/<instance_id>/failure.json`, the
same convention local `collect` uses.

## Flow

```
 laptop                             Kubernetes cluster                  GCS bucket (durable)
 ──────                             ──────────────────                  ────────────────────
 partial.jsonl
   │ k8s collect
   ├─ 1. validate all rows locally
   ├─ 2. server-side dry-run of all Jobs
   ├─ 3. upload via GCS API (ADC) ─────────────────────────────────▶  <run-dir>/run.json
   │     (run.json first, no-overwrite precondition)                   <run-dir>/input/rows.jsonl
   └─ 4. apply Jobs (kind: List) ──▶ one Job per incomplete row,
                                      created Kueue-suspended
                                        │ Kueue admits within quota
                                        ▼
                                      worker pod (the row's own image)
                                        ├─ init: inject runner binary
                                        ├─ download input, run the row command
                                        │  (GCS API via Workload Identity)
                                        ├─ upload artifacts ───────▶  workers/<id>/artifacts/logs/, raw-results/
                                        ├─ success, uploaded last ─▶  workers/<id>/result.json
                                        └─ failure, uploaded last ─▶  workers/<id>/artifacts/failure.json

 k8s status ◀── kubectl get jobs,pods by run label ── reads cluster state only, touches no data
                (finished Jobs self-delete after 1h, so a long-finished run shows zero jobs)

 k8s download
   ├─ 1. refuse while any worker Job is pending or active
   ├─ 2. read run.json + inputs, then per row ◀────────────────────  <run-dir>/workers/<id>/...
   │     result.json or failure.json (GCS API)
   └─ 3. write filled.jsonl + .failures/ locally, in input order

 k8s cancel ──▶ deletes the run's Jobs (pods cascade); never touches the run directory
```

## Failure modes

- **A worker fails** (test command infra error, OOM, eviction, preemption at
  priority `low`): only that row fails. `download` copies its remote
  `failure.json` locally. Re-run failed rows by submitting them as a new run.
- **A worker Job goes terminal without writing `result.json` or
  `failure.json`**: `download` synthesizes a failure with reason
  `missing_worker_artifact` and category `infrastructure`.
- **A worker dies before the worker protocol starts** (it cannot reach GCS,
  or the injected binary cannot start in the row's image): no
  `failure.json` exists, so `download` reports `missing_worker_artifact`. The
  pod log is the only evidence — read it promptly with
  `kubectl -n <ns> logs <pod> -c execute`; autoscaler node deletion takes
  logs away within minutes of the pod finishing, and the Job TTL deletes the
  pod itself an hour after the Job finishes.
- **`cancel`** deletes the run's Jobs (pods cascade). A subsequent `download`
  returns partial results: finished rows are assembled, unfinished rows are
  reported as `missing_worker_artifact` failures.
- **The final `kubectl apply` fails after upload** (for example quota or
  admission errors): the run directory is already in the bucket and some Jobs may
  exist. The error message says so; run `k8s cancel` and re-submit with a new
  `--run-name`.

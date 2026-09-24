import json
import subprocess
from typing import Any

from sourceworldbench_benchmarks.k8s.config import K8sRunError


class KubectlClient:
    """Small wrapper around `kubectl`, injectable for tests."""

    def __init__(self, *, runner: Any | None = None) -> None:
        self._runner = runner or _subprocess_runner

    def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a `kubectl` command and optionally raise on non-zero exit."""
        proc = self._runner(args, input_bytes)
        if check and proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")
            raise K8sRunError(f"kubectl {' '.join(args)} failed: {stderr.strip()}")
        return proc

    def apply(
        self,
        manifest: dict[str, Any] | list[dict[str, Any]],
        *,
        namespace: str,
        dry_run_server: bool = False,
    ) -> None:
        """Apply a Kubernetes manifest using JSON over stdin."""
        payload = json.dumps(manifest, sort_keys=True).encode("utf-8")
        args = ["kubectl", "-n", namespace, "apply"]
        if dry_run_server:
            args.append("--dry-run=server")
        self.run([*args, "-f", "-"], input_bytes=payload)

    def delete_selector(self, resource: str, *, namespace: str, selector: str) -> None:
        """Delete Kubernetes resources by label selector."""
        self.run(
            ["kubectl", "-n", namespace, "delete", resource, "-l", selector, "--ignore-not-found=true"],
            check=True,
        )

    def get_json(self, args: list[str]) -> dict[str, Any]:
        """Run a `kubectl get ... -o json` command and parse the response."""
        proc = self.run(args)
        return json.loads(proc.stdout.decode("utf-8"))


def _subprocess_runner(args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, input=input_bytes, capture_output=True, check=False)

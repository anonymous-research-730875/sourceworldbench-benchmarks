"""Try SWE-rebench patch application strategies inside eval images."""

import subprocess
from pathlib import Path

SWE_REBENCH_REPO_DIR = "/testbed"
DOCKER_PLATFORM = "linux/amd64"
CONTAINER_PATCH_PATH = "/tmp/patch.diff"

_RUN_COMMANDS = [
    f"git apply --verbose {CONTAINER_PATCH_PATH}",
    f"git apply --verbose --reject {CONTAINER_PATCH_PATH}",
    f"patch --batch --fuzz=5 -p1 -i {CONTAINER_PATCH_PATH}",
]

_DOCKERFILE_COMMANDS = [
    "git apply {path}",
    "git apply --reject {path}",
    "patch --batch --fuzz=5 -p1 -i {path}",
]


def _image_ref(docker_image: str) -> str:
    return docker_image if ":" in docker_image else f"{docker_image}:latest"


def _run_docker(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=args,
            returncode=124,
            stdout=exc.stdout or b"",
            stderr=(exc.stderr or b"") + f"\ntimed out after {timeout}s".encode(),
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(args=args, returncode=127, stdout=b"", stderr=str(exc).encode())


def _proc_output(proc: subprocess.CompletedProcess[bytes]) -> str:
    """Merge stdout + stderr into a single string for error reporting."""
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    if proc.stdout:
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        return f"{stdout}\n{stderr}".strip() if stderr else stdout
    return stderr


def pull_image(docker_image: str, *, timeout: int = 600) -> str | None:
    """Pull the eval image. Returns an error string on failure, None on success."""
    proc = _run_docker(
        ["docker", "pull", "--platform", DOCKER_PLATFORM, _image_ref(docker_image)],
        timeout=timeout,
    )
    if proc.returncode != 0:
        return _proc_output(proc) or f"exit code {proc.returncode}"
    return None


def dockerfile_apply_command(attempt: int, container_path: str) -> str:
    """Return the Dockerfile RUN fragment for a given apply attempt (1-based)."""
    return _DOCKERFILE_COMMANDS[attempt - 1].format(path=container_path)


def resolve_apply_attempt(
    *,
    docker_image: str,
    patch_path: Path,
    apply_timeout: int = 120,
    skip_pull: bool = False,
    pull_timeout: int = 600,
) -> int | None:
    """Return the first working apply attempt (1, 2, or 3), or None if all fail."""
    if not skip_pull:
        if pull_image(docker_image, timeout=pull_timeout) is not None:
            return None

    for attempt, cmd in enumerate(_RUN_COMMANDS, start=1):
        proc = _run_docker(
            [
                "docker", "run", "--rm", "--platform", DOCKER_PLATFORM,
                "--mount", f"type=bind,src={patch_path},dst={CONTAINER_PATCH_PATH},ro",
                "-w", SWE_REBENCH_REPO_DIR,
                _image_ref(docker_image), "bash", "-lc", cmd,
            ],
            timeout=apply_timeout,
        )
        if proc.returncode == 0:
            return attempt

    return None

import logging
import subprocess
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class DockerError(Exception):
    """Failure from a `docker` invocation. Carries the process stdout/stderr."""

    def __init__(self, message: str, *, stdout: bytes = b"", stderr: bytes = b"") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


class DockerTimeout(DockerError):
    pass


class PatchApplyFailed(DockerError):
    """The container's wrapper script exited 99, signalling `git apply` failure."""


class RunResult(BaseModel):
    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""


_PATCH_APPLY_EXIT_CODE = 99

# In-container destination where `run_state` bind-mounts the patch. Single source
# of truth shared with `execution.wrap_command`, which must `git apply` this exact
# path before running the command.
CONTAINER_PATCH_PATH = "/work/patch.diff"


def _timeout_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", errors="replace")


def _invoke(args: list[str], timeout: int, *, capture_output: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Single point of contact with `subprocess.run`. Monkey-patched in tests."""
    return subprocess.run(args, capture_output=capture_output, timeout=timeout, check=False)


def run_state(
    *,
    image: str,
    script: str,
    host_results: Path,
    patch_path: Path | None = None,
    timeout: int,
    extra_mounts: list[tuple[Path, str, bool]] | None = None,
    extra_env: dict[str, str] | None = None,
) -> RunResult:
    """Run `script` inside `image` with `/results` bind-mounted, plus `patch_path`
    (when given) bind-mounted read-only at `CONTAINER_PATCH_PATH`.

    Exit code 99 with a mounted patch signals `git apply` failure and raises
    `PatchApplyFailed`; without a patch it is an ordinary command exit code.

    `extra_mounts` is a list of `(host_src, container_dst, read_only)` tuples
    appended after the fixed mounts. `extra_env` adds `--env KEY=VALUE` flags.
    Both default to empty and are intended for tools that need to layer
    additional bind-mounts (e.g. a tracer bundle) on top of the standard
    sourceworldbench-benchmarks run contract without duplicating the docker invocation.
    """
    args: list[str] = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "/bin/sh",
        "--mount",
        f"type=bind,src={host_results},dst=/results",
    ]
    if patch_path is not None:
        args += ["--mount", f"type=bind,src={patch_path},dst={CONTAINER_PATCH_PATH},ro"]
    for src, dst, read_only in extra_mounts or []:
        spec = f"type=bind,src={src},dst={dst}"
        if read_only:
            spec += ",ro"
        args += ["--mount", spec]
    for key, value in (extra_env or {}).items():
        args += ["--env", f"{key}={value}"]
    args += [image, "-c", script]

    try:
        proc = _invoke(args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DockerTimeout(
            f"docker run timed out after {timeout}s",
            stdout=_timeout_bytes(exc.output),
            stderr=_timeout_bytes(exc.stderr),
        ) from exc
    except FileNotFoundError as exc:
        raise DockerError(f"docker executable not found: {exc}") from exc

    if patch_path is not None and proc.returncode == _PATCH_APPLY_EXIT_CODE:
        raise PatchApplyFailed(
            "git apply failed inside the container",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    return RunResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def buildx_build(
    *,
    dockerfile: Path,
    context: Path,
    tag: str,
    build_args: dict[str, str],
    ssh: str | None,
    push: bool,
    timeout: int,
    platform: str | None = None,
    capture_output: bool = False,
) -> RunResult:
    """Run `docker buildx build`. Raises `DockerError` on non-zero exit."""
    args = _buildx_build_args(
        dockerfile=dockerfile,
        context=context,
        tag=tag,
        build_args=build_args,
        ssh=ssh,
        push=push,
        platform=platform,
    )

    try:
        proc = _invoke(args, timeout=timeout, capture_output=capture_output)
    except subprocess.TimeoutExpired as exc:
        raise DockerTimeout(
            f"docker buildx build timed out after {timeout}s",
            stdout=_timeout_bytes(exc.output),
            stderr=_timeout_bytes(exc.stderr),
        ) from exc
    except FileNotFoundError as exc:
        raise DockerError(f"docker executable not found: {exc}") from exc

    if proc.returncode != 0:
        raise DockerError(
            f"docker buildx build failed with exit code {proc.returncode}",
            stdout=_timeout_bytes(proc.stdout),
            stderr=_timeout_bytes(proc.stderr),
        )
    return RunResult(exit_code=0, stdout=_timeout_bytes(proc.stdout), stderr=_timeout_bytes(proc.stderr))


def _buildx_build_args(
    *,
    dockerfile: Path,
    context: Path,
    tag: str,
    build_args: dict[str, str],
    ssh: str | None,
    push: bool,
    platform: str | None,
) -> list[str]:
    args: list[str] = ["docker", "buildx", "build", "--file", str(dockerfile), "--tag", tag]
    for key, value in build_args.items():
        args += ["--build-arg", f"{key}={value}"]
    if ssh is not None:
        args += ["--ssh", ssh]
    if platform is not None:
        args += ["--platform", platform]
    if push:
        args.append("--push")
    else:
        args.append("--load")
    args.append(str(context))
    return args


def imagetools_digest(tag: str, *, timeout: int = 60) -> str | None:
    """Return the `sha256:...` digest of a pushed image, or `None` on failure."""
    args = ["docker", "buildx", "imagetools", "inspect", tag]
    try:
        proc = _invoke(args, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("imagetools inspect could not be invoked for %s", tag)
        return None
    if proc.returncode != 0:
        logger.warning(
            "imagetools inspect failed for %s: %s",
            tag,
            proc.stderr.decode(errors="replace"),
        )
        return None
    for raw in proc.stdout.decode("utf-8", errors="replace").splitlines():
        if raw.strip().startswith("Digest:"):
            return raw.split("Digest:", 1)[1].strip()
    logger.warning("no Digest line in imagetools inspect output for %s", tag)
    return None


def imagetools_tag(source: str, tag: str, *, timeout: int = 60) -> None:
    """Add `tag` to an already-pushed image via `docker buildx imagetools create`.

    Raises `DockerError` on failure.
    """
    args = ["docker", "buildx", "imagetools", "create", "--tag", tag, source]
    try:
        proc = _invoke(args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DockerTimeout(
            f"imagetools create timed out after {timeout}s",
            stdout=_timeout_bytes(exc.output),
            stderr=_timeout_bytes(exc.stderr),
        ) from exc
    except FileNotFoundError as exc:
        raise DockerError(f"docker executable not found: {exc}") from exc
    if proc.returncode != 0:
        raise DockerError(
            f"imagetools create failed with exit code {proc.returncode}",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

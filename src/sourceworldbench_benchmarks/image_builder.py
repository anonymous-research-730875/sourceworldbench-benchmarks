import logging
from pathlib import Path

from sourceworldbench_benchmarks.docker_runner import (
    DockerError,
    DockerTimeout,
    buildx_build,
    imagetools_digest,
    imagetools_tag,
)

logger = logging.getLogger(__name__)

GAR_REGISTRY = "xxx-docker.pkg.dev/xxx/ml"
# Per-instance environment images and the shared k8s runner image live under `sourceworldbench`.
# Survival is governed by tags, not the path: the cleanup policy keeps only
# versions tagged keep*/persistent*/v* (see cleanup_keep_tag).
GAR_INSTANCE_PREFIX = f"{GAR_REGISTRY}/sourceworldbench"
GAR_RUNNER_PREFIX = f"{GAR_REGISTRY}/sourceworldbench"
K8S_RUNNER_IMAGE_NAME = "sourceworldbench-runner"
K8S_RUNNER_DOCKERFILE = Path("k8s-runner.Dockerfile")

DEFAULT_PUSH_PLATFORM = "linux/amd64,linux/arm64"

_BUILDX_TIMEOUT_SECONDS = 1800
_INSPECT_TIMEOUT_SECONDS = 60
# Artifact Registry is intermittently slow to create tags on freshly pushed manifests.
_KEEP_TAG_TIMEOUT_SECONDS = 300


class BuildImageError(Exception):
    pass


def _decode_output(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def _docker_error_message(instance_id: str, exc: DockerError) -> str:
    parts = [f"docker buildx build failed for {instance_id}: {exc}"]
    stdout = _decode_output(exc.stdout)
    stderr = _decode_output(exc.stderr)
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    return "\n".join(parts)


def local_tag(instance_id: str) -> str:
    return f"sourceworldbench/{instance_id.lower()}:dev"


def remote_tag(instance_id: str, tag: str) -> str:
    return f"{GAR_INSTANCE_PREFIX}/{instance_id.lower()}:{tag}"


def cleanup_keep_tag(digest: str) -> str:
    """Tag that exempts a pushed version from the registry cleanup policy.

    The `ml` repository deletes versions older than 30 days unless one of their
    tags starts with `keep`, `persistent`, or `v`. Deriving the tag from the
    digest makes it unique per version, so re-pushing an image never leaves a
    previously pushed digest unprotected. Untagged manifests referenced by a
    kept index (arch manifests, attestations) are not deleted.
    """
    return f"keep-{digest.removeprefix('sha256:')[:12]}"


def _protect_from_cleanup(digest_ref: str, keep_tag: str) -> None:
    try:
        try:
            imagetools_tag(digest_ref, keep_tag, timeout=_KEEP_TAG_TIMEOUT_SECONDS)
        except DockerTimeout:
            # Artifact Registry intermittently stalls on tag creation; a fresh attempt usually succeeds.
            imagetools_tag(digest_ref, keep_tag, timeout=_KEEP_TAG_TIMEOUT_SECONDS)
    except DockerError as exc:
        raise BuildImageError(
            f"failed to add cleanup keep tag {keep_tag} to {digest_ref}: {exc}. "
            "Without it the pushed version is deleted by the registry cleanup policy after 30 days."
        ) from exc


def local_k8s_runner_tag(tag: str) -> str:
    return f"sourceworldbench/{K8S_RUNNER_IMAGE_NAME}:{tag}"


def remote_k8s_runner_tag(tag: str) -> str:
    return f"{GAR_RUNNER_PREFIX}/{K8S_RUNNER_IMAGE_NAME}:{tag}"


def resolve_dockerfile(*, repo_root: Path, instance_id: str) -> Path:
    candidate = repo_root / "environments" / instance_id / "Dockerfile"
    if not candidate.exists():
        raise BuildImageError(f"no Dockerfile found for instance_id {instance_id!r} at {candidate}")
    return candidate


def resolve_k8s_runner_dockerfile(*, repo_root: Path) -> Path:
    candidate = repo_root / K8S_RUNNER_DOCKERFILE
    if not candidate.exists():
        raise BuildImageError(f"no Kubernetes runner Dockerfile found at {candidate}")
    return candidate


def parse_build_arg(raw: str) -> tuple[str, str]:
    """Parse `KEY=VALUE`, splitting on the first `=`."""
    if "=" not in raw:
        raise ValueError(f"build-arg must be KEY=VALUE (got {raw!r})")
    key, value = raw.split("=", 1)
    if not key or not value:
        raise ValueError(f"build-arg must be KEY=VALUE with both parts (got {raw!r})")
    return key, value


def build_image(
    *,
    repo_root: Path,
    instance_id: str,
    push: bool,
    tag: str,
    build_args: dict[str, str],
    ssh: str | None,
    platform: str | None = None,
    capture_output: bool = False,
) -> str:
    """Build the image for `instance_id` and print `image=<ref>` on stdout.

    With `--push`, returns the digest-pinned reference when `docker buildx
    imagetools inspect` can read it, otherwise the pushed tag.
    """
    dockerfile = resolve_dockerfile(repo_root=repo_root, instance_id=instance_id)
    target_tag = remote_tag(instance_id, tag) if push else local_tag(instance_id)

    if platform is None and push:
        platform = DEFAULT_PUSH_PLATFORM

    try:
        buildx_build(
            dockerfile=dockerfile,
            context=repo_root,
            tag=target_tag,
            build_args=build_args,
            ssh=ssh,
            push=push,
            timeout=_BUILDX_TIMEOUT_SECONDS,
            platform=platform,
            capture_output=capture_output,
        )
    except DockerError as exc:
        raise BuildImageError(_docker_error_message(instance_id, exc)) from exc

    if not push:
        print(f"image={target_tag}")
        return target_tag

    digest = imagetools_digest(target_tag, timeout=_INSPECT_TIMEOUT_SECONDS)
    if digest is None:
        logger.warning("no digest for %s: cannot add a cleanup keep tag, the registry deletes it in 30 days", target_tag)
        print(f"image={target_tag}")
        return target_tag

    immutable = f"{GAR_INSTANCE_PREFIX}/{instance_id.lower()}@{digest}"
    _protect_from_cleanup(immutable, remote_tag(instance_id, cleanup_keep_tag(digest)))
    print(f"image={immutable}")
    return immutable


def build_k8s_runner_image(
    *,
    repo_root: Path,
    push: bool,
    image: str,
    build_args: dict[str, str],
    platform: str | None,
    capture_output: bool = False,
) -> str:
    """Build the Kubernetes runner image and print `image=<ref>` on stdout.

    With `--push`, returns a digest-pinned reference when the pushed image can be
    inspected, otherwise the pushed tag.
    """
    dockerfile = resolve_k8s_runner_dockerfile(repo_root=repo_root)

    try:
        buildx_build(
            dockerfile=dockerfile,
            context=repo_root,
            tag=image,
            build_args=build_args,
            ssh=None,
            push=push,
            timeout=_BUILDX_TIMEOUT_SECONDS,
            platform=platform,
            capture_output=capture_output,
        )
    except DockerError as exc:
        raise BuildImageError(_docker_error_message("k8s runner", exc)) from exc

    if not push:
        print(f"image={image}")
        return image

    digest = imagetools_digest(image, timeout=_INSPECT_TIMEOUT_SECONDS)
    if digest is None:
        logger.warning("no digest for %s: cannot add a cleanup keep tag, the registry deletes it in 30 days", image)
        print(f"image={image}")
        return image

    repository = image.rsplit(":", 1)[0]
    immutable = f"{repository}@{digest}"
    _protect_from_cleanup(immutable, f"{repository}:{cleanup_keep_tag(digest)}")
    print(f"image={immutable}")
    return immutable

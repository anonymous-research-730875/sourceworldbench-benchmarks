from pathlib import Path

import pytest
from pytest import MonkeyPatch

from sourceworldbench_benchmarks import image_builder
from sourceworldbench_benchmarks.docker_runner import DockerError
from sourceworldbench_benchmarks.image_builder import BuildImageError


def test_build_image_error_includes_docker_output(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    env_dir = tmp_path / "environments" / "example"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    def _fail_buildx(**_kwargs: object) -> None:
        raise DockerError(
            "docker buildx build failed with exit code 1",
            stdout=b"build stdout",
            stderr=b"build stderr",
        )

    monkeypatch.setattr(image_builder, "buildx_build", _fail_buildx)

    with pytest.raises(BuildImageError) as exc_info:
        image_builder.build_image(
            repo_root=tmp_path,
            instance_id="example",
            push=False,
            tag="latest",
            build_args={},
            ssh=None,
            capture_output=True,
        )

    message = str(exc_info.value)
    assert "docker buildx build failed for example" in message
    assert "stderr:\nbuild stderr" in message
    assert "stdout:\nbuild stdout" in message


def test_build_image_push_adds_cleanup_keep_tag(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Every pushed digest must get a keep-* tag or the registry cleanup policy deletes it after 30 days."""
    env_dir = tmp_path / "environments" / "example"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    monkeypatch.setattr(image_builder, "buildx_build", lambda **_kwargs: None)
    monkeypatch.setattr(image_builder, "imagetools_digest", lambda *_args, **_kwargs: "sha256:0123456789abcdef")
    keep_tags: list[tuple[str, str]] = []
    monkeypatch.setattr(image_builder, "imagetools_tag", lambda source, tag, **_kwargs: keep_tags.append((source, tag)))

    image = image_builder.build_image(
        repo_root=tmp_path,
        instance_id="example",
        push=True,
        tag="latest",
        build_args={},
        ssh=None,
        capture_output=True,
    )

    assert image == f"{image_builder.GAR_INSTANCE_PREFIX}/example@sha256:0123456789abcdef"
    assert keep_tags == [(image, f"{image_builder.GAR_INSTANCE_PREFIX}/example:keep-0123456789ab")]


def test_build_k8s_runner_image_uses_local_tag(
    monkeypatch: MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dockerfile = tmp_path / "k8s-runner.Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def _record_buildx(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(image_builder, "buildx_build", _record_buildx)

    image = image_builder.build_k8s_runner_image(
        repo_root=tmp_path,
        push=False,
        image="sourceworldbench/sourceworldbench-runner:dev",
        build_args={"UV_LINK_MODE": "copy"},
        platform=None,
    )

    assert image == "sourceworldbench/sourceworldbench-runner:dev"
    assert capsys.readouterr().out == "image=sourceworldbench/sourceworldbench-runner:dev\n"
    [call] = calls
    assert call["dockerfile"] == dockerfile
    assert call["context"] == tmp_path
    assert call["tag"] == "sourceworldbench/sourceworldbench-runner:dev"
    assert call["build_args"] == {"UV_LINK_MODE": "copy"}
    assert call["push"] is False


def test_build_k8s_runner_image_returns_digest_when_pushed(
    monkeypatch: MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dockerfile = tmp_path / "k8s-runner.Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def _record_buildx(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(image_builder, "buildx_build", _record_buildx)
    monkeypatch.setattr(image_builder, "imagetools_digest", lambda *_args, **_kwargs: "sha256:abc123")
    keep_tags: list[tuple[str, str]] = []
    monkeypatch.setattr(image_builder, "imagetools_tag", lambda source, tag, **_kwargs: keep_tags.append((source, tag)))

    image = image_builder.build_k8s_runner_image(
        repo_root=tmp_path,
        push=True,
        image="xxx-docker.pkg.dev/xxx/ml/sourceworldbench/sourceworldbench-runner:smoke",
        build_args={},
        platform="linux/amd64,linux/arm64",
    )

    assert image == "xxx-docker.pkg.dev/xxx/ml/sourceworldbench/sourceworldbench-runner@sha256:abc123"
    assert keep_tags == [(image, "xxx-docker.pkg.dev/xxx/ml/sourceworldbench/sourceworldbench-runner:keep-abc123")]
    assert capsys.readouterr().out == (
        "image=xxx-docker.pkg.dev/xxx/ml/sourceworldbench/sourceworldbench-runner@sha256:abc123\n"
    )
    assert calls[0]["tag"] == "xxx-docker.pkg.dev/xxx/ml/sourceworldbench/sourceworldbench-runner:smoke"
    assert calls[0]["push"] is True
    assert calls[0]["platform"] == "linux/amd64,linux/arm64"


def test_build_k8s_runner_image_preserves_registry_port_in_digest_ref(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    dockerfile = tmp_path / "k8s-runner.Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")

    monkeypatch.setattr(image_builder, "buildx_build", lambda **_kwargs: None)
    monkeypatch.setattr(image_builder, "imagetools_digest", lambda *_args, **_kwargs: "sha256:def456")
    monkeypatch.setattr(image_builder, "imagetools_tag", lambda *_args, **_kwargs: None)

    image = image_builder.build_k8s_runner_image(
        repo_root=tmp_path,
        push=True,
        image="registry.example.com:5000/sourceworldbench/runner:smoke",
        build_args={},
        platform="linux/amd64",
    )

    assert image == "registry.example.com:5000/sourceworldbench/runner@sha256:def456"

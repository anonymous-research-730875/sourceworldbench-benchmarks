import sys
from pathlib import Path

import typer

from sourceworldbench_benchmarks.image_builder import BuildImageError, build_image, parse_build_arg

app = typer.Typer(add_completion=False)


@app.command("build-image")
def build_image_command(
    instance_id: str = typer.Option(..., "--instance-id", help="Benchmark instance id under environments/."),
    push: bool = typer.Option(False, "--push", help="Push the built image to the configured registry."),
    tag: str = typer.Option("latest", "--tag", help="Image tag to build and optionally push."),
    ssh: str | None = typer.Option(
        None,
        "--ssh",
        metavar="VALUE",
        help="Forwarded to docker buildx --ssh, e.g. 'default' for private GitHub repos.",
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
        help="Forwarded to docker buildx --platform. Defaults to linux/amd64 when --push is set.",
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Capture Docker build output instead of streaming it."),
) -> None:
    """Build (and optionally push) an instance's container image."""
    try:
        build_args = dict(parse_build_arg(raw) for raw in build_arg)
    except ValueError as exc:
        print(f"error: invalid --build-arg: {exc}", file=sys.stderr)
        raise typer.Exit(code=2) from exc
    try:
        build_image(
            repo_root=Path.cwd(),
            instance_id=instance_id,
            push=push,
            tag=tag,
            build_args=build_args,
            ssh=ssh,
            platform=platform,
            capture_output=quiet,
        )
    except BuildImageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise typer.Exit(code=1) from exc

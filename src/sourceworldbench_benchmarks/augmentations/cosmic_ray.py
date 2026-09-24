import json
import random
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import typer

from sourceworldbench_benchmarks.augmentations.docker_worktree import temporary_worktree_from_image
from sourceworldbench_benchmarks.augmentations.runner import AugmentationError, fail, run_augmentations
from sourceworldbench_benchmarks.dataset_io import DEFAULT_DATASET_REPO_ID


def cosmic_ray_cmd(
    image: str | None = typer.Option(
        None,
        "--image",
        help="Local or remote Docker image with the repo checked out at /app.",
    ),
    repo: Path | None = typer.Option(
        None,
        "--repo",
        help="Path to a clean local git working tree.",
    ),
    base_commit: str | None = typer.Option(
        None,
        "--base-commit",
        help="Git revision to check out before augmenting. Defaults to the current checkout.",
    ),
    scope: list[str] = typer.Option(
        [],
        "--scope",
        help="Path(s) within the repo that CosmicRay is allowed to mutate. Repeatable.",
    ),
    out: Path = typer.Option(..., "--out", help="Directory where patch files and manifest.jsonl are written."),
    limit: int = typer.Option(10, "--limit", min=1, help="Maximum number of mutation patches to generate."),
    seed: int | None = typer.Option(None, "--seed", help="Shuffle mutations deterministically with this seed."),
    operator: list[str] = typer.Option(
        [],
        "--operator",
        help="CosmicRay operator name to include. Repeatable. Accepts exact names or suffixes.",
    ),
    restore: bool = typer.Option(
        True,
        "--restore/--no-restore",
        help="Return to the original checkout after generating patches.",
    ),
) -> None:
    """Generate Python mutation patches with CosmicRay without running tests."""
    manifest_entries: list[dict[str, Any]] = []
    mutate_many = _build_mutate_many(
        scope=scope,
        limit=limit,
        seed=seed,
        operators=operator,
        manifest_entries=manifest_entries,
    )
    try:
        if repo is not None and image is not None:
            raise AugmentationError("provide only one of --image or --repo")
        if repo is not None:
            run_augmentations(
                repo=repo,
                base_commit=base_commit,
                scope=scope,
                mutate_many=mutate_many,
                out=out,
                restore=restore,
            )
        elif image is not None:
            if base_commit is not None:
                raise AugmentationError("--base-commit is not supported with --image")
            restore = False
            with temporary_worktree_from_image(image) as repo_path:
                run_augmentations(
                    repo=repo_path,
                    base_commit=base_commit,
                    scope=scope,
                    mutate_many=mutate_many,
                    out=out,
                    restore=restore,
                )
        else:
            raise AugmentationError("provide --image or --repo")
        _write_manifest(out, manifest_entries)
    except AugmentationError as exc:
        fail(str(exc))


def _build_mutate_many(
    *,
    scope: list[str],
    limit: int,
    seed: int | None,
    operators: list[str],
    manifest_entries: list[dict[str, Any]] | None = None,
) -> Callable[[Path], Iterable[None]]:
    def mutate_many(repo: Path) -> Iterable[None]:
        from cosmic_ray import plugins
        from cosmic_ray.commands.init import _all_work_items
        from cosmic_ray.modules import find_modules
        from cosmic_ray.mutating import mutate_code

        module_paths = _module_paths(repo, scope, find_modules)
        work_items = list(_all_work_items(module_paths, {}))
        if operators:
            work_items = [item for item in work_items if _operator_matches(item.mutations[0].operator_name, operators)]
        if seed is not None:
            random.Random(seed).shuffle(work_items)

        generated = 0
        for item in work_items:
            mutation = item.mutations[0]
            module_path = mutation.module_path
            operator_class = plugins.get_operator(mutation.operator_name)
            cosmic_operator = operator_class(**mutation.operator_args)
            original_code = module_path.read_text(encoding="utf-8")
            mutated_code = mutate_code(original_code, cosmic_operator, mutation.occurrence)
            if mutated_code is None:
                continue

            module_path.write_text(mutated_code, encoding="utf-8")
            generated += 1
            if manifest_entries is not None:
                manifest_entries.append(
                    {
                        "index": generated,
                        "patch": f"{generated:06d}.patch",
                        "source": "cosmic-ray",
                        "file": str(module_path.relative_to(repo)),
                        "operator": mutation.operator_name,
                        "occurrence": mutation.occurrence,
                        "start_pos": list(mutation.start_pos),
                        "end_pos": list(mutation.end_pos),
                        "definition_name": mutation.definition_name,
                    }
                )
            yield None
            if generated >= limit:
                return

        if generated == 0:
            raise AugmentationError("CosmicRay produced no matching mutations")

    return mutate_many


def _write_manifest(out: Path, entries: list[Mapping[str, Any]]) -> None:
    manifest_path = out / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as manifest:
        for entry in entries:
            manifest.write(json.dumps(entry) + "\n")


def _module_paths(repo: Path, scope: list[str], find_modules: Callable[[list[Path]], Iterable[Path]]) -> list[Path]:
    roots = [repo / path for path in scope] if scope else [repo]
    missing = [path for path in roots if not path.exists()]
    if missing:
        missing_display = ", ".join(str(path) for path in missing)
        raise AugmentationError(f"scope path does not exist: {missing_display}")
    module_paths = sorted(set(find_modules(roots)))
    if not module_paths:
        raise AugmentationError("CosmicRay found no Python modules to mutate")
    return module_paths


def _operator_matches(operator_name: str, requested: list[str]) -> bool:
    return any(operator_name == name or operator_name.endswith(f"/{name}") for name in requested)


def cosmic_ray_hf_cmd(
    base_instance_id: str = typer.Option(
        ..., "--base-instance-id", help="instance_id of the bare base row in the HF dataset."
    ),
    suffix: str | None = typer.Option(
        None, "--suffix", help="Suffix replacing the base state's `base` segment. Defaults to a UTC timestamp."
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing row with the same instance_id."),
    in_path: str = typer.Option(
        DEFAULT_DATASET_REPO_ID, "--in", help="Dataset to augment: a local JSONL file or an HF dataset repo id."
    ),
    push: bool = typer.Option(True, "--push/--no-push", help="Push the dataset to the hub after augmenting."),
    out_path: str | None = typer.Option(
        None, "--out", help="Write the resulting dataset to this JSONL path. Skipped when omitted."
    ),
    scope: list[str] = typer.Option([], "--scope", help="Path(s) CosmicRay is allowed to mutate. Repeatable."),
    seed: int | None = typer.Option(None, "--seed", help="Shuffle mutations deterministically with this seed."),
    operator: list[str] = typer.Option(
        [], "--operator", help="CosmicRay operator name to include. Repeatable."
    ),
) -> None:
    """Run one CosmicRay mutation against the row's container and push a new augmented row.

    # TODO: support adding multiple rows at once (one per mutation) so cosmic-ray
    # can leverage its full `limit` parameter in the HF workflow.
    """
    from sourceworldbench_benchmarks.augmentations.runner import AugmentationError
    from sourceworldbench_benchmarks.commands.augment_hf import run_hf_augmentation

    def mutate(repo: Path) -> None:
        gen = _build_mutate_many(scope=scope, limit=1, seed=seed, operators=operator)(repo)
        try:
            next(gen)
        except StopIteration as exc:
            raise AugmentationError("CosmicRay produced no matching mutations") from exc

    run_hf_augmentation(
        base_instance_id=base_instance_id,
        suffix=suffix,
        overwrite=overwrite,
        scope=scope,
        mutate=mutate,
        in_path=in_path,
        push=push,
        out_path=out_path,
    )

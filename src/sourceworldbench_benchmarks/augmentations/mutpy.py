"""MutPy-based augmentation: mutate ``.py`` source files and capture a git diff.

We use MutPy as a library (not its CLI): parse each in-scope file, enumerate
every first-order mutant from the standard operator set (minus any banned
operators), shuffle the pool with a seeded RNG, and write the first pick back
to disk. ``runner.run_augmentation`` captures the diff and restores the worktree.
"""

from __future__ import annotations

import ast
import logging
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path

import typer

from sourceworldbench_benchmarks.augmentations.docker_worktree import temporary_worktree_from_image
from sourceworldbench_benchmarks.augmentations.runner import AugmentationError, fail, run_augmentation
from sourceworldbench_benchmarks.dataset_io import DEFAULT_DATASET_REPO_ID

logger = logging.getLogger(__name__)

# MutPy 3-letter operator codes we never apply (constant replacement is too noisy).
BANNED_OPERATORS: frozenset[str] = frozenset({"CRP"})


def _require_mutpy():
    """Lazy MutPy import so the CLI works without the optional ``augmentation`` group.

    The fourth return value is stdlib ``ast.unparse``, used to regenerate source from a
    mutated AST.
    """
    try:
        from mutpy import controller, operators
        from mutpy.utils import create_ast
    except ImportError as exc:
        raise AugmentationError(
            "mutpy is not installed. Sync augmentation deps: uv sync --group augmentation"
        ) from exc
    return controller, operators, create_ast, ast.unparse


def _active_operators(all_operators: list) -> list:
    """Return standard operators minus ``BANNED_OPERATORS``."""
    kept = [op for op in all_operators if op.name() not in BANNED_OPERATORS]
    if not kept:
        raise AugmentationError("every standard operator is banned; nothing left to mutate")
    return kept


@dataclass(frozen=True)
class AppliedMutation:
    """Record of one mutation that was written to disk."""

    number: int  # 1-based position in this file's pre-shuffle MutPy enumeration
    operator: str  # short operator name, e.g. ``COD``, ``CRP``
    module: str  # dotted path, e.g. ``flask_wtf.csrf``
    file: str


def _resolve_scope_entry(repo: Path, entry: str) -> list[Path]:
    """Expand one ``--scope`` value (file or directory) to ``.py`` paths.

    Tries ``repo`` and ``repo/src`` so flat and src-layout projects both work.
    """
    rel = Path(entry)
    for base in (repo, repo / "src"):
        path = base / rel
        if path.is_file() and path.suffix == ".py":
            return [path.resolve()]
        if path.is_dir():
            files = sorted(p.resolve() for p in path.rglob("*.py"))
            if files:
                return files
    raise AugmentationError(f"could not resolve --scope {entry!r} under {repo}")


def resolve_source_files(repo: Path, scope: list[str], *, exclude: list[str] | None = None) -> list[Path]:
    """Return deduplicated, sorted ``.py`` paths from ``--scope`` entries."""
    repo = repo.resolve()
    if not scope:
        raise AugmentationError("provide --scope")
    seen: set[str] = set()
    files: list[Path] = []
    for entry in scope:
        for path in _resolve_scope_entry(repo, entry):
            key = path.as_posix()
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    if exclude:
        excluded: set[str] = set()
        for entry in exclude:
            excluded.update(p.as_posix() for p in _resolve_scope_entry(repo, entry))
        before = len(files)
        files = [path for path in files if path.as_posix() not in excluded]
        dropped = before - len(files)
        if dropped:
            logger.info("excluded %d file(s) via --exclude", dropped)
    if not files:
        raise AugmentationError("no .py files left after applying --scope and --exclude")
    return files


def apply_mutations(
    repo: Path,
    *,
    scope: list[str],
    exclude: list[str],
    file_fraction: float,
    compile_check: bool,
    percentage: int,
    seed: int | None = None,
) -> list[AppliedMutation]:
    """Pick a seeded-random MutPy mutation per sampled file and write it to disk.

    Sample ``k = max(1, ceil(file_fraction * N))`` files from the in-scope set,
    then for each one enumerate its mutants (optionally dropping those whose
    rendered source does not compile), shuffle with ``seed``, and write the
    first pick. ``file_fraction = 0`` mutates one file; ``file_fraction = 1``
    mutates every in-scope file.

    ``percentage`` is MutPy's per-site sampler rate (1..100); we seed the global
    RNG since MutPy's sampler uses it. When ``seed`` is omitted, uses the
    current Unix time in seconds.
    """
    controller, operators_module, create_ast, to_source = _require_mutpy()
    effective_seed = seed if seed is not None else int(time.time())
    logger.info("mutpy seed: %d", effective_seed)
    rng = random.Random(effective_seed)
    random.seed(effective_seed)
    operators = _active_operators(operators_module.standard_operators)
    mutator = controller.FirstOrderMutator(operators, percentage)
    repo = repo.resolve()
    source_files = resolve_source_files(repo, scope, exclude=exclude)

    k = max(1, math.ceil(file_fraction * len(source_files)))
    k = min(k, len(source_files))
    sampled = sorted(rng.sample(source_files, k))

    applied: list[AppliedMutation] = []
    for path in sampled:
        tree = create_ast(path.read_text(encoding="utf-8"))
        candidates: list[tuple[int, str, str]] = []
        for index, (mutations, mutant_ast) in enumerate(mutator.mutate(tree, None, module=None), 1):
            source = to_source(mutant_ast)
            if not source.endswith("\n"):
                source += "\n"
            if compile_check:
                try:
                    compile(source, str(path), "exec")
                except SyntaxError:
                    continue
            candidates.append((index, mutations[0].operator.name(), source))

        if not candidates:
            logger.warning("no mutations available for %s; skipping", path)
            continue

        rng.shuffle(candidates)
        number, op_name, source = candidates[0]
        path.write_text(source, encoding="utf-8")
        module = path.relative_to(repo).with_suffix("").as_posix().replace("/", ".")
        applied.append(AppliedMutation(number=number, operator=op_name, module=module, file=str(path)))

    if not applied:
        raise AugmentationError(
            "no compile-valid mutation generated for any sampled file"
            if compile_check
            else "no mutations generated for any sampled file"
        )
    return applied


def mutpy_hf_cmd(
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
    scope: list[str] = typer.Option(
        ..., "--scope", help="Repo-relative .py file(s) or directory(ies) to mutate. Repeatable."
    ),
    exclude: list[str] = typer.Option([], "--exclude", help="Repo-relative paths to skip. Repeatable."),
    file_fraction: float = typer.Option(0.0, "--file-fraction", min=0.0, max=1.0),
    compile_check: bool = typer.Option(False, "--compile-check/--no-compile-check"),
    percentage: int = typer.Option(100, "--percentage", min=1, max=100),
    seed: int | None = typer.Option(None, "--seed"),
) -> None:
    """Run mutpy against the row's container and push a new augmented row."""
    from sourceworldbench_benchmarks.commands.augment_hf import run_hf_augmentation

    def mutate(repo: Path) -> None:
        apply_mutations(
            repo,
            scope=scope,
            exclude=exclude,
            file_fraction=file_fraction,
            compile_check=compile_check,
            percentage=percentage,
            seed=seed,
        )

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


def mutpy_cmd(
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
        help="Repo-relative .py file(s) or directory(ies) to mutate. Repeatable.",
    ),
    exclude: list[str] = typer.Option(
        [],
        "--exclude",
        help="Repo-relative .py file(s) or directory(ies) to skip. Repeatable.",
    ),
    out: Path | None = typer.Option(None, "--out", help="Write the diff here. Defaults to stdout."),
    file_fraction: float = typer.Option(
        0.0,
        "--file-fraction",
        min=0.0,
        max=1.0,
        help=(
            "Fraction of in-scope files to mutate (rounded up, minimum 1)."
            " 0 = one file (default); 1 = every in-scope file."
        ),
    ),
    compile_check: bool = typer.Option(
        False,
        "--compile-check/--no-compile-check",
        help="Drop mutants whose rendered source fails to compile before shuffling.",
    ),
    percentage: int = typer.Option(
        100,
        "--percentage",
        min=1,
        max=100,
        help="MutPy per-site sampling rate. Lower = smaller pool / faster on large scopes.",
    ),
    seed: int | None = typer.Option(
        None,
        "--seed",
        help="Random seed for file/mutation sampling. Defaults to current Unix time.",
    ),
    restore: bool = typer.Option(
        True,
        "--restore/--no-restore",
        help="Reset the working tree after capturing the diff.",
    ),
) -> None:
    """Typer entrypoint: ``sourceworldbench-benchmarks augment mutpy``."""
    def mutate(repo_path: Path) -> None:
        applied_list = apply_mutations(
            repo_path,
            scope=scope,
            exclude=exclude,
            file_fraction=file_fraction,
            compile_check=compile_check,
            percentage=percentage,
            seed=seed,
        )
        for a in applied_list:
            logger.info("applied mutation #%d (%s) to %s", a.number, a.operator, a.file)

    try:
        if repo is not None and image is not None:
            raise AugmentationError("provide only one of --image or --repo")
        if repo is not None:
            run_augmentation(
                repo=repo,
                base_commit=base_commit,
                scope=scope,
                mutate=mutate,
                out=out,
                restore=restore,
            )
        elif image is not None:
            if base_commit is not None:
                raise AugmentationError("--base-commit is not supported with --image")
            restore = False
            with temporary_worktree_from_image(image) as repo_path:
                run_augmentation(
                    repo=repo_path,
                    base_commit=base_commit,
                    scope=scope,
                    mutate=mutate,
                    out=out,
                    restore=restore,
                )
        else:
            raise AugmentationError("provide --image or --repo")
    except AugmentationError as exc:
        fail(str(exc))

# Augmentations

Each augmentation is a Typer command that mutates a git working tree and
returns the resulting diff. The framework in `runner.py` handles the plumbing
(cleanliness check, diff capture, restore) so a new augmenter only needs to
describe *how* to mutate the tree.

Use `run_augmentation` for tools that naturally produce one patch, such as
formatters and linters. Use `run_augmentations` for tools that naturally produce
many independent patches, such as mutation testers; in that case `--out` should
be an output directory containing numbered patch files.

## Target repo: `--repo` or `--image`

Every augmenter accepts exactly one of:

- **`--repo`**: path to a clean local git working tree (mutates in place).
- **`--image`**: local or remote Docker image with the repo checked out at
  `/app` (e.g. `sourceworldbench/hvac__09902dea:dev` or a digest-pinned registry ref).

When `--image` is used, `docker_worktree.py` extracts `/app` into a temp
directory, runs the augmentation there, and deletes the copy on exit. The image
itself is never modified. The checkout baked into the image is the base commit,
so `--base-commit` is rejected. Restore is a no-op on a disposable copy, so
`--restore` is forced off.

```bash
# local repo
sourceworldbench-benchmarks augment ruff --repo ~/projects/hvac --scope hvac --out patch.diff

# environment image
sourceworldbench-benchmarks augment mutpy --image sourceworldbench/hvac__09902dea:dev --scope hvac --out patch.diff
```

## Adding a new augmentation

1. **Create `augmentations/<name>.py`**. Define a `mutate(repo: Path) -> None`
   function that performs the change in place. How you do that is up to you —
   shell out to a tool, edit files in Python, apply a precomputed patch, etc.
2. **Define a Typer command function** that builds the mutate and calls
   `run_augmentation` (or `run_augmentations`):

   ```python
   from sourceworldbench_benchmarks.augmentations.docker_worktree import temporary_worktree_from_image
   from sourceworldbench_benchmarks.augmentations.runner import AugmentationError, fail, run_augmentation

   def <name>_cmd(
       image: str | None = typer.Option(None, "--image"),
       repo: Path | None = typer.Option(None, "--repo"),
       base_commit: str | None = typer.Option(None, "--base-commit"),
       scope: list[str] = typer.Option([], "--scope"),
       out: Path | None = typer.Option(None, "--out"),
       restore: bool = typer.Option(True, "--restore/--no-restore"),
       # ...your augmenter-specific options...
   ) -> None:
       mutate = build_mutate(...)
       try:
           if repo is not None and image is not None:
               raise AugmentationError("provide only one of --image or --repo")
           if repo is not None:
               run_augmentation(
                   repo=repo, base_commit=base_commit, scope=scope, out=out,
                   restore=restore, mutate=mutate,
               )
           elif image is not None:
               if base_commit is not None:
                   raise AugmentationError("--base-commit is not supported with --image")
               restore = False
               with temporary_worktree_from_image(image) as repo_path:
                   run_augmentation(
                       repo=repo_path, base_commit=base_commit, scope=scope,
                       out=out, restore=restore, mutate=mutate,
                   )
           else:
               raise AugmentationError("provide --image or --repo")
       except AugmentationError as exc:
           fail(str(exc))
   ```

3. **Register it** by adding one line to the `COMMANDS` dict in
   `augmentations/__init__.py`:

   ```python
   from sourceworldbench_benchmarks.augmentations.<name> import <name>_cmd

   COMMANDS = {
       "mutpy": mutpy_cmd,
       "ruff": ruff_cmd,
       "<name>": <name>_cmd,
   }
   ```

That's it, `sourceworldbench-benchmarks augment <name> --help` will pick it up.

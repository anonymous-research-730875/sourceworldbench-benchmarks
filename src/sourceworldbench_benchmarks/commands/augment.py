import typer

from sourceworldbench_benchmarks.augmentations import COMMANDS

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Build augmentation patches from a target repo.",
)
for _name, _fn in COMMANDS.items():
    app.command(_name)(_fn)

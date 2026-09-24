import typer

from sourceworldbench_benchmarks.commands.add_base import add_base
from sourceworldbench_benchmarks.commands.add_datapoint import add_datapoint
from sourceworldbench_benchmarks.commands.augment import app as augment_local_app
from sourceworldbench_benchmarks.commands.augment_hf import app as augment_app
from sourceworldbench_benchmarks.commands.collect import app as collect_app
from sourceworldbench_benchmarks.commands.execute import execute, execute_trace, selfcheck_trace_bundle
from sourceworldbench_benchmarks.commands.git_history import app as git_history_app
from sourceworldbench_benchmarks.commands.hf import app as hf_app
from sourceworldbench_benchmarks.commands.image import app as image_app
from sourceworldbench_benchmarks.commands.k8s import app as k8s_app
from sourceworldbench_benchmarks.commands.pairing import pair
from sourceworldbench_benchmarks.execution_tracer.cli import app as execution_tracer_app

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Collect, augment, package, and publish sourceworldbench-benchmarks rows.",
)
app.command("add-base")(add_base)
app.command("add-datapoint")(add_datapoint)
app.command("pair")(pair)
app.add_typer(collect_app)
app.add_typer(git_history_app)
app.command("execute", hidden=True)(execute)
app.command("execute-trace", hidden=True)(execute_trace)
app.command("selfcheck-trace-bundle", hidden=True)(selfcheck_trace_bundle)
app.add_typer(k8s_app, name="k8s")
app.add_typer(image_app)
app.add_typer(augment_app, name="augment")
app.add_typer(augment_local_app, name="augment-local")
app.add_typer(hf_app, name="hf")
app.add_typer(execution_tracer_app, name="execution-tracer")


def main() -> None:
    app()


if __name__ == "__main__":
    main()

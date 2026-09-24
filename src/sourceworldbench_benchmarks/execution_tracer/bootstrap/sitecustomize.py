"""Auto-imported by Python at startup when this directory is on PYTHONPATH.

Loads the sibling `tracer.py` file under the stable module name
`sourceworldbench_tracer`. The tracer source registers its own hooks at import time
(pytest plugin functions, or `unittest.TestCase.run` monkey-patch),
which is what activates tracing for the rest of the process.

Compatible with Python 3.5+: the four tracer sources target that floor
and so does this shim.
"""
import importlib.util
import os
import sys


def _load_sourceworldbench_tracer():
    here = os.path.dirname(os.path.abspath(__file__))
    tracer_path = os.path.join(here, "tracer.py")
    if not os.path.exists(tracer_path):
        return
    spec = importlib.util.spec_from_file_location("sourceworldbench_tracer", tracer_path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["sourceworldbench_tracer"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # Surface the failure on stderr but never abort interpreter
        # startup -- the host run should still produce *some* test
        # output even if tracer initialization fails.
        import traceback
        sys.stderr.write("[sourceworldbench-tracer] failed to load tracer.py:\n")
        traceback.print_exc(file=sys.stderr)
        sys.modules.pop("sourceworldbench_tracer", None)


_load_sourceworldbench_tracer()

"""
tests/test_harness.py — Unit tests for harness functions.

Tests cover: patch directory extraction, eval script patching (env vars,
compress flag, custom output path, Django wrapper), gzip trace loading,
binary container extraction (mocked), and resume logic.

All tests are pure-logic and do NOT require Docker.
"""

import gzip
import json
import os
from unittest.mock import MagicMock, patch

from sourceworldbench_benchmarks.execution_tracer.legacy_swebench_harness.run_traced import (
    _extract_binary_from_container,
    _extract_file_from_container,
    _extract_patch_dirs,
    _extract_trace_from_container,
    _find_existing_trace,
    _is_django_instance,
    _is_session_trace_instance,
    _is_sympy_instance,
    patch_eval_script,
)
from sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_traced import run_trace

# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------

SAMPLE_PATCH = """\
--- a/src/flask/app.py
+++ b/src/flask/app.py
@@ -100,3 +100,5 @@
 def hello():
     pass
+def goodbye():
+    pass
--- a/src/flask/helpers.py
+++ b/src/flask/helpers.py
@@ -10,1 +10,2 @@
 import os
+import sys
"""

SAMPLE_DJANGO_PATCH = """\
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -1,1 +1,2 @@
 import copy
+import functools
"""

SAMPLE_SYMPY_PATCH = """\
--- a/sympy/geometry/point.py
+++ b/sympy/geometry/point.py
@@ -100,3 +100,5 @@
 class Point3D(Point):
     pass
+    def distance(self, other):
+        pass
"""


def _make_test_spec(repo="pytest-dev/pytest", eval_script=None):
    """Create a mock TestSpec."""
    spec = MagicMock()
    spec.repo = repo
    spec.instance_id = "pytest-dev__pytest-10356"
    if eval_script is None:
        eval_script = (
            "#!/bin/bash\n"
            "set -uxo pipefail\n"
            "cd /testbed\n"
            "pytest tests/test_foo.py -v\n"
        )
    spec.eval_script = eval_script
    return spec


def _make_pred(patch_text=SAMPLE_PATCH):
    """Create a mock prediction dict."""
    return {"model_name_or_path": "gold", "instance_id": "pytest-dev__pytest-10356",
            "model_patch": patch_text}


# ---------------------------------------------------------------------------
#  Tests: _extract_patch_dirs
# ---------------------------------------------------------------------------

class TestExtractPatchDirs:
    def test_extracts_directories_from_patch(self):
        dirs = _extract_patch_dirs(SAMPLE_PATCH)
        assert "src/flask" in dirs

    def test_deduplicates(self):
        dirs = _extract_patch_dirs(SAMPLE_PATCH)
        # Both app.py and helpers.py are under src/flask
        assert dirs.count("src/flask") == 1

    def test_empty_patch(self):
        assert _extract_patch_dirs("") == []

    def test_dev_null_ignored(self):
        patch = "--- /dev/null\n+++ b/new_file.py\n"
        dirs = _extract_patch_dirs(patch)
        assert "/dev/null" not in str(dirs)


# ---------------------------------------------------------------------------
#  Tests: patch_eval_script
# ---------------------------------------------------------------------------

class TestPatchEvalScript:
    def test_contains_all_env_vars(self):
        spec = _make_test_spec()
        pred = _make_pred()
        result = patch_eval_script(spec, pred, trace_level="function")
        assert "SWEBENCH_TRACE_ENABLED=1" in result
        assert "SWEBENCH_TRACE_LEVEL=function" in result
        assert "SWEBENCH_TRACE_OUTPUT=" in result
        assert "SWEBENCH_REPO_DIR=/testbed" in result
        assert "SWEBENCH_TRACE_COMPRESS=" in result

    def test_compress_true(self):
        spec = _make_test_spec()
        pred = _make_pred()
        result = patch_eval_script(spec, pred, compress=True)
        assert "SWEBENCH_TRACE_COMPRESS=1" in result

    def test_compress_false(self):
        spec = _make_test_spec()
        pred = _make_pred()
        result = patch_eval_script(spec, pred, compress=False)
        assert "SWEBENCH_TRACE_COMPRESS=0" in result

    def test_custom_output_path(self):
        spec = _make_test_spec()
        pred = _make_pred()
        result = patch_eval_script(
            spec, pred, output_path="/testbed/trace_output_pre.json",
        )
        assert "SWEBENCH_TRACE_OUTPUT=/testbed/trace_output_pre.json" in result

    def test_default_output_path(self):
        spec = _make_test_spec()
        pred = _make_pred()
        result = patch_eval_script(spec, pred)
        assert "SWEBENCH_TRACE_OUTPUT=/testbed/trace_output.json" in result

    def test_trace_paths_from_patch(self):
        spec = _make_test_spec()
        pred = _make_pred()
        result = patch_eval_script(spec, pred)
        assert "SWEBENCH_TRACE_PATHS=src/flask" in result

    def test_env_vars_after_pipefail(self):
        spec = _make_test_spec()
        pred = _make_pred()
        result = patch_eval_script(spec, pred)
        lines = result.split("\n")
        pipefail_idx = next(i for i, line in enumerate(lines) if "set -uxo pipefail" in line)
        trace_idx = next(i for i, line in enumerate(lines) if "SWEBENCH_TRACE_ENABLED" in line)
        assert trace_idx > pipefail_idx

    def test_django_wrapper_replacement(self):
        eval_script = (
            "#!/bin/bash\n"
            "set -uxo pipefail\n"
            "cd /testbed\n"
            "python -W ignore::DeprecationWarning ./tests/runtests.py --verbosity 2\n"
        )
        spec = _make_test_spec(repo="django/django", eval_script=eval_script)
        pred = _make_pred(SAMPLE_DJANGO_PATCH)
        result = patch_eval_script(spec, pred)
        assert "python /testbed/_trace_wrapper.py ./tests/runtests.py" in result
        assert "SWEBENCH_TRACE_DJANGO=1" in result

    def test_line_level(self):
        spec = _make_test_spec()
        pred = _make_pred()
        result = patch_eval_script(spec, pred, trace_level="line")
        assert "SWEBENCH_TRACE_LEVEL=line" in result

    def test_sympy_wrapper_replacement(self):
        eval_script = (
            "#!/bin/bash\n"
            "set -uxo pipefail\n"
            "cd /testbed\n"
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' "
            "bin/test -C --verbose sympy/geometry/tests/test_point.py\n"
        )
        spec = _make_test_spec(repo="sympy/sympy", eval_script=eval_script)
        pred = _make_pred(SAMPLE_SYMPY_PATCH)
        result = patch_eval_script(spec, pred)
        assert "python /testbed/_trace_wrapper.py bin/test" in result
        assert "SWEBENCH_TRACE_DJANGO=1" in result
        assert "PYTHONPATH=/testbed" in result

    def test_sympy_does_not_affect_pytest(self):
        """Ensure bin/test replacement only fires for sympy repos."""
        eval_script = (
            "#!/bin/bash\n"
            "set -uxo pipefail\n"
            "cd /testbed\n"
            "pytest tests/\n"
        )
        spec = _make_test_spec(repo="pytest-dev/pytest", eval_script=eval_script)
        pred = _make_pred()
        result = patch_eval_script(spec, pred)
        assert "_trace_wrapper.py" not in result
        assert "SWEBENCH_TRACE_DJANGO" not in result


# ---------------------------------------------------------------------------
#  Tests: instance type detection
# ---------------------------------------------------------------------------

class TestInstanceTypeDetection:
    def test_django_instance(self):
        spec = _make_test_spec(repo="django/django")
        assert _is_django_instance(spec) is True
        assert _is_sympy_instance(spec) is False
        assert _is_session_trace_instance(spec) is True

    def test_sympy_instance(self):
        spec = _make_test_spec(repo="sympy/sympy")
        assert _is_django_instance(spec) is False
        assert _is_sympy_instance(spec) is True
        assert _is_session_trace_instance(spec) is True

    def test_pytest_instance(self):
        spec = _make_test_spec(repo="pytest-dev/pytest")
        assert _is_django_instance(spec) is False
        assert _is_sympy_instance(spec) is False
        assert _is_session_trace_instance(spec) is False

    def test_matplotlib_is_pytest(self):
        spec = _make_test_spec(repo="matplotlib/matplotlib")
        assert _is_session_trace_instance(spec) is False


# ---------------------------------------------------------------------------
#  Tests: _find_existing_trace
# ---------------------------------------------------------------------------

class TestFindExistingTrace:
    def test_finds_json(self, tmp_path):
        (tmp_path / "trace_output.json").write_text("{}")
        assert _find_existing_trace(tmp_path) == tmp_path / "trace_output.json"

    def test_finds_json_gz(self, tmp_path):
        (tmp_path / "trace_output.json.gz").write_bytes(b"\x1f\x8b")
        assert _find_existing_trace(tmp_path) == tmp_path / "trace_output.json.gz"

    def test_prefers_gz(self, tmp_path):
        (tmp_path / "trace_output.json").write_text("{}")
        (tmp_path / "trace_output.json.gz").write_bytes(b"\x1f\x8b")
        result = _find_existing_trace(tmp_path)
        assert result == tmp_path / "trace_output.json.gz"

    def test_none_when_missing(self, tmp_path):
        assert _find_existing_trace(tmp_path) is None

    def test_custom_basename(self, tmp_path):
        (tmp_path / "trace_output_pre.json").write_text("{}")
        assert _find_existing_trace(tmp_path, "trace_output_pre") == tmp_path / "trace_output_pre.json"


# ---------------------------------------------------------------------------
#  Tests: load_traces (gzip support)
# ---------------------------------------------------------------------------

class TestLoadTraces:
    def test_load_json(self, tmp_path):
        from sourceworldbench_benchmarks.execution_tracer.scripts.run_evaluation import load_traces
        # Create directory structure: trace_dir/run_id/instance_id/trace_output.json
        inst_dir = tmp_path / "run_1" / "pytest-dev__pytest-10356"
        inst_dir.mkdir(parents=True)
        trace_data = {"tests": {"test_foo": {"functions": []}}, "tracer_version": "0.3.0"}
        (inst_dir / "trace_output.json").write_text(json.dumps(trace_data))

        traces, trace_dirs = load_traces(str(tmp_path))
        assert "pytest-dev__pytest-10356" in traces
        assert traces["pytest-dev__pytest-10356"]["tracer_version"] == "0.3.0"

    def test_load_gzip(self, tmp_path):
        from sourceworldbench_benchmarks.execution_tracer.scripts.run_evaluation import load_traces
        inst_dir = tmp_path / "run_1" / "pytest-dev__pytest-10356"
        inst_dir.mkdir(parents=True)
        trace_data = {"tests": {"test_foo": {"functions": []}}, "tracer_version": "0.3.0"}
        with gzip.open(inst_dir / "trace_output.json.gz", "wt", encoding="utf-8") as f:
            json.dump(trace_data, f)

        traces, trace_dirs = load_traces(str(tmp_path))
        assert "pytest-dev__pytest-10356" in traces
        assert traces["pytest-dev__pytest-10356"]["tracer_version"] == "0.3.0"

    def test_prefers_gz_over_json(self, tmp_path):
        from sourceworldbench_benchmarks.execution_tracer.scripts.run_evaluation import load_traces
        inst_dir = tmp_path / "run_1" / "pytest-dev__pytest-10356"
        inst_dir.mkdir(parents=True)

        # Write plain JSON with version "plain"
        (inst_dir / "trace_output.json").write_text(
            json.dumps({"tests": {}, "tracer_version": "plain"})
        )
        # Write gzip with version "gzip"
        with gzip.open(inst_dir / "trace_output.json.gz", "wt", encoding="utf-8") as f:
            json.dump({"tests": {}, "tracer_version": "gzip"}, f)

        traces, _ = load_traces(str(tmp_path))
        # .json.gz sorts before .json, so should be preferred
        assert traces["pytest-dev__pytest-10356"]["tracer_version"] == "gzip"


# ---------------------------------------------------------------------------
#  Tests: _extract_binary_from_container (mock)
# ---------------------------------------------------------------------------

class TestExtractBinaryFromContainer:
    def _make_mock_container(self, content: bytes, filename: str = "trace_output.json"):
        """Create a mock container that returns a tar archive with the given content."""
        import io
        import tarfile
        container = MagicMock()
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        tar_data = tar_buffer.getvalue()
        container.get_archive.return_value = (iter([tar_data]), {"size": len(tar_data)})
        return container

    def test_extract_binary(self):
        content = b'{"hello": "world"}'
        container = self._make_mock_container(content)
        result = _extract_binary_from_container(container, "/testbed/trace_output.json")
        assert result == content

    def test_extract_text_uses_binary(self):
        content = b'{"hello": "world"}'
        container = self._make_mock_container(content)
        result = _extract_file_from_container(container, "/testbed/trace_output.json")
        assert result == '{"hello": "world"}'

    def test_extract_gzip_binary(self):
        data = json.dumps({"tests": {}}).encode("utf-8")
        compressed = gzip.compress(data)
        container = self._make_mock_container(compressed, "trace_output.json.gz")
        result = _extract_binary_from_container(container, "/testbed/trace_output.json.gz")
        assert gzip.decompress(result) == data

    def test_returns_none_on_error(self):
        container = MagicMock()
        container.get_archive.side_effect = Exception("not found")
        result = _extract_binary_from_container(container, "/testbed/missing.json")
        assert result is None


# ---------------------------------------------------------------------------
#  Tests: _extract_trace_from_container
# ---------------------------------------------------------------------------

class TestExtractTraceFromContainer:
    def _make_mock_container_with_files(self, files: dict[str, bytes]):
        """Mock container where get_archive succeeds for known paths."""
        import io
        import tarfile

        container = MagicMock()

        def mock_get_archive(path):
            if path not in files:
                raise Exception(f"not found: {path}")
            content = files[path]
            tar_buffer = io.BytesIO()
            with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
                info = tarfile.TarInfo(name=os.path.basename(path))
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
            tar_data = tar_buffer.getvalue()
            return iter([tar_data]), {"size": len(tar_data)}

        container.get_archive.side_effect = mock_get_archive
        return container

    def test_extracts_gzip_first(self, tmp_path):
        trace_data = {"tests": {"test_a": {}}, "tracer_version": "0.3.0"}
        compressed = gzip.compress(json.dumps(trace_data).encode("utf-8"))
        container = self._make_mock_container_with_files({
            "/testbed/trace_output.json.gz": compressed,
        })

        json_str, trace_file = _extract_trace_from_container(
            container, "/testbed/trace_output.json", tmp_path,
        )
        assert json_str is not None
        assert json.loads(json_str)["tracer_version"] == "0.3.0"
        assert trace_file.name == "trace_output.json.gz"
        assert trace_file.exists()

    def test_falls_back_to_json(self, tmp_path):
        trace_data = {"tests": {"test_a": {}}, "tracer_version": "0.3.0"}
        container = self._make_mock_container_with_files({
            "/testbed/trace_output.json": json.dumps(trace_data).encode("utf-8"),
        })

        json_str, trace_file = _extract_trace_from_container(
            container, "/testbed/trace_output.json", tmp_path,
        )
        assert json_str is not None
        assert json.loads(json_str)["tracer_version"] == "0.3.0"
        assert trace_file.name == "trace_output.json"

    def test_returns_none_when_not_found(self, tmp_path):
        container = self._make_mock_container_with_files({})
        json_str, trace_file = _extract_trace_from_container(
            container, "/testbed/trace_output.json", tmp_path,
        )
        assert json_str is None
        assert trace_file is None


# ---------------------------------------------------------------------------
#  run_trace() success detection
# ---------------------------------------------------------------------------

class TestRunTraceDetection:
    """Test that run_trace() correctly detects success via file existence,
    even when SWE-bench's make_run_report() prints misleading error strings."""

    def _make_trace_dir(self, tmp_path, instance_id, gz=True):
        """Create a trace directory with a trace file."""
        run_dir = tmp_path / f"trace_single_{instance_id}"
        inst_dir = run_dir / instance_id
        inst_dir.mkdir(parents=True)
        ext = "trace_output.json.gz" if gz else "trace_output.json"
        (inst_dir / ext).write_text("{}")
        return str(tmp_path)

    @patch("sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_traced.subprocess.run")
    def test_success_via_stdout(self, mock_run, tmp_path):
        """Traced: 1/1 in stdout → success even without file check."""
        mock_run.return_value = MagicMock(
            stdout="All instances run. Traced: 1/1\n",
            returncode=0,
        )
        assert run_trace("test-instance", str(tmp_path)) is True

    @patch("sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_traced.subprocess.run")
    def test_success_via_file_despite_error_string(self, mock_run, tmp_path):
        """Trace file exists on disk → success even when stdout has error strings."""
        trace_dir = self._make_trace_dir(tmp_path, "sklearn-13779")
        mock_run.return_value = MagicMock(
            stdout=(
                "Traced: 0/1\n"
                "Instances with errors: 1\n"
                "Unstopped containers: 1\n"
            ),
            returncode=1,
        )
        assert run_trace("sklearn-13779", trace_dir) is True

    @patch("sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_traced.subprocess.run")
    def test_failure_when_no_file_and_error_string(self, mock_run, tmp_path):
        """No trace file + error string → failure."""
        mock_run.return_value = MagicMock(
            stdout=(
                "Traced: 0/1\n"
                "Instances with errors: 1\n"
                "Unstopped containers: 1\n"
                "Report written to gold.json\n"
            ),
            returncode=1,
        )
        assert run_trace("missing-instance", str(tmp_path)) is False

    @patch("sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_traced.subprocess.run")
    def test_failure_no_file_no_error(self, mock_run, tmp_path):
        """No trace file and no error string → failure with 'No trace output found'."""
        mock_run.return_value = MagicMock(
            stdout="Traced: 0/1\n",
            returncode=0,
        )
        assert run_trace("missing-instance", str(tmp_path)) is False

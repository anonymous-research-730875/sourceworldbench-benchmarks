"""Tests for the SWE-bench → single-state Datapoint adapter.

Network-free: swebench's `make_test_spec` is monkeypatched to return a
stub object so we exercise the conversion logic without depending on
Hugging Face or the swebench eval-script template.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sourceworldbench_benchmarks.execution_tracer.adapters import swebench as adapter


@dataclass
class _StubSpec:
    eval_script: str
    instance_image_key: str


_SAMPLE_ROW = {
    "instance_id": "demo__demo-1",
    "repo": "demo/demo",
    "base_commit": "a" * 40,
    "patch": "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@\n-1\n+2\n",
    "test_patch": "diff --git a/tests/t.py b/tests/t.py\n--- a/tests/t.py\n+++ b/tests/t.py\n@@\n-a\n+b\n",
}


def _eval_script(test_body: str) -> str:
    # Mimics swebench's framing — header + test command between markers + cleanup.
    return (
        "#!/bin/bash\n"
        "set -uxo pipefail\n"
        "cd /testbed\n"
        "git apply -v - <<EOF\n<test_patch_body>\nEOF\n"
        ": '>>>>> Start Test Output'\n"
        f"{test_body}\n"
        ": '>>>>> End Test Output'\n"
        "git checkout HEAD -- .\n"
    )


def _install_stub(monkeypatch: pytest.MonkeyPatch, spec: _StubSpec) -> None:
    import types

    fake_const = types.SimpleNamespace(
        START_TEST_OUTPUT=">>>>> Start Test Output",
        END_TEST_OUTPUT=">>>>> End Test Output",
    )
    monkeypatch.setattr(
        adapter,
        "_import_swebench",
        lambda: (fake_const, lambda _row, **_kw: spec),
    )


def test_adapt_swebench_row_yields_base_and_augmented_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _StubSpec(
        eval_script=_eval_script("pytest -rA tests/test_x.py"),
        instance_image_key="sweb.eval.x86_64.demo__demo-1:latest",
    )
    _install_stub(monkeypatch, spec)

    base, augmented = adapter.adapt_swebench_row(_SAMPLE_ROW)

    expected_command = (
        ". /opt/miniconda3/etc/profile.d/conda.sh && "
        "conda activate testbed && "
        "cd /testbed && pytest -rA tests/test_x.py"
    )
    # Both states share repo/base_commit/container/command.
    for dp in (base, augmented):
        assert dp.repo == "demo/demo"
        assert dp.base_commit == "a" * 40
        assert dp.container == "sweb.eval.x86_64.demo__demo-1:latest"
        assert dp.command == expected_command

    # base side: tests present, fix absent.
    assert base.instance_id == "demo__demo-1__base"
    assert base.patch == _SAMPLE_ROW["test_patch"]

    # augmented side: tests + gold patch, sharing the base's (repo, base_commit).
    assert augmented.instance_id == "demo__demo-1__gold"
    assert augmented.patch == _SAMPLE_ROW["test_patch"] + _SAMPLE_ROW["patch"]


def test_adapt_swebench_row_preserves_multiline_test_body(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "tox -e py39 -- tests/foo.py\nmake test"
    spec = _StubSpec(eval_script=_eval_script(body), instance_image_key="img:1")
    _install_stub(monkeypatch, spec)

    base, _augmented = adapter.adapt_swebench_row(_SAMPLE_ROW)

    assert "tox -e py39 -- tests/foo.py" in base.command
    assert "make test" in base.command


def test_adapt_swebench_row_empty_test_patch_leaves_base_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _StubSpec(
        eval_script=_eval_script("pytest"),
        instance_image_key="img:1",
    )
    _install_stub(monkeypatch, spec)
    row = dict(_SAMPLE_ROW, test_patch="")

    base, augmented = adapter.adapt_swebench_row(row)
    # No test_patch → base has no patch; augmented carries just the gold patch.
    assert base.patch is None
    assert augmented.patch == _SAMPLE_ROW["patch"]


def test_adapt_swebench_row_rejects_missing_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _StubSpec(
        eval_script="#!/bin/bash\ncd /testbed\npytest\n",
        instance_image_key="img:1",
    )
    _install_stub(monkeypatch, spec)
    with pytest.raises(ValueError, match="markers"):
        adapter.adapt_swebench_row(_SAMPLE_ROW)


def test_adapt_swebench_row_rejects_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _eval_script("").replace("\n\n", "\n")  # body is empty between markers
    spec = _StubSpec(eval_script=script, instance_image_key="img:1")
    _install_stub(monkeypatch, spec)
    with pytest.raises(ValueError, match="empty"):
        adapter.adapt_swebench_row(_SAMPLE_ROW)

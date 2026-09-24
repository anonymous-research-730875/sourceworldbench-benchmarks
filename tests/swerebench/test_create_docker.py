"""Tests for Dockerfile rendering (``swerebench.create_docker._render_dockerfile``).

The golden_base image materialises the golden_base state by ``git checkout``-ing the
merge commit.  That commit is frequently absent from the eval image's local clone,
and the clone has **no** ``origin`` remote, so the on-demand fetch must target the
repo's GitHub URL explicitly — fetching ``origin`` fails with
``fatal: 'origin' does not appear to be a git repository`` (the astropy regression).
"""

import shlex

from sourceworldbench_benchmarks.swerebench.create_docker import _render_dockerfile, render_test_command

SHA = "0aa40c5126c7c4f88e9291eb6fb81eb3b880fe59"
URL = "https://github.com/astropy/astropy"


def test_no_checkout_omits_fetch_and_checkout():
    df = _render_dockerfile(docker_image="swerebench/x", copy_prefix="environments/i", patch_attempts={})
    assert "git fetch" not in df
    assert "git checkout" not in df


def test_checkout_fetches_from_github_url_not_origin():
    df = _render_dockerfile(
        docker_image="swerebench/x",
        copy_prefix="environments/i",
        patch_attempts={},
        checkout=SHA,
        checkout_url=URL,
    )
    # Fetch only when the commit is missing locally, from the explicit repo URL.
    assert f"git cat-file -e {SHA}" in df
    assert f"git fetch --no-tags {URL} {SHA}" in df
    assert f"git checkout -f {SHA}" in df
    # The bug being guarded against: never fetch from a nonexistent ``origin`` remote.
    assert "git fetch --no-tags origin" not in df


def test_render_is_valid_multi_line_run_block():
    df = _render_dockerfile(
        docker_image="swerebench/x",
        copy_prefix="environments/i",
        patch_attempts={},
        checkout=SHA,
        checkout_url=URL,
    )
    assert df.startswith("# syntax=docker/dockerfile:1.7")
    assert "FROM --platform=linux/amd64 swerebench/x:latest" in df
    # The checkout precedes the /results mkdir in one RUN.
    run_block = df.split("RUN ln", 1)[1]
    assert run_block.index("git checkout") < run_block.index("mkdir -p /results")


# --- render_test_command ---------------------------------------------------
# swefficiency images' conda hooks are bash-only (conda-forge's older
# activate-binutils_linux-64.sh opens with `function name()`), and `conda activate`
# sources them into the calling shell. Under the runner's `/bin/sh` that is a parse
# error, which aborts the shell before the tests run.


def test_render_test_command_default_is_unwrapped():
    cmd = render_test_command("pytest -q", conda_base="/opt/miniconda3")
    assert cmd == (
        "mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh "
        "&& conda activate testbed && pytest -q "
        "--junitxml=/results/junit.xml --continue-on-collection-errors"
    )


def test_render_test_command_use_bash_wraps_activation_and_tests_together():
    cmd = render_test_command("pytest -q", conda_base="/opt/miniconda3", use_bash=True)
    # The prefix stays outside; everything conda touches goes inside one bash -c.
    assert cmd.startswith("mkdir -p /results && cd /app && bash -c '")
    assert cmd.endswith("'")
    inner = cmd.split("bash -c ", 1)[1]
    assert "conda activate testbed" in inner
    assert "pytest -q" in inner


def test_render_test_command_use_bash_survives_quotes_in_the_test_command():
    # Real commands carry single quotes (PYTHONWARNINGS='...') and command
    # substitution (pandas' addopts probe); neither may be mangled or expanded
    # by the outer sh.
    raw = "PYTHONWARNINGS='ignore::UserWarning' pytest $(echo -q)"
    cmd = render_test_command(raw, conda_base="/opt/miniconda3", use_bash=True)
    inner = shlex.split(cmd.split("&& ", 2)[2])[2]
    assert raw in inner

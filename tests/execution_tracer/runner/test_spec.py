from sourceworldbench_benchmarks.execution_tracer.runner.spec import BY_NAME, CPROFILE, MEMPROF, MEMRSS, TRACE, WALLTIME


def test_presets_present_and_addressable_by_name() -> None:
    assert set(BY_NAME) == {"trace", "walltime", "memprof", "memrss", "cprofile"}
    assert BY_NAME["trace"] is TRACE
    assert BY_NAME["walltime"] is WALLTIME
    assert BY_NAME["memprof"] is MEMPROF
    assert BY_NAME["memrss"] is MEMRSS
    assert BY_NAME["cprofile"] is CPROFILE


def test_preset_source_files_exist() -> None:
    for spec in BY_NAME.values():
        assert spec.source_file.is_file(), f"missing tracer source: {spec.source_file}"


def test_env_vars_uses_prefix_and_paths() -> None:
    env = TRACE.env_vars(
        output_path_in_container="/sourceworldbench-tracing-out/trace_output.json",
        repo_dir="/app",
        trace_paths=["src", "tests"],
    )
    assert env["SWEBENCH_TRACE_OUTPUT"] == "/sourceworldbench-tracing-out/trace_output.json"
    assert env["SWEBENCH_TRACE_MODE"] == "pytest"
    assert env["SWEBENCH_TRACE_PATHS"] == "src,tests"
    assert env["SWEBENCH_REPO_DIR"] == "/app:/testbed"


def test_env_vars_omits_paths_when_empty() -> None:
    env = WALLTIME.env_vars(output_path_in_container="/x.json", repo_dir="/app")
    assert "SWEBENCH_TIMER_PATHS" not in env
    assert env["SWEBENCH_TIMER_MODE"] == "pytest"

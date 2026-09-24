"""Outcome provider for SWE-bench Verified rows: grade by parsing stdout."""

from __future__ import annotations

from sourceworldbench_benchmarks.schema import StateDatapoint

_SWEBENCH_INSTALL_HINT = (
    "swebench is required to grade SWE-bench logs. "
    "Install with: pip install 'sourceworldbench-benchmarks[swebench]'"
)


def _load_parser_map() -> dict:
    try:
        from swebench.harness.log_parsers import MAP_REPO_TO_PARSER
    except ImportError as exc:  # pragma: no cover
        raise ImportError(_SWEBENCH_INSTALL_HINT) from exc
    return MAP_REPO_TO_PARSER


class SweBenchLogOutcomes:
    """Run the per-repo SWE-bench log parser against the captured stdout.

    SWE-bench ships one parser per supported repo (`MAP_REPO_TO_PARSER`);
    each returns a `{test_id: status}` dict from the raw log text. We
    select by `datapoint.repo` and surface the result unchanged.

    `run_log` is the bytes captured from the docker run; supply it from
    `RunResult.stdout` (concatenate stderr too if a run mixes them).
    """

    def __init__(self, parser_map: dict | None = None) -> None:
        # Injectable for tests; defaults to the lazy-imported swebench map.
        self._parser_map = parser_map

    def outcomes_for(
        self,
        datapoint: StateDatapoint,
        run_log: bytes | None = None,
    ) -> dict[str, str]:
        if run_log is None:
            raise ValueError(
                f"SweBenchLogOutcomes requires the container stdout for "
                f"{datapoint.instance_id}; pass it via run_log."
            )
        parser_map = self._parser_map or _load_parser_map()
        try:
            parser = parser_map[datapoint.repo]
        except KeyError as exc:
            raise ValueError(
                f"no swebench log parser registered for repo {datapoint.repo!r}; "
                f"available: {sorted(parser_map.keys())[:5]}..."
            ) from exc
        log_text = run_log.decode("utf-8", errors="replace")
        return dict(parser(log_text))

"""Generate LLM workload variants and build the submission JSONL.

Two steps, one subcommand each:
  generate     one LLM call per pair, writing a `<pair>.py` or a refusal marker
               to OUT_DIR; `--mode` picks how (amplify / scratch / light)
  build-jsonl  splice the generated files into the pairs' dataset rows as the
               `command_workload_amplified` column (the original workload stays
               in `command_workload`), producing a JSONL ready for
               `sourceworldbench-benchmarks k8s trace --workload --amplified`

`generate` needs a `.env` (or the environment) with `LITELLM_API_KEY` and
`LITELLM_BASE_URL`. Run with `<subcommand> --help` for the arguments.
"""

import argparse
import ast
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from litellm import completion

from sourceworldbench_benchmarks.dataset_io import _row_from_hub, load_existing_rows
from sourceworldbench_benchmarks.execution_tracer.runner.script import find_pytest_invocation
from sourceworldbench_benchmarks.schema import StateDatapoint

MODEL = "litellm_proxy/anthropic/claude-sonnet-5"
REQUIRED_TESTS = ("test_workload_light", "test_workload_medium", "test_workload_heavy")

WORKLOAD_FILE = "/tmp/test_workload.py"
HEREDOC_START = "<<'PYEOF'\n"
HEREDOC_END = "PYEOF\n"
CANONICAL_PYTEST = f"pytest {WORKLOAD_FILE} -rA -vv --tb=no --junitxml=/results/junit.xml'"

SCRATCH_PROMPT = """\
Here is a performance-optimization diff from the {repo} repository. We measure how such
patches change MEMORY consumption: the same workload runs under a memory profiler on the
code before and after the patch, and we compare peaks. No existing benchmark exercises
this patch at meaningful data sizes, so write one from scratch.

Write a single self-contained pytest file with exactly three tests, in this order:
`test_workload_light`, `test_workload_medium`, `test_workload_heavy`. All three run the
SAME code — a realistic call into the public API that reaches the code changed in the
diff — at three input scales:

1. light — small, fast sanity scale (sub-second, a few MB at most).
2. medium — inputs sized so the workload allocates at least ~10 MB.
3. heavy — inputs sized so the workload allocates at least ~100 MB.

Rules:
- Three plain test functions, NOT parametrized, no fixtures. Import only the standard
  library and the profiled package (plus numpy if the package's API takes arrays).
- Each test builds its inputs deterministically (seed any randomness), then calls the
  workload exactly once. No timeit, no timing prints. End each test with `assert True`.
- The workload MUST execute the functions changed in the diff — reach them through the
  public API a real user would call, and say in the header which changed function(s)
  the workload reaches and how.
- The three tests must differ only in input sizes. Reason about complexity: if memory
  grows quadratically with a knob, scale the knob by the square root.
- Memory budget (hard): every single test must stay under ~8 GB peak RSS. Estimate from
  data sizes, then be conservative: temporary copies and intermediates routinely make
  the true peak 3-5x the naive estimate, so keep the NAIVE estimate under ~1.5 GB.
  Each test should finish within a few minutes on one CPU core.
- Use only APIs that exist at this commit (the diff shows the code as it is being
  changed — match its imports and call signatures, not today's versions).
- Shared helpers may be module-level, but each test must be runnable in isolation and
  the three tests must not share mutable state (build the inputs inside each test).
- First line of the file must be a comment: `# from-scratch: <one line — what the
  workload does, which changed functions it reaches, what is scaled>`.

If the changed code CANNOT have size-dependent memory behavior (e.g. it operates on
O(1) state regardless of input size), do NOT write code: reply with the single line
`NOT_POSSIBLE: <one-line reason>` and nothing else.

Otherwise reply with ONE fenced python code block containing the complete file,
nothing else.

The diff:

```diff
{patch}
```
"""

PROMPT = """\
You are given a performance-benchmark workload from the SWE-fficiency dataset: a single
pytest test that builds inputs in `setup()` and times a `workload()` call with
`timeit.repeat`. We re-run these workloads under a memory profiler to measure how a
performance patch changes memory consumption. Small inputs often hide the memory effect,
so we want the same workload at three input scales.

Rewrite the file so it contains exactly three tests, in this order:

1. `test_workload_light` — the original workload (same inputs, same logic, only the
   timing harness removed as described below).
2. `test_workload_medium` — identical logic, input sizes scaled so the workload's peak
   memory is roughly 10x the light version, or at least ~10 MB of workload data,
   whichever is larger.
3. `test_workload_heavy` — identical logic, input sizes scaled so peak memory is roughly
   100x the light version, or at least ~100 MB of workload data, whichever is larger.

The absolute floors matter: if the light workload only touches a few KB (tiny arrays),
a literal 10x/100x is still tiny and useless for memory profiling — scale far enough
to reach the floors instead, and say so in the header comment. The floors are subject
to the same budget as everything else: if reaching a floor would break the memory or
runtime budget below, get as close as the budget allows.

Rules:
- Three plain test functions, NOT parametrized, no fixtures, no imports of anything the
  original didn't import (plus nothing outside the standard library / the profiled
  package itself).
- Scale only the DATA: array lengths, row counts, grid resolutions, n_samples, etc.
  Never change which functions are called or add new code paths. Reason about complexity:
  if memory grows quadratically with a knob, scale that knob by sqrt(10)/sqrt(100).
- Strip the timing harness: do NOT use `timeit` (we measure memory, not time, and the
  peak is per-call). Each test builds its inputs — call `setup()` once if the original
  defines one, keep inline setup inline — then calls the workload function exactly once.
  Drop the runtime mean/stddev prints; keep the trailing `assert True`.
- Memory budget (hard): every single test must stay under ~8 GB peak RSS. Estimate from
  the data sizes (element count x itemsize), then be conservative: temporary copies,
  format conversions, and intermediates routinely make the true peak 3-5x the naive
  estimate, so keep the NAIVE estimate under ~1.5 GB. If the nominal 100x heavy would
  exceed that, scale it as far as the budget allows and say so in the header comment.
  Each test should also plausibly finish within a few minutes on one CPU core.
- Shared helpers may be module-level, but each test must be runnable in isolation and
  the three tests must not share mutable state (re-run setup per test; if the original
  uses `global`, keep that pattern per test with distinct global names or re-assignment).
- First line of the file must be a comment: `# amplification: <one-line note on what was
  scaled and by how much>`.

If the workload has NO meaningful data-size knob (e.g. it measures a fixed-size
operation, import time, or a scalar API call), do NOT write any code: reply with the
single line `NOT_AMPLIFIABLE: <one-line reason>` and nothing else.

Otherwise reply with ONE fenced python code block containing the complete file,
nothing else.

Original workload file:

```python
{workload}
```
"""

LIGHT_PROMPT = """\
You are given a performance-benchmark workload from the SWE-fficiency dataset: a single
pytest test that builds inputs in `setup()` and times a `workload()` call with
`timeit.repeat`. We re-run these workloads under a memory profiler; the timing harness only
adds runtime (memory peaks are per-call), so we want the same workload with it stripped.

Rewrite the file so it contains exactly one test, `test_workload_light`: the original
workload — same inputs, same logic, no scaling — with the timing harness removed. Build
the inputs (call `setup()` once if the original defines one, keep inline setup inline),
then call the workload function exactly once. Do NOT use `timeit`; drop the runtime
mean/stddev prints; keep the trailing `assert True`. Import nothing the original didn't
import.

First line of the file must be a comment: `# light-only: <one-line note on what the
workload does>`.

Reply with ONE fenced python code block containing the complete file, nothing else.

Original workload file:

```python
{workload}
```
"""

# Per-mode knobs: the prompt sent, the refusal sentinel the model may reply with,
# the mandatory first-line header, and the test functions the file must define.
MODES = {
    "amplify": (PROMPT, "NOT_AMPLIFIABLE", "# amplification:", REQUIRED_TESTS),
    "scratch": (SCRATCH_PROMPT, "NOT_POSSIBLE", "# from-scratch:", REQUIRED_TESTS),
    "light": (LIGHT_PROMPT, "NOT_POSSIBLE", "# light-only:", ("test_workload_light",)),
}

MODE_HELP = (
    "how the workload is generated: 'amplify' (default) rewrites the pair's existing "
    "workload into three tests at light (original), medium (~10x / >=10MB) and heavy "
    "(~100x / >=100MB) input scales; 'scratch' writes a new workload from the pair's "
    "patch instead, for pairs whose existing workload cannot be amplified; 'light' "
    "keeps the original workload at its original scale as a single test_workload_light "
    "with only the timing harness stripped, for pairs where even 'scratch' is refused"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("workloads_llm")


def extract_workload(command_workload: str) -> str:
    return command_workload.split(HEREDOC_START, 1)[1].split(HEREDOC_END, 1)[0]


def load_rows(source: str) -> list[dict]:
    """Load dataset rows from SOURCE.

    SOURCE is a local JSONL path if such a file exists, otherwise a Hugging
    Face dataset id (e.g. anonymous-research-730875/sourceworldbench-single-state).
    """
    path = Path(source)
    if path.exists():
        return [json.loads(line) for line in path.open()]
    rows = load_existing_rows(source)
    if not rows:
        raise SystemExit(f"{source}: not a local file, and the HF dataset is empty or missing")
    return rows


def extract_code_block(response: str) -> str:
    fenced = re.search(r"```(?:python|py)?[ \t]*\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1)
    try:
        ast.parse(response)
    except SyntaxError:
        raise ValueError("no fenced code block and response is not bare Python")
    return response


def validate(code: str, required_tests: tuple[str, ...]) -> None:
    tree = ast.parse(code)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    missing = [t for t in required_tests if t not in defined]
    if missing:
        raise ValueError(f"missing test functions: {missing}")
    if "timeit" in code:
        raise ValueError("timing harness not stripped")


class GenerationError(Exception):
    """A generation failed after the LLM call; carries the call's token usage."""

    def __init__(self, message: str, usage: dict):
        super().__init__(message)
        self.usage = usage


def generate_one(pair: str, prompt: str, refusal: str, header: str, required_tests: tuple[str, ...],
                 out_dir: Path, api_key: str, base_url: str) -> tuple[str, dict]:
    resp = completion(
        model=MODEL,
        api_key=api_key,
        base_url=base_url,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=48000,
    )
    usage = {
        "input_tokens": resp.usage.prompt_tokens,
        "output_tokens": resp.usage.completion_tokens,
        # price computed by the LiteLLM gateway (x-litellm-response-cost header)
        "cost_usd": resp._hidden_params.get("response_cost") or 0.0,
    }
    try:
        if resp.choices[0].finish_reason == "length":
            raise ValueError("response truncated at max_tokens")
        text = resp.choices[0].message.content
        (out_dir / f"{pair}.raw.txt").write_text(text)
        if text.strip().startswith(f"{refusal}:"):
            (out_dir / f"{pair}.{refusal}").write_text(text.strip() + "\n")
            return text.strip(), usage
        code = extract_code_block(text)
        note = next((line for line in text.splitlines() if line.startswith(header)), None)
        if note is None:
            raise ValueError(f"no `{header}` header anywhere in the response")
        if not code.lstrip().startswith(header):
            code = note + "\n\n" + code.lstrip()
        validate(code, required_tests)
        (out_dir / f"{pair}.py").write_text(code)
        return note.removeprefix(header).strip(), usage
    except Exception as exc:
        raise GenerationError(str(exc), usage) from exc


def cmd_generate(out_dir: Path, rows_source: str, pair_filter: set[str], mode: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv()
    api_key, base_url = os.environ["LITELLM_API_KEY"], os.environ["LITELLM_BASE_URL"]

    prompt_template, refusal, header, required_tests = MODES[mode]

    prompts: dict[str, str] = {}
    for row in load_rows(rows_source):
        iid = row["instance_id"]
        if mode == "scratch" and iid.endswith("__fixed"):
            prompts[iid.removesuffix("__fixed")] = prompt_template.format(
                repo=row["repo"], patch=row["patch"]
            )
        elif mode != "scratch" and iid.endswith("__base"):
            prompts[iid.removesuffix("__base")] = prompt_template.format(
                workload=extract_workload(row["command_workload"])
            )
    pair_keys = sorted(pair_filter or prompts)
    missing = set(pair_keys) - set(prompts)
    if missing:
        raise SystemExit(f"pairs not found in {rows_source}: {sorted(missing)}")
    todo = [p for p in pair_keys
            if not (out_dir / f"{p}.py").exists() and not (out_dir / f"{p}.{refusal}").exists()]
    log.info("%d pairs requested, %d already done, %d to generate",
             len(pair_keys), len(pair_keys) - len(todo), len(todo))

    def run(pair: str) -> tuple[str, str, dict]:
        try:
            note, usage = generate_one(pair, prompts[pair], refusal, header,
                                       required_tests, out_dir, api_key, base_url)
            return pair, note, usage
        except GenerationError as exc:  # failed after the LLM call: usage still counts
            return pair, f"FAILED: {exc}", exc.usage
        except Exception as exc:  # surface per-pair failures without killing the batch
            return pair, f"FAILED: {exc}", {}

    totals = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    n_done = 0
    with (out_dir / "generation_report.jsonl").open("a") as report, \
            ThreadPoolExecutor(max_workers=12) as pool:
        for pair, note, usage in pool.map(run, todo):
            n_done += 1
            for k in totals:
                totals[k] += usage.get(k, 0)
            report.write(json.dumps({"pair": pair, "note": note, **usage}) + "\n")
            log.info("[%d/%d] %s -> %s", n_done, len(todo), pair, note[:110])

    log.info("tokens: %s input, %s output; gateway-reported cost: $%.2f",
             f"{totals['input_tokens']:,}", f"{totals['output_tokens']:,}", totals["cost_usd"])


def build_command(command: str, file_content: str) -> str:
    """Splice the workload file into a row's command.

    The heredoc goes at the top shell level, followed by the original
    preamble with the pytest invocation replaced by the canonical one.
    """
    # Without the trailing newline the closing PYEOF shares the file's last
    # line and the heredoc swallows the rest of the command, silently running
    # no tests at all.
    file_content = file_content.rstrip() + "\n"
    heredoc = f"cat > {WORKLOAD_FILE} {HEREDOC_START}{file_content}{HEREDOC_END}"
    match = find_pytest_invocation(command)
    if not command.endswith("'"):
        raise ValueError(f"command does not end with the closing bash -c quote: {command!r}")
    return heredoc + command[: match.start()] + CANONICAL_PYTEST


def pair_key(row: dict, pairs: set[str]) -> str | None:
    """Map a row to its pair key `<repo>__<base-id>__<sweff problem id>`.

    Long-form instance ids carry the pair as their first four segments;
    short-form prediction ids (`<repo>__<base-id>__prediction_*`) name the
    problem only in `metadata.swefficiency.instance_id`.
    """
    segments = row["instance_id"].split("__")
    candidate = "__".join(segments[:4])
    if candidate in pairs:
        return candidate
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    problem = (metadata.get("swefficiency") or {}).get("instance_id")
    if problem:
        candidate = "__".join(segments[:2]) + f"__{problem}"
        if candidate in pairs:
            return candidate
    return None


def cmd_build_jsonl(amp_dir: Path, rows_source: str, out_jsonl: Path) -> None:
    workload_files = {p.stem: p.read_text() for p in amp_dir.glob("*.py")}
    pairs = set(workload_files)

    n, skipped, matched_pairs = 0, [], set()
    with out_jsonl.open("w") as f:
        for raw in load_rows(rows_source):
            pair = pair_key(raw, pairs)
            if pair is None:
                skipped.append((raw["instance_id"], "no generated workload for this row's pair"))
                continue
            try:
                command = build_command(raw["command"], workload_files[pair])
            except ValueError as exc:
                skipped.append((raw["instance_id"], repr(exc)))
                continue
            row = _row_from_hub(raw)
            row["command_workload_amplified"] = command
            StateDatapoint.model_validate(row)
            f.write(json.dumps(row, sort_keys=True) + "\n")
            n += 1
            matched_pairs.add(pair)
    for key, why in skipped:
        print(f"SKIPPED {key}: {why}")
    print(f"wrote {n} rows ({len(matched_pairs)} pairs, {len(skipped)} skipped) to {out_jsonl}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser(
        "generate", help="generate light/medium/heavy variants, one LLM call per pair")
    gen.add_argument(
        "out_dir", type=Path,
        help="output directory: per pair a generated <pair>.py or a refusal marker,"
             " plus the raw model response; token/cost records go to"
             " generation_report.jsonl. Pairs already present here are skipped (resumable).")
    gen.add_argument(
        "rows_source",
        help="dataset rows: a local JSONL file, or an HF dataset id (e.g."
             " anonymous-research-730875/sourceworldbench-single-state); workloads are read from __base rows,"
             " patches from __fixed rows")
    gen.add_argument(
        "pair_keys", nargs="*",
        help="pairs to generate, as instance ids without the __base/__fixed suffix,"
             " e.g. sympy__31c68eef__sympy__sympy-14772 (default: every pair in ROWS_SOURCE)")
    gen.add_argument("--mode", choices=list(MODES), default="amplify", help=MODE_HELP)

    build = sub.add_parser(
        "build-jsonl", help="splice generated files into dataset rows, ready to submit")
    build.add_argument(
        "amp_dir", type=Path,
        help="directory with the generated <pair>.py files (output of `generate`);"
             " refusal markers and raw responses in it are ignored")
    build.add_argument(
        "rows_source",
        help="dataset rows: a local JSONL file, or an HF dataset id; every row whose"
             " instance id starts with a pair in AMP_DIR (base, fixed, trajectories) is emitted")
    build.add_argument(
        "out_jsonl", type=Path,
        help="output JSONL: decoded, StateDatapoint-valid rows with the amplified command in"
             " command_workload_amplified and the original untouched")

    args = parser.parse_args()
    if args.command == "generate":
        cmd_generate(args.out_dir, args.rows_source, set(args.pair_keys), args.mode)
    else:
        cmd_build_jsonl(args.amp_dir, args.rows_source, args.out_jsonl)


if __name__ == "__main__":
    main()

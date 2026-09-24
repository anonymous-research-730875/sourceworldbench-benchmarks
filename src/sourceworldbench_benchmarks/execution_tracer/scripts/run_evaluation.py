"""Evaluate models on the v0.3 benchmark samples.

Usage:
    # OpenAI
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_evaluation \
        --samples_dir benchmark_samples \
        --out_dir eval_results \
        --model gpt-5-mini --max_samples_per_task 10

    # Nebius (Qwen)
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_evaluation \
        --samples_dir benchmark_samples \
        --out_dir eval_results \
        --model 'Qwen/Qwen3-30B-A3B-Instruct-2507' \
        --base_url https://api.studio.nebius.com/v1/ \
        --api_key_env NEBIUS_API_KEY \
        --max_tokens 2048
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from sourceworldbench_benchmarks.execution_tracer.benchmark.scoring import aggregate_combined, score

# ── Output JSON parsing ────────────────────────────────────────────────────

# Some models wrap JSON in code fences despite the system prompt. Strip them.
_CODE_FENCE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)
# Matches a fenced ```json...``` block anywhere in the response — Anthropic
# (especially haiku) tends to prepend conversational text before the JSON,
# so we also look for any json-tagged fence and prefer the LAST one (the
# answer is usually after the chain-of-thought).
_CODE_FENCE_ANYWHERE = re.compile(
    r"```(?:json|JSON)?\s*\n(\{.*?\})\s*\n```", re.DOTALL
)
# Matches a fenced block whose content is bare "key": value pairs without
# the enclosing {} (haiku sometimes emits JSON fields in a ```python``` fence).
_CODE_FENCE_BARE = re.compile(
    r"```[a-zA-Z]*\s*\n(\"[a-z_]+\"\s*:.*?)\n```", re.DOTALL
)
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
# JSONC-style // line comments (gpt-oss-120b annotates JSON values with these).
_LINE_COMMENT = re.compile(r"//[^\n]*")


# Top-level keys we expect after "reasoning" in a combined-task response.
# Used by the fallback that nulls a broken reasoning string and retries.
_KNOWN_FIELDS_AFTER_REASONING = (
    "outcome", "failure_line", "exception_type",
    "peak_bytes", "wall_ms",
    "hot_methods_time", "hot_methods_alloc",
    "hot_lines_time", "hot_lines_alloc",
    "bytes", "milliseconds",         # peak_rss / wall_time individual tasks
    "functions", "lines",            # hotspot tasks
)
_NEXT_KEY_PATTERN = re.compile(
    r',\s*"(' + "|".join(_KNOWN_FIELDS_AFTER_REASONING) + r')"\s*:'
)
_REASONING_KEY_RE = re.compile(r'"reasoning"\s*:')


def _strip_broken_reasoning(txt: str) -> str | None:
    """If `txt` looks like a JSON object whose `reasoning` field is
    malformed (the common Qwen failure: unescaped quotes / backslashes
    in a sentence describing code), replace the reasoning value with
    `""` so the rest of the object can still be parsed. Returns the
    edited text, or None if we can't locate the reasoning field.

    Models almost always emit prediction fields AFTER reasoning, so
    nulling that one field is enough to recover the actual answer.
    """
    m = _REASONING_KEY_RE.search(txt)
    if not m:
        return None
    after_colon = m.end()
    next_key = _NEXT_KEY_PATTERN.search(txt, after_colon)
    if not next_key:
        return None
    # Replace everything between `"reasoning":` and the next known key
    # with an empty string literal.
    return txt[:after_colon] + ' ""' + txt[next_key.start():]


def parse_response(raw: str) -> dict | None:
    """Best-effort JSON parse of a model response. Returns None on failure."""
    if not raw:
        return None
    txt = raw.strip()
    # Strip <think>...</think> reasoning blocks (Qwen3 thinking models)
    txt = _THINK_BLOCK.sub("", txt).strip()
    # Strip ```json ... ``` fences
    m = _CODE_FENCE.match(txt)
    if m:
        txt = m.group(1).strip()

    def _try_loads(s: str):
        """json.loads + JSONC comment-strip, no brace hunting."""
        try:
            return json.loads(s)
        except Exception:
            pass
        # Strip // line comments (gpt-oss-120b emits JSONC) and retry
        stripped_comments = _LINE_COMMENT.sub("", s)
        if stripped_comments != s:
            try:
                return json.loads(stripped_comments)
            except Exception:
                pass
        return None

    def _try_parse(s: str):
        """_try_loads + brace-hunting fallback for isolated fence content."""
        result = _try_loads(s)
        if result:
            return result
        # Fallback: pull the first {...} block (useful for fence content
        # that has minor leading/trailing whitespace or a wrapper key).
        start = s.find("{")
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        return None
        return None

    # Initial attempt: loads only (no brace hunting) on the full text.
    # Brace hunting on the full text would latch onto the first stray {...}
    # in any code example in the prose before the real answer is reached.
    result = _try_loads(txt)
    if result:
        return result

    # Recovery #1: Anthropic (haiku especially) often replies with
    # "Looking at the test... <reasoning paragraphs> ... ```json {...} ```".
    # Extract the last ```json``` fence and try parsing it.
    fences = _CODE_FENCE_ANYWHERE.findall(txt)
    if fences:
        result = _try_parse(fences[-1])
        if result:
            return result
        # Apply the reasoning-strip recovery to the fenced content too.
        stripped = _strip_broken_reasoning(fences[-1])
        if stripped is not None:
            result = _try_parse(stripped)
            if result:
                return result

    # Recovery #1b: haiku sometimes emits bare "key": value pairs in a fence
    # without the enclosing {}. Wrap the last such fence in braces and retry.
    bare_fences = _CODE_FENCE_BARE.findall(txt)
    if bare_fences:
        wrapped = "{" + bare_fences[-1].rstrip(",\n ") + "}"
        result = _try_parse(wrapped)
        if result:
            return result

    # Recovery #2: walk every '{' in the response from last to first
    # and try json.JSONDecoder().raw_decode. The model may have emitted
    # the JSON without a fence, or after prose containing other braces.
    # We prefer the LAST valid object (typically the model's final
    # answer, after any reasoning prose).
    decoder = json.JSONDecoder()
    brace_positions = [i for i, c in enumerate(txt) if c == "{"]
    for start in reversed(brace_positions):
        try:
            obj, _end = decoder.raw_decode(txt[start:])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and obj:  # skip empty {} from stray braces
            return obj

    # Recovery #3: null the reasoning field and retry. Catches the case
    # where the model wrote invalid JSON inside `reasoning` (unescaped
    # quotes around variable names, raw `\n` in code snippets, etc.) but
    # the prediction fields after it are well-formed.
    stripped = _strip_broken_reasoning(txt)
    if stripped is not None and stripped != txt:
        return _try_parse(stripped)
    return None


def extract_prediction(task: str, parsed: dict | None) -> Any:
    """Pull the prediction value from a parsed JSON response."""
    if parsed is None:
        return None
    if task == "combined":
        # Bundle every sub-task's fields into a dict matching gt_combined()'s
        # shape. Be defensive about types: model may emit strings for ints,
        # missing fields, etc.
        line = parsed.get("failure_line")
        try:
            line = int(line) if line is not None else None
        except Exception:
            line = None
        exc = parsed.get("exception_type")
        if isinstance(exc, str):
            exc = exc.strip() or None
        def _int(v):
            try:
                return int(v) if v is not None else None
            except Exception:
                return None
        def _float(v):
            try:
                return float(v) if v is not None else None
            except Exception:
                return None
        def _list(v):
            return [str(x) for x in v] if isinstance(v, list) else None
        return {
            "outcome": {
                "outcome": (parsed.get("outcome") or "").strip() or None,
                "failure_line": line,
                "exception_type": exc,
            },
            "peak_bytes": _int(parsed.get("peak_bytes")),
            "wall_ms": _float(parsed.get("wall_ms")),
            "hot_methods_time": _list(parsed.get("hot_methods_time")),
            "hot_methods_alloc": _list(parsed.get("hot_methods_alloc")),
            "hot_lines_time": _list(parsed.get("hot_lines_time")),
            "hot_lines_alloc": _list(parsed.get("hot_lines_alloc")),
        }
    if task == "outcome":
        # Merged outcome task: return the whole dict so scorer can read
        # outcome + failure_line + exception_type together.
        line = parsed.get("failure_line")
        try:
            line = int(line) if line is not None else None
        except Exception:
            line = None
        exc = parsed.get("exception_type")
        if isinstance(exc, str):
            exc = exc.strip() or None
        return {
            "outcome": (parsed.get("outcome") or "").strip() or None,
            "failure_line": line,
            "exception_type": exc,
        }
    if task == "peak_rss":
        v = parsed.get("bytes")
        try:
            return int(v) if v is not None else None
        except Exception:
            return None
    if task == "wall_time":
        # Prompt asks for milliseconds.
        v = parsed.get("milliseconds")
        if v is None:
            # Backwards compat: older prompts asked for seconds.
            v = parsed.get("seconds")
            try:
                return float(v) * 1000.0 if v is not None else None
            except Exception:
                return None
        try:
            return float(v) if v is not None else None
        except Exception:
            return None
    if task in ("hot_methods_time", "hot_methods_alloc"):
        v = parsed.get("functions")
        return [str(x) for x in v] if isinstance(v, list) else None
    if task in ("hot_lines_time", "hot_lines_alloc"):
        v = parsed.get("lines")
        return [str(x) for x in v] if isinstance(v, list) else None
    return parsed


# ── Provider wrappers ─────────────────────────────────────────────────────

def _is_anthropic(model: str) -> bool:
    return model.lower().startswith("claude-")


def call_anthropic(model: str, system: str, user: str,
                   api_key: str,
                   max_tokens: int = 2048,
                   timeout: float = 180.0) -> tuple[str, dict]:
    """Call Anthropic Messages API. Returns (raw_text, usage_dict)."""
    import anthropic
    # Force the production endpoint. ANTHROPIC_BASE_URL is set in some
    # agent/sandbox environments (e.g. Claude Code) and would silently
    # reroute API calls to a local proxy that rate-limits us instantly.
    client = anthropic.Anthropic(
        api_key=api_key,
        base_url="https://api.anthropic.com",
        timeout=timeout,
    )
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    # Concatenate text blocks (model may emit multiple).
    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text += block.text
    usage = {
        "prompt_tokens": getattr(resp.usage, "input_tokens", None),
        "completion_tokens": getattr(resp.usage, "output_tokens", None),
    }
    if usage["prompt_tokens"] is not None and usage["completion_tokens"] is not None:
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return text, usage


def call_openai(model: str, system: str, user: str,
                base_url: str | None, api_key: str,
                max_tokens: int = 2048,
                temperature: float = 0.0,
                timeout: float = 180.0) -> tuple[str, dict]:
    """Call OpenAI-compatible endpoint. Returns (raw_text, usage_dict)."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url \
        else OpenAI(api_key=api_key)
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        timeout=timeout,
    )
    # gpt-5 models reject `temperature` and `max_tokens` (use the
    # default). Plain non-gpt-5 / non-reasoning models keep them.
    is_gpt5 = "gpt-5" in model.lower()
    if not is_gpt5:
        kwargs["temperature"] = temperature
        kwargs["max_tokens"] = max_tokens
    resp = client.chat.completions.create(**kwargs)
    text = resp.choices[0].message.content or ""
    usage = {}
    if resp.usage:
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
        }
    return text, usage


def call_model(model: str, system: str, user: str,
               base_url: str | None, api_key: str,
               max_tokens: int = 2048,
               timeout: float = 180.0) -> tuple[str, dict]:
    """Dispatch to Anthropic or OpenAI-compatible API based on model name."""
    if _is_anthropic(model):
        return call_anthropic(model, system, user, api_key,
                              max_tokens=max_tokens, timeout=timeout)
    return call_openai(model, system, user, base_url, api_key,
                       max_tokens=max_tokens, timeout=timeout)


# Retry policy: transient errors get backoff. Rate-limit + 5xx + timeout
# are transient; 4xx (except 429) are not.
_TRANSIENT_ERROR_SUBSTRINGS = (
    "rate_limit",         # OpenAI / Anthropic rate limit
    "429",                # generic
    "RateLimitError",
    "ServiceUnavailable",
    "InternalServerError",
    "ConnectionError",
    "APIConnectionError",
    "ReadTimeout",
    "Timeout",
    "Connection reset",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "504 Gateway Timeout",
    "overloaded_error",
)


def _is_transient(err: Exception) -> bool:
    """True if the error is worth retrying."""
    s = repr(err)
    if any(sub in s for sub in _TRANSIENT_ERROR_SUBSTRINGS):
        return True
    # Generic HTTP status check via attribute if present
    status = getattr(err, "status_code", None) or getattr(err, "status", None)
    if status in (408, 429, 500, 502, 503, 504, 529):
        return True
    return False


def call_model_with_retries(model: str, system: str, user: str,
                            base_url: str | None, api_key: str,
                            max_tokens: int = 2048,
                            timeout: float = 180.0,
                            max_retries: int = 8,
                            base_delay: float = 2.0,
                            max_delay: float = 90.0) -> tuple[str, dict]:
    """Wrap call_model with exponential backoff on transient errors.

    Backoff schedule (with jitter): 2, 4, 8, 16, 32, 64, 90, 90... seconds
    until max_retries exhausted. Non-transient errors fail fast.
    """
    import random as _random
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return call_model(model, system, user, base_url, api_key,
                              max_tokens=max_tokens, timeout=timeout)
        except Exception as e:
            last_err = e
            if not _is_transient(e) or attempt >= max_retries:
                raise
            # Exponential backoff with full jitter
            delay = min(base_delay * (2 ** attempt), max_delay)
            delay = delay * (0.5 + _random.random())
            time.sleep(delay)
    # Unreachable: loop either returns or raises
    if last_err is not None:
        raise last_err
    raise RuntimeError("call_model_with_retries: no attempts made")


# ── Trace loading ──────────────────────────────────────────────────────────

def load_traces(trace_dir: str) -> tuple[dict, dict]:
    """Load trace JSON files from a run directory tree.

    Walks trace_dir/<run_id>/<instance_id>/trace_output.json[.gz] and
    returns (traces, trace_dirs) where:
      traces[instance_id]    = parsed trace dict (most recent run wins)
      trace_dirs[instance_id] = Path to the instance directory
    Prefers .json.gz over .json when both are present.
    """
    import gzip as _gzip

    traces: dict = {}
    trace_dirs: dict = {}
    root = Path(trace_dir)
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        for inst_dir in sorted(run_dir.iterdir()):
            if not inst_dir.is_dir():
                continue
            gz = inst_dir / "trace_output.json.gz"
            plain = inst_dir / "trace_output.json"
            if gz.exists():
                with _gzip.open(gz, "rt", encoding="utf-8") as f:
                    traces[inst_dir.name] = json.load(f)
                trace_dirs[inst_dir.name] = inst_dir
            elif plain.exists():
                traces[inst_dir.name] = json.loads(plain.read_text())
                trace_dirs[inst_dir.name] = inst_dir
    return traces, trace_dirs


# ── Sample iteration & scoring ─────────────────────────────────────────────

def load_samples(samples_dir: Path, tasks: list[str] | None = None,
                 max_per_task: int | None = None,
                 sides: list[str] | None = None,
                 sample_fraction: float | None = None,
                 sample_seed: int = 0) -> list[dict]:
    """Walk benchmark_samples/<task>/<sample_id>.json. Return list of
    samples with 'path' added so we can re-load lazily later.

    sample_fraction (0, 1]: keep this fraction of instances (not samples).
    Sampling is at the *instance* level so all tasks + sides + parametric
    tests from the same SWE-bench instance go together — prevents the
    subset from being dominated by one repo. Same sample_seed → same
    subset across models so their scores are comparable.
    """
    out = []
    task_dirs = sorted(p for p in samples_dir.iterdir() if p.is_dir())

    # Build instance-level subset first
    instance_keep: set[str] | None = None
    if sample_fraction is not None and sample_fraction < 1.0:
        all_instances: set[str] = set()
        for tdir in task_dirs:
            for f in tdir.iterdir():
                if not f.name.endswith(".json"):
                    continue
                # sample_id format: <instance_id>::<hash>::<task>::<side>
                inst = f.name.split("::")[0]
                all_instances.add(inst)
        instances = sorted(all_instances)
        import random as _random
        rng = _random.Random(sample_seed)
        rng.shuffle(instances)
        k = max(1, int(round(len(instances) * sample_fraction)))
        instance_keep = set(instances[:k])
        print(f"sample_fraction={sample_fraction} seed={sample_seed} "
              f"keeps {len(instance_keep)} / {len(instances)} instances")

    for tdir in task_dirs:
        if tasks and tdir.name not in tasks:
            continue
        files = sorted(tdir.iterdir())
        n_kept = 0
        for f in files:
            if not f.name.endswith(".json"):
                continue
            if instance_keep is not None:
                inst = f.name.split("::")[0]
                if inst not in instance_keep:
                    continue
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            side = (d.get("metadata") or {}).get("side")
            if sides and side not in sides:
                continue
            d["__path"] = str(f)
            out.append(d)
            n_kept += 1
            if max_per_task and n_kept >= max_per_task:
                break
    return out


def score_one(sample: dict, prediction: Any) -> dict:
    """Score a single prediction against ground truth. Returns flat per-sample
    signals (see `score_combined_task`). `valid_pred` = 1 iff the model
    returned a parseable JSON dict; None predictions still get scored (they
    contribute fn=1 for failures, executed_ratio=0, etc.)."""
    metric = sample["metric"]
    args = sample.get("metric_args") or {}
    gt = sample["ground_truth"]
    out = score(metric, prediction, gt, args)
    out["valid_pred"] = 1 if prediction is not None else 0
    return out


# ── Runner ────────────────────────────────────────────────────────────────

def evaluate_one(sample: dict, model: str, base_url: str | None,
                 api_key: str, max_tokens: int) -> dict:
    """Send one sample to the model, parse, score. Returns rich record."""
    raw = ""
    usage = {}
    err = None
    t0 = time.time()
    try:
        raw, usage = call_model_with_retries(
            model, sample["system_prompt"], sample["user_prompt"],
            base_url, api_key, max_tokens=max_tokens,
        )
    except Exception as e:
        err = repr(e)[:512]
    dur = time.time() - t0

    parsed = parse_response(raw) if raw else None
    pred = extract_prediction(sample["task"], parsed)
    metrics = score_one(sample, pred)
    return {
        "sample_id": sample["sample_id"],
        "task": sample["task"],
        "instance_id": sample["instance_id"],
        "test_nodeid": sample["test_nodeid"],
        "side": (sample.get("metadata") or {}).get("side"),
        "model": model,
        "duration_s": round(dur, 2),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "raw_response": raw,
        "parsed_response": parsed,
        "prediction": pred,
        "ground_truth": sample["ground_truth"],
        "metric": sample["metric"],
        "metric_args": sample.get("metric_args") or {},
        "metrics": metrics,
        "error": err,
    }


def aggregate(results: list[dict]) -> dict:
    """Compute per-subtask aggregate scores. Only `combined` task is
    supported; other tasks (if any) are returned with raw count metadata
    only."""
    by_task: dict[str, list[dict]] = {}
    for r in results:
        by_task.setdefault(r["task"], []).append(r)
    out: dict[str, dict] = {}
    for task, recs in by_task.items():
        if task == "combined":
            out.update(aggregate_combined(recs))
        else:
            n = len(recs)
            n_valid = sum(1 for r in recs
                          if (r.get("metrics") or {}).get("valid_pred"))
            n_api_error = sum(1 for r in recs if r.get("error"))
            n_parse_failure = max(0, n - n_valid - n_api_error)
            out[task] = {
                "n": n,
                "n_valid": n_valid,
                "n_parse_failure": n_parse_failure,
                "n_api_error": n_api_error,
                "parse_failure_rate": (n_parse_failure / n) if n else 0.0,
                "api_error_rate": (n_api_error / n) if n else 0.0,
                "valid_pred_rate": (n_valid / n) if n else 0.0,
            }
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples_dir", required=True, type=str)
    parser.add_argument("--out_dir", required=True, type=str)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base_url", default=None,
                        help="Custom base URL (Nebius, etc). Default = OpenAI")
    parser.add_argument("--api_key_env", default=None,
                        help="Env var name for the API key. Defaults to "
                             "ANTHROPIC_API_KEY for claude-*, OPENAI_API_KEY otherwise.")
    parser.add_argument("--tasks", nargs="+", default=None,
                        help="Subset of tasks to run; default = all")
    parser.add_argument("--max_samples_per_task", type=int, default=None,
                        help="Limit samples per task (for smoke tests)")
    parser.add_argument("--sample_fraction", type=float, default=None,
                        help="Random fraction of INSTANCES to keep (0..1]. "
                             "Stratifies subsample at the instance level so "
                             "you don't end up evaluating only one repo. "
                             "Use --sample_seed to control reproducibility.")
    parser.add_argument("--sample_seed", type=int, default=20260511,
                        help="Seed for the instance-level subset. Same seed "
                             "across models → same instances → comparable scores.")
    parser.add_argument("--sides", nargs="+", default=None,
                        choices=["pre", "post"],
                        help="Limit to one side; default = both")
    parser.add_argument("--max_tokens", type=int, default=4096,
                        help="Output cap. Bump above 4096 for reasoning "
                             "models (gpt-oss family on Nebius eats its "
                             "budget on internal reasoning before content; "
                             "8192-16384 recommended for those).")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--resume", action="store_true",
                        help="Skip samples that already have a result file")
    args = parser.parse_args()

    key_env = args.api_key_env
    if key_env is None:
        key_env = "ANTHROPIC_API_KEY" if _is_anthropic(args.model) else "OPENAI_API_KEY"
    api_key = os.environ.get(key_env)
    if not api_key:
        print(f"ERROR: ${key_env} not set", file=sys.stderr)
        sys.exit(1)

    samples_dir = Path(args.samples_dir)
    out_dir = Path(args.out_dir) / args.model.replace("/", "_")
    out_dir.mkdir(parents=True, exist_ok=True)

    samples = load_samples(samples_dir, tasks=args.tasks,
                           max_per_task=args.max_samples_per_task,
                           sides=args.sides,
                           sample_fraction=args.sample_fraction,
                           sample_seed=args.sample_seed)
    print(f"Loaded {len(samples)} samples")

    if args.resume:
        existing = {p.stem for p in (out_dir / "responses").rglob("*.json")} \
            if (out_dir / "responses").exists() else set()
        before = len(samples)
        samples = [s for s in samples if s["sample_id"] not in existing]
        print(f"Resume: skipping {before - len(samples)} already-done")

    (out_dir / "responses").mkdir(exist_ok=True)
    results: list[dict] = []
    lock = threading.Lock()
    done = [0]

    def worker(s: dict) -> dict:
        rec = evaluate_one(s, args.model, args.base_url, api_key,
                           max_tokens=args.max_tokens)
        # Persist per-sample so we can resume
        out_file = out_dir / "responses" / f"{rec['sample_id']}.json"
        out_file.write_text(json.dumps(rec, indent=2))
        with lock:
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == len(samples):
                print(f"  {done[0]}/{len(samples)} done", flush=True)
        return rec

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for fut in as_completed(ex.submit(worker, s) for s in samples):
            try:
                results.append(fut.result())
            except Exception as e:
                print(f"Worker error: {e}", file=sys.stderr)

    # Re-load all per-sample results (including any from resume) for aggregation
    all_results: list[dict] = []
    for f in sorted((out_dir / "responses").rglob("*.json")):
        try:
            all_results.append(json.loads(f.read_text()))
        except Exception:
            continue

    agg = aggregate(all_results)
    summary = {
        "model": args.model,
        "base_url": args.base_url,
        "samples_dir": str(samples_dir),
        "n_samples_evaluated": len(all_results),
        "tasks": agg,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nWrote summary to {out_dir / 'summary.json'}")
    _headline_per_task = {
        "outcome": "f1",
        "peak_rss": "log10_mae",
        "wall_time": "log10_mae",
        "hot_methods_time": "ndcg_at_5",
        "hot_methods_alloc": "ndcg_at_5",
        "hot_lines_time": "ndcg_at_5",
        "hot_lines_alloc": "ndcg_at_5",
    }
    for task, info in sorted(agg.items()):
        print(f"  {task:30s} n={info.get('n', 0):4d}  "
              f"valid={info.get('n_valid', 0):4d}  "
              f"parse_fail={info.get('n_parse_failure', 0):3d}  "
              f"api_err={info.get('n_api_error', 0):3d}", end="")
        hk = _headline_per_task.get(task)
        if hk and info.get(hk) is not None:
            print(f"  {hk}={info[hk]:.3f}", end="")
        print()


if __name__ == "__main__":
    main()

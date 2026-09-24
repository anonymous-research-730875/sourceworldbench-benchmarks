"""trace → benchmark sample transformation.

Loads a v0.3 trace + repo snapshot, applies eligibility filters per task,
produces BenchmarkSample objects ready for serialization.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .samples import BenchmarkSample, SampleContext, make_sample_id

# Django FAIL_TO_PASS format: "test_method_name (dotted.module.path.ClassName)".
# Captures the dotted module + ClassName.
_DJANGO_F2P_RE = re.compile(r"^\S+\s+\(([\w.]+)\)\s*$")


def _test_file_from_f2p(f2p: list[str],
                        snapshot_dir: Path | list[Path]) -> str:
    """Pick a relative test file path for a session-trace record from the
    instance's FAIL_TO_PASS list.

    Three cases (in order):
      1. pytest format ("path/file.py::test_name") — split on "::".
      2. Django format ("test_method (dotted.module.ClassName)") — drop the
         class name (last `.<TitleCase>` segment), turn dots into slashes,
         and try both `<module>.py` and `tests/<module>.py` (the latter is
         where Django keeps its test modules in-repo) against the snapshot
         so we return a path that actually exists.
      3. Docstring-style entries (Django uses these for some custom tests)
         have no parseable path; we skip them and look at the next entry.
    """
    for tid in f2p or []:
        if "::" in tid:
            return tid.split("::", 1)[0]
        m = _DJANGO_F2P_RE.match(tid)
        if not m:
            continue
        dotted = m.group(1)
        # Drop the final class segment to get the module path.
        parts = dotted.split(".")
        if len(parts) > 1 and parts[-1][:1].isupper():
            parts = parts[:-1]
        if not parts:
            continue
        module_path = "/".join(parts) + ".py"
        # Django keeps its tests under `tests/` in the repo root; that's
        # where the file actually lives in the snapshot.
        dirs = [snapshot_dir] if isinstance(snapshot_dir, Path) else list(snapshot_dir)
        for candidate in (f"tests/{module_path}", module_path):
            if any((d / candidate).exists() for d in dirs):
                return candidate
        # Nothing matched the snapshot — return the bare guess so callers
        # at least get a hint of what was expected.
        return f"tests/{module_path}"
    return ""


# ── Source budget ──────────────────────────────────────────────────────────

SOURCE_BUDGET_CHARS = 400_000
PER_FILE_CHARS = 30_000
# Test file window: when the test file alone exceeds this many chars,
# carve a window keeping the prefix (imports + fixtures + helpers) and
# context around the target test function rather than truncating the
# tail. Models still need to see the test being asked about.
TEST_FILE_MAX_CHARS = 60_000
TEST_FILE_PREFIX_CHARS = 8_000
TEST_FILE_WINDOW_CHARS = 50_000


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + "\n... [truncated]"


def _test_func_name(test_nodeid: str) -> str:
    """Extract the test FUNCTION name from a test_nodeid in any of the
    three formats the benchmark supports.

    - pytest:   "path/to/file.py::test_name"           or
                "path/to/file.py::Class::test_name"    →  "test_name"
    - django:   "test_method (module.ClassName)"       →  "test_method"
    - bare:     "test_name"                            →  "test_name"
    """
    if "::" in test_nodeid:
        return test_nodeid.rsplit("::", 1)[1].split("[", 1)[0]
    if "(" in test_nodeid:
        return test_nodeid.split(" (", 1)[0].split("[", 1)[0]
    return test_nodeid.split("[", 1)[0]


def _carve_test_file_window(content: str, test_nodeid: str) -> str:
    """If `content` is too long to embed fully, return a windowed view:
    keep a prefix (imports/fixtures) and a context window centered on
    the target test function. If the function can't be located, fall
    back to plain prefix-truncation.

    Aligns the cut points to line boundaries so we don't slice a hunk
    of code mid-line.
    """
    if len(content) <= TEST_FILE_MAX_CHARS:
        return content
    func = _test_func_name(test_nodeid)
    pat = re.compile(rf"^\s*def\s+{re.escape(func)}\s*\(", re.MULTILINE)
    m = pat.search(content)
    if m is None:
        return content[:TEST_FILE_MAX_CHARS] + (
            f"\n# ... [test file truncated — target `{func}` not located]\n")
    # Align prefix end to the next newline after TEST_FILE_PREFIX_CHARS.
    prefix_end = content.find("\n", TEST_FILE_PREFIX_CHARS)
    if prefix_end == -1:
        prefix_end = TEST_FILE_PREFIX_CHARS
    # Window around target: half before, half after.
    half = TEST_FILE_WINDOW_CHARS // 2
    win_start = max(prefix_end + 1, m.start() - half)
    win_end = min(len(content), m.start() + half)
    # Align window edges to newlines.
    win_start_nl = content.rfind("\n", 0, win_start)
    if win_start_nl >= prefix_end:
        win_start = win_start_nl + 1
    win_end_nl = content.find("\n", win_end)
    if win_end_nl != -1:
        win_end = win_end_nl
    # If the prefix already covers the target, just return the prefix
    # extended to include the rest of the target's neighborhood.
    if m.start() < prefix_end:
        keep_to = max(prefix_end, win_end)
        return (content[:keep_to]
                + (f"\n# ... [test file truncated past byte {keep_to}]\n"
                   if keep_to < len(content) else ""))
    return (content[:prefix_end]
            + f"\n# ... [skipped {win_start - prefix_end} chars]\n"
            + content[win_start:win_end]
            + (f"\n# ... [skipped {len(content) - win_end} trailing chars]\n"
               if win_end < len(content) else ""))


def _read_repo_file(snapshot_dir: Path | list[Path],
                    rel_path: str) -> str | None:
    """Read a file from one or more snapshot directories, in order.

    Accepts a list to support sparse pre-patch snapshots: for pre samples,
    the caller passes [pre_dir, post_dir] — files the gold patch touched
    live in `pre_dir`; everything else (unchanged between pre and post)
    is read from `post_dir`. Passing a single Path keeps backward compat
    for post-only callers.
    """
    if isinstance(snapshot_dir, Path):
        dirs = [snapshot_dir]
    else:
        dirs = list(snapshot_dir)
    for d in dirs:
        fp = d / rel_path
        if fp.is_file():
            try:
                return fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
    return None


# ── Smart slicer ───────────────────────────────────────────────────────────
#
# The simple per-file truncate (`_truncate(c, PER_FILE_CHARS)`) drops the
# tail of every file longer than the cap. Measured on
# `benchmark_samples`, that cap (30 000 chars) clipped 25% of
# source files and removed executed code in 60.3% of samples.
#
# The smart slicer parses each file with `ast`, identifies every function /
# method that contains an executed line, and guarantees those bodies are
# always kept. The remaining budget is filled (in source order) with the
# module prefix and non-executed siblings. If essentials alone exceed the
# budget, they are kept anyway and the sample is flagged
# `budget_exceeded=True` in metadata.

# Marker inserted between two kept ranges (or at the head/tail of a file)
# to communicate which original lines were elided. Keeping the marker on a
# single line means an elision shifts subsequent line numbers in the
# rendered slice by exactly one line per gap, which is much easier to
# reason about than the previous "[truncated]" tail.
_ELISION_FMT = "# ... [{n} lines elided: original lines {a}-{b}]"


def _executed_lines_per_file(trace_test: dict) -> dict[str, set[int]]:
    """Union of `lines` keys and `functions[*].lineno`, by file."""
    out: dict[str, set[int]] = defaultdict(set)
    for fname, file_lines in (trace_test.get("lines") or {}).items():
        for ln in file_lines:
            try:
                out[fname].add(int(ln))
            except (TypeError, ValueError):
                pass
    for f in trace_test.get("functions") or []:
        fp = f.get("file") or ""
        ln = f.get("lineno")
        if fp and ln is not None:
            try:
                out[fp].add(int(ln))
            except (TypeError, ValueError):
                pass
    return out


def _node_span(node: ast.AST) -> tuple[int, int]:
    """(start_lineno, end_lineno) including decorators."""
    start = node.lineno
    decos = getattr(node, "decorator_list", None) or []
    if decos:
        start = min(start, min(d.lineno for d in decos))
    end = getattr(node, "end_lineno", None) or node.lineno
    return start, end


def _line_offsets(text: str) -> list[int]:
    """Char offset where each 1-based line starts. offsets[i] = start of line i.

    offsets[0] is unused. offsets[len(offsets)-1] == len(text) sentinel.
    """
    offs = [0, 0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offs.append(i + 1)
    offs.append(len(text))
    return offs


def _range_chars(offsets: list[int], a: int, b: int) -> int:
    a = max(1, a)
    b = min(len(offsets) - 2, b)
    if b < a:
        return 0
    return offsets[b + 1] - offsets[a]


def _classify(tree: ast.Module, exec_lines: set[int], nlines: int):
    """Return (essential_ranges, optional_ranges) — lists of (start, end)
    1-based inclusive line tuples, in source order.

    Essential:
      * module prefix (lines before first def/class)
      * every top-level FunctionDef containing an executed line
      * every top-level non-def statement (imports, module-level
        assignments, ``if __name__ == '__main__':`` blocks, …)
      * every ClassDef containing an executed line: keep the entire
        class span MINUS the bodies of non-executed methods. That
        preserves class-body content (attribute assignments, blank
        lines between methods, nested classes, decorators on class-
        level attributes) which the sysmon / settrace tracer can
        report as executed line events.
    Optional (droppable, in source order):
      * non-executed top-level functions
      * non-executed classes (entire body)
      * inside an executed class, non-executed methods
    """
    essential: list[tuple[int, int]] = []
    optional: list[tuple[int, int]] = []

    # Walk top-level items in source order with the same "everything is
    # essential except non-executed defs/classes" policy used inside
    # executed classes. This guarantees gaps BETWEEN top-level items
    # (blank lines, comments, module-level statements interleaved with
    # defs) end up in essential — Python's tracer can report those lines
    # as executed even when they're not the body of any def.
    top_items = []
    for node in tree.body:
        ns = node.lineno
        ne = getattr(node, "end_lineno", None) or ns
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ns, ne = _node_span(node)
        top_items.append((ns, ne, node))
    top_items.sort(key=lambda x: x[0])

    cursor = 1
    for ns, ne, node in top_items:
        if cursor < ns:
            essential.append((cursor, ns - 1))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            (essential if any(ns <= ln <= ne for ln in exec_lines)
             else optional).append((ns, ne))
        elif isinstance(node, ast.ClassDef):
            class_has_exec = any(ns <= ln <= ne for ln in exec_lines)
            if not class_has_exec:
                optional.append((ns, ne))
            else:
                # Walk the class body. Everything is essential except
                # non-executed methods.
                inner_cursor = ns
                body_items = []
                for inner in node.body:
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        bs, be = _node_span(inner)
                    else:
                        bs = inner.lineno
                        be = getattr(inner, "end_lineno", None) or bs
                    body_items.append((bs, be, inner))
                body_items.sort(key=lambda x: x[0])
                for bs, be, inner in body_items:
                    if inner_cursor < bs:
                        essential.append((inner_cursor, bs - 1))
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_exec = any(bs <= ln <= be for ln in exec_lines)
                        (essential if method_exec else optional).append((bs, be))
                    else:
                        essential.append((bs, be))
                    inner_cursor = be + 1
                if inner_cursor <= ne:
                    essential.append((inner_cursor, ne))
        else:
            essential.append((ns, ne))
        cursor = ne + 1
    if cursor <= nlines:
        essential.append((cursor, nlines))

    essential.sort()
    optional.sort()
    return essential, optional


def _render_slice(source: str, kept: list[tuple[int, int]]) -> str:
    """Render `kept` ranges of `source` with elision markers between gaps."""
    if not kept:
        return ""
    offs = _line_offsets(source)
    nlines = len(offs) - 2
    kept = sorted(kept)
    # Merge overlaps / adjacency.
    merged: list[list[int]] = []
    for a, b in kept:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    parts: list[str] = []
    cursor = 1
    for a, b in merged:
        if a > cursor:
            parts.append(_ELISION_FMT.format(n=a - cursor, a=cursor, b=a - 1))
        # Slice the source content for [a, b] inclusive.
        start = offs[a]
        end = offs[min(b + 1, len(offs) - 1)]
        body = source[start:end]
        if body.endswith("\n"):
            body = body[:-1]
        parts.append(body)
        cursor = b + 1
    if cursor <= nlines:
        parts.append(_ELISION_FMT.format(n=nlines - cursor + 1,
                                         a=cursor, b=nlines))
    return "\n".join(parts) + "\n"


def smart_slice_file(source: str, exec_lines: set[int],
                     budget: int | None) -> tuple[str, dict]:
    """Return (rendered_slice, stats).

    Always includes the essential set (prefix + every executed def). When
    `budget` is None, only essentials are rendered. Otherwise non-executed
    siblings are added in source order until `budget` would be exceeded.

    Stats fields:
      full_chars        : len(source)
      essential_chars   : chars used by essentials (in source bytes)
      kept_chars        : chars used by the rendered slice (with markers)
      essential_over_budget : True iff essentials alone exceed `budget`
                              (still kept anyway)
      parse_fail        : True if ast.parse failed (we return source as-is)
      exec_total        : count of `exec_lines`
      exec_in_kept      : exec_lines whose lineno is inside a kept range
    """
    stats = {
        "full_chars": len(source),
        "essential_chars": 0,
        "kept_chars": len(source),
        "essential_over_budget": False,
        "parse_fail": False,
        "exec_total": len(exec_lines),
        "exec_in_kept": len(exec_lines),
    }
    if not source.strip():
        stats["kept_chars"] = 0
        return source, stats
    try:
        tree = ast.parse(source)
    except SyntaxError:
        stats["parse_fail"] = True
        stats["essential_chars"] = len(source)
        return source, stats

    offs = _line_offsets(source)
    nlines = len(offs) - 2
    essential, optional = _classify(tree, exec_lines, nlines)

    essential_chars = sum(_range_chars(offs, a, b) for a, b in essential)
    stats["essential_chars"] = essential_chars

    kept = list(essential)
    used = essential_chars

    if budget is not None and used > budget:
        stats["essential_over_budget"] = True

    if budget is None or used >= budget:
        # No room for optionals (or no budget cap requested in
        # "essential-only" mode).
        pass
    else:
        for a, b in optional:
            c = _range_chars(offs, a, b)
            if used + c <= budget:
                kept.append((a, b))
                used += c

    rendered = _render_slice(source, kept) if kept else ""
    stats["kept_chars"] = len(rendered)
    if exec_lines:
        kept_sorted = sorted(kept)
        in_kept = 0
        for ln in exec_lines:
            for a, b in kept_sorted:
                if a <= ln <= b:
                    in_kept += 1
                    break
        stats["exec_in_kept"] = in_kept
    return rendered, stats


def collect_smart_context(
    trace_test: dict, snapshot_dir: Path | list[Path], test_file: str,
    budget: int = SOURCE_BUDGET_CHARS,
    shuffle_seed: int | str | None = None,
    test_nodeid: str | None = None,
) -> tuple[str, dict, dict]:
    """Smart-sliced context: every executed function/method is kept in full.

    Optional sibling defs fill the remaining budget. Files whose essential
    content alone exceeds the remaining budget are still kept (over-budget),
    and the sample is flagged via `source_stats["budget_exceeded"] = True`.

    Returns (test_file_content, source_files_dict, source_stats).
    """
    test_content_raw = _read_repo_file(snapshot_dir, test_file) or ""
    test_content = _carve_test_file_window(
        test_content_raw, test_nodeid or "") if test_content_raw else ""

    exec_by_file = _executed_lines_per_file(trace_test)

    seen: set[str] = set()
    files: list[str] = []
    for fn in trace_test.get("functions", []):
        fp = fn.get("file", "")
        if not fp or fp == test_file or fp in seen:
            continue
        seen.add(fp)
        files.append(fp)

    if shuffle_seed is None:
        shuffle_seed = test_file
    rng = random.Random(hashlib.sha256(str(shuffle_seed).encode()).hexdigest())
    rng.shuffle(files)

    used = len(test_content)
    source_files: dict[str, str] = {}
    per_file_stats: dict[str, dict] = {}
    over_budget_files: list[str] = []

    # Phase 1: keep every file's essential content unconditionally. The
    # remaining-budget calculation uses the live `used` so the first files
    # get more room for optionals than the last ones — same shape as the
    # current oracle implementation.
    for rel in files:
        raw = _read_repo_file(snapshot_dir, rel)
        if raw is None:
            continue
        exec_lines = exec_by_file.get(rel, set())
        remaining = max(0, budget - used)
        rendered, stats = smart_slice_file(raw, exec_lines, budget=remaining)
        # If essential alone exceeds the remaining budget, smart_slice_file
        # still returned the essential content (over-budget). Track it.
        if stats["essential_over_budget"] or used + stats["kept_chars"] > budget:
            over_budget_files.append(rel)
        source_files[rel] = rendered
        per_file_stats[rel] = stats
        used += stats["kept_chars"]

    source_stats = {
        "test_file_chars": len(test_content),
        "total_chars": used,
        "budget": budget,
        "budget_exceeded": used > budget,
        "n_files": len(source_files),
        "n_files_over_budget": len(over_budget_files),
        "files_over_budget": over_budget_files,
        "n_parse_failures": sum(1 for s in per_file_stats.values() if s["parse_fail"]),
        "essential_chars_total": sum(s["essential_chars"] for s in per_file_stats.values()),
    }
    return test_content, source_files, source_stats


def collect_oracle_context(
    trace_test: dict, snapshot_dir: Path | list[Path], test_file: str,
    budget: int = SOURCE_BUDGET_CHARS,
    shuffle_seed: int | str | None = None,
    test_nodeid: str | None = None,
) -> tuple[str, dict]:
    """Build the oracle context: test file + visited source files within budget.

    Files are emitted in a **deterministic random order** seeded by
    `shuffle_seed` (typically the sample_id), so the model can't infer
    importance from prompt position. The dict returned is a regular dict
    (insertion-ordered in CPython 3.7+) — order is preserved through
    JSON serialization, so downstream consumers see the same order.

    Returns (test_file_content, source_files_dict) where source_files maps
    rel_path -> content (excluding the test file itself).

    The test file is included in full when it fits under
    TEST_FILE_MAX_CHARS. Otherwise we carve a window: keep the prefix
    (imports / fixtures / helpers at the top of the file) and a wider
    window around the target test function. test_nodeid is used to
    locate that target; passing it is recommended for any session-mode
    or pytest record whose test_id is known.
    """
    test_content_raw = _read_repo_file(snapshot_dir, test_file) or ""
    test_content = _carve_test_file_window(
        test_content_raw, test_nodeid or "") if test_content_raw else ""

    # Collect visited project files (any file with a recorded function).
    seen: set[str] = set()
    files: list[str] = []
    for fn in trace_test.get("functions", []):
        fp = fn.get("file", "")
        if not fp or fp == test_file or fp in seen:
            continue
        seen.add(fp)
        files.append(fp)

    # Deterministic shuffle so the model doesn't get a ranking hint from
    # the order. Same sample_id → same order across builds + re-builds.
    if shuffle_seed is None:
        shuffle_seed = test_file
    rng = random.Random(hashlib.sha256(str(shuffle_seed).encode()).hexdigest())
    rng.shuffle(files)

    used = len(test_content)
    source_files: dict[str, str] = {}
    for rel in files:
        if used >= budget:
            break
        c = _read_repo_file(snapshot_dir, rel)
        if c is None:
            continue
        c = _truncate(c, PER_FILE_CHARS)
        # Final budget check
        if used + len(c) > budget:
            remaining = budget - used
            if remaining < 2000:
                break
            c = _truncate(c, remaining)
        source_files[rel] = c
        used += len(c)

    return test_content, source_files


# ── Trace loading ──────────────────────────────────────────────────────────

def load_trace(trace_path: Path) -> dict:
    if str(trace_path).endswith(".gz"):
        with gzip.open(trace_path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(trace_path.read_text())


# ── Eligibility filters ─────────────────────────────────────────────────────

OUTCOME_TASKS = {"outcome", "combined"}
TIME_TASKS = {"wall_time", "hot_methods_time", "hot_lines_time", "combined"}
RSS_TASKS = {"peak_rss"}
ALLOC_TASKS = {"hot_methods_alloc", "hot_lines_alloc", "peak_rss", "combined"}
LINE_TASKS = {"hot_lines_time", "hot_lines_alloc", "combined"}

# Number of top-K entries the model is asked to return for hotspot tasks.
TOP_K = 20


def is_eligible(task: str, trace_test: dict) -> bool:
    """True if this trace can produce a sample for this task."""
    # Skip totally trimmed traces — bias the rankings.
    if trace_test.get("trimmed"):
        for ti in trace_test.get("trim_info", []):
            if ti.get("reason") == "max_events":
                return False

    wall = trace_test.get("wall_time_s") or 0.0
    if task in TIME_TASKS or task in RSS_TASKS or "hot_" in task:
        if wall < 0.001:
            return False

    if task in OUTCOME_TASKS:
        outcome = trace_test.get("outcome")
        if outcome is None:
            return False
        if outcome == "skipped":
            return False
        # Merged outcome task: keep "passed", "failed", "error" all.
        # When outcome is "failed" or "error", we'll score failure_line and
        # exception_type as bonus fields IF the trace has them; missing
        # details just reduce the bonus, they don't drop the sample.
    # peak_rss uses max(tracemalloc-peak-delta, rss-peak-delta). Accept as
    # long as at least one signal was recorded.
    if task == "peak_rss":
        has_traced = (trace_test.get("peak_traced_bytes") is not None
                      and trace_test.get("start_traced_bytes") is not None)
        has_rss = (trace_test.get("peak_rss_bytes") is not None
                   and trace_test.get("start_rss_bytes") is not None)
        if not (has_traced or has_rss):
            return False

    if task in ALLOC_TASKS:
        if not trace_test.get("tracemalloc_enabled"):
            return False
        # Require that some functions have alloc data
        if task == "hot_methods_alloc":
            funcs = trace_test.get("functions") or []
            if not any(f.get("exclusive_alloc_bytes") for f in funcs):
                return False
        if task == "hot_lines_alloc":
            lines = trace_test.get("lines") or {}
            has = False
            for fl in lines.values():
                for info in fl.values():
                    if info.get("alloc_bytes"):
                        has = True
                        break
                if has:
                    break
            if not has:
                return False

    if task in LINE_TASKS:
        lines = trace_test.get("lines") or {}
        if not lines:
            return False

    return True


# ── Ground-truth extractors ─────────────────────────────────────────────────

def gt_outcome(t: dict) -> dict:
    """Merged outcome: label + failure_line + exception_type.

    For passed tests, failure_line and exception_type are None.
    For failed/error tests, they are populated when the tracer captured
    them (always for pytest; sometimes missing for session-mode runs).
    """
    o = t["outcome"]
    details = t.get("outcome_details") or {}
    if o in ("failed", "error"):
        label = "failed" if details.get("is_assertion") else "error"
        return {
            "outcome": label,
            "failure_line": int(details["failure_line"])
                if details.get("failure_line") is not None else None,
            "exception_type": details.get("exception_type") or None,
        }
    return {
        "outcome": o,  # "passed"
        "failure_line": None,
        "exception_type": None,
    }


def _excl_mem(f: dict) -> int:
    """Per-function combined exclusive memory: max(tracemalloc, RSS).

    Tracemalloc and RSS catch overlapping but non-identical sets of
    allocations. Taking the max per-frame credits each in-project
    method with the bigger of:
      - tracemalloc-visible (small Python objects, fine-grained)
      - RSS-delta (large malloc-based allocations such as numpy data
        buffers, which may bypass pymalloc on older numpy versions)

    Traces from tracer v0.3.1 and older don't have
    `exclusive_rss_bytes`; this falls back to tracemalloc alone for
    those. Re-mine with v0.3.2+ to pick up RSS attribution.
    """
    a = int(f.get("exclusive_alloc_bytes", 0) or 0)
    r = int(f.get("exclusive_rss_bytes", 0) or 0)
    return a if a >= r else r


def gt_peak_rss(t: dict) -> int:
    """Ground-truth: peak memory the test needs above its baseline, in
    bytes.

    Defined as additional working-set memory the test holds at its high
    water mark — i.e. "how much RAM headroom does the system need to
    run this test correctly?" That's the per-test PEAK minus the
    per-test BASELINE, taken as the larger of two complementary
    measurements (Python-heap via tracemalloc, process-wide via RSS):

      max( peak_traced_bytes - start_traced_bytes,
           peak_rss_bytes    - start_rss_bytes )

    Both baselines are captured at test ENTRY — tracemalloc has had
    `reset_peak()` called so `peak_traced_bytes` reflects only
    allocations made during this test, and `peak_rss_bytes` is the
    high-water of RSS sampled at frame boundaries during the test.

    Taking the max catches:
      * Python-heap allocations (lists, dicts, strings) via tracemalloc.
      * Large numpy / C-extension buffers that bypass pymalloc and only
        show up as RSS deltas.

    Previous formula summed per-method exclusive allocations across the
    trace. That over-counts allocate-and-free patterns: a method that
    does `for _ in range(100): x = list(range(1_000_000)); del x` has
    total≈800 MB but peak≈8 MB, and the test only needs ~8 MB of RAM.
    """
    peak_traced  = int(t.get("peak_traced_bytes")  or 0)
    start_traced = int(t.get("start_traced_bytes") or 0)
    peak_rss     = int(t.get("peak_rss_bytes")     or 0)
    start_rss    = int(t.get("start_rss_bytes")    or 0)
    return max(
        max(0, peak_traced - start_traced),
        max(0, peak_rss    - start_rss),
    )


def gt_wall_time(t: dict) -> float:
    """Total time in the patched code, in milliseconds.

    We use Σ(exclusive_time_s) across all traced functions rather than
    the raw `wall_time_s` of the pytest test record. This makes the
    target a property of the *code under analysis* — what the model
    can actually reason about — instead of test-harness overhead
    (pytest setup, fixture loading, traceback formatting on failures).

    Each traced function's `exclusive_time_s` is its wall time minus
    the wall time of in-project callees, so summing across functions
    counts every traced frame exactly once.
    """
    funcs = t.get("functions") or []
    total_s = sum(float(f.get("exclusive_time_s") or 0.0) for f in funcs)
    return total_s * 1000.0


def _func_values(t: dict, key: str) -> dict:
    """Map function name → metric value (e.g., exclusive_time_s)."""
    return {f["func"]: (f.get(key) or 0)
            for f in (t.get("functions") or []) if f.get("func")}


def _line_values(t: dict, key: str) -> dict:
    """Map `file:line` → metric value."""
    out = {}
    for fname, file_lines in (t.get("lines") or {}).items():
        for ln, info in file_lines.items():
            v = info.get(key) or 0
            if v > 0:
                out[f"{fname}:{ln}"] = v
    return out


def gt_hot_methods_time(t: dict, k: int = TOP_K) -> list[str]:
    funcs = sorted(
        t.get("functions") or [],
        key=lambda f: -(f.get("exclusive_time_s") or 0),
    )
    return [f["func"] for f in funcs[:k] if (f.get("exclusive_time_s") or 0) > 0]


def gt_hot_methods_alloc(t: dict, k: int = TOP_K) -> list[str]:
    funcs = sorted(
        t.get("functions") or [],
        key=lambda f: -_excl_mem(f),
    )
    return [f["func"] for f in funcs[:k] if _excl_mem(f) > 0]


def gt_hot_lines_time(t: dict, k: int = TOP_K) -> list[str]:
    flat = [(v, name) for name, v in _line_values(t, "time_ns").items()]
    flat.sort(reverse=True)
    return [s for _, s in flat[:k]]


def _line_combined_alloc(t: dict) -> dict:
    """Per-line combined memory: max(tracemalloc per-line, RSS per-line)."""
    out: dict = {}
    for fname, file_lines in (t.get("lines") or {}).items():
        for ln, info in file_lines.items():
            a = info.get("alloc_bytes", 0) or 0
            r = info.get("rss_bytes", 0) or 0
            v = a if a >= r else r
            if v > 0:
                out[f"{fname}:{ln}"] = v
    return out


def gt_hot_lines_alloc(t: dict, k: int = TOP_K) -> list[str]:
    flat = [(v, name) for name, v in _line_combined_alloc(t).items()]
    flat.sort(reverse=True)
    return [s for _, s in flat[:k]]


def gt_combined(t: dict) -> dict:
    """Single dict holding ground truth for every sub-task in the combined prompt."""
    return {
        "outcome": gt_outcome(t),
        "peak_bytes": gt_peak_rss(t),
        "wall_ms": gt_wall_time(t),
        "hot_methods_time": gt_hot_methods_time(t),
        "hot_methods_alloc": gt_hot_methods_alloc(t),
        "hot_lines_time": gt_hot_lines_time(t),
        "hot_lines_alloc": gt_hot_lines_alloc(t),
    }


GT_EXTRACTORS = {
    "combined": gt_combined,
    "outcome": gt_outcome,
    "peak_rss": gt_peak_rss,
    "wall_time": gt_wall_time,
    "hot_methods_time": gt_hot_methods_time,
    "hot_methods_alloc": gt_hot_methods_alloc,
    "hot_lines_time": gt_hot_lines_time,
    "hot_lines_alloc": gt_hot_lines_alloc,
}

METRIC = {
    "combined": "combined_task",
    "outcome": "outcome_combined",
    "peak_rss": "log10_mae",
    "wall_time": "log10_mae",
    "hot_methods_time": "hotspot_topk",
    "hot_methods_alloc": "hotspot_topk",
    "hot_lines_time": "hotspot_topk",
    "hot_lines_alloc": "hotspot_topk",
}


def value_lookup_for_task(task: str, t: dict) -> dict:
    """Build the `value_by_name` lookup attached to metric_args for hot tasks.

    Hotspot scoring needs the full per-name value table (not just the top-K
    names) so the time/alloc ratio metrics can compute how much of the actual
    cost is captured by the model's predictions.

    For the merged "combined" task, returns a dict-of-dicts keyed by the
    sub-task name; the scorer pulls the right lookup per sub-list.
    """
    if task == "hot_methods_time":
        return _func_values(t, "exclusive_time_s")
    if task == "hot_methods_alloc":
        return {f["func"]: _excl_mem(f)
                for f in (t.get("functions") or []) if f.get("func")}
    if task == "hot_lines_time":
        return _line_values(t, "time_ns")
    if task == "hot_lines_alloc":
        return _line_combined_alloc(t)
    if task == "combined":
        return {
            "hot_methods_time": _func_values(t, "exclusive_time_s"),
            "hot_methods_alloc": {f["func"]: _excl_mem(f)
                                  for f in (t.get("functions") or [])
                                  if f.get("func")},
            "hot_lines_time": _line_values(t, "time_ns"),
            "hot_lines_alloc": _line_combined_alloc(t),
        }
    return {}


# ── Prompt builders ─────────────────────────────────────────────────────────

def _format_source_files(test_file_path: str, test_content: str,
                         source_files: dict) -> str:
    """Format the user-prompt body. Source files first (context), then the
    test file last (the thing we're predicting about) — matches the order
    in docs/BENCHMARK_PROMPTS.md."""
    parts = []
    if source_files:
        parts.append("## Source files (slice of the project)\n")
        for rel_path, content in source_files.items():
            parts.append(f"### `{rel_path}`\n```python\n{content}\n```\n")
    parts.append(f"## Test file: `{test_file_path}`\n```python\n{test_content}\n```")
    return "\n".join(parts)


_SYSTEM_BASE = (
    "You are an expert Python developer analyzing a software project. "
    "You will be shown a slice of the project's source code and the full "
    "test file. You will predict a specific runtime property of running "
    "that test against THIS source code.\n\n"
    "You do NOT execute the code. Reason about it by reading the source. "
    "Respond with ONLY a JSON object — no prose, no markdown fences."
)

TASK_INSTRUCTIONS = {
    "combined": (
        "Predict the runtime behavior of running the test `{test_name}`\n"
        "against the source code shown above. Return a single JSON object\n"
        "with all of the following keys:\n\n"
        "  reasoning           — 2-4 sentences explaining your overall analysis\n"
        "  outcome             — \"passed\", \"failed\" (AssertionError), or \"error\"\n"
        "                        (non-assertion exception)\n"
        "  failure_line        — 1-based line in test file `{test_file}` where\n"
        "                        the failure occurs; null if outcome == passed\n"
        "  exception_type      — exception class name (\"AssertionError\",\n"
        "                        \"TypeError\", ...); null if outcome == passed\n"
        "  peak_bytes          — peak memory the test needs above its\n"
        "                        baseline, in bytes (int). i.e. how much\n"
        "                        additional RAM the system must have free\n"
        "                        for the test to run correctly. Concretely:\n"
        "                        the high-water mark of memory usage during\n"
        "                        test execution MINUS the memory already in\n"
        "                        use when the test started; taken as the\n"
        "                        larger of two complementary measurements:\n"
        "                          * Python-heap (tracemalloc) peak delta —\n"
        "                            catches lists / dicts / strings.\n"
        "                          * Process-RSS peak delta — catches large\n"
        "                            numpy / C-extension buffers that bypass\n"
        "                            pymalloc.\n"
        "                        Allocate-then-free patterns count their\n"
        "                        peak, NOT the cumulative bytes: a loop of\n"
        "                        100 iterations each allocating + freeing\n"
        "                        80 MB has peak_bytes ≈ 80 MB.\n"
        "                        Process-baseline (imports + state from\n"
        "                        prior tests in the session) is NOT counted.\n"
        "  wall_ms             — total wall-clock time to run the test, in\n"
        "                        milliseconds (float). What a stopwatch\n"
        "                        would show from when the test framework\n"
        "                        (pytest / unittest) invokes the test method\n"
        "                        to when it returns — the test method body\n"
        "                        plus any setUp / fixtures / tearDown.\n"
        "                        Includes time spent in stdlib, numpy,\n"
        "                        database drivers, network I/O, etc., —\n"
        "                        anything called from the test or its setup\n"
        "                        chain. Does NOT include test-runner\n"
        "                        collection or reporting time outside the\n"
        "                        test invocation.\n"
        "  hot_methods_time    — up to {top_k} fully-qualified function names\n"
        "                        from this project, ranked by total time\n"
        "                        spent executing them during the test.\n"
        "                        Uses EXCLUSIVE wall time: time in each\n"
        "                        function's own body, with time in nested\n"
        "                        in-project calls credited to the callee.\n"
        "                        Time in stdlib / numpy / third-party calls\n"
        "                        invoked from a method IS credited to it\n"
        "                        (we don't trace into those frames).\n"
        "                        Hottest first. Synthetic frames\n"
        "                        (`<lambda>`, `<listcomp>`, `<dictcomp>`,\n"
        "                        `<genexpr>`) are eligible.\n"
        "  hot_methods_alloc   — up to {top_k} fully-qualified function names\n"
        "                        from this project, ranked by total bytes\n"
        "                        ALLOCATED (directly or indirectly via\n"
        "                        library calls) while executing during the\n"
        "                        test. Counts EVERY allocation event,\n"
        "                        including transient allocations that are\n"
        "                        freed before the function returns — a\n"
        "                        method that builds a 100 MB array, uses\n"
        "                        it, and discards it inside one call gets\n"
        "                        full credit. Combines Python-heap\n"
        "                        (tracemalloc) and process-RSS deltas to\n"
        "                        capture pymalloc AND C-extension buffers.\n"
        "                        Exclusive: allocations in stdlib / numpy /\n"
        "                        third-party calls invoked from this method\n"
        "                        ARE credited to it (not traced), but\n"
        "                        allocations inside in-project child methods\n"
        "                        are credited to those children.\n"
        "                        Hottest allocator first.\n"
        "  hot_lines_time      — up to {top_k} `<rel_file_path>:<line_number>`\n"
        "                        strings, ranked by total wall time spent\n"
        "                        executing that line during the test\n"
        "                        (summed across every execution of the\n"
        "                        line). Use the paths from the source slice;\n"
        "                        only lines in those files are eligible.\n"
        "                        Hottest first.\n"
        "  hot_lines_alloc     — up to {top_k} `<rel_file_path>:<line_number>`\n"
        "                        strings, ranked by total bytes allocated\n"
        "                        when that line executes (summed across\n"
        "                        every execution of the line). Captures\n"
        "                        the line's own allocation activity plus\n"
        "                        any library / stdlib allocations made by\n"
        "                        code called from that line. Largest\n"
        "                        allocator first.\n\n"
        "Use \"reasoning\" to think before committing to a value. Return\n"
        "fewer than {top_k} entries in any list if you expect fewer that\n"
        "many to be relevant. Wrong names or paths in the lists score 0\n"
        "for that slot.\n\n"
        "Return JSON:\n"
        "{{\n"
        '  "reasoning": "<2-4 sentences>",\n'
        '  "outcome": "passed" | "failed" | "error",\n'
        '  "failure_line": <int> | null,\n'
        '  "exception_type": "<ClassName>" | null,\n'
        '  "peak_bytes": <int>,\n'
        '  "wall_ms": <float>,\n'
        '  "hot_methods_time": ["fn1", "fn2", ...],\n'
        '  "hot_methods_alloc": ["fn1", "fn2", ...],\n'
        '  "hot_lines_time": ["path/to/file.py:42", ...],\n'
        '  "hot_lines_alloc": ["path/to/file.py:42", ...]\n'
        "}}"
    ),
    "outcome": (
        "Predict what happens when the test `{test_name}` is run against the\n"
        "source code shown above.\n\n"
        "Possible outcomes:\n"
        "- \"passed\"  — the test completes without raising\n"
        "- \"failed\"  — the test raises AssertionError\n"
        "- \"error\"   — the test raises a non-assertion exception (TypeError,\n"
        "              ImportError, AttributeError, etc.)\n\n"
        "If you predict \"failed\" or \"error\", also predict:\n"
        "- the 1-based line number in the test file `{test_file}` at which\n"
        "  the failure occurs (where pytest's `longrepr` would point — the\n"
        "  failing assertion or the call that raises an unhandled exception),\n"
        "- the exact name of the exception class that will be raised. For\n"
        "  \"failed\" this is always \"AssertionError\".\n\n"
        "If you predict \"passed\", set `failure_line` to null and\n"
        "`exception_type` to null.\n\n"
        "Return JSON:\n"
        "{{\n"
        '  "reasoning": "<1-2 sentences>",\n'
        '  "outcome": "passed" | "failed" | "error",\n'
        '  "failure_line": <int> | null,\n'
        '  "exception_type": "<ClassName>" | null\n'
        "}}"
    ),
    "peak_rss": (
        "Predict the peak memory the test `{test_name}` needs above its\n"
        "baseline, in bytes — i.e. how much additional RAM the system must\n"
        "have free for the test to run correctly.\n\n"
        "Concretely: the high-water mark of memory usage during test\n"
        "execution MINUS the memory already in use when the test started.\n"
        "Two complementary signals; we take the larger:\n"
        "  * Python-heap peak delta (tracemalloc) — catches lists, dicts,\n"
        "    strings, and other Python objects.\n"
        "  * Process-RSS peak delta — catches large numpy arrays and other\n"
        "    C-extension buffers that bypass Python's allocator.\n\n"
        "Allocate-then-free patterns count their PEAK, not the cumulative\n"
        "bytes. A loop iterating 100 times, each iteration allocating then\n"
        "freeing an 80 MB array, has peak ≈ 80 MB (one iteration's size),\n"
        "NOT 8 GB.\n\n"
        "Process-baseline memory (imports, leftover state from previous\n"
        "tests in the same session) is NOT counted — we subtract whatever\n"
        "was already held at test entry.\n\n"
        "Return JSON:\n"
        "{{\n"
        '  "reasoning": "<1-2 sentences>",\n'
        '  "bytes": <int>\n'
        "}}"
    ),
    "wall_time": (
        "Predict the total wall-clock time to run the test `{test_name}`,\n"
        "in milliseconds.\n\n"
        "This is what a stopwatch would show from when the test framework\n"
        "(pytest / unittest) invokes the test method to when it returns:\n"
        "the test method body plus any setUp / fixtures / tearDown invoked\n"
        "for it.\n\n"
        "Includes time spent in stdlib, numpy, database drivers, network\n"
        "I/O, etc. — anything called from the test or its setup chain. Does\n"
        "NOT include test-runner collection or reporting time outside the\n"
        "test invocation.\n\n"
        "Use milliseconds as a float. 1.5 means 1.5 ms. 500 means 500 ms.\n\n"
        "Return JSON:\n"
        "{{\n"
        '  "reasoning": "<1-2 sentences>",\n'
        '  "milliseconds": <float>\n'
        "}}"
    ),
    "hot_methods_time": (
        "Predict the top-{top_k} in-project functions ranked by total time\n"
        "spent executing them when running the test `{test_name}`.\n\n"
        "Ranking metric: EXCLUSIVE wall time, summed across every call of\n"
        "the function during the test. \"Exclusive\" = time in the function's\n"
        "own body, with time in nested IN-PROJECT calls credited to the\n"
        "callees. Time in stdlib / numpy / third-party calls invoked from\n"
        "the function IS credited to it (we don't trace into those frames).\n\n"
        "Use fully-qualified names of the form `module.path.Class.method`\n"
        "or `module.path.function_name`. Match the names that pytest /\n"
        "Python itself would use for these callables.\n\n"
        "Only include functions defined within this project — not stdlib,\n"
        "not third-party packages. Synthetic frames (`<lambda>`,\n"
        "`<listcomp>`, `<dictcomp>`, `<genexpr>`) are eligible if they\n"
        "would qualify.\n\n"
        "Order from hottest to coldest. Return up to {top_k} entries. If\n"
        "you expect fewer than {top_k} distinct in-project functions to be\n"
        "called, return fewer.\n\n"
        "Return JSON:\n"
        "{{\n"
        '  "reasoning": "<1-2 sentences>",\n'
        '  "functions": ["fn1", "fn2", ...]\n'
        "}}"
    ),
    "hot_lines_time": (
        "Predict the top-{top_k} source lines ranked by total time spent\n"
        "executing them when running the test `{test_name}`.\n\n"
        "Ranking metric: wall time summed across every execution of the\n"
        "line during the test. Time in stdlib / third-party functions\n"
        "called from a line counts toward that line.\n\n"
        "Each entry must be of the form `<rel_file_path>:<line_number>`,\n"
        "where the path is relative to the repository root and the line\n"
        "number is 1-based. Use the paths shown in the source slice and\n"
        "test file above; only lines in those files are eligible.\n\n"
        "Order from hottest to coldest. Return up to {top_k} entries. If\n"
        "you expect fewer than {top_k} distinct lines to dominate, return\n"
        "fewer.\n\n"
        "Return JSON:\n"
        "{{\n"
        '  "reasoning": "<1-2 sentences>",\n'
        '  "lines": ["path/to/file.py:42", "..."]\n'
        "}}"
    ),
    "hot_methods_alloc": (
        "Predict the top-{top_k} in-project functions ranked by total\n"
        "bytes ALLOCATED (directly or indirectly via library calls) while\n"
        "executing during the test `{test_name}`.\n\n"
        "Counts EVERY allocation event, including transient allocations\n"
        "that are freed before the function returns. A method that builds\n"
        "a 100 MB array, uses it, and discards it inside one call gets\n"
        "full credit for 100 MB on each invocation. Same method called\n"
        "100 times → 100 × that.\n\n"
        "Combines two measurements to capture both pymalloc and\n"
        "C-extension allocations:\n"
        "  * Python-heap (tracemalloc) deltas during the call.\n"
        "  * Process-RSS deltas — catches large numpy / C-extension\n"
        "    buffers that bypass Python's allocator.\n\n"
        "Exclusive allocation: allocations performed in stdlib / numpy /\n"
        "third-party calls invoked from this method ARE credited to it\n"
        "(we don't trace into those frames). Allocations inside nested\n"
        "IN-PROJECT calls are credited to those callees instead.\n\n"
        "Use fully-qualified names of the form `module.path.Class.method`\n"
        "or `module.path.function_name`. Only project-internal functions\n"
        "are eligible. Order from largest allocator to smallest. Return up\n"
        "to {top_k} entries. If you expect fewer than {top_k} distinct\n"
        "in-project allocating functions, return fewer.\n\n"
        "Return JSON:\n"
        "{{\n"
        '  "reasoning": "<1-2 sentences>",\n'
        '  "functions": ["fn1", "fn2", ...]\n'
        "}}"
    ),
    "hot_lines_alloc": (
        "Predict the top-{top_k} source lines ranked by total bytes\n"
        "ALLOCATED when that line executes, summed across every execution\n"
        "during the test `{test_name}`.\n\n"
        "Captures the line's own allocation activity plus any library /\n"
        "stdlib allocations made by code called from that line. Counts\n"
        "transient allocations (allocated then freed within the line's\n"
        "execution window): a line that constructs a 100 MB list and\n"
        "discards it still ranks high.\n\n"
        "Combines Python-heap (tracemalloc) deltas and process-RSS deltas\n"
        "between consecutive line events to catch both pymalloc and\n"
        "C-extension / numpy allocations.\n\n"
        "Each entry must be of the form `<rel_file_path>:<line_number>`,\n"
        "1-based line numbers, paths from the source slice. Order from\n"
        "largest allocator to smallest. Return up to {top_k} entries. If\n"
        "you expect fewer than {top_k} distinct lines to dominate, return\n"
        "fewer.\n\n"
        "Return JSON:\n"
        "{{\n"
        '  "reasoning": "<1-2 sentences>",\n'
        '  "lines": ["path/to/file.py:42", "..."]\n'
        "}}"
    ),
}


def build_user_prompt(task: str, repo: str, instance: dict, test_nodeid: str,
                      test_file: str, test_content: str, source_files: dict,
                      problem_statement: str) -> str:
    # Note: problem_statement (the SWE-bench bug report) and `repo` are
    # intentionally NOT embedded in the prompt — we want the model to treat
    # this as a generic "predict runtime behavior of this code" task with
    # no issue-tracker context, regardless of whether this sample came from
    # the pre-patch or post-patch trace.
    parts = [
        _format_source_files(test_file, test_content, source_files),
        "## Task",
        TASK_INSTRUCTIONS[task].format(
            test_name=test_nodeid, test_file=test_file, top_k=TOP_K,
        ),
    ]
    return "\n\n".join(parts)


# ── Top-level builder ──────────────────────────────────────────────────────

def build_samples_for_trace(
    trace_path: Path, snapshot_dir: Path | list[Path], instance_meta: dict,
    tasks: Iterable[str] | None = None,
    side: str = "post",
    context_strategy: str = "smart",
) -> list[BenchmarkSample]:
    """Build all eligible samples for a single instance's trace.

    instance_meta must contain: instance_id, repo, base_commit, problem_statement.
    `side` is "post" or "pre". The same (instance, test, task) tuple produces
    two samples (post + pre) with distinct sample_ids when both traces exist.

    `context_strategy`:
      * "smart" (default) — AST-guided slice that keeps every executed
        function/method in full, then fills the remaining budget with
        non-executed siblings. Files whose essential content alone
        exceeds the budget are kept anyway and flagged in
        ``metadata['source_stats']['budget_exceeded']``.
      * "oracle" — legacy per-file truncate at PER_FILE_CHARS.
    """
    if tasks is None:
        tasks = list(GT_EXTRACTORS.keys())

    trace = load_trace(trace_path)
    out: list[BenchmarkSample] = []
    repo = instance_meta.get("repo") or trace.get("instance_id", "").replace("__", "/")
    instance_id = trace.get("instance_id") or instance_meta.get("instance_id")
    base_commit = instance_meta.get("base_commit") or ""
    problem_statement = instance_meta.get("problem_statement") or ""

    # Build a set of FAIL_TO_PASS test ids (and their bare/parametrized forms)
    # to filter the trace down to just those tests. SWE-bench traces collected
    # *before* the in-tracer filter was added contain every test in the file;
    # this post-filter cleans those up at sample-build time.
    f2p_raw = instance_meta.get("FAIL_TO_PASS") or []
    if isinstance(f2p_raw, str):
        try:
            f2p_raw = json.loads(f2p_raw)
        except Exception:
            f2p_raw = []
    f2p = set(f2p_raw)

    def _keep(nodeid: str, t: dict) -> bool:
        # Strict equality with the dataset's FAIL_TO_PASS list. Drops:
        #   - the synthetic "session" catch-all bucket (its nodeid is
        #     "session", never appears in f2p) — for v0.4 session-mode
        #     traces, per-test records are the source of truth and the
        #     session bucket would only contribute session-aggregate
        #     numbers at the wrong granularity.
        #   - parametric variants / sibling tests not named in f2p —
        #     SWE-bench grades only the exact entries in FAIL_TO_PASS,
        #     and our benchmark must match that scope.
        # If f2p is empty (rare; e.g. dataset-less debug runs), fall
        # back to keeping everything except "session".
        if not f2p:
            return nodeid != "session"
        return nodeid in f2p

    for test_nodeid, t in (trace.get("tests") or {}).items():
        # Skip degenerate session-mode entries with no functions
        if not t.get("functions"):
            continue
        if not _keep(test_nodeid, t):
            continue

        # Determine test file for context: from test_nodeid (pytest) or
        # extract directly from the current test_nodeid (session traces).
        # IMPORTANT: derive the test file from THIS specific nodeid — not
        # from the F2P list as a whole — because in a session trace the
        # F2P list often spans multiple test files and the first one
        # doesn't match the test we're building a sample for.
        if "::" in test_nodeid:
            test_file = test_nodeid.split("::")[0]
        else:
            test_file = _test_file_from_f2p([test_nodeid], snapshot_dir)
            if not test_file:
                funcs = t.get("functions") or []
                test_file = funcs[0]["file"] if funcs else ""

        # Seed the file-order shuffle with (instance_id, test_nodeid, side)
        # — deterministic per sample, but different across pre/post sides
        # (so the model can't memorize positions across the pair).
        shuffle_seed = f"{instance_id}::{test_nodeid}::{side}"
        source_stats: dict | None = None
        if context_strategy == "smart":
            test_content, source_files, source_stats = collect_smart_context(
                t, snapshot_dir, test_file, shuffle_seed=shuffle_seed,
                test_nodeid=test_nodeid,
            )
        else:
            test_content, source_files = collect_oracle_context(
                t, snapshot_dir, test_file, shuffle_seed=shuffle_seed,
                test_nodeid=test_nodeid,
            )
        if not test_content and not source_files:
            continue

        for task in tasks:
            if not is_eligible(task, t):
                continue
            try:
                gt = GT_EXTRACTORS[task](t)
            except Exception:
                continue
            # Skip empty hot lists — can't score those.
            if isinstance(gt, list) and not gt:
                continue

            sid = make_sample_id(instance_id, test_nodeid, task, side=side)
            metric_args: dict = {}
            if task.startswith("hot_"):
                metric_args = {
                    "k_list": [1, 5, 20],
                    "value_by_name": value_lookup_for_task(task, t),
                }
            elif task == "combined":
                metric_args = {
                    "k_list": [1, 5, 20],
                    "values_by_subtask": value_lookup_for_task(task, t),
                }
            sample = BenchmarkSample(
                sample_id=sid,
                task=task,
                instance_id=instance_id,
                test_nodeid=test_nodeid,
                repo=repo,
                base_commit=base_commit,
                context=SampleContext(
                    test_file_path=test_file,
                    test_file_content=test_content,
                    source_files=source_files,
                    context_strategy=context_strategy,
                ),
                system_prompt=_SYSTEM_BASE,
                user_prompt=build_user_prompt(
                    task, repo, instance_meta, test_nodeid,
                    test_file, test_content, source_files, problem_statement,
                ),
                ground_truth=gt,
                metric=METRIC[task],
                metric_args=metric_args,
                metadata={
                    "side": side,
                    "wall_time_s": t.get("wall_time_s"),
                    "peak_rss_bytes": t.get("peak_rss_bytes"),
                    "peak_traced_bytes": t.get("peak_traced_bytes"),
                    "outcome": t.get("outcome"),
                    "tracer_version": trace.get("tracer_version"),
                    "memory_tracking": trace.get("memory_tracking"),
                    "trace_level": trace.get("trace_level"),
                    "source_stats": source_stats,
                },
            )
            out.append(sample)

    return out

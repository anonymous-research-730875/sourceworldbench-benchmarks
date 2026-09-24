# ruff: noqa: E501
"""Render eval_results/ into a self-contained HTML report.

Per-task tables show the new metric set (P/R/F1, log-log linear fit + log10
MAE, executed-ratio, NDCG@20). Old metrics (overlap@k, LCS, ratio@k,
bucket_acc_3, etc.) are gone.

Usage:
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.build_report \
        --root eval_results \
        --out docs/experiment_report.html
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from sourceworldbench_benchmarks.execution_tracer.benchmark.scoring import (
    EPS_BY_FIELD,
    aggregate_combined,
    score_combined_task,
)

TASK_ORDER = [
    "outcome", "peak_rss", "wall_time",
    "hot_methods_time", "hot_methods_alloc",
    "hot_lines_time", "hot_lines_alloc",
]
TASK_DESC = {
    "outcome": "Pass / fail / error label. Scored as binary classification "
               "with non-pass (failed or error) treated as the positive "
               "class — precision, recall, F1 are computed over the whole "
               "task. exception_match is the rate of exact exception-class "
               "match on samples whose GT is a failure (0 when the model "
               "didn't predict a class).",
    "peak_rss": "Predict peak Python heap allocation (tracemalloc) in bytes. "
                "We fit log10(max(pred, 10_000)) = a · log10(max(gt, 10_000)) "
                "+ b across all samples and report slope a (1 = perfect "
                "calibration) and bias b (0 = no offset). log10 MAE is the "
                "mean absolute log10 error. Values below 10 KB are floored "
                "at 10 KB so log10 is well-defined; values ≥ 10 KB are "
                "untouched.",
    "wall_time": "Predict wall-clock time in milliseconds. We fit "
                 "log10(max(pred, 0.01)) = a · log10(max(gt, 0.01)) + b. "
                 "Values below 0.01 ms are floored at 0.01 ms; values ≥ "
                 "0.01 ms are untouched. slope=1 + bias=0 is perfect "
                 "calibration.",
    "hot_methods_time": "Predict the top-20 functions by exclusive wall-clock "
                        "time. executed_ratio is the fraction of predicted "
                        "names that were actually executed in the trace; "
                        "NDCG@5 grades the ordering of the top-5 predictions "
                        "using self-time as relevance (unexecuted names → 0); "
                        "recall@5 is the rate at which the single hottest "
                        "ground-truth function appears in the predicted top-5.",
    "hot_methods_alloc": "Predict the top-20 functions by exclusive Python "
                         "heap allocation. executed_ratio + NDCG@5 + "
                         "recall@5 with allocation bytes as relevance.",
    "hot_lines_time": "Predict the top-20 source lines by exclusive wall "
                      "time. executed_ratio + NDCG@5 + recall@5 with line "
                      "self-time as relevance.",
    "hot_lines_alloc": "Predict the top-20 source lines by exclusive heap "
                       "allocation. executed_ratio + NDCG@5 + recall@5.",
}
# (column_key, arrow, label) — arrow only used for the headline.
HEADLINE = {
    "outcome":           ("f1",        "↑", "F1"),
    "peak_rss":          ("log10_mae", "↓", "log10 MAE"),
    "wall_time":         ("log10_mae", "↓", "log10 MAE"),
    "hot_methods_time":  ("ndcg_at_5", "↑", "NDCG@5"),
    "hot_methods_alloc": ("ndcg_at_5", "↑", "NDCG@5"),
    "hot_lines_time":    ("ndcg_at_5", "↑", "NDCG@5"),
    "hot_lines_alloc":   ("ndcg_at_5", "↑", "NDCG@5"),
}

# Display names for known model dirs.
MODEL_DISPLAY = {
    "gpt-5.5": "gpt-5.5",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.2": "gpt-5.2",
    "gpt-5-mini": "gpt-5-mini",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-haiku-4-5": "claude-haiku-4-5",
    "openai_gpt-oss-120b": "gpt-oss-120b",
    "Qwen_Qwen3-235B-A22B-Instruct-2507": "Qwen3-235B-A22B-Instruct",
    "Qwen_Qwen3-30B-A3B-Instruct-2507": "Qwen3-30B-A3B-Instruct",
}


# Secondary metric columns surfaced in each per-task table.
EXTRA_COLS = {
    "outcome": [
        ("precision",       "precision↑",       "{:.3f}"),
        ("recall",          "recall↑",          "{:.3f}"),
        ("f1",              "F1↑",              "{:.3f}"),
        ("exception_match", "exception_match↑", "{:.3f}"),
        ("n_failure_gt",    "n_failure_gt",     "{:d}"),
    ],
    "peak_rss": [
        ("slope",        "slope (1=ideal)",   "{:+.3f}"),
        ("bias",         "bias (0=ideal)",    "{:+.3f}"),
        ("residual_std", "resid σ↓ (0=ideal)", "{:.3f}"),
        ("log10_mae",    "log10 MAE↓",        "{:.3f}"),
        ("n_fitted",     "n_fitted",          "{:d}"),
    ],
    "wall_time": [
        ("slope",        "slope (1=ideal)",   "{:+.3f}"),
        ("bias",         "bias (0=ideal)",    "{:+.3f}"),
        ("residual_std", "resid σ↓ (0=ideal)", "{:.3f}"),
        ("log10_mae",    "log10 MAE↓",        "{:.3f}"),
        ("n_fitted",     "n_fitted",          "{:d}"),
    ],
    "hot_methods_time": [
        ("executed_ratio",     "exec_ratio↑",    "{:.3f}"),
        ("ndcg_at_5",          "NDCG@5↑",        "{:.3f}"),
        ("ndcg_gt5_shuffled",  "GT5 shuf",       "{:.3f}"),
        ("ndcg_gt20_shuffled", "GT20 shuf",      "{:.3f}"),
        ("ndcg_pred_shuffled", "pred shuf",      "{:.3f}"),
        ("ndcg_pred_oracle",   "pred oracle↑",   "{:.3f}"),
        ("recall_at_5_top1",   "recall@5↑",      "{:.3f}"),
    ],
    "hot_methods_alloc": [
        ("executed_ratio",     "exec_ratio↑",    "{:.3f}"),
        ("ndcg_at_5",          "NDCG@5↑",        "{:.3f}"),
        ("ndcg_gt5_shuffled",  "GT5 shuf",       "{:.3f}"),
        ("ndcg_gt20_shuffled", "GT20 shuf",      "{:.3f}"),
        ("ndcg_pred_shuffled", "pred shuf",      "{:.3f}"),
        ("ndcg_pred_oracle",   "pred oracle↑",   "{:.3f}"),
        ("recall_at_5_top1",   "recall@5↑",      "{:.3f}"),
    ],
    "hot_lines_time": [
        ("executed_ratio",     "exec_ratio↑",    "{:.3f}"),
        ("ndcg_at_5",          "NDCG@5↑",        "{:.3f}"),
        ("ndcg_gt5_shuffled",  "GT5 shuf",       "{:.3f}"),
        ("ndcg_gt20_shuffled", "GT20 shuf",      "{:.3f}"),
        ("ndcg_pred_shuffled", "pred shuf",      "{:.3f}"),
        ("ndcg_pred_oracle",   "pred oracle↑",   "{:.3f}"),
        ("recall_at_5_top1",   "recall@5↑",      "{:.3f}"),
    ],
    "hot_lines_alloc": [
        ("executed_ratio",     "exec_ratio↑",    "{:.3f}"),
        ("ndcg_at_5",          "NDCG@5↑",        "{:.3f}"),
        ("ndcg_gt5_shuffled",  "GT5 shuf",       "{:.3f}"),
        ("ndcg_gt20_shuffled", "GT20 shuf",      "{:.3f}"),
        ("ndcg_pred_shuffled", "pred shuf",      "{:.3f}"),
        ("ndcg_pred_oracle",   "pred oracle↑",   "{:.3f}"),
        ("recall_at_5_top1",   "recall@5↑",      "{:.3f}"),
    ],
}


def _rescore_record(rec: dict) -> dict:
    """Re-run scoring so the record reflects the current metric definitions.
    Metrics stored on disk may have been written by an older scoring version."""
    pred = rec.get("prediction")
    gt = rec.get("ground_truth") or {}
    args = rec.get("metric_args") or {}
    metrics = score_combined_task(
        pred, gt,
        k_list=args.get("k_list"),
        values_by_subtask=args.get("values_by_subtask"),
    )
    metrics["valid_pred"] = 1 if pred is not None else 0
    new = dict(rec)
    new["metrics"] = metrics
    return new


def load_model_records(model_dir: Path) -> list[dict]:
    rdir = model_dir / "responses"
    if not rdir.is_dir():
        return []
    out: list[dict] = []
    for f in rdir.iterdir():
        if not f.name.endswith(".json"):
            continue
        try:
            rec = json.loads(f.read_text())
        except Exception:
            continue
        if rec.get("task") != "combined":
            continue
        out.append(_rescore_record(rec))
    return out


def _outcome_counts_from_records(recs: list[dict]) -> dict:
    counts = {"passed": 0, "failed": 0, "error": 0, "other_or_none": 0}
    for r in recs:
        pred = r.get("prediction") or {}
        outcome_obj = pred.get("outcome") if isinstance(pred, dict) else None
        o = ""
        if isinstance(outcome_obj, dict):
            o = (outcome_obj.get("outcome") or "").strip().lower()
        if o == "passed":
            counts["passed"] += 1
        elif o == "failed":
            counts["failed"] += 1
        elif o == "error":
            counts["error"] += 1
        else:
            counts["other_or_none"] += 1
    return counts


def _gt_outcome_distribution(recs: list[dict]) -> dict:
    counts = {"passed": 0, "failed": 0, "error": 0}
    for r in recs:
        gt = (r.get("ground_truth") or {}).get("outcome") or {}
        o = (gt.get("outcome") or "").strip().lower() if isinstance(gt, dict) else ""
        if o in counts:
            counts[o] += 1
    return counts


def _top_predictions(recs: list[dict], field: str, n: int = 3) -> list[tuple[float, int]]:
    from collections import Counter
    vals: list[float] = []
    for r in recs:
        pred = r.get("prediction") or {}
        if not isinstance(pred, dict):
            continue
        v = pred.get(field)
        if v is not None:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
    if not vals:
        return []
    return Counter(vals).most_common(n)


def _fmt_field_val(v: float, field: str) -> str:
    if field == "wall_ms":
        if v >= 1000:
            return f"{v / 1000:.3g} s"
        if v >= 1:
            return f"{v:.3g} ms"
        return f"{v:.3g} ms"
    if field == "peak_bytes":
        if v >= 1e9:
            return f"{v / 1e9:.3g} GB"
        if v >= 1e6:
            return f"{v / 1e6:.3g} MB"
        if v >= 1e3:
            return f"{v / 1e3:.3g} KB"
        return f"{v:.3g} B"
    return f"{v:g}"


def _render_top_preds(top_preds: list[tuple[float, int]], field: str) -> str:
    if not top_preds:
        return ""
    items = ", ".join(
        f'<span class="top-pred-item">{_fmt_field_val(v, field)}'
        f'<span class="top-pred-count"> ×{c}</span></span>'
        for v, c in top_preds
    )
    return f'<div class="top-preds">top-3 predicted: {items}</div>'


def _scatter_pairs(recs: list[dict], field: str) -> list[tuple[float, float, str]]:
    pairs = []
    for r in recs:
        pred = r.get("prediction") or {}
        gt = r.get("ground_truth") or {}
        if not isinstance(pred, dict):
            continue
        p = pred.get(field)
        g = gt.get(field)
        try:
            pv = float(p) if p is not None else None
            gv = float(g) if g is not None else None
        except (TypeError, ValueError):
            continue
        if pv is None or gv is None:
            continue
        pv = max(pv, 0.0)
        gv = max(gv, 0.0)
        pairs.append((gv, pv, r.get("side") or ""))
    return pairs


def _render_scatter_svg(pairs, *, width=380, height=320,
                        x_label: str, y_label: str, title: str,
                        unit_label: str = "",
                        slope: float | None = None,
                        bias: float | None = None,
                        eps: float = 1.0) -> str:
    """Scatter pred-vs-gt on log10 axes. Orange dashed = identity (perfect
    prediction); cyan solid = fitted log10-log10 regression."""
    import math
    if not pairs:
        return f'<div class="plot-empty">No data for {html.escape(title)}</div>'
    xs = [math.log10(max(g, eps)) for g, p, _ in pairs]
    ys = [math.log10(max(p, eps)) for g, p, _ in pairs]
    lo = min(min(xs), min(ys))
    hi = max(max(xs), max(ys))
    pad_x = (hi - lo) * 0.05 + 0.2
    lo -= pad_x
    hi += pad_x

    margin_l, margin_r, margin_t, margin_b = 50, 12, 26, 36
    pw = width - margin_l - margin_r
    ph = height - margin_t - margin_b

    def to_px_x(v):
        return margin_l + (v - lo) / (hi - lo) * pw

    def to_px_y(v):
        return margin_t + (hi - v) / (hi - lo) * ph

    parts = [
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#1a1b26"/>',
        f'<text x="{width/2}" y="16" text-anchor="middle" font-size="12" '
        f'font-family="sans-serif" fill="#c0caf5">{html.escape(title)}</text>',
    ]

    tick_min = int(math.floor(lo))
    tick_max = int(math.ceil(hi))
    for t in range(tick_min, tick_max + 1):
        if t < lo or t > hi:
            continue
        gx = to_px_x(t)
        gy = to_px_y(t)
        parts.append(
            f'<line x1="{gx:.1f}" y1="{margin_t}" x2="{gx:.1f}" '
            f'y2="{height - margin_b}" stroke="#3b4261" stroke-width="0.5"/>'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{height - margin_b + 14}" text-anchor="middle" '
            f'font-size="10" font-family="sans-serif" fill="#a9b1d6">'
            f'10^{t}</text>'
        )
        parts.append(
            f'<line x1="{margin_l}" y1="{gy:.1f}" x2="{width - margin_r}" '
            f'y2="{gy:.1f}" stroke="#3b4261" stroke-width="0.5"/>'
        )
        parts.append(
            f'<text x="{margin_l - 4}" y="{gy + 3:.1f}" text-anchor="end" '
            f'font-size="10" font-family="sans-serif" fill="#a9b1d6">'
            f'10^{t}</text>'
        )

    parts.append(
        f'<line x1="{to_px_x(lo):.1f}" y1="{to_px_y(lo):.1f}" '
        f'x2="{to_px_x(hi):.1f}" y2="{to_px_y(hi):.1f}" '
        f'stroke="#e0af68" stroke-width="1" stroke-dasharray="3,3" opacity="0.7"/>'
    )

    if slope is not None and bias is not None:
        y_lo = slope * lo + bias
        y_hi = slope * hi + bias
        parts.append(
            f'<line x1="{to_px_x(lo):.1f}" y1="{to_px_y(y_lo):.1f}" '
            f'x2="{to_px_x(hi):.1f}" y2="{to_px_y(y_hi):.1f}" '
            f'stroke="#7dcfff" stroke-width="1.5" opacity="0.85"/>'
        )

    for g, p, side in pairs:
        lx = math.log10(max(g, eps))
        ly = math.log10(max(p, eps))
        color = "#f7768e" if side == "pre" else "#9ece6a"
        parts.append(
            f'<circle cx="{to_px_x(lx):.1f}" cy="{to_px_y(ly):.1f}" r="2.2" '
            f'fill="{color}" fill-opacity="0.55" stroke="none"/>'
        )

    parts.append(
        f'<text x="{margin_l + pw/2}" y="{height - 4}" text-anchor="middle" '
        f'font-size="11" font-family="sans-serif" fill="#a9b1d6">'
        f'{html.escape(x_label)} ({unit_label}, log10)</text>'
    )
    parts.append(
        f'<text x="{14}" y="{margin_t + ph/2}" text-anchor="middle" '
        f'font-size="11" font-family="sans-serif" fill="#a9b1d6" '
        f'transform="rotate(-90 14 {margin_t + ph/2})">'
        f'{html.escape(y_label)} ({unit_label}, log10)</text>'
    )

    parts.append('</svg>')
    return "".join(parts)


def fmt(v, spec):
    if v is None:
        return "—"
    try:
        if spec.endswith("d}") or spec == "{:d}":
            return spec.format(int(v))
        return spec.format(v)
    except Exception:
        return "—"


def render_task_table(task: str, model_aggs: dict,
                      outcome_counts: dict | None = None,
                      model_groups: dict | None = None,
                      model_safe_names: dict | None = None) -> str:
    headline_key, arrow, _ = HEADLINE[task]
    cols = EXTRA_COLS[task]
    higher_better = arrow == "↑"
    show_group_col = model_groups is not None and len(set(model_groups.values())) > 1

    rows = []
    for safe_name, agg in model_aggs.items():
        info = (agg or {}).get(task) or {}
        if not info:
            continue
        rows.append((safe_name, info.get(headline_key), info))

    sentinel = float("inf") if not higher_better else float("-inf")
    rows.sort(key=lambda r: (r[1] if r[1] is not None else sentinel),
              reverse=higher_better)

    best_val = None
    for r in rows:
        if r[1] is not None:
            best_val = r[1]
            break

    th_cells = ["<th>Model</th>"]
    if show_group_col:
        th_cells.append("<th>prompts</th>")
    th_cells.append('<th class="num">n</th>')
    for _, label, _ in cols:
        th_cells.append(f'<th class="num">{html.escape(label)}</th>')
    show_pred_counts = task == "outcome" and outcome_counts is not None
    if show_pred_counts:
        th_cells.extend([
            '<th class="num">pred_passed</th>',
            '<th class="num">pred_failed</th>',
            '<th class="num">pred_error</th>',
        ])

    body = []
    for key_name, headline_val, info in rows:
        model_dir = (model_safe_names or {}).get(key_name, key_name)
        display = MODEL_DISPLAY.get(model_dir, model_dir)
        cells = [f"<td>{html.escape(display)}</td>"]
        if show_group_col:
            grp = (model_groups or {}).get(key_name, "")
            cells.append(f'<td>{html.escape(grp)}</td>')
        cells.append(f'<td class="num">{info.get("n", 0)}</td>')
        safe_name = key_name
        for key, _, spec in cols:
            v = info.get(key)
            cls = ' class="num"'
            if (key == headline_key and best_val is not None and v is not None
                    and abs(v - best_val) < 1e-9):
                cls = ' class="num best"'
            cells.append(f"<td{cls}>{fmt(v, spec)}</td>")
        if show_pred_counts:
            oc = (outcome_counts or {}).get(safe_name) or {}
            cells.extend([
                f'<td class="num">{oc.get("passed", 0)}</td>',
                f'<td class="num">{oc.get("failed", 0)}</td>',
                f'<td class="num">{oc.get("error", 0)}</td>',
            ])
        body.append(f"<tr>{''.join(cells)}</tr>")

    return (
        f"<table>\n<tr>{''.join(th_cells)}</tr>\n"
        + "\n".join(body)
        + "\n</table>"
    )


def render_summary_table(model_aggs: dict,
                         sample_counts: dict | None = None,
                         model_groups: dict | None = None,
                         model_safe_names: dict | None = None) -> str:
    show_group_col = model_groups is not None and len(set(model_groups.values())) > 1

    th = ["<th>Model</th>"]
    if show_group_col:
        th.append("<th>prompts</th>")
    th.append('<th class="num">n_samples</th>')
    for task in TASK_ORDER:
        _, arrow, label = HEADLINE[task]
        th.append(
            f'<th class="num">{html.escape(task)}<br>'
            f'<span style="font-weight:400;color:var(--fg2);font-size:0.85em">'
            f'{html.escape(label)} {arrow}</span></th>'
        )

    rows = []
    for key_name, agg in model_aggs.items():
        if sample_counts is not None and key_name in sample_counts:
            n_unique = sample_counts[key_name]
        else:
            n_unique = next(
                ((agg or {}).get(t, {}).get("n", 0) for t in TASK_ORDER
                 if (agg or {}).get(t, {}).get("n")),
                0,
            )
        model_dir = (model_safe_names or {}).get(key_name, key_name)
        display = MODEL_DISPLAY.get(model_dir, model_dir)
        cells = [f"<td>{html.escape(display)}</td>"]
        if show_group_col:
            grp = (model_groups or {}).get(key_name, "")
            cells.append(f'<td>{html.escape(grp)}</td>')
        cells.append(f'<td class="num">{n_unique}</td>')
        for task in TASK_ORDER:
            info = (agg or {}).get(task) or {}
            key, _, _ = HEADLINE[task]
            v = info.get(key)
            cells.append(f'<td class="num">{fmt(v, "{:.3f}")}</td>')
        rows.append((key_name, n_unique, "".join(cells)))

    rows.sort(key=lambda r: -r[1])
    body = "\n".join(f"<tr>{cs}</tr>" for _, _, cs in rows)
    return f"<table>\n<tr>{''.join(th)}</tr>\n{body}\n</table>"


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Execution Reasoning Benchmark — Results by Task</title>
<style>
  :root {{
    --bg: #1a1b26; --bg2: #24283b; --bg3: #292e42;
    --fg: #c0caf5; --fg2: #a9b1d6; --accent: #7aa2f7;
    --green: #9ece6a; --orange: #e0af68; --red: #f7768e;
    --border: #3b4261; --th-bg: #1f2335;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--fg);
         font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         line-height: 1.6; padding: 2rem;
         max-width: 1200px; margin: 0 auto; }}
  h1 {{ color: var(--accent); font-size: 2rem; margin-bottom: 0.4rem;
       border-bottom: 2px solid var(--border); padding-bottom: 0.4rem; }}
  h2 {{ color: var(--accent); font-size: 1.4rem; margin-top: 2.5rem;
       margin-bottom: 0.7rem; border-bottom: 1px solid var(--border);
       padding-bottom: 0.25rem; }}
  h3 {{ color: var(--orange); font-size: 1.05rem; margin-top: 1.4rem;
       margin-bottom: 0.4rem; }}
  p {{ margin-bottom: 0.7rem; color: var(--fg2); }}
  strong {{ color: var(--fg); }}
  code {{ background: var(--bg3); padding: 0.1em 0.4em; border-radius: 3px;
         font-family: 'SF Mono', Menlo, Consolas, monospace;
         font-size: 0.86em; color: var(--green); }}
  hr {{ border: none; border-top: 1px solid var(--border); margin: 2rem 0; }}
  .subtitle {{ color: var(--fg2); font-size: 0.95rem; margin-bottom: 1.5rem; font-style: italic; }}
  table {{ width: 100%; border-collapse: collapse; margin: 0.7rem 0 1.2rem;
          font-size: 0.84rem; }}
  th {{ background: var(--th-bg); color: var(--accent); text-align: left;
       padding: 0.45rem 0.7rem; border: 1px solid var(--border); font-weight: 600;
       white-space: nowrap; }}
  th.num {{ text-align: right; }}
  td {{ padding: 0.4rem 0.7rem; border: 1px solid var(--border); color: var(--fg2);
       vertical-align: top; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr:nth-child(even) td {{ background: var(--bg2); }}
  tr:hover td {{ background: var(--bg3); }}
  .best {{ color: var(--green); font-weight: 700; }}
  .section {{ background: var(--bg2); border-radius: 8px; padding: 1rem 1.4rem;
             margin: 1rem 0; border: 1px solid var(--border); }}
  .note {{ color: var(--fg2); font-size: 0.85rem; font-style: italic;
          margin-top: -0.3rem; }}
  th.sortable {{ cursor: pointer; user-select: none; }}
  th.sortable:hover {{ background: var(--bg3); }}
  th.sortable::after {{ content: " ⇅"; color: var(--fg2); font-size: 0.8em;
                       opacity: 0.5; }}
  th.sortable.asc::after  {{ content: " ▲"; color: var(--orange); opacity: 1; }}
  th.sortable.desc::after {{ content: " ▼"; color: var(--orange); opacity: 1; }}
  .plot-row {{ display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-start;
              margin: 0.4rem 0 1.4rem; }}
  .plot-card {{ background: var(--bg2); border: 1px solid var(--border);
               border-radius: 6px; padding: 0.4rem; }}
  .plot-card svg {{ display: block; }}
  .plot-model-label {{ color: var(--accent); font-size: 0.9rem;
                      margin-top: 1rem; margin-bottom: 0.3rem;
                      font-weight: 600; }}
  .plot-empty {{ color: var(--fg2); font-size: 0.85rem; padding: 0.3rem; }}
  .outcome-footer {{ font-size: 0.85rem; color: var(--fg2); margin-top: 0.4rem;
                    margin-bottom: 1.2rem; }}
  .outcome-footer strong {{ color: var(--fg); }}
  .top-preds {{ font-size: 0.78rem; color: var(--fg2); margin-top: 0.35rem;
               padding: 0.15rem 0.3rem; }}
  .top-pred-item {{ color: var(--green); font-variant-numeric: tabular-nums; }}
  .top-pred-count {{ color: var(--fg2); }}
</style>
</head>
<body>

<h1>Execution Reasoning Benchmark</h1>
<p class="subtitle">Predict runtime properties of a SWE-bench test from source code alone. Each per-task table shows the metric set described in its caption — see the headlines table for the at-a-glance numbers.</p>

<h2>Headline metrics</h2>
<p class="note">n_samples = number of combined-task prompts evaluated per model. ↑ = higher is better, ↓ = lower is better.</p>
{summary_table}

<h2>Per-task breakdown</h2>

{per_task_sections}

<h2>Predicted vs ground truth — wall_time &amp; peak_rss (log–log)</h2>
<p class="note">Each plot shows one dot per evaluated sample. <span style="color:#9ece6a">●</span> post-patch, <span style="color:#f7768e">●</span> pre-patch. Orange dashed = identity (perfect prediction); cyan solid = fitted log-log regression for that model.</p>
{scatter_section}

<h2>About these metrics</h2>
<div class="section">
  <p><strong>Outcome (P/R/F1):</strong> binary classification with non-pass outcomes (<code>failed</code> or <code>error</code>) treated as the positive class. The model is rewarded for catching failures; passing tests that it incorrectly flags as failures hurt precision.</p>
  <p><strong>exception_match:</strong> only scored on samples whose GT outcome is <code>failed</code> or <code>error</code>. Exact match on the predicted exception class string. If the model didn't predict a class, the sample contributes 0.</p>
  <p><strong>peak_rss / wall_time:</strong> log-log fit log10(max(pred, F)) = a · log10(max(gt, F)) + b, with a per-field floor F: <em>F = 10,000 bytes</em> for peak_rss, <em>F = 0.01 ms</em> for wall_time. The floor only kicks in when a value would otherwise undefine the log (zero or sub-floor); values above the floor are untouched. We report slope <em>a</em>, bias <em>b</em>, residual &sigma; (RMSE of log10 residuals after the linear fit — measures unexplained spread), and log10 MAE. A perfect oracle has <em>a = 1, b = 0, &sigma; = 0, MAE = 0</em>.</p>
  <p><strong>hot_methods / hot_lines:</strong> <em>exec_ratio</em> is the fraction of predicted names that were actually executed (i.e. appear in the trace value table). <em>NDCG@5</em> grades the ordering of the top-5 predictions using the entity's recorded self-time (or alloc bytes) as relevance; predictions for unexecuted names contribute 0. <em>recall@5</em> is the rate at which the single hottest ground-truth entity (highest self-time or highest alloc bytes) appears anywhere in the predicted top-5.</p>
</div>

<script>
(function () {{
  const sortTable = (table, idx, asc) => {{
    const tbody = table.tBodies[0] || table;
    const rows = Array.from(table.rows).slice(1);
    const parse = (s) => {{
      if (s === "—" || s === "" || s == null) return null;
      const n = Number(String(s).replace(/[,%+]/g, ""));
      return Number.isFinite(n) ? n : String(s);
    }};
    rows.sort((a, b) => {{
      const va = parse(a.cells[idx].textContent.trim());
      const vb = parse(b.cells[idx].textContent.trim());
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      const cmp = (typeof va === "number" && typeof vb === "number")
        ? va - vb
        : String(va).localeCompare(String(vb));
      return asc ? cmp : -cmp;
    }});
    rows.forEach(r => tbody.appendChild(r));
  }};

  document.querySelectorAll("table").forEach(table => {{
    const ths = table.tHead ? table.tHead.rows[0].cells : table.rows[0].cells;
    Array.from(ths).forEach((th, idx) => {{
      th.classList.add("sortable");
      th.addEventListener("click", () => {{
        const isAsc = th.classList.contains("asc");
        Array.from(ths).forEach(s => s.classList.remove("asc", "desc"));
        th.classList.add(isAsc ? "desc" : "asc");
        sortTable(table, idx, !isAsc);
      }});
    }});
  }});
}})();
</script>

</body>
</html>
"""


def _default_group_label(path: Path) -> str:
    """Derive a short group label from an eval_results root path.

    "eval_results"        → "oracle"
    "eval_results_smart"  → "smart"
    "eval_results_foo"                  → "foo"
    Anything else falls back to the directory name verbatim.
    """
    name = path.name
    if name == "eval_results":
        return "oracle"
    for prefix in ("eval_results_", "eval_results_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", nargs="+", default=["eval_results"],
        help="One or more directories of per-model eval results. When "
             "multiple roots are given each model gets tagged with a group "
             "label derived from its root (or via --labels), so smart-prompt "
             "results can sit alongside the oracle baseline in one report.",
    )
    parser.add_argument(
        "--labels", nargs="*",
        help="Group label for each --root, in matching order. Defaults to a "
             "label derived from the root directory name.",
    )
    parser.add_argument("--out", default="docs/experiment_report.html")
    parser.add_argument("--include", nargs="*",
                        help="If given, only include model dirs whose name "
                             "matches any of these (exact or substring).")
    parser.add_argument("--exclude", nargs="*",
                        help="Skip model dirs whose name matches any of these "
                             "(exact or substring).")
    args = parser.parse_args()

    roots = [Path(p) for p in args.root]
    for r in roots:
        if not r.exists():
            raise SystemExit(f"{r} does not exist")
    if args.labels:
        if len(args.labels) != len(roots):
            raise SystemExit(
                f"--labels has {len(args.labels)} values but --root has "
                f"{len(roots)}; counts must match"
            )
        labels = list(args.labels)
    else:
        labels = [_default_group_label(r) for r in roots]

    def _keep(name: str) -> bool:
        if args.exclude and any(p in name for p in args.exclude):
            return False
        if args.include:
            return any(p in name for p in args.include)
        return True

    # Composite key = f"{group}::{model_dir_name}" so two groups can carry
    # the same model name without collision.
    model_aggs: dict[str, dict] = {}
    model_records: dict[str, list] = {}
    sample_counts: dict[str, int] = {}
    outcome_pred_counts: dict[str, dict] = {}
    model_groups: dict[str, str] = {}
    model_safe_names: dict[str, str] = {}

    for root, group in zip(roots, labels):
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name == "logs":
                continue
            if not _keep(d.name):
                continue
            recs = load_model_records(d)
            if not recs:
                continue
            key = f"{group}::{d.name}"
            model_records[key] = recs
            model_aggs[key] = aggregate_combined(recs)
            sample_counts[key] = len(recs)
            outcome_pred_counts[key] = _outcome_counts_from_records(recs)
            model_groups[key] = group
            model_safe_names[key] = d.name

    if not model_aggs:
        raise SystemExit(f"no per-model aggregates found in {roots}")

    any_recs = next(iter(model_records.values()), [])
    gt_dist = _gt_outcome_distribution(any_recs)
    gt_total = sum(gt_dist.values())
    gt_footer = (
        f'<p class="outcome-footer">Ground-truth outcome distribution '
        f'across the {gt_total} samples evaluated by every model: '
        f'<strong>passed</strong> = {gt_dist.get("passed", 0)}, '
        f'<strong>failed</strong> = {gt_dist.get("failed", 0)}, '
        f'<strong>error</strong> = {gt_dist.get("error", 0)}.</p>'
    )

    summary = render_summary_table(
        model_aggs, sample_counts=sample_counts,
        model_groups=model_groups, model_safe_names=model_safe_names,
    )
    per_task_sections = []
    for task in TASK_ORDER:
        desc = TASK_DESC.get(task, "")
        table = render_task_table(
            task, model_aggs,
            outcome_counts=outcome_pred_counts,
            model_groups=model_groups,
            model_safe_names=model_safe_names,
        )
        section = (f'<h3>{html.escape(task)}</h3>\n'
                   f'<p class="note">{html.escape(desc)}</p>\n{table}')
        if task == "outcome":
            section += "\n" + gt_footer
        per_task_sections.append(section)

    scatter_blocks = []
    for key_name, recs in model_records.items():
        wall_pairs = _scatter_pairs(recs, "wall_ms")
        peak_pairs = _scatter_pairs(recs, "peak_bytes")
        wall_agg = (model_aggs.get(key_name) or {}).get("wall_time") or {}
        peak_agg = (model_aggs.get(key_name) or {}).get("peak_rss") or {}
        model_dir = model_safe_names.get(key_name, key_name)
        group = model_groups.get(key_name, "")
        base_display = MODEL_DISPLAY.get(model_dir, model_dir)
        display = f"{base_display}  [{group}]" if group else base_display

        wall_slope, wall_bias = wall_agg.get("slope"), wall_agg.get("bias")
        peak_slope, peak_bias = peak_agg.get("slope"), peak_agg.get("bias")
        wall_title = (
            f"wall_time   slope={wall_slope:+.2f}  bias={wall_bias:+.2f}"
            if wall_slope is not None and wall_bias is not None
            else "wall_time"
        )
        peak_title = (
            f"peak_rss   slope={peak_slope:+.2f}  bias={peak_bias:+.2f}"
            if peak_slope is not None and peak_bias is not None
            else "peak_rss"
        )

        wall_top = _top_predictions(recs, "wall_ms")
        peak_top = _top_predictions(recs, "peak_bytes")
        scatter_blocks.append(
            f'<div class="plot-model-label">{html.escape(display)}</div>'
            f'<div class="plot-row">'
            f'<div class="plot-card">'
            + _render_scatter_svg(wall_pairs,
                                  x_label="ground truth wall_ms",
                                  y_label="predicted wall_ms",
                                  title=wall_title,
                                  unit_label="ms",
                                  slope=wall_slope, bias=wall_bias,
                                  eps=EPS_BY_FIELD["wall_ms"])
            + _render_top_preds(wall_top, "wall_ms")
            + '</div><div class="plot-card">'
            + _render_scatter_svg(peak_pairs,
                                  x_label="ground truth peak_bytes",
                                  y_label="predicted peak_bytes",
                                  title=peak_title,
                                  unit_label="bytes",
                                  slope=peak_slope, bias=peak_bias,
                                  eps=EPS_BY_FIELD["peak_bytes"])
            + _render_top_preds(peak_top, "peak_bytes")
            + '</div></div>'
        )
    scatter_section = "\n".join(scatter_blocks)

    html_out = HTML_TEMPLATE.format(
        summary_table=summary,
        per_task_sections="\n".join(per_task_sections),
        scatter_section=scatter_section,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_out, encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"  models: {len(model_aggs)}")
    for key in model_aggs:
        n = sample_counts.get(key, 0)
        grp = model_groups.get(key, "")
        print(f"    [{grp:>8}] {model_safe_names[key]:45s} n_samples={n}")


if __name__ == "__main__":
    main()

"""Scoring metrics for the runtime-behavior benchmark.

Per-sample `score_combined_task` emits raw signals (TP/FP/FN counts, paired
log10 values, executed-ratio, NDCG@5, recall@5-top1). `aggregate_combined`
turns a list of those records into per-subtask aggregates (P/R/F1, log-log
linear fit + residual std + log10 MAE, mean executed-ratio, mean NDCG, mean recall@5-top1).
"""

from __future__ import annotations

import math
import random
from typing import Any

NDCG_K = 5
RECALL_K = 5
N_SHUFFLE_TRIALS = 100

EPS_BY_FIELD = {
    "peak_bytes": 10_000.0,
    "wall_ms": 0.01,
}


def _safe_log10(v, eps: float) -> float | None:
    try:
        return math.log10(max(float(v), eps))
    except (TypeError, ValueError):
        return None


def _dcg(ordered_names: list[str], lookup: dict, k: int) -> float:
    return sum(
        float(lookup.get(name, 0) or 0) / math.log2(i + 2)
        for i, name in enumerate(ordered_names[:k])
    )


def _shuffle_ndcg_mean(items: list[str], lookup: dict, k: int,
                       idcg: float, rng: random.Random) -> float:
    if not items or idcg <= 0:
        return 0.0
    buf = list(items)
    total = 0.0
    for _ in range(N_SHUFFLE_TRIALS):
        rng.shuffle(buf)
        total += _dcg(buf, lookup, k) / idcg
    return total / N_SHUFFLE_TRIALS


def score_combined_task(pred, gt, k_list=None, values_by_subtask=None) -> dict:
    """Score a single combined-task sample. Emits flat per-sample signals.

    Aggregation (P/R/F1, regression fit) happens in `aggregate_combined`; this
    function only emits numbers we can average or sum sample-by-sample.
    """
    pred = pred or {}
    gt = gt or {}
    out: dict[str, Any] = {}

    # === outcome ===
    o_pred = pred.get("outcome") or {}
    if isinstance(o_pred, str):
        o_pred = {"outcome": o_pred}
    o_gt = gt.get("outcome") or {}
    gt_outcome = (o_gt.get("outcome") or "").strip().lower()
    pred_outcome = (o_pred.get("outcome") or "").strip().lower()
    gt_pos = gt_outcome in ("failed", "error")
    pred_pos = pred_outcome in ("failed", "error")
    out["outcome.tp"] = 1 if (gt_pos and pred_pos) else 0
    out["outcome.fp"] = 1 if ((not gt_pos) and pred_pos) else 0
    out["outcome.fn"] = 1 if (gt_pos and (not pred_pos)) else 0
    out["outcome.tn"] = 1 if ((not gt_pos) and (not pred_pos)) else 0
    out["outcome.gt_positive"] = 1 if gt_pos else 0

    if gt_pos:
        gt_exc = (o_gt.get("exception_type") or "").strip()
        pred_exc = (o_pred.get("exception_type") or "").strip()
        out["outcome.exception_match"] = (
            1.0 if (gt_exc and pred_exc and gt_exc == pred_exc) else 0.0
        )

    # === peak_rss / wall_time ===
    for sub, key in (("peak_rss", "peak_bytes"), ("wall_time", "wall_ms")):
        eps = EPS_BY_FIELD[key]
        g = gt.get(key)
        p = pred.get(key)
        lg = _safe_log10(g, eps) if g is not None else None
        lp = _safe_log10(p if p is not None else 0, eps)
        if lg is not None:
            out[f"{sub}.gt_log10"] = lg
            out[f"{sub}.pred_log10"] = lp
            out[f"{sub}.log10_mae"] = abs(lp - lg)

    # === hot methods / lines ===
    values_by_subtask = values_by_subtask or {}
    rng = random.Random(42)
    for sub in ("hot_methods_time", "hot_methods_alloc",
                "hot_lines_time", "hot_lines_alloc"):
        sub_pred = list(pred.get(sub) or [])
        sub_gt = list(gt.get(sub) or [])
        lookup = values_by_subtask.get(sub) or {}

        if sub_pred:
            n_in = sum(1 for n in sub_pred if n in lookup)
            out[f"{sub}.executed_ratio"] = n_in / len(sub_pred)
        else:
            out[f"{sub}.executed_ratio"] = 0.0

        ideal_vals = sorted(
            (float(lookup.get(n, 0) or 0) for n in sub_gt),
            reverse=True,
        )[:NDCG_K]
        idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal_vals))
        dcg = _dcg(sub_pred, lookup, NDCG_K)
        out[f"{sub}.ndcg_at_5"] = (dcg / idcg) if idcg > 0 else 0.0

        # --- NDCG baselines ---
        if idcg > 0:
            out[f"{sub}.ndcg_gt5_shuffled"] = _shuffle_ndcg_mean(
                sub_gt[:NDCG_K], lookup, NDCG_K, idcg, rng)

            gt_top20 = sorted(
                lookup, key=lambda n: float(lookup.get(n, 0) or 0),
                reverse=True,
            )[:20]
            out[f"{sub}.ndcg_gt20_shuffled"] = _shuffle_ndcg_mean(
                gt_top20, lookup, NDCG_K, idcg, rng)

            if sub_pred:
                out[f"{sub}.ndcg_pred_shuffled"] = _shuffle_ndcg_mean(
                    sub_pred, lookup, NDCG_K, idcg, rng)

                oracle_ordered = sorted(
                    sub_pred,
                    key=lambda n: float(lookup.get(n, 0) or 0),
                    reverse=True,
                )
                out[f"{sub}.ndcg_pred_oracle"] = (
                    _dcg(oracle_ordered, lookup, NDCG_K) / idcg)

        gt_ranked = sorted(
            sub_gt, key=lambda n: float(lookup.get(n, 0) or 0), reverse=True,
        )
        hottest = next(
            (n for n in gt_ranked if float(lookup.get(n, 0) or 0) > 0), None,
        )
        if hottest is not None:
            out[f"{sub}.recall_at_5_top1"] = (
                1.0 if hottest in sub_pred[:RECALL_K] else 0.0
            )

    return out


def _linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """OLS slope+bias for y = a*x + b. None if degenerate."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None
    a = num / den
    b = my - a * mx
    return a, b


def _aggregate_outcome(records: list[dict]) -> dict:
    tp = sum((r["metrics"].get("outcome.tp") or 0) for r in records)
    fp = sum((r["metrics"].get("outcome.fp") or 0) for r in records)
    fn = sum((r["metrics"].get("outcome.fn") or 0) for r in records)
    tn = sum((r["metrics"].get("outcome.tn") or 0) for r in records)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) > 0 else 0.0)
    exc_vals = [r["metrics"]["outcome.exception_match"]
                for r in records
                if "outcome.exception_match" in (r.get("metrics") or {})]
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "exception_match": (sum(exc_vals) / len(exc_vals)
                            if exc_vals else None),
        "n_failure_gt": tp + fn,
    }


def _aggregate_numeric(records: list[dict], sub: str) -> dict:
    xs, ys, maes = [], [], []
    for r in records:
        m = r.get("metrics") or {}
        lg = m.get(f"{sub}.gt_log10")
        lp = m.get(f"{sub}.pred_log10")
        mae = m.get(f"{sub}.log10_mae")
        if lg is not None and lp is not None:
            xs.append(lg)
            ys.append(lp)
        if mae is not None and mae != float("inf"):
            maes.append(mae)
    fit = _linear_fit(xs, ys)
    residual_std = None
    if fit is not None and len(xs) >= 3:
        a, b = fit
        residuals = [y - (a * x + b) for x, y in zip(xs, ys)]
        residual_std = (sum(r ** 2 for r in residuals) / len(residuals)) ** 0.5
    return {
        "n_fitted": len(xs),
        "slope": fit[0] if fit else None,
        "bias": fit[1] if fit else None,
        "residual_std": residual_std,
        "log10_mae": (sum(maes) / len(maes)) if maes else None,
    }


def _mean_or_none(vals: list[float]) -> float | None:
    return (sum(vals) / len(vals)) if vals else None


def _aggregate_hot(records: list[dict], sub: str) -> dict:
    ratios, ndcgs, recalls = [], [], []
    gt5_shuf, gt20_shuf, pred_shuf, pred_oracle = [], [], [], []
    for r in records:
        m = r.get("metrics") or {}
        er = m.get(f"{sub}.executed_ratio")
        nd = m.get(f"{sub}.ndcg_at_5")
        rc = m.get(f"{sub}.recall_at_5_top1")
        if er is not None:
            ratios.append(er)
        if nd is not None:
            ndcgs.append(nd)
        if rc is not None:
            recalls.append(rc)
        v = m.get(f"{sub}.ndcg_gt5_shuffled")
        if v is not None:
            gt5_shuf.append(v)
        v = m.get(f"{sub}.ndcg_gt20_shuffled")
        if v is not None:
            gt20_shuf.append(v)
        v = m.get(f"{sub}.ndcg_pred_shuffled")
        if v is not None:
            pred_shuf.append(v)
        v = m.get(f"{sub}.ndcg_pred_oracle")
        if v is not None:
            pred_oracle.append(v)
    return {
        "executed_ratio": _mean_or_none(ratios),
        "ndcg_at_5": _mean_or_none(ndcgs),
        "ndcg_gt5_shuffled": _mean_or_none(gt5_shuf),
        "ndcg_gt20_shuffled": _mean_or_none(gt20_shuf),
        "ndcg_pred_shuffled": _mean_or_none(pred_shuf),
        "ndcg_pred_oracle": _mean_or_none(pred_oracle),
        "recall_at_5_top1": _mean_or_none(recalls),
        "recall_at_5_top1_n": len(recalls),
    }


def aggregate_combined(records: list[dict]) -> dict[str, dict]:
    """Per-subtask aggregates from a list of combined-task response records.

    Each record must have `metrics` (from `score_combined_task`); may have
    `error` (API error string) and `side` ("pre"/"post").
    """
    n_total = len(records)
    n_valid = sum(1 for r in records
                  if (r.get("metrics") or {}).get("valid_pred"))
    n_api_err = sum(1 for r in records if r.get("error"))
    n_parse_fail = max(0, n_total - n_valid - n_api_err)
    common = {
        "n": n_total,
        "n_valid": n_valid,
        "n_parse_failure": n_parse_fail,
        "n_api_error": n_api_err,
        "valid_pred_rate": (n_valid / n_total) if n_total else 0.0,
        "parse_failure_rate": (n_parse_fail / n_total) if n_total else 0.0,
        "api_error_rate": (n_api_err / n_total) if n_total else 0.0,
    }

    out: dict[str, dict] = {
        "outcome": {**common, **_aggregate_outcome(records)},
        "peak_rss": {**common, **_aggregate_numeric(records, "peak_rss")},
        "wall_time": {**common, **_aggregate_numeric(records, "wall_time")},
    }
    for sub in ("hot_methods_time", "hot_methods_alloc",
                "hot_lines_time", "hot_lines_alloc"):
        out[sub] = {**common, **_aggregate_hot(records, sub)}
    return out


METRIC_FUNCS = {
    "combined_task": score_combined_task,
}


def score(metric: str, pred: Any, gt: Any, metric_args: dict | None = None) -> dict:
    """Dispatch to the right scoring function. Only `combined_task` is
    supported in the new metric set."""
    fn = METRIC_FUNCS.get(metric)
    if fn is None:
        return {}
    args = metric_args or {}
    if metric == "combined_task":
        return fn(pred, gt,
                  k_list=args.get("k_list"),
                  values_by_subtask=args.get("values_by_subtask"))
    return fn(pred, gt)

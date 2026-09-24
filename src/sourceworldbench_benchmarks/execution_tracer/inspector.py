"""Build a self-contained HTML report for inspecting execution traces.

Reads a traces directory laid out as::

    <traces_dir>/<instance>/trace_output.json

(e.g. ``traces/astropy__astropy-12907/trace_output.json``) and renders a
single HTML file that embeds every trace and lets you browse each instance
and individual test interactively in the browser.

Exposed via the ``sourceworldbench-benchmarks execution-tracer inspect`` CLI command.
"""

from __future__ import annotations

import json
from pathlib import Path

TRACE_FILENAME = "trace_output.json"


def collect_traces(traces_dir: Path) -> dict:
    """Walk ``traces_dir`` and build ``{instance: trace_data}``.

    An instance is any subdirectory of ``traces_dir`` that contains a
    ``trace_output.json`` file. Unreadable files are skipped with a warning
    recorded under a ``_warnings`` key on the returned dict.
    """
    report: dict[str, dict] = {}
    warnings: list[str] = []
    for instance_dir in sorted(p for p in traces_dir.iterdir() if p.is_dir()):
        trace_file = instance_dir / TRACE_FILENAME
        if not trace_file.is_file():
            continue
        try:
            report[instance_dir.name] = json.loads(trace_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"skipping {trace_file}: {exc}")
    if warnings:
        report["_warnings"] = warnings  # type: ignore[assignment]
    return report


def build_html(report: dict) -> str:
    """Embed ``report`` into the HTML template as a JSON blob.

    The ``_warnings`` key, if present, is dropped before embedding.
    """
    data = {k: v for k, v in report.items() if k != "_warnings"}
    # Guard against `</script>` injection from any embedded string values.
    data_json = json.dumps(data).replace("</", "<\\/")
    return HTML_TEMPLATE.replace("/*__DATA__*/", data_json)


def write_report(traces_dir: Path, output: Path) -> tuple[int, int, list[str]]:
    """Collect traces under ``traces_dir`` and write an HTML report to ``output``.

    Returns ``(n_instances, n_tests, warnings)``.
    """
    report = collect_traces(traces_dir)
    warnings = report.pop("_warnings", [])  # type: ignore[arg-type]
    output.write_text(build_html(report))
    n_inst = len(report)
    n_tests = sum(len(t.get("tests", {})) for t in report.values())
    return n_inst, n_tests, warnings


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trace Inspector</title>
<style>
  :root {
    --bg: #1e1e2e; --panel: #181825; --panel2: #11111b; --fg: #cdd6f4;
    --muted: #9399b2; --border: #313244; --accent: #89b4fa; --accent2: #cba6f7;
    --pass: #a6e3a1; --fail: #f38ba8; --skip: #f9e2af; --bar: #585b70;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 13px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--fg); height: 100vh; display: flex;
  }
  code, .mono { font-family: "SF Mono", Menlo, monospace; }
  #sidebar {
    width: 380px; min-width: 240px; max-width: 60vw; background: var(--panel);
    border-right: 1px solid var(--border); overflow-y: auto; flex-shrink: 0;
    resize: horizontal;
  }
  #sidebar header {
    padding: 12px 14px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; background: var(--panel); z-index: 2;
  }
  #sidebar header h1 { font-size: 15px; margin: 0 0 8px; }
  #filter {
    width: 100%; padding: 6px 8px; background: var(--panel2); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px; font-size: 12px;
  }
  .instance > .row, .test {
    padding: 5px 10px; cursor: pointer; display: flex; gap: 6px;
    align-items: center; user-select: none; white-space: nowrap;
  }
  .instance > .row { font-weight: 600; }
  .instance > .row:hover, .test:hover { background: var(--panel2); }
  .test { padding-left: 22px; font-size: 12px; }
  .test.active { background: #2a2a40; border-left: 2px solid var(--accent); }
  .test .name { overflow: hidden; text-overflow: ellipsis; }
  .caret { width: 10px; display: inline-block; color: var(--muted); transition: transform .1s; }
  .collapsed .caret { transform: rotate(-90deg); }
  .collapsed > .children { display: none; }
  .badge {
    display: inline-block; padding: 0 6px; border-radius: 10px; font-size: 10px;
    font-weight: 600; text-transform: uppercase; flex-shrink: 0;
  }
  .b-passed { background: #2d4a2b; color: var(--pass); }
  .b-failed, .b-error { background: #4a2b32; color: var(--fail); }
  .b-skipped { background: #4a432b; color: var(--skip); }
  .count { color: var(--muted); font-size: 11px; margin-left: auto; }
  #main { flex: 1; overflow-y: auto; padding: 20px 28px; }
  #main h2 { font-size: 16px; margin: 0 0 4px; word-break: break-all; }
  #breadcrumb { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
  .empty { color: var(--muted); margin-top: 40px; text-align: center; }
  .cards { display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0 22px; }
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 12px; min-width: 110px;
  }
  .card .k { color: var(--muted); font-size: 11px; }
  .card .v { font-size: 15px; font-weight: 600; margin-top: 2px; }
  .section-title {
    font-size: 13px; font-weight: 600; margin: 22px 0 8px; color: var(--accent);
    display: flex; align-items: center; gap: 8px;
  }
  table { border-collapse: collapse; width: 100%; font-size: 12px; }
  th, td { text-align: right; padding: 5px 9px; border-bottom: 1px solid var(--border); }
  th:first-child, td:first-child, th.l, td.l { text-align: left; }
  th { cursor: pointer; color: var(--muted); user-select: none; position: sticky; top: 0; background: var(--bg); }
  th:hover { color: var(--fg); }
  th.sorted::after { content: " \25BE"; color: var(--accent); }
  th.sorted.asc::after { content: " \25B4"; }
  tbody tr:hover { background: var(--panel); }
  .fn { color: var(--accent); }
  .file { color: var(--muted); font-size: 11px; }
  .seq-row { display: flex; align-items: center; gap: 8px; padding: 1px 0; font-size: 12px; }
  .seq-bar-wrap {
    flex: 1; min-width: 60px; background: var(--panel2); border-radius: 3px;
    height: 14px; position: relative;
  }
  .seq-bar { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2)); border-radius: 3px; }
  .seq-time { color: var(--muted); width: 90px; text-align: right; flex-shrink: 0; }
  .seq-depth { color: var(--bar); width: 26px; text-align: right; flex-shrink: 0; }
  .trunc-note { color: var(--skip); font-size: 12px; margin: 8px 0; }
  .meta-line { color: var(--muted); font-size: 12px; margin-bottom: 6px; }
  .meta-line b { color: var(--fg); font-weight: 600; }
  .toggle { font-size: 11px; color: var(--accent); cursor: pointer; }
</style>
</head>
<body>
<aside id="sidebar">
  <header>
    <h1>🔍 Trace Inspector</h1>
    <input id="filter" placeholder="filter tests…" autocomplete="off">
  </header>
  <div id="tree"></div>
</aside>
<main id="main"><div class="empty">Select a test from the sidebar to inspect its trace.</div></main>

<script>
const DATA = /*__DATA__*/;

// ---- formatting helpers ----
function fmtTime(s) {
  if (s == null) return "–";
  if (s >= 1) return s.toFixed(3) + " s";
  if (s >= 1e-3) return (s * 1e3).toFixed(2) + " ms";
  return (s * 1e6).toFixed(1) + " µs";
}
function fmtNs(ns) {
  if (ns == null) return "–";
  return fmtTime(ns / 1e9);
}
function fmtBytes(b) {
  if (b == null) return "–";
  const u = ["B", "KB", "MB", "GB"]; let i = 0; let v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(i === 0 ? 0 : 1) + " " + u[i];
}
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const k in (attrs || {})) {
    if (k === "class") e.className = attrs[k];
    else if (k === "text") e.textContent = attrs[k];
    else if (k.startsWith("on")) e[k] = attrs[k];
    else e.setAttribute(k, attrs[k]);
  }
  for (const kid of kids) if (kid != null) e.append(kid);
  return e;
}

// ---- sidebar tree ----
const tree = document.getElementById("tree");
let activeEl = null;

function badge(outcome) {
  const o = (outcome || "").toLowerCase();
  return el("span", { class: "badge b-" + o, text: o || "?" });
}

function buildTree() {
  for (const [inst, data] of Object.entries(DATA)) {
    const instBox = el("div", { class: "instance" });
    const tests = data.tests || {};
    const instRow = el("div", { class: "row" },
      el("span", { class: "caret", text: "▾" }),
      el("span", { text: inst }),
      el("span", { class: "count", text: Object.keys(tests).length + " tests" }));
    instRow.onclick = () => instBox.classList.toggle("collapsed");
    instBox.append(instRow);

    const instKids = el("div", { class: "children" });
    for (const [nodeid, test] of Object.entries(tests)) {
      const shortName = nodeid.split("::").slice(1).join("::") || nodeid;
      const testEl = el("div", { class: "test" },
        badge(test.outcome),
        el("span", { class: "name", text: shortName, title: nodeid }));
      testEl.dataset.search = (inst + " " + nodeid).toLowerCase();
      testEl.onclick = () => selectTest(testEl, inst, data, nodeid, test);
      instKids.append(testEl);
    }
    instBox.append(instKids);
    tree.append(instBox);
  }
}

// ---- filter ----
document.getElementById("filter").addEventListener("input", (e) => {
  const q = e.target.value.toLowerCase().trim();
  for (const t of tree.querySelectorAll(".test")) {
    t.style.display = !q || t.dataset.search.includes(q) ? "" : "none";
  }
});

// ---- detail rendering ----
const main = document.getElementById("main");

function card(k, v) {
  return el("div", { class: "card" }, el("div", { class: "k", text: k }), el("div", { class: "v", text: v }));
}

function selectTest(node, inst, data, nodeid, test) {
  if (activeEl) activeEl.classList.remove("active");
  activeEl = node; node.classList.add("active");

  main.innerHTML = "";
  main.append(el("div", { class: "breadcrumb", id: "breadcrumb" },
    document.createTextNode(`${inst}`)));
  main.append(el("h2", {}, badge(test.outcome), document.createTextNode(" " + nodeid)));

  main.append(el("div", { class: "meta-line" },
    metaSpan("tracer", data.tracer_version),
    metaSpan("backend", test.backend || data.backend),
    metaSpan("level", test.trace_level || data.trace_level),
    metaSpan("memory", test.memory_tracking || data.memory_tracking),
    metaSpan("python", (data.python_version || "").split(" ")[0])));

  // summary cards
  main.append(el("div", { class: "cards" },
    card("wall time", fmtTime(test.wall_time_s)),
    card("setup", fmtTime(test.setup_time_s)),
    card("call", fmtTime(test.call_time_s)),
    card("teardown", fmtTime(test.teardown_time_s)),
    card("peak RSS", fmtBytes(test.peak_rss_bytes)),
    card("start RSS", fmtBytes(test.start_rss_bytes)),
    card("events", (test.event_count ?? "–") + ""),
    card("call events", (test.total_call_events ?? "–") + ""),
    card("functions", (test.functions || []).length + "")));

  renderFunctions(test.functions || []);
  renderSequence(test);
}

function metaSpan(k, v) {
  return el("span", {}, document.createTextNode(k + ": "), el("b", { text: v || "–" }),
    document.createTextNode("   "));
}

// ---- functions table (sortable) ----
const FN_COLS = [
  { key: "func", label: "function", l: true },
  { key: "file", label: "file:line", l: true },
  { key: "call_count", label: "calls" },
  { key: "total_time_s", label: "total", fmt: fmtTime },
  { key: "exclusive_time_s", label: "self", fmt: fmtTime },
  { key: "max_time_s", label: "max", fmt: fmtTime },
  { key: "max_depth", label: "depth" },
  { key: "exclusive_rss_bytes", label: "self RSS", fmt: fmtBytes },
  { key: "max_rss_inclusive_bytes", label: "max RSS", fmt: fmtBytes },
];

function renderFunctions(fns) {
  main.append(el("div", { class: "section-title", text: `Functions (${fns.length})` }));
  if (!fns.length) { main.append(el("div", { class: "meta-line", text: "no function data" })); return; }

  let sortKey = "exclusive_time_s", sortAsc = false;
  const table = el("table");
  const thead = el("thead"); const trh = el("tr");
  FN_COLS.forEach((c) => {
    const th = el("th", { class: c.l ? "l" : "", text: c.label });
    th.onclick = () => {
      if (sortKey === c.key) sortAsc = !sortAsc;
      else { sortKey = c.key; sortAsc = !!c.l; }
      draw();
    };
    th._col = c; trh.append(th);
  });
  thead.append(trh); table.append(thead);
  const tbody = el("tbody"); table.append(tbody);

  function draw() {
    for (const th of trh.children) {
      th.classList.toggle("sorted", th._col.key === sortKey);
      th.classList.toggle("asc", th._col.key === sortKey && sortAsc);
    }
    const rows = fns.slice().sort((a, b) => {
      let x = a[sortKey], y = b[sortKey];
      if (typeof x === "string") { x = x || ""; y = y || ""; return sortAsc ? x.localeCompare(y) : y.localeCompare(x); }
      x = x ?? 0; y = y ?? 0; return sortAsc ? x - y : y - x;
    });
    tbody.innerHTML = "";
    for (const f of rows) {
      const tr = el("tr");
      FN_COLS.forEach((c) => {
        if (c.key === "func") {
          tr.append(el("td", { class: "l fn mono", text: f.func, title: f.func }));
        } else if (c.key === "file") {
          tr.append(el("td", { class: "l file mono", text: (f.file || "") + ":" + (f.lineno ?? "") }));
        } else {
          const v = f[c.key];
          tr.append(el("td", { text: c.fmt ? c.fmt(v) : (v ?? "–") + "" }));
        }
      });
      tbody.append(tr);
    }
  }
  draw();
  main.append(table);
}

// ---- call sequence ----
function renderSequence(test) {
  const seq = test.call_sequence || [];
  const title = el("div", { class: "section-title" },
    document.createTextNode(`Call sequence (${seq.length}`
      + (test.total_call_events && test.total_call_events !== seq.length ? ` of ${test.total_call_events}` : "")
      + ")"));
  main.append(title);

  if (test.call_sequence_truncated) {
    main.append(el("div", { class: "trunc-note",
      text: `⚠ Sequence truncated — showing first ${seq.length} of ${test.total_call_events} call events.` }));
  }
  if (!seq.length) { main.append(el("div", { class: "meta-line", text: "no call sequence" })); return; }

  const maxNs = Math.max(1, ...seq.map((s) => s.wall_ns || 0));
  const wrap = el("div");

  // Render lazily in chunks to stay responsive for long sequences.
  let i = 0;
  const CHUNK = 500;
  function renderChunk() {
    const frag = document.createDocumentFragment();
    const end = Math.min(i + CHUNK, seq.length);
    for (; i < end; i++) {
      const s = seq[i];
      const depth = s.depth || 0;
      const pct = ((s.wall_ns || 0) / maxNs) * 100;
      const bar = el("div", { class: "seq-bar-wrap" }, el("div", { class: "seq-bar" }));
      bar.firstChild.style.width = pct.toFixed(1) + "%";
      const indent = el("span", { class: "fn mono",
        text: "  ".repeat(depth) + s.func, title: s.func });
      indent.style.flex = "0 0 auto";
      indent.style.maxWidth = "42%";
      indent.style.overflow = "hidden";
      indent.style.textOverflow = "ellipsis";
      indent.style.whiteSpace = "nowrap";
      frag.append(el("div", { class: "seq-row" },
        el("span", { class: "seq-depth", text: "d" + depth }),
        indent, bar,
        el("span", { class: "seq-time mono", text: fmtNs(s.wall_ns) })));
    }
    wrap.append(frag);
    if (i < seq.length) {
      const more = el("div", { class: "toggle",
        text: `▾ show ${Math.min(CHUNK, seq.length - i)} more (${i}/${seq.length})` });
      more.onclick = () => { more.remove(); renderChunk(); };
      wrap.append(more);
    }
  }
  renderChunk();
  main.append(wrap);
}

buildTree();
</script>
</body>
</html>
"""

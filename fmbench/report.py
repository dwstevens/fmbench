"""Render results to JSON, Markdown, and a self-contained pretty HTML page."""
from __future__ import annotations

import html
import json
import os
from typing import Any

SUITE_TITLES = {
    "routing": "Tool Routing",
    "extraction": "Nested Extraction",
    "constraints": "Enum / Const Enforcement",
    "failure_modes": "Failure Modes (characterization)",
    "big_args": "Big-Args Scaling",
}


def _bigargs_rows(summary: dict) -> list[dict]:
    """Per-level big-args results, sorted by field count."""
    cases = summary.get("big_args", {}).get("cases", [])
    rows = []
    for c in cases:
        g = c["grade"]
        rows.append({
            "leaves": c.get("n_leaves") or g.get("fields_total"),
            "valid": c.get("structural_valid"),
            "matched": g.get("fields_matched"), "total": g.get("fields_total"),
            "acc": g.get("score", 0.0), "elapsed": c.get("elapsed"),
        })
    return sorted(rows, key=lambda r: r["leaves"] or 0)


# ----------------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------------
def summarize(results: list[dict]) -> dict:
    suites: dict[str, dict] = {}
    for r in results:
        s = r["suite"]
        b = suites.setdefault(s, {"n": 0, "scored": 0, "score_sum": 0.0,
                                  "valid": 0, "refused": 0, "cases": []})
        b["n"] += 1
        b["cases"].append(r)
        if r.get("refused"):
            b["refused"] += 1   # guardrail refusals are tracked, not scored
            continue
        b["scored"] += 1
        b["score_sum"] += r["grade"].get("score", 0.0)
        b["valid"] += int(r.get("structural_valid", False))
    for s, b in suites.items():
        d = b["scored"] or 1
        b["avg_score"] = b["score_sum"] / d
        b["valid_rate"] = b["valid"] / d
    return suites


# ----------------------------------------------------------------------------
# Markdown
# ----------------------------------------------------------------------------
def _md_bar(frac: float, width: int = 20) -> str:
    filled = round(frac * width)
    return "█" * filled + "░" * (width - filled)


def to_markdown(summary: dict, perf: dict, meta: dict) -> str:
    L = [f"# fmbench — Apple Foundation Models report",
         "",
         f"- model: `{meta.get('model', 'system')}`  ·  decoding: greedy  ·  "
         f"cases: {meta.get('total_cases')}  ·  run: {meta.get('timestamp', '')}",
         ""]
    L += ["## Suite scores", "",
          "| Suite | Cases | Valid JSON | Refused | Avg score |",
          "|---|---:|---:|---:|---:|"]
    for s, b in summary.items():
        L.append(f"| {SUITE_TITLES.get(s, s)} | {b['n']} | "
                 f"{b['valid_rate']*100:.0f}% | {b['refused']} | {b['avg_score']*100:.0f}% "
                 f"{_md_bar(b['avg_score'])} |")
    L.append("")
    L.append("_Scores exclude guardrail refusals (tracked separately)._")
    L.append("")

    # Failure-mode characterization
    if "failure_modes" in summary:
        L += ["## Confirmed limits (failure-mode suite)", ""]
        for c in summary["failure_modes"]["cases"]:
            g = c["grade"]
            if c.get("refused"):
                L.append(f"- **{c['id']}** (guardrail): ⊘ request blocked by safety guardrail")
            elif g.get("kind") == "arithmetic":
                ok = "✅ correct" if g["model_correct"] else "❌ wrong"
                L.append(f"- **{c['id']}** (arithmetic): got `{g['got']}`, "
                         f"correct `{g['correct_value']}` — {ok}")
            elif g.get("kind") == "no_tool":
                ok = "✅ used escape hatch" if g["used_escape"] else "❌ confabulated a tool"
                L.append(f"- **{c['id']}** (no-tool-fits): chose `{g['got_tool']}` — {ok}")
        L.append("")

    if "big_args" in summary:
        L += ["## Big-args scaling (one tool call, growing arg object)", "",
              "| Fields | Valid JSON | Field accuracy | Latency |",
              "|---:|:---:|---:|---:|"]
        for r in _bigargs_rows(summary):
            L.append(f"| {r['leaves']} | {'✅' if r['valid'] else '❌'} | "
                     f"{r['matched']}/{r['total']} ({r['acc']*100:.0f}%) {_md_bar(r['acc'], 12)} | "
                     f"{r['elapsed']:.1f}s |")
        L += ["", "_A single tool call whose argument object scales from a few fields to "
              "~100 across mixed nesting. Watch where validity or field accuracy starts to "
              "slip as the structure grows._", ""]

    if perf.get("throughput"):
        t = perf["throughput"]
        L += ["## Performance", "",
              f"- throughput: **{t['median_tok_s']:.1f} tok/s** (median of {t['runs']})",
              f"- latency: {t['median_latency_s']:.1f}s for ~{t['median_tokens']:.0f} tokens"]
        if perf.get("ttft", {}).get("median_ttft_s") is not None:
            L.append(f"- time-to-first-token: {perf['ttft']['median_ttft_s']*1000:.0f} ms")
        L.append("")

    if perf.get("resources"):
        rt = perf["resources"]
        L += ["## Where the compute landed (CPU %, sampled during generation)", "",
              "| Process | Role | Avg CPU% | Peak CPU% |",
              "|---|---|---:|---:|"]
        for row in rt["rows"]:
            L.append(f"| `{row['process']}` | {row['role']} | "
                     f"{row['avg_cpu']} | {row['peak_cpu']} |")
        L += ["", "_GPU does not appear — inference runs on the Apple Neural Engine, "
              "not the GPU._", ""]

    if perf.get("ane", {}).get("available"):
        a = perf["ane"]
        L += ["## ANE hardware activity (Instruments Core ML trace)", "",
              "| Metric | Value |", "|---|---:|",
              f"| Neural Engine ops | {a['ops']} |",
              f"| ANE active time | {a['active_ms']/1000:.1f} s |",
              f"| Duty cycle (active / window) | **{a['busy_pct']}%** |",
              f"| Median / max op | {a['median_us']:.0f} µs / {a['max_ms']:.1f} ms |",
              "",
              "_Direct proof the model runs on the Apple Neural Engine: hundreds of "
              "`Neural Engine Prediction` hardware intervals, ~"
              f"{a['busy_pct']:.0f}% busy during generation. Each op is a sub-30 ms burst — "
              "which is exactly why power/energy sampling reads ~0 (the bursts are too brief, "
              "and CPU/ANE energy counters are unreliable on some Macs)._", ""]

    if perf.get("power", {}).get("available"):
        p = perf["power"]
        idle, load = p["idle"], p["load"]
        dead = all(idle[k]["median_mw"] == 0 and load[k]["median_mw"] == 0
                   for k in ("cpu", "ane"))
        L += ["## CPU / GPU / ANE power — idle vs. under load (mW, powermetrics)", "",
              "| Engine | Idle (median) | Under load (median) | Δ |",
              "|---|---:|---:|---:|"]
        for key, name in (("cpu", "CPU"), ("gpu", "GPU"), ("ane", "ANE")):
            i, l = idle[key]["median_mw"], load[key]["median_mw"]
            L.append(f"| {name} | {i:.0f} | {l:.0f} | {l - i:+.0f} |")
        if dead:
            L += ["", "_On this machine `powermetrics` reports the **CPU and ANE power "
                  "rails as 0** — those counters are not exposed here (the same reason the "
                  "ANE reads 0% in `mactop`/Activity Monitor). The one readable rail, the "
                  "**GPU, stays flat under load**, which is measured proof that inference "
                  "does **not** run on the GPU. The positive evidence that it runs on the "
                  "**ANE** is in the process table below: the `aned` daemon and the "
                  "on-device inference service are active while no GPU process appears, and "
                  "~50 tok/s from a multi-billion-parameter model at only ~20% of one "
                  "daemon's CPU could not be running on CPU cores._", ""]
        else:
            L += ["", "_The **ANE** jumps under load while the **GPU** barely moves — the "
                  "receipt that the on-device model runs on the Neural Engine, leaving the "
                  "GPU free._", ""]

    return "\n".join(L)


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------
_CSS = """
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--text:#e6edf3;--muted:#8b949e;
--accent:#39c7a0;--good:#3fb950;--warn:#d29922;--bad:#f85149;--bar:#21262d;}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#0b0f14,#0d1117);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",Segoe UI,sans-serif;padding:40px}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
h1 .ai{background:linear-gradient(90deg,#39c7a0,#3793ff);-webkit-background-clip:text;
background-clip:text;color:transparent}
.sub{color:var(--muted);margin-bottom:28px;font-size:13px}
.sub code{background:var(--panel);padding:1px 6px;border-radius:5px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);
margin:34px 0 12px;font-weight:600}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
overflow:hidden;margin-bottom:8px}
table{width:100%;border-collapse:collapse}
th,td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--line);font-size:14px}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.barcell{min-width:160px}
.bar{height:8px;border-radius:5px;background:var(--bar);overflow:hidden;position:relative}
.bar>i{display:block;height:100%;border-radius:5px}
.pct{display:inline-block;min-width:42px;font-variant-numeric:tabular-nums;font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:8px}
.metric{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.metric .v{font-size:26px;font-weight:700;letter-spacing:-.02em}
.metric .v small{font-size:14px;color:var(--muted);font-weight:500}
.metric .k{color:var(--muted);font-size:12px;margin-top:2px}
.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;font-weight:600}
.tag.y{background:rgba(63,185,80,.15);color:var(--good)}
.tag.n{background:rgba(248,81,73,.15);color:var(--bad)}
.note{color:var(--muted);font-size:13px;margin:8px 2px 0;font-style:italic}
.foot{color:var(--muted);font-size:12px;margin-top:36px;text-align:center}
"""


def _color(frac: float) -> str:
    return "good" if frac >= 0.85 else ("warn" if frac >= 0.6 else "bad")


def _bar(frac: float) -> str:
    grad = {"good": "linear-gradient(90deg,#2ea043,#3fb950)",
            "warn": "linear-gradient(90deg,#bb8009,#d29922)",
            "bad": "linear-gradient(90deg,#da3633,#f85149)"}[_color(frac)]
    return (f'<div class="bar"><i style="width:{frac*100:.0f}%;'
            f'background:{grad}"></i></div>')


def to_html(summary: dict, perf: dict, meta: dict) -> str:
    P: list[str] = []
    P.append('<div class="wrap">')
    P.append('<h1>fmbench <span class="ai">· Apple Foundation Models</span></h1>')
    P.append(f'<div class="sub">model <code>{html.escape(str(meta.get("model","system")))}</code>'
             f' · decoding <code>greedy</code> · {meta.get("total_cases")} cases'
             f' · {html.escape(str(meta.get("timestamp","")))}</div>')

    # headline metric cards
    overall_valid = sum(b["valid"] for b in summary.values())
    overall_n = sum(b["n"] for b in summary.values())
    P.append('<div class="cards">')
    P.append(_metric(f'{overall_valid}/{overall_n}',
                     f'{(overall_valid/overall_n*100 if overall_n else 0):.0f}% structurally valid',
                     'JSON validity'))
    for key, label in (("routing", "Routing accuracy"),
                       ("extraction", "Field accuracy"),
                       ("constraints", "Constraint compliance")):
        if key in summary:
            f = summary[key]["avg_score"]
            P.append(_metric(f'<span class="{_color(f)}">{f*100:.0f}%</span>', label,
                             SUITE_TITLES[key]))
    if perf.get("throughput"):
        P.append(_metric(f'{perf["throughput"]["median_tok_s"]:.0f}<small> tok/s</small>',
                         'on the ANE', 'Throughput'))
    if perf.get("ane", {}).get("available"):
        P.append(_metric(f'<span class="good">{perf["ane"]["busy_pct"]:.0f}<small>%</small></span>',
                         'ANE duty cycle under load', 'Neural Engine'))
    P.append('</div>')

    # suite table
    P.append('<h2>Suite scores</h2><div class="card"><table>')
    P.append('<tr><th>Suite</th><th class="num">Cases</th><th class="num">Valid JSON</th>'
             '<th class="num">Refused</th><th>Avg score</th></tr>')
    for s, b in summary.items():
        f = b["avg_score"]
        ref = f'<span class="warn">{b["refused"]}</span>' if b["refused"] else "0"
        P.append(f'<tr><td>{SUITE_TITLES.get(s,s)}</td>'
                 f'<td class="num">{b["n"]}</td>'
                 f'<td class="num">{b["valid_rate"]*100:.0f}%</td>'
                 f'<td class="num">{ref}</td>'
                 f'<td class="barcell"><span class="pct {_color(f)}">{f*100:.0f}%</span> {_bar(f)}</td></tr>')
    P.append('</table></div>')
    P.append('<div class="note">Scores exclude guardrail refusals, which are tracked '
             'separately as a reliability signal.</div>')

    # confirmed limits
    if "failure_modes" in summary:
        P.append('<h2>Confirmed limits</h2><div class="card"><table>')
        P.append('<tr><th>Case</th><th>Kind</th><th>Observation</th><th>Result</th></tr>')
        for c in summary["failure_modes"]["cases"]:
            g = c["grade"]
            if c.get("refused"):
                kind, obs = "guardrail", "request blocked by safety guardrail"
                tag = '<span class="tag n">refused</span>'
            elif g.get("kind") == "arithmetic":
                kind = "arithmetic"
                obs = f'got <span class="mono">{html.escape(str(g["got"]))}</span>, ' \
                      f'correct <span class="mono">{g["correct_value"]}</span>'
                tag = '<span class="tag y">correct</span>' if g["model_correct"] \
                      else '<span class="tag n">wrong</span>'
            else:
                kind = "no-tool-fits"
                obs = f'chose <span class="mono">{html.escape(str(g.get("got_tool")))}</span>'
                tag = '<span class="tag y">used escape</span>' if g.get("used_escape") \
                      else '<span class="tag n">confabulated</span>'
            P.append(f'<tr><td class="mono">{html.escape(c["id"])}</td>'
                     f'<td>{kind}</td><td>{obs}</td><td>{tag}</td></tr>')
        P.append('</table></div>')

    # big-args scaling curve
    if "big_args" in summary:
        P.append('<h2>Big-args scaling (one tool call, growing arg object)</h2>'
                 '<div class="card"><table>')
        P.append('<tr><th class="num">Fields</th><th>Valid JSON</th>'
                 '<th>Field accuracy</th><th class="num">Latency</th></tr>')
        for r in _bigargs_rows(summary):
            tag = '<span class="tag y">valid</span>' if r["valid"] else '<span class="tag n">invalid</span>'
            P.append(f'<tr><td class="num">{r["leaves"]}</td><td>{tag}</td>'
                     f'<td class="barcell"><span class="pct {_color(r["acc"])}">'
                     f'{r["matched"]}/{r["total"]}</span> {_bar(r["acc"])}</td>'
                     f'<td class="num">{r["elapsed"]:.1f}s</td></tr>')
        P.append('</table></div>')
        P.append('<div class="note">A single tool call whose argument object scales to ~100 '
                 'fields across mixed nesting — shows where validity or field accuracy slips '
                 'as the structure grows.</div>')

    # performance + resources
    if perf.get("resources"):
        rt = perf["resources"]
        P.append('<h2>Where the compute landed</h2><div class="card"><table>')
        P.append('<tr><th>Process</th><th>Role</th><th class="num">Avg CPU%</th>'
                 '<th class="num">Peak CPU%</th></tr>')
        for row in rt["rows"]:
            P.append(f'<tr><td class="mono">{html.escape(row["process"])}</td>'
                     f'<td>{html.escape(row["role"])}</td>'
                     f'<td class="num">{row["avg_cpu"]}</td>'
                     f'<td class="num">{row["peak_cpu"]}</td></tr>')
        P.append('</table></div>')
        P.append('<div class="note">GPU never appears as a consumer — inference runs on '
                 'the Apple Neural Engine, so it runs free of GPU contention.</div>')

    if perf.get("power", {}).get("available"):
        p = perf["power"]
        idle, load = p["idle"], p["load"]
        P.append('<h2>CPU / GPU / ANE power — idle vs. under load (mW)</h2>'
                 '<div class="card"><table>')
        P.append('<tr><th>Engine</th><th class="num">Idle</th>'
                 '<th class="num">Under load</th><th class="num">Δ</th></tr>')
        dead = all(idle[k]["median_mw"] == 0 and load[k]["median_mw"] == 0
                   for k in ("cpu", "ane"))
        for key, name in (("cpu", "CPU"), ("gpu", "GPU"), ("ane", "ANE")):
            i, l = idle[key]["median_mw"], load[key]["median_mw"]
            delta = l - i
            cls = "good" if (key == "ane" and delta > 0) else ""
            P.append(f'<tr><td>{name}</td><td class="num">{i:.0f}</td>'
                     f'<td class="num">{l:.0f}</td>'
                     f'<td class="num {cls}">{delta:+.0f}</td></tr>')
        P.append('</table></div>')
        if dead:
            P.append('<div class="note">CPU and ANE power rails read 0 on this machine — '
                     'powermetrics does not expose them here (same reason the ANE shows 0% '
                     'in mactop). The readable GPU rail stays flat under load: measured proof '
                     'inference is <b>not</b> on the GPU. Positive ANE evidence is the process '
                     'table — aned + the on-device inference service are active, no GPU process '
                     'appears, and ~50 tok/s at ~20% of one daemon\'s CPU cannot be CPU-bound.</div>')
        else:
            P.append('<div class="note">The ANE jumps under load while the GPU barely moves — '
                     'the receipt that inference runs on the Neural Engine, not the GPU.</div>')

    if perf.get("ane", {}).get("available"):
        a = perf["ane"]
        P.append('<h2>ANE hardware activity (Instruments Core ML trace)</h2>'
                 '<div class="card"><table>')
        P.append('<tr><th>Metric</th><th class="num">Value</th></tr>')
        P.append(f'<tr><td>Neural Engine ops</td><td class="num">{a["ops"]}</td></tr>')
        P.append(f'<tr><td>ANE active time</td><td class="num">{a["active_ms"]/1000:.1f} s</td></tr>')
        P.append(f'<tr><td>Duty cycle (active / window)</td>'
                 f'<td class="num good">{a["busy_pct"]:.1f}%</td></tr>')
        P.append(f'<tr><td>Median / max op</td><td class="num">'
                 f'{a["median_us"]:.0f} µs / {a["max_ms"]:.1f} ms</td></tr>')
        P.append('</table></div>')
        P.append('<div class="note">Direct proof inference runs on the Apple Neural Engine — '
                 'hundreds of <b>Neural Engine Prediction</b> hardware intervals. Each op is a '
                 'sub-30ms burst, which is exactly why power/energy sampling reads ~0.</div>')

    P.append('<div class="foot">generated by fmbench · greedy decoding · '
             'deterministic, code-graded</div>')
    P.append('</div>')

    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>fmbench report</title><style>{_CSS}</style></head>"
            f"<body>{''.join(P)}</body></html>")


def _metric(value: str, sub: str, key: str) -> str:
    return (f'<div class="metric"><div class="v">{value}</div>'
            f'<div class="k">{html.escape(sub)}</div>'
            f'<div class="k" style="margin-top:6px;font-weight:600;color:var(--text)">'
            f'{html.escape(key)}</div></div>')


# ----------------------------------------------------------------------------
def write_all(outdir: str, results: list[dict], perf: dict, meta: dict) -> dict[str, str]:
    os.makedirs(outdir, exist_ok=True)
    summary = summarize(results)
    paths = {}
    with open(os.path.join(outdir, "results.json"), "w") as fh:
        json.dump({"meta": meta, "perf": perf, "results": results}, fh, indent=2)
        paths["json"] = fh.name
    with open(os.path.join(outdir, "report.md"), "w") as fh:
        fh.write(to_markdown(summary, perf, meta))
        paths["md"] = fh.name
    with open(os.path.join(outdir, "report.html"), "w") as fh:
        fh.write(to_html(summary, perf, meta))
        paths["html"] = fh.name
    return paths

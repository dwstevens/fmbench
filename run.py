#!/usr/bin/env python3
"""fmbench entrypoint — run the suites, grade, and emit JSON / Markdown / HTML reports.

Usage:
    uv run python run.py                      # all suites + perf
    uv run python run.py --suite routing      # one suite
    uv run python run.py --no-perf            # skip the perf/resource track
    uv run python run.py --power              # add CPU/GPU/ANE power table (needs sudo)
    uv run python run.py --quick              # first 4 cases per suite, no perf
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time

from fmbench import ane as anemod, grading, perf as perfmod, report, schemas
from fmbench.runner import run_fm

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["routing", "extraction", "constraints", "failure_modes"]

DEFAULT_INSTRUCTIONS = {
    "routing": ("You are a tool router. Choose exactly ONE tool and output only its "
                "JSON arguments matching the schema. Today is Wednesday, 2026-06-10."),
    "extraction": "Extract the structured data from the message into JSON matching the schema.",
    "constraints": ("Output JSON matching the schema. Choose every value only from the "
                    "allowed options defined in the schema."),
    "failure_modes": "Choose exactly ONE tool that matches the request.",
}

C = {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "d": "\033[2m",
     "b": "\033[1m", "x": "\033[0m", "c": "\033[36m"}


def _color(frac: float) -> str:
    return C["g"] if frac >= 0.85 else (C["y"] if frac >= 0.6 else C["r"])


def load_cases(suite: str, limit: int | None) -> list[dict]:
    path = os.path.join(HERE, "cases", f"{suite}.jsonl")
    cases = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases[:limit] if limit else cases


def run(suites: list[str], limit: int | None, schema_paths: dict[str, str]) -> list[dict]:
    schema_objs = schemas.build_all()
    results: list[dict] = []
    for suite in suites:
        cases = load_cases(suite, limit)
        print(f"\n{C['b']}{C['c']}▶ {suite}{C['x']} ({len(cases)} cases)")
        for case in cases:
            schema_key = case["schema"]
            schema_path = schema_paths[schema_key]
            schema_obj = schema_objs[schema_key]
            instr = case.get("instructions", DEFAULT_INSTRUCTIONS[suite])
            res = run_fm(case["prompt"], schema_path=schema_path, instructions=instr)

            obj = res.obj
            structural_valid = bool(obj is not None and not grading.validate(obj, schema_obj))
            if res.refused:
                grade = {"score": 0.0, "refused": True}
            elif obj is None:
                grade = {"score": 0.0, "error": res.parse_error or "no output"}
            else:
                grade = grading.GRADERS[suite](obj, schema_obj, case["expect"])

            row = {
                "id": case["id"], "suite": suite, "prompt": case["prompt"],
                "output": obj, "raw": res.raw.strip() if obj is None else None,
                "elapsed": round(res.elapsed, 2), "refused": res.refused,
                "structural_valid": structural_valid, "grade": grade,
            }
            results.append(row)

            if res.refused:
                mark = f"{C['y']}⊘{C['x']}"
            elif grade.get("score", 0) >= 0.999:
                mark = f"{C['g']}✓{C['x']}"
            elif grade.get("score", 0) > 0:
                mark = f"{C['y']}~{C['x']}"
            else:
                mark = f"{C['r']}✗{C['x']}"
            extra = f"{C['y']}guardrail refusal{C['x']}" if res.refused else _case_note(suite, grade)
            print(f"  {mark} {case['id']:<28} {C['d']}{res.elapsed:4.1f}s{C['x']}  {extra}")
    return results


def _case_note(suite: str, g: dict) -> str:
    if "error" in g:
        return f"{C['r']}{g['error']}{C['x']}"
    if suite == "routing":
        t = "tool✓" if g.get("tool_ok") else ("tool~drift" if g.get("tool_drifted") else "tool✗")
        return f"{t} args {g.get('args_matched')}/{g.get('args_total')}"
    if suite == "extraction":
        return f"fields {g.get('fields_matched')}/{g.get('fields_total')}"
    if suite == "constraints":
        v = g.get("violations") or []
        return "compliant" if g.get("compliant") else f"{C['r']}{'; '.join(v)[:60]}{C['x']}"
    if suite == "failure_modes":
        if g.get("kind") == "arithmetic":
            return f"got {g.get('got')} want {g.get('correct_value')} " + \
                   ("✓" if g.get("model_correct") else "✗")
        return f"chose {g.get('got_tool')} " + ("✓escape" if g.get("used_escape") else "✗confab")
    return ""


def print_summary(summary: dict) -> None:
    print(f"\n{C['b']}══ Summary ═════════════════════════════════════════════{C['x']}")
    print(f"  {'Suite':<28}{'Cases':>6}{'Valid':>8}{'Refus':>7}{'Score':>8}")
    for s, b in summary.items():
        col = _color(b["avg_score"])
        print(f"  {report.SUITE_TITLES.get(s, s):<28}{b['n']:>6}"
              f"{b['valid_rate']*100:>7.0f}%{b['refused']:>7}{col}{b['avg_score']*100:>7.0f}%{C['x']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="fmbench — Apple FM structured-output benchmark")
    ap.add_argument("--suite", action="append", choices=SUITES, help="suite(s) to run")
    ap.add_argument("--limit", type=int, help="max cases per suite")
    ap.add_argument("--quick", action="store_true", help="4 cases/suite, skip perf")
    ap.add_argument("--no-perf", action="store_true", help="skip perf/resource track")
    ap.add_argument("--power", action="store_true", help="add CPU/GPU/ANE power table (sudo)")
    ap.add_argument("--ane", action="store_true",
                    help="capture ANE hardware-interval activity via Instruments (~20s, no sudo)")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args = ap.parse_args()

    suites = args.suite or SUITES
    limit = 4 if args.quick else args.limit

    schema_paths = schemas.write_all(os.path.join(HERE, "schemas"))
    results = run(suites, limit, schema_paths)

    perf: dict = {}
    if not args.no_perf and not args.quick:
        print(f"\n{C['b']}{C['c']}▶ perf{C['x']} (throughput / ttft / resources)")
        perf["throughput"] = perfmod.throughput()
        print(f"  throughput: {C['b']}{perf['throughput']['median_tok_s']:.1f} tok/s{C['x']}")
        perf["ttft"] = perfmod.ttft()
        if perf["ttft"]["median_ttft_s"]:
            print(f"  ttft:       {perf['ttft']['median_ttft_s']*1000:.0f} ms")
        perf["resources"] = perfmod.sample_resources().__dict__
        if args.power:
            print("  power: validating sudo (powermetrics needs root)…")
            subprocess.run(["sudo", "-v"])  # one interactive prompt, inherits terminal
            print("  power: sampling CPU/GPU/ANE for ~20s under sustained load…")
            perf["power"] = perfmod.sample_power()
            p = perf["power"]
            if p.get("available"):
                il, lo = p["idle"], p["load"]
                print(f"  power: ANE {il['ane']['median_mw']:.0f}→{lo['ane']['median_mw']:.0f}"
                      f" · GPU {il['gpu']['median_mw']:.0f}→{lo['gpu']['median_mw']:.0f}"
                      f" · CPU {il['cpu']['median_mw']:.0f}→{lo['cpu']['median_mw']:.0f} mW (idle→load)")
            else:
                print(f"  {C['y']}power: unavailable — {p.get('reason')}{C['x']}")

    if args.ane and not args.quick:
        print(f"\n{C['b']}{C['c']}▶ ANE{C['x']} (Instruments Core ML hardware-interval trace, ~20s)…")
        perf["ane"] = anemod.measure_ane()
        a = perf["ane"]
        if a.get("available"):
            print(f"  ANE: {C['b']}{a['ops']} ops · {a['active_ms']/1000:.1f}s active · "
                  f"{a['busy_pct']}% duty cycle{C['x']} (median op {a['median_us']:.0f}µs)")
        else:
            print(f"  {C['y']}ANE: unavailable — {a.get('reason')}{C['x']}")

    meta = {"model": "system", "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_cases": len(results), "suites": suites}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(args.out, stamp)
    paths = report.write_all(outdir, results, perf, meta)

    summary = report.summarize(results)
    print_summary(summary)
    if perf.get("resources"):
        print(f"\n{C['b']}══ Where the compute landed (CPU%) ══════════════════════{C['x']}")
        print(f"  {'Process':<46}{'Avg':>6}{'Peak':>7}")
        for r in perf["resources"]["rows"]:
            print(f"  {r['process']:<46}{r['avg_cpu']:>6}{r['peak_cpu']:>7}")
        print(f"  {C['d']}(GPU absent — inference runs on the ANE){C['x']}")
    pw = perf.get("power")
    if pw and pw.get("available"):
        il, lo = pw["idle"], pw["load"]
        print(f"\n{C['b']}══ CPU / GPU / ANE power — idle → load (mW) ═════════════{C['x']}")
        print(f"  {'Engine':<8}{'Idle':>8}{'Load':>8}{'Δ':>9}")
        for key, name in (("cpu", "CPU"), ("gpu", "GPU"), ("ane", "ANE")):
            i, l = il[key]["median_mw"], lo[key]["median_mw"]
            col = C["g"] if (key == "ane" and l - i > 0) else ""
            print(f"  {name:<8}{i:>8.0f}{l:>8.0f}{col}{l - i:>+9.0f}{C['x']}")
        print(f"  {C['d']}(ANE jumps under load, GPU barely moves — the receipts){C['x']}")
    an = perf.get("ane")
    if an and an.get("available"):
        print(f"\n{C['b']}══ ANE hardware activity (Instruments Core ML trace) ════{C['x']}")
        print(f"  {'Neural Engine ops':<26}{an['ops']:>10}")
        print(f"  {'ANE active time':<26}{an['active_ms']/1000:>9.1f}s")
        print(f"  {'duty cycle (active/window)':<26}{C['g']}{an['busy_pct']:>9.1f}%{C['x']}")
        print(f"  {'median / max op':<26}{an['median_us']:>6.0f}µs /{an['max_ms']:>5.1f}ms")
        print(f"  {C['d']}(every ANE op a sub-30ms burst — why power sampling reads ~0){C['x']}")
    print(f"\n{C['g']}reports:{C['x']} {paths['html']}")
    print(f"         {paths['md']}\n         {paths['json']}")


if __name__ == "__main__":
    main()

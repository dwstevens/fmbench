# AGENTS.md

Guidance for AI coding agents (and humans) working in **fmbench**. This is the
canonical agent guide; `CLAUDE.md` points here.

## What this project is

A deterministic benchmark for **Apple Foundation Models** structured output and tool
calling, driven entirely through the on-device `fm` CLI (macOS Apple Intelligence). No
network, no third-party Python deps — standard library only. Outputs are graded in code,
not by an LLM judge, and every run uses `--greedy` so scores are reproducible.

## Environment & commands

- Requires macOS with Apple Intelligence enabled, so `/usr/bin/fm` exists. Check with
  `fm available` (expect "System model available").
- Python is managed with [`uv`](https://docs.astral.sh/uv/). Do **not** use bare `pip`
  or activate the venv manually.

```bash
uv run python run.py                 # all suites + perf/resource track
uv run python run.py --quick         # 4 cases/suite, no perf — fast smoke test
uv run python run.py --suite routing # one suite (repeatable: --suite a --suite b)
uv run python run.py --no-perf       # skip perf/resource sampling
uv run python run.py --power         # add CPU/GPU/ANE power table (prompts for sudo)
uv run python run.py --ane           # ANE hardware-interval capture via Instruments (~20s, needs xctrace)

uv run python -m py_compile run.py fmbench/*.py   # syntax check
uv run python -m fmbench.schemas schemas          # regenerate schema files
```

Use `--quick` while iterating: it exercises the full pipeline (schema → `fm` →
parse → grade → report) in ~20s without the multi-minute perf track.

## Architecture (one job per module)

- `run.py` — entrypoint. Loads cases, applies the per-suite default instruction (or a
  case override), runs each through `fm`, grades, prints colored tables, writes reports.
- `fmbench/schemas.py` — builds schemas in the **fm dialect** and exposes a named
  registry (`build_all()` / `write_all()`). Cases reference schemas by key.
- `fmbench/runner.py` — invokes `fm respond`, strips ANSI, parses JSON, and flags
  **guardrail refusals** as a distinct outcome.
- `fmbench/grading.py` — pure functions: structural validation plus one grader per suite
  (`routing`, `extraction`, `constraints`, `failure_modes`). No I/O, no `fm` calls.
- `fmbench/perf.py` — throughput, time-to-first-token, per-process CPU sampling, and
  optional `powermetrics` power sampling.
- `fmbench/ane.py` — records a system-wide Instruments "Core ML" trace via `xctrace` while
  generating, exports the `ane-hw-intervals-internal` table, and reports ANE op count +
  active time + duty cycle. The authoritative "is it the ANE?" signal; no sudo.
- `fmbench/report.py` — renders JSON + Markdown + a self-contained HTML page.
- `cases/*.jsonl` — one file per suite; one JSON object per line.

Data flow: `cases/<suite>.jsonl` → `run.py` → `runner.run_fm` → `grading.GRADERS[suite]`
→ `report.write_all` → `results/<timestamp>/{report.html,report.md,results.json}`.

## The fm schema dialect — read before touching schemas

1. **Every object must include an `x-order` array** listing property order. Omit it and
   `fm` rejects the schema with *"data couldn't be read because it is missing."* The
   `obj()` / `union()` builders in `schemas.py` handle this — always go through them.
2. **`enum` and `const` work but are not exposed by `fm schema object`.** The decoder
   enforces them hard (an out-of-enum value literally cannot be emitted). Use `const` to
   pin tool discriminators and `enum` to lock choice fields.
3. Nested objects go in root-level `$defs` and are referenced with `$ref`. Unions
   (tool routers) use a root of `{title, $defs, anyOf:[{$ref}...]}`.

## Conventions

- Expected values in `cases/*.jsonl` must be **objectively correct** — the harness scores
  against ground truth, so a real model miss should surface as a low score, never be
  tuned away. String comparison is case/space-insensitive; numbers use a small tolerance.
- Known model limits are **characterized, not failed**: arithmetic and relative-date
  reasoning are unreliable, and guardrails occasionally false-positive on benign prompts.
  Keep these in the `failure_modes` suite as observations, excluded from capability scores.
- `results/` and `schemas/` are generated and gitignored. Don't commit them.
- Match the existing style: small focused modules, stdlib only, type hints, short
  docstrings explaining *why*.

## Adding to the benchmark

- **A new case:** add a JSON line to the relevant `cases/<suite>.jsonl` with `id`,
  `suite`, `schema` (a registry key), `prompt`, and `expect` shaped for that suite's
  grader (see existing lines).
- **A new schema:** add a builder + registry entry in `schemas.py`; reference its key
  from cases. Run `--quick` to confirm `fm` accepts it.
- **A new suite:** add a grader to `grading.GRADERS`, a default instruction in
  `run.py`, a `cases/<suite>.jsonl`, and a title in `report.SUITE_TITLES`.

## Verify before claiming done

Run `uv run python run.py --quick` and confirm it completes with sensible per-suite
scores. For perf/report changes, do a full `uv run python run.py` and check the
generated `report.html`.

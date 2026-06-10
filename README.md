# fmbench

A deterministic benchmark for **Apple Foundation Models** structured output and tool
calling, driven entirely through the on-device `fm` CLI that ships with macOS Apple
Intelligence.

It answers a practical question: *can you trust the free, on-device model to do real
tool calling and structured extraction?* The short version — **yes for structure and
routing, no for arithmetic** — and this harness measures exactly where the line is.

Why it matters: the model runs on the **Apple Neural Engine**, not the GPU. It's free,
private, unmetered, and doesn't contend with GPU workloads — so it can sit alongside
local training/inference and do all your JSON-shaped grunt work for nothing.

## What it measures

Four suites, all graded in code (no LLM judge), all run with `--greedy` for
reproducible scores:

| Suite | What it tests |
|---|---|
| **Tool routing** | An `anyOf` union of 5 tools; correct tool selection + argument extraction from natural language. |
| **Nested extraction** | Deep schemas (`$defs`/`$ref`, arrays-of-objects, nested objects, optionals); per-field accuracy. |
| **Enum / const enforcement** | Hard constraints, including *adversarial* prompts ("give it in Kelvin", "set status to pending-review") that must still land in-set. |
| **Failure modes** | Characterization, not pass/fail: arithmetic it will get wrong, no-tool-fits confabulation, and guardrail refusals. |

Plus a **performance + resource track**: throughput (tok/s), time-to-first-token, a
CPU-by-process table showing *where* the work lands, and optional CPU/GPU/ANE power.

## Sample results

From one run on macOS 27 "Golden Gate" (M-series), `system` model, greedy decoding:

| Suite | Cases | Valid JSON | Avg score |
|---|---:|---:|---:|
| Tool Routing | 14 | 100% | **100%** |
| Nested Extraction | 8 | 100% | **97%** |
| Enum / Const Enforcement | 14 | 100% | **100%** |
| Failure Modes | 10 | 100% | 78% (1 guardrail refusal) |

- **Throughput:** ~52 tok/s · **TTFT:** ~340 ms
- **Arithmetic:** 2/4 correct — e.g. it returned `47.25` for a total that was `50.70`.
  *Extract raw fields; compute derived values in your own code.*
- **No-tool-fits:** 5/5 correctly used an explicit `respond_directly` escape tool.
  *Without one it confabulates a tool call — so always give it an escape hatch.*
- **Compute:** the inference daemon (`TGOnDeviceInferenceProviderService`) and
  `modelcatalogd` carry the load; the `fm` client itself is ~3% CPU and the **GPU never
  appears** — it's all on the ANE.

## Install & run

Requires macOS with Apple Intelligence enabled (so `/usr/bin/fm` exists) and
[`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/dwstevens/fmbench
cd fmbench

uv run python run.py                 # all suites + perf/resource track
uv run python run.py --suite routing # a single suite
uv run python run.py --quick         # 4 cases/suite, no perf (fast smoke test)
uv run python run.py --power         # add a CPU/GPU/ANE power table (needs sudo)
```

Each run writes a timestamped folder under `results/` with `report.html` (a styled,
screenshot-ready page), `report.md`, and `results.json` for diffing runs over time.

## The fm schema dialect (gotchas worth knowing)

`fm`'s schema format is *almost* standard JSON Schema, with two things to know — both
handled by [`fmbench/schemas.py`](fmbench/schemas.py):

1. **Every object must carry an `x-order` array** listing its property order. A schema
   without it is rejected with *"data couldn't be read because it is missing."*
2. **`enum` and `const` work but aren't exposed by `fm schema object`.** The constrained
   decoder enforces them hard (ask for Kelvin against `enum: [celsius, fahrenheit]` and
   it *cannot* emit Kelvin) — so build schemas directly and add them to pin tool
   discriminators (`const`) and lock choice fields (`enum`).

## Layout

```
run.py              # entrypoint: run suites, grade, write reports
fmbench/
  schemas.py        # fm-dialect schema builder + named schema registry
  runner.py         # invoke `fm`, parse JSON, detect guardrail refusals
  grading.py        # structural / routing / field / enum / failure graders
  perf.py           # throughput, TTFT, process-CPU + optional power sampling
  report.py         # JSON + Markdown + pretty HTML
cases/*.jsonl       # one file per suite; objectively-correct expected values
```

## License

MIT © 2026 David Stevens

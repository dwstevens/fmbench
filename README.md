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
CPU-by-process table showing *where* the work lands, optional CPU/GPU/ANE power
(`--power`), and an **ANE hardware-activity capture** via Instruments (`--ane`) that
proves the model runs on the Neural Engine even when every power meter reads zero.

Every run is deterministic (greedy decoding) and graded by code, so scores are stable
and runs are diffable over time — it works as a regression harness as the OS updates.

## Sample results

From one run on macOS 27 Golden Gate (build 26A5353q, M-series MacBook Pro),
`system` model, greedy decoding:

**Capability — suite scores**

| Suite | Cases | Valid JSON | Avg score |
|---|---:|---:|---:|
| Tool Routing | 14 | 100% | **100%** |
| Nested Extraction | 8 | 100% | **97%** |
| Enum / Const Enforcement | 14 | 100% | **100%** |
| Failure Modes | 10 | 100% | 78% (1 guardrail refusal) |

**Performance & compute**

| Metric | Value |
|---|---:|
| Throughput | ~52 tok/s |
| Time-to-first-token | ~340 ms |
| ANE duty cycle under load (`--ane`) | **~87%** |
| Neural Engine ops / run | ~1,500 |
| `fm` client CPU | ~3% (it's a thin XPC client) |
| GPU power under load | flat (~idle) — work isn't on the GPU |

**Behavioral findings**

- **Structure is bulletproof:** 100% valid JSON across every suite — the decoder is
  grammar-constrained, so malformed output is impossible. `enum`/`const` are *hard*
  enforced (ask for Kelvin against `[celsius, fahrenheit]` and it physically cannot
  emit Kelvin).
- **Arithmetic is not:** 2/4 correct — it returned `47.25` for a total that was `50.70`.
  *Extract raw fields; compute derived values in your own code.*
- **No-tool-fits:** 5/5 correctly used an explicit `respond_directly` escape tool.
  *Without one it confabulates a tool call — so always give it an escape hatch.*
- **It runs on the ANE:** an Instruments Core ML trace captured **~1,500 "Neural Engine
  Prediction" hardware intervals at ~87% ANE duty cycle** during generation — direct
  proof inference is on the **Apple Neural Engine**, leaving CPU and GPU free.

### Why power tools say the ANE is idle (and how `--ane` proves it isn't)

`powermetrics`, `mactop`, and even raw IOReport energy counters all report **~0 W for the
ANE under load** on some Macs — which is misleading. Two reasons: (1) ANE inference is
hundreds of **sub-30 ms bursts**, so any instantaneous power sample lands in the idle gaps;
(2) on this hardware the CPU/ANE energy rails simply aren't populated (they read 0 even
when busy). The fix isn't a faster sampler — it's a different *kind* of signal. Instruments'
`ane-hw-intervals-internal` table records **every ANE hardware interval** directly, so
`fmbench --ane` can show the ANE was busy ~87% of the generation window even though every
watt-meter read zero. The GPU's only readable power rail, meanwhile, stays flat under load
— measured confirmation the work isn't on the GPU.

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
uv run python run.py --ane           # capture ANE hardware activity (Instruments, ~20s, no sudo)
```

`--ane` needs the Xcode command-line tools (`xctrace`); everything else needs only `fm`.

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
  ane.py            # ANE hardware-interval capture via Instruments Core ML trace
  grading.py        # structural / routing / field / enum / failure graders
  perf.py           # throughput, TTFT, process-CPU + optional power sampling
  report.py         # JSON + Markdown + pretty HTML
cases/*.jsonl       # one file per suite; objectively-correct expected values
```

## Open questions & next experiments

We've mapped a lot, but this model's ceiling is far from charted. Threads worth pulling —
PRs and findings welcome:

- **End-to-end pipelines (the obvious next step).** Chain the model through a full
  pipeline — interactive **chat → freeform response → structured extraction → tool-schema
  fill**, each stage its own `fm` call — and measure where errors *compound* across stages.
  Can a ~3B on-device model drive a multi-stage agent loop, or does it drift?
- **The context ceiling.** Structured output breaks at ~85–90 fields because the
  schema-as-grammar shares the ~4K window with prompt + output. How much does schema
  verbosity actually cost in tokens? Would a more compact schema buy more fields? Is there
  a larger-context configuration?
- **Multimodal.** `fm respond --image` is untested here — how good is *structured
  extraction from images* (receipts, forms, screenshots) on the ANE?
- **`fm serve` as an agent backend.** It speaks the OpenAI API — wire it into a real
  tool-use loop and see how many steps it sustains before losing the thread.
- **Reasoning workarounds.** Arithmetic is ~50%. Does chain-of-thought help, or is the
  honest fix always "give it a calculator tool"?
- **Guardrails.** Benign prompts get refused sometimes — what's the false-positive rate,
  and does `--guardrails permissive-content-transformations` move it?
- **PCC vs `system`.** The Private Cloud Compute model wasn't reachable in our context;
  the capability / latency / quota delta vs on-device is wide open.
- **As a regression harness.** fmbench is deterministic by design — run it across OS
  updates and watch the numbers move as Apple ships new weights.

## License

MIT © 2026 David Stevens

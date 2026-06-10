"""Capture Apple Neural Engine activity via Instruments' Core ML template.

Power/energy counters miss ANE inference: the work is hundreds of sub-30ms bursts,
and on some Macs the CPU/ANE energy rails read 0 entirely (powermetrics, mactop, and
raw IOReport all showed ~0 here). Instruments, however, records every ANE *hardware
interval* directly in the ``ane-hw-intervals-internal`` table. We record a system-wide
Core ML trace while running a generation workload, export the ANE intervals, and report
op count + total active time + duty cycle. This is the authoritative "is it the ANE?"
signal — and it needs no sudo.

Requires the Xcode command-line tools (`xctrace`).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time

# Safe, non-refused prompts (the guardrail rejects some benign phrasings; these generate).
WORKLOAD_PROMPTS = [
    "What is Swift? Explain in detail with examples.",
    "Explain how a transistor works.",
    "Describe the water cycle step by step.",
    "What is photosynthesis and why does it matter?",
    "Summarize how the internet routes packets.",
    "Explain the difference between RAM and an SSD.",
]

_ANE_XPATH = ('/trace-toc/run[@number="1"]/data/'
              'table[@schema="ane-hw-intervals-internal"]')


def available() -> bool:
    if not shutil.which("xcrun"):
        return False
    try:
        subprocess.run(["xcrun", "xctrace", "version"], capture_output=True, timeout=20)
        return True
    except Exception:  # noqa: BLE001
        return False


def _run_workload(time_limit_s: float) -> int:
    """Fire generations back-to-back until the recording window should be ending."""
    deadline = time.perf_counter() + time_limit_s
    n = 0
    while time.perf_counter() < deadline:
        p = WORKLOAD_PROMPTS[n % len(WORKLOAD_PROMPTS)]
        try:
            subprocess.run(["fm", "respond", "--no-stream", p],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30)
        except Exception:  # noqa: BLE001
            break
        n += 1
    return n


def measure_ane(time_limit_s: int = 18, warmup_s: float = 3.0) -> dict:
    """Record a system-wide Core ML trace while generating, and quantify ANE activity.

    Returns {available, ops, active_ms, window_ms, busy_pct, median_us, max_ms, gens}
    or {available: False, reason}.
    """
    if not available():
        return {"available": False, "reason": "xctrace not found (install Xcode command-line tools)"}

    tmp = tempfile.mkdtemp(prefix="fmbench-ane-")
    trace = os.path.join(tmp, "coreml.trace")
    rec = subprocess.Popen(
        ["xcrun", "xctrace", "record", "--template", "Core ML", "--all-processes",
         "--time-limit", f"{time_limit_s}s", "--output", trace],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    time.sleep(warmup_s)                       # let the recorder spin up
    gens = _run_workload(time_limit_s - warmup_s)
    rec.wait()

    try:
        xml = subprocess.run(
            ["xcrun", "xctrace", "export", "--input", trace, "--xpath", _ANE_XPATH],
            capture_output=True, text=True, timeout=120).stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Each row carries its own <duration ...>NS</duration> (no XML refs) — parse directly.
    durs = [int(m) for m in re.findall(r"<duration[^>]*>(\d+)</duration>", xml)]
    starts = [int(m) for m in re.findall(r"<start-time[^>]*>(\d+)</start-time>", xml)]
    if not durs:
        return {"available": False,
                "reason": "no ANE hardware intervals captured (Core ML may not have run on the ANE)"}

    active_ns = sum(durs)
    span_ns = (max(s + d for s, d in zip(starts, durs)) - min(starts)) if starts else active_ns
    durs_sorted = sorted(durs)
    return {
        "available": True,
        "ops": len(durs),
        "active_ms": round(active_ns / 1e6, 1),
        "window_ms": round(span_ns / 1e6, 1),
        "busy_pct": round(100 * active_ns / span_ns, 1) if span_ns else None,
        "median_us": round(durs_sorted[len(durs_sorted) // 2] / 1e3, 1),
        "max_ms": round(max(durs) / 1e6, 1),
        "gens": gens,
    }

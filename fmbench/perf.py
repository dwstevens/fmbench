"""Performance + resource tracks.

Three measurements:
  throughput()       — median tokens/sec over a few sustained generations
  ttft()             — time-to-first-token via streaming
  sample_resources() — process-CPU sampling during a sustained generation, to show
                       WHERE the work lands (inference daemon + aned), proving the
                       `fm` client itself is idle and the GPU is untouched
  sample_power()     — optional CPU/GPU/ANE power (mW) via `powermetrics` (needs sudo)

The whole point: the model runs on the Apple Neural Engine, so GPU stays ~0 and you
can run this alongside GPU workloads with no contention. The power table makes that
visible; the process table localizes the CPU-side dispatch cost.
"""
from __future__ import annotations

import os
import re
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

# System daemons that do (or coordinate) on-device inference.
DAEMONS = [
    "TGOnDeviceInferenceProviderService",
    "aned",
    "modelcatalogd",
    "GenerativeExperiencesSafetyInferenceProvider",
    "IntelligencePlatformComputeService",
    "fm",
]

LONG_PROMPT = (
    "Write a detailed 1200-word essay on the history of computing, from the abacus "
    "through Babbage, vacuum tubes, transistors, the microprocessor, personal "
    "computers, the internet, and modern AI accelerators. Be specific with names "
    "and dates."
)


def _token_count(text: str) -> int:
    try:
        out = subprocess.run(["fm", "token-count", text], capture_output=True,
                             text=True, timeout=30).stdout
        return int(re.search(r"\d+", out).group())
    except Exception:  # noqa: BLE001
        return len(text.split())  # rough fallback


def throughput(runs: int = 3) -> dict:
    """Median tok/s and latency over `runs` sustained non-streaming generations."""
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        out = subprocess.run(["fm", "respond", "--no-stream", LONG_PROMPT],
                             capture_output=True, text=True, timeout=300).stdout
        elapsed = time.perf_counter() - start
        toks = _token_count(out)
        samples.append({"elapsed": elapsed, "tokens": toks, "tok_s": toks / elapsed})
    return {
        "runs": runs,
        "median_tok_s": statistics.median(s["tok_s"] for s in samples),
        "median_latency_s": statistics.median(s["elapsed"] for s in samples),
        "median_tokens": statistics.median(s["tokens"] for s in samples),
        "samples": samples,
    }


def ttft(runs: int = 3) -> dict:
    """Time to first streamed token (seconds), median over `runs`."""
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        proc = subprocess.Popen(
            ["fm", "respond", "--stream", "Count slowly from 1 to 30."],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        first = None
        assert proc.stdout is not None
        for ch in iter(lambda: proc.stdout.read(1), ""):
            if ch.strip():
                first = time.perf_counter() - start
                break
        proc.kill()
        proc.wait()
        if first is not None:
            times.append(first)
    return {"runs": len(times), "median_ttft_s": statistics.median(times) if times else None,
            "samples": times}


# ----------------------------------------------------------------------------
# Resource sampling
# ----------------------------------------------------------------------------
@dataclass
class ResourceTable:
    rows: list[dict] = field(default_factory=list)   # per-daemon avg/peak CPU%
    samples: int = 0
    duration_s: float = 0.0


def _ps_snapshot() -> dict[str, float]:
    """comm -> %cpu for processes of interest (summed across matching pids)."""
    out = subprocess.run(["ps", "-Ac", "-o", "pid,%cpu,comm"],
                         capture_output=True, text=True).stdout
    agg: dict[str, float] = {}
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            cpu = float(parts[1])
        except ValueError:
            continue
        comm = parts[2].strip()
        for d in DAEMONS:
            if comm == d:
                agg[d] = agg.get(d, 0.0) + cpu
    return agg


def sample_resources(interval: float = 0.5) -> ResourceTable:
    """Sample inference-daemon CPU while a sustained generation runs in background."""
    per: dict[str, list[float]] = {d: [] for d in DAEMONS}
    proc = subprocess.Popen(["fm", "respond", "--no-stream", LONG_PROMPT],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    start = time.perf_counter()
    n = 0
    while proc.poll() is None:
        snap = _ps_snapshot()
        for d in DAEMONS:
            per[d].append(snap.get(d, 0.0))
        n += 1
        time.sleep(interval)
    duration = time.perf_counter() - start
    proc.wait()

    table = ResourceTable(samples=n, duration_s=duration)
    for d in DAEMONS:
        vals = per[d] or [0.0]
        table.rows.append({
            "process": d,
            "avg_cpu": round(sum(vals) / len(vals), 1),
            "peak_cpu": round(max(vals), 1),
            "role": _role(d),
        })
    table.rows.sort(key=lambda r: r["peak_cpu"], reverse=True)
    return table


def _role(d: str) -> str:
    return {
        "TGOnDeviceInferenceProviderService": "on-device inference worker",
        "aned": "Apple Neural Engine daemon (ANE dispatch)",
        "modelcatalogd": "model weight management",
        "GenerativeExperiencesSafetyInferenceProvider": "safety/guardrail inference",
        "IntelligencePlatformComputeService": "compute coordination",
        "fm": "CLI client (should be ~idle)",
    }.get(d, "")


# ----------------------------------------------------------------------------
# Optional power sampling via powermetrics (needs sudo)
# ----------------------------------------------------------------------------
def sudo_cached() -> bool:
    """True if sudo credentials are already cached (no password needed now)."""
    try:
        return subprocess.run(["sudo", "-n", "true"],
                              capture_output=True).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def sample_power(duration_s: float = 20.0, interval_ms: int = 250) -> dict:
    """Sample CPU/GPU/ANE power (mW) via powermetrics while a generation runs.

    On Apple Silicon the CPU/GPU/ANE power lines all live in the ``cpu_power``
    sampler block. Requires cached sudo (call after `sudo -v`); returns
    {available: False, ...} otherwise so the rest of the run is unaffected.
    """
    if not sudo_cached():
        return {"available": False,
                "reason": "sudo not available — run `sudo -v` first, or omit --power"}

    n = max(1, int(duration_s / (interval_ms / 1000)))
    # Write powermetrics output to a file, NOT a pipe: it emits far more than the 64 KB
    # pipe buffer, and draining a pipe only after the load loop would deadlock.
    with tempfile.NamedTemporaryFile("w+", suffix=".pm", delete=False) as tf:
        pm_path = tf.name
    hard_deadline = time.perf_counter() + duration_s + 45  # wall-clock guard
    with open(pm_path, "w") as out_fh:
        pm = subprocess.Popen(
            ["sudo", "-n", "powermetrics", "--samplers", "cpu_power,gpu_power",
             "-i", str(interval_ms), "-n", str(n)],
            stdout=out_fh, stderr=subprocess.DEVNULL)

        # Keep the ANE busy across the sampling window (re-launch if a gen finishes).
        while pm.poll() is None and time.perf_counter() < hard_deadline:
            try:
                subprocess.run(["fm", "respond", "--no-stream", LONG_PROMPT],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=30)
            except Exception:  # noqa: BLE001
                break
        try:
            pm.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pm.kill()

    with open(pm_path) as fh:
        out_text = fh.read()
    try:
        os.unlink(pm_path)
    except OSError:
        pass

    def _series(label: str) -> list[float]:
        return [float(m) for m in re.findall(rf"{label}:\s*([\d.]+)\s*mW", out_text)]

    if not _series("CPU Power") and not _series("GPU Power"):
        return {"available": False,
                "reason": "powermetrics produced no power samples (unexpected output format)"}

    result = {"available": True, "samples": n, "window_s": duration_s}
    for key, label in (("cpu", "CPU Power"), ("gpu", "GPU Power"), ("ane", "ANE Power")):
        vals = _series(label)
        result[key] = {
            "avg_mw": round(statistics.mean(vals), 1) if vals else None,
            "peak_mw": round(max(vals), 1) if vals else None,
            "n": len(vals),
        }
    return result

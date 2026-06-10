"""Invoke the `fm` CLI and parse its structured output."""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


@dataclass
class RunResult:
    ok: bool                       # process exited 0
    elapsed: float                 # wall-clock seconds
    raw: str                       # raw stdout
    stderr: str
    obj: Any | None = None         # parsed JSON, if any
    parse_error: str | None = None
    refused: bool = False          # safety guardrail blocked the request
    extras: dict = field(default_factory=dict)


def _is_refusal(stdout: str, stderr: str) -> bool:
    blob = strip_ansi(stdout + "\n" + stderr).lower()
    return ("guardrail" in blob) or ("safety" in blob and "trigger" in blob)


def _extract_json(text: str) -> Any:
    text = strip_ansi(text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced {...} or [...] span.
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON object found in output")
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in output")


def run_fm(prompt: str, *, schema_path: str | None = None,
           instructions: str | None = None, greedy: bool = True,
           model: str = "system", use_case: str | None = None,
           guardrails: str | None = None, timeout: int = 120) -> RunResult:
    """Run `fm respond` (non-streaming) and parse the JSON result."""
    cmd = ["fm", "respond", "--no-stream", "--model", model]
    if greedy:
        cmd.append("--greedy")
    if instructions:
        cmd += ["--instructions", instructions]
    if schema_path:
        cmd += ["--schema", schema_path]
    if use_case:
        cmd += ["--use-case", use_case]
    if guardrails:
        cmd += ["--guardrails", guardrails]
    cmd.append(prompt)

    start = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return RunResult(ok=False, elapsed=timeout, raw="", stderr="timeout",
                         parse_error="timeout")
    elapsed = time.perf_counter() - start

    res = RunResult(ok=(proc.returncode == 0), elapsed=elapsed,
                    raw=proc.stdout, stderr=proc.stderr,
                    refused=_is_refusal(proc.stdout, proc.stderr))
    if schema_path and not res.refused:  # only schema-constrained runs are JSON
        try:
            res.obj = _extract_json(proc.stdout)
        except Exception as exc:  # noqa: BLE001 - record any parse failure
            res.parse_error = f"{type(exc).__name__}: {exc}"
    return res

# CLAUDE.md

This project's agent guidance lives in **[AGENTS.md](AGENTS.md)** — read it first. It is
the single source of truth for build/run commands, architecture, the `fm` schema dialect,
and conventions. This file exists so Claude Code finds the guidance; it intentionally just
points at AGENTS.md to avoid drift.

Quick reminders (see AGENTS.md for the full version):

- Run with `uv` only — never bare `pip` or a manually activated venv.
  - `uv run python run.py --quick` for a fast end-to-end smoke test.
  - `uv run python -m py_compile run.py fmbench/*.py` to syntax-check.
- Standard library only; no third-party Python dependencies.
- All schemas go through the builders in `fmbench/schemas.py` — the `fm` dialect requires
  an `x-order` array on every object, and supports `enum`/`const` even though the CLI
  builder doesn't expose them.
- `cases/*.jsonl` expected values must be objectively correct; known model limits are
  characterized in the `failure_modes` suite, not tuned away.
- `results/` and `schemas/` are generated and gitignored — don't commit them.

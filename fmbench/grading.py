"""Deterministic graders. No LLM judge — every check is code.

Scoring vocabulary returned per case:
  structural_valid : bool   — output parsed and satisfies the schema's hard rules
  score            : float  — suite-specific 0..1 primary score
  detail           : dict   — human-readable breakdown for the report
"""
from __future__ import annotations

from typing import Any


# ----------------------------------------------------------------------------
# Value comparison / normalization
# ----------------------------------------------------------------------------
def _norm(v: Any) -> Any:
    if isinstance(v, str):
        return v.strip().lower()
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return v


def values_match(expected: Any, actual: Any, *, num_tol: float = 0.01) -> bool:
    if isinstance(expected, (int, float)) and not isinstance(expected, bool) \
            and isinstance(actual, (int, float)) and not isinstance(actual, bool):
        return abs(float(expected) - float(actual)) <= num_tol
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return False
        # order-insensitive for scalar lists
        ea = [_norm(x) for x in expected]
        aa = [_norm(x) for x in actual]
        try:
            return sorted(ea) == sorted(aa)
        except TypeError:
            return ea == aa
    return _norm(expected) == _norm(actual)


def _get_path(obj: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted path like 'customer.email' or 'items.0.name'."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return False, None
        elif isinstance(cur, dict):
            if part not in cur:
                return False, None
            cur = cur[part]
        else:
            return False, None
    return True, cur


# ----------------------------------------------------------------------------
# Structural validation against the fm-dialect schema (hard-rule re-check)
# ----------------------------------------------------------------------------
_TYPECHECK = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _resolve(schema: dict, root: dict) -> dict:
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return root.get("$defs", {}).get(name, {})
    return schema


def validate(obj: Any, schema: dict, root: dict | None = None) -> list[str]:
    """Return a list of violation strings ([] == valid). Checks the constraints
    fm claims to enforce: required keys, types, enum membership, const equality."""
    root = root or schema
    errs: list[str] = []
    schema = _resolve(schema, root)

    if "anyOf" in schema:
        branch_errs = [validate(obj, b, root) for b in schema["anyOf"]]
        if not any(len(e) == 0 for e in branch_errs):
            errs.append("matched no anyOf branch")
        return errs

    typ = schema.get("type")
    if typ and typ in _TYPECHECK and not _TYPECHECK[typ](obj):
        return [f"expected {typ}, got {type(obj).__name__}"]

    if typ == "object":
        for key in schema.get("required", []):
            if key not in obj:
                errs.append(f"missing required '{key}'")
        for key, val in obj.items():
            prop = schema.get("properties", {}).get(key)
            if prop is None:
                if not schema.get("additionalProperties", True):
                    errs.append(f"unexpected property '{key}'")
                continue
            errs += validate(val, prop, root)
    elif typ == "array":
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(obj):
                errs += [f"[{i}] {e}" for e in validate(item, item_schema, root)]

    if "const" in schema and obj != schema["const"]:
        errs.append(f"const violation: {obj!r} != {schema['const']!r}")
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"enum violation: {obj!r} not in {schema['enum']}")
    return errs


# ----------------------------------------------------------------------------
# Suite graders
# ----------------------------------------------------------------------------
def grade_routing(obj: Any, schema: dict, expect: dict) -> dict:
    """Tool selection + argument extraction."""
    want_tool = expect["tool"]
    got_tool = obj.get("tool") if isinstance(obj, dict) else None
    # Accept the const value, or the schema title if the discriminator drifted.
    tool_ok = _norm(got_tool) == _norm(want_tool)
    drifted = (not tool_ok) and _norm(got_tool) == _norm(want_tool.replace("_", ""))

    args = expect.get("args", {})
    matched, total = 0, len(args)
    arg_detail = {}
    for k, v in args.items():
        ok_present, actual = _get_path(obj, k)
        hit = ok_present and values_match(v, actual)
        arg_detail[k] = {"want": v, "got": actual if ok_present else None, "ok": hit}
        matched += int(hit)
    arg_score = (matched / total) if total else 1.0

    # Optional args: only checked if the model emitted them.
    for k, v in expect.get("args_optional", {}).items():
        ok_present, actual = _get_path(obj, k)
        if ok_present:
            arg_detail[k] = {"want": v, "got": actual,
                             "ok": values_match(v, actual), "optional": True}

    score = (0.5 if (tool_ok or drifted) else 0.0) + 0.5 * arg_score
    return {
        "score": score,
        "tool_ok": tool_ok,
        "tool_drifted": drifted,
        "got_tool": got_tool,
        "arg_score": arg_score,
        "args_matched": matched,
        "args_total": total,
        "detail": arg_detail,
    }


def grade_extraction(obj: Any, schema: dict, expect: dict) -> dict:
    """Per-field accuracy over an expected {dotpath: value} map."""
    fields = expect["fields"]
    matched, detail = 0, {}
    for path, want in fields.items():
        present, actual = _get_path(obj, path)
        hit = present and values_match(want, actual)
        detail[path] = {"want": want, "got": actual if present else None, "ok": hit}
        matched += int(hit)
    total = len(fields)
    return {
        "score": (matched / total) if total else 1.0,
        "fields_matched": matched,
        "fields_total": total,
        "detail": detail,
    }


def grade_constraints(obj: Any, schema: dict, expect: dict) -> dict:
    """Hard-constraint compliance: every enum/const in the schema must hold,
    and adversarial 'push out of set' prompts must still land in-set."""
    violations = validate(obj, schema)
    compliant = len(violations) == 0
    # Optional: correctness of a specific field when the prompt has a right answer.
    correctness = None
    if "expected_field" in expect:
        path, want = expect["expected_field"]["path"], expect["expected_field"]["value"]
        present, actual = _get_path(obj, path)
        correctness = present and values_match(want, actual)
    return {
        "score": 1.0 if compliant else 0.0,
        "compliant": compliant,
        "violations": violations,
        "correctness": correctness,
    }


def grade_failure(obj: Any, schema: dict, expect: dict) -> dict:
    """Characterization, not pass/fail. Two kinds:
      - 'arithmetic': record whether the model got a computed value right.
      - 'no_tool'   : record whether it used the escape hatch vs confabulated.
    """
    kind = expect["kind"]
    out = {"kind": kind}
    if kind == "arithmetic":
        path, correct = expect["field"], expect["correct"]
        present, actual = _get_path(obj, path)
        out["got"] = actual if present else None
        out["correct_value"] = correct
        out["model_correct"] = present and values_match(correct, actual)
        out["score"] = 1.0 if out["model_correct"] else 0.0
    elif kind == "no_tool":
        escape = expect["escape_tool"]
        got = obj.get("tool") if isinstance(obj, dict) else None
        out["got_tool"] = got
        out["used_escape"] = _norm(got) == _norm(escape)
        out["score"] = 1.0 if out["used_escape"] else 0.0
    else:
        out["score"] = 0.0
    return out


GRADERS = {
    "routing": grade_routing,
    "extraction": grade_extraction,
    "constraints": grade_constraints,
    "failure_modes": grade_failure,
}

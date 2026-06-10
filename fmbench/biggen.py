"""Generate escalating 'big-args' tool schemas — large, deeply-nested argument objects.

Stress-tests whether Apple FM's structural fidelity holds as a *single* tool call grows
from a handful of fields to ~100 leaves across mixed nesting: flat scalars, nested
objects, a deep chain, an array of objects, enums, and a flat filler block. Each level
is bigger than the last, so the suite yields a size → (validity, field-accuracy, latency)
curve and shows where, if anywhere, the model starts dropping or misplacing fields.

`generate(n)` returns (schema_dict, prompt, expected_map) where `expected_map` is
{dotted.path: value} for every graded leaf. Values are deterministic and stated
explicitly in the prompt, so this measures structural placement at scale, not knowledge.
"""
from __future__ import annotations

from fmbench.schemas import obj, p_bool, p_int, p_num, p_str, p_arr, ref

LEVELS = [5, 10, 25, 50, 75, 100]      # graded leaf-field counts, each bigger than the last
PRI = ["low", "medium", "high", "critical"]
ST = ["draft", "active", "paused", "archived"]


def _scalar(kind: str, i: int):
    """Return (property_schema, value) for a leaf of the given kind, varied by index i."""
    if kind == "pri":
        return p_str(enum=PRI), PRI[i % len(PRI)]
    if kind == "st":
        return p_str(enum=ST), ST[i % len(ST)]
    if kind == "str":
        return p_str(), f"val-{i}"
    if kind == "int":
        return p_int(), (i * 7) % 100
    if kind == "num":
        return p_num(), float(i % 40) + 0.5
    if kind == "bool":
        return p_bool(), (i % 2 == 0)
    raise ValueError(kind)


def generate(n: int) -> tuple[dict, str, dict]:
    exp: dict = {}
    lines: list[str] = []
    defs: dict = {}
    idx = [0]      # global value-variety counter
    left = [n]     # remaining leaves to place

    def add(props: dict, prefix: str, key: str, kind: str) -> bool:
        if left[0] <= 0:
            return False
        sch, val = _scalar(kind, idx[0]); idx[0] += 1
        props[key] = sch
        path = f"{prefix}.{key}" if prefix else key
        exp[path] = val
        shown = str(val).lower() if isinstance(val, bool) else val
        lines.append(f"- {path} = {shown}")
        left[0] -= 1
        return True

    top: dict = {"tool": p_str(const="submit_record")}  # discriminator, not counted

    # 1. flat top-level scalars
    add(top, "", "record_id", "str")
    add(top, "", "title", "str")

    # 2. nested meta object (enums / bool / int)
    meta: dict = {}
    for k, kind in [("priority", "pri"), ("status", "st"), ("urgent", "bool"), ("version", "int")]:
        add(meta, "meta", k, kind)
    if meta:
        defs["Meta"] = obj("Meta", meta); top["meta"] = ref("Meta")

    # 3. profile with two sub-objects (depth 3)
    contact: dict = {}
    for k in ("email", "phone"):
        add(contact, "profile.contact", k, "str")
    address: dict = {}
    for k in ("city", "country", "postalCode"):
        add(address, "profile.address", k, "str")
    prof: dict = {}
    if contact:
        defs["Contact"] = obj("Contact", contact); prof["contact"] = ref("Contact")
    if address:
        defs["Address"] = obj("Address", address); prof["address"] = ref("Address")
    if prof:
        defs["Profile"] = obj("Profile", prof); top["profile"] = ref("Profile")

    # 4. deep chain (depth 4) — only when there's budget for it
    if left[0] >= 2:
        l3: dict = {}
        add(l3, "deep.outer.middle.inner", "value", "str")
        add(l3, "deep.outer.middle.inner", "count", "int")
        defs["Inner"] = obj("Inner", l3)
        defs["Middle"] = obj("Middle", {"inner": ref("Inner")})
        defs["Outer"] = obj("Outer", {"middle": ref("Middle")})
        defs["Deep"] = obj("Deep", {"outer": ref("Outer")})
        top["deep"] = ref("Deep")

    # 5. array of objects — sections[], 3 leaves each, fixed item schema
    if left[0] >= 3:
        n_items = min(left[0] // 3, 12)
        defs["Section"] = obj("Section", {"name": p_str(), "score": p_int(), "ok": p_bool()})
        top["sections"] = p_arr(ref("Section"))
        for s in range(n_items):
            for key, kind in [("name", "str"), ("score", "int"), ("ok", "bool")]:
                if left[0] <= 0:
                    break
                _, val = _scalar(kind, idx[0]); idx[0] += 1
                path = f"sections.{s}.{key}"
                exp[path] = val
                shown = str(val).lower() if isinstance(val, bool) else val
                lines.append(f"- {path} = {shown}")
                left[0] -= 1

    # 6. flat attributes filler — exhaust the remaining budget exactly
    if left[0] > 0:
        attrs: dict = {}
        kinds = ["str", "int", "num", "bool"]
        k = 0
        while left[0] > 0:
            add(attrs, "attributes", f"attr_{k}", kinds[k % len(kinds)])
            k += 1
        defs["Attributes"] = obj("Attributes", attrs); top["attributes"] = ref("Attributes")

    schema = obj("submit_record", top, defs=defs)
    prompt = ("Call the `submit_record` tool, filling EVERY field with exactly the value "
              "given below. Set `tool` to \"submit_record\".\n\n" + "\n".join(lines))
    return schema, prompt, exp


def cases() -> list[dict]:
    """One generated case per level, shaped for run.py / the extraction grader."""
    out = []
    for n in LEVELS:
        schema, prompt, expected = generate(n)
        out.append({
            "id": f"big-args-{n:03d}",
            "suite": "big_args",
            "schema_obj": schema,
            "prompt": prompt,
            "n_leaves": len(expected),
            "expect": {"fields": expected},
        })
    return out

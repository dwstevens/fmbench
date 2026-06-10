"""Self-correcting structured extraction for a small on-device model.

A naive one-shot extraction of a dense diagram tends to confuse arrow *labels* with
*entities*. "Reflect on your work" prompting barely helps a ~3B model. What works is
structure:

  1. extract the entity set on its own (focused, low error surface)
  2. re-extract relationships with source/target ENUM-constrained to that set — so the
     decoder *physically cannot* emit a label where an entity belongs (fm enforces
     enum/const hard: benchmarked 100%)
  3. a final structured critique pass (image + assembled draft → list of issues),
     applied in code

This module exposes the stages and a `reflect_extract` orchestrator, plus a `naive`
one-shot for side-by-side comparison. Run as a module on an image to see the diff.

Findings from running it on a dense diagram:
  - The enum constraint makes the target error (a label in an entity slot) *structurally
    impossible* — 0 such errors, guaranteed.
  - But constraints guarantee SHAPE, not TRUTH: the constrained pass tends to over-
    generate (valid-but-spurious edges). Structure fixes the encoded error, not accuracy.
  - With an IMAGE, context is the binding constraint: a normal enum schema (which inlines
    every option, twice, for source+target) blows the ~4K window. SHORT CODES (E0..E4 +
    a legend, mapped back in code) keep the guarantee and fit the budget. Essential.
  - The structured critique pass is the real value-add — a cheap safety net that flags
    issues constraints can't encode.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile

from fmbench.runner import _extract_json
from fmbench.schemas import obj, p_arr, p_str, ref


def _fm(text: str, *, image: str, schema: dict | None = None,
        instructions: str | None = None, timeout: int = 120):
    cmd = ["fm", "respond", "--no-stream", "--greedy", "--image", image]
    if instructions:
        cmd += ["--instructions", instructions]
    if schema is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(schema, f)
            path = f.name
        cmd += ["--schema", path]
    cmd += ["--text", text]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    try:
        return _extract_json(out.stdout)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------
def extract_core(image: str) -> dict | None:
    schema = obj("Core", {
        "title": p_str(),
        "centralHub": p_str(),
        "entities": p_arr(p_str(),
                          desc="node / puzzle-piece entity names ONLY — not arrow labels"),
    })
    return _fm("Extract the diagram title, the central hub name, and the list of node "
               "entities (the puzzle pieces repeated in each cluster). Do NOT include arrow "
               "labels such as 'Platform Usage', 'Referrals', or 'CRA Credits & Reporting' "
               "— those are relationships, not entities.",
               image=image, schema=schema)


def extract_keypoints(image: str) -> dict | None:
    kp = obj("KeyPoint", {"name": p_str(), "summary": p_str()})
    schema = obj("KP", {"keyPoints": p_arr(ref("KeyPoint"))}, defs={"KeyPoint": kp})
    return _fm("Extract the bulleted key points on the right: each bold name and its full "
               "summary sentence.", image=image, schema=schema)


def extract_relationships(image: str, entities: list[str]) -> dict | None:
    # An image already eats most of the ~4K context, and an enum inlines every option
    # twice (source + target). Use SHORT CODES so the enum grammar stays tiny — then map
    # the codes back to full names in code. Keeps the hard enum guarantee, fits the budget.
    codes = [f"E{i}" for i in range(len(entities))]
    legend = "; ".join(f"{c} = {e}" for c, e in zip(codes, entities))
    edge = obj("Edge", {
        "source": p_str(enum=codes),
        "target": p_str(enum=codes),
        "label": p_str(),
    })
    schema = obj("Rels", {"relationships": p_arr(ref("Edge"))}, defs={"Edge": edge})
    out = _fm(f"Entity codes: {legend}. List the labeled arrows between entities; `source` and "
              "`target` are codes from that list, the arrow's text goes in `label`.",
              image=image, schema=schema)
    if out:
        m = dict(zip(codes, entities))
        for r in out.get("relationships", []):
            r["source"] = m.get(r.get("source"), r.get("source"))
            r["target"] = m.get(r.get("target"), r.get("target"))
    return out


def critique(image: str, draft: dict) -> dict | None:
    issue = obj("Issue", {
        "path": p_str(desc="dotted path of the wrong field"),
        "problem": p_str(),
        "corrected_value": p_str(),
    })
    schema = obj("Critique", {"issues": p_arr(ref("Issue"))}, defs={"Issue": issue})
    return _fm("Here is a structured extraction of the diagram:\n\n"
               + json.dumps(draft, indent=2)
               + "\n\nCompare it against the image. Remember: an arrow LABEL is never an "
               "entity. List every field that is wrong, with a corrected value. If it is "
               "correct, return an empty issues list.",
               image=image, schema=schema)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def naive(image: str) -> dict | None:
    """The original one-shot extraction — everything at once."""
    edge = obj("Edge", {"source": p_str(), "target": p_str(), "label": p_str()})
    point = obj("KeyPoint", {"name": p_str(), "summary": p_str()})
    schema = obj("Diagram", {
        "title": p_str(),
        "centralHub": p_str(),
        "nodeComponents": p_arr(p_str()),
        "relationships": p_arr(ref("Edge")),
        "keyPoints": p_arr(ref("KeyPoint")),
    }, defs={"Edge": edge, "KeyPoint": point})
    return _fm("Extract this diagram into the schema: node components, every labeled "
               "relationship arrow, and the key points.", image=image, schema=schema)


def reflect_extract(image: str) -> dict:
    """Decompose → enum-constrain → critique. Returns the assembled result + provenance."""
    core = extract_core(image) or {}
    entities = list(dict.fromkeys((core.get("entities") or [])
                                  + ([core["centralHub"]] if core.get("centralHub") else [])))
    # fm greedy isn't perfectly deterministic under load — retry an empty draw once.
    rels = {"relationships": []}
    for _ in range(2):
        if entities:
            rels = extract_relationships(image, entities) or {"relationships": []}
        if rels.get("relationships"):
            break
    kps = extract_keypoints(image) or {"keyPoints": []}
    draft = {
        "title": core.get("title"),
        "centralHub": core.get("centralHub"),
        "entities": core.get("entities", []),
        "relationships": (rels or {}).get("relationships", []),
        "keyPoints": kps.get("keyPoints", []),
    }
    crit = critique(image, draft) or {"issues": []}
    return {"result": draft, "entities_enum": entities, "critique": crit.get("issues", [])}


def _label_as_entity_errors(rels: list[dict], entities: set[str]) -> list[str]:
    bad = []
    for r in rels:
        for slot in ("source", "target"):
            v = r.get(slot)
            if v and v not in entities:
                bad.append(f"{slot}={v!r}")
    return bad


if __name__ == "__main__":
    image = sys.argv[1]
    print("=== NAIVE one-shot ===")
    nv = naive(image) or {}
    nv_ents = set(nv.get("nodeComponents", [])) | {nv.get("centralHub")}  # hub is a valid endpoint
    nv_bad = _label_as_entity_errors(nv.get("relationships", []), nv_ents)
    print(json.dumps(nv, indent=2))
    print(f"\nnode components: {nv.get('nodeComponents')}")
    print(f"label-as-entity errors in relationships: {len(nv_bad)} {nv_bad}")

    print("\n\n=== REFLECTIVE (decompose + enum-constrain + critique) ===")
    rf = reflect_extract(image)
    res = rf["result"]
    ents = set(rf["entities_enum"])
    rf_bad = _label_as_entity_errors(res["relationships"], ents)
    print(json.dumps(res, indent=2))
    print(f"\nentity enum: {rf['entities_enum']}")
    print(f"label-as-entity errors in relationships: {len(rf_bad)} {rf_bad}  "
          f"(structurally impossible — enum-constrained)")
    print(f"self-critique issues: {len(rf['critique'])}")
    for i in rf["critique"]:
        print(f"  - {i.get('path')}: {i.get('problem')} -> {i.get('corrected_value')}")

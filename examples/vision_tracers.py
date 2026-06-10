"""Vision tracers — probe the coarse-to-fine / crop-and-reprocess strategy on-device.

Runs a battery of tracers on a single diagram image and compares whole-image extraction
against region-cropped extraction, scoring each against ground truth:

  A. cluster edges  — full image          vs  cropped single cluster
  B. key points     — full image          vs  cropped right-side bullets
  C. context-fit    — enum schema on full image (blows ~4K) vs on a crop (fits)
  D. localization    — can fm produce usable bounding boxes? (coarse only)

Crops are deterministic fractions of the image (no model localization needed — we showed
fm's boxes are too loose for tight crops). Saved to /tmp/tracers/ for eyeballing.

Run:  uv run --with pillow python examples/vision_tracers.py <image>
"""
import json
import os
import sys
import time

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fmbench.reflect import _fm                                    # noqa: E402
from fmbench.schemas import obj, p_arr, p_num, p_str, ref          # noqa: E402

OUT = "/tmp/tracers"
os.makedirs(OUT, exist_ok=True)

# Deterministic crop regions as (left, top, right, bottom) fractions.
CROPS = {
    "cluster": (0.00, 0.15, 0.26, 0.58),   # the top-left puzzle cluster
    "bullets": (0.61, 0.16, 1.00, 1.00),   # the three right-side key points
}

CLUSTER_ENTS = ["Local SMBs", "Local Nonprofits", "Giv Local (Community Node)", "Community Banks"]

# Ground truth for the TOP-LEFT cluster (its right-side label is "Referrals"; other
# clusters substitute "CRA Credits & Reporting"). Matching is direction-lenient — the
# arrows are genuinely hard to read, so we score the entity pair + label, not direction.
CANON_EDGES = [
    ("Local SMBs", "Local Nonprofits", "20% Funding"),
    ("Local SMBs", "Giv Local (Community Node)", "Platform Usage"),
    ("Giv Local (Community Node)", "Local Nonprofits", "Referrals"),
    ("Community Banks", "Giv Local (Community Node)", "SMB Referrals"),
]
CANON_KEYPOINTS = ["national nonprofit network", "community bank federation", "scaled result"]


def crop(name: str, box, src: str) -> str:
    im = Image.open(src).convert("RGB")
    w, h = im.size
    l, t, r, b = box
    path = f"{OUT}/{name}.png"
    im.crop((int(l * w), int(t * h), int(r * w), int(b * h))).save(path)
    return path


def _n(s):
    return (s or "").lower().strip()


def _label_match(a, b):
    a, b = _n(a), _n(b)
    if a in b or b in a:
        return True
    aw, bw = set(a.replace("&", " ").split()), set(b.replace("&", " ").split())
    return len(aw & bw) >= 1 and (len(aw & bw) / max(len(aw), 1)) >= 0.5


def score_edges(rels):
    # direction-lenient: match on the unordered entity pair + label
    got = [(frozenset({_n(r.get("source")), _n(r.get("target"))}), r.get("label"))
           for r in (rels or [])]
    correct = 0
    for s, t, lbl in CANON_EDGES:
        pair = frozenset({_n(s), _n(t)})
        if any(gp == pair and _label_match(lbl, gl) for gp, gl in got):
            correct += 1
    spurious = len(got) - correct
    return correct, len(CANON_EDGES), max(spurious, 0)


def score_keypoints(kps):
    names = [_n(k.get("name")) for k in (kps or [])]
    return sum(1 for c in CANON_KEYPOINTS if any(c in n or n in c for n in names)), len(CANON_KEYPOINTS)


def edge_schema(ents):
    edge = obj("Edge", {"source": p_str(enum=ents), "target": p_str(enum=ents), "label": p_str()})
    return obj("Rels", {"relationships": p_arr(ref("Edge"))}, defs={"Edge": edge})


def kp_schema():
    kp = obj("KeyPoint", {"name": p_str(), "summary": p_str()})
    return obj("KP", {"keyPoints": p_arr(ref("KeyPoint"))}, defs={"KeyPoint": kp})


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


def extract_edges(image):
    return _fm("List the labeled arrows between the entities; source and target are entities, "
               "the arrow text goes in label.", image=image, schema=edge_schema(CLUSTER_ENTS))


def extract_kps(image):
    return _fm("Extract the bulleted key points: each bold name and its full summary.",
               image=image, schema=kp_schema())


def main():
    src = sys.argv[1]
    paths = {name: crop(name, box, src) for name, box in CROPS.items()}
    print(f"crops saved: {paths}\n")

    print("== TRACER A: cluster edges (full image vs cropped cluster) ==")
    for label, img in [("full-image", src), ("crop:cluster", paths["cluster"])]:
        out, dt = timed(lambda i=img: extract_edges(i))
        rels = (out or {}).get("relationships", [])
        c, tot, sp = score_edges(rels)
        print(f"  {label:14} edges {c}/{tot} correct, {sp} spurious   {dt:4.1f}s")

    print("\n== TRACER B: key points (full image vs cropped bullets) ==")
    for label, img in [("full-image", src), ("crop:bullets", paths["bullets"])]:
        out, dt = timed(lambda i=img: extract_kps(i))
        kps = (out or {}).get("keyPoints", [])
        c, tot = score_keypoints(kps)
        verbatim = sum(len(_n(k.get("summary", ""))) > 40 for k in kps)
        print(f"  {label:14} keypoints {c}/{tot}, {verbatim} with full summary   {dt:4.1f}s")

    print("\n== TRACER C: context fit (heavy enum schema: full vs crop) ==")
    heavy_ents = CLUSTER_ENTS + ["Giv Local Platform - National Hub", "CRA Credits & Reporting",
                                 "Platform Usage", "Referrals", "SMB Referrals", "20% Funding"]
    for label, img in [("full-image", src), ("crop:cluster", paths["cluster"])]:
        out = _fm("List arrows; source/target from the entities.",
                  image=img, schema=edge_schema(heavy_ents))
        status = "OK" if out else "FAILED (context/parse)"
        n = len((out or {}).get("relationships", []))
        print(f"  {label:14} heavy-enum schema -> {status} ({n} edges)")

    print("\n== TRACER D: localization (fm bounding boxes — coarse only) ==")
    box = obj("Box", {"region": p_str(), "x": p_num(), "y": p_num(), "w": p_num(), "h": p_num()})
    bs = _fm("Normalized 0-1 bounding boxes (origin top-left) for: the title, the right-side "
             "bullet list, the central hub.", image=src,
             schema=obj("Boxes", {"regions": p_arr(ref("Box"))}, defs={"Box": box}))
    for r in (bs or {}).get("regions", []):
        print(f"  {r.get('region'):26} x={r.get('x')} y={r.get('y')} w={r.get('w')} h={r.get('h')}")


if __name__ == "__main__":
    main()

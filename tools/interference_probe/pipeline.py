r"""推奨案の流れの試作: 境界箱で絞る → General Fuse 1回で重なり・接触 → 触れていない近い組だけ最短距離。

    .\.venv\Scripts\python.exe -m tools.interference_probe.pipeline <STEP または文書.json> [--clearance 0.2]
    .\.venv\Scripts\python.exe -m tools.interference_probe.pipeline --compare output\interference_probe\pc_case.json <STEP>

分類(試作の定義):
- overlap: GF の像が立体を共有し、その厚さ 2V/A が THICKNESS_TOL を超える
- contact: 面・辺・点を共有する、または立体を共有するが厚さが THICKNESS_TOL 以下(形状の許容差の範囲の薄片)
- near:    触れておらず、最短距離 < clearance - GAP_TOL
- clear:   それ以外
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from itertools import combinations
from pathlib import Path

from . import methods as M

THICKNESS_TOL = 1e-3   # mm。PC ケースの薄片は 7.2e-6 以下、実際の食い込みは 0.79 以上(試作の仮置き)
GAP_TOL = 1e-6         # mm。0.2 で設計した隙間は 0.19999999999908〜0.19999999999998863 と測れる


def classify(shapes, clearance, names=None, parallel=True):
    t0 = time.perf_counter()
    boxes = [M.bbox(s, optimal=False) for s in shapes]
    cand = [(i, j) for i, j in combinations(range(len(shapes)), 2)
            if M.bbox_gap(boxes[i], boxes[j]) <= clearance]
    t_broad = time.perf_counter() - t0
    involved = sorted({k for p in cand for k in p})
    local = {k: n for n, k in enumerate(involved)}
    rel = {}
    t_gf = 0.0
    gf_warn = None
    if cand:
        g = M.gf_relations([shapes[k] for k in involved], parallel=parallel,
                           pairs=[(local[i], local[j]) for i, j in cand])
        t_gf = g["seconds"]
        gf_warn = g["warnings"]
        if g["errors"]:
            raise RuntimeError("General Fuse がエラー(試作では打ち切る。製品では組ごとに落とす)")
        rel = {(involved[a], involved[b]): r for (a, b), r in g["relations"].items()}
    t1 = time.perf_counter()
    out = []
    n_dist = 0
    for (i, j) in cand:
        r = dict(rel[(i, j)])
        if r["kind"] == "overlap" and r["thickness"] > THICKNESS_TOL:
            cls = "overlap"
        elif r["kind"] != "none":
            cls = "contact"
        else:
            d = M.dist(shapes[i], shapes[j], parallel=parallel)
            n_dist += 1
            r["dist"] = d["distance"]
            cls = "near" if d["distance"] < clearance - GAP_TOL else "clear"
        r["class"] = cls
        r["a"], r["b"] = (names[i], names[j]) if names else (i, j)
        out.append(r)
    t_dist = time.perf_counter() - t1
    return dict(bodies=len(shapes), cand=len(cand), broad_s=t_broad, gf_s=t_gf, gf_warn=gf_warn,
                dist_pairs=n_dist, dist_s=t_dist, total_s=time.perf_counter() - t0, pairs=out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--clearance", type=float, default=0.2)
    ap.add_argument("--compare", help="real_docs.py の JSON(厳密経路の分類)と突き合わせる")
    ap.add_argument("--serial", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.source)
    if path.suffix.lower() in (".step", ".stp"):
        from .real_docs import load_step
        solids, _ = load_step(path)
    else:
        from .real_docs import load
        doc, _ = load(path)
        solids = [b for b in doc.bodies if b.kind == "solid"]
    shapes, names = [s.shape for s in solids], [s.name for s in solids]
    res = classify(shapes, args.clearance, names, parallel=not args.serial)
    counts = {}
    for r in res["pairs"]:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in res.items() if k != "pairs"},
                     ensure_ascii=False), counts)
    if args.compare:
        ref = {(p["a"], p["b"]): p for p in json.loads(Path(args.compare).read_text(encoding="utf-8"))[0]["pairs"]}
        for r in res["pairs"]:
            e = ref.get((r["a"], r["b"]))
            exact = e["class"] if e else "clear"
            # 厳密経路(体積の相対しきい値)の overlap のうち、厚さで contact に回したものは区別して数える
            if exact != r["class"]:
                print(f"  diff {r['a']}-{r['b']}: exact={exact} pipeline={r['class']} "
                      f"{ {k: v for k, v in r.items() if k not in ('a', 'b', 'class')} }")
    return 0


if __name__ == "__main__":
    sys.exit(main())

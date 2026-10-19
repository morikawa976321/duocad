r"""推奨案の流れの試作: 境界箱で絞る → 2体 GF(または全体1回)で重なり・接触 → 触れていない近い組だけ最短距離。

    .\.venv\Scripts\python.exe -m tools.interference_probe.pipeline <STEP または文書.json> [--clearance 0.2] [--whole]
    .\.venv\Scripts\python.exe -m tools.interference_probe.pipeline <STEP> --compare output\interference_probe\pc_case.json

組ごとに2つの欄を返す(相談役の指摘で、幾何の関係と隙間の要求を分けた):
- relation(幾何の関係): overlap / tiny_overlap / contact / separated / unknown
  - overlap: GF の像が立体を共有する(下の tiny_overlap を除く)
  - tiny_overlap: 共有する立体の厚さの目安 2V/A と有向境界箱の最小辺が、どちらも THICKNESS_TOL 以下(平らな薄片)
  - contact: 面・辺・点を共有する(GF の許容差の範囲で触れている)
  - separated: 触れていない
  - unknown: GF がエラー
- clearance(clearance > 0 のときだけ): short / at / ok
  - 触れている組(overlap / tiny_overlap / contact)は short
  - 触れていない組は最短距離 d で、d < c - GAP_TOL を short、|d - c| <= GAP_TOL を at(要求値ちょうど)、それ以外 ok
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from itertools import combinations
from pathlib import Path

from . import methods as M

THICKNESS_TOL = 1e-3   # mm。PC ケースの薄片は 2V/A・有向箱の最小辺とも 8.3e-6 以下、実際の食い込みは 0.79 / 2.3 以上(仮置き)
GAP_TOL = 1e-6         # mm。0.2 で設計した隙間は 0.19999999999908〜0.19999999999998863 と測れる(仮置き)


def relation_of(r) -> str:
    """GF の読み取り結果から relation。知らない kind を separated へ落とさない。"""
    kind = r["kind"]
    if kind == "overlap":
        # 領域ごとの最大(どれか1つでも厚ければ overlap)。thickness / obb_min は overlap_measures が領域の最大を返す
        thin = r["thickness"] <= THICKNESS_TOL and r["obb_min"] <= THICKNESS_TOL
        return "tiny_overlap" if thin else "overlap"
    if kind in ("face", "edge", "vertex"):
        return "contact"
    if kind == "none":
        return "separated"
    if kind == "unknown":
        return "unknown"
    raise ValueError(f"知らない kind: {kind!r}")


def classify(shapes, clearance, names=None, parallel=True, whole=False):
    t0 = time.perf_counter()
    shapes = [M.BRepBuilderAPI_Copy(s, True, False).Shape() for s in shapes]   # 文書の形状に触れない
    t_copy = time.perf_counter() - t0
    boxes = [M.bbox(s, optimal=False) for s in shapes]
    # 候補の幅は比較帯まで含める(clearance < d <= clearance + GAP_TOL の「at」を落とさない)
    reach = clearance + GAP_TOL if clearance > 0 else 0.0
    cand = [(i, j) for i, j in combinations(range(len(shapes)), 2)
            if M.bbox_gap(boxes[i], boxes[j]) <= reach]
    # 箱が触れている組だけ GF に回す。clearance だけで候補になった組は最短距離だけでよい。
    touching = [p for p in cand if M.bbox_gap(boxes[p[0]], boxes[p[1]]) <= 0.0]
    outside = len(shapes) * (len(shapes) - 1) // 2 - len(cand)   # 箱の段で separated(clearance なら ok)と決まった組
    t_broad = time.perf_counter() - t0
    rel = {}
    t1 = time.perf_counter()
    if whole and touching:
        involved = sorted({k for p in touching for k in p})
        local = {k: n for n, k in enumerate(involved)}
        g = M.gf_relations([shapes[k] for k in involved], parallel=parallel,
                           pairs=[(local[i], local[j]) for i, j in touching])
        if g["errors"]:
            raise RuntimeError("全体1回の GF がエラー")
        rel = {(involved[a], involved[b]): r for (a, b), r in g["relations"].items()}
    else:
        for i, j in touching:
            g = M.gf_relations([shapes[i], shapes[j]], parallel=parallel)
            rel[(i, j)] = g["relations"][(0, 1)] if not g["errors"] else dict(kind="unknown")
    t_gf = time.perf_counter() - t1
    t2 = time.perf_counter()
    out, n_dist = [], 0
    for i, j in cand:
        r = dict(rel.get((i, j), dict(kind="none")))
        r["relation"] = relation_of(r)
        if clearance > 0:
            if r["relation"] in ("overlap", "tiny_overlap", "contact"):
                r["clearance"] = "short"
            else:  # separated と unknown は最短距離で隙間だけでも決める
                d = M.dist(shapes[i], shapes[j], parallel=parallel)
                n_dist += 1
                r["dist"] = d["distance"]
                if d["distance"] is None:
                    r["clearance"], r["clearance_reason"] = None, "最短距離が測れなかった"
                else:
                    r["clearance"] = ("short" if d["distance"] < clearance - GAP_TOL
                                      else "at" if d["distance"] <= clearance + GAP_TOL else "ok")
        r["a"], r["b"] = (names[i], names[j]) if names else (i, j)
        out.append(r)
    t_dist = time.perf_counter() - t2
    return dict(bodies=len(shapes), all_pairs=len(shapes) * (len(shapes) - 1) // 2, outside=outside,
                cand=len(cand), gf_pairs=len(touching), copy_s=t_copy, broad_s=t_broad,
                gf_s=t_gf, dist_pairs=n_dist, dist_s=t_dist, total_s=time.perf_counter() - t0, pairs=out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--clearance", type=float, default=0.2)
    ap.add_argument("--compare", help="real_docs.py の JSON(厳密経路の分類)と突き合わせる")
    ap.add_argument("--serial", action="store_true")
    ap.add_argument("--whole", action="store_true", help="2体ごとではなく全体に GF を1回")
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
    res = classify(shapes, args.clearance, names, parallel=not args.serial, whole=args.whole)
    rel_counts, clr_counts = {}, {}
    for r in res["pairs"]:
        rel_counts[r["relation"]] = rel_counts.get(r["relation"], 0) + 1
        if "clearance" in r:
            clr_counts[r["clearance"]] = clr_counts.get(r["clearance"], 0) + 1
    print(json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in res.items() if k != "pairs"},
                     ensure_ascii=False), "relation:", rel_counts, "clearance:", clr_counts)
    if args.compare:
        ref = {(p["a"], p["b"]): p for p in json.loads(Path(args.compare).read_text(encoding="utf-8"))[0]["pairs"]}
        mapping = dict(overlap={"overlap", "tiny_overlap"}, contact={"contact", "tiny_overlap"}, near={"separated"},
                       clear={"separated"})
        for r in res["pairs"]:
            e = ref.get((r["a"], r["b"]))
            exact = e["class"] if e else "clear"
            if r["relation"] not in mapping[exact]:
                print(f"  diff {r['a']}-{r['b']}: exact={exact} pipeline={r['relation']}/{r.get('clearance')} "
                      f"{ {k: v for k, v in r.items() if k not in ('a', 'b', 'relation', 'clearance')} }")
    return 0


if __name__ == "__main__":
    sys.exit(main())

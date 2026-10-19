"""実際の文書で、総当たりの絞り込みと方式ごとの計算時間を測る(読むだけ。文書は書き換えない)。

    .\\.venv\\Scripts\\python.exe -m tools.interference_probe.real_docs <文書.json> [...] [--clearance 0.2]

流れ: ログを再生 → 見えている B-rep ボディの境界箱(Add / AddOptimal)→ 箱の距離が clearance 以下の組だけ残す →
組ごとに DistShapeShape(最短距離)と、距離が CONTACT_TOL 以下の組だけ Common(重なり体積)。
比較として General Fuse 1回(全ボディ)と、manifold3d(三角形分割 0.01mm)の同じ組の判定も測る。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from itertools import combinations
from pathlib import Path

import numpy as np

from . import methods as M
from .run_cases import CONTACT_TOL, VOLUME_TOL_REL


def load(path: Path):
    import duocad.session  # noqa: F401  verb を登録する
    from duocad.document import Document
    from duocad.oplog import OpLog
    payload = json.loads(path.read_text(encoding="utf-8"))
    doc = Document()
    doc.log = OpLog.from_payload(payload)
    t0 = time.perf_counter()
    doc.log.replay(doc)
    return doc, time.perf_counter() - t0


def broad_phase(boxes, clearance):
    cand = []
    for i, j in combinations(range(len(boxes)), 2):
        if M.bbox_gap(boxes[i], boxes[j]) <= clearance + CONTACT_TOL:
            cand.append((i, j))
    return cand


class _Named:
    def __init__(self, name, shape):
        self.name, self.shape = name, shape


def load_step(path: Path):
    """STEP のソリッドを1つずつボディとみなす(import_step と同じく1ソリッド1ボディ)。"""
    from duocad import kernel
    t0 = time.perf_counter()
    shape = kernel.read_step(str(path))
    solids, exp = [], M.TopExp_Explorer(shape, M.TopAbs_SOLID)
    while exp.More():
        solids.append(_Named(f"solid{len(solids)}", exp.Current()))
        exp.Next()
    return solids, time.perf_counter() - t0


def run(path: Path, clearance: float, deviation: float, with_gf: bool, with_mf: bool):
    if path.suffix.lower() in (".step", ".stp"):
        solids, t_replay = load_step(path)
        meshes = []
    else:
        doc, t_replay = load(path)
        solids = [b for b in doc.bodies if b.kind == "solid" and b.shape is not None]
        meshes = [b for b in doc.bodies if b.kind == "mesh"]
    shapes = [b.shape for b in solids]
    vols = [M.volume(s) for s in shapes]
    faces = [M.count(s, M.TopAbs_FACE) for s in shapes]
    out = dict(doc=path.name, replay_s=round(t_replay, 3), solids=len(solids), meshes=len(meshes),
               faces_total=int(sum(faces)), faces_max=int(max(faces) if faces else 0),
               all_pairs=len(shapes) * (len(shapes) - 1) // 2)

    t0 = time.perf_counter()
    boxes = [M.bbox(s, optimal=False) for s in shapes]
    out["bbox_add_s"] = round(time.perf_counter() - t0, 4)
    t0 = time.perf_counter()
    boxes_opt = [M.bbox(s, optimal=True) for s in shapes]
    out["bbox_optimal_s"] = round(time.perf_counter() - t0, 4)
    cand = broad_phase(boxes, clearance)
    cand_opt = broad_phase(boxes_opt, clearance)
    out["cand_add"] = len(cand)
    out["cand_optimal"] = len(cand_opt)
    assert set(cand_opt) <= set(cand), "AddOptimal で残った組が Add で落ちている(Add の箱が小さすぎる)"

    # 厳密経路
    t0 = time.perf_counter()
    results = []
    t_dist = t_common = 0.0
    slow = []
    for i, j in cand_opt:
        d = M.dist(shapes[i], shapes[j])
        t_dist += d["seconds"]
        r = dict(i=i, j=j, a=solids[i].name, b=solids[j].name, dist=d.get("distance"), inner=d.get("inner"),
                 dist_s=round(d["seconds"], 4))
        if d.get("distance", math.inf) <= CONTACT_TOL:
            c = M.common(shapes[i], shapes[j])
            t_common += c["seconds"]
            r.update(volume=c["volume"], common_s=round(c["seconds"], 4), common_warn=c["warnings"],
                     common_err=c["errors"])
            r["class"] = "overlap" if c["volume"] > VOLUME_TOL_REL * min(vols[i], vols[j]) else "contact"
        elif d.get("distance", math.inf) < clearance:
            r["class"] = "near"
        else:
            r["class"] = "clear"
        results.append(r)
        slow.append((d["seconds"] + r.get("common_s", 0.0), solids[i].name, solids[j].name))
    out["exact_s"] = round(time.perf_counter() - t0, 3)
    out["exact_dist_s"] = round(t_dist, 3)
    out["exact_common_s"] = round(t_common, 3)
    out["classes"] = {k: sum(1 for r in results if r["class"] == k) for k in ("overlap", "contact", "near", "clear")}
    out["slowest_pairs"] = sorted(slow, reverse=True)[:3]
    out["pairs"] = [r for r in results if r["class"] != "clear"]

    if with_gf and shapes:
        g = M.gf_pairs(shapes)
        out["gf_s"] = round(g["seconds"], 3)
        out["gf_overlaps"] = len([v for v in g["pairs"].values() if v > 0])
        out["gf_warn"] = g["warnings"]
        exact_overlaps = {(r["i"], r["j"]): r["volume"] for r in results if r["class"] == "overlap"}
        out["gf_vs_exact"] = [dict(pair=k, gf=g["pairs"].get(k), exact=exact_overlaps.get(k))
                              for k in set(g["pairs"]) | set(exact_overlaps)
                              if abs((g["pairs"].get(k) or 0) - (exact_overlaps.get(k) or 0)) > 1e-6 * max(1.0, exact_overlaps.get(k) or 0)]

    if with_mf and shapes:
        t0 = time.perf_counter()
        mfs = []
        tris = 0
        for s in shapes:
            v, f = M.tessellated(s, deviation)
            tris += len(f)
            mfs.append(M.to_manifold(v, f))
        out["mf_tess_s"] = round(time.perf_counter() - t0, 3)
        out["mf_tris"] = tris
        t0 = time.perf_counter()
        mismatch = []
        for r in results:
            mr = M.manifold_pair(mfs[r["i"]], mfs[r["j"]], search=max(clearance, 1e-3) * 2)
            cls = ("overlap" if mr["volume"] > VOLUME_TOL_REL * min(vols[r["i"]], vols[r["j"]])
                   else "contact" if mr["min_gap"] <= CONTACT_TOL
                   else "near" if mr["min_gap"] < clearance else "clear")
            if cls != r["class"]:
                mismatch.append(dict(a=r["a"], b=r["b"], exact=r["class"], mf=cls, dist=r["dist"],
                                     mf_gap=mr["min_gap"], vol=r.get("volume"), mf_vol=mr["volume"]))
        out["mf_pairs_s"] = round(time.perf_counter() - t0, 3)
        out["mf_mismatch"] = mismatch
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("docs", nargs="+")
    ap.add_argument("--clearance", type=float, default=0.2)
    ap.add_argument("--deviation", type=float, default=0.01)
    ap.add_argument("--no-gf", action="store_true")
    ap.add_argument("--no-mf", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    rows = []
    for name in args.docs:
        row = run(Path(name), args.clearance, args.deviation, not args.no_gf, not args.no_mf)
        rows.append(row)
        brief = {k: v for k, v in row.items() if k not in ("pairs",)}
        print(json.dumps(brief, ensure_ascii=False, default=str), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

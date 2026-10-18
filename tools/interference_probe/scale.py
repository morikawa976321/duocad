"""ボディ数を増やしたときの絞り込みと計算時間(合成の配置)。

    .\\.venv\\Scripts\\python.exe -m tools.interference_probe.scale [--n 30 100 300]

格子に箱・円柱・フィレット付きの箱を並べ、一部を隣へ食い込ませ、一部を接触・0.1mm の隙間で置く。
組ごとの厳密経路(DistShapeShape → 必要な組だけ Common)と、General Fuse 1回を比べる。
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from itertools import combinations

from . import methods as M
from .cases import box, cyl, filleted_box
from .run_cases import CONTACT_TOL, VOLUME_TOL_REL


def layout(n: int, seed: int = 1):
    rng = random.Random(seed)
    side = int(n ** 0.5 + 0.999)
    shapes = []
    x = 0.0
    for k in range(n):
        gx, gy = k % side, k // side
        # 大きさ 10、行の間は 4 空ける。x 方向の左隣との関係を振る: 食い込み 0.5 / 接触 / 隙間 0.1 / 離れる 2
        relation = rng.choice(["overlap", "touch", "gap", "far", "far"])
        dx = {"overlap": -0.5, "touch": 0.0, "gap": 0.1, "far": 2.0}[relation]
        x = 0.0 if gx == 0 else x + 10.0 + dx
        y = gy * 14.0
        kind = rng.choice(["box", "cyl", "fbox"])
        if kind == "box":
            shapes.append(box(x, y, 0, 10, 10, 10))
        elif kind == "cyl":
            shapes.append(cyl(x + 5, y + 5, 0, 5, 10))
        else:
            shapes.append(filleted_box(x, y, 0, 10, 10, 10, 1.0))
    return shapes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, nargs="+", default=[30, 100, 300])
    ap.add_argument("--clearance", type=float, default=0.2)
    ap.add_argument("--no-gf", action="store_true")
    args = ap.parse_args(argv)
    for n in args.n:
        shapes = layout(n)
        vols = [M.volume(s) for s in shapes]
        t0 = time.perf_counter()
        boxes = [M.bbox(s, optimal=True) for s in shapes]
        t_box = time.perf_counter() - t0
        t0 = time.perf_counter()
        cand = [(i, j) for i, j in combinations(range(n), 2)
                if M.bbox_gap(boxes[i], boxes[j]) <= args.clearance + CONTACT_TOL]
        t_pairs = time.perf_counter() - t0
        t0 = time.perf_counter()
        classes = dict(overlap=0, contact=0, near=0, clear=0)
        overlaps = {}
        for i, j in cand:
            d = M.dist(shapes[i], shapes[j])["distance"]
            if d <= CONTACT_TOL:
                c = M.common(shapes[i], shapes[j])
                if c["volume"] > VOLUME_TOL_REL * min(vols[i], vols[j]):
                    classes["overlap"] += 1
                    overlaps[(i, j)] = c["volume"]
                else:
                    classes["contact"] += 1
            elif d < args.clearance:
                classes["near"] += 1
            else:
                classes["clear"] += 1
        t_exact = time.perf_counter() - t0
        line = (f"n={n} all_pairs={n * (n - 1) // 2} cand={len(cand)} bbox={t_box:.3f}s pairs={t_pairs:.3f}s "
                f"exact={t_exact:.2f}s {classes}")
        if not args.no_gf:
            g = M.gf_pairs(shapes)
            gf_over = {k: v for k, v in g["pairs"].items() if v > 0}
            agree = set(gf_over) == set(overlaps) and all(abs(gf_over[k] - overlaps[k]) <= 1e-6 * max(1, overlaps[k])
                                                          for k in overlaps)
            line += f" | gf={g['seconds']:.2f}s overlaps={len(gf_over)} agree={agree} warn={g['warnings']}"
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

r"""同心の曲面が向かい合う形で、隙間の検査(最短距離)が落ちないかを見る。

    .\.venv\Scripts\python.exe -m tools.interference_probe.concentric

docs/CAD_LIBRARY_PITFALLS.md「`BRepExtrema_DistShapeShape` は同心の円筒どうし・球と円筒の面の対で `StdFail_NotDone` を投げる」
(丸めた箱の内壁と外壁で実測)が、3D プリントの典型「角を丸めた箱に、角を丸めた中子・蓋を隙間 c で入れる」で起きるかを確かめる。
試作のピンと穴(全周の同心円筒)では落ちなかった。
"""
from __future__ import annotations

import sys
import time

from . import methods as M
from .cases import cut, filleted_box, sphere


def try_dist(a, b, parallel=True):
    t0 = time.perf_counter()
    try:
        d = M.dist(a, b, parallel=parallel)
        return f"d={d['distance']!r} done={d['done']} ({time.perf_counter() - t0:.2f}s)"
    except Exception as exc:  # StdFail_NotDone が Python の例外で上がる
        return f"例外 {type(exc).__name__} ({time.perf_counter() - t0:.2f}s)"


def facewise_distance(a, b):
    """面の対ごとに測り、落ちる対は片方の面の縁の点(頂点と辺の等分点)から相手の面へ測る(kernel_shell._face_distance の考え)。

    境界箱の距離が今の最小より大きい面の対は飛ばす。落ちた対の値は縁の標本からの値で、真の最小以上(同心の面なら一致)。
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    def faces(s):
        out, exp = [], M.TopExp_Explorer(s, M.TopAbs_FACE)
        while exp.More():
            out.append(exp.Current())
            exp.Next()
        return out

    def points(face, n=16):
        pts, exp = [], M.TopExp_Explorer(face, TopAbs_EDGE)
        while exp.More():
            e = TopoDS.Edge_s(exp.Current())
            if not BRep_Tool.Degenerated_s(e):
                c = BRepAdaptor_Curve(e)
                f, l = c.FirstParameter(), c.LastParameter()
                pts += [BRepBuilderAPI_MakeVertex(c.Value(f + (l - f) * k / n)).Vertex() for k in range(n + 1)]
            exp.Next()
        return pts

    def one(x, y):
        try:
            d = M.BRepExtrema_DistShapeShape(x, y)
            return d.Value() if d.IsDone() else None
        except Exception:
            return None

    t0 = time.perf_counter()
    fa, fb = faces(a), faces(b)
    ba, bb = [M.bbox(f) for f in fa], [M.bbox(f) for f in fb]
    order = sorted(((M.bbox_gap(ba[i], bb[j]), i, j) for i in range(len(fa)) for j in range(len(fb))))
    best, fallback_pairs, measured = float("inf"), 0, 0
    for gap, i, j in order:
        if gap > best:
            break
        measured += 1
        d = one(fa[i], fb[j])
        if d is None:
            fallback_pairs += 1
            ds = [one(p, fb[j]) for p in points(fa[i])] + [one(p, fa[i]) for p in points(fb[j])]
            ds = [x for x in ds if x is not None]
            if not ds:
                continue
            d = min(ds)
        best = min(best, d)
    return f"d={best!r} 測った面の対 {measured}・縁の標本に落ちた対 {fallback_pairs} ({time.perf_counter() - t0:.2f}s)"


def mesh_gap(a, b, dev=0.01):
    t0 = time.perf_counter()
    ma, mb = M.to_manifold(*M.tessellated(a, dev)), M.to_manifold(*M.tessellated(b, dev))
    return f"min_gap={ma.min_gap(mb, 1.0):.5f} ({time.perf_counter() - t0:.2f}s)"


def main():
    rows = []
    for r_out, c in [(3.0, 0.2), (3.0, 0.1), (2.0, 0.2)]:
        # 外箱 40×30×20、内側を角の丸め r_out の箱でくり抜く(壁 2、底 2)。中子は隙間 c だけ小さく、丸めは r_out − c(同心)
        outer = filleted_box(0, 0, 0, 40, 30, 20, r_out + 2.0)
        pocket = filleted_box(2, 2, 2, 36, 26, 30, r_out)
        box_ = cut(outer, pocket)
        core = filleted_box(2 + c, 2 + c, 2 + c, 36 - 2 * c, 26 - 2 * c, 25, r_out - c)
        g = M.gf_relations([box_, core])
        rows.append(f"丸めた箱 r={r_out} 隙間 {c}: GF={g['relations'][(0, 1)]['kind']} ({g['seconds']:.2f}s) | "
                    f"最短距離(並列) {try_dist(box_, core)} | 直列 {try_dist(box_, core, parallel=False)} | "
                    f"面の対ごと {facewise_distance(box_, core)} | メッシュ0.01 {mesh_gap(box_, core)}")
    # 球と円筒(球面の受けに球を置く)
    from .cases import block_with_hole, cyl
    ball = sphere(0, 0, 5, 5.0)
    hole = block_with_hole(5.2, size=20, h=10)
    rows.append(f"円筒の穴(r5.2)に球(r5): 最短距離 {try_dist(hole, ball)}")
    for r in rows:
        print(r, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

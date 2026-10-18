r"""相談役(astra)の1回目の指摘を実測で確かめる。

    .\.venv\Scripts\python.exe -m tools.interference_probe.astra_checks [--step <PC ケースの STEP>]

1. GF / COMMON は入力の形状を書き換えるか(既定の破壊モードと SetNonDestructive(True)、形状許容差の前後比較)
2. 第三のボディ C が A∩B を横切ると、A-B の体積・面積・厚さ 2V/A が変わるか(内部の分割面を数えると厚さが下がる)。
   引数の順を入れ替えても変わらないか
3. 広い薄片と深い食い込みが1つの領域につながると、2V/A が薄片の側に引っ張られるか(有向境界箱の最小辺も比べる)
4. PC ケースで、組ごとの2体 GF(写し+非破壊)と、全体1回の GF の時間と結果
"""
from __future__ import annotations

import argparse
import itertools
import sys
import time
from pathlib import Path

from OCP.BOPAlgo import BOPAlgo_Builder
from OCP.Bnd import Bnd_OBB
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopTools import TopTools_ListOfShape

from . import methods as M
from .cases import box, cyl
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse


def area(shape):
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return props.Mass()


def obb_min(shape):
    obb = Bnd_OBB()
    BRepBndLib.AddOBB_s(shape, obb, False, True, False)
    return 2 * min(obb.XHSize(), obb.YHSize(), obb.ZHSize())


def gf(shapes, nondestructive=False, parallel=True):
    b = BOPAlgo_Builder()
    args = TopTools_ListOfShape()
    for s in shapes:
        args.Append(s)
    b.SetArguments(args)
    b.SetNonDestructive(nondestructive)
    b.SetRunParallel(parallel)
    b.Perform()
    return b


def shared_pieces(b, shapes, i, j):
    """i と j の像が共有する立体片。hash で引いて IsSame で確かめる。"""
    def pieces(s):
        out = {}
        exp = TopExp_Explorer(s, TopAbs_SOLID)
        while exp.More():
            sol = exp.Current()
            imgs = list(b.Modified(sol))
            if not imgs and not b.IsDeleted(sol):
                imgs = [sol]
            for p in imgs:
                out[hash(p)] = p
            exp.Next()
        return out
    a, c = pieces(shapes[i]), pieces(shapes[j])
    return [a[h] for h in a.keys() & c.keys() if a[h].IsSame(c[h])]


def thickness(pieces, exclude_internal=True):
    """立体片の集まりの 2V/A。exclude_internal なら、2つの片に共有される面(内部の分割面)を面積から除く。"""
    v = sum(M.volume(p) for p in pieces)
    faces = {}
    for p in pieces:
        exp = TopExp_Explorer(p, TopAbs_FACE)
        while exp.More():
            f = exp.Current()
            faces.setdefault(hash(f), [f, 0])[1] += 1
            exp.Next()
    a_all = sum(area(f) for f, _ in faces.values()) if not exclude_internal else None
    a_ext = sum(area(f) for f, n in faces.values() if n == 1)
    a_naive = sum(area(p) for p in pieces)
    return dict(volume=v, area_naive=a_naive, area_external=a_ext,
                t_naive=2 * v / a_naive if a_naive else 0.0, t_external=2 * v / a_ext if a_ext else 0.0)


def check_destructive(shapes, label):
    st = ShapeAnalysis_ShapeTolerance()
    for nd in (False, True):
        copies = [BRepBuilderAPI_Copy(s, True, False).Shape() for s in shapes]
        before = [st.Tolerance(c, 1) for c in copies]
        t0 = time.perf_counter()
        b = gf(copies, nondestructive=nd)
        dt = time.perf_counter() - t0
        after = [st.Tolerance(c, 1) for c in copies]
        changed = [(k, before[k], after[k]) for k in range(len(copies)) if after[k] != before[k]]
        print(f"[1] {label} nondestructive={nd} {dt:.2f}s errors={b.HasErrors()} "
              f"入力の最大許容差が変わったボディ {len(changed)}/{len(copies)} {changed[:4]}", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", default=r"C:\Users\tm976\mf\3d-models\mATX_PC_case_20251122\PC_case_20251122.step")
    args = ap.parse_args(argv)

    # 1. 破壊モード(合成: 許容差を太らせる食い込みの浅い組)
    check_destructive([box(0, 0, 0, 10, 10, 10), box(9.99999, 0.3, 0.3, 10, 9, 9), cyl(5, 5, -2, 3, 14)], "合成3体")

    # 2. 第三のボディ
    A, B = box(0, 0, 0, 10, 10, 10), box(6, 0, 0, 10, 10, 10)        # A∩B = 4x10x10 (厚さの目安 2V/A = 2*400/(2*(40+40+100)) = 2.22)
    C = box(7.5, -5, 4, 1, 20, 2)                                       # A∩B を y 方向に貫く板
    two = gf([A, B])
    t2 = thickness(shared_pieces(two, [A, B], 0, 1))
    print(f"[2] 2体: {t2}", flush=True)
    for order in itertools.permutations([0, 1, 2]):
        shapes = [[A, B, C][k] for k in order]
        b = gf(shapes)
        ia, ib = order.index(0), order.index(1)
        t3 = thickness(shared_pieces(b, shapes, ia, ib))
        print(f"[2] 3体 順={order}: V={t3['volume']:.6f} t_naive={t3['t_naive']:.4f} "
              f"t_external={t3['t_external']:.4f} 片={len(shared_pieces(b, shapes, ia, ib))}", flush=True)

    # 3. 広い薄片 + 深い食い込みが1つの領域
    base = box(0, 0, 0, 100, 100, 10)
    lid = BRepAlgoAPI_Fuse(box(0, 0, 9.999999, 100, 100, 5), cyl(50, 50, 5, 1.0, 6)).Shape()   # 1e-6 の薄片 + 深さ 5 のピン
    b = gf([base, lid])
    pcs = shared_pieces(b, [base, lid], 0, 1)
    t = thickness(pcs)
    print(f"[3] 薄片+ピン: 片={len(pcs)} V={t['volume']:.6f} t_external={t['t_external']:.3g} "
          f"obb_min={[round(obb_min(p), 6) for p in pcs]} 片ごとの2V/A={[round(thickness([p])['t_external'], 6) for p in pcs]}",
          flush=True)

    # 3b. OBB を足した効果を示す形(2V/A だけなら tiny、OBB で overlap): 板を 200×200 に広げる
    from .pipeline import relation_of
    base = box(0, 0, 0, 200, 200, 10)
    lid = BRepAlgoAPI_Fuse(box(0, 0, 9.999999, 200, 200, 5), cyl(100, 100, 5, 1.0, 6)).Shape()
    rel = M.gf_relations([base, lid])["relations"][(0, 1)]
    print(f"[3b] 薄片 200x200 + ピン: 2V/A={rel['thickness']:.3g} obb_min={rel['obb_min']:.3g} 領域={rel['regions']} "
          f"→ relation={relation_of(rel)}(2V/A だけなら {'tiny_overlap' if rel['thickness'] <= 1e-3 else 'overlap'})", flush=True)

    # 4. PC ケース: 組ごとの2体 GF と全体1回
    step = Path(args.step)
    if step.exists():
        from .real_docs import load_step
        solids, _ = load_step(step)
        shapes = [s.shape for s in solids]
        check_destructive(shapes, "PCケース27体")
        boxes = [M.bbox(s) for s in shapes]
        cand = [(i, j) for i, j in itertools.combinations(range(len(shapes)), 2) if M.bbox_gap(boxes[i], boxes[j]) <= 0.0]
        t0 = time.perf_counter()
        copies = [BRepBuilderAPI_Copy(s, True, False).Shape() for s in shapes]
        t_copy = time.perf_counter() - t0
        t0 = time.perf_counter()
        pair_rel = {}
        for i, j in cand:
            r = M.gf_relations([copies[i], copies[j]], parallel=True)
            pair_rel[(i, j)] = r["relations"][(0, 1)]["kind"]
        t_pairs = time.perf_counter() - t0
        t0 = time.perf_counter()
        whole = M.gf_relations(copies, parallel=True, pairs=cand)
        t_whole = time.perf_counter() - t0
        diff = [(solids[i].name, solids[j].name, pair_rel[(i, j)], whole["relations"][(i, j)]["kind"])
                for i, j in cand if pair_rel[(i, j)] != whole["relations"][(i, j)]["kind"]]
        print(f"[4] PCケース 接触候補 {len(cand)} 組: 写し {t_copy:.2f}s / 2体GFを組ごと {t_pairs:.2f}s / 全体1回 {t_whole:.2f}s "
              f"/ 種類の食い違い {diff}", flush=True)
        # 5. PC ケースの重なり片の厚さ(外側の面だけ)と有向境界箱の最小辺
        for i, j in cand:
            if pair_rel[(i, j)] != "overlap":
                continue
            b = gf([copies[i], copies[j]], nondestructive=True)
            pcs = shared_pieces(b, [copies[i], copies[j]], 0, 1)
            t = thickness(pcs)
            print(f"[5] {solids[i].name}-{solids[j].name}: 片={len(pcs)} V={t['volume']:.4g} 2V/A={t['t_external']:.3g} "
                  f"obb_min(最大の片)={max(obb_min(p) for p in pcs):.3g}", flush=True)

    # 6. 曲面の薄片(ピン r=5+1e-6 を穴 r=5 へ)と、平面の薄片を回転させたもの
    from .cases import block_with_hole, rotated
    for label, a, c in [
        ("曲面の薄片 1e-6", block_with_hole(5.0), cyl(0, 0, -5, 5.000001, 20)),
        ("回転した平面の薄片 1e-6", rotated(box(0, 0, 0, 10, 10, 10), axis=(1, 1, 0), deg=30),
         rotated(box(9.999999, 0, 0, 10, 10, 10), axis=(1, 1, 0), deg=30)),
    ]:
        b = gf([a, c], nondestructive=True)
        pcs = shared_pieces(b, [a, c], 0, 1)
        t = thickness(pcs) if pcs else dict(volume=0.0, t_external=0.0)
        print(f"[6] {label}: 片={len(pcs)} V={t['volume']:.3g} 2V/A={t['t_external']:.3g} "
              f"obb_min={[round(obb_min(p), 6) for p in pcs]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

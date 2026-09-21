"""部材の型ごとに「探索の格子(k=1)の代表応力」と「形は変えず k×k×k に割った代表応力」を比べる。

    .\.venv\Scripts\python.exe topopt_checks\measure_member_refinement_ratio.py --out C:\tmp\member_probe.json
    .\.venv\Scripts\python.exe topopt_checks\measure_member_refinement_ratio.py --m 4 5 --groups straight ljoint diag2d --out C:\tmp\member_probe_m45.json

製品と同じ評価(現行の要素剛性・要素中心の von Mises・99.5% 点)を、m セル厚の合成部材に当てる。
部材の端(固定・荷重の塊)から離れた区間(ゲージ)だけを母集団にする。リポジトリのコードは変更しない。
**判定はしない**。結果は `docs/TOPOPT_TARGET_FACTOR.md` に転記してある。

    straight   m セル角の棒(長さ 16m)の軸力・曲げ・ねじり。理論値(区間内の表面の最大)との比 `p995_over_theory` も出す
    plate      m セル厚の板(幅 8m)の曲げ(k=1..4 の最初の4つ。KS[:4])
    diag2d / diag3d / neck111   m 立方を1セルずつずらしてつないだ階段(面内45°・空間対角・(1,1,1) ずらしの首)。真値は無く k=4 までの比
    ljoint     L字の合流(腕 8m)

`k` は各辺の分割数。`p995_ratio` = k の 99.5% 点 ÷ k=1 のもの(探索の格子の過大評価の倍率)。
**直接解法は 6,000 要素未満だけ**(k=6 の約3万要素で 3〜4GB を食い、並走する探索を圧迫した。
2026-09-21 実測)。それ以上は CG。`MAX_ELEMENTS` を超える k は解かない。
出力は既定で標準出力だけ(`--out` を渡したときだけファイルへ書く。k ごとに書き直す)。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
import _record
from duocad.topopt import fem, stress
from duocad.topopt.anchor import active_nodes

MAX_ELEMENTS = 120_000


def staircase(m, steps, axes, depth=None):
    """m 立方を `axes` の順に1セルずつずらして `steps` 個つないだ和集合。axes の要素は3成分のずらし。"""
    pos = np.zeros(3, int)
    cubes = [pos.copy()]
    for i in range(steps):
        pos = pos + np.array(axes[i % len(axes)])
        cubes.append(pos.copy())
    size = np.array([m, m, m if depth is None else depth])
    hi = np.max(cubes, axis=0) + size
    mask = np.zeros(tuple(hi), bool)
    for c in cubes:
        mask[c[0]:c[0] + size[0], c[1]:c[1] + size[1], c[2]:c[2] + size[2]] = True
    return mask, cubes, size


def box(shape):
    return np.ones(shape, bool)


def refine(mask, k):
    for axis in range(3):
        mask = np.repeat(mask, k, axis=axis)
    return mask


def node_coords(shape, k):
    grids = np.meshgrid(*[np.arange(n + 1) / k for n in shape], indexing="ij")
    return np.stack([g.ravel() for g in grids], axis=1)


def cell_centres(shape, k):
    grids = np.meshgrid(*[(np.arange(n) + 0.5) / k for n in shape], indexing="ij")
    return np.stack(grids, axis=-1)


def in_box(points, lo, hi, eps=1e-9):
    return np.all((points >= np.asarray(lo) - eps) & (points <= np.asarray(hi) + eps), axis=-1)


def run_case(name, mask1, fixed_box, load_box, load, gauge, ks):
    """load: ('force', vec) か ('torque_x', T)。gauge: 要素中心(物理座標) -> bool。"""
    rows = []
    for k in ks:
        mask = refine(mask1, k)
        n_el = int(mask.sum())
        if n_el > MAX_ELEMENTS:
            break
        live = active_nodes(mask).ravel()
        xyz = node_coords(mask.shape, k)
        fixed_nodes = np.flatnonzero(live & in_box(xyz, *fixed_box))
        load_nodes = np.flatnonzero(live & in_box(xyz, *load_box))
        f = np.zeros((xyz.shape[0], 3))
        if load[0] == "force":
            f[load_nodes] = np.asarray(load[1], float) / len(load_nodes)
        else:
            r = xyz[load_nodes] - xyz[load_nodes].mean(axis=0)
            r[:, 0] = 0.0
            tang = np.cross(np.array([1.0, 0, 0]), r)
            f[load_nodes] = load[1] * tang / np.sum(r[:, 1] ** 2 + r[:, 2] ** 2)
        fixed_dofs = (fixed_nodes[:, None] * 3 + np.arange(3)).ravel()
        active_dofs = np.repeat(live, 3)
        model = fem.Model(*mask.shape, fixed_dofs=fixed_dofs, load=f.ravel(),
                          extra_fixed_dofs=np.flatnonzero(~active_dofs), active_mask=mask)
        t0 = time.perf_counter()
        # 直接解法は k=6(約3万要素)で 3〜4GB を食い、並走する taskbox の探索を圧迫した(2026-09-21 実測)。
        u = model.solve(mask.astype(float), 3.0, solver="direct" if n_el < 6_000 else "cg")
        sec = time.perf_counter() - t0
        vm = stress.element_stress(model, u).von_mises * k ** 2
        pick = mask & gauge(cell_centres(mask.shape, k))
        values = vm[pick]
        rows.append(dict(k=k, elements=n_el, gauge_elements=int(pick.sum()),
                         p995=float(np.percentile(values, 99.5)), max=float(values.max()),
                         max_disp=float(np.abs(u).max()) * k, sec=round(sec, 2)))
    base = rows[0]
    for row in rows:
        row["p995_ratio"] = row["p995"] / base["p995"]
        row["max_ratio"] = row["max"] / base["max"]
        row["disp_ratio"] = row["max_disp"] / base["max_disp"]
    return dict(case=name, rows=rows)


def straight_cases(m):
    length = 16 * m
    mask = box((length, m, m))
    fixed = ((0, 0, 0), (m, m, m))
    loadb = ((length - m, 0, 0), (length, m, m))
    gauge = lambda c: (c[..., 0] >= 0.3 * length) & (c[..., 0] <= 0.7 * length)
    arm = (length - m / 2) - 0.3 * length
    out = []
    for label, load, theory in (
        ("axial", ("force", (1, 0, 0)), 1.0 / m ** 2),
        ("bending", ("force", (0, 1, 0)), arm * (m / 2) / (m ** 4 / 12)),
        ("torsion", ("torque_x", 1.0), np.sqrt(3) / (0.208 * m ** 3)),
    ):
        res = run_case(f"straight m={m} {label}", mask, fixed, loadb, load, gauge, KS)
        res["theory_surface_vm"] = float(theory)
        for row in res["rows"]:
            row["p995_over_theory"] = row["p995"] / theory
        out.append(res)
    return out


def plate_case(m):
    length, width = 16 * m, 8 * m
    mask = box((length, m, width))
    gauge = lambda c: (c[..., 0] >= 0.3 * length) & (c[..., 0] <= 0.7 * length)
    res = run_case(f"plate m={m} bending", mask, ((0, 0, 0), (m, m, width)),
                   ((length - m, 0, 0), (length, m, width)), ("force", (0, 1, 0)), gauge, KS[:4])
    arm = (length - m / 2) - 0.3 * length
    theory = arm * (m / 2) / (width * m ** 3 / 12)
    res["theory_surface_vm"] = float(theory)
    for row in res["rows"]:
        row["p995_over_theory"] = row["p995"] / theory
    return [res]


def diagonal_cases(m, kind):
    axes = {"diag2d": [(1, 0, 0), (0, 1, 0)],
            "diag3d": [(1, 0, 0), (0, 1, 0), (0, 0, 1)],
            "neck111": [(1, 1, 1)]}[kind]
    steps = {"diag2d": 24 * 1, "diag3d": 30, "neck111": 12}[kind] * (1 if m == 2 else 1)
    steps = steps + (m - 2) * len(axes) * 4
    mask, cubes, size = staircase(m, steps, axes)
    d = (cubes[-1] - cubes[0]).astype(float)
    d /= np.linalg.norm(d)
    normal = np.array([1.0, -1.0, 0.0]) / np.sqrt(2)
    c0 = cubes[0] + size / 2
    span = float(np.dot(cubes[-1] - cubes[0], d))
    gauge = lambda c: ((c - c0) @ d >= 0.3 * span) & ((c - c0) @ d <= 0.7 * span)
    fixed = (cubes[0], cubes[0] + size)
    loadb = (cubes[-1], cubes[-1] + size)
    out = []
    for label, vec in (("axial", d), ("bending", normal)):
        out.append(run_case(f"{kind} m={m} {label}", mask, fixed, loadb, ("force", vec), gauge, KS))
    return out


def l_joint_case(m):
    arm = 8 * m
    mask = np.zeros((arm, arm, m), bool)
    mask[:, :m, :] = True
    mask[:m, :, :] = True
    fixed = ((arm - m, 0, 0), (arm, m, m))
    loadb = ((0, arm - m, 0), (m, arm, m))
    far = 2 * m
    gauge = lambda c: ~in_box(c, (arm - m - far, 0, 0), (arm, m, m)) & ~in_box(c, (0, arm - m - far, 0), (m, arm, m))
    out = []
    for label, vec in (("opening", (-1, 0, 0)), ("shear", (0, -1, 0))):
        out.append(run_case(f"L-joint m={m} {label}", mask, fixed, loadb, ("force", vec), gauge, KS))
    return out


KS = (1, 2, 3, 4)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--m", type=int, nargs="+", default=[2, 3])
    parser.add_argument("--groups", nargs="+", default=["straight", "plate", "diag2d", "diag3d", "neck111", "ljoint"])
    parser.add_argument("--out", type=Path, default=None, help="結果JSONの書き出し先(省略時は標準出力だけ)")
    args = parser.parse_args()
    builders = {"straight": straight_cases, "plate": plate_case, "ljoint": l_joint_case,
                "diag2d": lambda m: diagonal_cases(m, "diag2d"), "diag3d": lambda m: diagonal_cases(m, "diag3d"),
                "neck111": lambda m: diagonal_cases(m, "neck111")}
    results = []
    for m in args.m:
        for group in (builders[name](m) for name in args.groups):
            for res in group:
                results.append(res)
                print(f"\n{res['case']}" + (f"  (theory surface VM {res['theory_surface_vm']:.4g})" if "theory_surface_vm" in res else ""))
                print(f"{'k':>2} {'elements':>8} {'gauge':>7} {'p99.5':>10} {'x':>6} {'max':>10} {'x':>6} {'disp x':>7} {'p/theory':>9} {'sec':>6}")
                for row in res["rows"]:
                    print(f"{row['k']:>2} {row['elements']:>8} {row['gauge_elements']:>7} {row['p995']:>10.4g} {row['p995_ratio']:>6.3f} "
                          f"{row['max']:>10.4g} {row['max_ratio']:>6.3f} {row['disp_ratio']:>7.3f} "
                          f"{row.get('p995_over_theory', float('nan')):>9.3f} {row['sec']:>6}", flush=True)
                if args.out:
                    _record.write(args.out, results, default=float)

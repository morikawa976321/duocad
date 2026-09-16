"""SIMP 1反復の秒数内訳を実測する(一時調査用)。

.\\.venv\\Scripts\\python.exe topopt_checks\\profile_simp_iter_breakdown.py
.\\.venv\\Scripts\\python.exe topopt_checks\\profile_simp_iter_breakdown.py --smoke
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np

from duocad.optimize_run import build_conditions
from duocad.session import Session
from duocad.topopt import anchor as an
from duocad.topopt import design_space as ds
from duocad.topopt import simp

MODEL = "hanger_bar_attachment.json"
SPACING_MM = 2.4
SMALLEST_RATIO = 0.066


def _load_bundle():
    session = Session(None, 0.2)
    session.open_document(MODEL)
    return build_conditions(session.document)


def _profile_optimize_loop(
    *,
    nelx: int,
    nely: int,
    nelz: int,
    void: np.ndarray,
    solid: np.ndarray,
    fixed_dofs: np.ndarray,
    load: np.ndarray,
    material_mm3: float,
    element_volume_mm3: float,
    max_iter: int,
    threads: int,
) -> dict:
    shape = (nelx, nely, nelz)
    model = simp.Model(
        nelx,
        nely,
        nelz,
        fixed_dofs=fixed_dofs,
        load=load,
        extra_fixed_dofs=simp._floating_dofs(nelx, nely, nelz, void) if void.any() else None,
        active_mask=~void if void.any() else None,
    )
    rmin_resolved = simp.DEFAULT_RMIN
    kernel, r = simp._filter_kernel(rmin_resolved)
    hs = simp._filter_conv(np.ones(shape), kernel, r)
    movable = ~void & ~solid
    n_movable = int(movable.sum())
    volfrac, target_volume = simp._resolve_volume_target(
        volfrac=None,
        material_mm3=material_mm3,
        element_volume_mm3=element_volume_mm3,
        n_movable=n_movable,
        n_solid=int(np.count_nonzero(solid)),
    )
    live = movable.astype(float)
    penal = 3.0
    move = 0.2

    def physical(design: np.ndarray) -> np.ndarray:
        field = simp._filter_conv(design, kernel, r) / hs
        field[void] = 0.0
        field[solid] = 1.0
        return field

    x = np.full(shape, volfrac)
    x[void] = 0.0
    x[solid] = 1.0
    x_phys = physical(x)
    u = np.zeros(model.ndof)

    filter_seconds = 0.0
    filter_calls = 0
    orig_filter = simp._filter_conv

    def timed_filter(field, k, rad):
        nonlocal filter_seconds, filter_calls
        t0 = time.perf_counter()
        out = orig_filter(field, k, rad)
        filter_seconds += time.perf_counter() - t0
        filter_calls += 1
        return out

    simp._filter_conv = timed_filter  # type: ignore[assignment]

    rows: list[dict] = []
    solve_stats = simp.new_solve_stats()
    prev_stats = simp._SOLVE_STATS
    simp._SOLVE_STATS = solve_stats
    try:
        for it in range(1, max_iter + 1):
            t_iter = time.perf_counter()
            filter_before = filter_seconds

            t0 = time.perf_counter()
            u = model.solve(x_phys, penal, u0=u, threads=threads)
            solve_sec = time.perf_counter() - t0

            t0 = time.perf_counter()
            c, ce = model.compliance(x_phys, penal, u)
            compliance_sec = time.perf_counter() - t0

            dc = -penal * x_phys ** (penal - 1.0) * (model.E - model.emin) * ce
            dv = np.ones_like(x)

            t0 = time.perf_counter()
            dcn = orig_filter(dc * live / hs, kernel, r)
            dvn = orig_filter(dv * live / hs, kernel, r)
            dcn_dvn_sec = time.perf_counter() - t0
            dvn = np.maximum(dvn, 1e-300)

            t0 = time.perf_counter()
            l1, l2 = 0.0, 1e9
            xp = x_phys
            while l1 + l2 > 0.0 and (l2 - l1) / (l1 + l2) > 1e-9:
                lm = 0.5 * (l1 + l2)
                xnew = np.clip(
                    x * np.sqrt(np.maximum(0.0, -dcn) / (dvn * lm)),
                    x - move,
                    x + move,
                )
                xnew = np.clip(xnew, 0.0, 1.0)
                xnew[void] = 0.0
                xnew[solid] = 1.0
                xp = physical(xnew)
                if xp.sum() > target_volume:
                    l1 = lm
                else:
                    l2 = lm
            oc_sec = time.perf_counter() - t0

            change = float(np.abs(xnew - x).max())
            x = xnew
            x_phys = physical(x)
            iter_sec = time.perf_counter() - t_iter
            filter_iter = filter_seconds - filter_before

            rows.append(
                {
                    "it": it,
                    "seconds_total": iter_sec,
                    "solve": solve_sec,
                    "compliance": compliance_sec,
                    "dcn_dvn_filter": dcn_dvn_sec,
                    "oc_loop": oc_sec,
                    "filter_in_iter": filter_iter,
                    "change": change,
                }
            )
    finally:
        simp._filter_conv = orig_filter
        simp._SOLVE_STATS = prev_stats

    return {
        "iterations": rows,
        "solve_stats": solve_stats,
        "filter_calls": filter_calls,
        "filter_seconds_total": filter_seconds,
        "grid": [nelx, nely, nelz],
        "grid_elements": nelx * nely * nelz,
        "active_elements": int(np.count_nonzero(~void)),
        "movable_elements": n_movable,
        "ndof": model.ndof,
        "free_dof": int(model.free.size),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="材料6.6%・2反復だけ回す")
    parser.add_argument("--threads", type=int, default=None)
    args = parser.parse_args()

    threads = args.threads
    if threads is None:
        threads = int(os.environ.get("TOPOPT_THREADS", simp.PARALLEL_THREADS))

    bundle = _load_bundle()
    conditions = bundle.conditions
    forces = bundle.forces

    t0 = time.perf_counter()
    space = ds.derive(conditions, spacing=SPACING_MM)
    capacity = float(space.element_count) * float(space.grid.element_volume)
    material_mm3 = capacity * SMALLEST_RATIO

    print(f"題材={MODEL} spacing={SPACING_MM}mm threads={threads}")
    print(
        f"箱={space.grid.shape} 箱要素={space.grid.element_count:,} "
        f"塊={space.element_count:,}(固定凸包) 容量={capacity:,.0f}mm^3 "
        f"導出 {time.perf_counter() - t0:.1f}秒"
    )

    model = space.model
    nx, ny, nz = space.grid.shape
    table = space.table if space.table is not None else an.build_anchors(space.soup, space.grid)
    fixed_dofs = table.fixed_dofs(space.fixed_faces)
    load, _ = an._assemble_load(table, space.load_faces, (0.0, 0.0, -1.0), forces)
    solid = model.keep.copy()
    void = ~model.design | model.obstacle

    max_iter = 2 if args.smoke else 3
    print(
        f"材料={material_mm3:,.0f}mm^3 ({material_mm3 / capacity:.1%}) "
        f"塊要素={int(np.count_nonzero(space.mask)):,} "
        f"void={int(void.sum()):,} active={int((~void).sum()):,} "
        f"max_iter={max_iter}",
        flush=True,
    )

    prof = _profile_optimize_loop(
        nelx=nx,
        nely=ny,
        nelz=nz,
        void=void,
        solid=solid,
        fixed_dofs=fixed_dofs,
        load=load,
        material_mm3=material_mm3,
        element_volume_mm3=float(space.grid.element_volume),
        max_iter=max_iter,
        threads=threads,
    )

    print("\n=== 格子 ===")
    print(f"  箱要素: {prof['grid_elements']:,}  active(組立): {prof['active_elements']:,}")
    print(f"  movable: {prof['movable_elements']:,}  自由度: {prof['ndof']:,}  自由: {prof['free_dof']:,}")

    print("\n=== 反復ごと(秒) ===")
    for row in prof["iterations"]:
        other = row["seconds_total"] - (
            row["solve"]
            + row["compliance"]
            + row["dcn_dvn_filter"]
            + row["oc_loop"]
        )
        print(
            f"  it={row['it']}: total={row['seconds_total']:.2f} "
            f"solve={row['solve']:.2f} compliance={row['compliance']:.3f} "
            f"dcn/dvn={row['dcn_dvn_filter']:.2f} oc={row['oc_loop']:.2f} "
            f"filter={row['filter_in_iter']:.2f} other={other:.3f}"
        )

    ss = prof["solve_stats"]
    n_solves = max(1, ss["solves"])
    print("\n=== solve 内訳(全反復合計 → 1 solve 平均) ===")
    for key, label in [
        ("assemble_seconds", "assemble(K組立)"),
        ("slice_seconds", "slice(自由DOF切出)"),
        ("mg_setup_seconds", "mg_setup(RAP+LU)"),
        ("solve_seconds", "cg/vcycle(残り)"),
    ]:
        total = float(ss[key])
        print(f"  {label}: {total:.2f}s total  ({total / n_solves:.2f}s/solve)")

    cg_each = ss["cg_iterations_each"]
    if cg_each:
        print(
            f"  CG反復: min={min(cg_each)} max={max(cg_each)} "
            f"avg={sum(cg_each) / len(cg_each):.1f} 各反復={cg_each}"
        )

    avg_iter = sum(r["seconds_total"] for r in prof["iterations"]) / len(prof["iterations"])
    avg_solve = sum(r["solve"] for r in prof["iterations"]) / len(prof["iterations"])
    avg_filter = prof["filter_seconds_total"] / max(1, prof["filter_calls"])
    print("\n=== サマリ ===")
    print(f"  1設計反復平均: {avg_iter:.2f}s")
    print(f"  solve平均: {avg_solve:.2f}s ({100 * avg_solve / avg_iter:.0f}%)")
    print(f"  密度フィルタ1回: {avg_filter:.3f}s  反復あたり呼び出し~4回(physical含む)")
    print(f"  フィルタ推定/反復: {avg_filter * 4:.2f}s ({100 * avg_filter * 4 / avg_iter:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

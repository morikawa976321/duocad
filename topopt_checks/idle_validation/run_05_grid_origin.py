"""タスク5・格子/原点ロバスト性: 最小幅・フィルタ幅をmmで固定した格子細分と、半セル原点移動を実行し、
形状の位相(mask_hash)/体積/性能(安全率)のばらつきを測る(タスク指示「最小幅とフィルタ幅をmmで
固定した格子細分・原点ずれを実行し…ばらつきを測る」)。

1) **原点移動**(10_SYNTHESIS 付録A(旧 01_PROTOCOL 6-1節)の8条件: 基準+x/y/z単独±0.5セル+全軸+0.5セル)。
   `synthetic_matrix_support.build_context_with_origin_shift` で新規探索そのものを原点シフト下で
   行う(`metric_eval`の原点シフトは既存マスクの再投影専用で新規探索には使えない——本スクリプトの
   拡張理由)。方式は製品コード(step20)を主とし、dilution/pathのbaselineで適応方式も1点確認する。
2) **フィルタ幅mm固定 vs セル数固定**(6-3節)。`binary_search.ENERGY_FILTER_SIZE`(既定3セル、
   物理幅は格子ごとに変わる)と、baseline解像度で得た物理幅(dilution/pathとも 3cell×2.0mm=6.0mm)を
   他の解像度でもmmで揃えた版(セル数を丸めて再計算)を比較する。

出力: `output/idle_validation_20260928/05_grid_origin.json`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

OUT = sup.OUT


def _shift_table(spacing_mm: float) -> dict:
    h = 0.5 * spacing_mm
    return {
        "0": (0.0, 0.0, 0.0),
        "+0.5x": (h, 0.0, 0.0), "-0.5x": (-h, 0.0, 0.0),
        "+0.5y": (0.0, h, 0.0), "-0.5y": (0.0, -h, 0.0),
        "+0.5z": (0.0, 0.0, h), "-0.5z": (0.0, 0.0, -h),
        "+0.5all": (h, h, h),
    }


def _score_step20(model, load_n, element_size_mm, min_width_mm, shift):
    ctx = sup.build_context_with_origin_shift(model, load_n=load_n, element_size_mm=element_size_mm,
                                              min_width_mm=min_width_mm, shift_mm=shift)
    rec = sm.run_fixed_step(ctx, max_step_ratio=0.2)
    summary = sup.record_summary(rec)
    summary["achieved_origin_mm"] = list(ctx.grid.origin)
    summary["requested_shift_mm"] = list(shift)
    return summary


def origin_sweep(name: str, model, load_n: float, element_size_mm: float, min_width_mm: float) -> dict:
    out = {}
    for tag, shift in _shift_table(element_size_mm).items():
        print(f"[{name}] origin shift {tag} (step20) ...", flush=True)
        out[tag] = _score_step20(model, load_n, element_size_mm, min_width_mm, shift)

    hashes = {tag: r["mask_hash"] for tag, r in out.items()}
    materials = {tag: r["material_mm3"] for tag, r in out.items()}
    base_hash = hashes["0"]
    base_material = materials["0"]
    topology_changed = {tag: (h != base_hash) for tag, h in hashes.items()}
    material_rel_change = {tag: (None if base_material in (None, 0) or m is None
                                 else (m - base_material) / base_material)
                           for tag, m in materials.items()}

    # 適応方式でも基準・全軸+0.5の2点だけ追加確認(製品コードだけでなく新方式の原点感度も見る)。
    print(f"[{name}] origin shift 0 (adaptive) ...", flush=True)
    ctx0 = sup.build_context_with_origin_shift(model, load_n=load_n, element_size_mm=element_size_mm,
                                                min_width_mm=min_width_mm, shift_mm=(0.0, 0.0, 0.0))
    adaptive0 = sm.run_adaptive(ctx0, sup.revert_config(seed=300, max_evaluations=40))
    print(f"[{name}] origin shift +0.5all (adaptive) ...", flush=True)
    shift_all = _shift_table(element_size_mm)["+0.5all"]
    ctx1 = sup.build_context_with_origin_shift(model, load_n=load_n, element_size_mm=element_size_mm,
                                                min_width_mm=min_width_mm, shift_mm=shift_all)
    adaptive1 = sm.run_adaptive(ctx1, sup.revert_config(seed=300, max_evaluations=40))

    return dict(step20_by_shift=out, topology_changed_vs_base=topology_changed,
               material_relative_change_vs_base=material_rel_change,
               adaptive_shift0=sup.record_summary(adaptive0), adaptive_shift_plus0_5all=sup.record_summary(adaptive1))


def filter_width_sweep(name: str, model, load_n: float, min_width_mm: float, base_spacing_mm: float,
                       resolutions_mm: list[float]) -> dict:
    """`ENERGY_FILTER_SIZE`(既定3セル)のセル数固定と、baseline解像度の物理幅(3セル×base_spacing)を
    mmで固定した版を、複数解像度で比較する。"""
    target_mm = 3 * base_spacing_mm
    out = {}
    for spacing in resolutions_mm:
        ctx = sm.build_context(model, load_n=load_n, element_size_mm=spacing, min_width_mm=min_width_mm)
        print(f"[{name}] filter width @ {spacing}mm: cell-fixed(3cells) ...", flush=True)
        cell_fixed = sm.run_fixed_step(ctx, max_step_ratio=0.2)  # 既定ENERGY_FILTER_SIZE=3セルのまま
        mm_cells = sup.filter_size_cells_for_mm(target_mm, ctx.grid.spacing)
        print(f"[{name}] filter width @ {spacing}mm: mm-fixed({target_mm}mm -> {mm_cells}cells) ...", flush=True)
        with sup.patched_filter_size(mm_cells):
            mm_fixed = sm.run_fixed_step(ctx, max_step_ratio=0.2)
        out[f"{spacing}mm"] = dict(
            element_size_resolved_mm=ctx.grid.spacing,
            cell_fixed_3cells_physical_mm=3 * ctx.grid.spacing,
            mm_fixed_target_mm=target_mm, mm_fixed_resolved_cells=mm_cells,
            mm_fixed_resolved_physical_mm=mm_cells * ctx.grid.spacing,
            cell_fixed_result=sup.record_summary(cell_fixed),
            mm_fixed_result=sup.record_summary(mm_fixed),
        )
    return out


def main() -> int:
    sup.MASK_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state())

    with _common.ExecutionLock(sup.LOCK, tag="run_05_grid_origin"):
        print("=== dilution origin sweep ===", flush=True)
        payload["dilution_origin_sweep"] = origin_sweep("dilution", sup.DILUTION_MODEL, 300.0, 2.0, 4.0)
        print("=== path origin sweep ===", flush=True)
        payload["path_origin_sweep"] = origin_sweep("path", sup.PATH_MODEL, 3000.0, 2.0, 6.0)

        print("=== dilution filter width sweep ===", flush=True)
        payload["dilution_filter_width_sweep"] = filter_width_sweep(
            "dilution", sup.DILUTION_MODEL, 300.0, 6.0, 2.0, [1.5, 2.0, 2.5, 3.0])
        print("=== path filter width sweep ===", flush=True)
        payload["path_filter_width_sweep"] = filter_width_sweep(
            "path", sup.PATH_MODEL, 3000.0, 6.0, 2.0, [1.5, 2.0, 2.5, 3.0])

    out_path = OUT / "05_grid_origin.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

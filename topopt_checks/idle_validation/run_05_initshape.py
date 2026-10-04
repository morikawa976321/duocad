"""タスク5・初期形状の比較(01_PROTOCOL.md 6-2節): 全材料(製品既定)以外の初期形状から
適応方式/再配置を開始した場合に、局所解(原因2)を避けられるか・結果が変わるかを見る
(タスク指示「途中形状…部材復元…の履歴を保存し…枝が荷重経路になるまで評価されない等の失敗を検査する」
の一部、および01_PROTOCOL 6-2「ウォームスタートが原因2を避けられるか」)。

2種類のwarm-start源:
- **粗い格子から写す形**: `run_05_main_matrix.py` が3.0mm解像度で見つけたstep20の採用マスクを、
  baseline解像度(2.0mm)の設計空間へ投影する(`refine_shape._project_mask`)。
- **04aの優越形**(pathのみ): `output/idle_validation_20260928/04a_masks/qualified_path_2.0mm.npz`。
  既にbaseline解像度(2.0mm)なので投影不要——「経路喪失から一度抜けた形」から続けても
  `run_adaptive`/`run_reallocation`がそこに留まるか、別の局所解へ落ちるかを見る。

投影後のマスクは `resolve_islands`→最小幅検査で構造的妥当性を検査する(`run_adaptive_from_mask`は
これを再検査しない契約——04_SEARCHES.md §後続への契約①)。不合格なら `status=warm_start_infeasible`
として記録し、無理に使わない。

出力: `output/idle_validation_20260928/05_initshape.json`
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

from duocad.topopt import binary_search  # noqa: E402

import _common  # noqa: E402
import refine_shape  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

OUT = sup.OUT
MAIN_MATRIX = OUT / "05_main_matrix.json"


def _prepare_start_mask(src_mask: np.ndarray, src_origin, src_spacing: float, ctx: sm.SearchContext) -> dict:
    projected = refine_shape._project_mask(src_mask, src_origin, src_spacing, ctx.space) & ctx.space.mask
    cond = ctx.cond
    keep = np.asarray(getattr(cond, "keep_cells", None), dtype=bool) if getattr(cond, "keep_cells", None) is not None \
        else np.zeros_like(projected)
    projected = projected | (keep & ctx.space.mask)
    survivors, diag = binary_search.resolve_islands(projected, cond)
    if not survivors:
        return dict(status="disconnected", diagnostics={k: v for k, v in diag.items() if k != "labels"})
    mask = max(survivors, key=lambda c: c.sum())
    exempt = binary_search._min_width_exempt(cond, mask.shape, ctx.min_width_cells)
    ok = binary_search._mask_satisfies_min_width(mask, ctx.min_width_cells, exempt=exempt)
    dilations = 0
    while not ok and dilations < 3:
        mask = ndimage.binary_dilation(mask, structure=sm.NEIGHBOURHOOD_STRUCTURE) & ctx.space.mask
        survivors2, diag2 = binary_search.resolve_islands(mask, cond)
        if not survivors2:
            return dict(status="disconnected_after_dilation", dilations=dilations + 1)
        mask = max(survivors2, key=lambda c: c.sum())
        ok = binary_search._mask_satisfies_min_width(mask, ctx.min_width_cells, exempt=exempt)
        dilations += 1
    if not ok:
        return dict(status="min_width_unsatisfiable", dilations=dilations)
    return dict(status="ok", mask=mask, dilations=dilations,
               material_mm3=float(mask.sum() * ctx.grid.element_volume))


def _run_from_mask(ctx, mask, *, seed, max_evaluations, tag) -> dict:
    adaptive_cfg = sup.revert_config(seed=seed, max_evaluations=max_evaluations)
    adaptive = sm.run_adaptive_from_mask(ctx, mask, adaptive_cfg)
    # 再配置は「run_adaptiveの削減→追加」の内部から始めるため、専用のwarm-start入口が無い
    # (04_SEARCHES.md §依存してはいけないもの: run_reallocationは常にfind_initial_feasible相当の
    # run_adaptiveから始める設計)。ここでは`_run_adaptive_from_mask`の削減結果を再配置の「削減フェーズ
    # 結果」として代用せず、warm-start自体の効果は`run_adaptive_from_mask`(方式3相当)でのみ測る
    # (方式4への拡張はスコープ外として限界に明記する)。
    return dict(adaptive=sup.record_summary(adaptive), adaptive_full=adaptive.to_json(mask_dir=sup.MASK_DIR, tag=tag))


def load_mask_source(case_id: str) -> dict:
    with np.load(sup.MASK_DIR / f"{case_id}_step20_best_ever.npz") as z:
        mask = np.asarray(z["mask"], dtype=bool)
    return mask


def main() -> int:
    sup.MASK_DIR.mkdir(parents=True, exist_ok=True)
    if not MAIN_MATRIX.exists():
        raise FileNotFoundError(f"{MAIN_MATRIX} が無い。先に run_05_main_matrix.py を完走させること")
    main_matrix = json.loads(MAIN_MATRIX.read_text(encoding="utf-8"))
    cases_by_id = {c["case_id"]: c for c in main_matrix["cases"]}

    payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state())

    with _common.ExecutionLock(sup.LOCK, tag="run_05_initshape"):
        # --- dilution: 3.0mm step20結果を2.0mmへ投影 ---
        print("=== dilution warm-start from 3.0mm mesh ===", flush=True)
        src_case = cases_by_id.get("dilution_res3.0_w6")
        ctx_d = sm.build_context(sup.DILUTION_MODEL, load_n=300.0, element_size_mm=2.0, min_width_mm=4.0)
        if src_case and src_case.get("status") == "ok":
            src_mask = load_mask_source("dilution_res3.0_w6")
            # `_project_mask`はワールド座標で対応付けるため、投影元(3.0mm)自身のorigin/spacingが要る
            # (`_snap_to_grid`で解像度ごとにbboxへ合わせてoriginがずれる。2.0mm文脈のoriginを流用すると
            # 誤って位置がずれた投影になる——実測で発見: 2.0mm origin(-16,-16,-16) vs 3.0mm origin
            # (-16.97,-17.70,-17.76))。
            ctx_d_src = sm.build_context(sup.DILUTION_MODEL, load_n=300.0, element_size_mm=3.0, min_width_mm=6.0)
            prep = _prepare_start_mask(src_mask, ctx_d_src.space.grid.origin, ctx_d_src.grid.spacing, ctx_d)
            entry = dict(source="dilution_res3.0_w6_step20", prep_status=prep["status"])
            if prep["status"] == "ok":
                entry.update(prep_material_mm3=prep["material_mm3"], dilations=prep["dilations"],
                            **_run_from_mask(ctx_d, prep["mask"], seed=400, max_evaluations=60,
                                            tag="dilution_warmstart_coarse"))
            payload["dilution_warmstart_coarse_mesh"] = entry
        else:
            payload["dilution_warmstart_coarse_mesh"] = dict(status="source_case_unavailable")

        # --- path: 04aの優越形(既にbaseline解像度、投影不要) ---
        print("=== path warm-start from 04a qualified form ===", flush=True)
        ctx_p = sm.build_context(sup.PATH_MODEL, load_n=3000.0, element_size_mm=2.0, min_width_mm=6.0)
        if sup.PATH_QUALIFIED_MASK_NPZ.exists():
            q_mask = sup.load_mask_npz(sup.PATH_QUALIFIED_MASK_NPZ)
            if q_mask.shape == tuple(ctx_p.space.mask.shape):
                entry = dict(source="04a_qualified_path_2.0mm", prep_status="ok_no_projection_needed")
                entry.update(**_run_from_mask(ctx_p, q_mask & ctx_p.space.mask, seed=401, max_evaluations=60,
                                             tag="path_warmstart_04a_qualified"))
                payload["path_warmstart_04a_qualified"] = entry
            else:
                payload["path_warmstart_04a_qualified"] = dict(
                    status="shape_mismatch", qualified_shape=list(q_mask.shape),
                    ctx_shape=list(ctx_p.space.mask.shape))
        else:
            payload["path_warmstart_04a_qualified"] = dict(status="source_missing")

        # --- path: 3.0mm step20結果を2.0mmへ投影 ---
        print("=== path warm-start from 3.0mm mesh ===", flush=True)
        src_case_p = cases_by_id.get("path_res3.0_w6")
        if src_case_p and src_case_p.get("status") == "ok":
            src_mask_p = load_mask_source("path_res3.0_w6")
            # dilution側と同じ理由でsrc(3.0mm)自身のcontextからorigin/spacingを取る。
            ctx_p_src = sm.build_context(sup.PATH_MODEL, load_n=3000.0, element_size_mm=3.0, min_width_mm=6.0)
            prep_p = _prepare_start_mask(src_mask_p, ctx_p_src.space.grid.origin, ctx_p_src.grid.spacing, ctx_p)
            entry = dict(source="path_res3.0_w6_step20", prep_status=prep_p["status"])
            if prep_p["status"] == "ok":
                entry.update(prep_material_mm3=prep_p["material_mm3"], dilations=prep_p["dilations"],
                            **_run_from_mask(ctx_p, prep_p["mask"], seed=402, max_evaluations=60,
                                            tag="path_warmstart_coarse"))
            payload["path_warmstart_coarse_mesh"] = entry
        else:
            payload["path_warmstart_coarse_mesh"] = dict(status="source_case_unavailable")

    out_path = OUT / "05_initshape.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

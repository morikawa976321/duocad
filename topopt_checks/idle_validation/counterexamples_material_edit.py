"""必須反例(b): 実形状の低寄与部分を除去/追加して再解析する(場だけでなく幾何・力学として
成立する変更。masks を編集してから `metric_eval.solve_shape` で実際にFEMを解き直す)。

対象: `dilution_r-1`@300N(基準形状)。
- 除去: 保持セル(`keep_cells`)以外の材料のうち、von Mises が低い順に約15%を削り、連結成分の中で
  保持セルを含む最大のものを残す(`binary_search.resolve_islands` を通す——製品の探索と同じ規則)。
- 追加: 現在の材料マスクを設計空間内へ1セル膨張し、同じ形へ材料を足す(低応力域を厚くする)。

出力: `output/idle_validation_20260928/counterexamples_material_edit.json`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import _common  # noqa: E402
import metric_eval as me  # noqa: E402
import refine_shape as rs  # noqa: E402
import stress_metrics as sm  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"


def _score(result: dict) -> dict:
    vm, mask, zones = result["von_mises_mpa"], result["mask"], result["zones"]
    allowable, spacing = result["allowable_mpa"], result["element_mm"]
    zone_dict = {"holding": zones["holding"], "joint": zones["joint"], "general": zones["general"]}
    return dict(
        full_population_percentile=sm.metric_full_population_percentile(vm, mask, allowable_mpa=allowable).__dict__,
        excluded_population_percentile=sm.metric_excluded_population_percentile(
            vm, mask, zones["holding"], allowable_mpa=allowable).__dict__,
        zone_percentile=sm.metric_zone_percentile(vm, zone_dict, allowable_mpa=allowable).__dict__,
        top_volume_rank__216mm3=sm.metric_top_volume_rank(
            vm, mask, spacing_mm=spacing, top_volume_mm3=216.0, allowable_mpa=allowable).__dict__,
        exceedance_volume=sm.metric_exceedance_volume(vm, mask, spacing_mm=spacing, allowable_mpa=allowable).__dict__,
        exceedance_integral=sm.metric_exceedance_integral(vm, mask, spacing_mm=spacing, allowable_mpa=allowable).__dict__,
        p_norm_unnormalized=sm.metric_p_norm_unnormalized(vm, mask, p=8.0, allowable_mpa=allowable).__dict__,
    )


def _solve_and_score(shape_cfg: dict, load_n: float, tag: str) -> dict:
    result = me.solve_shape(shape_cfg, load_n, 1)
    if result["status"] != "ok":
        return dict(tag=tag, status=result["status"], error=result.get("error"))
    return dict(
        tag=tag, status="ok", material_elements=result["material_elements"],
        safety_factor_product=result["safety_factor"], representative_mpa_product=result["representative_mpa"],
        metrics=_score(result),
    )


def main() -> int:
    load_n = 300.0
    base_cfg = rs._resolve_shape("dilution_r-1", None)
    base = me.solve_shape(base_cfg, load_n, 1)
    assert base["status"] == "ok"
    cond = base["cond"]
    keep = np.asarray(cond.keep_cells, dtype=bool)
    mask = base["mask"]
    design_mask = base["design_mask"]
    vm = base["von_mises_mpa"]

    # 除去可能セルを一般部(固定面・荷重面から離れた領域)に限る——保持部・接続部の境界セルを
    # 削ると `_holds_all_conditions` の面被覆判定(fixed_faces/load_faces の節点欠落禁止)に
    # 抵触しやすく、機械的に成立しない候補になりやすいと実測で分かった(一般部の外周から削っても
    # 同じ「低寄与部分の除去」を検証できるため、そちらへ寄せる)。
    removable = base["zones"]["general"] & ~keep
    removable_idx = np.argwhere(removable)
    removable_values = vm[removable]
    order = np.argsort(removable_values)  # 昇順(低応力から)
    n_remove = int(round(0.20 * removable_idx.shape[0]))
    remove_idx = removable_idx[order[:n_remove]]

    reduced_mask = mask.copy()
    reduced_mask[tuple(remove_idx.T)] = False
    labels, n_labels = ndimage.label(reduced_mask, structure=np.ones((3, 3, 3), dtype=bool))
    keep_labels = set(labels[keep & reduced_mask].tolist()) - {0}
    survivor = np.isin(labels, list(keep_labels)) if keep_labels else np.zeros_like(reduced_mask)

    added_mask = ndimage.binary_dilation(mask, iterations=1) & design_mask

    results = {}
    results["baseline"] = _solve_and_score(base_cfg, load_n, "baseline")
    if survivor.any():
        removed_cfg = dict(base_cfg, mask=survivor)
        results["removed_low_contribution_20pct"] = _solve_and_score(removed_cfg, load_n, "removed")
        results["removed_low_contribution_20pct"]["removed_cell_count"] = int(n_remove)
        results["removed_low_contribution_20pct"]["surviving_material_after_island_filter"] = int(survivor.sum())
    else:
        results["removed_low_contribution_20pct"] = dict(status="disconnected_before_fem",
                                                          error="保持セルを含む連結成分が残らなかった")

    added_cfg = dict(base_cfg, mask=added_mask)
    results["added_dilate_1cell"] = _solve_and_score(added_cfg, load_n, "added")
    results["added_dilate_1cell"]["added_cell_count"] = int((added_mask & ~mask).sum())

    payload = dict(
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        base_case=dict(shape="dilution_r-1", load_n=load_n, level=1, material_elements=int(mask.sum()),
                        keep_cell_count=int(keep.sum())),
        results=results,
    )
    out_path = OUT / "counterexamples_material_edit.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    for tag, r in results.items():
        print(tag, r.get("status"), r.get("safety_factor_product"), r.get("material_elements"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""必須反例のうち「場だけの操作」の3件(タスク指示の箇条書き): (a)応力場固定で遠方の低応力セルを
増減、(d)ピークを小領域へ集中、(e)複数hotspot間の移し替え。

**実際のFEMは解かない。** 実在の1解(`dilution_r-1`@300N)の von Mises 配列を取り、配列だけを
人為的に書き換えて「もし材料配置がこうだったら評価値はどう動くか」を見る——幾何や力学として
成立するかは検査しない、指標の数式そのものの脆弱性を暴くための操作(タスク指示「場だけの操作と
幾何/力学成立した例を区別する」)。幾何/力学的に成立する反例は `run_metric_comparison.py`
(格子細分・独立探索の複数解像度)と `counterexamples_material_edit.py`(低寄与部分の除去/追加+
再解析)・`counterexamples_origin_shift.py`(格子原点移動)が担う。

出力: `output/idle_validation_20260928/counterexamples_field_only.json`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

import _common  # noqa: E402
import metric_eval as me  # noqa: E402
import refine_shape as rs  # noqa: E402
import stress_metrics as sm  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"


def _score(vm: np.ndarray, mask: np.ndarray, allowable: float, spacing: float,
           exclude: np.ndarray | None = None) -> dict:
    out = {}
    out["full_population_percentile"] = sm.metric_full_population_percentile(vm, mask, allowable_mpa=allowable).__dict__
    if exclude is not None:
        out["excluded_population_percentile"] = sm.metric_excluded_population_percentile(
            vm, mask, exclude, allowable_mpa=allowable).__dict__
    out["top_volume_rank__216mm3"] = sm.metric_top_volume_rank(
        vm, mask, spacing_mm=spacing, top_volume_mm3=216.0, allowable_mpa=allowable).__dict__
    out["top_volume_average__216mm3"] = sm.metric_top_volume_average(
        vm, mask, spacing_mm=spacing, top_volume_mm3=216.0, allowable_mpa=allowable).__dict__
    out["exceedance_volume"] = sm.metric_exceedance_volume(vm, mask, spacing_mm=spacing, allowable_mpa=allowable).__dict__
    out["exceedance_integral"] = sm.metric_exceedance_integral(vm, mask, spacing_mm=spacing, allowable_mpa=allowable).__dict__
    out["p_mean"] = sm.metric_p_mean(vm, mask, p=8.0, allowable_mpa=allowable).__dict__
    out["p_norm_unnormalized"] = sm.metric_p_norm_unnormalized(vm, mask, p=8.0, allowable_mpa=allowable).__dict__
    out["ks_aggregation"] = sm.metric_ks_aggregation(vm, mask, rho=1.0, allowable_mpa=allowable).__dict__
    return out


def counterexample_a_far_low_stress(base_values: np.ndarray, allowable: float, spacing: float) -> dict:
    """(a) 応力場固定で遠方の低応力セルを増減。実材料の値はそのまま、末尾に「遠方」(=一般部相当、
    holding除外の対象外)の低応力セル N 個を継ぎ足すだけで各指標がどう動くか。
    """
    phantom_value = 0.05 * allowable  # 一般部の典型値程度(実測: 02_REFERENCEの一般部maxは概ね数MPa)
    mask_real = np.ones(base_values.shape, dtype=bool)
    exclude_none = np.zeros(base_values.shape, dtype=bool)
    sweeps = {}
    for n_extra in (0, len(base_values), 5 * len(base_values), 20 * len(base_values)):
        phantom = np.full(n_extra, phantom_value, dtype=float)
        vm = np.concatenate([base_values, phantom])
        mask = np.concatenate([mask_real, np.ones(n_extra, dtype=bool)])
        exclude = np.concatenate([exclude_none, np.zeros(n_extra, dtype=bool)])
        sweeps[str(n_extra)] = dict(
            n_extra=n_extra, total_population=int(mask.sum()),
            metrics=_score(vm, mask, allowable, spacing, exclude=exclude),
        )
    return sweeps


def counterexample_d_peak_concentration(base_values: np.ndarray, allowable: float, spacing: float) -> dict:
    """(d) ピークを小領域へ集中。超過量の総和(積分)を固定したまま、それを分散させるセル数を
    1 / 5 / 20 / 100 と変えて top-K系・exceedance系・p系がどう動くか比べる。
    """
    total_excess = 40.0  # MPa·個(超過量の合計。allowable超過分の総和を固定)
    background = base_values.copy()
    background[background > allowable] = allowable * 0.9  # 元の超過セルは一旦「危険でない」水準へ戻す
    sweeps = {}
    for n_cells in (1, 5, 20, 100):
        vm = background.copy()
        per_cell_excess = total_excess / n_cells
        # 最も低応力な(=どの指標にも既に拾われている心配がない)セルへ超過を注入する。
        idx = np.argsort(vm)[:n_cells]
        vm[idx] = allowable + per_cell_excess
        mask = np.ones(vm.shape, dtype=bool)
        sweeps[str(n_cells)] = dict(
            n_cells=n_cells, per_cell_excess_mpa=per_cell_excess, total_excess_mpa=total_excess,
            metrics=_score(vm, mask, allowable, spacing),
        )
    return sweeps


def counterexample_e_hotspot_transfer(
    base_values: np.ndarray, holding_mask: np.ndarray, joint_mask: np.ndarray, general_mask: np.ndarray,
    allowable: float, spacing: float,
) -> dict:
    """(e) 複数hotspot間の移し替え。同じ大きさの「危険な1セル」(許容応力を超える値)を、holding/
    joint/general のどのゾーンに置くかだけを変える。大域だけを見る指標(M1・M5・M7)は置き場所を
    区別できず、部位別指標(M3)だけが「どのゾーンが危険か」を正しく移し替えに追従するはずかを
    実測する。

    `holding_mask`・`joint_mask`・`general_mask` は `base_values` と同じ並びのbool配列であること。
    """
    if not holding_mask.any() or not general_mask.any() or not joint_mask.any():
        return {"skipped": "holding/joint/general zone のいずれかが空"}
    danger_value = allowable * 1.25  # 許容応力を25%超える1セル(現実的な局所超過の大きさ)
    zone_masks = dict(holding=holding_mask, joint=joint_mask, general=general_mask)
    mask = np.ones(base_values.shape, dtype=bool)

    scenarios = {}
    scenarios["baseline_no_danger_cell"] = dict(
        danger_zone=None, metrics=_score(base_values, mask, allowable, spacing),
        zone_metric=sm.metric_zone_percentile(base_values, zone_masks, allowable_mpa=allowable).__dict__,
    )
    for zone_name, zmask in zone_masks.items():
        vm = base_values.copy()
        # そのゾーン内で最も低応力なセル(=どの指標にも既に拾われていないセル)へ危険値を注入する。
        idx_local = np.argmin(vm[zmask])
        idx = np.where(zmask)[0][idx_local]
        vm[idx] = danger_value
        scenarios[f"danger_in_{zone_name}"] = dict(
            danger_zone=zone_name, injected_index=int(idx), injected_value_mpa=danger_value,
            metrics=_score(vm, mask, allowable, spacing),
            zone_metric=sm.metric_zone_percentile(vm, zone_masks, allowable_mpa=allowable).__dict__,
        )
    return dict(danger_value_mpa=danger_value, scenarios=scenarios)


def main() -> int:
    shape_cfg = rs._resolve_shape("dilution_r-1", None)
    base = me.solve_shape(shape_cfg, 300.0, 1)
    assert base["status"] == "ok"
    vm3d = base["von_mises_mpa"]
    mask3d = base["mask"]
    base_values = vm3d[mask3d]
    allowable = base["allowable_mpa"]
    spacing = base["element_mm"]

    payload = dict(
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        base_case=dict(shape="dilution_r-1", load_n=300.0, level=1, material_elements=int(base_values.size),
                        allowable_mpa=allowable, spacing_mm=spacing),
        a_far_low_stress_cells=counterexample_a_far_low_stress(base_values, allowable, spacing),
        d_peak_concentration=counterexample_d_peak_concentration(base_values, allowable, spacing),
        e_hotspot_transfer=counterexample_e_hotspot_transfer(
            base_values, base["zones"]["holding"][mask3d], base["zones"]["joint"][mask3d],
            base["zones"]["general"][mask3d], allowable, spacing,
        ),
    )
    out_path = OUT / "counterexamples_field_only.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

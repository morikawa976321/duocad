"""段階1(評価式選定)の主比較: 保存済みの実形状群を共通の評価パイプラインで解き、
`stress_metrics.py` の全候補式を同じ場に対して計算して1つの比較表にする。

対象(すべて実際にFEMを解き直す。合成場ではない):
- dilution: 母数反実仮想R-sweepの5形状(r=-1..3、task00がsearchを回した副産物)× 200N/300N
- path: 独立探索4解像度(1.5/2.0/2.5/3.0mm、task00の副産物)× 1500N/6000N
- path: 固定形状(2.5mm採用)の格子細分4段階(task02と同じ、ただし生の場を保持)× 1500N/6000N
- dilution: 固定形状(R=-1採用)の格子細分3段階(task02と同じ)× 200N/300N
- hanger: H1(hanger_base・200N)/H2(hanger_r2・2000N、ネイティブ解像度のみ)

出力: `output/idle_validation_20260928/metric_comparison.json`(cases配列。1 case = 1形状×1荷重×1レベル
に対する全候補式の値・合否)。

再現: `.\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\run_metric_comparison.py`
"""
from __future__ import annotations

import json
import sys
import time
import traceback
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

# 物理的に固定した体積窓(母数と無関係)。M4の説明変数として2点を併走させる——
# 8mm^3はdilution/pathの解析メッシュ1〜2セル分に相当する小さい窓、216mm^3はこの2模型で使った
# 最小幅6.0mm(00_COMPACT_MODELS.md)の立方=「単一の最小構造要素が壊れる」規模の窓。
TOP_VOLUME_WINDOWS_MM3 = {"tiny_8mm3": 8.0, "min_width_cube_216mm3": 216.0}
P_EXPONENT = 8.0
KS_RHO = 1.0  # 1/MPa。値のスケール(数〜数百MPa)に対して vmax±数MPa の差を分離できる大きさ。


def _score_all_metrics(result: dict) -> dict:
    vm = result["von_mises_mpa"]
    mask = result["mask"]
    zones = result["zones"]
    spacing = result["element_mm"]
    allowable = result["allowable_mpa"]
    percentile = result["percentile"]

    out: dict = {}
    out["full_population_percentile"] = sm.metric_full_population_percentile(
        vm, mask, percentile=percentile, allowable_mpa=allowable).__dict__
    out["excluded_population_percentile"] = sm.metric_excluded_population_percentile(
        vm, mask, zones["holding"], percentile=percentile, allowable_mpa=allowable).__dict__
    out["zone_percentile"] = sm.metric_zone_percentile(
        vm, {"holding": zones["holding"], "joint": zones["joint"], "general": zones["general"]},
        percentile=percentile, allowable_mpa=allowable).__dict__
    for tag, vol in TOP_VOLUME_WINDOWS_MM3.items():
        out[f"top_volume_rank__{tag}"] = sm.metric_top_volume_rank(
            vm, mask, spacing_mm=spacing, top_volume_mm3=vol, allowable_mpa=allowable).__dict__
        out[f"top_volume_average__{tag}"] = sm.metric_top_volume_average(
            vm, mask, spacing_mm=spacing, top_volume_mm3=vol, allowable_mpa=allowable).__dict__
    out["exceedance_volume"] = sm.metric_exceedance_volume(
        vm, mask, spacing_mm=spacing, allowable_mpa=allowable).__dict__
    out["exceedance_integral"] = sm.metric_exceedance_integral(
        vm, mask, spacing_mm=spacing, allowable_mpa=allowable).__dict__
    out["p_mean"] = sm.metric_p_mean(vm, mask, p=P_EXPONENT, allowable_mpa=allowable).__dict__
    out["p_norm_unnormalized"] = sm.metric_p_norm_unnormalized(
        vm, mask, p=P_EXPONENT, allowable_mpa=allowable).__dict__
    out["ks_aggregation"] = sm.metric_ks_aggregation(
        vm, mask, rho=KS_RHO, allowable_mpa=allowable).__dict__
    return out


def _run_case(shape_cfg: dict, load_n: float, level: int, mask_source: str = "") -> dict:
    t0 = time.time()
    try:
        result = me.solve_shape(shape_cfg, load_n, level)
    except Exception as exc:
        return dict(status="exception", shape=shape_cfg["name"], load_n=load_n, level=level,
                    mask_source=mask_source, error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc())
    if result["status"] != "ok":
        result["mask_source"] = mask_source
        result["elapsed_seconds"] = time.time() - t0
        return {k: v for k, v in result.items() if k not in ("mask", "grid", "table", "cond")}

    metrics = _score_all_metrics(result)
    record = dict(
        status="ok",
        case_id=_common.condition_id(shape=result["shape"], load_n=load_n, level=level, mask_source=mask_source),
        shape=result["shape"],
        mask_source=mask_source,
        load_n=load_n,
        level=level,
        element_mm=result["element_mm"],
        design_elements=result["design_elements"],
        material_elements=result["material_elements"],
        allowable_mpa=result["allowable_mpa"],
        percentile=result["percentile"],
        distance_mm=result["distance_mm"],
        zone_counts=result["zones"]["counts"],
        safety_factor_product=result["safety_factor"],
        representative_mpa_product=result["representative_mpa"],
        raw_max_mpa=result["raw_max_mpa"],
        metrics=metrics,
        elapsed_seconds=time.time() - t0,
    )
    return record


def _dilution_cases() -> list[dict]:
    cases = []
    for r in [-1, 0, 1, 2, 3]:
        npz = OUT / f"dilution_mask_r{r}.npz"
        if not npz.is_file():
            continue
        data = np.load(npz)
        base_cfg = rs._resolve_shape("dilution_r-1", None)
        cfg = dict(base_cfg, mask=data["mask"], origin=data["origin"], spacing=float(data["spacing"]))
        for load_n in (200.0, 300.0):
            cases.append((cfg, load_n, 1, f"counterfactual_r{r}"))
    return cases


def _path_mesh_cases() -> list[dict]:
    cases = []
    for tag, size in [("1.5mm", 1.5), ("2.0mm", 2.0), ("2.5mm", 2.5), ("3.0mm", 3.0)]:
        npz = OUT / f"path_mask_mesh_{tag}.npz"
        if not npz.is_file():
            continue
        data = np.load(npz)
        base_cfg = rs._resolve_shape("path_2.5mm", None)
        cfg = dict(base_cfg, mask=data["mask"], origin=data["origin"], spacing=float(data["spacing"]),
                   keep=data["keep"] if "keep" in data.files else None)
        for load_n in (1500.0, 6000.0):
            cases.append((cfg, load_n, 1, f"independent_search_{tag}"))
    return cases


def _refinement_cases() -> list[dict]:
    cases = []
    dilution_cfg = rs._resolve_shape("dilution_r-1", None)
    for level in (1, 2, 3):
        for load_n in (200.0, 300.0):
            cases.append((dilution_cfg, load_n, level, f"fixed_shape_refine_level{level}"))
    path_cfg = rs._resolve_shape("path_2.5mm", None)
    for level in (1, 2, 3, 4):
        for load_n in (1500.0, 6000.0):
            cases.append((path_cfg, load_n, level, f"fixed_shape_refine_level{level}"))
    return cases


def _hanger_cases() -> list[dict]:
    cases = []
    h1 = rs._resolve_shape("hanger_base", None)
    cases.append((h1, 200.0, 1, "hanger_H1_native"))
    h2 = rs._resolve_shape("hanger_r2", "F2000_h2.4_w2")
    cases.append((h2, 2000.0, 1, "hanger_H2_native"))
    return cases


def main() -> int:
    groups = [
        ("dilution_counterfactual", _dilution_cases()),
        ("path_independent_search", _path_mesh_cases()),
        ("fixed_shape_refinement", _refinement_cases()),
        ("hanger_external_check", _hanger_cases()),
    ]

    ledger_path = OUT / "03_metric_comparison_ledger.json"
    ledger = _common.load_ledger(ledger_path)
    records = []
    for group_name, cases in groups:
        for cfg, load_n, level, mask_source in cases:
            record = _run_case(cfg, load_n, level, mask_source=mask_source)
            record["group"] = group_name
            records.append(record)
            case_id = record.get("case_id") or f"{group_name}:{mask_source}:{load_n}:{level}"
            ledger = _common.upsert_case(ledger, case_id, record)
            ledger["generated_at"] = _common.now_iso()
            _common.write_atomic(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
            print(json.dumps({k: v for k, v in record.items() if k != "traceback"}, ensure_ascii=False, default=float)[:400])

    out_path = OUT / "metric_comparison.json"
    payload = dict(
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        parameters=dict(
            top_volume_windows_mm3=TOP_VOLUME_WINDOWS_MM3, p_exponent=P_EXPONENT, ks_rho=KS_RHO,
        ),
        cases=records,
    )
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path} ({len(records)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

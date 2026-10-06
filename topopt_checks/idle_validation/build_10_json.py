"""タスク10: 07〜09(と04〜06)の保存台帳から、統合報告に載せる数値を再計算して `10_synthesis.json` の `derived` を作る。

新しいFEM・最適化は走らせない(保存済みJSONの再集計と算術だけ)。報告書本文の数値が台帳と合うかの照合
(`discrepancy_checks`)も、ここで台帳から再計算して持つ。`audit`(指摘の分類)・`recommendations`・`coverage`・
`deferred_by_user_deadline`・`limitations` は `synthesis_10_static.json`(手書きの判断。台帳から出せないもの)から読む。
再生成: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\build_10_json.py`
出力: `output/idle_validation_20260928/10_synthesis.json`
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _common  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"
STATIC = Path(__file__).with_name("synthesis_10_static.json")
TARGET_SF = 4.8
SF_TOL = 0.005  # 製品SFの「目標の0.5%以内」(09の受理判定と同じ)
ALLOWABLE_MPA = 40.0


def load(name: str):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def rel(a: float, b: float) -> float:
    return a / b - 1.0


# ---------------------------------------------------------------------------
# 05: 小型15条件x4方式
# ---------------------------------------------------------------------------
def small_matrix(syn: dict) -> dict:
    rows = []
    for c in syn["cases"]:
        if c["kind"] != "four_method_comparison":
            continue
        s20 = c["step20"]
        row = dict(case_id=c["case_id"], cause=c["cause"], axis=c["axis"], load_n=c["load_n"],
                   element_size_mm=c["element_size_mm"], min_width_mm_requested=c["min_width_mm_requested"],
                   step20_mm3=s20["material_mm3"], step20_sf=s20["safety_factor_product"], step20_wall_s=s20["wall_seconds"])
        for m in ("step05", "adaptive", "reallocation"):
            r = c[m]
            row[m] = dict(mm3=r["material_mm3"], vs_step20=rel(r["material_mm3"], s20["material_mm3"]),
                          sf=r["safety_factor_product"], sf_target_met=r["safety_factor_product"] >= TARGET_SF * (1 - SF_TOL),
                          m5b_target_passed=r["m5b_target_passed"], wall_s=r["wall_seconds"],
                          wall_vs_step20=r["wall_seconds"] / s20["wall_seconds"], fem_solves=r["fem_solves"],
                          stop_reason=r["stop_reason"])
        rows.append(row)
    summary = {}
    for cause in ("dilution", "path"):
        rs = [r for r in rows if r["cause"] == cause]
        for m in ("step05", "adaptive", "reallocation"):
            deltas = [r[m]["vs_step20"] for r in rs]
            summary[f"{cause}.{m}"] = dict(
                n=len(rs), lighter=sum(d < -1e-9 for d in deltas), heavier=sum(d > 1e-9 for d in deltas),
                min_delta=min(deltas), max_delta=max(deltas), median_delta=statistics.median(deltas),
                sf_below_target=sum(not r[m]["sf_target_met"] for r in rs),
                lighter_and_sf_met=[(r["case_id"], round(r[m]["vs_step20"], 4)) for r in rs
                                    if r[m]["vs_step20"] < -1e-9 and r[m]["sf_target_met"]],
                median_wall_ratio=statistics.median(r[m]["wall_vs_step20"] for r in rs),
                max_wall_ratio=max(r[m]["wall_vs_step20"] for r in rs))
    return dict(rows=rows, summary=summary)


# ---------------------------------------------------------------------------
# 07: hanger92形のゲート別集計と、製品SFゲートが見ない部位別最悪応力
# ---------------------------------------------------------------------------
def hanger_gates(h7: dict) -> dict:
    shapes = h7["corpus_rescore"] + h7["grid"]
    by_load = {}
    for load in (200, 500, 1000, 2000):
        rs = [r for r in shapes if int(r["load_n"]) == load]
        w = [r["m3_worst_value_mpa"] for r in rs]
        by_load[f"{load}N"] = dict(
            n=len(rs), sf_gate=sum(r["sf_gate_passed"] for r in rs), m5b_raw=sum(r["m5b_raw_passed"] for r in rs),
            m5b_target=sum(r["m5b_target_passed"] for r in rs), m3=sum(r["m3_passed"] for r in rs),
            composite_sf_and_raw=sum(r["sf_gate_passed"] and r["m5b_raw_passed"] for r in rs),
            m3_worst_min=min(w), m3_worst_median=statistics.median(w), m3_worst_max=max(w),
            over_allowable=sum(v > ALLOWABLE_MPA for v in w), over_2x_allowable=sum(v > 2 * ALLOWABLE_MPA for v in w),
            peak_in_holding=sum(r["peak_zone"] == "holding" for r in rs),
            peak_within_2mm_of_fixed=sum(r["peak_distance_to_fixed_mm"] <= 2.0 for r in rs if r["peak_zone"] == "holding"),
            material_min=min(r["material_mm3"] for r in rs), material_max=max(r["material_mm3"] for r in rs),
            wall_hours_sum=sum((r.get("wall_hours") or 0.0) for r in rs),
            peak_working_set_mb_max=max((r.get("peak_working_set_mb") or 0.0) for r in rs))
    total = dict(n=len(shapes), sf_gate=sum(r["sf_gate_passed"] for r in shapes),
                 m5b_raw=sum(r["m5b_raw_passed"] for r in shapes), m5b_target=sum(r["m5b_target_passed"] for r in shapes),
                 m3=sum(r["m3_passed"] for r in shapes),
                 composite_sf_and_raw=sum(r["sf_gate_passed"] and r["m5b_raw_passed"] for r in shapes))
    sf_vals = [r["safety_factor_product"] for r in shapes]
    total["product_sf_min"], total["product_sf_max"] = min(sf_vals), max(sf_vals)
    full = [dict(case_id=r["case_id"], load_n=r["load_n"], material_mm3=r["material_mm3"], sf=r["safety_factor_product"],
                 m5b_raw=r["m5b_raw_exceedance"], m5b_target=r["m5b_target_exceedance"], m3_worst=r["m3_worst_value_mpa"],
                 peak_distance_to_fixed_mm=r["peak_distance_to_fixed_mm"]) for r in h7["full_material_probe"]]
    # 保存済みの2000N全材料(適応の出発点)の値は methods から取る(同一マスクで3本とも出発点を返している)
    start = [r for r in h7["methods"] if r["method"] == "adaptive" and r.get("best_equals_initial")]
    if start:
        r = start[0]
        full.append(dict(case_id="F2000_start_material(適応の出発点=全材料)", load_n=r["load_n"], material_mm3=r["material_mm3"],
                         sf=r["safety_factor_product"], m5b_raw=r["m5b_raw_exceedance"], m5b_target=r["m5b_target_exceedance"],
                         m3_worst=r["m3_worst_value_mpa"], peak_distance_to_fixed_mm=r["peak_distance_to_fixed_mm"]))
    for f in full:
        f["m3_worst_per_kN"] = f["m3_worst"] / (f["load_n"] / 1000.0)
    return dict(by_load=by_load, total=total, full_material=full)


def h2_check(h4: dict) -> dict:
    r = h4["results"]["H2"]
    cur, full = r["current"]["score"], r["adaptive"]["best_ever_score"]
    cur_p = ALLOWABLE_MPA / cur["safety_factor_product"]
    return dict(
        condition=r["hanger_condition"], design_elements=r["design_elements"],
        product_gate=dict(sf=cur["safety_factor_product"], representative_mpa_implied=cur_p,
                          note="製品SF=許容40MPa/代表応力(全材料99.5%点)。代表応力は SF から逆算した値"),
        current_step20=dict(material_mm3=cur["material_mm3"], m5b_raw=cur["m5b_raw_exceedance_mpa_mm3"],
                            m3_worst_zone=cur["m3_worst_zone"], m3_worst_mpa=cur["m3_worst_value"],
                            ratio_to_allowable=cur["m3_worst_value"] / ALLOWABLE_MPA, ks=cur["m7b_ks_mpa"]),
        near_full_material=dict(material_mm3=full["material_mm3"], sf=full["safety_factor_product"],
                                m5b_raw=full["m5b_raw_exceedance_mpa_mm3"], m3_worst_mpa=full["m3_worst_value"],
                                note="適応20評価が0件受理で、最良形=全材料のまま(出発点)"),
        removal_effect_on_m5b_raw=rel(cur["m5b_raw_exceedance_mpa_mm3"], full["m5b_raw_exceedance_mpa_mm3"]),
        removal_effect_on_m3_worst=rel(cur["m3_worst_value"], full["m3_worst_value"]),
        accepted_by_product_gate=cur["safety_factor_product"] >= TARGET_SF * (1 - SF_TOL),
        m3_over_2x_allowable=cur["m3_worst_value"] > 2 * ALLOWABLE_MPA)


# ---------------------------------------------------------------------------
# 06: hanger H1(200N)の方式比較・格子・原点・長予算
# ---------------------------------------------------------------------------
def hanger_h1(h6: dict) -> dict:
    out = {}
    for c in h6["cases"]:
        for m in ("step20", "step05", "adaptive", "reallocation"):
            r = c.get(m)
            if not r:
                continue
            out[f"{c['case_id']}.{m}"] = dict(
                mm3=r["material_mm3"], sf=r["safety_factor_product"], m5b_raw=r["m5b_raw_exceedance_mpa_mm3"],
                m5b_target=r["m5b_target_exceedance_mpa_mm3"], evaluations=r["evaluations"], fem_solves=r["fem_solves"],
                wall_hours=r["wall_seconds"] / 3600.0, stop_reason=r["stop_reason"],
                sf_target_met=r["safety_factor_product"] >= TARGET_SF * (1 - SF_TOL))
    base = out["hanger_h1_baseline.step20"]
    for k, v in out.items():
        v["vs_baseline_step20"] = rel(v["mm3"], base["mm3"])
    return out


# ---------------------------------------------------------------------------
# 08 / 09
# ---------------------------------------------------------------------------
def robustness(r8: dict) -> dict:
    runs = {r["case_id"]: r for r in r8["runs"]}
    out = {}
    for pair, (b, a) in dict(d3_raw=("d3_adaptive_raw_before", "d3_adaptive_raw_after"),
                             p15_raw=("p15_adaptive_raw_before", "p15_adaptive_raw_after")).items():
        out[pair] = dict(before_mm3=runs[b]["material_mm3"], after_mm3=runs[a]["material_mm3"],
                         delta=rel(runs[a]["material_mm3"], runs[b]["material_mm3"]),
                         before_sf=runs[b]["safety_factor_product"], after_sf=runs[a]["safety_factor_product"],
                         after_sf_target_met=runs[a]["safety_factor_product"] >= TARGET_SF * (1 - SF_TOL),
                         before_wall_s=runs[b]["wall_seconds"], after_wall_s=runs[a]["wall_seconds"],
                         after_reject_reasons=runs[a].get("reject_reasons"))
    s20 = runs["d3_step20_percentile"]
    pa = runs["d3_adaptive_percentile_after"]
    out["d3_percentile"] = dict(step20_mm3=s20["material_mm3"], adaptive_after_mm3=pa["material_mm3"],
                                delta=rel(pa["material_mm3"], s20["material_mm3"]), adaptive_after_sf=pa["safety_factor_product"],
                                step20_wall_s=s20["wall_seconds"], adaptive_wall_s=pa["wall_seconds"])
    ra = runs["dbase_realloc_raw_after"]
    ref = r8["realloc_reference_05"]
    out["realloc_cap"] = dict(after_mm3=ra["material_mm3"], reference_05_mm3=ref["material_mm3"],
                              adaptive_same_condition_mm3=ref["adaptive_same_condition_material_mm3"],
                              vs_adaptive_same_condition=rel(ra["material_mm3"], ref["adaptive_same_condition_material_mm3"]),
                              vs_reference_05=rel(ra["material_mm3"], ref["material_mm3"]))
    out["audit_summary"] = r8["audit_summary"]
    out["wall_s_total"] = sum(r["wall_seconds"] for r in r8["runs"])
    return out


def final_shapes(s9: dict) -> dict:
    out = {}
    saved = {r["shape_id"]: r for r in s9["derived"]["saved_vs_resolved"]}
    for s in s9["shapes"]:
        res = s["resolve"]
        sv = saved[s["shape_id"]]
        out[s["shape_id"]] = dict(
            role=s["role"], finalize_status=(s["finalize"] or {}).get("status"), resolve_source=sv["source"],
            sf_saved_resolved=sv["safety_factor_product"], material_change_resolved_vs_saved=rel(*reversed(sv["material_mm3"])),
            m5b_raw=sv["m5b_raw"], m5b_target=sv["m5b_target"], cell_iou_vs_saved_mask=sv["cell_iou_vs_saved_mask"],
            min_width_ok_after_resolve=(res.get("mask") or {}).get("min_width_ok"),
            m3_worst_mpa=((res.get("diagnosis") or {}).get("m3_worst_value")))
    return out


def discrepancy_checks(syn: dict, h6: dict, h7: dict, s9: dict, r8: dict) -> dict:
    cases = {c["case_id"]: c for c in syn["cases"]}
    abl = {c["case_id"]: c["summary"] for c in syn["cases"] if c["kind"] == "mechanism_ablation"}
    origin = {c["case_id"]: c for c in syn["cases"] if c["kind"] == "origin_shift_robustness"}
    out = {}
    out["05_same_condition_adaptive_material"] = dict(
        path_baseline=dict(main_matrix_100eval=cases["05_main_path_baseline_2.0mm"]["adaptive"]["material_mm3"],
                           ablation_revert_100eval_seed100=abl["05_ablation_path_revert"]["material_mm3"],
                           origin_sweep_shift0_40eval_seed300=origin["05_origin_path_origin_sweep"]["adaptive_shift0"]["material_mm3"]),
        dilution_baseline=dict(main_matrix_100eval=cases["05_main_dilution_base_w6"]["adaptive"]["material_mm3"],
                               ablation_revert_100eval_seed100=abl["05_ablation_dilution_revert"]["material_mm3"],
                               origin_sweep_shift0_40eval_seed300=origin["05_origin_dilution_origin_sweep"]["adaptive_shift0"]["material_mm3"]),
        note="評価予算(100対40)とseedが違う別実行。同一条件の再現性ではなく、未収束(evaluation_limit)の適応が予算で動く幅を示す")
    out["05_origin_shift_topology_changed"] = {k: dict(changed=sum(v for kk, v in c["topology_changed_vs_base"].items() if kk != "0"),
                                                       of=len(c["topology_changed_vs_base"]) - 1)
                                               for k, c in origin.items()}
    out["09_resolved_vs_saved_material_change"] = {sid: v["material_change_resolved_vs_saved"] for sid, v in final_shapes(s9).items()}
    grid = h7["corpus_rescore"] + h7["grid"]
    sf = [r["safety_factor_product"] for r in grid]
    out["07_product_sf_pass_definition"] = dict(min=min(sf), max=max(sf), n_above_target_plus_0_5pct=sum(v > TARGET_SF * (1 + SF_TOL) for v in sf),
                                                note="合格は片側(SF>=目標x0.995)。上側に外れた形(最大SF5.23)も合格に数える")
    d3 = {r["case_id"]: r for r in r8["runs"]}
    out["08_d3_prediction_error_counts"] = dict(
        before=dict(evaluations=d3["d3_adaptive_raw_before"]["evaluations"], reject_reasons=d3["d3_adaptive_raw_before"]["reject_reasons"]),
        after=dict(evaluations=d3["d3_adaptive_raw_after"]["evaluations"], reject_reasons=d3["d3_adaptive_raw_after"]["reject_reasons"]),
        note="d3の予測誤差拒否は件数では減っていない(別の拒否理由が加わる)。減ったのはp15(8件から0件)")
    out["06_wall_hours"] = dict(step20_h1=h6["cases"][0]["step20"]["wall_seconds"] / 3600.0,
                                step05_h1=h6["cases"][0]["step05"]["wall_seconds"] / 3600.0)
    return out


def _method_walls(o, acc):
    """`wall_seconds` と `method` を持つ辞書(=1回の探索結果)だけを数える。入れ子の集計値は二重に数えない。"""
    if isinstance(o, dict):
        if "wall_seconds" in o and "method" in o:
            acc.append(o["wall_seconds"])
            return
        for k, v in o.items():
            if not str(k).endswith("_full"):  # `step20_full` は `step20` の詳細版(同じ実行)
                _method_walls(v, acc)
    elif isinstance(o, list):
        for v in o:
            _method_walls(v, acc)


def cost_summary(syn: dict, h6: dict, h7: dict, r8: dict, s9: dict, h4: dict) -> dict:
    out = {}
    for name, doc in (("05_small_searches", syn["cases"]), ("06_hanger_searches", h6["cases"])):
        acc: list = []
        _method_walls(doc, acc)
        out[name] = dict(runs=len(acc), hours=sum(acc) / 3600.0, max_hours=max(acc) / 3600.0)
    rows = h7["grid"]
    out["07_step20_new28"] = dict(runs=len(rows), hours=sum((r.get("wall_hours") or 0) for r in rows),
                                  peak_working_set_mb_max=max((r.get("peak_working_set_mb") or 0) for r in rows))
    out["07_methods5"] = dict(runs=len(h7["methods"]), hours=sum((r.get("wall_hours") or 0) for r in h7["methods"]),
                              per_run_hours=[round(r.get("wall_hours") or 0, 2) for r in h7["methods"]],
                              peak_working_set_mb_max=max((r.get("peak_working_set_mb") or 0) for r in h7["methods"]))
    out["08_small8"] = dict(runs=len(r8["runs"]), hours=sum(r["wall_seconds"] for r in r8["runs"]) / 3600.0)
    out["09_jobs"] = dict(jobs=len(s9["run_ledger"]), hours=sum(r.get("wall_seconds") or 0 for r in s9["run_ledger"]) / 3600.0)
    h2 = h4["results"]["H2"]
    out["04_H2"] = dict(adaptive_hours=h2["adaptive"]["wall_seconds"] / 3600.0, total_hours=h2["total_wall_seconds"] / 3600.0)
    return out


_METRIC_KEYS = ("material_mm3", "safety_factor_product", "m5b_raw_exceedance_mpa_mm3", "m5b_target_exceedance_mpa_mm3",
                "m3_worst_zone", "m3_worst_value", "m7b_ks_mpa", "compliance", "max_displacement_mm")


def multi_metric(c4: dict, h6: dict) -> dict:
    """同一条件・同一評価器で、体積・部位別最悪応力・KS・コンプライアンス・最大変位を並べる(04の小型2題材と06のH1)。"""
    out = {}
    for name, case in c4["cases"].items():
        out[f"04_{name}"] = {m: dict({k: r["best_ever_score"][k] if "best_ever_score" in r else r[k] for k in _METRIC_KEYS if k in (r.get("best_ever_score") or r)},
                                       wall_s=r["wall_seconds"], stop_reason=r["stop_reason"])
                             for m, r in case.items() if isinstance(r, dict) and "wall_seconds" in r}
    h1 = h6["cases"][0]
    out["06_hanger_h1"] = {m: dict({k: h1[f"{m}_full"]["best_ever_score"][k] for k in _METRIC_KEYS if k in h1[f"{m}_full"]["best_ever_score"]},
                                    wall_s=h1[m]["wall_seconds"], stop_reason=h1[m]["stop_reason"])
                           for m in ("step20", "step05", "adaptive", "reallocation") if f"{m}_full" in h1}
    return out


def reaudit_03a() -> dict:
    """03aの訂正を生台帳から再計算する(荷重スイープの線形性・曲げ採点位置補正後の誤差)。"""
    sweep = load("03a_load_sweep_full_material.json")["records"]
    prod = [r["safety_factor"] * r["load_n"] for r in sweep if r["status"] == "ok"]
    bend = load("analytic_bending.json")["records"]
    return dict(
        load_sweep=dict(loads_n=[r["load_n"] for r in sweep], safety_factor=[round(r["safety_factor"], 4) for r in sweep],
                        sf_times_load_min=min(prod), sf_times_load_max=max(prod),
                        relative_spread=(max(prod) - min(prod)) / min(prod)),
        bending_position_corrected_rel_error={f'{r["element_mm"]:g}mm': r["relative_error_stress_position_corrected"] for r in bend})


def main() -> int:
    syn, h4, h6, h7, r8, s9 = (load(f) for f in ("05_synthetic.json", "04_hanger_external_check.json", "06_hanger_low.json",
                                                "07_hanger_loads.json", "08_robustness.json", "09_final_shapes.json"))
    static = json.loads(STATIC.read_text(encoding="utf-8")) if STATIC.exists() else {}
    cores = OUT / "10_work" / "core_groups.json"
    derived = dict(small_matrix=small_matrix(syn), hanger_gates=hanger_gates(h7), h2_5000N=h2_check(h4),
                   hanger_h1=hanger_h1(h6), robustness_08=robustness(r8), final_shapes_09=final_shapes(s9),
                   discrepancy_checks=discrepancy_checks(syn, h6, h7, s9, r8),
                   cost=cost_summary(syn, h6, h7, r8, s9, h4),
                   reaudit_03a=reaudit_03a(),
                   multi_metric=multi_metric(load("04_searches_compact.json"), h6),
                   design_core_groups=json.loads(cores.read_text(encoding="utf-8")) if cores.exists() else None)
    doc = dict(schema_version=1, generated_at=_common.now_iso(), code_version=_common.git_state(),
               scope_note="縮小範囲(期限再編版)の統合。元の全検証の完了・製品安全の保証ではない。",
               sources=["03b_metrics_recheck.json", "04_hanger_external_check.json", "04a_path_qualification.json", "05_synthetic.json",
                        "06_hanger_low.json", "07_hanger_loads.json", "08_robustness.json", "09_final_shapes.json",
                        "10_work/core_groups.json"],
               derived=derived, **static)
    _common.write_atomic(OUT / "10_synthesis.json", json.dumps(doc, ensure_ascii=False, indent=1, default=float) + "\n")
    print("wrote", OUT / "10_synthesis.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

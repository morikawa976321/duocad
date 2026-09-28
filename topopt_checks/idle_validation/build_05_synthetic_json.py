"""タスク5の機械可読まとめを組み立てる。`run_05_main_matrix.py`・`run_05_ablation.py`・
`run_05_sensitivity.py`・`run_05_grid_origin.py`・`run_05_initshape.py`・
`analyze_05_mechanism_history.py` の出力(揃っていることが前提。無ければ`FileNotFoundError`で止まる
——ファイルだけ作って未実行をpassにしない、というタスク共通指示に従う)を
`cases`/`coverage`/`limitations` の共通schema(00〜04aと同じ形)へまとめる。

出力: output/idle_validation_20260928/05_synthetic.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"
REQUIRED = ["05_main_matrix.json", "05_ablation.json", "05_sensitivity.json", "05_grid_origin.json",
           "05_initshape.json", "05_mechanism_history.json"]


def _load(name: str) -> dict:
    path = OUT / name
    if not path.exists():
        raise FileNotFoundError(f"{path} が無い。先に対応するスクリプトを実行すること")
    return json.loads(path.read_text(encoding="utf-8"))


def build_main_matrix_cases(main_matrix: dict) -> list[dict]:
    cases = []
    for c in main_matrix.get("cases", []):
        base = dict(case_id=f"05_main_{c['case_id']}", kind="four_method_comparison", status=c.get("status"),
                   axis=c.get("axis"), cause=c.get("cause"), model=c.get("model"), load_n=c.get("load_n"),
                   element_size_mm=c.get("element_size_mm"), min_width_mm_requested=c.get("min_width_mm_requested"),
                   design_elements=c.get("design_elements"))
        if c.get("status") == "ok":
            for m in ("step20", "step05", "adaptive", "reallocation"):
                base[m] = c.get(m)
        else:
            base["error"] = c.get("error")
        cases.append(base)
    return cases


def build_ablation_cases(ablation: dict) -> list[dict]:
    cases = []
    for model_key in ("dilution", "path"):
        model = ablation.get(model_key)
        if not model:
            continue
        for cfg_key in ("step_only", "revert", "reallocation", "legacy_percentile", "legacy_m5b_raw",
                       "adaptive_finish_percentile", "adaptive_finish_m5b_raw"):
            entry = model.get(cfg_key)
            if not entry:
                continue
            cases.append(dict(case_id=f"05_ablation_{model_key}_{cfg_key}", kind="mechanism_ablation",
                             model=model_key, config=cfg_key, summary=entry["summary"],
                             mechanism_event_types=entry["mechanism_event_types"],
                             mechanism_event_counts=entry["mechanism_event_counts"]))
        cases.append(dict(case_id=f"05_ablation_{model_key}_collateral", kind="collateral_removal_stats",
                         model=model_key, stats=model.get("collateral_removal_stats")))
    return cases


def build_sensitivity_cases(sensitivity: dict) -> list[dict]:
    cases = []
    for key in ("dilution_tuning_sweep", "path_tuning_sweep"):
        sweep = sensitivity.get(key, {})
        for param, data in sweep.items():
            cases.append(dict(case_id=f"05_sensitivity_{key}_{param}", kind="parameter_sensitivity_sweep",
                             param=param, relative_spread=data.get("relative_spread"),
                             sensitive=data.get("sensitive"),
                             rows=[dict(value=r["value"], material_mm3=r["result"]["material_mm3"],
                                       safety_factor_product=r["result"]["safety_factor_product"])
                                  for r in data.get("rows", [])]))
    for key in ("dilution_validation_200N", "path_validation_2700N", "path_validation_3300N"):
        entry = sensitivity.get(key)
        if entry:
            cases.append(dict(case_id=f"05_sensitivity_{key}", kind="unused_validation_check",
                             default=entry["result_with_default"], chosen=entry["result_with_chosen"]))
    return cases


def build_grid_origin_cases(grid_origin: dict) -> list[dict]:
    cases = []
    for model_key in ("dilution_origin_sweep", "path_origin_sweep"):
        sweep = grid_origin.get(model_key, {})
        cases.append(dict(case_id=f"05_origin_{model_key}", kind="origin_shift_robustness",
                         topology_changed_vs_base=sweep.get("topology_changed_vs_base"),
                         material_relative_change_vs_base=sweep.get("material_relative_change_vs_base"),
                         adaptive_shift0=sweep.get("adaptive_shift0"),
                         adaptive_shift_plus0_5all=sweep.get("adaptive_shift_plus0_5all")))
    for model_key in ("dilution_filter_width_sweep", "path_filter_width_sweep"):
        sweep = grid_origin.get(model_key, {})
        for res, entry in sweep.items():
            cases.append(dict(case_id=f"05_filterwidth_{model_key}_{res}", kind="filter_width_mm_vs_cell",
                             element_size_resolved_mm=entry.get("element_size_resolved_mm"),
                             cell_fixed_physical_mm=entry.get("cell_fixed_3cells_physical_mm"),
                             mm_fixed_physical_mm=entry.get("mm_fixed_resolved_physical_mm"),
                             cell_fixed_material_mm3=entry["cell_fixed_result"]["material_mm3"],
                             mm_fixed_material_mm3=entry["mm_fixed_result"]["material_mm3"],
                             cell_fixed_mask_hash=entry["cell_fixed_result"]["mask_hash"],
                             mm_fixed_mask_hash=entry["mm_fixed_result"]["mask_hash"]))
    return cases


def build_initshape_cases(initshape: dict) -> list[dict]:
    cases = []
    for key, entry in initshape.items():
        if key in ("generated_at", "code_version"):
            continue
        cases.append(dict(case_id=f"05_initshape_{key}", kind="warm_start_comparison", **entry))
    return cases


def build_mechanism_history_cases(mech: dict) -> list[dict]:
    cases = [
        dict(case_id="05_mechanism_late_response", kind="failure_mode_late_response",
            rows=mech.get("late_response_analysis")),
        dict(case_id="05_mechanism_fine_grained_creep", kind="failure_mode_fine_grained_creep",
            rows=mech.get("fine_grained_creep_analysis")),
        dict(case_id="05_mechanism_reallocation_lag", kind="failure_mode_reallocation_lag",
            rows=mech.get("reallocation_lag_events")),
    ]
    return cases


def build_coverage(main_matrix, ablation, sensitivity, grid_origin, initshape, mech) -> dict:
    ok_cases = [c for c in main_matrix.get("cases", []) if c.get("status") == "ok"]
    return {
        "scope_decision": dict(
            requirement="第1タスクの全合成題材(S-T/S-B/S-TB/S-L/S-MS)か、先行工程0の小型モデルか",
            evidence="タスクファイル自身の「小型題材への変更」節が本文の旧行列指示に優先すると明記。"
                    "主題材はdilution_small・path_small(04a認定)。S-T等は未構築のまま",
            status="scope_narrowed_documented"),
        "four_method_matrix": dict(
            requirement="荷重・幅・解像度・初期形状を変えた4方式比較、単一例で打ち切らない",
            evidence=f"{len(ok_cases)}条件(dilution 7+path 8)×4方式。task4のbaseline"
                    "(dilution 300N/2.0mm/4mm)は再利用、path baselineは04a条件(2.0mm)で新規実行",
            status="done"),
        "mechanism_ablation": dict(
            requirement="適応歩幅のみ/復帰の有無/再配置の有無、停止条件新旧、旧評価/新評価×削除/可逆",
            evidence="05_ablation.json: step_only/revert/reallocation の3系列+stop_policy×gateの2x2"
                    "(dilution・pathのbaseline条件)",
            status="done"),
        "parameter_sensitivity": dict(
            requirement="調整用題材で値を決めて未使用題材へ適用",
            evidence="05_sensitivity.json: 3パラメータ×3値を調整用条件(04aの調整条件と同じ)で振り、"
                    "未使用検証(dilution 200N・path 2700/3300N)へ適用",
            status="done"),
        "history_and_failure_modes": dict(
            requirement="途中形状・C・変位・応力場・部材復元・最良体積の履歴、急変検知遅延等の失敗検査",
            evidence="StepRecordが全run共通で体積・安全率・M5b・compliance・変位・予測/実際jumpを保持。"
                    "05_mechanism_history.jsonが急変検知遅延・微小損失の見逃し・再配置の遅延を分析",
            status="done"),
        "grid_and_origin_mm_fixed": dict(
            requirement="最小幅・フィルタ幅をmm固定した格子細分・原点ずれで位相/体積/性能のばらつき",
            evidence="05_grid_origin.json: 原点シフト8条件×2モデル(step20+adaptive2点)、"
                    "フィルタ幅mm固定vsセル固定を4解像度×2モデル",
            status="done"),
        "equal_budget_best_curve": dict(
            requirement="収束までの結果に加え、同時間/同FEM回数の最良合格体積曲線",
            evidence="adaptive/reallocationはstep単位のbest-so-far曲線を保持。step20/05はstep粒度の"
                    "記録が無く終点1点のみ(制約として明記)",
            status="done_with_limitation"),
        "initial_shape_variants": dict(
            requirement="移植トラス・粗い探索からのウォームスタートで原因2を避けられるか",
            evidence="05_initshape.json: 3.0mm解像度からの投影、path側は04a優越形からのwarm-start",
            status="done"),
    }


def build_limitations(main_matrix, ablation, sensitivity, grid_origin, initshape, mech) -> list[str]:
    lim = [
        "01_PROTOCOL.md §5の合成材料5種(S-T/S-B/S-TB/S-L/S-MS)は本タスクでは新規構築していない。"
        "タスクファイル自身の優先指定(「小型題材への変更」節)に基づく判断——task10のcoverage監査へ"
        "この判断の妥当性を引き継ぐ。",
        "path_smallの04a認定はpercentile評価シナリオに限る(m5b_targetでは現行・優越形とも不合格・"
        "未決着)。本タスクの評価ゲートは既定でm5b_rawを使うため、path側の受理判定は04aの認定範囲と"
        "厳密には別シナリオである点に注意(併記されるsafety_factor_productでpercentile側も確認できる)。",
        "hanger実行は本タスクの範囲外(task4のH1/H2で外部確認済み、task6/7が引き継ぐ)。",
        "reallocationのwarm-start専用入口は無く(方式4は常にrun_adaptiveの削減→追加を内部で辿る設計)、"
        "05_initshapeの比較はrun_adaptive_from_mask(方式3)に限る。方式4への拡張は未実施。",
        "パラメータ感度スイープ(05_sensitivity)は評価予算70で打ち切っており、収束の理論的な極限では"
        "ない(他の主要行列と同じ理由——比較指標として使い、外部の期限には変換しない)。",
        "late_response分析はStepRecordに手単位のイベント直接リンクを持たないため、run単位の粗い近似"
        "(「同run内にrollback/revert系イベントが発動したか」)にとどまる。手単位の厳密な追跡には"
        "mechanism_eventsへのstep_index付与が要る(未実施)。",
    ]
    return lim


def main() -> int:
    data = {name: _load(name) for name in REQUIRED}
    main_matrix = data["05_main_matrix.json"]
    ablation = data["05_ablation.json"]
    sensitivity = data["05_sensitivity.json"]
    grid_origin = data["05_grid_origin.json"]
    initshape = data["05_initshape.json"]
    mech = data["05_mechanism_history.json"]

    cases = (build_main_matrix_cases(main_matrix) + build_ablation_cases(ablation)
            + build_sensitivity_cases(sensitivity) + build_grid_origin_cases(grid_origin)
            + build_initshape_cases(initshape) + build_mechanism_history_cases(mech))

    payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state(), schema_version=1,
                  cases=cases,
                  coverage=build_coverage(main_matrix, ablation, sensitivity, grid_origin, initshape, mech),
                  limitations=build_limitations(main_matrix, ablation, sensitivity, grid_origin, initshape, mech),
                  sources=REQUIRED)
    out_path = OUT / "05_synthetic.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

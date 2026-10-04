"""タスク4a: `04a_qualification_run.json`(qualify_path_small.pyの生データ、3000N)と
`04a_robustness_loads.json`(robustness_path_qualification.pyの生データ、2700N/3300N)を合わせ、
`docs/TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md`「成果物と後続への契約」節の共通schema
(`cases`/`coverage`/`limitations`。00〜04の`build_0X_*_json.py`と同じ形)へまとめる。
2つの入力が両方揃っていることが前提(揃っていなければ`FileNotFoundError`で止まる——
ファイルだけ作って未実行をpassにしない、というタスク共通指示に従う)。

出力: output/idle_validation_20260928/04a_path_qualification.json
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


def _load(name: str) -> dict:
    path = OUT / name
    if not path.exists():
        raise FileNotFoundError(f"{path} が無い。先に対応するスクリプトを実行すること"
                                f"(qualify_path_small.py / robustness_path_qualification.py)")
    return json.loads(path.read_text(encoding="utf-8"))


def _case_from_score(case_id: str, kind: str, load_n: float, score: dict, *, extra: dict | None = None) -> dict:
    row = dict(case_id=case_id, kind=kind, load_n=load_n, **score)
    if extra:
        row.update(extra)
    return row


def build_cases(run: dict, robustness: dict) -> list[dict]:
    cases = []
    common = dict(model=run["model"], element_size_mm=run["element_size_mm"], min_width_mm=run["min_width_mm"],
                 design_elements=run["design_elements"], min_width_cells=run["min_width_cells"])
    cases.append(_case_from_score("baseline_native_step20_3000n", "native_step20_reference", run["load_n"],
                                  run["baseline_native_step20"], extra=common))
    cases.append(_case_from_score("seed_transplant_3000n", "transplant_2_5mm_to_2_0mm", run["load_n"],
                                  run["seed_transplant"], extra=common))
    cases.append(_case_from_score("after_growth_3000n", "local_reinforcement_result", run["load_n"],
                                  run["after_growth"],
                                  extra=dict(growth_rounds=len(run["growth_log"]), **common)))
    cases.append(_case_from_score("qualified_form_3000n", "superior_form_percentile_scenario", run["load_n"],
                                  run["qualified_form"],
                                  extra=dict(full_rules=run["qualified_form_full_rules"],
                                            finish_reduction=run["finish_reduction"],
                                            lighter_than_native=run["qualified_form_full_rules"]["lighter_than_native"],
                                            percentile_gate_passed=run["qualified_form_full_rules"]["percentile_gate_passed"],
                                            m5b_target_scenario_passed=run["qualified_form_full_rules"]["m5b_target_passed"],
                                            **common)))
    cc = run["causal_check"]
    cases.append(dict(case_id="causal_control_unprotected_3000n", kind="path_loss_contrast_control",
                      load_n=run["load_n"], corridor_cells=cc["corridor_cells"], corridor_mm3=cc["corridor_mm3"],
                      **cc["control_unprotected"], **common))
    cases.append(dict(case_id="causal_protected_corridor_3000n", kind="path_loss_contrast_protected",
                      load_n=run["load_n"], corridor_cells=cc["corridor_cells"], **cc["protected_corridor"],
                      **common))
    if cc.get("single_alternate_move"):
        cases.append(dict(case_id="causal_single_alternate_move_3000n", kind="path_loss_contrast_single_move",
                          load_n=run["load_n"], **cc["single_alternate_move"], **common))
    for load_key, result in robustness["cases"].items():
        cases.append(dict(case_id=f"robustness_qualified_{load_key}n", kind="superior_form_robustness_check",
                          load_n=result["load_n"], native_2_0mm=result["native_2_0mm"],
                          transplant=result["transplant"], after_growth=result["after_growth"],
                          qualified=result["qualified"], lighter_than_native=result["lighter_than_native"],
                          lighter_ratio=result["lighter_ratio"], full_rules=result["full_rules"],
                          wall_seconds=result["wall_seconds"], peak_memory=result["peak_memory"]))
    return cases


def build_coverage(run: dict, robustness: dict) -> dict:
    qf = run["qualified_form_full_rules"]
    return {
        "same_conditions_superior_form": dict(
            requirement="同じ格子・荷重・拘束・評価器・実現最小幅で、現行探索より軽くて合格する形",
            evidence=f"qualified_form: {qf['qualified_material_mm3']} mm3 (native {qf['native_material_mm3']} mm3、"
                    f"-{round((1-qf['qualified_material_mm3']/qf['native_material_mm3'])*100,1)}%)。"
                    f"percentile_gate_passed={qf['percentile_gate_passed']}。6条件+最小幅は04A_PATH_QUALIFICATION.md 4節",
            status="done_percentile_scenario_only"),
        "path_loss_contrast_experiment": dict(
            requirement="削除直前への復帰＋別の一手、または該当経路の再導入と対照追加で因果を検査する",
            evidence="causal_check: 無保護対照が現行と厳密一致(71928mm3)・corridor保護で71544mm3・"
                    "単一代替手でもcorridor保持率8.4%まで低下(04A_PATH_QUALIFICATION.md 5節)",
            status="done"),
        "reproduction_commands": dict(
            requirement="再現コマンド",
            evidence="qualify_path_small.py(引数無し)/robustness_path_qualification.py(引数無し)。"
                    "04A_PATH_QUALIFICATION.md 8節",
            status="done"),
        "all_rules_pass": dict(
            requirement="全規則の合否(保持・除外・アンカー・単一成分・正則境界・芯の連結・最小幅・閉空洞禁止)",
            evidence=f"qualified_form_full_rules.checks={qf['checks']}, min_width_ok={qf['min_width_ok']}",
            status="done"),
        "execution_cost": dict(
            requirement="速度とピークメモリを製品工程を含め測る",
            evidence=f"3000N主実証: {round(run['wall_seconds_total'],1)}秒・RSS peak "
                    f"{run['peak_memory']['process_rss_mb']['peak_working_set_mb']} MB。製品全行程の速度/メモリは"
                    "00_COMPACT_MODELS.md 3-2節に既存記録(本タスクは探索段のみ)",
            status="done_search_stage_only"),
        "robustness_range": dict(
            requirement="近傍荷重/原点条件でも再現性を確認し、偶然の一例と頑健な範囲を区別する",
            evidence="2700N/3300N(±10%)で同方向(優越形が軽く目標達成)を確認。原点シフトは未実施",
            status="done_load_only_origin_not_covered"),
        "evaluator_scenario_separation": dict(
            requirement="旧現行評価と03bの新評価はシナリオを分け、各シナリオ内で比較する",
            evidence=f"percentile: qualified passed(SF={run['qualified_form']['safety_factor_product']})。"
                    f"m5b_target: native/qualifiedとも不合格(native {run['baseline_native_step20']['m5b_target_exceedance_mpa_mm3']}"
                    f" / qualified {run['qualified_form']['m5b_target_exceedance_mpa_mm3']} MPa*mm3超過、シナリオ未決着)",
            status="done_percentile_only_m5b_target_unresolved"),
    }


def build_limitations(run: dict) -> list[str]:
    return [
        "対照実験(段C・D、経路喪失の因果検査)は3000N条件でのみ実施した。2700N/3300Nの頑健性確認は"
        "移植+局所成長+仕上げ削減のみで、因果検査は再実行していない。",
        "原点シフトでの頑健性は未実施(モデルのSTEP形状直接編集が必要で本タスク範囲外)。"
        "格子解像度を跨いだ優越形構成法の一般性(2.0mm以外での動作)も未検証。",
        "03bのm5b_target評価シナリオでは現行・優越形とも不合格のまま(超過が同程度)——"
        "『優越形は現行より軽く安全』という結論はパーセンタイル評価シナリオに限る。",
        "軸方向シフトによる局所成長は、この形状(細い部材が支持点近くで太いパッドへ急に接続する構成)で"
        "有効だった手法。他題材(hanger等)でも同じ効率が出る保証は無い。",
        "hangerの移植トラス(5セル最小幅)の優越形構成は本タスクの対象外(01_PROTOCOL.md参照、別件のまま残る)。",
    ]


def main() -> int:
    run = _load("04a_qualification_run.json")
    robustness = _load("04a_robustness_loads.json")
    payload = dict(
        generated_at=_common.now_iso(), code_version=run["code_version"], schema_version=1,
        cases=build_cases(run, robustness), coverage=build_coverage(run, robustness),
        limitations=build_limitations(run),
        sources=["04a_qualification_run.json", "04a_robustness_loads.json"],
    )
    out_path = OUT / "04a_path_qualification.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path} ({len(payload['cases'])} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

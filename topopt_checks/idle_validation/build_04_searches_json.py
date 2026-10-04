"""タスク4の機械可読まとめを組み立てる(`docs/TOPOPT_IDLE_VALIDATION_TASKS_2026-09-28.md`
「成果物と後続への契約」の schema: `cases`/`coverage`/`limitations`)。

入力(すべて実行済みであること。無ければ即座に失敗する——ファイルの実在で完了と扱わない):
- `04_searches_compact.json`(`run_04_searches_compact.py`。dilution_small/path_smallでの4方式)
- `04_mechanism_checks.json`(`search_methods_mechanism_checks.py`。各機構が発動した証拠)
- `04_hanger_external_check.json`(`hanger_external_check.py`。H1/H2の現行 vs 新方式)

再現: `.\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\build_04_searches_json.py`
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
        raise FileNotFoundError(f"{name} が無い。先に対応するスクリプトを実行すること: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    compact = _load("04_searches_compact.json")
    mechanisms = _load("04_mechanism_checks.json")
    hanger = _load("04_hanger_external_check.json")

    cases = []
    for model_name, model_result in compact["cases"].items():
        for method_key in ("step20", "step05", "adaptive", "adaptive_percentile_gate", "reallocation"):
            if method_key not in model_result:
                continue
            m = model_result[method_key]
            cases.append(dict(
                case_id=f"compact_{model_name}_{method_key}",
                kind="compact_model_search",
                model=model_result["model"], model_sha256=model_result["model_sha256"],
                load_n=model_result["load_n"], element_size_mm=model_result["element_size_mm"],
                min_width_mm=model_result["min_width_mm"], design_elements=model_result["design_elements"],
                method=m["method"], status=m["status"], stop_reason=m["stop_reason"],
                evaluations=m["evaluations"], fem_solves=m["fem_solves"], wall_seconds=m["wall_seconds"],
                seed=m.get("seed"), best_ever_score=m.get("best_ever_score"),
                mask_path=m.get("best_ever_mask_path"), mechanism_event_count=len(m.get("mechanism_events", [])),
            ))
    for check in mechanisms["checks"]:
        cases.append(dict(case_id=f"mechanism_check_{check['name']}", kind="mechanism_demonstration",
                          passed=check["passed"], detail=check))
    for cond_name, cond_result in hanger["results"].items():
        cases.append(dict(
            case_id=f"hanger_external_{cond_name}", kind="hanger_external_check",
            hanger_condition=cond_result["hanger_condition"], load_n=cond_result["load_n"],
            element_size_mm=cond_result["element_size_mm"],
            min_width_mm_resolved=cond_result["min_width_mm_resolved"],
            cause_represented=cond_result["cause_represented"], design_elements=cond_result["design_elements"],
            current_score=cond_result["current"]["score"],
            adaptive_status=cond_result["adaptive"]["status"], adaptive_stop_reason=cond_result["adaptive"]["stop_reason"],
            adaptive_evaluations=cond_result["adaptive"]["evaluations"],
            adaptive_best_ever_score=cond_result["adaptive"]["best_ever_score"],
            checkpoint_path=cond_result["adaptive"]["checkpoint_path"],
        ))

    coverage = dict(
        four_entry_points=dict(
            requirement="20%削除・5%削除・適応歩幅＋復帰・再配置の4方式の実行入口",
            evidence="search_methods.py: run_fixed_step(max_step_ratio=0.2/0.05)/run_adaptive/run_reallocation。"
                    "04_searches_compact.json の各case",
            status="done",
        ),
        old_path_reproduction=dict(
            requirement="既存条件で旧20%/5%経路を再現して比較器の正しさを確認する",
            evidence="dilution_small: step20が00_COMPACT_MODELS.md §2-2の既知値(88,368mm3・SF4.836・18評価)と一致",
            status="done",
        ),
        adaptive_multi_signal=dict(
            requirement="安全率・部位別応力・C・変位・巻き添え/島込みの実削除体積を用いた歩幅決定。固定5%化ではない",
            evidence="search_methods._run_adaptive_impl: gap_ratio(安全率)×multiplier(予測誤差/急変)、"
                    "_relative_zone_jump(M3部位別)、compliance/max_displacement_mmを毎候補で記録・監視、"
                    "removed_energyは実削除セル(巻き添え/島込み)の合計で一次予測に使う",
            status="done",
        ),
        rollback=dict(requirement="性能急変/予測誤差で巻き戻し、小さい候補を再評価",
                     evidence="mechanism_checks: rollback (実測12件発動)", status="done"),
        revert_restart=dict(requirement="過去の合格形状への復帰と別の削除順/歩幅での再開",
                           evidence="mechanism_checks: revert_restart (実測22件発動、working=best_ever)",
                           status="done"),
        reallocation_mechanism=dict(
            requirement="同体積付近で追加・削除、仮追加で一時的に重くなる形、候補ごとの状態記録",
            evidence="mechanism_checks: poison_invalidation_and_reallocation "
                    "(temporarily_heavier_observed=true、grow_events記録)。04_searches_compact.jsonのreallocation",
            status="done",
        ),
        long_member_restoration=dict(
            requirement="長い部材の再導入が実際に可能なことを確認する",
            evidence="mechanism_checks: max_added_cells_in_one_grow=6815(path_smallの連結成分をまるごと復元)。"
                    "**path_smallは04a未認定**(経路喪失=原因2の候補題材。04aが認定または代替計画を出すまで、"
                    "この復元実証は『未認定モデルでの機構確認』であって、実際の経路喪失を修復した証拠ではない)",
            status="done_with_caveat",
        ),
        add_side_invariants=dict(
            requirement="保持・除外・アンカー、単一成分、正則境界、芯の連結、最小幅、閉空洞禁止を追加側でも検査する",
            evidence="search_methods._growth_geometry_ok: boundary_check/connectivity_check/region_check×2/"
                    "condition_check/_mask_satisfies_min_width/closed_voids/core_group_count を全て呼ぶ",
            status="done",
        ),
        finish_and_stop_contrast=dict(
            requirement="安全率窓到達後の有限な仕上げ探索と改善停滞判定。元の停止条件との対照",
            evidence="mechanism_checks: stop_policy_contrast (stop_policy='legacy' vs 'adaptive_finish' を同条件で対照)",
            status="done",
        ),
        poison_hash_determinism_resume_cancel=dict(
            requirement="再配置後のpoison/失敗キャッシュの無効化、ハッシュ条件依存、決定性/seed、再開の一致、"
                        "キャンセル後に合格形が残ることを意味のある反例で検査する",
            evidence="mechanism_checks: poison_invalidation_and_reallocation(cleared_poison/cleared_rejected)、"
                    "hash_independence(条件の異なるctxを直後に走らせても対照実行と一致=キャッシュ非共有)、"
                    "determinism(同seed一致・異seed分岐)、resume(チェックポイント→再開が対照実行と完全一致)、"
                    "cancel_retains_pass(is_cancelledで打ち切り、best_everが合格形のまま残る)",
            status="done",
        ),
        no_superiority_claim=dict(
            requirement="新方式の性能優位は次の実測まで主張しない",
            evidence="docs/topopt_idle_validation/04_SEARCHES.md 全体で数値のみ記録し優劣を結論しない。"
                    "評価ゲート(m5b_raw/m5b_target/percentile)が異なる方式間の体積比較は評価シナリオが異なるため"
                    "直接比較しない(各シナリオ内で比較)",
            status="done",
        ),
        hanger_external_confirmation=dict(
            requirement="原因2の低荷重・太幅(H1)と原因1の高荷重(H2)の代表条件で外部確認する",
            evidence=f"hanger_external_check.py: H1={hanger['results'].get('H1', {}).get('adaptive', {}).get('stop_reason')}, "
                    f"H2={hanger['results'].get('H2', {}).get('adaptive', {}).get('stop_reason')}。"
                    "H1の現行(hanger_baseの再スコア)はM5b(raw)=69.5相当の既知不合格を再現(03bとの整合確認)。"
                    "H2はhanger_baseを2.4mmへ投影すると連結成分が条件面を失う(実測)ため、"
                    "現行をH2条件での製品コード(20%)新規実行に切り替えた",
            status="done",
        ),
    )

    limitations = [
        "path_smallは04aで原因2(経路喪失)の認定が未完了。長い部材の復元実証はこのタスクでは"
        "『機構が実際に動作する証拠』にとどまり、原因2の修復証明ではない(04aが引き継ぐ)。",
        "hanger外部確認はH1とH2で『現行』の作り方が異なる。H1は既存保存形hanger_baseの再スコア"
        "(新方式=adaptiveはhanger_baseから削減を継続。`run_adaptive_from_mask`で段階3の全設計空間FEMを"
        "回避——H1条件〈2.0mm要素〉で設計空間64万要素、全材料1回のFEM解に約157秒かかることを実測したため)。"
        "H2はhanger_baseの2.0mm→2.4mm投影が連結成分を失う(touches_condition=Trueの17,811セル分の"
        "成分が丸ごと落ちる。実測)ため使えず、現行を製品コード(20%固定歩幅)のH2条件での新規実行"
        "(段階3=find_initial_feasibleを含むフル探索)に切り替えた。新方式もH2では同じ入口(段階3から)で揃えた。",
        "評価ゲート`m5b_raw`(03b凍結定義。材料の生の許容応力に対する超過)と`m5b_target`"
        "(allowable/target_safety_factorを閾値にした版)は異なる安全の定義を測る。両方を記録するが、"
        "どちらが『正しい』かはこのタスクでは判定しない(03b以降の判断)。",
        "適応方式・再配置方式は実験用実装であり、製品の探索(binary_search.find_best_feasible)を置き換えていない。",
        "評価予算(max_evaluations)は各実行で有限(dilution/pathの主実証は120、hangerは20——"
        "H1条件で1FEM解あたり約157秒かかるため、時間による途中打切りではなく評価回数の予算として"
        "事前に決めた)。"
        "`evaluation_limit`で終わった実行は『収束』ではなく『予算内で到達した点』として記録する"
        "(チェックポイントがあるので延長・再開は可能)。",
    ]

    payload = dict(
        generated_at=_common.now_iso(), code_version=_common.git_state(),
        schema_version=1, cases=cases, coverage=coverage, limitations=limitations,
        sources=dict(compact="04_searches_compact.json", mechanisms="04_mechanism_checks.json",
                    hanger="04_hanger_external_check.json"),
    )
    out_path = OUT / "04_searches.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

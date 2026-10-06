"""段階8・機械可読まとめ `08_robustness.json` を、監査(`08_audit.json`)・小型比較(`08_runs.json`)・
最良形検査(`08_mask_checks.json`)・05の保存台帳から組み立てる(新規FEMなし)。

実行: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\build_08_json.py`
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
TARGET_SF = 4.8


def _load(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def _case05(cases, case_id):
    return next(c for c in cases if c["case_id"] == case_id)


def _row(c):
    s = c["search"]
    return dict(case_id=c["case_id"], pair=c["pair"], variant=c["variant"], kind=c["kind"], gate=c["gate"],
                grid_resolved_mm=c["element_size_resolved_mm"], min_width_cells=c["min_width_cells_resolved"],
                min_width_realized_mm=c["min_width_realized_mm"], load_n=c["load_n"], model=c["model"],
                prediction_error_floor=c.get("prediction_error_floor"),
                grow_max_fraction_of_removed=c.get("grow_max_fraction_of_removed"),
                stop_if_start_fails_gate=c.get("stop_if_start_fails_gate"), max_evaluations=c.get("max_evaluations"),
                status=c["status"], search_status=s["status"], stop_reason=s["stop_reason"], evaluations=s["evaluations"],
                wall_seconds=s["wall_seconds"], material_mm3=s["material_mm3"],
                safety_factor_product=s["safety_factor_product"],
                product_sf_target_met=(s["safety_factor_product"] is not None
                                       and s["safety_factor_product"] >= TARGET_SF * 0.995),
                m5b_raw_passed=s["m5b_raw_passed"], m5b_target_passed=s["m5b_target_passed"],
                m5b_target_exceedance_mpa_mm3=s["m5b_target_exceedance_mpa_mm3"], mask_hash=s["mask_hash"],
                reject_reasons=c["digest"]["reject_reasons"], mechanism_event_counts=c["digest"]["mechanism_event_counts"],
                grow_accepted=c["digest"]["grow_accepted"], mask=c.get("best_ever_mask_path"))


def main() -> int:
    runs = _load("08_runs.json")["cases"]
    by_id = {c["case_id"]: c for c in runs}
    m05 = _load("05_main_matrix.json")["cases"]
    checks = {r["case_id"]: r for r in _load("08_mask_checks.json")["rows"]}
    audit = _load("08_audit.json")

    rows = []
    for c in runs:
        if c["status"] == "ok":
            r = _row(c)
        elif c["status"] == "no_solution":
            s = c["search"]
            r = dict(case_id=c["case_id"], pair=c["pair"], variant=c["variant"], kind=c["kind"], gate=c["gate"],
                     grid_resolved_mm=c["element_size_resolved_mm"], min_width_cells=c["min_width_cells_resolved"],
                     min_width_realized_mm=c["min_width_realized_mm"], load_n=c["load_n"], model=c["model"],
                     stop_if_start_fails_gate=c.get("stop_if_start_fails_gate"), status="no_solution",
                     stop_reason=s["stop_reason"], evaluations=s["evaluations"], fem_solves=s["fem_solves"],
                     wall_seconds=s["wall_seconds"], start_material_mm3=s["material_mm3"],
                     start_safety_factor_product=s["safety_factor_product"],
                     start_m5b_raw_passed=s["m5b_raw_passed"], start_m5b_target_passed=s["m5b_target_passed"],
                     start_m5b_target_exceedance_mpa_mm3=s["m5b_target_exceedance_mpa_mm3"])
        else:
            r = dict(case_id=c["case_id"], status=c["status"], reason=c.get("stop_reason") or c.get("error"))
        r["mask_checks"] = checks.get(c["case_id"])
        rows.append(r)

    parity = []

    def check(pair_name, a, b, fields):
        diffs = {f: (a[f], b[f]) for f in fields if a.get(f) != b.get(f)}
        parity.append(dict(pair=pair_name, a=a["case_id"], b=b["case_id"], fields=list(fields), differing=diffs,
                           identical_conditions=not diffs))

    common = ("grid_resolved_mm", "min_width_cells", "min_width_realized_mm", "load_n", "gate", "model")
    rr = {r["case_id"]: r for r in rows if r.get("status") == "ok"}
    check("d3_raw", rr["d3_adaptive_raw_before"], rr["d3_adaptive_raw_after"], common + ("max_evaluations",))
    check("p15_raw", rr["p15_adaptive_raw_before"], rr["p15_adaptive_raw_after"], common + ("max_evaluations",))
    check("d3_percentile", rr["d3_step20_percentile"], rr["d3_adaptive_percentile_after"], common)

    control_05 = {}
    for case_id, run_id, method in (("dilution_res3.0_w6", "d3_adaptive_raw_before", "adaptive"),
                                    ("path_res1.5_w6", "p15_adaptive_raw_before", "adaptive"),
                                    ("dilution_res3.0_w6", "d3_step20_percentile", "step20")):
        old = _case05(m05, case_id)[method]
        control_05[run_id] = dict(case_05=case_id, method=method, mask_hash_05=old["mask_hash"],
                                  mask_hash_now=rr[run_id]["mask_hash"],
                                  reproduces_05=old["mask_hash"] == rr[run_id]["mask_hash"],
                                  material_mm3_05=old["material_mm3"], safety_factor_product_05=old["safety_factor_product"])
    base = _case05(m05, "dilution_base_w6")
    realloc_ref = dict(source="05_main_matrix.json dilution_base_w6 reallocation(再実行していない。同じ条件・同じseed=43・60評価x3サイクル)",
                       material_mm3=base["reallocation"]["material_mm3"],
                       safety_factor_product=base["reallocation"]["safety_factor_product"],
                       grow_accepted=[dict(source=e["source"], before=e["material_mm3_before"], after=e["material_mm3_after"])
                                      for e in base["reallocation_full"]["mechanism_events"] if e.get("type") == "grow_accepted"],
                       adaptive_same_condition_material_mm3=base["adaptive"]["material_mm3"],
                       adaptive_same_condition_safety_factor_product=base["adaptive"]["safety_factor_product"],
                       step20_material_mm3=base["step20"]["material_mm3"],
                       step20_safety_factor_product=base["step20"]["safety_factor_product"])

    def delta(a, b):
        return (rr[b]["material_mm3"] - rr[a]["material_mm3"]) / rr[a]["material_mm3"]

    derived = dict(
        d3_raw_after_vs_before_material=delta("d3_adaptive_raw_before", "d3_adaptive_raw_after"),
        p15_raw_after_vs_before_material=delta("p15_adaptive_raw_before", "p15_adaptive_raw_after"),
        d3_percentile_adaptive_vs_step20_material=delta("d3_step20_percentile", "d3_adaptive_percentile_after"),
        realloc_cap_vs_05_reference_material=(rr["dbase_realloc_raw_after"]["material_mm3"] - realloc_ref["material_mm3"])
        / realloc_ref["material_mm3"],
        realloc_cap_vs_05_adaptive_same_condition_material=(rr["dbase_realloc_raw_after"]["material_mm3"]
                                                            - realloc_ref["adaptive_same_condition_material_mm3"])
        / realloc_ref["adaptive_same_condition_material_mm3"],
    )

    result = dict(
        schema_version=1, generated_at=_common.now_iso(), code_version=_common.git_state(paths=("duocad", "topopt_checks")),
        scope_note="期限再編版の工程08(縮小範囲)。原計画の全検証の完了ではない。小型最大8実行(+一時的な書き込み失敗の再起動1回)。新規hanger最適化なし。",
        started_at="2026-10-05T20:19:36+09:00", compute_cutoff="2026-10-06T04:19:36+09:00",
        audit_summary=dict(
            runs_after_dedup=audit["runs_after_dedup"],
            prediction_error={k: v for k, v in audit["prediction_error"].items() if k != "per_run"},
            reallocation={k: v for k, v in audit["reallocation"].items() if k != "per_run"},
            gates=audit["gates"]),
        runs=rows, condition_parity=parity, controls_from_05=control_05, realloc_reference_05=realloc_ref,
        derived=derived,
        coverage=[
            dict(item="05/06/07の悪化原因の監査(予測誤差の小分母・再配置の全材料復帰・ゲート不一致・出発点不成立)", status="done",
                 evidence="08_audit.json(新規FEMなし、57実行・重複除去後)"),
            dict(item="研究用実装の小修正(opt-in 3点。既定=旧挙動)", status="done",
                 evidence="search_methods.py: prediction_error_floor・grow_max_fraction_of_removed・stop_if_start_fails_gate"),
            dict(item="修正前後の小型比較(最大8実行・各20分)", status="done_limited",
                 evidence="8実行すべてok/no_solution(timeoutなし)。比較が成立したのは d3_raw・p15_raw・d3_percentile。"
                          "再配置は修正前を再実行せず05の保存値を対照にした(d3・p15の修正前2件が05と同一マスクを再現した事実を根拠に)。"
                          "出発点不成立の早期停止は修正後のみ(修正前の同条件対照は未実施)。"),
            dict(item="既知の悪化例を最低1つ含める", status="done",
                 evidence="dilution解像度3.0mm(05で+168.8%)・path解像度1.5mm(同+83.3%)"),
            dict(item="最小幅の併記と最良形検査", status="done", evidence="08_mask_checks.json: 7形すべて最小幅・正則境界・単一成分・閉空洞なしを満たす"),
            dict(item="新しい5合成題材・hanger検証", status="not_run_by_scope", evidence="期限計画で禁止/対象外"),
        ],
        deferred_by_user_deadline=[
            "percentileゲートでの floor 有無の対照(d3_adaptive_percentile の修正前)",
            "再配置: 上限(cap)と floor の併用・複数条件・格子/荷重の拡張",
            "適応方式の calibrated_k 単点較正の再較正(再較正方式そのもの)",
            "m5b_raw と製品percentileを同時に課す複合ゲートの実装と比較",
            "出発点不成立の早期停止の修正前対照(小型)",
            "floor値(0.01)の感度(未調整の値。1点のみ)",
        ],
        adoption=dict(
            adaptive_with_prediction_floor="採用保留。D3で同一ゲート(percentile)ではstep20より+6.0%重く評価も多い(1条件)。m5b_rawでは軽いが製品SF目標4.8を割る形(4.03〜4.04)",
            reallocation_with_cap="採用候補から外す。上限で全材料復帰は止まったが、同条件の適応(05保存値57,768 mm³)より重く(64,496 mm³)、予測誤差拒否が78件再発(評価予算は05で4方式を揃えていないので評価回数の優劣は言わない)",
            m5b_raw_gate_alone="採用しない。合格形の約半数が製品SF目標を満たさない(監査: m5b_raw受理50形のうち27形)",
            stop_if_start_fails_gate="研究用の安全弁として採用可(挙動が変わるのは開始形がゲート不合格のときだけ)。小型で動作確認済み、性能への寄与は主張しない",
        ),
        limitations=[
            "小型2題材(dilution_small・path_small)の3条件のみ。hangerでの効果は測っていない。",
            "各条件1実行(seed固定)。ばらつきは測っていない。",
            "floor=0.01は保存台帳の分布(予測誤差だけで拒否された手のうち2,097/2,100(99.86%)が実ジャンプ1%以下)を見て置いた丸め値で、調整していない。感度は未測定。",
            "05の保存結果を対照にした再配置の前後比較は、同一コードでの再実行ではない(D3・P15の再現一致が間接根拠)。",
            "製品の既定・公開UIは変更していない。研究用実装の既定挙動も変えていない。",
        ],
    )
    (OUT / "08_robustness.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    print("written", len(rows), "rows;", json.dumps(derived, indent=1))
    print(json.dumps(parity, ensure_ascii=False, indent=1))
    print(json.dumps(control_05, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

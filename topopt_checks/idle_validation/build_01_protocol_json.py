"""`output/idle_validation_20260928/01_protocol.json`(段階1の機械可読まとめ)を組み立てる。

`cases`は本タスクが実際に実行・検査したもの(`kind`が`input_inventory`/`regeneration`)と、後続タスクへ渡す
定義(`kind`が`protocol_definition`/`planned`/`material_definition`/`matrix_definition`。`status`は
すべて`defined_not_executed`——**8章の実行そのものはtask2〜10の担当**で、本タスクは確定までが仕事)を両方含む。
`generated_at`・`git`は実行時に`_common`で実測する(手で書いた固定値にしない——QA round1で「手編集/非決定的
生成の疑い」を指摘された)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\build_01_protocol_json.py
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


def _stage1_summary() -> dict:
    registry = json.loads((OUT / "01_stage1_shapes.json").read_text(encoding="utf-8"))
    return dict(entries=len(registry["entries"]), mismatches_vs_doc=len(registry["mismatches"]),
               new_findings=len(registry["new_findings"]))


def _transplant_summary() -> dict:
    record = json.loads((OUT / "stage1_transplant_F0200_h2.2_w5_to_F0200_h2.0_w5_dilate1.json").read_text(encoding="utf-8"))
    return dict(material_cm3=record["material_cm3"], safety_factor=round(record["safety_factor"], 3),
               checks_all_pass=all(v == "pass" for v in record["checks"].values()),
               min_width_ok=record["min_width_ok"])


def build() -> dict:
    stage1 = _stage1_summary()
    transplant = _transplant_summary()

    cases = [
        dict(case_id="stage1_shape_registry", kind="input_inventory", status="ok",
             description="現行形・保持R2除外形・5%形・移植トラスの実在・ハッシュ・条件検査",
             output="output/idle_validation_20260928/01_stage1_shapes.json",
             script="topopt_checks/idle_validation/inventory_stage1_shapes.py",
             result_summary=stage1),
        dict(case_id="stage1_transplant_regen", kind="regeneration", status="ok",
             description="移植トラス(F0200_h2.2_w5→F0200_h2.0_w5, dilate=1)の再生成。旧stdout印字のみだった値をハッシュ付きレコード化",
             output="output/idle_validation_20260928/stage1_transplant_F0200_h2.2_w5_to_F0200_h2.0_w5_dilate1.json",
             script="topopt_checks/idle_validation/reproduce_stage1_transplant.py",
             result_summary=transplant,
             finding="acceptance.inspectの6条件は全pass・安全率8.87で文書報告値と一致するが、製品の最小幅規則(5セル=9.99mm)は不合格。"
                     "「同条件での確定的な優越形」は未実証(10_SYNTHESIS 付録A(旧 01_PROTOCOL 2-4節))"),
        dict(case_id="compare_unit_split", kind="protocol_definition", status="defined",
             description="旧64条件=セル数指定比較(凍結・追加実行なし)、新規追加=mm固定比較(min_width_mm指定・実現セル数を記録)に分離",
             output="docs/topopt_idle_validation/10_SYNTHESIS.md 付録A(旧 01_PROTOCOL#3)", execution="none (この区分は台帳規約。実行は各後続タスク)"),
        dict(case_id="hanger_matrix_disposition", kind="protocol_definition", status="defined",
             description="hanger旧64条件(4荷重×4要素サイズ×4幅)の扱い一覧(凍結/再現対照/新規最小限H1・H2/未実行)",
             output="docs/topopt_idle_validation/10_SYNTHESIS.md 付録A(旧 01_PROTOCOL#4)"),
        dict(case_id="hanger_external_check_H1", kind="planned", status="defined_not_executed",
             description="原因2代表条件(低荷重・太幅、200N・w5相当)のmm固定外部確認。比較相手はtask2〜8が選ぶ改善方式",
             depends_on_undetermined="task2〜8が確定する評価式・探索方式",
             assigned_to="task6 (20260928-1426-topopt-idle-06-hanger-low)"),
        dict(case_id="hanger_external_check_H2", kind="planned", status="defined_not_executed",
             description="原因1代表条件(高荷重、2000N・w2相当)のmm固定外部確認。比較相手はtask2〜8が選ぶ改善方式",
             depends_on_undetermined="task2〜8が確定する評価式・探索方式",
             assigned_to="task7 (20260928-1426-topopt-idle-07-hanger-loads)"),
    ]
    for cid, desc, geom, analytic, usage in (
        ("synthetic_S-T", "引張(一様断面角柱、一端固定・他端軸荷重)。解析解あり。調整用", "角柱 約20x20x200mm", True, "tuning"),
        ("synthetic_S-B", "曲げ(片持ち梁、自由端集中荷重)。解析解あり。調整用", "片持ち梁 約10x20x150mm", True, "tuning"),
        ("synthetic_S-TB", "ねじり+曲げ(円形断面片持ち軸、偏心荷重)。全域解析可(フィレット無し版)/フィレット版は数値参照。未使用検証用",
         "円形断面軸 直径約20x200mm、荷重オフセット10mm", "partial", "held_out"),
        ("synthetic_S-L", "L字(直角に接続した2腕、コーナーにフィレット)。腕中央は解析解、コーナーは数値参照のみ。未使用検証用",
         "各腕 約20x20x100mm、フィレット半径5mm", "partial", "held_out"),
        ("synthetic_S-MS", "複数支持(2支持=静定は解析解、3支持=不静定は数値参照のみ)。荷重分担の変化を検証。未使用検証用",
         "支持2〜3点の板/梁、第3支持位置を可変", "partial", "held_out"),
    ):
        assigned = "task2/task5" if usage == "tuning" else "task5"
        cases.append(dict(case_id=cid, kind="material_definition", status="defined_not_executed", description=desc,
                          geometry=geom, analytic_solution=analytic, usage=usage, assigned_to=assigned))

    cases += [
        dict(case_id="origin_shift_ledger", kind="matrix_definition", status="defined_not_executed",
             description="半セル原点移動: 基準+各軸単独±0.5セル(6件)+全軸同時+0.5セル(1件)=8条件。対象は主にtask00小型2題材",
             count=8, assigned_to="task4/task8",
             note="design_space.deriveは原点を明示的に受け取らないため、実装には格子計算のプロセス内パッチが要る(10_SYNTHESIS 付録A(旧 01_PROTOCOL 6-1節))"),
        dict(case_id="initial_shape_ledger", kind="matrix_definition", status="defined_not_executed",
             description="初期形状3種: 全材料(既定)/移植トラス/粗い探索から写す形",
             count=3, assigned_to="task4",
             note="投入前に移植先格子での最小幅・保持・除外を別途検査すること(2-4節の罠と同じ)"),
        dict(case_id="filter_width_ledger", kind="matrix_definition", status="defined_not_executed",
             description="エネルギーフィルタ実幅: (a)セル数固定(現行、物理幅が格子で動く) (b)物理mm固定(セル数を丸めて揃える)",
             count=2, assigned_to="task4/task8"),
        dict(case_id="multi_load_ledger", kind="matrix_definition", status="defined_not_executed",
             description="複数荷重: (a)同時載荷 (b)択一載荷(製品未サポートの可能性あり、要件のみ記録)",
             assigned_to="task5",
             note="択一載荷が製品ネイティブでない場合、実験用パッチと製品改修推奨を混同しない(10_SYNTHESIS 付録A(旧 01_PROTOCOL 11節))"),
    ]

    coverage = {
        "stage1_input_inventory": dict(requirement="現行形・保持R2除外形・5%形・移植トラスの登録(8章1段階1項)",
                                       status="done", evidence=["stage1_shape_registry", "stage1_transplant_regen"]),
        "stage1_compare_unit": dict(requirement="旧セル幅指定/新mm固定の区分(タスク指示)",
                                    status="done", evidence=["compare_unit_split", "hanger_matrix_disposition"]),
        "stage2_search_comparison_inputs": dict(
            requirement="20%/5%/適応歩幅/追加削除比較の題材(hanger低荷重高荷重・引張曲げ・L字・複数支持)",
            status="defined_not_executed",
            evidence=["hanger_external_check_H1", "hanger_external_check_H2", "synthetic_S-T", "synthetic_S-B",
                     "synthetic_S-TB", "synthetic_S-L", "synthetic_S-MS"]),
        "stage2_robustness_matrix": dict(
            requirement="半セル原点移動・初期形状・フィルタ実幅・複数荷重の条件台帳(タスク指示)",
            status="defined_not_executed",
            evidence=["origin_shift_ledger", "initial_shape_ledger", "filter_width_ledger", "multi_load_ledger"]),
        "question_experiment_table": dict(requirement="各疑問→必要実験→判定根拠→出力の対応表(タスク指示)",
                                          status="done", evidence="docs/topopt_idle_validation/10_SYNTHESIS.md 付録A(旧 01_PROTOCOL#7)"),
        "tolerance_basis": dict(requirement="精度・残差・力/モーメント許容値の根拠(タスク指示)",
                                status="done", evidence="docs/topopt_idle_validation/10_SYNTHESIS.md 付録A(旧 01_PROTOCOL#8) (製品既定値を流用、新規閾値なし)"),
        "common_infrastructure": dict(
            requirement="生データ索引・版管理・再開/重複排除・単一FEM実行制御・メモリ監視の共通化(タスク指示)",
            status="done",
            evidence="topopt_checks/idle_validation/_common.py (git_state/sha256_file/write_atomic/peak_memory_mb/"
                     "ExecutionLock/condition_id/load_ledger/upsert_case/is_case_done/SingleFemRun)"),
        "stage1_2_3_coverage_table": dict(requirement="全3段階のcoverage表(完了条件)",
                                          status="done", evidence="docs/topopt_idle_validation/10_SYNTHESIS.md 付録A(旧 01_PROTOCOL#10)"),
        "stage3_final_shape_reanalysis": dict(requirement="平滑化・集合演算後の再解析(8章第3段階)",
                                              status="framework_only",
                                              evidence="docs/topopt_idle_validation/10_SYNTHESIS.md 付録A(旧 01_PROTOCOL#7) "
                                                      "(出力先を10_SYNTHESIS 付録G(旧 09_FINAL_SHAPES)に指定、具体条件はtask9が確定)"),
    }

    limitations = [
        dict(item="移植トラスの最小幅未達",
             detail="2.2mm→2.0mm移植(dilate=1)は6条件(watertight/single_solid/preserve/exclude/conditions/safety)は"
                    "全pass・安全率8.87だが、製品の最小幅規則(5セル=9.99mm)を満たさない。"
                    "docs/TOPOPT_IDLE_MATERIAL.md 2-1節の『同条件での確定的な優越形』という結論は最小幅を満たした形では未実証",
             resolution_required_by="task4/task5 (最小幅を満たす優越形の構成、または限界の明記)"),
        dict(item="実際の取付条件が資料から決定できない",
             detail="hangerの3取付穴が面拘束か点拘束に近いかは未確定。task00契約④で複数保持リング相互作用は対象外と申し送り済み",
             resolution_required_by="本タスク群の対象外(申し送りのみ)"),
        dict(item="半セル原点移動の実装が未着手",
             detail="design_space.deriveは原点を明示的なパラメータとして受け取らない。実験にはプロセス内パッチが要り、"
                    "達成シフト量が意図した0.5セルからずれる可能性がある",
             resolution_required_by="task4/task8"),
        dict(item="合成題材S-TB/S-L/S-MSの具体的寸法・荷重が未決定",
             detail="幾何とゾーン定義(保持部/接続部/一般部)は確定したが、具体的なmm値・荷重値は未実測",
             resolution_required_by="task2またはtask5"),
        dict(item="複数荷重の択一載荷が製品ネイティブでない可能性",
             detail="同時載荷は既存の複数load_faces指定で可能とみられるが、択一載荷(複数ケースのうち最悪値を採用)の"
                    "サポート状況は未確認",
             resolution_required_by="task5"),
        dict(item="hanger外部確認(H1/H2)の具体的実行条件は未確定",
             detail="比較相手がtask2〜8で選ばれる改善方式のため、mm値・実行スクリプトは本タスクでは確定できない",
             resolution_required_by="task6/task7"),
    ]

    return dict(
        generated_at=_common.now_iso(), task_id="20260928-1426-topopt-idle-01-protocol",
        depends_on="20260928-1426-topopt-idle-00-compact-models",
        report="docs/topopt_idle_validation/10_SYNTHESIS.md 付録A(旧 01_PROTOCOL)", git=_common.git_state(),
        cases=cases, coverage=coverage, limitations=limitations,
    )


def main() -> int:
    doc = build()
    out_path = OUT / "01_protocol.json"
    _common.write_atomic(out_path, json.dumps(doc, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path.relative_to(REPO)}: {len(doc['cases'])} cases, {len(doc['coverage'])} coverage items, "
          f"{len(doc['limitations'])} limitations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

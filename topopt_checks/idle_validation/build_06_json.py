"""タスク6の機械可読まとめを組み立てる。前提: `run_06_*.py`・`analyze_06_load_zone.py`の全出力
(06_main_matrix / 06_grid_resolution / 06_origin_shift / 06_initshape / 06_adaptive_variants /
06_w5_reproduction / 06_w5_trajectory / 06_origin_diag / 06_long_budget / 06_load_zone)が揃っていること。

出力: `output/idle_validation_20260928/06_hanger_low.json`(schema_version=1)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import hanger_low_support as hls  # noqa: E402
import run_06_grid_resolution as grid_res  # noqa: E402
import run_06_adaptive_variants as adaptive_variants  # noqa: E402
import run_06_initshape as initshape  # noqa: E402
import run_06_main_matrix as main_matrix  # noqa: E402
import run_06_origin_shift as origin_shift  # noqa: E402
import run_06_w5_reproduction as w5_reproduction  # noqa: E402
import run_06_w5_trajectory as w5_trajectory  # noqa: E402
import run_06_origin_diag as origin_diag  # noqa: E402
import run_06_long_budget as long_budget  # noqa: E402
import analyze_06_load_zone as load_zone  # noqa: E402

OUT_PATH = hls.OUT / "06_hanger_low.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _mask_overlap(path_a: Path, path_b: Path) -> dict:
    """同じ格子の2マスク(npz)の重なり。a=最小幅6セルのstep20、b=最小幅5セルの再現(=保存形hanger_base)。"""
    if not (path_a.is_file() and path_b.is_file()):
        return {}
    with np.load(path_a) as za, np.load(path_b) as zb:
        a, b = np.asarray(za["mask"], dtype=bool), np.asarray(zb["mask"], dtype=bool)
    inter = int((a & b).sum())
    return dict(cells_a=int(a.sum()), cells_b=int(b.sum()), intersection_cells=inter,
                fraction_of_a_inside_b=inter / max(int(a.sum()), 1), fraction_of_b_inside_a=inter / max(int(b.sum()), 1))


def main() -> int:
    main_ledger = _load(main_matrix.LEDGER_PATH)
    grid_ledger = _load(grid_res.LEDGER_PATH)
    origin_ledger = _load(origin_shift.LEDGER_PATH)
    init_ledger = _load(initshape.LEDGER_PATH)
    variants_ledger = _load(adaptive_variants.LEDGER_PATH)
    w5_ledger = _load(w5_reproduction.LEDGER_PATH)
    traj_ledger = _load(w5_trajectory.LEDGER_PATH)
    diag_ledger = _load(origin_diag.LEDGER_PATH)
    long_ledger = _load(long_budget.LEDGER_PATH)
    load_zone_data = _load(load_zone.OUT_PATH)

    cases = []
    cases.extend(main_ledger.get("cases", []))
    cases.extend(grid_ledger.get("cases", []))
    cases.extend(origin_ledger.get("cases", []))
    cases.extend(init_ledger.get("cases", []))
    cases.extend(w5_ledger.get("cases", []))
    cases.extend(variants_ledger.get("cases", []))  # 追加調整版(凍結版の主成績とは別枠)
    cases.extend(traj_ledger.get("cases", []))  # w5の評価ごとの軌跡
    cases.extend(diag_ledger.get("cases", []))  # 全軸原点シフトの打ち切り診断
    cases.extend(long_ledger.get("cases", []))  # 大きい予算の適応・再配置(短い予算の結果とは別枠)

    def _ok(ledger: dict, case_id: str) -> bool:
        return _common.is_case_done(ledger, case_id)

    main_ok = _ok(main_ledger, hls.BASELINE["tag"])
    grid_ok = {s: _ok(grid_ledger, grid_res._case_id(s)) for s in hls.EXTRA_RESOLUTIONS_MM}
    origin_ok = all(_ok(origin_ledger, cid) for cid in origin_shift.SHIFTS)
    init_ok = {name: _ok(init_ledger, f"hanger_h1_initshape_{name}") for name in ("transplant", "coarse_skeleton")}
    load_zone_ok = bool(load_zone_data.get("results"))

    coverage = [
        dict(requirement="200N・2.0mm・w5の現行形/5%形/R2形/2.2mm由来移植トラスの再現・照合",
            status="satisfied_by_prior_task_plus_reproduction",
            evidence="03b_hanger_common_condition.json(既存、再実行せず引用)。現行形はw5再現"
                     "(06_w5_reproduction.json)で、最小幅5セルの製品step20が保存形hanger_baseと完全一致(same_mask=True、"
                     "31,337セル)することを確認。最小幅を実寸10mm以上(6セル)にすると別の形(139,230mm3、保存形との重なりは"
                     "新形の60%・保存形の33%)になる(10_SYNTHESIS 付録E(旧 06_HANGER_LOW 2節))。5%/R2/移植は03bの保存形の引用のみで再現していない。"),
        dict(requirement="1.8/2.0/2.2/2.4mmの実幅固定比較(格子依存)",
            status="satisfied_step20_only" if (main_ok and all(grid_ok.values())) else "partial",
            evidence="06_main_matrix.json(2.0mm)・06_grid_resolution.json(1.8/2.2/2.4mm)。製品step20のみ。"
                     "適応・再配置の格子依存は小型05が主実施。"
                     f"完了: 2.0mm={main_ok}, " + ", ".join(f"{s}mm={ok}" for s, ok in grid_ok.items())),
        dict(requirement="4方式(20%/5%/適応/再配置)の比較",
            status="executed_long_budget_adaptive_and_reallocation_do_not_reach_step20" if main_ok else "not_done",
            evidence="06_main_matrix.json(H1条件、search_methods.pyの同一インフラ)。step20/step05はconverged。"
                     "縮小予算の適応(50評価)はevaluation_limit、再配置(30評価x2)はgrow_cycles_exhaustedで未収束。"
                     "06_long_budget.json(凍結パラメータで300評価上限/60x3サイクル)では、適応は82評価でfinish_stagnation"
                     "(1,806,870mm3・SF84.3)、再配置は243評価でgrow_cycles_exhausted(1,555,520mm3・SF75.7、"
                     "追加3回とも全材料へ回帰)、拒否なし適応(gate=m5b_target)は200評価でfinish_budget_exhausted"
                     "(557,596mm3・SF30.7)。いずれもstep20(139,230mm3・SF4.84)に届かず、評価を増やしても材料が減らない領域に入っている"
                     "(10_SYNTHESIS 付録E(旧 06_HANGER_LOW) 2・5.1・9節)。hanger固有に調整したパラメータでの到達点は未実施。"),
        dict(requirement="原点半セル移動",
            status="satisfied_representative" if origin_ok else "not_done",
            evidence="06_origin_shift.json(全軸・x・y・z各+0.5セルの4点。フル8方向は小型05が実施済み)。"
                     "全軸+0.5は探索が22,915セル・SF4.812まで進んだが、最終の除外体ブーリアンがunknown(common-volume boolean "
                     "did not finish cleanly)になり製品が全材料(99.94%)へ戻してevaluation_errorとしたため比較不能"
                     "(06_origin_diag.jsonのregion_check_calls・final_region_recheckで診断)。"),
        dict(requirement="初期形状(全材料/移植トラス/粗い骨組み開始)",
            status="satisfied_not_converged" if (main_ok and all(init_ok.values())) else "partial",
            evidence="全材料=06_main_matrix.jsonのadaptive/reallocationと06_adaptive_variants.json(拒否なし版・別枠)。"
                     "移植/粗い骨組み=06_initshape.json(40評価・未収束)。粗い骨組みは島処理で投影失敗→保持セル244個を補修。"),
        dict(requirement="部材復元の証拠(斜めの部材=2.2mmトラス専有セルの残存率)",
            status="partial_no_recovery_observed" if main_ok else "not_done",
            evidence="残存率は*_truss_residual_fractionと06_w5_trajectory.json。w5(最小幅5セル)の評価ごとの軌跡では、"
                     "斜材のあたりは評価5の98%から評価12の14%へ、安全率55→17・最大変位0.16〜0.53mmのまま失われ、評価13の1手で"
                     "安全率16.99→5.21・最大変位0.53→7.64mmに顕在化して製品は受理した(軌跡は単調減少で回復なし)。"
                     "残存率の定義は5セル版の現行形を引き算するため5セル版は定義上0%(循環)で、荷重経路としての寄与は測っていない。"
                     "失った斜材が戻る回復はどの実験でも未確認。最小幅6セルのstep20の評価ごとの軌跡は未記録(製品コードが返さない)。"),
        dict(requirement="48.72 MPa一般部ピークの位置・幾何・荷重経路・離散化の影響",
            status="partial_discretization_not_separated" if load_zone_ok else "not_done",
            evidence="03A 7-1節の原因(荷重面4.99mmの荷重パッド端)を_fem_probe.zone_masks_with_load(新設)で再確認(06_load_zone.json)。"
                     "ピークは最小幅5セルの保存形(hanger_base/R2)にだけあり、最小幅6セルの新規形・解像度3・H2には出ない。"
                     "形の寄与と離散化の寄与は未分離(保存形を細分格子へ厳密投影して解き直す検証が残る)。"),
        dict(requirement="評価基準の変更と探索方式変更の交互作用",
            status="partial_target_sf_neighborhood_not_reached",
            evidence="gate=m5b_raw/m5b_targetの全材料開始(80評価)は同一マスク。m5b_target版(長予算200評価)でもm5b_targetは"
                     "safety_gateを加えるが同じ候補がsudden_jumpでも拒否されるため結果は変わらない(120/129件が両理由)。"
                     "prediction_error拒否の有無(方式側)は-69%(1.81M→0.56M mm3)で、ゲートの差(0%)より大きい。"
                     "どちらもSF30〜84で止まり目標SF近傍での比較は未到達。製品ゲートと局所評価(M5b_target/KS)の食い違いは8形で確認"
                     "(10_SYNTHESIS 付録E(旧 06_HANGER_LOW 8節))。05のm5b_rawゲートの目標SF未達は、粗い骨組み開始(SF4.459)でhangerでも再現。"),
    ]

    limitations = [
        "小型題材を主にする変更(共通指示書)により、4方式x幅x解像度x初期形状のフルマトリクスは"
        "小型モデル(task05)が主実施。hangerは代表条件の外部確認に限定した(10_SYNTHESIS 付録E(旧 06_HANGER_LOW) スコープの決定)。",
        "主マトリクスのadaptive/reallocationの評価予算は、hanger規模のFEM費用のため小型05(100/60x3)より縮小(50/30x2)。"
        "追加で凍結パラメータのまま長予算(300評価上限/60x3)を実行したが(06_long_budget.json)、step20に届かず評価を増やしても"
        "材料が減らない領域に入った。仕上げ段階の上限(finish_max_evaluations=200)・finish_patience・sudden_jump閾値など"
        "パラメータ自体を替えたhanger固有調整と、別シードでの再現は未実施(各方式1シード)。",
        "reallocation(方式4)はwarm-start専用入口が無い(10_SYNTHESIS 付録D(旧 05_SYNTHETIC)「制約」4と同じ)ため、"
        "本タスクでも全材料開始のみ実施した。",
        "原点半セル移動は全軸+0.5と単軸+0.5の4方向のみ(負方向・2軸組合せは未実施)。全軸+0.5は探索自体は"
        "SF4.812まで進んだが、最終検算の除外体ブーリアンが失敗して製品が全材料へ戻したため比較不能(失敗の根本原因は未切り分け)。",
        "H2(高荷重代表)は4ゾーン診断のみ(4方式比較は本タスクの対象外、task07が引き継ぐ)。",
        "トラス専有セルの残存率は5セル版の現行形を引き算した定義で、荷重経路としての寄与を測らない。",
        "荷重パッド端ピークの形の寄与と離散化の寄与は分離していない。",
    ]

    payload = dict(
        schema_version=1, generated_at=_common.now_iso(), code_version=_common.git_state(),
        baseline_condition=dict(load_n=hls.BASELINE["load_n"], element_size_mm=hls.BASELINE["element_size_mm"],
                               min_width_mm=hls.BASELINE["min_width_mm"]),
        cases=cases, load_zone_diagnostics=load_zone_data.get("results", []),
        derived=dict(step20_w6_vs_stored_hanger_base=_mask_overlap(
            hls.MASK_DIR / "hanger_h1_baseline_step20_best_ever.npz",
            hls.MASK_DIR / "hanger_h1_w5_reproduction_best_ever.npz")),
        coverage=coverage, limitations=limitations,
    )
    _common.write_atomic(OUT_PATH, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {OUT_PATH} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

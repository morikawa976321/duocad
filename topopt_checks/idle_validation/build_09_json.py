"""段階9の機械可読まとめ(新しいFEM・最適化なし)。`09_work/*.json` と保存台帳から `09_final_shapes.json` を組む。

実行: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\build_09_json.py`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _common  # noqa: E402
import final_shapes_09 as fs  # noqa: E402

OUT = fs.OUT
WORK = fs.WORK

ORDER = ["S1_dil25_realloc", "S1c_dil25_step20", "S2_hanger200_step20", "S3_hanger2000_step20"]
ROLE = {
    "S1_dil25_realloc": "小型の代表改善形(製品SFを満たしたうえで同条件の製品step20より軽い保存形のうち最大の-3.8%)",
    "S1c_dil25_step20": "S1の同条件対照(製品step20)。選定した形状3つには数えない(FEM予算4回のうち1回)",
    "S2_hanger200_step20": "hanger低荷重の合格候補(06のH1: 200N・2.0mm・最小幅10mm→6セル。製品SF・M5b rawを満たす)",
    "S3_hanger2000_step20": "hanger高荷重の代表形(07の2000N太い幅・2.2mmの製品step20。2000Nの太い幅でM5b rawの超過が最小)",
}
RESOLVE_SOURCE = {"S1_dil25_realloc": "final", "S1c_dil25_step20": "final",
                  "S2_hanger200_step20": "smoothed_keep", "S3_hanger2000_step20": "smoothed_keep"}


def read(name: str):
    path = WORK / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def pick(d: dict, keys):
    return {k: d.get(k) for k in keys}


def main() -> int:
    ledger = _common.load_ledger(OUT / "09_runs.json")
    shapes = []
    for sid in ORDER:
        conv, cut = read(f"{sid}_convert.json"), read(f"{sid}_cutdiag.json")
        src = RESOLVE_SOURCE[sid]
        res = read(f"{sid}_resolve_{src}.json")
        res_plain = read(f"{sid}_resolve_smoothed.json")
        diag = res["diagnosis"] if res else None
        row = dict(
            shape_id=sid, role=ROLE[sid], spec={k: str(v) for k, v in fs.SHAPES[sid].items()},
            saved_reference=conv["saved_reference"], grid=conv["grid"], voxel=conv["voxel"],
            smoothed=conv["smoothed"], finalize=conv["final"], finalize_seconds=conv.get("finalize_seconds"),
            cut_diagnosis=cut,
            plain_smoothed_resolve=None if res_plain is None else dict(
                status=res_plain["status"], fem_status=res_plain["diagnosis"].get("fem_status"),
                conditions=res_plain["conditions"], mask=res_plain["mask_stats"]),
            resolve=None if res is None else dict(
                source=src, status=res["status"], conditions=res.get("conditions") or conv["final"]["conditions"], mask=res["mask_stats"],
                diagnosis=pick(diag, ("material_mm3", "safety_factor_product", "representative_mpa", "m5b_raw", "m5b_raw_passed",
                                      "m5b_target", "m5b_target_passed", "m3_worst_zone", "m3_worst_value", "m3_passed",
                                      "m7b_ks_mpa", "max_displacement_mm", "peak", "zones", "min_width", "mask_hash")),
                equilibrium_residual_n=diag["supports"]["equilibrium_residual_n"],
                applied_load_magnitude_n=diag["supports"]["applied_load_magnitude_n"], wall_seconds=res["wall_seconds"]))
        shapes.append(row)

    s1, s1c = shapes[0], shapes[1]
    derived = dict(
        s1_vs_step20_after_finalize=dict(
            material_mm3=[s1["resolve"]["diagnosis"]["material_mm3"], s1c["resolve"]["diagnosis"]["material_mm3"]],
            material_ratio=s1["resolve"]["diagnosis"]["material_mm3"] / s1c["resolve"]["diagnosis"]["material_mm3"] - 1,
            safety_factor_product=[s1["resolve"]["diagnosis"]["safety_factor_product"], s1c["resolve"]["diagnosis"]["safety_factor_product"]],
            m5b_target=[s1["resolve"]["diagnosis"]["m5b_target"], s1c["resolve"]["diagnosis"]["m5b_target"]],
            note="同じ格子・実現幅・荷重の2形だが、探索の受理ゲートは違う(S1=M5b raw、S1c=製品パーセンタイル)ので体積差を優劣の根拠にしない。値は再占有化した同格子のもので、最終CADの細格子の応力ではない"),
        saved_vs_resolved=[dict(
            shape_id=r["shape_id"], source=r["resolve"]["source"],
            safety_factor_product=[r["saved_reference"]["safety_factor_product"], r["resolve"]["diagnosis"]["safety_factor_product"]],
            material_mm3=[r["saved_reference"]["material_mm3"], r["resolve"]["diagnosis"]["material_mm3"]],
            m5b_raw=[r["saved_reference"]["m5b_raw_exceedance_mpa_mm3"], r["resolve"]["diagnosis"]["m5b_raw"]],
            m5b_target=[r["saved_reference"]["m5b_target_exceedance_mpa_mm3"], r["resolve"]["diagnosis"]["m5b_target"]],
            cell_iou_vs_saved_mask=(r["finalize"]["vs_voxel"]["iou"] if r["finalize"]["status"] == "ok" else r["smoothed"]["vs_voxel"]["iou"]))
            for r in shapes if r["resolve"]])
    out = dict(
        schema_version=1, generated_at=_common.now_iso(), code_version=_common.git_state(paths=("duocad", "topopt_checks")),
        scope=("期限再編版の工程09(縮小範囲)。保存済み完成形候補から3形状(+小型の対照1)を選び、平滑化・集合演算の前後で幾何を検査し、"
               "同格子へ再占有化した固定形状をFEMで再解析した。新規の最適化はしていない。原計画の全検証の完了ではない"),
        shapes=shapes, derived=derived, run_ledger=ledger.get("cases", []),
        coverage=dict(
            selected_shapes=3, control_shapes=1, fem_solves=4, fem_solve_limit=4,
            conversions=dict(small_wall_limit_s=1200, hanger_wall_limit_s=1800), fem_wall_limit_s=2700,
            timeouts=sum(1 for c in ledger.get("cases", []) if c.get("status") == "timeout"),
            hanger_final_cad=dict(attempted=2, succeeded=0), small_final_cad=dict(attempted=2, succeeded=2)),
        limitations=[
            "最終CADの再解析は、最終形(または近似)を元の粗い格子へ中心サンプリングで再占有化した固定形状の1回解析。細分格子での応力は未確認(局所ピークは固定面の特異点で格子とともに増える。02)",
            "hangerの2形は最終ブーリアンが不成立のため、最終CADそのものを解析していない。平滑化メッシュの再占有化に保持セルを足した近似(最終ブーリアンの保持fuseの代用)を解析した",
            "小型は1形・1条件。hangerは各1形。代表性は選定基準の範囲に限る(統計的な代表ではない)",
            "最小幅の半格子測定の損失しきい値(0.1%・1%)は解析者が置いた値で未校正。損失の列そのものを一次の根拠とする",
            "M5b目標換算・M3は92/92不合格のままで、再占有化でも変わらない(製品SFゲートだけを満たす形)",
        ],
        deferred_by_user_deadline=[
            "最終CADを細分格子(半セル以下)へ占有化したFEM再解析(FEM予算4回で足りない)",
            "hangerの最終ブーリアン不成立の修正(薄い破片の除去・保持の再構成)と、修正後の最終形の再検証",
            "hanger 500N・1000Nの5セル形(旧コーパスで最終形が成立した2.2mm格子の形)の再解析",
            "小型の完成形での複数条件・複数seedの再現",
            "hangerの高荷重(2000N)での最終形評価(出発点がM5b rawを満たさない=07)",
        ])
    path = OUT / "09_final_shapes.json"
    _common.write_atomic(path, json.dumps(out, ensure_ascii=False, indent=1, default=float) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

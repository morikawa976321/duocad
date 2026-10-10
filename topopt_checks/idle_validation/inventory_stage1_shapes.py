"""段階1(01_PROTOCOL)の入力棚卸し: 現行形・保持R2除外形・5%形・移植トラスの実在・ハッシュ・条件を検査し、
`output/idle_validation_20260928/01_stage1_shapes.json` へ登録する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\inventory_stage1_shapes.py

既存データ(`output/idle_study/*.json`+`.npz`、`output/smoothing_corpus/manifest.json`)の数値を
`docs/TOPOPT_IDLE_MATERIAL.md` の報告値と突き合わせ、乖離があれば`mismatches`に記録する
(黙って一致したことにしない)。移植トラスは保存レコードが無かった(`output/idle_study/transplant.py`は
stdout印字のみ)ため、`reproduce_stage1_transplant.py`を先に実行しておくこと(無ければこのスクリプトが
実行する)。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402

IDLE_STUDY = REPO / "output" / "idle_study"
CORPUS = REPO / "output" / "smoothing_corpus"
OUT = REPO / "output" / "idle_validation_20260928"

#: `docs/TOPOPT_IDLE_MATERIAL.md`の報告値(1-4節・2-2節の表)。mm3はcm3表記からの逆算(±0.1cm3の丸め)。
DOC_REPORTED_MM3 = {
    ("F0200_h2.4_w2", "R2"): 76700.0,
    ("F0200_h2.0_w5", "R2"): 165900.0,
    ("F1000_h2.4_w2", "R2"): 184400.0,
    ("F2000_h2.4_w2", "R2"): 337400.0,
    ("F0200_h2.0_w5", "step05"): 155000.0,
    ("F0200_h2.0_w5", "base"): 249700.0,
}
TOLERANCE_MM3 = 300.0  # cm3表記(小数1桁)への丸め往復ぶん


def _record_for(name: str, variant: str) -> dict:
    stem = f"{name}_{variant}"
    json_path, npz_path = IDLE_STUDY / f"{stem}.json", IDLE_STUDY / f"{stem}.npz"
    if not json_path.is_file() or not npz_path.is_file():
        return dict(shape_kind=f"stage1_{variant}", condition=name, status="missing",
                   note=f"{json_path.relative_to(REPO)} または {npz_path.relative_to(REPO)} が無い")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    import numpy as np

    npz = np.load(npz_path)
    reported = DOC_REPORTED_MM3.get((name, variant))
    material_mm3 = data.get("material_mm3")
    mismatch = None
    if reported is not None and material_mm3 is not None and abs(material_mm3 - reported) > TOLERANCE_MM3:
        mismatch = dict(doc_reported_mm3=reported, measured_mm3=material_mm3, diff_mm3=material_mm3 - reported)
    return dict(
        shape_kind=f"stage1_{variant}", condition=name,
        # `data["status"]`は製品の`SearchResult.status`をそのまま転記する(counterfactual.pyの出力は実際に
        # "feasible"を持ち、この登録では"ok"に丸められずそのまま残る。実測: 01_stage1_shapes.jsonの全stage1_base/
        # stage1_R2/stage1_step05エントリが"status": "feasible")。"ok"分岐は、他の生成元(statusキーを持たない
        # 記録)や将来の製品戻り値が"ok"を使う場合のための保険。material_mm3が無ければ(候補が見つからなかった等)
        # "unknown"のまま残す。"missing"(上の早期return)とは別概念——"missing"は「ファイル自体が無い」、
        # ここはファイルはあって中身も読めている状態。
        status="ok" if data.get("status") in ("ok", None) and material_mm3 is not None else data.get("status", "unknown"),
        load_n=data.get("load_n"), element_size_requested_mm=data.get("size"), min_width_cells=data.get("cells"),
        exclude_radius_cells=data.get("radius"), max_step_ratio=data.get("max_step_ratio"),
        spacing_mm=data.get("spacing"), origin_mm=[float(v) for v in npz["origin"]],
        grid_shape=list(npz["mask"].shape),
        material_mm3=material_mm3, material_cm3=None if material_mm3 is None else round(material_mm3 / 1000, 1),
        safety_factor_used=data.get("safety_factor_used"), evaluations=data.get("evaluations"),
        seconds=data.get("seconds"), keep_cells=int(npz["keep"].sum()), material_cells=int(npz["mask"].sum()),
        doc_cross_check=dict(reported_mm3=reported, matches=mismatch is None if reported is not None else None,
                             mismatch=mismatch),
        note="acceptance.inspectの6条件・最小幅は未検査(このレコードは探索直後の候補。00_COMPACT_MODELS.md/task00契約②と同じ理由で"
             "探索の合否と最終acceptanceの合否は別の関門)。",
        files=dict(json=str(json_path.relative_to(REPO)), json_sha256=_common.sha256_file(json_path),
                  npz=str(npz_path.relative_to(REPO)), npz_sha256=_common.sha256_file(npz_path)),
        code_version=dict(script=f"output/idle_study/{'transplant' if variant == 'transplant' else 'counterfactual'}.py",
                          script_sha256=_common.sha256_file(IDLE_STUDY / "counterfactual.py")
                          if (IDLE_STUDY / "counterfactual.py").is_file() else None,
                          note="output/idle_study/配下はこのリポジトリの.gitignore対象(/output/)で、gitのコミットハッシュで"
                               "版を特定できない。ここではファイル自体のsha256を版の代わりにする(10_SYNTHESIS 付録A(旧 01_PROTOCOL)「入力棚卸し」節参照)。"),
    )


def _base_current_form(name: str) -> dict:
    """「現行形」= 製品どおり(反実仮想の基準R=-1相当)。base(20%歩幅)ファイルがあればそれを使う。"""
    return _record_for(name, "base")


def _transplant_record() -> dict:
    stem = "stage1_transplant_F0200_h2.2_w5_to_F0200_h2.0_w5_dilate1"
    json_path = OUT / f"{stem}.json"
    if not json_path.is_file():
        subprocess.run([sys.executable, str(REPO / "topopt_checks" / "idle_validation" / "reproduce_stage1_transplant.py")],
                       check=True, cwd=REPO)
    record = json.loads(json_path.read_text(encoding="utf-8"))
    record["doc_cross_check"] = dict(
        doc_reported_material_cm3=198.4, doc_reported_safety_factor=8.87,
        measured_material_cm3=record["material_cm3"], measured_safety_factor=record["safety_factor"],
        matches=abs(record["material_cm3"] - 198.4) < 0.5 and abs(record["safety_factor"] - 8.87) < 0.05,
    )
    return record


def main() -> int:
    entries = []
    entries.append(_base_current_form("F0200_h2.0_w5"))
    for name in ("F0200_h2.0_w5", "F0200_h2.4_w2", "F1000_h2.4_w2", "F2000_h2.4_w2"):
        entries.append(_record_for(name, "R2"))
    entries.append(_record_for("F0200_h2.0_w5", "step05"))
    entries.append(_transplant_record())

    mismatches = [e for e in entries if e.get("doc_cross_check", {}).get("matches") is False]
    new_findings = []
    transplant = entries[-1]
    if transplant.get("min_width_ok") is False:
        new_findings.append(
            "移植トラス(F0200_h2.2_w5→F0200_h2.0_w5, dilate=1)は acceptance.inspect の6条件は全pass・安全率8.87で"
            "TOPOPT_IDLE_MATERIAL.md 2-1節の報告値と一致するが、**製品の最小幅規則(5セル=9.99mm)を満たさない"
            "(min_width_ok=false)**。同節の記述は6条件のみを根拠にしており、最小幅は検査されていなかった"
            "(acceptance.inspectに最小幅が含まれないのはtask00契約②と同じ既知の罠)。したがってこの移植トラスは"
            "現時点で『同条件での確定的な優越形』の証拠として使えない——hangerでも原因2の限界はpath_smallと"
            "同種(00_COMPACT_MODELS.md 3-4節)。後続タスクへ引き継ぐ(10_SYNTHESIS 付録A(旧 01_PROTOCOL)「入力棚卸し」節)。"
        )

    registry = dict(
        generated_at=_common.now_iso(), git=_common.git_state(),
        stage="01_protocol_stage1_shape_inventory",
        source_note="現行形/保持R2除外形/5%形は output/idle_study/(hanger 64条件コーパスの反実仮想)、"
                    "移植トラスは reproduce_stage1_transplant.py で今回再生成(旧stdout印字のみだったものを"
                    "ハッシュ付きレコードへ格上げ)。",
        entries=entries,
        mismatches=mismatches,
        new_findings=new_findings,
    )
    out_path = OUT / "01_stage1_shapes.json"
    _common.write_atomic(out_path, json.dumps(registry, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path.relative_to(REPO)}: {len(entries)} entries, {len(mismatches)} mismatches, {len(new_findings)} new_findings")
    for f in new_findings:
        print("FINDING:", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

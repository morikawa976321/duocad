"""段階1タスク3の機械可読まとめを再生成する(引数なし)。`run_metric_comparison.py`・
`counterexamples_field_only.py`・`counterexamples_material_edit.py`・`counterexamples_origin_shift.py`
の出力を集約し、`03_metrics.json`(cases/coverage/limitations)を書き直す。**手で編集しないこと**
(10_SYNTHESIS 付録A(旧 01_PROTOCOL)のQA指摘を踏まえた運用と同じ——追記・変更はこのスクリプトのソースを直す)。
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

CANDIDATES = [
    dict(name="full_population_percentile", label="M1 全材料99.5%点(現行製品)",
         formula="percentile(v[material], 99.5)"),
    dict(name="excluded_population_percentile", label="M2 R2除外99.5%点",
         formula="percentile(v[material & ~holding_zone], 99.5)"),
    dict(name="zone_percentile", label="M3 部位別99.5%点",
         formula="all(percentile(v[zone], 99.5) <= allowable for zone in {holding,joint,general})"),
    dict(name="top_volume_rank", label="M4a 固定物理体積の上位順位",
         formula="sorted(v[material], desc)[k-1], k=round(V_top/cell_volume)"),
    dict(name="top_volume_average", label="M4b 固定物理体積の上位平均",
         formula="mean(sorted(v[material], desc)[:k]), k=round(V_top/cell_volume)"),
    dict(name="exceedance_volume", label="M5a 限度超過体積",
         formula="count(v[material] > allowable) * cell_volume"),
    dict(name="exceedance_integral", label="M5b 限度超過量積分",
         formula="sum(v[material][v>allowable] - allowable) * cell_volume  [MPa*mm^3]"),
    dict(name="p_mean", label="M6 p平均(正規化)",
         formula="mean(v[material]**p) ** (1/p)"),
    dict(name="p_norm_unnormalized", label="M7a 非正規化pノルム",
         formula="sum(v[material]**p) ** (1/p)"),
    dict(name="ks_aggregation", label="M7b KS集約",
         formula="vmax + (1/rho) * ln(sum(exp(rho*(v[material]-vmax))))"),
]


def _load(name: str) -> dict | None:
    path = OUT / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    comparison = _load("metric_comparison.json")
    field_only = _load("counterexamples_field_only.json")
    material_edit = _load("counterexamples_material_edit.json")
    origin_shift = _load("counterexamples_origin_shift.json")

    cases = []
    if comparison:
        for c in comparison["cases"]:
            if c.get("status") != "ok":
                cases.append(dict(case_id=c.get("case_id"), kind="baseline_comparison", status=c.get("status"),
                                   shape=c.get("shape"), mask_source=c.get("mask_source"), error=c.get("error")))
                continue
            cases.append(dict(
                case_id=c["case_id"], kind="baseline_comparison", status="ok", shape=c["shape"],
                mask_source=c["mask_source"], load_n=c["load_n"], level=c["level"],
                material_elements=c["material_elements"], allowable_mpa=c["allowable_mpa"],
                safety_factor_product=c["safety_factor_product"],
                metric_verdicts={name: dict(value=m["value"], passed=m["passed"]) for name, m in c["metrics"].items()},
            ))

    counterexamples = dict(
        a_far_low_stress_field_only=dict(
            description="場だけの操作。実在1解の値配列に遠方(一般部相当)の低応力セルを0/1x/5x/20x継ぎ足す。",
            source_file="counterexamples_field_only.json#a_far_low_stress_cells",
            present=field_only is not None,
        ),
        b_material_removal_addition=dict(
            description="幾何/力学成立。一般部の低寄与材料20%除去、および1セル膨張での追加。再FEM実施。",
            source_file="counterexamples_material_edit.json#results",
            present=material_edit is not None,
        ),
        c_danger_zone_relocation=dict(
            description="幾何/力学成立(hanger H1/H2の実測、低荷重=一般部にピーク・高荷重=保持部にピーク)"
                        "+ 場だけの操作(zone別に1セルへ許容応力の1.25倍を注入)。",
            source_file="metric_comparison.json(hanger_external_check group) + "
                        "counterexamples_field_only.json#e_hotspot_transfer",
            present=comparison is not None and field_only is not None,
        ),
        d_peak_concentration_field_only=dict(
            description="場だけの操作。超過量の合計40MPa相当を1/5/20/100セルへ分散/集中。",
            source_file="counterexamples_field_only.json#d_peak_concentration",
            present=field_only is not None,
        ),
        e_hotspot_transfer_field_only=dict(
            description="場だけの操作。許容応力の1.25倍の1セルをholding/joint/generalへ入れ替える。",
            source_file="counterexamples_field_only.json#e_hotspot_transfer",
            present=field_only is not None,
        ),
        f_grid_refinement=dict(
            description="幾何/力学成立。dilution 3段階・path 4段階の固定形状格子細分(全候補式を計算)。",
            source_file="metric_comparison.json(fixed_shape_refinement group)",
            present=comparison is not None,
        ),
        g_grid_origin_shift=dict(
            description="幾何/力学成立。design_space.grid_for_frameのプロセス内パッチで原点を"
                        "±0.5/1.0セルずらし再FEM。",
            source_file="counterexamples_origin_shift.json",
            present=origin_shift is not None,
        ),
    )

    coverage = dict(
        candidates_covered=[c["name"] for c in CANDIDATES],
        counterexamples_covered=list(counterexamples.keys()),
        shapes_covered=sorted({c["shape"] for c in cases if c.get("shape")}),
        missing=[
            "path_transplant_mesh_2.5mm_to_2.0mm(close0/close2)は採用マスクnpzが保存されておらず、"
            "本タスクでは生の場を再構成していない(再現には measure_path_local_optimum.py transplant "
            "の再実行が要る。02_REFERENCE契約③のhanger移植トラスとは別物)。",
            "hangerの5%形・移植トラスは02_REFERENCEが未実行のまま(7-1節)で、本タスクも新規実行していない"
            "(小型題材優先の方針どおり、hanger外部確認はH1/H2の2条件に限定)。",
            "M3(部位別)はholding/joint/generalの3ゾーンのみ。5.2節が挙げた『保持部・接続部ごと』の"
            "さらに細かい下位分割(取付穴ごと等)は実装していない。",
        ],
    )

    limitations = [
        "全ケースでallowable_mpa=40.0固定(cond.allowable_mpaがモデル間で共通値だったため)。"
        "モデルごとに異なる許容応力を使う場合はここを条件ごとの値に置き換える必要がある。",
        "M4(固定物理体積)・M6/M7(p/KS)のパラメータ(top_volume_mm3・p・rho)は本タスクが選んだ"
        "研究用の初期値(8mm^3/216mm^3、p=8、rho=1.0)であり、製品定数として較正していない"
        "(10_SYNTHESIS 付録B(旧 03_METRICS 5節)に選定根拠と感度を記載)。",
        "反例(c)(e)の『部位別ですら1セルの局所超過を見逃す』という結果は holding=1064〜joint=684セル"
        "程度の母集団サイズで確認したものであり、母集団がもっと小さい特殊なゾーン(数十セル程度)では"
        "挙動が変わりうる(未検証)。",
        "『独立解析に基づく局所評価』はgmsh等の外部メッシュツールがこの環境に無いため、02_REFERENCEと"
        "同じ制約下で「格子細分による収束の追跡」を代替手段として扱った(10_SYNTHESIS 付録B(旧 03_METRICS 5-8節))。",
    ]

    payload = dict(
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        candidates=CANDIDATES,
        parameters=(comparison or {}).get("parameters"),
        cases=cases,
        counterexamples=counterexamples,
        coverage=coverage,
        limitations=limitations,
        files=dict(
            metric_comparison="output/idle_validation_20260928/metric_comparison.json",
            counterexamples_field_only="output/idle_validation_20260928/counterexamples_field_only.json",
            counterexamples_material_edit="output/idle_validation_20260928/counterexamples_material_edit.json",
            counterexamples_origin_shift="output/idle_validation_20260928/counterexamples_origin_shift.json",
            report="docs/topopt_idle_validation/10_SYNTHESIS.md 付録B(旧 03_METRICS)",
        ),
    )
    out_path = OUT / "03_metrics.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path} ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""03b(訂正後の評価式・反例・採否再検証)の機械可読まとめを再生成する(引数なし)。

03の34ケース比較(`metric_comparison.json`)・03aの訂正(`03a_reference_repair.json`)・本タスクが
追加した4本の新規出力(`03b_hanger_common_condition.json`・`03b_counterexamples_hanger.json`・
`03b_keep_islands_volume_path25.json`・`03b_level4_exceedance_locality.json`)を集約し、
`03b_metrics_recheck.json`(cases/coverage/limitations)を書く。**手で編集しないこと**
(追記・変更はこのスクリプトのソースを直す)。

`coverage`・`limitations`の文言は10_SYNTHESIS 付録B(旧 03B_METRICS_RECHECK)の結論と対応させてある——数値の正はそちらの
JSON入力ファイル、判断の正は報告書本文(このスクリプトはaggregatorであり判断を下さない)。
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


def _load(name: str) -> dict | None:
    path = OUT / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    metric_comparison = _load("metric_comparison.json")
    reference_repair = _load("03a_reference_repair.json")
    hanger_common = _load("03b_hanger_common_condition.json")
    counterex_hanger = _load("03b_counterexamples_hanger.json")
    keep_islands = _load("03b_keep_islands_volume_path25.json")
    level4_locality = _load("03b_level4_exceedance_locality.json")

    cases = [
        dict(
            case_id="03b_reuse_audit_34cases",
            status="ok",
            description=(
                "03の34ケース比較(stress_metrics.py/metric_eval.py/run_metric_comparison.py)が"
                "03aの訂正コード(_fem_probe.richardson_extrapolate_general追加・analytic_checksの曲げ位置"
                "補正・refine_shapeの--exact-subdivide追加)の影響を一切受けていないことを、"
                "git diff 7915b40(task02)..HEADで対象4ファイルの差分ゼロを確認し、さらにhanger_base@H1・"
                "dilution_r-1@300Nの2ケースをmetric_eval.solve_shapeで再計算してmetric_comparison.json"
                "記載値とビット一致することを実測で確認した(浮動小数点まで一致)。"
            ),
            evidence=dict(
                git_diff_affected_files=0,
                spotcheck_cases=["hanger_base:200N:level1:hanger_H1_native",
                                  "dilution_r-1:300N:level1:counterfactual_r-1"],
                spotcheck_match="bit-exact",
            ),
        ),
        dict(
            case_id="03b_hanger_common_condition_scoring",
            status="ok" if hanger_common else "missing_input",
            description=(
                "現行形(hanger_base)・保持部除外形(hanger_r2)・5%歩幅形(hanger_step05)・移植トラス"
                "(hanger_transplant)を、全く同じ条件(200N・要素2.0mm・最小幅5セル、条件文字列"
                "F0200_h2.0_w5)でM1〜M7b全候補+最小幅(保持セル除外版)を採点した。"
            ),
            source_file="03b_hanger_common_condition.json",
        ),
        dict(
            case_id="03b_counterexamples_hanger_extension",
            status="ok" if counterex_hanger else "missing_input",
            description=(
                "反例(a)(母数希釈)・(e)(危険域の移し替え)を、03が使わなかった実形状(hanger_base@H1、"
                "拘束面複数・保持507/接続1606/一般29224セル)へ適用した。"
            ),
            source_file="03b_counterexamples_hanger.json",
        ),
        dict(
            case_id="03b_keep_islands_volume_contribution",
            status="ok" if keep_islands else "missing_input",
            description=(
                "10_SYNTHESIS 付録A(旧 03A_REFERENCE_REPAIR 3-3節)が未定量化と申し送ったkeepセル合成・resolve_islandsの"
                "体積寄与を、path_2.5mm(keep持ち)のlevel2で定量化した。"
            ),
            source_file="03b_keep_islands_volume_path25.json",
        ),
        dict(
            case_id="03b_level4_exceedance_locality",
            status="ok" if level4_locality else "missing_input",
            description=(
                "10_SYNTHESIS 付録B(旧 03_METRICS 5-6節)のpath_2.5mm@6000N・level4で新たに現れた8超過セルが、"
                "design_space.deriveの外枠再計算(03A 3節)が及ぶ境界近傍にあるか、内部にあるかを判定した。"
            ),
            source_file="03b_level4_exceedance_locality.json",
        ),
    ]

    coverage = [
        dict(
            requirement="03の全候補式・棄却理由・合否・推奨パラメータを03aの訂正に対応付け、"
                        "ハッシュ一致を確認して再利用/再採点/再実行を判定する",
            status="解消",
            evidence="03b_reuse_audit_34cases(本ファイルcases[0])。全34ケース訂正不要、再実行対象0件。",
        ),
        dict(
            requirement="raw_max/dense_max/percentile/固定物理体積の集約を区別し、"
                        "物理体積平均を未検証の理由で除外しない",
            status="解消(03aの実測を追認)",
            evidence="stress_metrics.py全10候補はmaterial_maskのみを母集団にしraw_maxを一切使わない"
                     "(コード読解で確認済み、03aの10_SYNTHESIS 付録B(旧 03_METRICS 1節)の記述とも一致)。"
                     "固定物理体積の局所平均は10_SYNTHESIS 付録A(旧 03A_REFERENCE_REPAIR 5節)が既に実測済みで、"
                     "本タスクはこれを追加で除外していない。",
        ),
        dict(
            requirement="保持/接続以外の危険箇所(荷重接続部等)を覆い、空隙を分母や平均に混ぜる抜け道を検査",
            status="解消",
            evidence="全metric_*関数は_population_valuesでmaterial_mask(またはそのサブセットのzone_mask)"
                     "のみを母集団にする実装をコード読解で確認(空隙混入の抜け道なし)。"
                     "荷重接続部の危険(03A 7節のhanger48.72MPaピーク)はM3(部位別)では引き続き見逃すが、"
                     "M5b(採用ゲート)はゾーン定義に依存せず全域で捕捉することをhanger_base再採点で再確認。",
        ),
        dict(
            requirement="原因1の純粋な母数操作と実際の形状変更後FEMを対にし、"
                        "保持部除外だけの軽量化を改善成功としない",
            status="解消",
            evidence="03b_hanger_common_condition_scoring。hanger_r2を200N/w5(hanger_baseと同一条件)で"
                     "全母集団M1により再採点すると安全率3.726(検索目標4.8未達、ただし製品floor2.0は満たす)。"
                     "R2探索が使った除外母集団評価では4.84だった数値が、製品既定の全母集団評価では"
                     "異なることを共通条件下で再確認(00/REVIEWの元の指摘を、条件を揃えた形で再現)。",
        ),
        dict(
            requirement="元の03が要求した全反例・部位別検査を維持し、題材を増やし未使用例でも試す",
            status="解消",
            evidence="03b_counterexamples_hanger_extension。反例(a)(e)をhanger_base(H1)という"
                     "03未使用の実形状・複数拘束面題材へ拡張し、同じ結論(母数希釈でM1が破綻、"
                     "M3はゾーン注入に無反応)を確認。",
        ),
        dict(
            requirement="現行/R2/5%/移植を同一条件で共通採点し、最小幅を6条件に加える",
            status="解消",
            evidence="03b_hanger_common_condition_scoring。4形状ともF0200_h2.0_w5で採点。"
                     "最小幅は_min_width_exemptで保持セルを除外した製品基準の判定を使用"
                     "(exempt無しの厳格版は現行形自身も不合格になり製品基準と矛盾するため不採用、"
                     "min_width_ok_strict_no_exemptフィールドに参考値として残す)。",
        ),
        dict(
            requirement="物理校正が足りない式を研究用と表示し、採用できる式が無ければ理由を明示",
            status="解消(03の凍結を維持)",
            evidence="10_SYNTHESIS 付録B(旧 03_METRICS 6-7節)の採否(M5b採用/M7b探索補助/他不採用)は03aの訂正で変わる"
                     "根拠が無いことを本タスクで確認(03b_reuse_audit_34cases)。研究用未較正パラメータ"
                     "(top_volume_mm3・p・rho・tolerance)の扱いも据え置く。",
        ),
        dict(
            requirement="keepセル合成・resolve_islandsの体積寄与を定量化する(03aの申し送り)",
            status="解消",
            evidence="03b_keep_islands_volume_contribution。path_2.5mm level2でkeep寄与0セル・"
                     "resolve_islands除去0セル(keepは構造的にmaskの部分集合のため加算が常に0になる)。",
        ),
        dict(
            requirement="00の時間・メモリ表とJSONの差を追跡する(03aの申し送り)",
            status="解消(記録上の揺らぎと確認、実害なし)",
            evidence="dilution_2.0mm_m2.json等(run_compact_search.py生成)のdesign_elements/material_mm3/"
                     "safety_factorは00_COMPACT_MODELS.mdの記載と厳密一致。所要時間・メモリのみ"
                     "別回の実行間variance(最大12%程度)があり、run_id・generated_atが記録されていない"
                     "という運用上の改善点は残るが、数値そのものの誤りではない。",
        ),
        dict(
            requirement="03の報告・集約に訂正先を明記し、再現コマンド・版・失敗例・選定変更表を残す",
            status="解消",
            evidence="10_SYNTHESIS 付録B(旧 03B_METRICS_RECHECK)本文(9節相当)に再現コマンド・新規ファイル一覧・03aとの対応表。",
        ),
    ]

    limitations = [
        "本タスクの新規実験(hanger共通条件採点・hanger反例拡張・keep/islands定量化・level4局所性判定)は"
        "全てdilution_r-1/path_2.5mm/hanger_base/hanger_r2/hanger_step05/hanger_transplantの既存6題材に"
        "限定している。06/07が予定する複数支持リング・他のhanger条件への一般化は本タスクの範囲外。",
        "hanger_r2の全母集団再採点(安全率3.726)は200N/w5の1条件のみ。R2探索が使ったF0200_h2.4_w2"
        "(w2、element2.4mm)条件では未検証——条件が変わると安全率の絶対値も変わるため、本タスクの数値を"
        "R2探索の元の条件へそのまま外挿しない。",
        "level4の8超過セルが境界非隣接(荷重接続部でも一般部でもなくholdingゾーン)と判定できたことは、"
        "「境界外枠再計算のアーティファクトではない」ことの証拠にはなるが、「真に物理収束した値である」"
        "ことの証明ではない(03A4節のholding zone maxがno_solution=判定不能である事実は変わらず、"
        "level5以降でさらに超過セルが増える可能性は排除できない)。",
        "M5bの合否ゲート採用・M7bの探索補助採用という03の凍結(6-7節)自体は本タスクで変更していない"
        "(変更を要する新証拠が出なかったため)。物理破損データとの校正(tolerance・p・rhoの較正)は"
        "引き続きタスク9まで持ち越し。",
    ]

    payload = dict(
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        source_task="20260928-1426-topopt-idle-03b-metrics-recheck",
        depends_on="20260928-1426-topopt-idle-03a-reference-repair",
        cases=cases,
        coverage=coverage,
        limitations=limitations,
    )
    out_path = OUT / "03b_metrics_recheck.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

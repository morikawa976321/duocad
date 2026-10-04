"""03a(独立解析の訂正・参照値再検証)の機械可読まとめを再生成する(引数なし)。

`load_sweep_full_material.py`・`03a_local_volume_average.json`(手動生成、後述)・既存の
`refine_*.json`(task02が保存したdilution_r-1/path_2.5mmの格子細分レコード)・
`analytic_bending.json`(本タスクで曲げ採点位置を補正)を集約し、`03a_reference_repair.json`
(cases/coverage/limitations)を書く。**手で編集しないこと**(追記・変更はこのスクリプトのソースを直す)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import _fem_probe  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"


def _load(name: str) -> dict | None:
    path = OUT / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _zone_convergence_case(shape: str, load_n: float, levels: list[int]) -> dict:
    recs = []
    for lv in levels:
        p = OUT / f"refine_{shape}_load{int(load_n) if load_n == int(load_n) else load_n}_level{lv}.json"
        if not p.is_file():
            return dict(case_id=f"03a_zone_convergence_{shape}_{load_n}", status="missing_input", missing=str(p))
        recs.append(json.loads(p.read_text(encoding="utf-8")))
    element_mm = [r["element_mm"] for r in recs]
    zones_out = {}
    for zone in ("holding", "joint", "general"):
        max_vals = [r["zone_stats"][zone]["max_mpa"] for r in recs]
        pct_vals = [r["zone_stats"][zone]["percentile_mpa"] for r in recs]
        entry = dict(max_mpa=max_vals, percentile_mpa=pct_vals)
        if len(max_vals) >= 3:
            rich = _fem_probe.richardson_extrapolate_general(max_vals, element_mm)
            entry["richardson_max"] = rich
        if len(pct_vals) >= 3:
            rich = _fem_probe.richardson_extrapolate_general(pct_vals, element_mm)
            entry["richardson_percentile"] = rich
        zones_out[zone] = entry
    rep_vals = [r["representative_mpa"] for r in recs]
    raw_vals = [r["raw_max_mpa"] for r in recs]
    levels_tag = "-".join(str(lv) for lv in levels)
    out = dict(
        case_id=f"03a_zone_convergence_{shape}_{load_n}_levels{levels_tag}",
        status="ok",
        shape=shape,
        load_n=load_n,
        levels=levels,
        element_mm=element_mm,
        is_geometric_ratio=len(set(round(element_mm[i] / element_mm[i + 1], 6) for i in range(len(element_mm) - 1))) == 1
        if len(element_mm) >= 2
        else None,
        zones=zones_out,
        representative_mpa=rep_vals,
        raw_max_mpa=raw_vals,
    )
    if len(rep_vals) >= 3:
        out["richardson_representative"] = _fem_probe.richardson_extrapolate_general(rep_vals, element_mm)
    if len(raw_vals) >= 3:
        out["richardson_raw_max"] = _fem_probe.richardson_extrapolate_general(raw_vals, element_mm)
    return out


def main() -> int:
    cases = []

    load_sweep = _load("03a_load_sweep_full_material.json")
    if load_sweep:
        cases.append(dict(case_id="03a_load_sweep_full_material", status="ok", source="03a_load_sweep_full_material.json",
                           summary="全材料dilution模型、荷重150/200/250/300/350Nの安全率を再取得しrun-id/ハッシュ付きで保存。"
                                    "safety_factor*load_n が8桁一致(線形弾性と整合)。00_COMPACT_MODELS.mdの200N:4.77/"
                                    "250N:5.97は誤り、正しくは200N:8.38/250N:6.71。",
                           records=load_sweep["records"]))
    else:
        cases.append(dict(case_id="03a_load_sweep_full_material", status="missing_input"))

    local_avg = _load("03a_local_volume_average.json")
    if local_avg:
        cases.append(dict(case_id="03a_local_volume_average", status="ok", source="03a_local_volume_average.json",
                           summary="dilution_r-1@300N、holding zoneピーク位置(level1で固定)まわりの固定物理半径"
                                    "(2/3/6/9mm)平均応力は3段細分で数%以内に安定(raw_max/zone-maxが2〜3倍に発散するのと対照)。",
                           records=local_avg["results"]))
    else:
        cases.append(dict(case_id="03a_local_volume_average", status="missing_input"))

    # ゾーン内max/raw_max/representativeの一般化Richardson再解析
    cases.append(_zone_convergence_case("dilution_r-1", 300.0, [1, 2, 3]))
    cases.append(_zone_convergence_case("dilution_r-1", 200.0, [1, 2, 3]))
    cases.append(_zone_convergence_case("path_2.5mm", 1500.0, [1, 2, 3, 4]))
    cases.append(_zone_convergence_case("path_2.5mm", 6000.0, [1, 2, 3, 4]))
    cases.append(_zone_convergence_case("path_2.5mm", 1500.0, [1, 2, 4]))
    cases.append(_zone_convergence_case("path_2.5mm", 6000.0, [1, 2, 4]))

    bending = _load("analytic_bending.json")
    if bending:
        cases.append(dict(case_id="03a_bending_position_corrected", status="ok", source="analytic_bending.json",
                           summary="採点位置(x=probe+h/2、断面はh/2内側)を補正した理論値と比較すると、"
                                    "レビューの0.88%(h=2.5mm)を再現し、真の等比細分(levels 1 2 4 8)ではh=1.25mmで0.23%まで縮小、"
                                    "一般化Richardson外挿はp≈1.0・v_inf≈23.98MPa(理論24.0MPaの0.075%以内)。"
                                    "02_REFERENCEの「15%の差は3次元効果」は採点位置ずれ+非等比Richardsonの誤差計上だったと判明。",
                           records=bending["records"]))
    else:
        cases.append(dict(case_id="03a_bending_position_corrected", status="missing_input"))

    # hanger H1 一般部48.72MPaピークの原因特定(7節)。metric_eval.solve_shapeで再解析した結果は
    # JSON化していないため、hanger_peak_origin.pyの実測をここに書き起こす(再現手順は7節参照)。
    cases.append(dict(
        case_id="03a_hanger_h1_general_peak_origin", status="ok",
        shape="hanger_base", load_n=200.0, level=1,
        summary="一般部ピーク(peak_index=[41,35,78])は拘束面から190.7/191.0/213.1mm、荷重面から4.99mmで、"
                 "荷重パッド接触端にある。zone_masksが荷重面近傍ゾーンを持たないため一般部(29,224セル)へ"
                 "希釈される。",
        peak_index=[41, 35, 78],
        peak_world_mm=[0.99869542, -193.03356681, 41.79488028],
        peak_principal_mpa=[-6.1638917278803795, -16.340782427902585, -59.16973584564884],
        distance_to_fixed_boxes_mm=[190.73514734897918, 190.98598411241574, 213.08051910273713],
        distance_to_load_boxes_mm=[4.993477337964833],
        holding_zone_peak_index=[43, 130, 27],
        holding_zone_peak_world_mm=[4.99347729, -3.28142796, -60.07205742],
        holding_zone_distance_to_fixed_boxes_mm=[118.37806616818732, 120.38320071951603, 0.9986954675929667],
        holding_zone_distance_to_load_boxes_mm=[211.7093061749751],
        limitation="H1条件・このピーク1点のみを確認した。他条件・他ピークへの一般化は06/07の作業。",
    ))

    exact_subdivide_check = OUT / "03a_exact_subdivide_check" / "refine_dilution_r-1_load300_level2.json"
    if exact_subdivide_check.is_file():
        rec = json.loads(exact_subdivide_check.read_text(encoding="utf-8"))
        cases.append(dict(
            case_id="03a_exact_subdivide_volume_check", status="ok",
            source=str(exact_subdivide_check.relative_to(REPO)),
            summary="dilution_r-1 level2@300Nで、整数分割(体積保存)マスクと既存の独立regrid+最近傍投影マスクは"
                     "完全一致(0セル差)。体積差(88368→88013mm3)は投影の再サンプル誤差ではなく、"
                     "design_space.deriveがspacingごとに設計空間の外枠を独立再計算し355セル(0.40%)が"
                     "外枠の外へ出ることが原因(exact_subdivide_diagnostics参照)。",
            exact_subdivide_diagnostics=rec.get("exact_subdivide_diagnostics"),
            material_elements=rec.get("material_elements"),
        ))
    else:
        cases.append(dict(case_id="03a_exact_subdivide_volume_check", status="not_run_in_this_build",
                           note="03A_REFERENCE_REPAIR.md 3節の再現コマンドで再実行できる(検証用、34ケース比較用データは変更しない)"))

    payload = dict(
        task_id="20260928-1426-topopt-idle-03a-reference-repair",
        generated_at=_common.now_iso(),
        git_head=_common.git_state(),
        cases=cases,
        coverage=dict(
            requirements=[
                "raw_max/dense_maxの誤用監査(材料内ピークとの混同箇所の洗い出し)",
                "曲げパッチテストの採点位置補正・非等比Richardsonの一般化",
                "refine_shape.pyの体積変化の定量化・整数分割経路の実装",
                "反力/パッチテスト以外の独立評価(固定物理体積の局所平均)",
                "00の全材料荷重スイープの非逆比例の追跡・訂正",
                "hanger H1/H2制限の是正・一般部48.72MPaピークの原因特定",
            ],
            addressed_in_this_task=[
                "raw_max監査: 00/01/02本文にraw_maxの誤用は無し(02は一貫してraw_maxを製品不使用・材料外と明記)。"
                "コード上raw_maxは空セル(rho<rho_min)を含む全要素maxであることをstress.py/_fem_probe.pyで確認",
                "曲げ採点位置補正+一般化Richardson実装、真の等比細分(levels 1,2,4,8)で再取得",
                "refine_shape.py体積変化の根本原因特定(design_space.derive の外枠再計算、投影ではない)。"
                "整数分割ヘルパー実装・テスト済み、既存投影と完全一致することを実証",
                "固定物理体積の局所平均(holding zoneピーク近傍)の格子収束を実測、raw_max/zone-maxと対比",
                "全材料荷重スイープをrun-id/ハッシュ付きで再取得、00の200N/250N記載の誤りを実測で訂正",
                "hanger一般部48.72MPaピークが荷重パッド接触端(距離4.99mm)であることを特定。"
                "zone_masksに'load'ゾーンが無いことがgeneral zoneへの希釈原因と特定",
                "01のhanger新規実行制限は上位のTASKS文書(0〜2レビュー後の訂正節)で既に是正済みであることを確認・明記",
            ],
            deferred_with_assignment=[
                "dilution_r-1の等比細分(level4, spacing 0.5mm)によるholding zone maxの厳密な収束/発散判定: "
                "既知の設計要素安全上限(約130万)に近いためこのタスクでは実行せず、03b/09で必要なら実施",
                "hanger H1以外の条件(H2・500/1000N等)でのload-zone希釈の一般性確認、zone_masksへのload zone追加実装: 06/07",
                "path_2.5mmのkeep合成・resolve_islandsが体積差に与える寄与の分離定量化: 03b",
                "00の時間・メモリ表とJSONの差のrun-id追跡: 未着手、03bへ",
                "真に独立な局所応力評価(gmsh等の外部メッシュ・submodel): 環境制約により未実装(02/03既報の継続)",
            ],
        ),
        limitations=[
            "本タスクはコスト・時間を理由に検証範囲を削っていないが、'計算の継続と資源'節の安全上限"
            "(設計要素約130万)を超える新規重量計算(dilution level4等)は行っていない——これは時間の都合ではなく、"
            "OOMの実リスクを回避する判断。",
            "hanger 48.72MPaピークの'load zone'仮説はH1の1条件・1ピークでのみ確認した(距離4.99mm対190mm超)。"
            "他条件・他ピークへの一般化は未検証。",
        ],
    )
    out = OUT / "03a_reference_repair.json"
    _common.write_atomic(out, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""タスク7の機械可読まとめ(保存済み台帳の集計。新しいFEMは走らせない)。

入力: `output/idle_validation_20260928/` の 07_corpus_rescore / 07_grid / 07_methods / 07_full_material_probe(あれば)
と、合流先の 06_main_matrix / 06_grid_resolution。出力: `07_hanger_loads.json`(schema_version=1)。

2026-10-05の期限再編(`docs/topopt_idle_validation/DEADLINE_PLAN_2026-10-05.md`)で、origin/init 行列と methods の残り19件は
実行されず、`deferred_by_user_deadline` として `coverage` に列挙する。
実行: `.venv/Scripts/python.exe topopt_checks/idle_validation/build_07_json.py`
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import hanger_loads_support as hl  # noqa: E402

OUT = hl.OUT
OUT_PATH = OUT / "07_hanger_loads.json"
#: 製品の目標安全率。製品SFは二分探索の許容幅でこの値を僅かに下回って止まるため 0.5% の余裕を見て判定する。
TARGET_SF = 4.8
SF_TOLERANCE = 0.995
FACES = ("10", "7", "23")


def _ledger(name: str) -> dict:
    p = OUT / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"cases": []}


def _row(case_id, load, req_size, diag, *, size_resolved, cells, realized_mm, material, sf, extra=None):
    peak = diag["peak"]
    pf = diag["supports"]["per_face"]
    tot = sum(v["force_magnitude_n"] for v in pf.values()) or 1.0
    zones = {z: v for z, v in diag["zones"].items() if v.get("cells")}
    ex_total = sum(v["exceedance_target_mpa_mm3"] for v in zones.values()) or 1.0
    row = dict(
        case_id=case_id, load_n=load, element_size_mm=req_size, element_size_resolved_mm=size_resolved,
        min_width_cells=cells, min_width_realized_mm=realized_mm, material_mm3=material, safety_factor_product=sf,
        sf_gate_passed=sf >= TARGET_SF * SF_TOLERANCE, m5b_raw_exceedance=diag["m5b_raw"], m5b_raw_passed=diag["m5b_raw_passed"],
        m5b_target_exceedance=diag["m5b_target"], m5b_target_passed=diag["m5b_target_passed"],
        m3_worst_zone=diag["m3_worst_zone"], m3_worst_value_mpa=diag["m3_worst_value"], m3_passed=diag["m3_passed"],
        ks_mpa=diag["m7b_ks_mpa"], raw_max_mpa=diag["raw_max_mpa"], max_displacement_mm=diag["max_displacement_mm"],
        peak_zone=peak["zone"], peak_mpa=peak["von_mises_mpa"], peak_nearest_fixed_face=peak["nearest_fixed_face"],
        peak_distance_to_fixed_mm=peak["distance_to_fixed_mm"], peak_distance_to_load_mm=peak["distance_to_load_mm"],
        hotspot_count=diag["hotspot_count"], hotspot_cells=diag["hotspot_cells"],
        hotspot_support_exceedance={k: v["exceedance_target_mpa_mm3"] for k, v in diag["hotspot_by_support"].items()},
        zone_exceedance_share={z: v["exceedance_target_mpa_mm3"] / ex_total for z, v in zones.items()},
        zone_max_mpa={z: v["max_mpa"] for z, v in zones.items()},
        reaction_force_share={k: pf[k]["force_magnitude_n"] / tot for k in FACES if k in pf},
        reaction_force_over_load={k: pf[k]["force_magnitude_n"] / load for k in FACES if k in pf},
        equilibrium_residual_n=diag["supports"]["equilibrium_residual_n"],
        low_stress_volume_fraction=diag["low_stress_volume_fraction"], low_energy=diag["low_energy"],
        mask_hash=diag["mask_hash"],
    )
    row.update(extra or {})
    return row


def _spread(values):
    values = list(values)
    return dict(min=min(values), max=max(values), mean=statistics.fmean(values), n=len(values))


def _ref06_row(case_id, source, c, method, s):
    return dict(case_id=case_id, source=source, load_n=200.0, element_size_resolved_mm=c["element_size_resolved_mm"],
                min_width_cells=c["min_width_cells_resolved"], min_width_realized_mm=c["min_width_guaranteed_mm"],
                width_key="thick", method=method, material_mm3=s["material_mm3"], safety_factor_product=s["safety_factor_product"],
                m5b_raw_exceedance=s["m5b_raw_exceedance_mpa_mm3"], m5b_target_exceedance=s["m5b_target_exceedance_mpa_mm3"],
                evaluations=s["evaluations"], stop_reason=s["stop_reason"], wall_hours=s["wall_seconds"] / 3600.0,
                mask_hash=s["mask_hash"])


def main() -> int:
    corpus = _ledger("07_corpus_rescore.json")
    grid = _ledger("07_grid.json")
    methods = _ledger("07_methods.json")
    probe = _ledger("07_full_material_probe.json")
    ref06_main = _ledger("06_main_matrix.json")
    ref06_grid = _ledger("06_grid_resolution.json")

    status_counts = {n: dict(Counter(c.get("status") for c in led["cases"]))
                     for n, led in (("07_corpus_rescore", corpus), ("07_grid", grid), ("07_methods", methods),
                                    ("07_full_material_probe", probe))}

    corpus_rows, repro = [], []
    for c in corpus["cases"]:
        o, g = c["old"], c["diagnosis"]
        corpus_rows.append(_row(
            c["case_id"], o["load_n"], o["element_size_requested_mm"], g, size_resolved=o["element_size_resolved_mm"],
            cells=c["min_width_cells_resolved"], realized_mm=c["min_width_realized_mm"], material=g["material_mm3"],
            sf=g["safety_factor_product"],
            extra=dict(old_material_mm3=o["material_mm3"], old_safety_factor=o["safety_factor"], old_evaluations=o["evaluations"],
                       old_wall_hours=o["total_seconds"] / 3600.0, grid_match=c["grid_match"], rescore_seconds=c["wall_seconds_total"])))
        repro.append(dict(d_sf=abs(o["safety_factor"] - g["safety_factor_product"]),
                          d_material=abs(o["material_mm3"] - g["material_mm3"]), grid_match=c["grid_match"]))

    grid_rows = []
    for c in grid["cases"]:
        s, g = c["search"], c["diagnosis"]
        grid_rows.append(_row(
            c["case_id"], c["load_n"], c["element_size_mm"], g, size_resolved=c["element_size_resolved_mm"],
            cells=c["min_width_cells_resolved"], realized_mm=c["min_width_realized_mm"], material=s["material_mm3"],
            sf=s["safety_factor_product"],
            extra=dict(width_key=c["width_key"], width_requested_mm=c["min_width_mm_requested"], method=c["kind"],
                       stop_reason=s["stop_reason"], evaluations=s["evaluations"], fem_solves=s["fem_solves"],
                       wall_hours=s["wall_seconds"] / 3600.0, peak_working_set_mb=c.get("peak_working_set_mb"),
                       unknown_safety_count=c["unknown_safety"]["unknown_count"])))

    # 06の200N・太い幅(合流先)。06台帳の値を転記するだけで再計算しない。
    merged06 = []
    for c in ref06_main["cases"]:
        for key in ("step20", "step05"):
            merged06.append(_ref06_row(f"{c['case_id']}_{key}", "06_main_matrix.json", c, key, c[key]))
        for key in ("adaptive", "reallocation"):
            merged06.append(_ref06_row(f"{c['case_id']}_{key}_frozen", "06_main_matrix.json(縮小予算50/30x2評価)", c,
                                       f"{key}_frozen", c[key]))
    for c in ref06_grid["cases"]:
        merged06.append(_ref06_row(f"{c['case_id']}_step20", "06_grid_resolution.json", c, "step20", c["step20"]))

    method_rows = []
    for c in methods["cases"]:
        s, g = c["search"], c["diagnosis"]
        steps = c["search_full"]["steps"]
        outcomes = Counter(f"{st['kind']}/{st['outcome']}/{'+'.join(st['reasons']) or '-'}" for st in steps)
        m5b_over_steps = [st["m5b_raw_exceedance_mpa_mm3"] for st in steps if st.get("m5b_raw_exceedance_mpa_mm3") is not None]
        initial = c["search_full"].get("initial_material_mm3")
        method_rows.append(_row(
            c["case_id"], c["load_n"], c["element_size_mm"], g, size_resolved=c["element_size_resolved_mm"],
            cells=c["min_width_cells_resolved"], realized_mm=c["min_width_realized_mm"], material=s["material_mm3"],
            sf=s["safety_factor_product"],
            extra=dict(width_key=c["width_key"], method=c["kind"], gate=c.get("gate", "percentile(製品)"),
                       prediction_error_multiplier=c.get("prediction_error_multiplier"), stop_reason=s["stop_reason"],
                       evaluations=s["evaluations"], fem_solves=s["fem_solves"], wall_hours=s["wall_seconds"] / 3600.0,
                       peak_working_set_mb=c.get("peak_working_set_mb"), initial_material_mm3=initial,
                       steps_total=len(steps), steps_accepted=sum(1 for st in steps if st["outcome"] == "accepted"),
                       step_outcomes=dict(outcomes),
                       m5b_raw_exceedance_range_over_steps=[min(m5b_over_steps), max(m5b_over_steps)] if m5b_over_steps else None,
                       best_equals_initial=initial is not None and abs(initial - s["material_mm3"]) < 1e-6)))

    probe_rows = []
    for c in probe["cases"]:
        if c.get("status") != "ok":
            probe_rows.append(dict(case_id=c["case_id"], status=c.get("status"), diagnosis=c.get("diagnosis")))
            continue
        g = c["diagnosis"]
        probe_rows.append(_row(c["case_id"], c["load_n"], c["element_size_mm"], g, size_resolved=c["element_size_resolved_mm"],
                               cells=0, realized_mm=None, material=c["material_mm3"], sf=g["safety_factor_product"],
                               extra=dict(source="07_full_material_probe.json(固定形状・新規FEM1回)", wall_seconds=c["wall_seconds_total"])))
    for r in method_rows:  # 2000Nの全材料は適応の初期形=最良形(同一マスク)。新規FEMなし。
        if r["best_equals_initial"] and r["case_id"].endswith("adaptive_frozen"):
            probe_rows.append(dict(r, case_id="F2000_full_material_h2", source="07_methods.json(適応の初期形=最良形。新規FEMなし)"))

    all_rows = corpus_rows + grid_rows

    def gate_counts(rows):
        return dict(n=len(rows), sf_gate=sum(r["sf_gate_passed"] for r in rows), m5b_raw=sum(r["m5b_raw_passed"] for r in rows),
                    m5b_target=sum(r["m5b_target_passed"] for r in rows), m3=sum(r["m3_passed"] for r in rows))

    by_load = defaultdict(list)
    for r in all_rows:
        by_load[r["load_n"]].append(r)
    gates = {f"{int(k)}N": gate_counts(v) for k, v in sorted(by_load.items())}
    gates["corpus(64)"] = gate_counts(corpus_rows)
    gates["grid(28)"] = gate_counts(grid_rows)

    zone_support = {}
    for load, rs in sorted(by_load.items()):
        zs, hs = Counter(), Counter()
        for r in rs:
            for z, v in r["zone_exceedance_share"].items():
                zs[z] += v / len(rs)
            tot = sum(r["hotspot_support_exceedance"].values()) or 1.0
            for k, v in r["hotspot_support_exceedance"].items():
                hs[k] += v / tot / len(rs)
        zone_support[f"{int(load)}N"] = dict(
            n=len(rs), zone_exceedance_share_mean=dict(zs), hotspot_support_exceedance_share_mean=dict(hs),
            peak_zone_counts=dict(Counter(r["peak_zone"] for r in rs)), peak_mpa=_spread(r["peak_mpa"] for r in rs),
            ks_mpa=_spread(r["ks_mpa"] for r in rs), hotspot_count=_spread(r["hotspot_count"] for r in rs),
            reaction_force_share_mean={f: statistics.fmean(r["reaction_force_share"][f] for r in rs) for f in FACES},
            reaction_force_over_load_mean={f: statistics.fmean(r["reaction_force_over_load"][f] for r in rs) for f in FACES},
            low_stress_lt_0p1_fraction=_spread(r["low_stress_volume_fraction"]["vm_lt_0.1x_target_allowable"] for r in rs),
            low_energy_lowest5pct_volume_fraction=_spread(
                r["low_energy"]["volume_fraction_carrying_lowest_0.05_energy"] for r in rs))

    sens = defaultdict(list)
    for r in grid_rows:
        sens[(r["load_n"], r["width_key"])].append(dict(
            size=r["element_size_resolved_mm"], cells=r["min_width_cells"], realized=r["min_width_realized_mm"],
            material=r["material_mm3"], sf=r["safety_factor_product"], hash=r["mask_hash"], src="07_grid"))
    for r in merged06:
        if r["method"] == "step20":
            sens[(200.0, "thick")].append(dict(
                size=r["element_size_resolved_mm"], cells=r["min_width_cells"], realized=r["min_width_realized_mm"],
                material=r["material_mm3"], sf=r["safety_factor_product"], hash=r["mask_hash"], src="06"))
    grid_sensitivity = {}
    for (load, wk), v in sorted(sens.items()):
        mats = [x["material"] for x in v]
        grid_sensitivity[f"{int(load)}N_{wk}"] = dict(
            points=sorted(v, key=lambda x: x["size"]), material_max_over_min=max(mats) / min(mats),
            realized_width_range_mm=[min(x["realized"] for x in v), max(x["realized"] for x in v)])

    def _find(load, wk, size):
        return next(r for r in grid_rows if r["load_n"] == load and r["width_key"] == wk and abs(r["element_size_mm"] - size) < 1e-9)

    thick_thin = {}
    for load in (500.0, 1000.0, 2000.0):
        for size in (1.8, 2.0, 2.2, 2.4):
            th, tn = _find(load, "thick", size), _find(load, "thin", size)
            thick_thin[f"{int(load)}N_h{size}"] = dict(
                thick_mm3=th["material_mm3"], thin_mm3=tn["material_mm3"], thick_over_thin=th["material_mm3"] / tn["material_mm3"],
                thick_cells=th["min_width_cells"], thin_cells=tn["min_width_cells"],
                thick_realized_mm=th["min_width_realized_mm"], thin_realized_mm=tn["min_width_realized_mm"])

    # 旧コーパス(セル数指定)と同じ格子・同じセル数になった新探索のマスクハッシュ一致。
    by_key = {(r["load_n"], r["element_size_mm"], r["min_width_cells"]): r for r in corpus_rows}
    identity = []
    for r in grid_rows:
        o = by_key.get((r["load_n"], r["element_size_mm"], r["min_width_cells"]))
        if o is None:
            continue
        identity.append(dict(
            grid_case=r["case_id"], corpus_case=o["case_id"],
            same_resolved_grid=abs(o["element_size_resolved_mm"] - r["element_size_resolved_mm"]) < 1e-9,
            same_mask_hash=o["mask_hash"] == r["mask_hash"], material_grid=r["material_mm3"], material_corpus=o["material_mm3"]))

    ks_vs_grid = {}
    for r in all_rows:
        if r["load_n"] == 2000.0:
            ks_vs_grid.setdefault(r["element_size_mm"], []).append(r["ks_mpa"])
    ks_vs_grid = {str(k): _spread(v) for k, v in sorted(ks_vs_grid.items())}

    cost = dict(
        corpus_rescore_seconds=_spread(r["rescore_seconds"] for r in corpus_rows),
        corpus_original_search_hours=_spread(r["old_wall_hours"] for r in corpus_rows),
        step20_grid_hours=_spread(r["wall_hours"] for r in grid_rows), step20_grid_hours_total=sum(r["wall_hours"] for r in grid_rows),
        step20_grid_evaluations=_spread(r["evaluations"] for r in grid_rows),
        methods_hours={r["case_id"]: r["wall_hours"] for r in method_rows},
        methods_hours_total=sum(r["wall_hours"] for r in method_rows),
        peak_working_set_mb_max=max(r.get("peak_working_set_mb") or 0 for r in grid_rows + method_rows),
    )

    coverage = [
        dict(item="旧64条件(荷重4x格子4x最小幅2〜5セル)の同一評価器での再採点", status="done",
             evidence="07_corpus_rescore.json 64件ok。保存マスクの格子一致64/64、製品SFと材料体積は旧記録と完全一致(derived.rescore_reproduction)。"),
        dict(item="荷重500/1000/2000N x 細い/太い最小幅 x 格子1.8〜2.4mm の製品step20(24)+200N細い幅(4)", status="done",
             evidence="07_grid.json 28件ok。実現幅はセル丸めのため格子で変わる(細い5.39〜7.22mm・太い10.79〜12.03mm)。"),
        dict(item="4方式比較(2.0mm・6条件x4方式=24)", status="partial_deferred_by_user_deadline",
             evidence="完了5件(2000N太い: step05/適応凍結/再配置凍結/拒否なし適応、2000N細い: step05)。残り19件は未実施(deferred_by_user_deadline)。"
                      "2000N細い・適応凍結は2026-10-05 19:04に約9.7時間走った後 exit=-1 で台帳に書かず死亡(原因未特定・checkpoint無し。本人の期限停止19:40より前)。"),
        dict(item="原点移動(16)", status="deferred_by_user_deadline",
             evidence="未実行。shift_all(2000N太い)は2026-10-05 19:05に開始した直後、本人の期限変更(19:40)で停止(interrupted_by_user_deadline)。台帳・マスクなし。"),
        dict(item="初期形状(8)", status="deferred_by_user_deadline", evidence="未実行。"),
        dict(item="支持/荷重ゾーン・高応力域・支持別反力・最小幅の併記", status="done",
             evidence="全97形(64+28+5)と全材料の診断2形のdiagnosisに4ゾーン・高応力域の塊の支持帰属・支持別反力と釣合い残差・低寄与材料・実現最小幅を保存。荷重別のゾーン・反力・ゲート集計(derived)は旧64+新28=92形(methods 5形は除く)。"),
        dict(item="M5b(raw/目標換算)・M3・KS・製品SF(パーセンタイル)のゲート差の併記", status="done",
             evidence="derived.gates_by_load。製品SFを満たした形はすべて M5b目標換算・M3 が不合格。M5b raw は荷重・形で合否が分かれる。"),
        dict(item="06(200N)との合流", status="done", evidence="merged_06_reference(06台帳の転記)と derived.grid_sensitivity(200N太い幅を含む)。"),
        dict(item="欠損を埋める固定形状診断(最大2件・各45分)", status="see_full_material_probe",
             evidence="全材料の500N・1000N(2.0mm)。full_material_probe。"),
    ]

    limitations = [
        "旧64条件はセル数指定(実現幅3.6〜11.9mm)、新28条件は実寸mm指定で、別表。混ぜて順位付けしない。",
        "新規探索は1シード・各条件1回。格子・原点・最小幅を動かしたときの形の変化(離散化と形の分離)は未検証。",
        "最小幅の実現値が格子で変わる(太い幅: 6セル10.79/11.98mm、5セル10.99/12.03mm)ため、格子の効果と幅の効果が分離していない。",
        "適応・再配置の3本は2000N太い幅の1条件のみ。全材料がゲートを満たさないため全材料のまま動かず、方式の優劣は測れていない。",
        "ゾーン『holding』は固定面から3セル以内。固定面端の応力集中が格子を細かくするほど上がる傾向(2000NのKS 約44→60 MPa、2.4→1.8mm)と整合するが、特異性かどうかは未切り分け。",
        "低寄与材料のしきい値(目標許容応力の5/10/25%、エネルギー下位1/5%)は解析者の値で未校正。",
        "製品の安全性の保証ではない。荷重・支持の物理入力が実機と一致するかは外部入力。",
    ]

    payload = dict(
        schema_version=1, generated_at=_common.now_iso(), code_version=_common.git_state(),
        deadline_note="2026-10-05期限再編: 保存済み台帳の集計のみ。新規最適化なし。",
        ledgers=dict(status_counts=status_counts),
        corpus_rescore=corpus_rows, grid=grid_rows, merged_06_reference=merged06, methods=method_rows,
        full_material_probe=probe_rows,
        derived=dict(
            rescore_reproduction=dict(max_d_sf=max(r["d_sf"] for r in repro), max_d_material_mm3=max(r["d_material"] for r in repro),
                                      grid_match_count=sum(r["grid_match"] for r in repro), n=len(repro)),
            gates_by_load=gates, zone_support_by_load=zone_support, grid_sensitivity=grid_sensitivity,
            thick_over_thin=thick_thin, same_mask_as_corpus=identity, ks_vs_grid_at_2000N=ks_vs_grid,
            max_equilibrium_residual_n=max(r["equilibrium_residual_n"] for r in all_rows), cost=cost),
        interrupted_or_lost=[
            dict(case_id="F2000_thin5mm_h2_adaptive_frozen", status="lost_exit_minus1",
                 note="2026-10-05T19:04:49 に台帳へ書かず終了コード-1。約9.7時間。checkpoint無し。原因未特定。"),
            dict(case_id="F2000_thick10mm_h2_step20_shift_all_+0.5cell", status="interrupted_by_user_deadline",
                 note="2026-10-05T19:05開始、19:40の本人停止で中断。台帳・マスクなし。"),
        ],
        coverage=coverage, limitations=limitations,
    )
    _common.write_atomic(OUT_PATH, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {OUT_PATH} (corpus={len(corpus_rows)} grid={len(grid_rows)} methods={len(method_rows)} probe={len(probe_rows)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

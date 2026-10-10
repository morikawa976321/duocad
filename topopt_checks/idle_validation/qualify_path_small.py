"""タスク4a: `topopt_idle_path_small.json` の2.0mm格子(現行探索=`step20`が経路を失う格子)で、
同条件・同評価器・同実現最小幅で合格する優越形を実際に構成し、経路喪失の対照実験を行う。

00_COMPACT_MODELS.md 3-4節の未達点(「同格子上の確定的な優越形」)を埋めるのが目的。
2.5mm格子(現行探索が斜材解へ収束する格子)の採用マスクを2.0mm格子へ移植すると、幾何は全合格するが
安全率が目標(4.8)にわずかに届かない(4.40〜4.48)。本スクリプトはこの移植形を起点に、
`search_methods._growth_geometry_ok` で検査しながら局所的に材料を再導入し(段B)、
`run_adaptive_from_mask` で過剰分を削って仕上げ(段B')、2.0mm現行(`step20`)結果より軽く
目標安全率を満たす形を作る(移植=段A。段ラベルは `docs/topopt_idle_validation/10_SYNTHESIS.md` 付録C 2節と揃える)。

続けて、`search_methods.run_adaptive` を `gate="percentile"`・`stop_policy="legacy"`・
`sudden_*`/`prediction_error_multiplier` 無効化に設定すると製品の `_reduce_volume` と同じ受理条件・
同じ `peel`/`ranked_cells`/`step_ratio` を辿る対照になることを利用し、`extra_protected` で
段B/B'が見つけた斜材専有セルを保護した場合/しない場合を比較する(段C)。`on_accept` で受理ごとの
途中マスクを記録し、非保護版でその専有セルの保持率が初めて崩れる手を特定して
「削除直前への復帰＋別の一手」も行う(段D)。

出力: output/idle_validation_20260928/04a_qualification_run.json(生データ。schema付きのまとめは
`build_04a_json.py`が`04a_robustness_loads.json`と合わせて`04a_path_qualification.json`へ作る)。
"""
from __future__ import annotations

import dataclasses
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import _common  # noqa: E402
import search_methods as sm  # noqa: E402
from duocad.topopt import acceptance, binary_search  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"
MODEL = REPO / "models" / "topopt_idle_path_small.json"
LOAD_N = 3000.0
ELEMENT_SIZE_MM = 2.0
MIN_WIDTH_MM = 6.0
TRANSPLANT_SRC = "mesh_2.5mm"
CLOSING_ITERATIONS = 2  # 00_COMPACT_MODELS.md 3-4節と同条件(隙間閉じ版)


def _load_native_mesh_mask(tag: str) -> tuple[np.ndarray, dict]:
    s = np.load(OUT / f"path_mask_{tag}.npz")
    return s["mask"], dict(origin=s["origin"], spacing=float(s["spacing"]))


def build_transplant(ctx: sm.SearchContext) -> np.ndarray:
    """`measure_path_local_optimum.cmd_transplant` と同じ射影+closing+keep+resolve_islands の手順
    (00_COMPACT_MODELS.md 3-4節)。`ctx` をそのまま使うので座標系は `build_context` の導出と一致する
    (00の値 57,656 mm3, SF 4.481 への一致で同一性を検算)。"""
    s = np.load(OUT / f"path_mask_{TRANSPLANT_SRC}.npz")
    idx = np.indices(ctx.grid.shape).reshape(3, -1).T
    w = np.asarray(ctx.grid.origin) + (idx + 0.5) * ctx.grid.spacing
    j = np.floor((w - s["origin"]) / float(s["spacing"])).astype(int)
    ok = np.all((j >= 0) & (j < np.array(s["mask"].shape)), axis=1)
    m = np.zeros(len(idx), bool)
    jj = j[ok]
    m[ok] = s["mask"][jj[:, 0], jj[:, 1], jj[:, 2]]
    mask = m.reshape(ctx.grid.shape) & ctx.space.mask
    if CLOSING_ITERATIONS:
        mask = ndimage.binary_closing(mask, structure=np.ones((3, 3, 3)),
                                      iterations=CLOSING_ITERATIONS) & ctx.space.mask
    keep = np.asarray(ctx.cond.keep_cells, bool) if getattr(ctx.cond, "keep_cells", None) is not None \
        else np.zeros_like(mask)
    mask |= keep & ctx.space.mask
    survivors, _diag = binary_search.resolve_islands(mask, ctx.cond)
    best = max(survivors, key=lambda c: c.sum()) if survivors else None
    if best is None:
        raise RuntimeError("transplant: no surviving component")
    return best


# ---------------------------------------------------------------------------
# 段B: 局所growth(軸方向1セルシフトの貪欲選択。search_methods._growth_geometry_okで検査)
# ---------------------------------------------------------------------------
#: 最小幅3セルでは、孤立点周りの表面追加(`_surface_growth_pool` や単一点の立方体膨張)は
#: `min_width` 検査で落ちる(実測: 追加分自身がどの m立方ブロックにも属さない)か、通っても非効率
#: (実測: 最悪応力点中心の立方体膨張は半径4〜10まで拡げてようやく安全率が動き、10ラウンドで
#: 83,768 mm3〈現行71,928 mm3超過〉)。**現在の材料全体を軸方向へ1セルだけシフトして重ねる
#: (=断面をその方向へ1セル太らせる)と、追加分は既存材料に全面で接するので大半のセルが最小幅を
#: 保ったまま通る**(実測: 6方向とも `_growth_geometry_ok` 合格、449〜572セル/方向)。
#: 6方向を毎ラウンド全部評価し、指標を最も改善する方向を貪欲に採用する(3ラウンドで
#: 69,440 mm3・SF4.838。現行は71,928 mm3)。
_AXIS_SHIFTS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


def _shift(mask: np.ndarray, d: tuple[int, int, int]) -> np.ndarray:
    dx, dy, dz = d
    out = np.zeros_like(mask)
    sx = slice(max(0, dx), mask.shape[0] + min(0, dx))
    sxs = slice(max(0, -dx), mask.shape[0] + min(0, -dx))
    sy = slice(max(0, dy), mask.shape[1] + min(0, dy))
    sys_ = slice(max(0, -dy), mask.shape[1] + min(0, -dy))
    sz = slice(max(0, dz), mask.shape[2] + min(0, dz))
    szs = slice(max(0, -dz), mask.shape[2] + min(0, -dz))
    out[sx, sy, sz] = mask[sxs, sys_, szs]
    return out


def _gate_score(s: "sm.CandidateScore", gate: str) -> float:
    """大きいほど良い、の単一スカラーへ揃える(貪欲選択の比較用。ゲートの合否そのものは別途`gate_pass`が見る)。"""
    if gate == "percentile":
        return s.safety_factor_product
    if gate == "m5b_raw":
        return -s.m5b_raw_exceedance_mpa_mm3
    if gate == "m5b_target":
        return -s.m5b_target_exceedance_mpa_mm3
    raise ValueError(gate)


def local_reinforcement(ctx: sm.SearchContext, start_mask: np.ndarray, allowed_material: np.ndarray, *,
                        gate: str, max_rounds: int, log: list) -> tuple[np.ndarray, "sm.CandidateScore"]:
    """`start_mask`(幾何は合格・安全率/M5b不足)から、6軸方向シフト(`_AXIS_SHIFTS`)のうち
    `_growth_geometry_ok` を通り `gate` の指標を最も改善する1つを毎ラウンド貪欲に採用する。
    `allowed_material` は `find_initial_feasible` が返す最小幅済み全材料で、この外には出ない。
    全方向が幾何で落ちるか改善しなければ打ち切る。
    """
    working = start_mask.copy()
    score = sm.score_mask(working, ctx)
    target = float(ctx.cond.target_safety_factor)

    def gate_pass(s: "sm.CandidateScore") -> bool:
        if gate == "percentile":
            return s.safety_factor_product >= target
        if gate == "m5b_raw":
            return s.m5b_raw_passed
        if gate == "m5b_target":
            return s.m5b_target_passed
        raise ValueError(gate)

    rounds = 0
    while not gate_pass(score) and rounds < max_rounds:
        # 芯の連結上限(`base_core_groups`)は毎ラウンド `working` から取り直す
        # (材料が増えるので、古いままだと妥当な追加まで拒否しうる)。
        base_core_groups = sm.core_group_count(working, ctx.min_width_cells)
        candidates = []
        for d in _AXIS_SHIFTS:
            candidate = (working | _shift(working, d)) & allowed_material
            added = int((candidate & ~working).sum())
            if added == 0:
                continue
            ok, problems = sm._growth_geometry_ok(candidate, base_core_groups, ctx.cond, ctx.min_width_cells)
            if not ok:
                log.append(dict(kind="grow_reject", round=rounds + 1, direction=list(d), added_cells=added,
                                problems=[p["check"] for p in problems]))
                continue
            cand_score = sm.score_mask(candidate, ctx)
            candidates.append((d, added, candidate, cand_score))
        if not candidates:
            log.append(dict(kind="grow_no_candidate", round=rounds + 1, note="全6方向が幾何検査で落ちた"))
            break
        d, added, candidate, cand_score = max(candidates, key=lambda c: _gate_score(c[3], gate))
        rounds += 1
        log.append(dict(kind="grow_accept", round=rounds, direction=list(d), added_cells=added,
                        material_mm3_before=float(working.sum() * ctx.grid.element_volume),
                        material_mm3_after=cand_score.material_mm3,
                        safety_factor_product=cand_score.safety_factor_product,
                        m5b_raw_exceedance_mpa_mm3=cand_score.m5b_raw_exceedance_mpa_mm3,
                        m5b_target_exceedance_mpa_mm3=cand_score.m5b_target_exceedance_mpa_mm3,
                        alternatives_considered=len(candidates)))
        working, score = candidate, cand_score
    return working, score


def finish_trim(ctx: sm.SearchContext, mask: np.ndarray) -> tuple[np.ndarray, "sm.CandidateScore", "sm.MethodRecord"]:
    """段B(貪欲成長)は `gate` を跨いだ瞬間に止まるので、越えた分を削る仕上げ(段B')。
    `stop_policy="legacy"` だと越えた直後に `within_target_tolerance` で即停止して削れない
    (実測: 3000Nでは越え幅が小さく無変更だった)ため、既定の `stop_policy="adaptive_finish"` を使う。
    `run_adaptive` のロールバック機構も有効なまま。"""
    finish_cfg = sm.AdaptiveConfig(gate="percentile", stop_policy="adaptive_finish", max_step_ratio=0.05,
                                   min_step_ratio=0.0, max_evaluations=500, seed=0)
    rec = sm.run_adaptive_from_mask(ctx, mask, finish_cfg)
    qualified_mask = rec.best_ever_mask if rec.best_ever_mask is not None else mask
    qualified_score = sm.score_mask(qualified_mask, ctx)
    return qualified_mask, qualified_score, rec


# ---------------------------------------------------------------------------
# 段C/D: 「該当経路の再導入と対照追加」「削除直前への復帰＋別の一手」
# ---------------------------------------------------------------------------
def _tracer_config(seed: int = 0, max_evaluations: int = 1000) -> sm.AdaptiveConfig:
    """`_reduce_volume` と同じ受理条件・停止条件になるよう、adaptive方式固有の機構
    (ロールバック・復帰・仕上げ局面)を実質無効化した設定(04aの対照実験用)。"""
    return sm.AdaptiveConfig(
        gate="percentile", stop_policy="legacy", legacy_target_tolerance=binary_search.DEFAULT_TARGET_TOLERANCE,
        step_exponent=binary_search.DEFAULT_STEP_EXPONENT, max_step_ratio=binary_search.DEFAULT_MAX_STEP_RATIO,
        min_step_ratio=0.0, max_evaluations=max_evaluations,
        stagnation_patience=10**9,  # revert_restartを起こさせない(製品にこの機構は無い)
        prediction_error_multiplier=10**9, sudden_jump_relative_drop=10**9,
        sudden_compliance_relative_increase=10**9, sudden_displacement_relative_increase=10**9,
        seed=seed,
    )


def traced_run(ctx: sm.SearchContext, *, extra_protected=None, seed: int = 0) -> tuple[sm.MethodRecord, list]:
    trace: list[dict] = []

    def on_accept(evaluations, working, score):
        trace.append(dict(evaluation=evaluations, material_mm3=score.material_mm3,
                          safety_factor_product=score.safety_factor_product, mask=working.copy()))

    rec = sm.run_adaptive(ctx, _tracer_config(seed=seed), extra_protected=extra_protected, on_accept=on_accept)
    return rec, trace


def diff_step(trace: list, corridor: np.ndarray) -> dict:
    """`corridor`(斜材専有セル)の保持率が手ごとにどう推移し、どの1手で初めて崩れたかを返す。"""
    total = int(corridor.sum())
    rows = []
    prev_mask = None
    first_drop = None
    for row in trace:
        kept = int((row["mask"] & corridor).sum())
        frac = kept / total if total else 1.0
        rows.append(dict(evaluation=row["evaluation"], corridor_kept=kept, corridor_fraction=frac,
                         material_mm3=row["material_mm3"], safety_factor_product=row["safety_factor_product"]))
        if first_drop is None and frac < 0.5 and prev_mask is not None:
            removed = prev_mask & ~row["mask"] & corridor
            first_drop = dict(evaluation=row["evaluation"], removed_corridor_cells=int(removed.sum()),
                              removed_cells_total=int((prev_mask & ~row["mask"]).sum()))
        prev_mask = row["mask"]
    return dict(total_corridor_cells=total, trajectory=rows, first_drop=first_drop)


def main() -> int:
    t_start = time.perf_counter()
    mem = _common.SingleFemRun()
    mem.__enter__()
    ctx = sm.build_context(MODEL, load_n=LOAD_N, element_size_mm=ELEMENT_SIZE_MM, min_width_mm=MIN_WIDTH_MM)
    target = float(ctx.cond.target_safety_factor)
    payload: dict = dict(model=str(MODEL.relative_to(REPO)), load_n=LOAD_N, element_size_mm=ELEMENT_SIZE_MM,
                         min_width_mm=MIN_WIDTH_MM, target_safety_factor=target,
                         design_elements=int(ctx.space.mask.sum()), min_width_cells=ctx.min_width_cells,
                         code_version=_common.git_state())

    # --- S1: 現行(step20)ベースライン(既存 path_mask_mesh_2.0mm.npz を再スコア) ---
    native_mask, native_meta = _load_native_mesh_mask("mesh_2.0mm")
    native_score = sm.score_mask(native_mask, ctx)
    payload["baseline_native_step20"] = dict(source="path_mask_mesh_2.0mm.npz(00_COMPACT_MODELS.md 3-2節と同一マスク)",
                                             **native_score.to_json())
    print("S1 native", native_score.material_mm3, native_score.safety_factor_product, flush=True)

    # --- S2: 2.5mm斜材解の移植(00 3-4節と同条件) ---
    transplant_mask = build_transplant(ctx)
    transplant_score = sm.score_mask(transplant_mask, ctx)
    payload["seed_transplant"] = dict(source=f"{TRANSPLANT_SRC}→{ELEMENT_SIZE_MM}mm closing={CLOSING_ITERATIONS}",
                                      **transplant_score.to_json())
    print("S2 transplant", transplant_score.material_mm3, transplant_score.safety_factor_product, flush=True)

    # --- S3: 局所growth(percentileゲート=現行探索と同じ受理条件まで引き上げる) ---
    initial_all = binary_search.find_initial_feasible(
        ctx.space.mask, ctx.cond, max_evaluations=1, min_width_cells=ctx.min_width_cells).best_feasible
    grow_log: list = []
    grown_mask, grown_score = local_reinforcement(ctx, transplant_mask, initial_all.mask, gate="percentile",
                                                  max_rounds=200, log=grow_log)
    payload["growth_log"] = grow_log
    payload["after_growth"] = grown_score.to_json()
    print("S3 grown", grown_score.material_mm3, grown_score.safety_factor_product,
         "batches", len(grow_log), flush=True)

    # --- S4: 仕上げ削減(run_adaptive_from_mask, percentileゲート, 小さい歩幅) ---
    qualified_mask, qualified_score, finish_rec = finish_trim(ctx, grown_mask)
    payload["finish_reduction"] = dict(stop_reason=finish_rec.stop_reason, evaluations=finish_rec.evaluations,
                                       fem_solves=finish_rec.fem_solves,
                                       step_count=len(finish_rec.steps))
    payload["qualified_form"] = qualified_score.to_json()
    print("S4 qualified", qualified_score.material_mm3, qualified_score.safety_factor_product, flush=True)

    # --- S5: 全規則の明示的な最終検査(acceptance.inspectそのもの。最小幅は別途) ---
    rep = acceptance.inspect(qualified_mask, ctx.space.mask, ctx.grid, **ctx.cond.inspect_kwargs(bbox_cache=None))
    exempt = binary_search._min_width_exempt(ctx.cond, qualified_mask.shape, ctx.min_width_cells)
    min_width_ok = bool(binary_search._mask_satisfies_min_width(qualified_mask, ctx.min_width_cells, exempt))
    payload["qualified_form_full_rules"] = dict(
        checks={k: v.get("status") for k, v in rep["checks"].items()}, min_width_ok=min_width_ok,
        lighter_than_native=bool(qualified_score.material_mm3 < native_score.material_mm3),
        native_material_mm3=native_score.material_mm3, qualified_material_mm3=qualified_score.material_mm3,
        percentile_gate_passed=bool(qualified_score.safety_factor_product >= target),
        m5b_raw_passed=qualified_score.m5b_raw_passed, m5b_target_passed=qualified_score.m5b_target_passed,
    )
    (OUT / "04a_masks").mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT / "04a_masks" / "qualified_path_2.0mm.npz", mask=qualified_mask,
                        origin=np.asarray(ctx.grid.origin, float), spacing=float(ctx.grid.spacing))
    np.savez_compressed(OUT / "04a_masks" / "native_step20_2.0mm.npz", mask=native_mask,
                        origin=np.asarray(ctx.grid.origin, float), spacing=float(ctx.grid.spacing))

    # --- S6: 因果検査(段C: protect/control。段D: 削除直前+別の一手) ---
    corridor = qualified_mask & ~native_mask  # 軽い優越形に固有で現行"腕"形には無いセル(=斜材専有)
    print("corridor cells", int(corridor.sum()), flush=True)

    control_rec, control_trace = traced_run(ctx, extra_protected=None, seed=0)
    control_diff = diff_step(control_trace, corridor)
    print("S6 control(unprotected) final", control_rec.best_ever_score["material_mm3"],
         control_rec.stop_reason, flush=True)

    protected_rec, protected_trace = traced_run(ctx, extra_protected=corridor, seed=0)
    protected_diff = diff_step(protected_trace, corridor)
    print("S6 protected final", protected_rec.best_ever_score["material_mm3"], protected_rec.stop_reason, flush=True)

    payload["causal_check"] = dict(
        corridor_cells=int(corridor.sum()), corridor_mm3=float(corridor.sum() * ctx.grid.element_volume),
        control_unprotected=dict(final_material_mm3=control_rec.best_ever_score["material_mm3"],
                                 final_safety_factor=control_rec.best_ever_score["safety_factor_product"],
                                 stop_reason=control_rec.stop_reason, evaluations=control_rec.evaluations,
                                 corridor_final_fraction=control_diff["trajectory"][-1]["corridor_fraction"]
                                 if control_diff["trajectory"] else None,
                                 first_drop=control_diff["first_drop"],
                                 trajectory=control_diff["trajectory"]),
        protected_corridor=dict(final_material_mm3=protected_rec.best_ever_score["material_mm3"],
                                final_safety_factor=protected_rec.best_ever_score["safety_factor_product"],
                                stop_reason=protected_rec.stop_reason, evaluations=protected_rec.evaluations,
                                corridor_final_fraction=protected_diff["trajectory"][-1]["corridor_fraction"]
                                if protected_diff["trajectory"] else None,
                                trajectory=protected_diff["trajectory"]),
    )

    # --- S6b: 削除直前への復帰＋別の一手(段D) ---
    single_move = None
    fd = control_diff["first_drop"]
    if fd is not None:
        idx_before = next(i for i, r in enumerate(control_trace) if r["evaluation"] == fd["evaluation"]) - 1
        mask_before = control_trace[idx_before]["mask"] if idx_before >= 0 else None
        mask_after = next(r["mask"] for r in control_trace if r["evaluation"] == fd["evaluation"])
        if mask_before is not None:
            removed_this_move = mask_before & ~mask_after
            single_protect = removed_this_move & corridor
            resume_ctx_score = sm.score_mask(mask_before, ctx)
            alt_rec, alt_trace = traced_run_from(ctx, mask_before, extra_protected_first_move=single_protect,
                                                 corridor=corridor, seed=1)
            single_move = dict(
                reverted_to_evaluation=control_trace[idx_before]["evaluation"] if idx_before >= 0 else "initial",
                reverted_material_mm3=resume_ctx_score.material_mm3,
                blocked_cells=int(single_protect.sum()),
                alt_final_material_mm3=alt_rec.best_ever_score["material_mm3"] if alt_rec.best_ever_score else None,
                alt_final_safety_factor=alt_rec.best_ever_score["safety_factor_product"]
                if alt_rec.best_ever_score else None,
                alt_stop_reason=alt_rec.stop_reason,
                alt_corridor_final_fraction=diff_step(alt_trace, corridor)["trajectory"][-1]["corridor_fraction"]
                if alt_trace else None,
            )
    payload["causal_check"]["single_alternate_move"] = single_move

    mem.__exit__(None, None, None)
    payload["wall_seconds_total"] = time.perf_counter() - t_start
    payload["peak_memory"] = dict(traced_python_heap_mb=mem.traced_peak_mb, process_rss_mb=mem.peak,
                                  note="全工程(S1〜S6b: 移植・成長・仕上げ削減・対照2本・単一代替手)を通した"
                                  "1プロセスの計測。製品全行程(探索→表面生成→finalize→STEP保存)の速度・"
                                  "メモリは00_COMPACT_MODELS.md 3-2節に既存記録がある(本タスクは探索段のみ)。")
    out_path = OUT / "04a_qualification_run.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


def traced_run_from(ctx: sm.SearchContext, start_mask: np.ndarray, *, extra_protected_first_move: np.ndarray,
                    corridor: np.ndarray, seed: int) -> tuple[sm.MethodRecord, list]:
    """段D: `start_mask`(削除直前)から、直後の1手だけ `extra_protected_first_move`(実際に削られた
    斜材専有セル)を保護し、以降は無保護で `_tracer_config` を再開する。`extra_protected` はループ全体に
    効くので、1手だけに限定するには2段階に分けて呼ぶ(1手目=保護あり `max_evaluations=1`、以降=保護なし)。
    **`max_evaluations=1` が保証するのは「FEMを解く候補が1個までしか試されない」ことであり
    「1手が受理される」ことではない**(却下されれば `working` は `start_mask` のまま渡る)。
    実測(3000N条件)ではその1個が受理され、無保護版の元の1手(322セル除去)とほぼ同規模の318セル
    (保護した33セルを避けた代替)が除去された。"""
    trace: list[dict] = []

    def on_accept(evaluations, working, score):
        trace.append(dict(evaluation=evaluations, material_mm3=score.material_mm3,
                          safety_factor_product=score.safety_factor_product, mask=working.copy()))

    cfg_first = dataclasses.replace(_tracer_config(seed=seed), max_evaluations=1)
    rec1 = sm.run_adaptive_from_mask(ctx, start_mask, cfg_first, extra_protected=extra_protected_first_move,
                                     on_accept=on_accept)
    resume_mask = rec1.best_ever_mask if rec1.best_ever_mask is not None else start_mask
    rec2 = sm.run_adaptive_from_mask(ctx, resume_mask, _tracer_config(seed=seed), extra_protected=None,
                                     on_accept=on_accept)
    return rec2, trace


if __name__ == "__main__":
    raise SystemExit(main())

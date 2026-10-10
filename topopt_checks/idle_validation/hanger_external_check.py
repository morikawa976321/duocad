"""04の「小型題材を主にする変更」節が要求する最低限のhanger外部確認。

原因2の低荷重・太幅条件(H1=`F0200_h2.0_w5`)と原因1の高荷重条件(H2=`F5000_h2.4_w2`)で、
「現行」と「小型検証で選んだ新方式」(`run_adaptive`。小型モデルで安全率余裕・部位別応力・
実削除体積の一次予測・stagnation復帰の全機構が発動することを `search_methods_mechanism_checks.py`
で確認済み)を、同じ評価器(`score_mask`)・同じ条件で比較する。

**H1とH2で「現行」の作り方が違う**(実測で発見・修正した理由)。H1は製品の既存保存形
`hanger_base`(`01_stage1_shapes.json`由来、native `F0200_h2.0_w5`=2.0mm)をH1条件自身
(同じ2.0mm)へ投影するだけなので、そのまま使える。H2(2.4mm)へ同じ手順で投影しようとすると、
**2.0mm→2.4mmへ粗くする投影で連結成分が条件面を失い(`resolve_islands`が0件を返す)、
使えなかった**(実測: `removed_islands`に`touches_condition=True`の17,811セルの成分が丸ごと
落ちた)。H2には`hanger_base`と互換の保存形が無い(`HANGER_DEFAULTS`は全て200N・条件依存)ので、
**H2の「現行」は製品コードそのもの(`run_fixed_step`、20%固定歩幅)を新規に1回走らせた結果**とする
——「現行」を単なる別解像度への投影で近似するより、製品の探索アルゴリズムを実条件でそのまま
走らせるほうが「現行」の意味に忠実。

**hanger本体の設計空間は大きい**(実測: H1条件で64万要素)。H1は`hanger_base`から
`search_methods.run_adaptive_from_mask`で削減を続ける(段階3=設計空間全体のFEMを経由しない)。
H2は「現行」自体が段階3から新規探索するため、新方式(`run_adaptive`)も同じ入口(段階3から)で
揃える。**時間で打ち切らない**——`checkpoint_path`を渡して逐次保存し、打ち切られても`--resume`で
続きから再開できる。

再現: `.\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\hanger_external_check.py --condition H1 [--resume] [--max-evaluations N]`
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from duocad.topopt import binary_search  # noqa: E402

import _common  # noqa: E402
import refine_shape as rs  # noqa: E402
import search_methods as sm  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"
LOCK = OUT / "_locks" / "fem_single_run.lock"
RESULTS_PATH = OUT / "04_hanger_external_check.json"

CONDITIONS = {
    # 名前: (hanger_condition文字列, load_n, element_size_mm, min_width_mm, 由来)
    # min_width_mm=None は「製品既定(2×実spacing)」。要求mmを固定値で書くと、格子スナップ後の実spacingが
    # わずかにずれたとき `_resolve_min_width` の下限(2×実spacing)未満になりうる
    # (実測: 2.4mm指定がスナップ後2.406mmになり、4.8mm指定が下限4.813mmを下回ってエラー)。
    "H1": ("F0200_h2.0_w5", 200.0, 2.0, 10.0, "原因2(低荷重・太幅)代表。03bのhanger共通条件採点と同じ条件"),
    "H2": ("F5000_h2.4_w2", 5000.0, 2.4, None, "原因1(高荷重)代表。製品既定幅(w2=2セル相当)"),
}


def _project_current_shape(ctx: sm.SearchContext, *, native_condition: str) -> np.ndarray:
    """`hanger_base`(製品の既存保存形、`native_condition` で保存済み)を `ctx` の格子へ投影する。
    同一解像度(H1)でのみ使う——異なる解像度への投影は連結成分が条件面を失いうる(モジュール
    docstring参照)。`metric_eval.solve_shape` と同じ手順(`_project_mask`→keep合成→最大連結成分)。
    """
    shape_cfg = rs._resolve_shape("hanger_base", native_condition)
    src_mask = np.asarray(shape_cfg["mask"], dtype=bool)
    src_origin = np.asarray(shape_cfg["origin"], dtype=float)
    src_spacing = float(shape_cfg["spacing"])
    mask = rs._project_mask(src_mask, src_origin, src_spacing, ctx.space) & ctx.space.mask
    keep = shape_cfg.get("keep")
    if keep is not None:
        keep_f = rs._project_mask(np.asarray(keep, dtype=bool), src_origin, src_spacing, ctx.space) & ctx.space.mask
        mask = mask | keep_f
    survivors, diag = binary_search.resolve_islands(mask, ctx.cond)
    if not survivors:
        raise RuntimeError(f"投影後に連結成分が残らなかった: {diag}")
    return max(survivors, key=lambda c: c.sum())


def run_condition_h1(name: str, *, max_evaluations: int, resume: bool) -> dict:
    hanger_condition, load_n, element_size_mm, min_width_mm, cause = CONDITIONS[name]
    t0 = time.time()
    ctx = sm.build_context(rs.HANGER_MODEL, load_n=load_n, element_size_mm=element_size_mm, min_width_mm=min_width_mm)
    context_seconds = time.time() - t0

    current_mask = _project_current_shape(ctx, native_condition="F0200_h2.0_w5")
    current_score = sm.score_mask(current_mask, ctx)

    ckpt = OUT / f"04_hanger_{name}_adaptive.checkpoint.json"
    resume_state = sm.load_checkpoint(ckpt) if (resume and ckpt.exists()) else None
    cfg = sm.AdaptiveConfig(seed=100, max_evaluations=max_evaluations, gate="m5b_raw",
                            stagnation_patience=8, finish_max_evaluations=20)
    if resume_state is not None:
        rec = sm._run_adaptive_impl(ctx, sm._SeedResult(resume_state["working_mask"]), cfg,
                                    checkpoint_path=ckpt, is_cancelled=None, resume_from=resume_state)
    else:
        rec = sm.run_adaptive_from_mask(ctx, current_mask, cfg, checkpoint_path=ckpt)

    return dict(
        condition=name, hanger_condition=hanger_condition, load_n=load_n, element_size_mm=element_size_mm,
        min_width_mm_requested=min_width_mm, cause_represented=cause, design_elements=int(ctx.space.mask.sum()),
        min_width_cells=ctx.min_width_cells, min_width_mm_resolved=ctx.min_width_cells * ctx.grid.spacing,
        context_build_seconds=context_seconds,
        current=dict(source="hanger_base(01_stage1_shapes.json、native F0200_h2.0_w5から同解像度投影)",
                    score=current_score.to_json() if current_score else None),
        adaptive=dict(status=rec.status, stop_reason=rec.stop_reason, evaluations=rec.evaluations,
                     fem_solves=rec.fem_solves, wall_seconds=rec.wall_seconds,
                     mechanism_events=rec.mechanism_events, best_ever_score=rec.best_ever_score,
                     checkpoint_path=str(ckpt.relative_to(REPO))),
        total_wall_seconds=time.time() - t0,
        note="「現行」は製品の既存保存形(hanger_base)の再スコア(新規探索ではない)。"
            "新方式は同じ形からの削減継続。性能優位はここでは主張しない(実測数値のみ)。",
    )


def run_condition_h2(name: str, *, max_evaluations: int, resume: bool) -> dict:
    """H2は`hanger_base`の互換保存形が無い(投影が連結成分を失う。モジュールdocstring参照)ので、
    「現行」を製品コード(`run_fixed_step`、20%固定歩幅)の新規実行そのものとする。新方式
    (`run_adaptive`)も同じ入口(段階3=`find_initial_feasible`から)で揃える。
    """
    hanger_condition, load_n, element_size_mm, min_width_mm, cause = CONDITIONS[name]
    t0 = time.time()
    ctx = sm.build_context(rs.HANGER_MODEL, load_n=load_n, element_size_mm=element_size_mm, min_width_mm=min_width_mm)
    context_seconds = time.time() - t0

    current_rec = sm.run_fixed_step(ctx, max_step_ratio=0.2, max_evaluations=max_evaluations)

    ckpt = OUT / f"04_hanger_{name}_adaptive.checkpoint.json"
    resume_state = sm.load_checkpoint(ckpt) if (resume and ckpt.exists()) else None
    cfg = sm.AdaptiveConfig(seed=100, max_evaluations=max_evaluations, gate="m5b_raw",
                            stagnation_patience=8, finish_max_evaluations=20)
    if resume_state is not None:
        rec = sm._run_adaptive_impl(ctx, sm._SeedResult(resume_state["working_mask"]), cfg,
                                    checkpoint_path=ckpt, is_cancelled=None, resume_from=resume_state)
    else:
        rec = sm.run_adaptive(ctx, cfg, checkpoint_path=ckpt)

    return dict(
        condition=name, hanger_condition=hanger_condition, load_n=load_n, element_size_mm=element_size_mm,
        min_width_mm_requested=min_width_mm, cause_represented=cause, design_elements=int(ctx.space.mask.sum()),
        min_width_cells=ctx.min_width_cells, min_width_mm_resolved=ctx.min_width_cells * ctx.grid.spacing,
        context_build_seconds=context_seconds,
        current=dict(source="run_fixed_step(製品コード20%固定歩幅、H2条件で新規実行——"
                           "hanger_baseはこの条件に投影できないため)",
                    stop_reason=current_rec.stop_reason, evaluations=current_rec.evaluations,
                    score=current_rec.best_ever_score),
        adaptive=dict(status=rec.status, stop_reason=rec.stop_reason, evaluations=rec.evaluations,
                     fem_solves=rec.fem_solves, wall_seconds=rec.wall_seconds,
                     mechanism_events=rec.mechanism_events, best_ever_score=rec.best_ever_score,
                     checkpoint_path=str(ckpt.relative_to(REPO))),
        total_wall_seconds=time.time() - t0,
        note="「現行」はH2条件での製品コード(20%固定歩幅)の新規実行(hanger_baseの投影ではない)。"
            "新方式も同じ入口(段階3)から実行。性能優位はここでは主張しない(実測数値のみ)。",
    )


RUNNERS = {"H1": run_condition_h1, "H2": run_condition_h2}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", choices=["H1", "H2", "both"], default="both")
    parser.add_argument("--max-evaluations", type=int, default=60)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    names = ["H1", "H2"] if args.condition == "both" else [args.condition]

    with _common.ExecutionLock(LOCK, tag="hanger_external_check"):
        # 既存結果を読み込んでから更新する(条件ごとに別プロセスで走らせても先の条件の結果を上書きで失わない)。
        results = {}
        if RESULTS_PATH.exists():
            results = json.loads(RESULTS_PATH.read_text(encoding="utf-8")).get("results", {})
        for name in names:
            print(f"=== {name} 開始 ===", flush=True)
            results[name] = RUNNERS[name](name, max_evaluations=args.max_evaluations, resume=args.resume)
            print(f"=== {name} 完了: {results[name]['adaptive']['stop_reason']} "
                 f"evaluations={results[name]['adaptive']['evaluations']} ===", flush=True)
            payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state(), results=results)
            _common.write_atomic(RESULTS_PATH, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
            print(f"wrote {RESULTS_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

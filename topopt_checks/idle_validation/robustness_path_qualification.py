"""タスク4a: 04A_PATH_QUALIFICATIONの頑健性確認(近傍荷重)。

`qualify_path_small.py`は3000N(00_COMPACT_MODELSの原因2代表条件)だけで「移植+軸方向成長+仕上げ削減」が
現行(step20)より軽く目標安全率を満たす形を作れることを示した。単一条件の偶然でないことを、荷重を
±10%動かした2条件(2700N・3300N)で同じ手順が同じ方向の結果(軽い・合格)を出すか確認する
(「近傍荷重/原点条件でも再現性を確認し、偶然の一例と頑健な範囲を区別する」)。原点シフトは
本モデルの拘束・荷重パッド位置がSTEP形状の直接編集を要するため本タスクでは実施しない
(10_SYNTHESIS 付録C(旧 04A_PATH_QUALIFICATION)「限界」節に明記)。

出力: output/idle_validation_20260928/04a_robustness_loads.json
"""
from __future__ import annotations

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
import qualify_path_small as q  # noqa: E402
from duocad.topopt import acceptance, binary_search  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"
LOADS = (2700.0, 3300.0)


def _native_mesh(ctx: sm.SearchContext, *, max_step_ratio: float) -> tuple[np.ndarray, dict]:
    """`binary_search.find_best_feasible`を直接呼ぶ(`measure_path_local_optimum._run_one`と同じ経路。
    探索のみ、finalizeは回さない)。"""
    t0 = time.perf_counter()
    r = binary_search.find_best_feasible(ctx.space.mask, ctx.cond, max_evaluations=1000,
                                         max_step_ratio=max_step_ratio, min_width_cells=ctx.min_width_cells)
    seconds = time.perf_counter() - t0
    if r.status != "feasible" or r.best_feasible is None:
        raise RuntimeError(f"native search infeasible: status={r.status} termination={r.termination}")
    return r.best_feasible.mask, dict(seconds=seconds, evaluations=r.evaluations, termination=r.termination)


def _transplant(ctx20: sm.SearchContext, mesh25_mask: np.ndarray, meta25: dict) -> np.ndarray:
    idx = np.indices(ctx20.grid.shape).reshape(3, -1).T
    w = np.asarray(ctx20.grid.origin) + (idx + 0.5) * ctx20.grid.spacing
    j = np.floor((w - meta25["origin"]) / meta25["spacing"]).astype(int)
    ok = np.all((j >= 0) & (j < np.array(mesh25_mask.shape)), axis=1)
    m = np.zeros(len(idx), bool)
    jj = j[ok]
    m[ok] = mesh25_mask[jj[:, 0], jj[:, 1], jj[:, 2]]
    mask = m.reshape(ctx20.grid.shape) & ctx20.space.mask
    mask = ndimage.binary_closing(mask, structure=np.ones((3, 3, 3)), iterations=q.CLOSING_ITERATIONS) & ctx20.space.mask
    keep = np.asarray(ctx20.cond.keep_cells, bool) if getattr(ctx20.cond, "keep_cells", None) is not None \
        else np.zeros_like(mask)
    mask |= keep & ctx20.space.mask
    survivors, _diag = binary_search.resolve_islands(mask, ctx20.cond)
    best = max(survivors, key=lambda c: c.sum()) if survivors else None
    if best is None:
        raise RuntimeError("transplant: no surviving component")
    return best


def run_one_load(load_n: float) -> dict:
    ctx25 = sm.build_context(q.MODEL, load_n=load_n, element_size_mm=2.5, min_width_mm=q.MIN_WIDTH_MM)
    mesh25_mask, meta25 = _native_mesh(ctx25, max_step_ratio=binary_search.DEFAULT_MAX_STEP_RATIO)
    mesh25_full_meta = dict(origin=np.asarray(ctx25.grid.origin, float), spacing=float(ctx25.grid.spacing))

    ctx20 = sm.build_context(q.MODEL, load_n=load_n, element_size_mm=2.0, min_width_mm=q.MIN_WIDTH_MM)
    native20_mask, native20_meta = _native_mesh(ctx20, max_step_ratio=binary_search.DEFAULT_MAX_STEP_RATIO)
    native20_score = sm.score_mask(native20_mask, ctx20)

    transplant_mask = _transplant(ctx20, mesh25_mask, mesh25_full_meta)
    transplant_score = sm.score_mask(transplant_mask, ctx20)

    initial_all = binary_search.find_initial_feasible(
        ctx20.space.mask, ctx20.cond, max_evaluations=1, min_width_cells=ctx20.min_width_cells).best_feasible
    grow_log: list = []
    grown_mask, grown_score = q.local_reinforcement(ctx20, transplant_mask, initial_all.mask, gate="percentile",
                                                     max_rounds=200, log=grow_log)
    qualified_mask, qualified_score, finish_rec = q.finish_trim(ctx20, grown_mask)
    rep = acceptance.inspect(qualified_mask, ctx20.space.mask, ctx20.grid, **ctx20.cond.inspect_kwargs(bbox_cache=None))
    exempt = binary_search._min_width_exempt(ctx20.cond, qualified_mask.shape, ctx20.min_width_cells)
    min_width_ok = bool(binary_search._mask_satisfies_min_width(qualified_mask, ctx20.min_width_cells, exempt))
    (OUT / "04a_masks").mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT / "04a_masks" / f"qualified_path_2.0mm_load{int(load_n)}.npz", mask=qualified_mask,
                        origin=np.asarray(ctx20.grid.origin, float), spacing=float(ctx20.grid.spacing))

    target = float(ctx20.cond.target_safety_factor)
    return dict(
        load_n=load_n, target_safety_factor=target,
        design_elements_2_5mm=int(ctx25.space.mask.sum()), design_elements_2_0mm=int(ctx20.space.mask.sum()),
        native_2_5mm=dict(material_mm3=float(mesh25_mask.sum() * ctx25.grid.element_volume), **meta25),
        native_2_0mm=dict(material_mm3=native20_score.material_mm3,
                          safety_factor_product=native20_score.safety_factor_product, **native20_meta),
        transplant=dict(material_mm3=transplant_score.material_mm3,
                        safety_factor_product=transplant_score.safety_factor_product,
                        percentile_gate_passed=bool(transplant_score.safety_factor_product >= target)),
        after_growth=dict(material_mm3=grown_score.material_mm3,
                         safety_factor_product=grown_score.safety_factor_product, growth_rounds=len(grow_log)),
        qualified=dict(material_mm3=qualified_score.material_mm3,
                      safety_factor_product=qualified_score.safety_factor_product,
                      percentile_gate_passed=bool(qualified_score.safety_factor_product >= target),
                      finish_stop_reason=finish_rec.stop_reason, finish_evaluations=finish_rec.evaluations),
        lighter_than_native=bool(qualified_score.material_mm3 < native20_score.material_mm3),
        lighter_ratio=qualified_score.material_mm3 / native20_score.material_mm3,
        full_rules=dict(checks={k: v.get("status") for k, v in rep["checks"].items()}, min_width_ok=min_width_ok),
    )


def main() -> int:
    payload = dict(model=str(q.MODEL.relative_to(REPO)), element_size_mm_pair=[2.5, 2.0],
                  min_width_mm=q.MIN_WIDTH_MM, closing_iterations=q.CLOSING_ITERATIONS,
                  code_version=_common.git_state(), reference_load_n=3000.0,
                  reference_result="output/idle_validation_20260928/04a_path_qualification.json",
                  cases={})
    for load_n in LOADS:
        print(f"=== load {load_n} N ===", flush=True)
        with _common.SingleFemRun() as mem:
            result = run_one_load(load_n)
        result["wall_seconds"] = mem.elapsed_seconds
        result["peak_memory"] = dict(traced_python_heap_mb=mem.traced_peak_mb, process_rss_mb=mem.peak)
        payload["cases"][str(load_n)] = result
        print(json.dumps(result, ensure_ascii=False, default=float), flush=True)
    out_path = OUT / "04a_robustness_loads.json"
    _common.write_atomic(out_path, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

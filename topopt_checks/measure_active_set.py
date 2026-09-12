"""動的な active set(`simp.optimize(dynamic_active=...)`)の実測。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_active_set.py --pick 0.60 --max-iter 3 --tag base060
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_active_set.py --pick 0.60 --max-iter 3 --dynamic --tag dyn060
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_active_set.py --pick 0.08 --max-iter 40 --tag off008
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_active_set.py --pick 0.08 --max-iter 40 --dynamic --tag on008
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_active_set.py --merge

題材は `hanger_bar_attachment.json` 2.4mm・材料 70,634.5mm^3(docs/TOPOPT.md「下端の崖」と
同じ)。塊は `PICK_RATIO`(`--pick`)で振る。**1ケース1ファイル**
(`output/active_set_<tag>.json`)に書き、`--merge` が `output/active_set.json` へまとめる。

**参照(`Reference`)は `measure_simp_convergence.py` が作った pickle を使い回す。** 無ければ
先にそちらを1回走らせること —— 参照 run は本番と同じ格子で回るので作り直すと数十分かかる。

結果の読み方は docs/TOPOPT_ACTIVE_SET.md。
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
import traceback
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401
import numpy as np

from duocad.topopt import OUTPUT_DIR
from duocad.topopt import design_space as ds
from duocad.topopt import simp
from measure_reaction_constraint import keep_survival

MODEL_NAME = "hanger_bar_attachment.json"
SPACING_MM = 2.4
MATERIAL_MM3 = 70634.5
REF_PICKLE = OUTPUT_DIR / "simp_convergence_hanger_2.4mm_ref.pkl"
MERGED = OUTPUT_DIR / "active_set.json"

# ρ の分布を積む区間の境目。**閾値 1e-3(emin=1e-9 / penal=3)を境目に含めてある** ——
# 含めないと「何%が閾値を下回るか」を後から読めない。
EDGES = [0.0, 1e-12, 1e-9, 1e-6, 1e-4, 1e-3, 1e-2, 0.05, 0.1, 0.3, 0.5, 0.9, 1.001]


def _sha256(arr: np.ndarray) -> str:
    import hashlib

    return hashlib.sha256(np.ascontiguousarray(arr, dtype=np.float64).tobytes()).hexdigest()


def run_case(*, pick: float, max_iter: int, dynamic: bool, tag: str, threads: int | None) -> dict:
    payload: dict = {
        "tag": tag,
        "model": MODEL_NAME,
        "spacing_mm": SPACING_MM,
        "pick_ratio": pick,
        "max_iter": max_iter,
        "dynamic_active": bool(dynamic),
        "material_mm3": MATERIAL_MM3,
        "threads": threads,
    }
    started = time.perf_counter()
    with open(REF_PICKLE, "rb") as handle:
        conditions, reference, forces = pickle.load(handle)
    space = ds.derive_from_reference(
        conditions,
        material_mm3=MATERIAL_MM3,
        reference=reference,
        pick_ratio=pick,
        threads=1,
        spacing=SPACING_MM,
    )
    payload["design_elements"] = int(space.element_count)
    payload["grid_shape"] = list(space.grid.shape)
    print(f"塊 {space.element_count} 要素 / 箱 {space.grid.shape}", flush=True)

    # **反復ごとの ρ 分布は `prepare` を包んで取る。** `callback` は更新のあとに呼ばれるので、
    # 「その反復で実際に組み立てた密度場」はそこからは見えない。
    original = simp.Model.prepare
    prepares: list[dict] = []
    design_mask = np.asarray(space.mask).ravel()

    def spy(self, rho, penal, **kwargs):
        flat = np.asarray(rho).ravel()
        threshold = float(self.emin) ** (1.0 / float(penal))
        design = flat[design_mask]
        hist = [
            int(((design >= lo) & (design < hi)).sum())
            for lo, hi in zip(EDGES[:-1], EDGES[1:])
        ]
        t0 = time.perf_counter()
        system = original(self, rho, penal, **kwargs)
        prepares.append(
            {
                "call": len(prepares) + 1,
                "penal": float(penal),
                "threshold": threshold,
                "design_elements": int(design.size),
                "below_threshold": int((design < threshold).sum()),
                "assembled": int(self.nele if self.active is None else self.active.size),
                "fixed": int(self.fixed.size),
                "free": int(self.free.size),
                "rho_min": float(design.min()),
                "rho_mean": float(design.mean()),
                "hist": hist,
                "prepare_seconds": time.perf_counter() - t0,
            }
        )
        print(json.dumps(prepares[-1], ensure_ascii=False), flush=True)
        return system

    simp.Model.prepare = spy  # type: ignore[method-assign]

    def progress(iteration, _rho):
        print(
            f"  反復 {iteration.it}: C={iteration.compliance:.4f} "
            f"change={iteration.change:.4f} {iteration.seconds:.1f}s",
            flush=True,
        )

    try:
        with simp.collect_solve_stats() as stats:
            out = ds.run_topopt(
                space,
                material_mm3=MATERIAL_MM3,
                max_iter=max_iter,
                threads=threads,
                forces=forces,
                stl_path=str(OUTPUT_DIR / f"active_set_{tag}.stl"),
                progress=progress,
                **({"dynamic_active": True} if dynamic else {}),
            )
        payload["solve_stats"] = dict(stats)
        result = out["result"]
        payload.update(
            {
                "stopped_by": result.stopped_by,
                "iterations": len(result.history),
                "compliance_final": float(result.history[-1].compliance),
                "rho_sha256": _sha256(result.rho),
                "achieved_material_mm3": float(out.get("achieved_material_mm3", float("nan"))),
                "reactions": out.get("reactions"),
                "keep": keep_survival(space, out["surface"]),
                "history": [
                    {
                        "it": int(item.it),
                        "compliance": float(item.compliance),
                        "change": float(item.change),
                        "volfrac": float(item.volfrac),
                        "seconds": float(item.seconds),
                    }
                    for item in result.history
                ],
                "error": None,
            }
        )
    except Exception as exc:  # noqa: BLE001
        payload.setdefault("solve_stats", None)
        payload["error"] = f"{type(exc).__name__}: {exc}"
        payload["traceback"] = traceback.format_exc()
        print(payload["error"], flush=True)
    finally:
        simp.Model.prepare = original  # type: ignore[method-assign]

    payload["prepares"] = prepares
    payload["seconds_total"] = time.perf_counter() - started
    out_path = OUTPUT_DIR / f"active_set_{tag}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"書き出し: {out_path}  {payload['seconds_total']:.1f}秒", flush=True)
    return payload


def merge() -> dict:
    cases = []
    for path in sorted(OUTPUT_DIR.glob("active_set_*.json")):
        cases.append(json.loads(path.read_text(encoding="utf-8")))
    payload = {
        "model": MODEL_NAME,
        "spacing_mm": SPACING_MM,
        "material_mm3": MATERIAL_MM3,
        "threshold_rule": "emin ** (1 / penal)",
        "hist_edges": EDGES,
        "cases": cases,
    }
    MERGED.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"まとめ: {MERGED}({len(cases)} ケース)", flush=True)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="動的な active set の実測")
    parser.add_argument("--pick", type=float, default=None, help="PICK_RATIO")
    parser.add_argument("--max-iter", type=int, default=40)
    parser.add_argument("--dynamic", action="store_true", help="dynamic_active=True で回す")
    parser.add_argument("--tag", default=None, help="出力ファイル名の識別子")
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--merge", action="store_true", help="ケースを1つの JSON へまとめる")
    args = parser.parse_args()

    if args.merge:
        merge()
        return 0
    if args.pick is None or args.tag is None:
        parser.error("--pick と --tag は必須(--merge のときを除く)")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = run_case(
        pick=args.pick,
        max_iter=args.max_iter,
        dynamic=args.dynamic,
        tag=args.tag,
        threads=args.threads,
    )
    return 1 if payload.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

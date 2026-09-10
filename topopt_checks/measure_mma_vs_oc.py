"""OC と MMA を**同じ入力**で回して、答え(形・硬さ・材料量)と費用を並べる実測。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_mma_vs_oc.py --stage demo30
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_mma_vs_oc.py --stage hanger
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_mma_vs_oc.py --stage all

`simp.optimize` の `update` は **`run_topopt`/`run_pipeline` へ通っていない**ので、
`anchor.run_pipeline` が `simp.optimize` へ渡す引数をそのまま横取りし(`_capture_optimize_kwargs`)、
その同じ引数で `simp.optimize(update="oc")` と `simp.optimize(update="mma")` を直接呼ぶ。
**両者は1つの process で連続して回すので、格子・境界条件・パッシブ要素は同一物である。**

**`change_tol=0.0` を明示する。** MMA は漸近線が歩幅を殺すので `Iteration.change` が
OC より早く小さくなり、既定の `change_tol=0.01` では停止条件の差が費用の差に化ける
(taskbox `20260910-1312` の契約 ③2)。反復数は `max_iter` で揃え、収束は
コンプライアンス履歴から**事後に**判定する(`_settle_iter`)。

`threads` は段ごとに固定する(既定は下の `STAGES`)。本番経路は `simp.PARALLEL_THREADS`
で動くが、比較の公平性のため測定側で固定する。
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any, Callable

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

OUTPUT_JSON = OUTPUT_DIR / "mma_vs_oc.json"

# 「収束した」と読む幅。最終コンプライアンスからの相対距離で、**これ以降ずっと**内側に
# 入っている最初の反復を収束反復と呼ぶ(`_settle_iter`)。1% と 0.1% の2本を出す。
SETTLE_TOLS = (0.01, 0.001)

STAGES: dict[str, dict[str, Any]] = {
    # 小さい2段は煙検査。**threads=1** で回すのは、格子が小さいと並列の当たり外れが
    # 1反復の秒数に乗って更新則そのものの費用が見えなくなるため。
    "demo30": {
        "kind": "demo",
        "derive": {"elements_long_axis": 30},
        "ratios": [0.10, 0.20, 0.40],
        "threads": 1,
        "max_iter": 100,
    },
    "demo44": {
        "kind": "demo",
        "derive": {"elements_long_axis": 44},
        "ratios": [0.10, 0.20, 0.40],
        "threads": 1,
        "max_iter": 100,
    },
    # 採否の判断はこの段の数字で行う。**threads=7** は本番経路の `simp.PARALLEL_THREADS`
    # と同じ値(この機械)。
    "hanger": {
        "kind": "hanger",
        "model": "hanger_test.json",
        "derive": {"spacing": 2.4},
        "ratios": [0.10, 0.30],
        "threads": 7,
        "max_iter": 40,
    },
}

UPDATE_NAMES = ("oc", "mma")


# ---------------------------------------------------------------------------
# 題材の組み立て
# ---------------------------------------------------------------------------
def _build_space(stage: dict[str, Any]) -> tuple[ds.DesignSpace, Any, Any]:
    """(塊, forces, conditions) を返す。"""
    if stage["kind"] == "demo":
        conditions = ds.demo_conditions()
        space = ds.derive(conditions, **stage["derive"])
        return space, None, conditions
    from duocad.optimize_run import build_conditions
    from duocad.session import Session

    session = Session(None, 0.2)
    session.open_document(stage["model"])
    bundle = build_conditions(session.document)
    space = ds.derive(bundle.conditions, **stage["derive"])
    return space, bundle.forces, bundle.conditions


class _Abort(Exception):
    """引数を捕まえたので最適化へは進ませない、という合図。"""


def _capture_optimize_kwargs(
    space: ds.DesignSpace, *, material_mm3: float, forces, threads: int, stl_path: Path
) -> tuple[tuple, dict]:
    """`anchor.run_pipeline` が `simp.optimize` へ渡す引数をそのまま横取りする。

    自分で境界条件を組み直すと、**製品が解いている問題とは別の問題を測る**ことになる。
    `anchor.py` は `from . import simp` を関数の中でやるので、モジュール属性を
    差し替えれば届く(taskbox `20260910-1312` の契約 ⑤)。
    """
    box: dict[str, Any] = {}
    real = simp.optimize

    def spy(*args: Any, **kwargs: Any):
        box["args"] = args
        box["kwargs"] = kwargs
        raise _Abort()

    simp.optimize = spy  # type: ignore[assignment]
    try:
        ds.run_topopt(
            space,
            material_mm3=material_mm3,
            max_iter=1,
            threads=threads,
            forces=forces,
            stl_path=stl_path,
        )
    except _Abort:
        pass
    finally:
        simp.optimize = real  # type: ignore[assignment]
    if "kwargs" not in box:
        raise RuntimeError("simp.optimize が呼ばれないまま run_topopt が終わった")
    return box["args"], box["kwargs"]


# ---------------------------------------------------------------------------
# 更新則の step だけを計る
# ---------------------------------------------------------------------------
def _install_step_timers(record: dict[str, list[float]]) -> dict[str, type]:
    """`UPDATES` の各クラスを、`step` の所要を記録する派生クラスへ差し替える。

    数値は変わらない(`step` の中身はそのまま `super()` を呼ぶ)。1反復の秒数のうち
    **更新則そのものが占める分**を分離するために要る —— 大半は CG(`model.solve`)で、
    そこは OC と MMA で同じコードだから、差が出るならここに出る。
    """
    originals = dict(simp.UPDATES)

    def wrap(name: str, cls: type) -> type:
        class _Timed(cls):  # type: ignore[valid-type,misc]
            def step(self, **kwargs: Any):  # type: ignore[override]
                t0 = time.perf_counter()
                out = super().step(**kwargs)
                record.setdefault(name, []).append(time.perf_counter() - t0)
                return out

        _Timed.__name__ = f"Timed{cls.__name__}"
        return _Timed

    for name, cls in originals.items():
        simp.UPDATES[name] = wrap(name, cls)
    return originals


# ---------------------------------------------------------------------------
# 指標
# ---------------------------------------------------------------------------
def _sha256(arr: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(arr, dtype=np.float64).tobytes()).hexdigest()


def _settle_iter(c_hist: np.ndarray, tol: float) -> int | None:
    """最終値から相対 `tol` の内側に入り、**以降ずっと**内側に留まる最初の反復(1始まり)。

    「その反復で初めて内側へ入った」ではなく「そこから出ない」で数える —— コンプライアンスは
    途中で下から突き抜けて戻ることがあり、初回到達だと早く見えすぎる。
    """
    if c_hist.size == 0:
        return None
    final = float(c_hist[-1])
    if not np.isfinite(final) or final <= 0.0:
        return None
    inside = np.abs(c_hist - final) / final < tol
    idx = c_hist.size
    for i in range(c_hist.size - 1, -1, -1):
        if not inside[i]:
            break
        idx = i
    return int(idx + 1) if idx < c_hist.size else None


def _run_metrics(result: simp.Result, step_seconds: list[float]) -> dict[str, Any]:
    c_hist = np.array([item.compliance for item in result.history], dtype=float)
    sec = np.array([item.seconds for item in result.history], dtype=float)
    steps = np.array(step_seconds, dtype=float)
    return {
        "update": result.update,
        "stopped_by": result.stopped_by,
        "iterations": len(result.history),
        "seconds_total": float(result.seconds),
        "seconds_iters_sum": float(sec.sum()),
        "seconds_per_iter_mean": float(sec.mean()) if sec.size else float("nan"),
        "seconds_per_iter_median": float(np.median(sec)) if sec.size else float("nan"),
        "seconds_per_iter_last10_mean": float(sec[-10:].mean()) if sec.size else float("nan"),
        "step_seconds_sum": float(steps.sum()) if steps.size else float("nan"),
        "step_seconds_median": float(np.median(steps)) if steps.size else float("nan"),
        "compliance_first": float(c_hist[0]) if c_hist.size else float("nan"),
        "compliance_final": float(c_hist[-1]) if c_hist.size else float("nan"),
        "compliance_min": float(c_hist.min()) if c_hist.size else float("nan"),
        "change_final": float(result.history[-1].change) if result.history else float("nan"),
        "settle_iter": {
            f"{tol:g}": _settle_iter(c_hist, tol) for tol in SETTLE_TOLS
        },
        "target_volume": float(result.target_volume),
        "achieved_volume": float(np.asarray(result.rho, dtype=float).sum()),
        "target_material_mm3": result.target_material_mm3,
        "achieved_material_mm3": result.achieved_material_mm3,
        "rho_sha256": _sha256(np.asarray(result.rho, dtype=float)),
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
    }


def _compare(
    rho_a: np.ndarray, rho_b: np.ndarray, *, movable: np.ndarray, element_volume: float
) -> dict[str, Any]:
    """a=OC / b=MMA の答えを比べる。`movable` は設計変数が動く要素(パッシブを除く)。

    **一致率はパッシブ要素を除いた集合で読む** —— void を含めると、外側の空要素が
    両方 0 で一致するぶんだけ一致率が水増しされる(hanger では箱の過半が void)。
    """
    a = np.asarray(rho_a, dtype=float)
    b = np.asarray(rho_b, dtype=float)
    sa = a >= 0.5
    sb = b >= 0.5
    inter = int(np.count_nonzero(sa & sb))
    union = int(np.count_nonzero(sa | sb))
    diff = sa != sb
    mv = np.asarray(movable, dtype=bool)
    n_mv = int(np.count_nonzero(mv))
    return {
        "agree_rate_all": float(np.count_nonzero(~diff) / diff.size),
        "agree_rate_movable": (
            float(np.count_nonzero(~diff & mv) / n_mv) if n_mv else float("nan")
        ),
        "iou_solid": float(inter / union) if union else float("nan"),
        "solid_elements_oc": int(np.count_nonzero(sa)),
        "solid_elements_mma": int(np.count_nonzero(sb)),
        "disagree_elements": int(np.count_nonzero(diff)),
        "disagree_mm3": float(np.count_nonzero(diff) * element_volume),
        "rho_abs_diff_max": float(np.abs(a - b).max()),
        "rho_abs_diff_mean_movable": (
            float(np.abs(a - b)[mv].mean()) if n_mv else float("nan")
        ),
        "gray_elements_oc": int(np.count_nonzero((a > 0.05) & (a < 0.95) & mv)),
        "gray_elements_mma": int(np.count_nonzero((b > 0.05) & (b < 0.95) & mv)),
    }


def _progress(tag: str) -> Callable[[simp.Iteration, np.ndarray], None]:
    def callback(iteration: simp.Iteration, _rho: np.ndarray) -> None:
        print(
            f"  [{tag}] 反復 {iteration.it}: C={iteration.compliance:.6f} "
            f"vol={iteration.volfrac:.4f} change={iteration.change:.5f} "
            f"{iteration.seconds:.2f}s",
            flush=True,
        )

    return callback


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------
def _apply_mma_consts(pairs: list[str]) -> dict[str, float]:
    """`MMA_ASYMPTOTE_INIT=0.2` の形で `simp` の MMA 定数を上書きする。

    **定数の校正で結果が変わるかを測るための口**(taskbox `20260910-1312` の契約 ④
    「MMA の定数は文献の標準値そのままで、この題材で校正していない」)。
    """
    applied: dict[str, float] = {}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        key = key.strip()
        if not key.startswith("MMA_") or not hasattr(simp, key):
            raise SystemExit(f"未知の MMA 定数 {key!r}")
        value = float(raw)
        setattr(simp, key, value)
        applied[key] = value
    return applied


def run_stage(
    name: str,
    *,
    max_iter: int | None = None,
    threads: int | None = None,
    ratios: list[float] | None = None,
) -> dict[str, Any]:
    stage = dict(STAGES[name])
    if ratios:
        stage["ratios"] = list(ratios)
    n_threads = int(stage["threads"] if threads is None else threads)
    n_iter = int(stage["max_iter"] if max_iter is None else max_iter)
    started = time.perf_counter()

    t0 = time.perf_counter()
    space, forces, _conditions = _build_space(stage)
    derive_seconds = time.perf_counter() - t0
    element_volume = float(space.grid.element_volume)
    capacity = float(space.element_count) * element_volume
    print(
        f"[{name}] 塊={space.element_count}要素 格子={space.grid.shape} "
        f"要素={element_volume ** (1/3):.4f}mm 容量={capacity:.1f}mm^3 "
        f"({derive_seconds:.1f}秒)",
        flush=True,
    )

    t0 = time.perf_counter()
    args, kwargs = _capture_optimize_kwargs(
        space,
        material_mm3=capacity * float(stage["ratios"][0]),
        forces=forces,
        threads=n_threads,
        stl_path=OUTPUT_DIR / f"mma_vs_oc_{name}_setup.stl",
    )
    setup_seconds = time.perf_counter() - t0
    void = np.asarray(kwargs["void_mask"], dtype=bool)
    solid = np.asarray(kwargs["solid_mask"], dtype=bool)
    movable = ~void & ~solid
    assert kwargs.get("volfrac") is None, "material_mm3 で回すので volfrac は None のはず"
    print(
        f"[{name}] 格子={args} 自由={int(movable.sum())} 固定ρ=1={int(solid.sum())} "
        f"空={int(void.sum())} (組み立て {setup_seconds:.1f}秒)",
        flush=True,
    )
    solid_mm3 = float(solid.sum()) * element_volume

    cases: list[dict[str, Any]] = []
    for ratio in stage["ratios"]:
        material = capacity * float(ratio)
        label = f"{name}-{ratio:.0%}"
        print(
            f"--- {label}: 材料={material:.1f}mm^3 (容量の{ratio:.0%} / "
            f"固定ρ=1 の {solid_mm3:.1f}mm^3 に対し {material / max(solid_mm3, 1e-12):.2f}倍) ---",
            flush=True,
        )
        runs: dict[str, Any] = {}
        rho: dict[str, np.ndarray] = {}
        for update in UPDATE_NAMES:
            call_kwargs = copy.deepcopy(kwargs)
            call_kwargs["material_mm3"] = material
            call_kwargs["max_iter"] = n_iter
            call_kwargs["change_tol"] = 0.0  # 反復数を揃える(契約 ③2)
            call_kwargs["threads"] = n_threads
            call_kwargs["update"] = update
            call_kwargs["callback"] = _progress(f"{label}/{update}")
            step_record: dict[str, list[float]] = {}
            originals = _install_step_timers(step_record)
            t_run = time.perf_counter()
            try:
                result = simp.optimize(*args, **call_kwargs)
            except Exception as exc:  # VolumeTargetError など
                runs[update] = {
                    "update": update,
                    "error": f"{type(exc).__name__}: {exc}",
                    "seconds_total": time.perf_counter() - t_run,
                }
                print(f"  [{label}/{update}] ERROR {type(exc).__name__}: {exc}", flush=True)
                continue
            finally:
                simp.UPDATES.update(originals)
            metrics = _run_metrics(result, step_record.get(update, []))
            runs[update] = metrics
            rho[update] = np.asarray(result.rho, dtype=float).copy()
            del result
            print(
                f"  [{label}/{update}] 完了 {metrics['iterations']}反復 "
                f"C={metrics['compliance_final']:.6f} "
                f"材料={metrics['achieved_material_mm3']:.1f}mm^3 "
                f"{metrics['seconds_total']:.1f}秒 "
                f"({metrics['seconds_per_iter_mean']:.2f}s/反復 "
                f"更新則 {metrics['step_seconds_median']:.3f}s)",
                flush=True,
            )
        case: dict[str, Any] = {
            "label": label,
            "stage": name,
            "material_ratio": float(ratio),
            "material_mm3": material,
            "runs": runs,
        }
        if "oc" in rho and "mma" in rho:
            case["compare"] = _compare(
                rho["oc"], rho["mma"], movable=movable, element_volume=element_volume
            )
            oc, mma = runs["oc"], runs["mma"]
            case["compare"].update(
                {
                    "compliance_rel_diff": (mma["compliance_final"] - oc["compliance_final"])
                    / oc["compliance_final"],
                    "material_rel_diff": (
                        mma["achieved_material_mm3"] - oc["achieved_material_mm3"]
                    )
                    / oc["achieved_material_mm3"],
                    "material_vs_target_oc": oc["achieved_material_mm3"] / material - 1.0,
                    "material_vs_target_mma": mma["achieved_material_mm3"] / material - 1.0,
                    "seconds_ratio": mma["seconds_total"] / oc["seconds_total"],
                    "seconds_per_iter_ratio": mma["seconds_per_iter_median"]
                    / oc["seconds_per_iter_median"],
                    "step_seconds_ratio": mma["step_seconds_median"]
                    / max(oc["step_seconds_median"], 1e-12),
                }
            )
            c = case["compare"]
            print(
                f"  => 一致率(自由要素)={c['agree_rate_movable']:.4f} IoU={c['iou_solid']:.4f} "
                f"C相対差={c['compliance_rel_diff']:+.4%} 材料相対差={c['material_rel_diff']:+.2e} "
                f"秒比={c['seconds_ratio']:.3f}",
                flush=True,
            )
        cases.append(case)

    return {
        "stage": name,
        "kind": stage["kind"],
        "model": stage.get("model"),
        "derive": dict(stage["derive"]),
        "threads": n_threads,
        "max_iter": n_iter,
        "change_tol": 0.0,
        "element_mm": element_volume ** (1.0 / 3.0),
        "element_volume_mm3": element_volume,
        "grid_shape": [int(v) for v in space.grid.shape],
        "design_elements": int(space.element_count),
        "capacity_mm3": capacity,
        "movable_elements": int(movable.sum()),
        "solid_elements": int(solid.sum()),
        "void_elements": int(void.sum()),
        "solid_mm3": solid_mm3,
        "derive_seconds": derive_seconds,
        "setup_seconds": setup_seconds,
        "cases": cases,
        "seconds_total": time.perf_counter() - started,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OC と MMA を同じ入力で比べる実測")
    parser.add_argument("--stage", default="all", help=f"{sorted(STAGES)} または all")
    parser.add_argument("--max-iter", type=int, default=None)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--out", default=None, help="書き出し先 JSON(既定は output/mma_vs_oc.json)")
    parser.add_argument("--merge", action="store_true", help="既存 JSON の他の段を残して更新する")
    parser.add_argument(
        "--mma-const",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="MMA_ASYMPTOTE_INIT=0.2 のように simp の MMA 定数を上書きする",
    )
    parser.add_argument("--tag", default=None, help="書き出し先のキー(既定は段の名前)")
    parser.add_argument(
        "--ratios",
        default=None,
        help="材料量(容量比)をカンマ区切りで上書きする。例 0.30",
    )
    args = parser.parse_args(argv)
    mma_consts = _apply_mma_consts(list(args.mma_const))

    names = list(STAGES) if args.stage == "all" else [s for s in args.stage.split(",")]
    for name in names:
        if name not in STAGES:
            raise SystemExit(f"未知の段 {name!r}(選べるのは {sorted(STAGES)})")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else OUTPUT_JSON
    payload: dict[str, Any] = {}
    if args.merge and out_path.is_file():
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    payload.setdefault("stages", {})
    payload["environment"] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": __import__("os").cpu_count(),
        "parallel_threads_default": int(simp.PARALLEL_THREADS),
        "numpy": np.__version__,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "script": "topopt_checks/measure_mma_vs_oc.py",
        "settle_tols": list(SETTLE_TOLS),
    }
    for name in names:
        stage_payload = run_stage(
            name,
            max_iter=args.max_iter,
            threads=args.threads,
            ratios=(
                [float(v) for v in args.ratios.split(",")] if args.ratios else None
            ),
        )
        stage_payload["mma_consts_overridden"] = mma_consts
        key = args.tag or name
        payload["stages"][key] = stage_payload
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"書き出し: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

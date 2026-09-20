"""段階4に warm start を通した探索を、`warm_start` の有無で1回ずつ回して比べる測定。

    .\.venv\Scripts\python.exe topopt_checks\measure_warm_start_search.py --mode warm --result C:\tmp\warm.json
    .\.venv\Scripts\python.exe topopt_checks\measure_warm_start_search.py --mode cold --result C:\tmp\cold.json
    .\.venv\Scripts\python.exe topopt_checks\measure_warm_start_search.py --compare C:\tmp\warm.json C:\tmp\cold.json

hanger・要素サイズ要求 2.4mm・目標2.0・既定の最小幅(m=1)。**1プロセス1モード**(探索は約7GBを使うので、
同じプロセスで2回回して2回目が1回目の残りに乗らないようにする)。**判定はしない**(数字と、採用形状の
`mask_hash` が一致したかを書き出すだけ)。`trace.DEFAULT_ROOT` を一時フォルダへ向け、`output/` に何も残さない。
空きメモリが `--min-free-gb` 未満なら測らずに終わる(終了コード3)。

`--mask-npz` を付けると採用マスクを保存する(`mask_hash` だけでは形の差が分からない)。保存した2つを
`--diff-masks WARM_NPZ COLD_NPZ [--stl-dir DIR]` で並べると、重なり・片方だけのセル・その距離を出し、
ボクセルのままのSTL(warm / cold / warm_only / cold_only)を書く。

**対照実験**: `--cg-rtol` は全 solve の CG 収束判定を上書きし、`--noise` は solve が返した変位の各自由度へ
相対 `noise` の正規乱数(`--noise-seed` で固定)を掛ける。どちらも warm start ではなく cold の解を微小に
ずらすので、「探索が微小な摂動で別の形へ分かれる」ことが warm 固有かを確かめるのに使う。
`--summarize REF.json OTHER.json ...` は、各 JSON を REF と並べて秒・CG反復・CPU競合・軌道の分岐点・形の重なりを出す
(形は JSON と同じ名前の `.npz` があるときだけ)。各実行は測定中の CPU 使用(全体・自プロセス)を10秒ごとに
記録するので、他のジョブと同時に走った実行は `load` の `other_cpu_*` で見分けられる。時間計測では、`--wait-quiet PCT`
が開始前に「全体のCPUが PCT% 未満で、他に大きな python が無い」状態が続くまで待ち、`--abort-if-busy PCT` が測定中に
他プロセスのCPUが高い状態が続いたらその実行を捨てて終了コード3で終わる(待ち行列側が再試行する)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  (topopt_checks の import 経路を通す)
import numpy as np
from measure_min_width_floor import voxel_surface
from measure_warm_start import free_gb

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "hanger_bar_attachment.json"
REQUESTED_ELEMENT_SIZE_MM = 2.4
TARGET_SAFETY_FACTOR = 2.0


class LoadSampler(threading.Thread):
    """測定中の CPU 使用(全体と自プロセス、どちらも全論理CPUに対する%)を一定間隔で記録する。

    他のジョブと同時に走ると秒が伸びるので、その有無を後から見分けるために取る(`other` = 全体 − 自プロセス)。
    psutil が無い環境では何も記録せず `summary()` が None を返す。
    """

    def __init__(self, interval: float = 10.0, abort_if_busy: float | None = None, abort_window: int = 6) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.abort_if_busy = abort_if_busy
        self.abort_window = abort_window
        self.samples: list[tuple[float, float]] = []
        self._stop_event = threading.Event()
        try:
            import psutil
        except ImportError:
            self._psutil = None
        else:
            self._psutil = psutil

    def run(self) -> None:
        psutil = self._psutil
        if psutil is None:
            return
        process, cpus = psutil.Process(), psutil.cpu_count()
        psutil.cpu_percent(None)
        process.cpu_percent(None)
        while not self._stop_event.wait(self.interval):
            self.samples.append((psutil.cpu_percent(None), process.cpu_percent(None) / cpus))
            if self.abort_if_busy is not None and len(self.samples) >= self.abort_window:
                other = float(np.mean([max(total - own, 0.0) for total, own in self.samples[-self.abort_window:]]))
                if other > self.abort_if_busy:
                    print(f"abort-if-busy: 他のジョブの CPU 使用が直近 {self.abort_window * self.interval:.0f}秒で平均 {other:.1f}% "
                          f"(上限 {self.abort_if_busy:g}%)。この実行は捨てる(終了コード3)", file=sys.stderr, flush=True)
                    os._exit(3)

    def stop(self) -> None:
        self._stop_event.set()
        self.join()

    def summary(self) -> dict | None:
        if not self.samples:
            return None
        total = np.array([s[0] for s in self.samples])
        own = np.array([s[1] for s in self.samples])
        other = np.maximum(total - own, 0.0)
        return dict(samples=len(self.samples), system_cpu_mean=float(total.mean()), own_cpu_mean=float(own.mean()),
                    other_cpu_mean=float(other.mean()), other_cpu_max=float(other.max()))


def busy_python_pids(min_rss: float = 1e9) -> list[int]:
    """自分以外で、常駐メモリが `min_rss` バイトを超える python(他の探索・測定)の pid。読み込み中でまだ CPU を
    使っていないジョブは CPU だけでは静かに見えるので、これも見る。"""
    import psutil

    me = psutil.Process().pid
    return [p.info["pid"] for p in psutil.process_iter(["pid", "name", "memory_info"])
            if p.info["pid"] != me and (p.info["name"] or "").lower().startswith("python")
            and p.info["memory_info"] is not None and p.info["memory_info"].rss > min_rss]


def wait_quiet(threshold: float, checks: int = 6, interval: float = 20.0, timeout: float = 3 * 3600) -> dict:
    """全体の CPU 使用が `threshold`% 未満で、他に大きな python が無い状態が `checks` 回続くまで待つ(他のジョブと
    同時に測って秒を汚さないため)。`timeout` を過ぎたら待つのをやめて測る(結果の `timed_out` が真になり、
    `load` で競合の有無を見られる)。"""
    import psutil

    started, quiet = time.time(), 0
    while quiet < checks and time.time() - started < timeout:
        usage = psutil.cpu_percent(interval=interval)
        heavy = busy_python_pids()
        quiet = quiet + 1 if usage < threshold and not heavy else 0
        print(f"wait-quiet: cpu={usage:.1f}% other_python={heavy} (need <{threshold:g}% and none, x{checks}; have {quiet})",
              file=sys.stderr, flush=True)
    return dict(waited_seconds=time.time() - started, timed_out=quiet < checks, threshold=threshold)


def instrumented_solve(original, *, cg_rtol_override: float | None, noise: float, seed: int, stats: dict, log: list):
    """`PreparedSystem.solve` を包んで、solve ごとの [秒, CG反復数] を `log` に積む。`cg_rtol_override` があれば
    収束判定を上書きし、`noise` があれば返った変位の各自由度に相対 `noise` の正規乱数を掛ける(拘束成分は 0 のまま)。"""
    rng = np.random.default_rng(seed)

    def solve(self, rhs, *, u0=None, cg_rtol=1e-9):
        started, before = time.perf_counter(), stats["cg_iterations"]
        out = original(self, rhs, u0=u0, cg_rtol=cg_rtol if cg_rtol_override is None else cg_rtol_override)
        if noise:
            out *= 1.0 + noise * rng.standard_normal(out.shape)
        log.append([time.perf_counter() - started, int(stats["cg_iterations"] - before)])
        return out

    return solve


def run(mode: str, budget: int, mask_npz: Path | None = None, *, label: str | None = None,
        cg_rtol: float | None = None, noise: float = 0.0, noise_seed: int = 0,
        abort_if_busy: float | None = None) -> dict:
    from duocad import optimize_run
    from duocad.server import Session
    from duocad.topopt import binary_search, design_space as ds, fem, trace

    out: dict = {"mode": mode, "label": label or mode, "model": str(MODEL),
                 "element_size_requested_mm": REQUESTED_ELEMENT_SIZE_MM,
                 "target_safety_factor": TARGET_SAFETY_FACTOR, "max_evaluations": int(budget),
                 "perturbation": {"cg_rtol": cg_rtol, "noise": noise, "noise_seed": noise_seed},
                 "free_gb_at_start": free_gb()}
    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root, \
            patch.object(trace, "DEFAULT_ROOT", Path(trace_root)):
        session = Session(None, 0.2, models_dir=MODEL.resolve().parent)
        session.open_document(MODEL.name)
        document = session.document
        bundle = optimize_run.build_conditions(document)
        limits = optimize_run.resolution_limits(document)
        spacing = optimize_run._snap_to_grid(
            limits["bbox_min"], limits["bbox_max"],
            optimize_run._resolve_element_size(limits, REQUESTED_ELEMENT_SIZE_MM))
        min_width = optimize_run._resolve_min_width(spacing, None)
        out.update(actual_spacing_mm=spacing, min_width_cells=min_width["cells"])
        space = ds.derive(bundle.conditions, spacing=spacing)
        table, mapping = optimize_run._map_boundary_conditions(space, bundle)
        out.update(grid_shape=list(space.grid.shape), design_space_elements=int(space.mask.sum()))
        if mapping["status"] != "pass":
            out["error"] = f"確定表が不成立: {mapping['status']}"
            return out
        opts = optimize_run.OptimizeOptions(target_safety_factor=TARGET_SAFETY_FACTOR, max_evaluations=budget)
        conditions = optimize_run._build_search_conditions(document, space, bundle, opts, table=table, mapping=mapping)
        solve_log: list = []
        sampler = LoadSampler(abort_if_busy=abort_if_busy)
        with fem.collect_solve_stats() as stats, patch.object(
                fem.PreparedSystem, "solve",
                instrumented_solve(fem.PreparedSystem.solve, cg_rtol_override=cg_rtol, noise=noise, seed=noise_seed,
                                   stats=stats, log=solve_log)):
            sampler.start()
            started, cpu_started = time.perf_counter(), time.process_time()
            result = binary_search.find_best_feasible(
                space.mask, conditions, max_evaluations=budget, min_width_cells=min_width["cells"],
                warm_start=(mode == "warm"))
            out["seconds"] = time.perf_counter() - started
            out["process_cpu_seconds"] = time.process_time() - cpu_started
            sampler.stop()
    out["free_gb_at_end"] = free_gb()

    out.update(search_status=result.status, termination=result.termination, evaluations=int(result.evaluations),
               seconds_per_evaluation=out["seconds"] / max(1, int(result.evaluations)),
               solves=int(stats["solves"]), cg_iterations_total=int(stats["cg_iterations"]),
               cg_iterations_each=[int(n) for n in stats["cg_iterations_each"]],
               cg_fallbacks=int(stats["fallbacks"]), solve_seconds=float(stats["solve_seconds"]),
               mg_setup_seconds=float(stats["mg_setup_seconds"]),
               assemble_seconds=float(stats["assemble_seconds"]), slice_seconds=float(stats["slice_seconds"]),
               solve_log=solve_log, load=sampler.summary())
    best = result.best_feasible
    reduction = (result.diagnostics or {}).get("reduction") or {}
    out.update(
        stop_reason=reduction.get("stop_reason"),
        warm_start_requested=reduction.get("warm_start"),
        warm_start_used=reduction.get("warm_start_used"),
        warm_start_fallbacks=reduction.get("warm_start_fallbacks"),
        reduction_fem_solves=reduction.get("fem_solves"),
        history=[dict(index=t.index, operation=t.operation, outcome=t.outcome, mask_hash=t.mask_hash,
                      safety_factor=t.safety_factor) for t in result.history],
    )
    if best is None:
        out["error"] = f"best_feasible が無い(status={result.status})"
        return out
    out.update(mask_hash=best.mask_hash, accepted_elements=int(best.mask.sum()),
               safety_factor=float(best.safety_factor))
    if mask_npz is not None:
        np.savez_compressed(mask_npz, mask=best.mask, origin=np.asarray(space.grid.origin, dtype=float),
                            spacing=float(space.grid.spacing))
    return out


def compare_masks(warm_npz: Path, cold_npz: Path, stl_dir: Path | None) -> dict:
    from scipy import ndimage
    from duocad.topopt import mesh

    warm, cold = np.load(warm_npz), np.load(cold_npz)
    origin, spacing = np.asarray(warm["origin"], dtype=float), float(warm["spacing"])
    if not (warm["mask"].shape == cold["mask"].shape and np.allclose(origin, cold["origin"])
            and spacing == float(cold["spacing"])):
        raise SystemExit("格子が違う2つのマスクは比べられない")
    w, c = warm["mask"].astype(bool), cold["mask"].astype(bool)
    only = {"warm_only": w & ~c, "cold_only": c & ~w}
    both = int((w & c).sum())
    out: dict = dict(spacing_mm=spacing, warm_cells=int(w.sum()), cold_cells=int(c.sum()), both=both,
                     warm_only=int(only["warm_only"].sum()), cold_only=int(only["cold_only"].sum()),
                     iou=both / int((w | c).sum()))
    for name, other in (("warm_only", c), ("cold_only", w)):
        cells = only[name]
        if not cells.any():
            continue
        distance = ndimage.distance_transform_edt(~other)[cells] * spacing
        labels, count = ndimage.label(cells)
        index = np.argwhere(cells)
        out[name + "_detail"] = dict(
            distance_to_other_mm=dict(mean=float(distance.mean()), max=float(distance.max())),
            components=int(count),
            largest_components=[int(n) for n in sorted(np.bincount(labels.ravel())[1:], reverse=True)[:5]],
            bbox_min_mm=(origin + spacing * index.min(axis=0)).tolist(),
            bbox_max_mm=(origin + spacing * (index.max(axis=0) + 1)).tolist())
    if stl_dir is not None:
        stl_dir.mkdir(parents=True, exist_ok=True)
        for name, cells in {"warm": w, "cold": c, **only}.items():
            verts, faces = voxel_surface(cells, origin, spacing)
            if len(faces):
                mesh.write_stl(stl_dir / f"{name}.stl", verts, faces)
                out[name + "_stl_triangles"] = int(len(faces))
    return out


def compare(warm_path: Path, cold_path: Path) -> dict:
    warm, cold = (json.loads(p.read_text(encoding="utf-8")) for p in (warm_path, cold_path))
    same_history = [(t["mask_hash"], t["outcome"]) for t in warm["history"]] == \
                   [(t["mask_hash"], t["outcome"]) for t in cold["history"]]
    return dict(
        mask_hash_equal=warm.get("mask_hash") == cold.get("mask_hash"),
        history_equal=same_history,
        evaluations=(warm["evaluations"], cold["evaluations"]),
        termination=(warm["termination"], cold["termination"]),
        seconds=(warm["seconds"], cold["seconds"]),
        seconds_ratio=warm["seconds"] / cold["seconds"],
        seconds_per_evaluation=(warm["seconds_per_evaluation"], cold["seconds_per_evaluation"]),
        cg_iterations_total=(warm["cg_iterations_total"], cold["cg_iterations_total"]),
        cg_iterations_ratio=warm["cg_iterations_total"] / cold["cg_iterations_total"],
        warm_start_used_fallbacks=(warm["warm_start_used"], warm["warm_start_fallbacks"]),
    )


def summarize(reference: Path, others: list[Path]) -> list[dict]:
    """各結果を REF と並べる。形の重なりは、JSON と同じ名前の `.npz` が両方にあるときだけ出す。"""
    def load_mask(path: Path):
        npz = path.with_suffix(".npz")
        return np.load(npz)["mask"].astype(bool) if npz.exists() else None

    ref = json.loads(reference.read_text(encoding="utf-8"))
    ref_mask = load_mask(reference)
    rows = []
    for path in [reference, *others]:
        data = json.loads(path.read_text(encoding="utf-8"))
        pairs = list(zip(data["history"], ref["history"]))
        mask = load_mask(path)
        load = data.get("load") or {}
        rows.append(dict(
            file=path.name, label=data.get("label"), mode=data["mode"], perturbation=data.get("perturbation"),
            evaluations=data["evaluations"], seconds=round(data["seconds"], 1),
            seconds_per_evaluation=round(data["seconds_per_evaluation"], 2),
            process_cpu_seconds=round(data["process_cpu_seconds"], 1) if "process_cpu_seconds" in data else None,
            solve_seconds=round(data["solve_seconds"], 1), cg_iterations_total=data["cg_iterations_total"],
            other_cpu_mean=round(load["other_cpu_mean"], 1) if load else None,
            other_cpu_max=round(load["other_cpu_max"], 1) if load else None,
            first_divergence_vs_reference=next((i for i, (a, b) in enumerate(pairs) if a["mask_hash"] != b["mask_hash"]), None),
            safety_factor_rel_diff_first4=[
                None if a["safety_factor"] is None or b["safety_factor"] is None
                else abs(a["safety_factor"] - b["safety_factor"]) / b["safety_factor"] for a, b in pairs[:4]],
            accepted_elements=data.get("accepted_elements"), final_safety_factor=data.get("safety_factor"),
            mask_hash8=(data.get("mask_hash") or "")[:8],
            iou_vs_reference=None if mask is None or ref_mask is None
            else float((mask & ref_mask).sum() / (mask | ref_mask).sum())))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("warm", "cold"))
    parser.add_argument("--budget", type=int, default=20, help="評価予算(既定20)")
    parser.add_argument("--min-free-gb", type=float, default=12.0)
    parser.add_argument("--result", type=Path, default=None, help="結果JSONの書き出し先")
    parser.add_argument("--label", default=None, help="結果に書く名前(既定は --mode)")
    parser.add_argument("--cg-rtol", type=float, default=None, help="全 solve の CG 収束判定を上書きする(対照実験)")
    parser.add_argument("--noise", type=float, default=0.0, help="solve が返す変位の各自由度に掛ける相対ノイズの大きさ(対照実験)")
    parser.add_argument("--noise-seed", type=int, default=0)
    parser.add_argument("--wait-quiet", type=float, default=None, metavar="PCT",
                        help="全体のCPU使用が PCT%% 未満の状態が続くまで待ってから測る(時間計測用。空きメモリの検査のあとに待つ)")
    parser.add_argument("--abort-if-busy", type=float, default=None, metavar="PCT",
                        help="測定中、他プロセスのCPU使用が直近60秒で平均 PCT%% を超えたらこの実行を捨てて終了コード3で終わる")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("WARM_JSON", "COLD_JSON"))
    parser.add_argument("--summarize", nargs="+", type=Path, metavar="JSON", help="先頭を基準に、残りを並べる")
    parser.add_argument("--mask-npz", type=Path, default=None, help="採用マスクの保存先(省略時は保存しない)")
    parser.add_argument("--diff-masks", nargs=2, type=Path, metavar=("WARM_NPZ", "COLD_NPZ"))
    parser.add_argument("--stl-dir", type=Path, default=None, help="--diff-masks のSTLの書き出し先")
    args = parser.parse_args()
    if args.compare:
        print(json.dumps(compare(*args.compare), ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.summarize:
        print(json.dumps(summarize(args.summarize[0], args.summarize[1:]), ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.diff_masks:
        print(json.dumps(compare_masks(*args.diff_masks, args.stl_dir), ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.mode is None:
        parser.error("--mode か --compare か --summarize が要る")
    free = free_gb()
    if free < args.min_free_gb:
        print(f"空きメモリ {free:.1f}GB が下限 {args.min_free_gb}GB 未満。測らずに終わる", flush=True)
        return 3
    gate = wait_quiet(args.wait_quiet) if args.wait_quiet else None
    result = run(args.mode, args.budget, args.mask_npz, label=args.label, cg_rtol=args.cg_rtol,
                 noise=args.noise, noise_seed=args.noise_seed, abort_if_busy=args.abort_if_busy)
    result["quiet_gate"] = gate
    text = json.dumps(result, ensure_ascii=False, indent=2, default=float)
    if args.result:
        args.result.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

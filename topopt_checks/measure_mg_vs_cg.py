"""探索が解く形(二値マスク・材料要素だけで組んだ K)で、Jacobi 前処理 CG(`solver='cg'`)と
幾何マルチグリッド前処理 CG(`solver='mg'`)の反復数・秒・メモリ・解の一致を並べる。題材は hanger。

形は「全設計空間」「採用マスクを設計空間の中で n 層膨らませたもの」「採用マスクそのもの」。
各形を cg → mg の順に `--repeat` 回ずつ解く(秒の散らばりを見るため)。

メモリは solve の直前の常駐量(`rss_before_gb`。格子・`fem.Model` まで組んだ状態)と、solve 中の最大
(`rss_peak_gb`。50ms 間隔の標本と Windows の `peak_wset` の大きいほう)。`--isolate` を付けると solve 1回ごとに
子プロセスを立てる —— 同じプロセスで続けて解くと、前の solve が返したメモリが常駐量に残って次の増分が小さく出る。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_mg_vs_cg.py --spacing 2.4 \\
        --mask-npz <採用マスク.npz> --dilate 6 2 0 --isolate --result <json>
"""
import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import _record
import numpy as np
import psutil
from scipy import ndimage

from duocad.topopt import fem
from measure_warm_start import PENAL, free_gb, solve_once
from measure_warm_start_kcurve import _setup, model_for

SOLVERS = ("cg", "mg")


def shapes(space, adopted, layers):
    """(名前, マスク) の並び。膨らませた形は設計空間の中に切り、連結は採用マスクから引き継ぐ。"""
    rows = [("full", np.array(space.mask, dtype=bool))]
    for n in layers:
        grown = adopted if n == 0 else ndimage.binary_dilation(adopted, iterations=n) & space.mask
        rows.append((f"adopted+{n}", grown))
    return rows


class RssSampler(threading.Thread):
    def __init__(self, interval: float = 0.05) -> None:
        super().__init__(daemon=True)
        self.interval, self.peak = interval, 0
        self._process, self._stop_event = psutil.Process(), threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            self.peak = max(self.peak, self._process.memory_info().rss)

    def stop(self) -> int:
        self._stop_event.set()
        self.join()
        return self.peak


def _memory():
    info = psutil.Process().memory_info()
    return info.rss, getattr(info, "peak_wset", info.rss)


def solve_measured(model, density, solver):
    rss_before, wset_before = _memory()
    sampler = RssSampler()
    sampler.start()
    displacement, stats = solve_once(model, density, solver, None)
    sampled = sampler.stop()
    rss_after, wset_after = _memory()
    # peak_wset はプロセスの通算最大。solve 中に伸びていれば、それが solve 中の最大そのもの(標本の取りこぼしが無い)。
    peak = max(sampled, rss_after, wset_after if wset_after > wset_before else 0)
    residual = model.stiffness_apply(density, PENAL, displacement) - model.f
    relative = float(np.linalg.norm(residual[model.free]) / np.linalg.norm(model.f[model.free]))
    return displacement, dict(solver=solver, **stats, relative_residual=relative,
                              rss_before_gb=rss_before / 1e9, rss_peak_gb=peak / 1e9,
                              rss_increase_gb=(peak - rss_before) / 1e9, free_gb_before=free_gb())


def measure(name, mask, conditions, repeat):
    model = model_for(mask, conditions)
    density = mask.astype(float)
    row = dict(shape=name, cells=int(mask.sum()), free_dofs=int(model.free.size), runs=[])
    reference = None
    for index in range(repeat):
        for solver in SOLVERS:
            displacement, run = solve_measured(model, density, solver)
            if reference is None:
                reference = displacement
            scale = max(float(np.abs(reference).max()), 1e-30)
            run.update(index=index,
                       max_relative_difference_to_first=float(np.abs(displacement - reference).max() / scale))
            row["runs"].append(run)
            print(name, run, flush=True)
    return row


def measure_isolated(argv, names, repeat):
    """solve 1回ごとに子プロセス(`--one`)を立てる。変位は子の中に閉じるので、解の一致は同じプロセスの版で見る。"""
    rows = []
    for name in names:
        row = dict(shape=name, runs=[])
        for index in range(repeat):
            for solver in SOLVERS:
                out = subprocess.run([sys.executable, __file__, *argv, "--one", name, solver],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
                lines = [line for line in out.stdout.splitlines() if line.startswith("RESULT ")]
                if out.returncode != 0 or not lines:
                    raise SystemExit(f"子プロセスが失敗した({name}/{solver}): {out.stderr[-800:]}")
                child = json.loads(lines[-1][len("RESULT "):])
                row.update(cells=child["cells"], free_dofs=child["free_dofs"])
                row["runs"].append(dict(child["run"], index=index))
                print(name, row["runs"][-1], flush=True)
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spacing", type=float, required=True, help="要素サイズの要求値[mm](製品と同じ丸めを通す)")
    parser.add_argument("--mask-npz", type=Path, required=True, help="採用マスク(mask・origin・spacing を持つ npz)")
    parser.add_argument("--dilate", type=int, nargs="+", default=[6, 2, 0], help="採用マスクを膨らませる層数")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--isolate", action="store_true", help="solve 1回ごとに子プロセスを立てる(メモリの測定用)")
    parser.add_argument("--one", nargs=2, metavar=("SHAPE", "SOLVER"), help="(内部用)1つの形を1回だけ解いて RESULT 行を出す")
    parser.add_argument("--min-free-gb", type=float, default=10.0)
    parser.add_argument("--result", type=Path, default=None)
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    if free_gb() < args.min_free_gb:
        raise SystemExit(f"空きメモリ {free_gb():.1f}GB < {args.min_free_gb}GB。測らずに終わる")

    space, conditions, _min_width = _setup(args.spacing)
    data = np.load(args.mask_npz)
    adopted = data["mask"].astype(bool)
    if adopted.shape != tuple(space.grid.shape) or not np.allclose(data["origin"], space.grid.origin):
        raise SystemExit(f"格子が違う: モデル {tuple(space.grid.shape)} / マスク {adopted.shape}")
    rows = shapes(space, adopted, args.dilate)

    if args.one:
        name, solver = args.one
        mask = dict(rows)[name]
        model = model_for(mask, conditions)
        _, run = solve_measured(model, mask.astype(float), solver)
        print("RESULT " + json.dumps(dict(cells=int(mask.sum()), free_dofs=int(model.free.size), run=run)), flush=True)
        return 0

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parent).stdout.strip()
    record = dict(commit=commit, spacing=float(space.grid.spacing), grid=list(space.grid.shape),
                  threads=int(fem.PARALLEL_THREADS), cg_rtol=1e-9, mask_npz=str(args.mask_npz),
                  isolated=bool(args.isolate), started=time.strftime("%Y-%m-%dT%H:%M:%S"), shapes=[])
    if args.isolate:
        argv = ["--spacing", str(args.spacing), "--mask-npz", str(args.mask_npz),
                "--dilate", *[str(n) for n in args.dilate], "--min-free-gb", str(args.min_free_gb)]
        record["shapes"] = measure_isolated(argv, [name for name, _ in rows], args.repeat)
    else:
        for name, mask in rows:
            record["shapes"].append(measure(name, mask, conditions, args.repeat))
            if args.result:
                _record.write(args.result, record, default=float)
    if args.result:
        _record.write(args.result, record, default=float)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

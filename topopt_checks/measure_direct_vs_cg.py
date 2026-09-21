"""探索が出した形(二値マスク・材料要素だけで組んだ K)で、Jacobi 前処理 CG(`solver='cg'`)と直接解法
(`solver='direct'`。`scipy.sparse.linalg.spsolve`)の秒・メモリの増分・解の一致を並べる。題材は hanger。

形は `--mask-npz` に並べた npz と、それぞれを設計空間の中で `--dilate` 層膨らませたもの。条件節点は形ごとに取り直す
(探索の `acceptance.safety_check` と同じ)。メモリの増分は同じプロセスで続けて解いた値(前の solve が返した分が
常駐量に残るので、小さめに出る)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_direct_vs_cg.py --spacing 2.4 \\
        --mask-npz <形1.npz> <形2.npz> --dilate 0 --result <json>
"""
import argparse
import sys
import threading
from pathlib import Path

import _bootstrap  # noqa: F401
import _record
import numpy as np
import psutil
from scipy import ndimage

from measure_warm_start import free_gb, solve_once
from measure_warm_start_kcurve import _setup, model_for


def solve_measured(model, density, solver):
    process = psutil.Process()
    before, peak, stop = process.memory_info().rss, [0], threading.Event()

    def sample():
        while not stop.wait(0.02):
            peak[0] = max(peak[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    displacement, stats = solve_once(model, density, solver, None)
    stop.set()
    sampler.join()
    return displacement, dict(stats, rss_increase_gb=max(peak[0] - before, 0) / 1e9)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spacing", type=float, required=True, help="要素サイズの要求値[mm](製品と同じ丸めを通す)")
    parser.add_argument("--mask-npz", type=Path, nargs="+", required=True, help="形(mask を持つ npz)")
    parser.add_argument("--dilate", type=int, nargs="+", default=[0], help="各形を設計空間の中で膨らませる層数")
    parser.add_argument("--direct-limit", type=int, default=120_000,
                        help="直接解法で解く自由度の上限(超える形は cg だけ測る。3次元の太い形は分解のメモリが急に増える)")
    parser.add_argument("--min-free-gb", type=float, default=10.0)
    parser.add_argument("--result", type=Path, default=None)
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    if free_gb() < args.min_free_gb:
        raise SystemExit(f"空きメモリ {free_gb():.1f}GB < {args.min_free_gb}GB。測らずに終わる")

    space, conditions, _min_width = _setup(args.spacing)
    record = dict(spacing=float(space.grid.spacing), cg_rtol=1e-9, shapes=[])
    for path in args.mask_npz:
        base = np.load(path)["mask"].astype(bool)
        if base.shape != tuple(space.grid.shape):
            raise SystemExit(f"格子が違う: モデル {tuple(space.grid.shape)} / {path} {base.shape}")
        for layers in args.dilate:
            mask = base if layers == 0 else ndimage.binary_dilation(base, iterations=layers) & space.mask
            model = model_for(mask, conditions)
            density = mask.astype(float)
            row = dict(shape=path.stem, layers=layers, cells=int(mask.sum()), free_dofs=int(model.free.size))
            u_cg, row["cg"] = solve_measured(model, density, "cg")
            if model.free.size <= args.direct_limit:
                u_direct, row["direct"] = solve_measured(model, density, "direct")
                row["max_relative_difference"] = float(np.abs(u_cg - u_direct).max() / np.abs(u_direct).max())
            record["shapes"].append(row)
            print(row, flush=True)
            if args.result:
                _record.write(args.result, record, default=float)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

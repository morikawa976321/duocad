"""CG(Jacobi 前処理・cold)を1回だけ深く回し、相対残差が 10^-p を初めて切った反復数と、その時点の安全率の誤差を並べる。題材は hanger。

`cg_rtol` を緩めたときの反復数と安全率を、rtol ごとに解き直さずに1回の solve から取る(cold の CG は止める場所が違うだけで
同じ列をたどる)。残差は `EVERY` 反復ごとに `‖K x − f‖/‖f‖` を計算し直した値(scipy の判定は漸化式の残差なので、
1e-9 付近から下は一致しない)。基準の安全率は直接解法、自由度が `--direct-limit` を超える形は最後の反復。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_cg_convergence.py --spacing 2.4 \\
        --mask-npz <採用マスク.npz> --dilate 0 2 6 --result <json>
"""
import argparse
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import _record
import numpy as np
import scipy.sparse.linalg as spla
from scipy import ndimage

from duocad.topopt import stress
from _hanger_setup import PENAL, _setup, free_gb, model_for

EVERY = 20
DEEPEST = 12


def converge(model, density, conditions, direct_limit):
    prepared = model.prepare(density, PENAL, solver="cg")
    kf, ff = prepared.kf, model.f[model.free]
    norm = float(np.linalg.norm(ff))
    scale = stress.UnitScale(conditions.grid.spacing, 1.0, conditions.youngs_modulus_mpa, solved_load=1.0)
    state = dict(k=0, p=1)
    history, checkpoints = [], {}

    def callback(xk):
        state["k"] += 1
        if state["k"] % EVERY:
            return
        relative = float(np.linalg.norm(kf @ xk - ff)) / norm
        history.append((state["k"], relative))
        while state["p"] <= DEEPEST and relative <= 10.0 ** -state["p"]:
            checkpoints[state["p"]] = (state["k"], relative, xk.copy())
            state["p"] += 1

    def report(x):
        u = np.zeros(model.ndof)
        u[model.free] = x
        result = stress.evaluate(model, density, scale=scale, allowable_mpa=conditions.allowable_mpa, u=u,
                                 percentile=stress.DEFAULT_PERCENTILE)
        return float(result.safety_factor), u

    started = time.perf_counter()
    x_final, info = spla.cg(prepared._a_op, ff, M=prepared._precond, rtol=10.0 ** -DEEPEST, maxiter=200_000,
                            callback=callback)
    seconds = time.perf_counter() - started
    direct = model.free.size <= direct_limit
    sf_reference, u_reference = report(spla.spsolve(kf, ff) if direct else x_final)
    u_scale = float(np.abs(u_reference).max())
    points = []
    for p, (k, relative, x) in sorted(checkpoints.items()):
        sf, u = report(x)
        points.append(dict(p=p, iterations=k, relative_residual=relative, safety_factor=sf,
                           sf_relative_error=sf / sf_reference - 1,
                           u_max_relative_error=float(np.abs(u - u_reference).max() / u_scale)))
    return dict(iterations_total=state["k"], info=int(info), seconds=seconds,
                reference="direct" if direct else "cg_final", sf_reference=sf_reference, points=points,
                history=history[:: max(1, len(history) // 200)])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spacing", type=float, required=True, help="要素サイズの要求値[mm](製品と同じ丸めを通す)")
    parser.add_argument("--mask-npz", type=Path, required=True, help="採用マスク(mask を持つ npz)")
    parser.add_argument("--dilate", type=int, nargs="+", default=[0], help="採用マスクを設計空間の中で膨らませる層数")
    parser.add_argument("--direct-limit", type=int, default=120_000, help="基準を直接解法で取る自由度の上限")
    parser.add_argument("--min-free-gb", type=float, default=10.0)
    parser.add_argument("--result", type=Path, default=None)
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    if free_gb() < args.min_free_gb:
        raise SystemExit(f"空きメモリ {free_gb():.1f}GB < {args.min_free_gb}GB。測らずに終わる")

    space, conditions, _min_width = _setup(args.spacing)
    adopted = np.load(args.mask_npz)["mask"].astype(bool)
    if adopted.shape != tuple(space.grid.shape):
        raise SystemExit(f"格子が違う: モデル {tuple(space.grid.shape)} / マスク {adopted.shape}")
    record = dict(spacing=float(space.grid.spacing), mask_npz=str(args.mask_npz), every=EVERY, shapes=[])
    for layers in args.dilate:
        mask = adopted if layers == 0 else ndimage.binary_dilation(adopted, iterations=layers) & space.mask
        model = model_for(mask, conditions)
        row = dict(layers=layers, cells=int(mask.sum()), free_dofs=int(model.free.size),
                   **converge(model, mask.astype(float), conditions, args.direct_limit))
        record["shapes"].append(row)
        print({key: value for key, value in row.items() if key != "history"}, flush=True)
        if args.result:
            _record.write(args.result, record, default=float)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

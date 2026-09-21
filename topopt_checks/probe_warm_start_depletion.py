"""1セル削りの連鎖で、warm start の比が「除去するセルのエネルギー」とともに変わることを測る。

旧測定(1セル 0.02)が今と食い違う理由の確認用(docs/TOPOPT_SPEED.md の節「旧測定の1セル 0.02 が今と食い違う理由」)。
種の形は 4.8mm の採用マスクを写して条件節点を支えるセルを足しただけで、探索(エネルギー順の削減)を経ていない。
そこから1セルずつ、直前の解(cold)を warm の初期値にして削り、手ごとに 比(warm / cold の反復数)・初期残差 r0・
除去したセルのエネルギーが全体に占める割合 を残す。

    .\\.venv\\Scripts\\python.exe topopt_checks\\probe_warm_start_depletion.py --spacing 2.4 --masks <t2-default.npz のある所> --steps 60 --out <json>
"""
import argparse
import json
import sys
from pathlib import Path

import _bootstrap  # noqa: F401
import measure_warm_start_kcurve as kc
import numpy as np
from measure_warm_start import PENAL, free_gb, support_cells, transfer

from duocad.topopt import binary_search


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--spacing', type=float, required=True)
    parser.add_argument('--masks', type=Path, required=True)
    parser.add_argument('--steps', type=int, default=60)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--min-free-gb', type=float, default=2.5)
    args = parser.parse_args()

    space, conditions, _ = kc._setup(args.spacing)
    anchor_ids = binary_search._anchor_node_ids(conditions)
    coarse = np.load(args.masks / 't2-default.npz')
    moved = transfer(coarse['best'], coarse['origin'], float(coarse['spacing']), space.grid) & space.mask
    support = support_cells(anchor_ids, space.grid.shape, space.mask)
    survivors, _ = binary_search.resolve_islands(moved | support, conditions)
    if not survivors:
        raise SystemExit('写した形が島除去で消えた')
    parent, _ = kc.cold_parent(np.ascontiguousarray(survivors[0]), conditions)

    def energy_of(chain):
        _, energy = chain.model.compliance(chain.mask.astype(float), PENAL, chain.displacement)
        return np.where(chain.mask, energy, 0.0)

    energy = energy_of(parent)
    fractions = np.sort(energy[parent.mask]) / energy.sum()
    census = {f'<{t:g}': int((fractions < t).sum()) for t in (1e-13, 1e-12, 1e-11, 1e-10, 1e-9, 1e-8, 1e-7, 1e-6)}
    print(f'種の形 {int(parent.mask.sum())} セル(写した {int(moved.sum())})。エネルギーが全体の t 未満のセル数: {census}', flush=True)

    records = []
    for step in range(args.steps):
        if free_gb() < args.min_free_gb:
            print(f'空きメモリ {free_gb():.1f}GB で中断', file=sys.stderr)
            break
        _, peeled = binary_search.peel(parent.mask, parent.order, 1, anchor_ids)
        out, result = kc.one_step(parent, 1, conditions, anchor_ids)
        if out is None:
            break
        picked = np.asarray(peeled.picked_cells)
        records.append(dict(step=step, cells=result['cells'], ratio=result['iteration_ratio'],
                            r0=result['initial_relative_residual'],
                            removed_energy_fraction=float(energy.ravel()[picked].sum() / energy.sum()),
                            cold_iterations=result['cold']['iterations'], warm_iterations=result['warm']['iterations']))
        print('手 %2d 残り%d セル 比 %.3f  r0 %.2e  除去セルのエネルギー割合 %.2e  cold %d / warm %d 反復' % (
            step, records[-1]['cells'], records[-1]['ratio'], records[-1]['r0'],
            records[-1]['removed_energy_fraction'], records[-1]['cold_iterations'], records[-1]['warm_iterations']), flush=True)
        parent = kc.Chain(out[0], out[1], conditions)
        energy = energy_of(parent)
    if len(records) > 2:
        fraction = np.array([r['removed_energy_fraction'] for r in records])
        residual = np.array([r['r0'] for r in records])
        slope = float(np.polyfit(np.log10(fraction), np.log10(residual), 1)[0])
        print('r0 ~ (除去セルのエネルギー割合)^%.3f、r0 / sqrt(割合) の中央値 %.0f' % (slope, np.median(residual / np.sqrt(fraction))))
    if args.out:
        args.out.write_text(json.dumps(dict(census=census, records=records), ensure_ascii=False), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

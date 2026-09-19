"""warm start(前の候補の変位を次の候補のCG初期値にする)が効くかを、hanger の 2.4mm と 2.0mm で測る。

段階4が実際に扱う細さの形から出発する必要があるので、初期形状は 4.8mm の採用形状を各格子へ写した
もの(材料量は実測の採用形状とほぼ同じ)。そこから `peel` で削った形を、u0 なし(cold)と
u0=前の形の変位(warm)の両方で解き、CG反復数・秒・解の一致を比べる。

    .\.venv\Scripts\python.exe topopt_checks/measure_warm_start.py --spacing 2.4 \
        --masks <4.8mm の採用マスク t2-default.npz がある所> --out <json>
"""
import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np

from duocad import optimize_run
from duocad.session import Session
from duocad.topopt import acceptance, binary_search, design_space, fem

MODELS_DIR = str(Path(__file__).resolve().parent.parent / 'models')
PENAL = 3.0


class _MemoryStatus(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


def free_gb():
    """空き物理メモリ(GB)。2.0mm は過去にここで機械を止めかけているので毎回見る。"""
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullAvailPhys / 1e9


def transfer(coarse_mask, coarse_origin, coarse_spacing, grid):
    """粗い格子のマスクを、細かい格子のセル中心の位置で引き当てる。"""
    out = np.zeros(grid.shape, dtype=bool)
    index, inside = [], []
    for axis in range(3):
        centre = np.asarray(grid.centers_1d(axis), dtype=float)
        raw = np.floor((centre - coarse_origin[axis]) / coarse_spacing)
        index.append(np.clip(raw, 0, coarse_mask.shape[axis] - 1).astype(np.int64))
        inside.append((raw >= 0) & (raw < coarse_mask.shape[axis]))
    picked = coarse_mask[np.ix_(*index)]
    keep = np.ix_(*inside)
    out[keep] = picked[keep]
    return out


def support_cells(anchor_ids, shape, design):
    """条件節点に角として接する設計空間セル。

    粗い格子の形をそのまま写すと、細かい格子の条件節点(拘束面・荷重面)を掴めずに
    `resolve_islands` で全部落ちる(2.4mm の実測: 3,535セル残っているのに fixed_face_23 の
    18節点中14個が支えられず survivors=0)。測りたいのは warm start の効きであって
    粗→細の写しの質ではないので、条件節点を支えるぶんだけ足して成立させる。
    """
    sizes = [n + 1 for n in shape]
    corners = np.unravel_index(np.asarray(anchor_ids), tuple(sizes))
    out = np.zeros(shape, dtype=bool)
    for di in (0, -1):
        for dj in (0, -1):
            for dk in (0, -1):
                i, j, k = corners[0] + di, corners[1] + dj, corners[2] + dk
                ok = ((i >= 0) & (i < shape[0]) & (j >= 0) & (j < shape[1])
                      & (k >= 0) & (k < shape[2]))
                out[i[ok], j[ok], k[ok]] = True
    return out & design


def build_model(material, fixed, load):
    live = acceptance.active_nodes(material).ravel()
    active = np.repeat(live, 3)
    return fem.Model(*material.shape, fixed_dofs=fixed, load=load,
                     extra_fixed_dofs=np.flatnonzero(~active), active_mask=material)


def solve_once(model, density, solver, u0):
    with fem.collect_solve_stats() as stats:
        started = time.perf_counter()
        displacement = model.solve(density, PENAL, solver=solver, u0=u0)
        seconds = time.perf_counter() - started
    return displacement, dict(iterations=int(stats['cg_iterations']), seconds=seconds,
                              solve_seconds=float(stats['solve_seconds']),
                              fallbacks=int(stats['fallbacks']))


def compare(model, density, solver, u_previous):
    """同じ形を cold(初期値0)と warm(前の形の変位)で解き、反復数・秒・解の一致を返す。"""
    u_cold, cold = solve_once(model, density, solver, None)
    u_warm, warm = solve_once(model, density, solver, u_previous)
    scale = max(float(np.abs(u_cold).max()), 1e-30)
    return u_cold, u_warm, dict(
        cold=cold, warm=warm,
        iteration_ratio=(warm['iterations'] / cold['iterations']) if cold['iterations'] else None,
        seconds_ratio=(warm['seconds'] / cold['seconds']) if cold['seconds'] else None,
        max_relative_difference=float(np.abs(u_cold - u_warm).max() / scale))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--spacing', type=float, required=True)
    parser.add_argument('--masks', required=True, help='4.8mm の採用マスク(t2-default.npz)がある所')
    parser.add_argument('--solver', default='cg')
    parser.add_argument('--out')
    parser.add_argument('--chain', type=int, default=5)
    parser.add_argument('--min-free-gb', type=float, default=2.5)
    args = parser.parse_args()

    record = dict(spacing=args.spacing, solver=args.solver, penal=PENAL, steps=[], chain=[])
    session = Session(None, 0.2, models_dir=MODELS_DIR)
    session.open_document('hanger_bar_attachment.json')
    bundle = optimize_run.build_conditions(session.document)
    started = time.perf_counter()
    space = design_space.derive(bundle.conditions, spacing=args.spacing)
    record['derive_seconds'] = time.perf_counter() - started
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    options = optimize_run.OptimizeOptions(target_safety_factor=2.0)
    conditions = optimize_run._build_search_conditions(session.document, space, bundle, options,
                                                       table=table, mapping=mapping)
    record.update(grid_shape=list(space.grid.shape), grid_cells=int(np.prod(space.grid.shape)),
                  design_cells=int(space.mask.sum()), mapping=mapping['status'],
                  actual_spacing=float(space.grid.spacing), free_gb_at_start=free_gb())
    print(json.dumps({k: v for k, v in record.items() if k not in ('steps', 'chain')},
                     ensure_ascii=False), flush=True)

    coarse = np.load(args.masks + '/t2-default.npz')
    anchor_ids = binary_search._anchor_node_ids(conditions)
    moved = transfer(coarse['best'], coarse['origin'], float(coarse['spacing']), space.grid) & space.mask
    seed = moved | support_cells(anchor_ids, space.grid.shape, space.mask)
    record.update(transferred_cells=int(moved.sum()), patched_cells=int(seed.sum()))
    survivors, island = binary_search.resolve_islands(seed, conditions)
    if not survivors:
        print('写した形が島除去で消えた', file=sys.stderr)
        return 1
    mask = np.ascontiguousarray(survivors[0])
    _, fixed, load = acceptance.condition_check(mask, conditions.table, conditions.fixed_faces,
                                                conditions.load_faces, conditions.forces)
    record.update(seed_cells=int(mask.sum()), seed_components=island['components'],
                  fixed_dofs=int(np.size(fixed)), loaded_dofs=int(np.count_nonzero(load)))
    print('初期形状 %d セル (4.8mm の採用 %d を写した)' % (mask.sum(), coarse['best'].sum()), flush=True)

    model = build_model(mask, fixed, load)
    density = mask.astype(float)
    u_seed, seed_stats = solve_once(model, density, args.solver, None)
    record['seed_solve'] = seed_stats
    print('初期形状の求解: %d反復 %.1f秒' % (seed_stats['iterations'], seed_stats['seconds']), flush=True)
    _, energy = model.compliance(density, PENAL, u_seed)
    order = binary_search.ranked_cells(mask, np.where(mask, energy, 0.0))

    # 削減幅ごとに、同じ初期形状から1回削って cold と warm を比べる。
    # 1セルは段階4後半の実態(patience が効く場面)、20% は step_ratio の上限。
    cells = int(mask.sum())
    plans = [('1セル', 1), ('1%', max(1, cells // 100)), ('5%', max(1, cells // 20)),
             ('20%', max(1, cells // 5))]
    for label, count in plans:
        if free_gb() < args.min_free_gb:
            record['aborted'] = '空きメモリ %.2fGB' % free_gb()
            break
        candidate, _ = binary_search.peel(mask, order, count, anchor_ids)
        removed = int(mask.sum() - candidate.sum())
        if removed == 0:
            continue
        sub = build_model(candidate, fixed, load)
        _, _, result = compare(sub, candidate.astype(float), args.solver, u_seed)
        result.update(label=label, requested=count, removed=removed, cells=int(candidate.sum()))
        record['steps'].append(result)
        print('  %6s 削除%6dセル → cold %6d反復 %7.1f秒 / warm %6d反復 %7.1f秒 (反復比 %.2f, 解の差 %.1e)'
              % (label, removed, result['cold']['iterations'], result['cold']['seconds'],
                 result['warm']['iterations'], result['warm']['seconds'],
                 result['iteration_ratio'], result['max_relative_difference']), flush=True)

    # 1セルずつ連続して削り、そのつど前の解を使う(段階4後半の実際の並び)。
    u_previous, current, current_order = u_seed, mask, order
    for index in range(args.chain):
        if free_gb() < args.min_free_gb:
            record['aborted'] = '空きメモリ %.2fGB (連鎖 %d 回目)' % (free_gb(), index)
            break
        candidate, _ = binary_search.peel(current, current_order, 1, anchor_ids)
        removed = int(current.sum() - candidate.sum())
        if removed == 0:
            break
        sub = build_model(candidate, fixed, load)
        rho = candidate.astype(float)
        _, u_warm, result = compare(sub, rho, args.solver, u_previous)
        result.update(index=index, cells=int(candidate.sum()), removed=removed)
        record['chain'].append(result)
        print('  連鎖%d: cold %6d反復 %7.1f秒 / warm %6d反復 %7.1f秒 (反復比 %.2f)'
              % (index, result['cold']['iterations'], result['cold']['seconds'],
                 result['warm']['iterations'], result['warm']['seconds'],
                 result['iteration_ratio']), flush=True)
        _, energy = sub.compliance(rho, PENAL, u_warm)
        current = np.ascontiguousarray(candidate)
        current_order = binary_search.ranked_cells(current, np.where(current, energy, 0.0))
        u_previous = u_warm

    record['free_gb_at_end'] = free_gb()
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        print('wrote', args.out, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

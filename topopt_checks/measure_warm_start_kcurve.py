"""warm start の CG 反復数の比(warm / cold)を、削るセル数 k の関数として測る(hanger の 2.4mm・2.0mm)。

**6条件の合否(`acceptance.inspect`・幾何の却下)は判定しない**: 段階4の1手の CG 求解までを再現し、合否に関わらず候補を次の親にする。
探索は回さないので `trace.DEFAULT_ROOT` を一時フォルダへ向け、`output/` に実行記録を残さない(4.8mm の採用マスクを作る `coarse` を除く)。
"""
import argparse
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
import _record
import numpy as np
from measure_warm_start import (MODELS_DIR, PENAL, build_model, compare, free_gb, solve_once, support_cells,
                                transfer)

from duocad import optimize_run
from duocad.session import Session
from duocad.topopt import acceptance, binary_search, design_space, trace

K_MAIN = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 18, 20, 24, 28, 32, 36, 40, '1%', '5%']
K_SMALL = [1, 2, 3, 5, 8, 12, 20, 30, 40]
LONG_STEPS = 12
TARGET_CELLS = {2.4: 4000, 2.0: 7000}
SMALL_FRACTION = 0.6
MODEL_NAME = 'hanger_bar_attachment.json'
COARSE_BUDGET = 60


def _setup(spacing_request):
    session = Session(None, 0.2, models_dir=MODELS_DIR)
    session.open_document(MODEL_NAME)
    bundle = optimize_run.build_conditions(session.document)
    limits = optimize_run.resolution_limits(session.document)
    spacing = optimize_run._snap_to_grid(limits['bbox_min'], limits['bbox_max'],
                                         optimize_run._resolve_element_size(limits, spacing_request))
    space = design_space.derive(bundle.conditions, spacing=spacing)
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    if mapping['status'] != 'pass':
        raise SystemExit(f"確定表が不成立: {mapping['status']}")
    options = optimize_run.OptimizeOptions(target_safety_factor=2.0, search_target_factor=1.0)
    conditions = optimize_run._build_search_conditions(session.document, space, bundle, options,
                                                       table=table, mapping=mapping)
    min_width = optimize_run._resolve_min_width(spacing, None)
    return space, conditions, min_width


def make_coarse(masks_dir):
    space, conditions, min_width = _setup(4.8)
    started = time.perf_counter()
    result = binary_search.find_best_feasible(space.mask, conditions, max_evaluations=COARSE_BUDGET,
                                              min_width_cells=min_width['cells'])
    if result.best_feasible is None:
        raise SystemExit(f'4.8mm の探索が採用形状を返さなかった: {result.status}')
    best = result.best_feasible
    masks_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(masks_dir / 't2-default.npz', best=best.mask,
                        origin=np.asarray(space.grid.origin, dtype=float), spacing=float(space.grid.spacing))
    meta = dict(spacing=float(space.grid.spacing), grid_shape=list(space.grid.shape),
                min_width_cells=int(min_width['cells']), evaluations=int(result.evaluations),
                termination=result.termination, adopted_cells=int(best.mask.sum()),
                safety_factor=float(best.safety_factor), seconds=time.perf_counter() - started)
    _record.write(masks_dir / 't2-default.json', meta)
    print(json.dumps(meta, ensure_ascii=False), flush=True)


class Chain:

    def __init__(self, mask, displacement, conditions):
        self.mask = np.ascontiguousarray(mask, dtype=bool)
        self.displacement = displacement
        self.model = model_for(self.mask, conditions)
        self.order = self._rank()

    def _rank(self):
        """探索(`acceptance.safety_check`)と同じ定義: 密度=材料マスク・PENAL=3・材料外は 0。"""
        _, energy = self.model.compliance(self.mask.astype(float), PENAL, self.displacement)
        return binary_search.ranked_cells(self.mask, np.where(self.mask, energy, 0.0))


def model_for(mask, conditions):
    """形ごとに条件節点(拘束・荷重)を取り直す。探索(`acceptance.safety_check`)と同じ。"""
    _, fixed, load = acceptance.condition_check(mask, conditions.table, conditions.fixed_faces,
                                                conditions.load_faces, conditions.forces)
    return build_model(mask, fixed, load)


def cold_parent(mask, conditions):
    displacement, stats = solve_once(model_for(mask, conditions), mask.astype(float), 'cg', None)
    return Chain(mask, displacement, conditions), stats


def one_step(parent, requested, conditions, anchor_ids):
    candidate, peeled = binary_search.peel(parent.mask, parent.order, requested, anchor_ids)
    survivors, island = binary_search.resolve_islands(candidate, conditions)
    if not survivors:
        return None, dict(skipped='no_survivor', requested=requested, peeled=int(peeled.picked))
    child = np.ascontiguousarray(survivors[0])
    removed = int(parent.mask.sum() - child.sum())
    if removed <= 0:
        return None, dict(skipped='nothing_removed', requested=requested, peeled=int(peeled.picked))
    model = model_for(child, conditions)
    u_cold, _, result = compare(model, child.astype(float), 'cg', parent.displacement)
    residual = model.stiffness_apply(child.astype(float), PENAL, parent.displacement) - model.f
    ranks = np.flatnonzero(np.isin(parent.order, peeled.picked_cells))
    result.update(requested=requested, peeled=int(peeled.picked), removed=removed,
                  initial_relative_residual=float(np.linalg.norm(residual[model.free]) / np.linalg.norm(model.f[model.free])),
                  picked_rank_max=int(ranks.max()),
                  parent_cells=int(parent.mask.sum()), cells=int(child.sum()),
                  islands_removed=int(sum(i['cells'] for i in island['removed_islands'])),
                  surviving_components=int(island['surviving_components']))
    return (child, u_cold), result


def shrink(chain, conditions, anchor_ids, target_cells):
    for _ in range(12):
        excess = int(chain.mask.sum()) - target_cells
        if excess <= 0:
            break
        step = min(excess, max(1, int(chain.mask.sum()) // 5))
        candidate, _ = binary_search.peel(chain.mask, chain.order, step, anchor_ids)
        survivors, _ = binary_search.resolve_islands(candidate, conditions)
        if not survivors or int(survivors[0].sum()) >= int(chain.mask.sum()):
            break
        chain, _ = cold_parent(np.ascontiguousarray(survivors[0]), conditions)
    return chain


def build_parents(spacing, masks_dir, state_dir, conditions, space, anchor_ids):
    parents, info = {}, {}
    for name in ('large', 'small'):
        path = state_dir / f'parent-{name}.npz'
        if path.exists():
            saved = np.load(path)
            if saved['mask'].shape != space.grid.shape:
                raise SystemExit(f'{path} の格子 {saved["mask"].shape} が今の格子 {space.grid.shape} と違う。--state を格子ごとに分ける')
            parents[name] = Chain(saved['mask'], saved['u'], conditions)
            info[name] = json.loads(str(saved['info']))
    if len(parents) == 2:
        return parents, info

    coarse = np.load(masks_dir / 't2-default.npz')
    moved = transfer(coarse['best'], coarse['origin'], float(coarse['spacing']), space.grid) & space.mask
    seed = moved | support_cells(anchor_ids, space.grid.shape, space.mask)
    survivors, _ = binary_search.resolve_islands(seed, conditions)
    if not survivors:
        raise SystemExit('写した形が島除去で消えた')
    chain, seed_stats = cold_parent(np.ascontiguousarray(survivors[0]), conditions)
    seed_cells = int(chain.mask.sum())
    target = TARGET_CELLS[spacing]
    large = chain if seed_cells <= target * 1.05 else shrink(chain, conditions, anchor_ids, target)
    small = shrink(large, conditions, anchor_ids, int(SMALL_FRACTION * large.mask.sum()))
    shared = dict(coarse_adopted_cells=int(coarse['best'].sum()), transferred_cells=int(moved.sum()),
                  seed_cells=seed_cells, seed_cold_iterations=seed_stats['iterations'])
    for name, built in (('large', large), ('small', small)):
        info[name] = dict(shared, cells=int(built.mask.sum()))
        np.savez_compressed(state_dir / f'parent-{name}.npz', mask=built.mask, u=built.displacement,
                            info=json.dumps(info[name]))
        parents[name] = built
    return parents, info


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    coarse = sub.add_parser('coarse')
    coarse.add_argument('--masks', type=Path, required=True)
    run = sub.add_parser('run')
    run.add_argument('--spacing', type=float, required=True, choices=sorted(TARGET_CELLS))
    run.add_argument('--stage', type=int, required=True, choices=(1, 3, 5, 6),
                     help='1=k ごとの1手目 / 3=連鎖の2・3手目 / 5=小さい親で k を絞って連鎖2手 / 6=各親から k=1 を12手')
    run.add_argument('--masks', type=Path, required=True)
    run.add_argument('--out', type=Path, required=True)
    run.add_argument('--state', type=Path, required=True)
    run.add_argument('--head', default='unknown', help='測定に使ったコードの HEAD のハッシュ(記録用)')
    run.add_argument('--min-free-gb', type=float, default=2.5)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix='topopt-trace-') as trace_root, \
            patch.object(trace, 'DEFAULT_ROOT', Path(trace_root)):
        if args.command == 'coarse':
            make_coarse(args.masks)
            return 0
        return run_stage(args)


def run_stage(args):
    args.state.mkdir(parents=True, exist_ok=True)
    space, conditions, _ = _setup(args.spacing)
    anchor_ids = binary_search._anchor_node_ids(conditions)
    parents, info = build_parents(args.spacing, args.masks, args.state, conditions, space, anchor_ids)

    if args.out.exists():
        record = json.loads(args.out.read_text(encoding='utf-8'))
        if (record['spacing_requested'], record['grid_shape']) != (args.spacing, list(space.grid.shape)) or (
                args.head != 'unknown' and record['head'] != args.head):
            raise SystemExit(f'{args.out} は別の条件(spacing={record["spacing_requested"]}・格子 {record["grid_shape"]}・head={record["head"]})の記録')
    else:
        record = dict(spacing_requested=args.spacing, actual_spacing=float(space.grid.spacing), head=args.head,
                      solver='cg', penal=PENAL, min_width_cells=1, grid_shape=list(space.grid.shape),
                      design_cells=int(space.mask.sum()), points=[])
    record['parents'] = info
    done = {(p['parent'], p['label'], p['step']) for p in record['points']}

    if args.stage == 1:
        plan = [('large', label, (1,)) for label in K_MAIN]
    elif args.stage == 3:
        plan = [('large', label, (2, 3)) for label in K_MAIN]
    elif args.stage == 5:
        plan = [('small', label, (1, 2)) for label in K_SMALL]
    else:
        plan = [(name, 'L1', tuple(range(1, LONG_STEPS + 1))) for name in ('large', 'small')]

    incomplete = False
    for parent_name, label, steps in plan:
        chain_path = args.state / f'chain-{parent_name}-{str(label).replace("%", "pct")}.npz'
        for step in steps:
            if (parent_name, label, step) in done:
                continue
            if step == 1:
                chain = parents[parent_name]
            elif chain_path.exists() and int(np.load(chain_path)['step']) == step - 1:
                saved = np.load(chain_path)
                chain = Chain(saved['mask'], saved['u'], conditions)
            else:
                previous = [p for p in record['points'] if (p['parent'], p['label'], p['step']) == (parent_name, label, step - 1)]
                if not previous or not previous[0].get('skipped'):
                    print(f'  {parent_name} k={label} step{step}: 親の状態 {chain_path.name} が無い', flush=True)
                    incomplete = True
                break
            if free_gb() < args.min_free_gb:
                record['aborted'] = f'空きメモリ {free_gb():.2f}GB({parent_name} k={label} step {step} の前)'
                _record.write(args.out, record)
                print(record['aborted'], flush=True)
                return 4
            if label == 'L1':
                requested = 1
            elif step == 1:
                requested = int(label) if isinstance(label, int) else max(1, int(chain.mask.sum()) * int(label[:-1]) // 100)
            else:
                requested = next((p['requested_k'] for p in record['points']
                                  if (p['parent'], p['label'], p['step']) == (parent_name, label, 1)), None)
                if requested is None:
                    print(f'  {parent_name} k={label} step{step}: 1手目の記録が --out に無い', flush=True)
                    incomplete = True
                    break
            produced, result = one_step(chain, requested, conditions, anchor_ids)
            result.update(parent=parent_name, label=label, step=step, requested_k=requested, free_gb=free_gb())
            record['points'].append(result)
            _record.write(args.out, record)
            if produced is None:
                print(f'  {parent_name} k={label} step{step}: 飛ばした({result["skipped"]})', flush=True)
                break
            child, u_cold = produced
            np.savez_compressed(chain_path, mask=child, u=u_cold, step=step)
            print(f'  {parent_name} k={label:>3} step{step} 親{result["parent_cells"]:5d}→{result["cells"]:5d} '
                  f'抜{result["removed"]:3d} cold {result["cold"]["iterations"]:6d} warm {result["warm"]["iterations"]:6d} '
                  f'比 {result["iteration_ratio"]:.3f} 差 {result["max_relative_difference"]:.1e} '
                  f'({result["cold"]["seconds"]:.1f}s/{result["warm"]["seconds"]:.1f}s)', flush=True)
    print(f'stage {args.stage} {"未完了(親の状態が欠けた点がある)" if incomplete else "完了"} spacing={args.spacing}', flush=True)
    return 5 if incomplete else 0


if __name__ == '__main__':
    raise SystemExit(main())

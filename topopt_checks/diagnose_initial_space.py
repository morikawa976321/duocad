"""導出の各マスクでアンカー被覆を照合する。探索・FEM・メッシュ化は実行しない。"""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
import numpy as np

from duocad import optimize_run
from duocad.session import Session
from duocad.topopt import acceptance, binary_search, design_space


def diagnose(document, spacing):
    bundle = optimize_run.build_conditions(document)
    stages = {}

    def capture(name, **values):
        if name == 'derive-masks':
            stages.update(values)

    with patch.object(design_space.trace, 'snapshot', capture):
        space = design_space.derive(bundle.conditions, spacing=spacing)
    faces = {}
    for face in space.fixed_faces + space.load_faces:
        ids = space.table.node_ids([face])
        faces[str(face)] = dict(nodes=int(ids.size), lost_by_stage={
            name: int((~acceptance.active_nodes(stages[name]).ravel()[ids]).sum())
            for name in ('frame', 'hull', 'minus_obstacle', 'connected')
        })
    conditions, _, _ = acceptance.condition_check(
        space.mask, space.table, space.fixed_faces, space.load_faces, bundle.forces)
    search_conditions = optimize_run._build_search_conditions(
        document, space, bundle, optimize_run.OptimizeOptions(target_safety_factor=2.0))
    survivors, island_diagnostics = binary_search.resolve_islands(space.mask, search_conditions)
    indices = np.argwhere(space.mask)
    candidate_counts = {
        str(size): int(np.unique(indices // size, axis=0).shape[0])
        for size in binary_search.block_sizes(space.grid.shape)
    }
    return dict(requested_spacing=spacing, grid=space.grid.as_dict(), cells=space.element_count,
                pruned_cells=int((stages['minus_obstacle'] & ~stages['connected']).sum()),
                final_equals_minus_obstacle=bool(np.array_equal(space.mask, stages['minus_obstacle'])),
                faces=faces, conditions=conditions, boundary=acceptance.boundary_check(space.mask),
                connectivity=acceptance.connectivity_check(space.mask),
                initial_survivors=len(survivors), island_diagnostics=island_diagnostics,
                candidate_memory_estimate=dict(
                    note='Full initial material has no addable cells. Counts are per-size delete proposals before deduplication; no candidate masks allocated.',
                    mask_bytes=space.mask.nbytes, delete_counts=candidate_counts,
                    payload_bytes=sum(candidate_counts.values()) * space.mask.nbytes))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--spacing', type=float, nargs='+', default=[4.8, 2.4])
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    session = Session(None, .2, models_dir=args.model.resolve().parent)
    session.open_document(args.model.name)
    rows = []
    for spacing in args.spacing:
        rows.append(diagnose(session.document, spacing))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

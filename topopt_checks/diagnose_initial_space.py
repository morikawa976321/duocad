"""導出の各マスクで、幾何の表と探索用の確定表のアンカー被覆・荷重保存を照合する。探索・FEM・メッシュ化は実行しない。

    .\\.venv\\Scripts\\python.exe topopt_checks\\diagnose_initial_space.py --model models\\hanger_bar_attachment.json --spacing 4.8 2.4 --out output\\initial_space_diagnosis\\stages.json

段階ごとのマスク(外枠・凸包・除外後・接続後)に対して、導出が接続の確認に使った幾何の表
(`DesignSpace.table`。材料を見ない)と、探索へ渡す確定表(`optimize_run._map_boundary_conditions`。
材料に接する節点だけへ対応付ける)の両方で、条件面ごとの節点数・被覆されない節点数を数える。
確定表については荷重保存(合力・作用位置・モーメント)の検証も書く。
"""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
import _record
import numpy as np

from duocad import optimize_run
from duocad.session import Session
from duocad.topopt import acceptance, binary_search, design_space


def _faces_by_stage(table, faces, stages):
    out = {}
    for face in faces:
        anchor = table[int(face)]
        ids = anchor.node_ids
        out[str(face)] = dict(
            nodes=int(ids.size), unmapped_samples=int(anchor.unmapped_samples),
            lost_by_stage={name: int((~acceptance.active_nodes(stages[name]).ravel()[ids]).sum())
                           for name in ('frame', 'hull', 'minus_obstacle', 'connected')},
        )
    return out


def _compact(mapping):
    out = json.loads(json.dumps(mapping))
    for row in out.get('faces', []):
        row['unmapped_positions'] = row.get('unmapped_positions', [])[:5]
    return out


def diagnose(document, spacing):
    bundle = optimize_run.build_conditions(document)
    stages = {}

    def capture(name, **values):
        if name == 'derive-masks':
            stages.update(values)

    with patch.object(design_space.trace, 'snapshot', capture):
        space = design_space.derive(bundle.conditions, spacing=spacing)
    faces = space.fixed_faces + space.load_faces
    geometry_faces = _faces_by_stage(space.table, faces, stages)
    geometry_conditions, _, _ = acceptance.condition_check(
        space.mask, space.table, space.fixed_faces, space.load_faces, bundle.forces)

    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    mapped_faces = _faces_by_stage(table, faces, stages)
    mapped_conditions, _, _ = acceptance.condition_check(
        space.mask, table, space.fixed_faces, space.load_faces, bundle.forces)
    options = optimize_run.OptimizeOptions(target_safety_factor=2.0, search_target_factor=1.0)
    search_conditions = optimize_run._build_search_conditions(document, space, bundle, options,
                                                              table=table, mapping=mapping)
    entry = binary_search.uncovered_condition_nodes(space.mask, search_conditions)
    survivors, island_diagnostics = binary_search.resolve_islands(space.mask, search_conditions)
    geometry_conditions_for_search = optimize_run._build_search_conditions(
        document, space, bundle, options, table=space.table, mapping=None)
    geometry_entry = binary_search.uncovered_condition_nodes(space.mask, geometry_conditions_for_search)
    geometry_survivors, _ = binary_search.resolve_islands(space.mask, geometry_conditions_for_search)

    return dict(
        requested_spacing=spacing, grid=space.grid.as_dict(), cells=space.element_count,
        mask_hash=space.mask_hash,
        pruned_cells=int((stages['minus_obstacle'] & ~stages['connected']).sum()),
        final_equals_minus_obstacle=bool(np.array_equal(space.mask, stages['minus_obstacle'])),
        boundary=acceptance.boundary_check(space.mask), connectivity=acceptance.connectivity_check(space.mask),
        geometry_table=dict(stats={k: v for k, v in space.table.stats.items() if k != 'empty_faces'},
                            faces=geometry_faces, conditions=geometry_conditions,
                            entry_uncovered=geometry_entry, initial_survivors=len(geometry_survivors)),
        search_table=dict(stats={k: v for k, v in table.stats.items() if k != 'empty_faces'},
                          faces=mapped_faces, conditions=mapped_conditions, mapping=_compact(mapping),
                          entry_uncovered=entry, initial_survivors=len(survivors),
                          island_diagnostics=island_diagnostics,
                          identifier=binary_search.evaluation_identifier(search_conditions)),
    )


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
        _record.write(args.out, rows)
        row = rows[-1]
        summary = dict(spacing=spacing, cells=row['cells'],
                       geometry=[(f, v['nodes'], v['lost_by_stage']['connected']) for f, v in row['geometry_table']['faces'].items()],
                       search=[(f, v['nodes'], v['lost_by_stage']['connected'], v['unmapped_samples']) for f, v in row['search_table']['faces'].items()],
                       mapping=row['search_table']['mapping']['status'],
                       survivors=(row['geometry_table']['initial_survivors'], row['search_table']['initial_survivors']))
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

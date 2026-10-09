"""Issue #122: 最小幅の芯の隣接を6連結のままにした探索と、既定(m ≥ 4 で26連結)の探索を比べる(材料量・安全率・時間・停止理由・
静的診断と、完成形の検査 `final_check.inspect`)。

.venv/Scripts/python.exe topopt_checks/compare_core_adjacency_122.py path --size 1.5
.venv/Scripts/python.exe topopt_checks/compare_core_adjacency_122.py path --size 1.2
.venv/Scripts/python.exe topopt_checks/compare_core_adjacency_122.py hanger [--only 6|26]

「6」は `min_width.core_structure` を常に6近傍に差し替えて、#122 より前の探索を再現する(S(m)・くびれ判定・静的診断が同じ
`CoreConnectivity.structure` を使うので、差し替えはこの1か所で足りる)。path は最小幅6mm(要素1.5mm で m=4、1.2mm で m=5)、
hanger は段階09の S2(200N、要素2.0mm、最小幅10mm、m=6)と同じ条件で全材料から探索する(1本30分〜2時間)。
結果は `output/conn_122/<題材>.json`。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'topopt_checks'))
sys.path.insert(0, str(REPO / 'topopt_checks' / 'idle_validation'))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from duocad import optimize_run  # noqa: E402
from duocad.topopt import binary_search, final_check, mesh, min_width, thin_voids  # noqa: E402
from duocad.topopt.acceptance import NEIGHBOURHOOD_STRUCTURE  # noqa: E402
from duocad.topopt.finalize import finalize  # noqa: E402

OUT = REPO / 'output' / 'conn_122'


def _setup(subject, size):
    if subject == 'path':
        import measure_path_local_optimum as mp
        from _documents import open_session
        doc = open_session(mp.MODEL).document
        space, cond, cells = mp._search_conditions(doc, size)
        bundle = optimize_run.build_conditions(doc)
        return space, cond, cells, doc, bundle, mp.MIN_WIDTH_MM
    import final_shapes_09 as fs
    ld = fs.load_shape('S2_hanger200_step20')
    ctx = ld['ctx']
    return ctx.space, ctx.cond, ctx.min_width_cells, ld['doc'], ld['bundle'], 10.0


def _final(mask0, space, cond, doc, bundle, width_mm):
    grid = space.grid
    resolved = optimize_run._resolve_min_width(grid.spacing, width_mm)
    mask, fill = thin_voids.fill_tubes(mask0, space.mask)
    surface = mesh.build_surface_from_mask(mask)
    smooth = grid.origin + surface.verts_smooth * grid.spacing
    v, f = finalize(smooth, surface.faces, [doc.get_body(b).shape for b in bundle.keep_body_ids],
                    [doc.get_body(b).shape for b in bundle.exclude_body_ids])
    r = final_check.inspect(v, f, grid=grid, conditions=cond, mask=mask, min_width=resolved, target_safety_factor=2.0,
                            keep_cells=cond.keep_cells, anchor_soup=space.soup)
    nk = r['neck']
    ev = nk['events']
    return dict(final_safety_factor=r['safety_factor'], conditions=r['conditions']['status'], neck_status=nk['status'],
                suspects=[e for e in ev if e['verdict'] == 'suspect'],
                undetermined=sum(e['verdict'] == 'undetermined' for e in ev), micro=nk['micro_pieces'],
                end_probe_mm=nk['end_probe_mm'])


def _run(space, cond, cells, *, adj, name, doc, bundle, width_mm):
    stats = []
    real_peel = binary_search.peel

    def peel(*args, **kwargs):
        after, s = real_peel(*args, **kwargs)
        stats.append(s)
        return after, s

    patches = [mock.patch.object(binary_search, 'peel', peel)]
    if adj == 6:
        patches.append(mock.patch.object(min_width, 'core_structure', lambda m: NEIGHBOURHOOD_STRUCTURE))
    started = time.perf_counter()
    for p in patches:
        p.start()
    try:
        result = binary_search.find_best_feasible(space.mask, cond, max_evaluations=1000, min_width_cells=cells)
    finally:
        for p in patches:
            p.stop()
    best = result.best_feasible
    reduction = (result.diagnostics or {}).get('reduction', {})
    row = dict(adjacency=adj, status=result.status, termination=result.termination, evaluations=result.evaluations,
               seconds=time.perf_counter() - started, stop_reason=reduction.get('stop_reason'),
               peel_seconds=float(sum(s.seconds for s in stats)),
               skipped_neck=int(sum(s.skipped_neck for s in stats)),
               skipped_core=int(sum(s.skipped_core for s in stats)),
               static_necks=reduction.get('static_necks'))
    if best is not None:
        row.update(material_mm3=float(best.material_mm3), cells=int(best.mask.sum()), safety_factor=float(best.safety_factor))
        np.savez_compressed(OUT / f'{name}_{adj}.npz', mask=best.mask)
        row['final'] = _final(best.mask, space, cond, doc, bundle, width_mm)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('subject', choices=['path', 'hanger'])
    parser.add_argument('--size', type=float, default=1.5)
    parser.add_argument('--only', choices=['6', '26'])
    args = parser.parse_args()
    space, cond, cells, doc, bundle, width_mm = _setup(args.subject, args.size)
    name = f'path_{args.size}' if args.subject == 'path' else 'hanger'
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f'{name}.json'
    record = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(
        subject=args.subject, size_mm=float(space.grid.spacing), min_width_cells=int(cells),
        design_elements=int(space.element_count), rows=[])
    record['code_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True, cwd=REPO).strip()
    print(name, 'm =', cells, 'elements =', int(space.element_count), flush=True)
    for adj in (6, 26):
        if args.only and int(args.only) != adj:
            continue
        row = _run(space, cond, cells, adj=adj, name=name, doc=doc, bundle=bundle, width_mm=width_mm)
        record['rows'] = [r for r in record['rows'] if r['adjacency'] != adj] + [row]
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=float), encoding='utf-8')
        print(json.dumps({k: v for k, v in row.items() if k not in ('static_necks', 'final')}, ensure_ascii=False, default=float),
              flush=True)
        print('  final:', json.dumps({k: v for k, v in row.get('final', {}).items() if k != 'suspects'}, default=float),
              'suspects', len(row.get('final', {}).get('suspects', [])), flush=True)


if __name__ == '__main__':
    main()

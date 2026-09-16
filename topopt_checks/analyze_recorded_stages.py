"""保存済み最適化の時間配分、密度の連結性、採用表面の変化を調べる。再最適化しない。"""
import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
from scipy import ndimage

from duocad.topopt.trace import load_snapshot
from duocad.topopt.mesh import mesh_volume
from check_final_shape import topology, make_material


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    run = args.run
    out = run / 'analysis'
    out.mkdir(exist_ok=True)
    events = [json.loads(s) for s in (run / 'events.jsonl').read_text().splitlines()]
    snapshots = [e for e in events if e['kind'] == 'snapshot']
    def records(name, phase=None):
        return [e for e in snapshots if e['name'] == name and (phase is None or e['phase'] == phase)]
    def raw(event):
        return json.loads((run / event['file']).read_text(encoding='utf-8'))
    def load(event):
        return load_snapshot(run / event['file'])
    begins = {e['child']: e for e in events if e['kind'] == 'begin'}
    ends = {e['phase']: e for e in events if e['kind'] in ('end', 'error')}
    result = raw(records('result')[-1])
    report = dict(run=run.name, result=result, phases=[], surfaces=[], smoothing=[])
    def save():
        (out / 'stages.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for phase, begin in begins.items():
        if begin['name'] not in ('reference', 'safety-evaluation', 'simp', 'surface', 'stress'):
            continue
        row = dict(id=phase, parent=begin['phase'], name=begin['name'],
                   seconds=ends.get(phase, begin)['elapsed_seconds'] - begin['elapsed_seconds'])
        if begin['name'] == 'simp':
            output = records('simp-output', phase)
            if output:
                data = raw(output[-1])
                history = data['history']
                row.update(iterations=len(history), history=history)
            else:
                row.update(failed=True)
        report['phases'].append(row)
    spacing = result['grid']['spacing']
    for event in records('surface-fields'):
        phase = event['phase']
        inp = load(records('surface-input', phase)[0])
        fields = load(event)
        keep, zero = inp['keep_mask'], inp['zero_mask']
        masked, clean = fields['masked'], fields['rho_clean']
        labels, count = ndimage.label(masked > inp['level'])
        sizes = np.bincount(labels.ravel())[1:]
        row = dict(phase=phase, components_before=int(count),
                   component_sizes=sorted(sizes.tolist(), reverse=True),
                   components_after=int(ndimage.label(clean > inp['level'])[1]),
                   keep_cells=int(keep.sum()), zero_cells=int(zero.sum()),
                   overlap_cells=int((keep & zero).sum()),
                   keep_below_one_before=int((inp['rho'][keep] != 1).sum()),
                   keep_below_one_after=int((clean[keep] != 1).sum()),
                   excluded_nonzero_before=int((inp['rho'][zero] != 0).sum()),
                   excluded_nonzero_after=int((clean[zero] != 0).sum()),
                   removed_above_threshold=int(((masked > inp['level']) & (clean <= inp['level'])).sum()),
                   density_integral_before_mm3=float(masked.sum()*spacing**3),
                   density_integral_after_mm3=float(clean.sum()*spacing**3))
        report['surfaces'].append(row)
        print(json.dumps(row), flush=True)
        save()
    world = load(records('final-mesh-world')[-1])
    final_faces = world['faces']
    # 採用形状は最後に評価した候補とは限らないので、配列一致で対応する工程を選ぶ。
    selected = []
    for event in records('marching-cubes'):
        data = load(event)
        candidate_world = np.asarray(world['grid']['origin']) + spacing*data['vertices']
        if np.array_equal(data['faces'], final_faces) and np.array_equal(candidate_world, world['raw_vertices']):
            selected.append(event['phase'])
    if len(selected) != 1:
        raise RuntimeError(f'採用表面の対応が一意でない: {selected}')
    phase = selected[0]
    report['selected_surface_phase'] = phase
    stages = [('raw', world['raw_vertices'])]
    for event in records('taubin-mu', phase):
        data = load(event)
        if data['iteration'] in (1, 5, 10, 20, 30):
            stages.append((f"mu-{data['iteration']}", np.asarray(world['grid']['origin'])+spacing*data['vertices']))
    for name, vertices in stages:
        stats, components = topology(vertices, final_faces)
        try:
            _, diagnostics = make_material(vertices, final_faces, components)
        except Exception as error:
            diagnostics = dict(error=str(error))
        row = dict(stage=name, topology=stats, diagnostics=diagnostics,
                   signed_boundary_volume_mm3=mesh_volume(vertices, final_faces),
                   bbox_min=vertices.min(0).tolist(), bbox_max=vertices.max(0).tolist(),
                   max_displacement_mm=float(np.linalg.norm(vertices-world['raw_vertices'],axis=1).max()))
        report['smoothing'].append(row)
        save()
        print(json.dumps(row), flush=True)


if __name__ == '__main__':
    main()

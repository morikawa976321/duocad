r"""探索の実行記録から、各候補が親から消したセル(一口・巻き添え・島)の中身と、候補の安全率の落ち方を並べる。
崖(数十セル消しただけで安全率が大きく落ちる候補)が、消したセルの中に親でよく働いていたセルを含むせいかを見る。
usage: .venv\Scripts\python.exe topopt_checks\speed_20261011\analyze_cliffs.py <実行フォルダ> <出力 .json>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\tm976\mf\duocad")
import numpy as np
from scipy import ndimage

from duocad.topopt import fem, trace
from duocad.topopt.binary_search import ENERGY_FILTER_SIZE

run = trace.RunArchive(sys.argv[1])
inputs = run.snapshots('stress-input')
outputs = run.snapshots('stress-output')
displacements = run.snapshots('stress-displacement')
progress = [run.raw(e) for e in run.snapshots('progress')]
progress = [p for p in progress if p.get('outcome') in ('initial', 'accepted', 'rejected')]
assert len(inputs) == len(progress), (len(inputs), len(progress))


def smoothed(mask, energy):
    total = ndimage.uniform_filter(np.where(mask, energy, 0.0), size=ENERGY_FILTER_SIZE, mode='constant')
    count = ndimage.uniform_filter(mask.astype(float), size=ENERGY_FILTER_SIZE, mode='constant')
    out = np.zeros_like(energy)
    out[mask] = total[mask] / count[mask]
    return out


rows = []
parent = None
for index, (ev_in, ev_out, ev_u, prog) in enumerate(zip(inputs, outputs, displacements, progress)):
    data = run.load(ev_in)
    mask = data['rho'] > 0.5
    md = data['model']
    shape = (int(md['nelx']), int(md['nely']), int(md['nelz']))
    vm = run.load(ev_out)['field']['von_mises']
    u = run.load(ev_u)['displacement']
    if parent is not None:
        p = parent
        removed = p['mask'] & ~mask
        cells = np.flatnonzero(p['mask'])
        rank_energy = np.empty(p['mask'].size)
        order = np.argsort(p['energy'].ravel()[cells], kind='stable')
        rank_energy[cells[order]] = np.arange(cells.size) / max(cells.size - 1, 1)
        rank_smooth = np.empty(p['mask'].size)
        order = np.argsort(p['smooth'].ravel()[cells], kind='stable')
        rank_smooth[cells[order]] = np.arange(cells.size) / max(cells.size - 1, 1)
        rank_vm = np.empty(p['mask'].size)
        order = np.argsort(p['vm'].ravel()[cells], kind='stable')
        rank_vm[cells[order]] = np.arange(cells.size) / max(cells.size - 1, 1)
        r = np.flatnonzero(removed)
        rows.append(dict(
            evaluation=prog['evaluation'], outcome=prog['outcome'], parent_cells=int(p['mask'].sum()),
            removed=int(r.size), parent_sf=p['sf'], candidate_sf=prog.get('candidate_safety_factor'),
            energy_share=float(p['energy'].ravel()[r].sum() / p['energy'].ravel()[cells].sum()),
            max_energy_rank=float(rank_energy[r].max()), min_smooth_rank=float(rank_smooth[r].min()),
            max_smooth_rank=float(rank_smooth[r].max()), mean_energy_rank=float(rank_energy[r].mean()),
            max_vm_rank=float(rank_vm[r].max()),
            max_vm_over_p99=float(p['vm'].ravel()[r].max() / np.percentile(p['vm'].ravel()[cells], 99))))
    if prog['outcome'] in ('initial', 'accepted'):
        model = fem.Model(*shape, fixed_dofs=md['fixed'], load=md['f'],
                          active_mask=np.isin(np.arange(int(np.prod(shape))), md['active']).reshape(shape))
        cells = np.flatnonzero(mask)
        energy = np.zeros(mask.shape)
        energy.ravel()[cells] = model.element_energy(u, cells)
        parent = dict(mask=mask, energy=energy, smooth=smoothed(mask, energy), vm=np.where(mask, vm, 0.0),
                      sf=prog['safety_factor'])
Path(sys.argv[2]).write_text(json.dumps(rows, indent=0), encoding='utf-8')
print(len(rows))

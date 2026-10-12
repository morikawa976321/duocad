r"""安全率で落ちた候補が消した「親の代表応力 Q_V 以上のセル」(熱いセル)を親ごとに集め、同じ親の失敗どうしで熱いセルを
共有しているか、合格した候補が熱いセルに触れていたかを数える(選択肢 B: 失敗の一口の熱いセルを覚えて飛ばす、の上限)。
usage: .venv\Scripts\python.exe topopt_checks\speed_20261011\analyze_hot_sharing.py <実行フォルダ>
"""
import sys

sys.path.insert(0, r"C:\Users\tm976\mf\duocad")
import numpy as np

from duocad.topopt import trace

run = trace.RunArchive(sys.argv[1])
inputs = run.snapshots('stress-input')
outputs = run.snapshots('stress-output')
progress = [run.raw(e) for e in run.snapshots('progress')]
progress = [p for p in progress if p.get('outcome') in ('initial', 'accepted', 'rejected')]

parent = None
learned: set = set()        # 探索の初めから覚えた熱いセル(B は採用をまたいで持つ)
per_parent: set = set()
counts = dict(failures=0, failures_already_covered=0, failures_covered_in_parent=0, failures_without_hot=0,
              accepted=0, accepted_touching_learned=0)
for ev_in, ev_out, prog in zip(inputs, outputs, progress):
    mask = run.load(ev_in)['rho'] > 0.5
    out = run.load(ev_out)
    if parent is not None and prog['outcome'] == 'rejected':
        removed = np.flatnonzero(parent['mask'] & ~mask)
        hot = set(removed[parent['vm'].ravel()[removed] >= parent['q']].tolist())
        counts['failures'] += 1
        if not hot:
            counts['failures_without_hot'] += 1
        if hot & learned:
            counts['failures_already_covered'] += 1
        if hot & per_parent:
            counts['failures_covered_in_parent'] += 1
        learned |= hot
        per_parent |= hot
    if parent is not None and prog['outcome'] == 'accepted':
        removed = set(np.flatnonzero(parent['mask'] & ~mask).tolist())
        counts['accepted'] += 1
        counts['accepted_touching_learned'] += bool(removed & learned)
    if prog['outcome'] in ('initial', 'accepted'):
        parent = dict(mask=mask, vm=np.where(mask, out['field']['von_mises'], 0.0), q=float(out['representative_mpa']))
        per_parent = set()
print(counts, 'learned cells', len(learned))

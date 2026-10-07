"""Issue #120: 探索の1手のくびれ判定(`neck.NeckJudge`)の有無で、探索結果(材料量・安全率・時間・停止理由・静的診断)を比べる。

.venv/Scripts/python.exe topopt_checks/compare_neck_judge_120.py dilution
.venv/Scripts/python.exe topopt_checks/compare_neck_judge_120.py path --size 2.0
.venv/Scripts/python.exe topopt_checks/compare_neck_judge_120.py hanger

「なし」は `NeckJudge.creates_neck` を常に偽に差し替えて、判定を入れる前の探索を再現する。題材ごとに「なし → あり」の順に回し、
1本終わるごとに `output/neck_judge_120/<題材>.json` を書き直す。hanger は段階09の S2(200N、要素2.0mm、最小幅10mm)と同じ条件で、
全材料から探索をやり直す(1本数時間)。
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'topopt_checks'))
sys.path.insert(0, str(REPO / 'topopt_checks' / 'idle_validation'))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from duocad.topopt import binary_search, neck  # noqa: E402

OUT = REPO / 'output' / 'neck_judge_120'
SHAPE_IDS = {'dilution': 'S1c_dil25_step20', 'hanger': 'S2_hanger200_step20'}


def _setup(subject, size):
    if subject == 'path':
        import measure_path_local_optimum as mp
        from _documents import open_session
        space, cond, cells = mp._search_conditions(open_session(mp.MODEL).document, size)
        return space, cond, cells
    import final_shapes_09 as fs
    ctx = fs.load_shape(SHAPE_IDS[subject])['ctx']
    return ctx.space, ctx.cond, ctx.min_width_cells


def _run(space, cond, cells, *, rule, name):
    stats = []
    real_peel = binary_search.peel

    def peel(*args, **kwargs):
        after, s = real_peel(*args, **kwargs)
        stats.append(s)
        return after, s

    patches = [mock.patch.object(binary_search, 'peel', peel)]
    if not rule:
        patches.append(mock.patch.object(neck.NeckJudge, 'creates_neck', lambda self, mask, removed: False))
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
    row = dict(rule=rule, status=result.status, termination=result.termination, evaluations=result.evaluations,
               seconds=time.perf_counter() - started, stop_reason=reduction.get('stop_reason'),
               peel_seconds=float(sum(s.seconds for s in stats)),
               skipped_neck=int(sum(s.skipped_neck for s in stats)),
               skipped_core=int(sum(s.skipped_core for s in stats)),
               neck_rejected_6_only=int(sum(s.neck_rejected_6_only for s in stats)),
               neck_full_grid=int(sum(s.neck_full_grid for s in stats)),
               static_necks=reduction.get('static_necks'))
    if best is not None:
        row.update(material_mm3=float(best.material_mm3), cells=int(best.mask.sum()), safety_factor=float(best.safety_factor))
        OUT.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(OUT / f'{name}_{"rule" if rule else "none"}.npz', mask=best.mask)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('subject', choices=['path', 'dilution', 'hanger'])
    parser.add_argument('--size', type=float, default=2.5)
    parser.add_argument('--only', choices=['none', 'rule'])
    args = parser.parse_args()
    space, cond, cells = _setup(args.subject, args.size)
    name = f'{args.subject}_{args.size}' if args.subject == 'path' else args.subject
    path = OUT / f'{name}.json'
    record = json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(
        subject=args.subject, size_mm=args.size, min_width_cells=int(cells), design_elements=int(space.element_count), rows=[])
    record['code_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True, cwd=REPO).strip()
    OUT.mkdir(parents=True, exist_ok=True)
    for rule in (False, True):
        if args.only and args.only != ('rule' if rule else 'none'):
            continue
        row = _run(space, cond, cells, rule=rule, name=name)
        record['rows'] = [r for r in record['rows'] if r['rule'] != rule] + [row]
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in row.items() if k != 'static_necks'}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

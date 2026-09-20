"""最小幅つきの段階3(初期合格探索)を、除外された列を持つ2セル厚の板の族で測る(`docs/TOPOPT_THICKNESS.md` 節L)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_stage3_min_width.py --result C:\\tmp\\new.json
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_stage3_min_width.py --root C:\\tmp\\old-checkout --result C:\\tmp\\old.json
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_stage3_min_width.py --compare C:\\tmp\\old.json C:\\tmp\\new.json

題材は `nx × ny × 2` の板(固定面 = x 最小・荷重面 = x 最大)の1列 (i, j) が除外領域に掛かる。全材料は除外で不合格になるので、
探索は列を含むブロックを削らなければならない。m=2。**判定はしない**(初期合格に届いたか・評価回数・FEMまで進んだ回数・
追加の候補のうち大域判定で落ちた数を書き出すだけ)。`--root` は別の版の作業ツリー(`git archive <commit> duocad` の展開先)を
`sys.path` の先頭に置き、変更前と後を同じ題材・同じ予算で比べる。FEMは小さな直方体なので1ケース 1 秒未満。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (topopt_checks の import 経路を通す)
import _record

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = {'default': dict(max_evaluations=600), 'wide': dict(max_evaluations=6000, frontier_capacity=4096)}
SHAPES = ((8, 7, 2), (6, 7, 2), (7, 7, 2))


def measure(root: str | None) -> dict:
    import numpy as np
    from build123d import Box, Pos

    if root:
        sys.path.insert(0, root)
    sys.path.insert(1, str(REPO_ROOT / 'tests'))
    from duocad.topopt import binary_search
    from test_binary_search import bar_conditions

    rows = {}
    for name, options in CONFIGS.items():
        for shape in SHAPES:
            for i in range(2, shape[0] - 2):
                for j in range(1, shape[1] - 1):
                    hole = Pos(i + .5, j + .5, shape[2] / 2) * Box(.8, .8, shape[2] - .2)
                    conditions = dataclasses.replace(bar_conditions(shape=shape), excludes=[('hole', hole.wrapped)])
                    result = binary_search.find_initial_feasible(np.ones(shape, bool), conditions,
                                                                 min_width_cells=2, **options)
                    adds = [t for t in result.history if t.operation == 'add']
                    rows[f'{name}|{"x".join(map(str, shape))}@{i},{j}'] = dict(
                        status=result.status, evaluations=result.evaluations,
                        fem=sum(1 for t in result.history if t.outcome != 'no_component' and t.checks != dict(min_width='fail')),
                        adds=len(adds), adds_refused_by_global_check=sum(t.checks == dict(min_width='fail') for t in adds))
    return rows


def compare(old: dict, new: dict) -> None:
    for name in CONFIGS:
        for shape in (*("x".join(map(str, s)) for s in SHAPES), None):
            keys = [k for k in old if k.startswith(f'{name}|') and (shape is None or f'|{shape}@' in k)]
            feasible = lambda rows, ks: [k for k in ks if rows[k]['status'] == 'feasible']
            both = [k for k in feasible(old, keys) if k in feasible(new, keys)]
            median = lambda rows, field: statistics.median(rows[k][field] for k in both) if both else None
            print(f'{name:8s} {shape or "全部":7s} 題材{len(keys):3d}  初期合格 前{len(feasible(old, keys)):3d} 後{len(feasible(new, keys)):3d}'
                  f'  前のみ{len([k for k in feasible(old, keys) if k not in feasible(new, keys)]):3d} 後のみ{len([k for k in feasible(new, keys) if k not in feasible(old, keys)]):3d}'
                  f'  両方で届いた{len(both):3d}件の評価回数の中央値 前{median(old, "evaluations")} 後{median(new, "evaluations")}'
                  f'  FEM 前{median(old, "fem")} 後{median(new, "fem")}')
    for label, rows in (('前', old), ('後', new)):
        for name in CONFIGS:
            ks = [k for k in rows if k.startswith(f'{name}|')]
            print(f'{label} {name:8s} 追加の候補 {sum(rows[k]["adds"] for k in ks):6d} 件のうち大域判定で落ちた {sum(rows[k]["adds_refused_by_global_check"] for k in ks):4d} 件'
                  f'  評価合計 {sum(rows[k]["evaluations"] for k in ks)}  FEM合計 {sum(rows[k]["fem"] for k in ks)}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', help='別の版の作業ツリー(duocad パッケージの親)。省略すると今の作業ツリー')
    parser.add_argument('--result', type=Path, help='題材ごとの結果 JSON の書き出し先')
    parser.add_argument('--compare', nargs=2, type=Path, metavar=('BEFORE', 'AFTER'), help='2つの結果 JSON を並べる')
    args = parser.parse_args()
    if args.compare:
        compare(*(json.loads(p.read_text(encoding='utf-8')) for p in args.compare))
        return
    rows = measure(args.root)
    if args.result:
        _record.write(args.result, rows)
    print(f'{len(rows)} 件')


if __name__ == '__main__':
    main()

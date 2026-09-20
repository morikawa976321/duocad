"""設計空間の全材料マスクで、閉じた空洞の検査 1 回の秒を測る(探索・FEM は回さない)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_void_check_cost.py --model models\\hanger_bar_attachment.json --spacing 4.8 2.4 2.0 --out output\\stage4_peel\\void-check-cost.json

`design_space.derive` までで測れる。空洞の検査(`void_components`)と、それを含む `boundary_check`、
比較のための頂点の検査(`irregular_vertices`)・単一成分の検査(`connectivity_check`)を、それぞれ `--repeat` 回
測って最小・中央値を出す。設計空間のマスクに閉じた空洞が無いこと(`closed_voids` が空)も確かめる。
"""
import argparse
import json
import statistics
import time
from pathlib import Path

import _bootstrap  # noqa: F401
import _record

from duocad import optimize_run
from duocad.session import Session
from duocad.topopt import acceptance, design_space


def _seconds(function, mask, repeat):
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        function(mask)
        samples.append(time.perf_counter() - started)
    return dict(min=min(samples), median=statistics.median(samples))


def measure(document, spacing, repeat):
    bundle = optimize_run.build_conditions(document)
    space = design_space.derive(bundle.conditions, spacing=spacing)
    mask = space.mask
    return dict(spacing=spacing, grid=list(mask.shape), cells=int(mask.size), material_cells=int(mask.sum()),
                void_components=acceptance.void_components(mask), closed_voids=acceptance.closed_voids(mask),
                repeat=repeat,
                seconds=dict(void_components=_seconds(acceptance.void_components, mask, repeat),
                             boundary_check=_seconds(acceptance.boundary_check, mask, repeat),
                             irregular_vertices=_seconds(acceptance.irregular_vertices, mask, repeat),
                             connectivity_check=_seconds(acceptance.connectivity_check, mask, repeat)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--spacing', type=float, nargs='+', default=[4.8, 2.4])
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    session = Session(None, .2, models_dir=args.model.resolve().parent)
    session.open_document(args.model.name)
    rows = []
    for spacing in args.spacing:
        rows.append(measure(session.document, spacing, args.repeat))
        _record.write(args.out, rows)
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

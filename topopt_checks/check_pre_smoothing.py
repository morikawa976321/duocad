"""島除去後の6条件を検査する。終了コード: 0=合格、1=不合格、2=判定不能。"""
import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401
import _record
import numpy as np

from duocad.session import Session
from duocad.topopt import trace, voxelize
from duocad.topopt.acceptance import inspect_document


def read_run(run):
    selected = list(run.glob('*-selected-result.json'))
    if len(selected) != 1:
        raise ValueError('採用候補を示すselected-result記録が一意でない。--fieldsで明示すること')
    data = trace.load_snapshot(selected[0])['result']
    candidate = data['run']
    surface = candidate['surface']
    # 旧記録(密度場)の塗り直し: 除外を0、保持を1。重なりは保持が優先。
    before = np.array(candidate['result']['rho'], dtype=np.float64, copy=True)
    before[np.asarray(candidate['void'], dtype=bool)] = 0.0
    before[np.asarray(candidate['solid'], dtype=bool)] = 1.0
    grid = data['space']['grid']
    grid = voxelize.Grid(tuple(grid['origin']), grid['spacing'], tuple(grid['shape']))
    search_inputs = list(run.glob('*-safety-search-input.json'))
    max_evaluations = None
    if len(search_inputs) == 1:
        search_input = json.loads(search_inputs[0].read_text(encoding='utf-8'))
        max_evaluations = search_input.get('max_evaluations')
    return (run / 'input.json', surface['rho_clean'], before, grid,
            dict(level=surface['level'], target_safety_factor=data['target_safety_factor'],
                 allowable_mpa=data['allowable_mpa'],
                 youngs_modulus_mpa=data['report']['scale']['youngs_modulus_mpa'],
                 percentile=data['report']['representative']['percentile'],
                 history=data['history'], converged=data['converged'], max_evaluations=max_evaluations))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--run', type=Path, help='selected-resultとinput.jsonを含む実行フォルダ')
    source.add_argument('--fields', type=Path, help='rho_clean,rho_before,origin,spacingを含むNPZ')
    parser.add_argument('--document', type=Path, help='NPZに対応する条件文書JSON')
    parser.add_argument('--target', type=float, help='目標安全率。--fieldsでは必須')
    parser.add_argument('--level', type=float, help='材料はrho > level。NPZでは既定0.5')
    parser.add_argument('--allowable-mpa', type=float)
    parser.add_argument('--youngs-modulus-mpa', type=float)
    parser.add_argument('--percentile', type=float)
    parser.add_argument('--solver', choices=('cg', 'direct'), default='cg')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.run:
            document, clean, before, grid, options = read_run(args.run)
        else:
            if args.document is None or args.target is None:
                raise ValueError('--fieldsには--documentと--targetが必要')
            document, options = args.document, {}
            with np.load(args.fields, allow_pickle=False) as data:
                clean, before = data['rho_clean'], data['rho_before']
                grid = voxelize.Grid(tuple(data['origin']), float(data['spacing']), clean.shape)
        for key, value in dict(target_safety_factor=args.target, level=args.level,
                               allowable_mpa=args.allowable_mpa, youngs_modulus_mpa=args.youngs_modulus_mpa,
                               percentile=args.percentile).items():
            if value is not None:
                options[key] = value
        session = Session(None, 0.2, models_dir=document.resolve().parent)
        session.open_document(document.name)
        report = inspect_document(session.document, clean, before, grid, solver=args.solver, **options)
        report['source'] = dict(document=str(document.resolve()),
                              run=str(args.run.resolve()) if args.run else None,
                              fields=str(args.fields.resolve()) if args.fields else None)
    except Exception as error:
        report = dict(status='unknown', exit_code=2, error=f'{type(error).__name__}: {error}')
    _record.write(args.out, report, allow_nan=False)
    print(f"{report['status'].upper()}: {args.out}")
    return report['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())

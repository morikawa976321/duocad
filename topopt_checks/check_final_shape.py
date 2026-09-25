"""最終形状の水密性・単一ソリッドを判定する。保持・除外比較は調査オプション。

python topopt_checks/check_final_shape.py --mesh result.stl --out report.json
既定の条件文書は models/hanger_bar_attachment.json。STLはワールド座標[mm]で指定する。
終了コード: 0=全条件合格、1=不合格、2=判定不能または入力エラー。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

import _bootstrap  # noqa: F401
import _record
import numpy as np
from OCP.BOPAlgo import BOPAlgo_BOP, BOPAlgo_Operation
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopAbs import TopAbs_SOLID

from _documents import open_session
from duocad.paths import DEFAULT_MODELS_DIR
from duocad import kernel
from duocad.mesh_geometry import TOLERANCE_MM, TOPOLOGY_DEFECTS, make_material, subshapes, topology, weld_exact

LABELS = {
    "preserve": "保持部分をすべて含む",
    "exclude": "除外部分と体積的な重なりがない",
    "watertight": "水密な自己交差のない境界である",
    "single_solid": "材料領域が単一ソリッドである",
}


def read_stl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    data = path.read_bytes()
    count = struct.unpack_from('<I', data, 80)[0] if len(data) >= 84 else -1
    if count >= 0 and len(data) == 84 + count * 50:
        dtype = np.dtype([('normal', '<f4', 3), ('vertices', '<f4', (3, 3)), ('attr', '<u2')])
        triangles = np.frombuffer(data, dtype=dtype, offset=84, count=count)['vertices'].astype(float)
    else:
        points = []
        for line in data.decode('ascii').splitlines():
            words = line.split()
            if words and words[0].lower() == 'vertex':
                if len(words) != 4:
                    raise ValueError('STLのvertex行が不正')
                points.append([float(x) for x in words[1:]])
        if not points or len(points) % 3:
            raise ValueError('STLに有効な三角形がない')
        triangles = np.asarray(points).reshape(-1, 3, 3)
    if not triangles.size or not np.isfinite(triangles).all():
        raise ValueError('STLが空、または非有限座標を含む')
    return weld_exact(triangles)  # 完全一致する座標だけを共有する(近い頂点の融着で亀裂を隠さない)


def boolean_remainder(left, right, operation):
    boolean = BOPAlgo_BOP()
    boolean.AddArgument(left)
    boolean.AddTool(right)
    boolean.SetOperation(operation)
    boolean.SetFuzzyValue(0.0)
    boolean.Perform()
    if boolean.HasErrors() or boolean.HasWarnings():
        raise RuntimeError('集合演算にエラーまたは警告がある')
    result = boolean.Shape()
    if result.IsNull() or not BRepCheck_Analyzer(result).IsValid():
        raise RuntimeError('集合演算の結果が不正')
    solids = subshapes(result, TopAbs_SOLID)
    return dict(residual_solids=len(solids), residual_volume_mm3=sum(abs(kernel.volume(s)) for s in solids))


def evaluate(vertices, faces, preserve=(), exclude=(), *, progress=None, check_constraints=False):
    checks = {key: dict(label=label, status='unknown') for key, label in LABELS.items()
              if check_constraints or key in ('watertight', 'single_solid')}
    report = dict(checks=checks, tolerance_mm=TOLERANCE_MM,
                  method='OCCT CAD vs faceted mesh; finite kernel precision; no sampling or allowed residual volume',
                  boundary_contact_allowed=True)
    if check_constraints and not preserve:
        report['error'] = '保持部分の指定が空。未指定を合格にはしない'
        return finish(report)
    try:
        if progress:
            progress('境界の接続を検査')
        vertices, faces = np.asarray(vertices), np.asarray(faces)
        stats, components = topology(vertices, faces)
        report['mesh'] = stats
        invalid = any(stats[k] for k in TOPOLOGY_DEFECTS)
        if invalid:
            checks['watertight']['status'] = 'fail'
            return finish(report)
        if progress:
            progress(f"三角形{len(faces)}枚をソリッドへ変換し、自己干渉を検査")
        material, diagnostics = make_material(vertices, faces, components)
        report['mesh'].update(diagnostics)
        if material is None:
            checks['watertight']['status'] = 'fail'
            return finish(report)
        checks['watertight']['status'] = 'pass'
        checks['single_solid'].update(status='pass' if diagnostics['solids'] == 1 else 'fail',
                                      solids=diagnostics['solids'])
        if not check_constraints:
            return finish(report)
        for key, shapes, operation in (
            ('preserve', preserve, BOPAlgo_Operation.BOPAlgo_CUT),
            ('exclude', exclude, BOPAlgo_Operation.BOPAlgo_COMMON),
        ):
            rows = []
            for name, shape in shapes:
                if progress:
                    progress(f'{key} {len(rows)+1}/{len(shapes)}: {name}')
                try:
                    if shape is None or shape.IsNull() or not subshapes(shape, TopAbs_SOLID) or not BRepCheck_Analyzer(shape).IsValid():
                        raise ValueError('条件ソリッドが空または不正')
                    row = boolean_remainder(shape, material, operation)
                    rows.append(dict(body=name, status='pass' if row['residual_solids'] == 0 else 'fail', **row))
                except Exception as error:
                    rows.append(dict(body=name, status='unknown', error=str(error)))
                if progress:
                    progress(f"{name}: {rows[-1]['status'].upper()}")
            status = 'fail' if any(r['status']=='fail' for r in rows) else (
                'unknown' if any(r['status']=='unknown' for r in rows) else 'pass')
            checks[key].update(status=status, bodies=rows, checked_bodies=len(rows))
            if not shapes:
                checks[key]['note'] = '除外条件なし（空集合）'
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    return finish(report)


def finish(report):
    statuses = [check['status'] for check in report['checks'].values()]
    report['status'] = 'fail' if 'fail' in statuses else ('unknown' if 'unknown' in statuses else 'pass')
    report['exit_code'] = {'pass':0, 'fail':1, 'unknown':2}[report['status']]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='hanger_bar_attachment.json', help='models内の条件文書')
    parser.add_argument('--model-dir', type=Path, help='条件文書のフォルダ。診断記録のinput.jsonにも使用可')
    parser.add_argument('--check-constraints', action='store_true', help='調査用に旧保持・除外比較も実施する')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--mesh', type=Path, help='ワールド座標[mm]の最終STL。自動修復しない')
    source.add_argument('--mesh-body', help='条件文書内の最終メッシュボディID')
    source.add_argument('--solid-body', help='文書内の最終CADソリッドID（水密・単一ソリッドを直接検査）')
    source.add_argument('--solid-snapshot', type=Path,
                        help='記録の final-solid スナップショット（{"brep": ...} の JSON）。結果はメッシュボディで入るので、'
                             'ブーリアン後の B-rep を直接検査するときはこちら')
    parser.add_argument('--out', type=Path, required=True, help='判定JSONの保存先')
    args = parser.parse_args(argv)
    # `--model-dir` 省略時は `Session` の既定と同じ `DEFAULT_MODELS_DIR`。
    model_path = (Path(args.model_dir).resolve() if args.model_dir else DEFAULT_MODELS_DIR) / args.model
    if args.out.resolve() == model_path.resolve() or (args.mesh and args.out.resolve() == args.mesh.resolve()):
        parser.error('--out は入力ファイルと別のパスにしてください')
    started = time.perf_counter()
    try:
        session = open_session(model_path)
        document = session.document
        preserve = [(bid, document.get_body(bid).shape) for bid in document.optimize.preserve_bodies]
        exclude = [(bid, document.get_body(bid).shape) for bid in document.optimize.exclude_bodies]
        if args.solid_body or args.solid_snapshot:
            if args.check_constraints:
                raise ValueError('--solid-body / --solid-snapshot は水密・単一ソリッドの検査用です')
            from duocad.kernel_brep import inspect_solid, encode_brep, decode_brep
            if args.solid_body:
                shape = document.get_body(args.solid_body).shape
                source_info = dict(body=args.solid_body)
            else:
                shape = decode_brep(json.loads(args.solid_snapshot.read_text(encoding='utf-8'))['brep'])
                source_info = dict(path=str(args.solid_snapshot.resolve()))
            diagnostics = inspect_solid(shape)
            report = finish(dict(checks={key: dict(label=LABELS[key], status='pass' if diagnostics[key] else 'fail')
                                         for key in ('watertight', 'single_solid')}, solid=diagnostics))
            source_info['sha256'] = hashlib.sha256(encode_brep(shape).encode('ascii')).hexdigest()
        elif args.mesh:
            vertices, faces = read_stl(args.mesh)
            source_info = dict(path=str(args.mesh.resolve()), sha256=hashlib.sha256(args.mesh.read_bytes()).hexdigest())
        else:
            body = document.get_body(args.mesh_body)
            if body.mesh is None:
                raise ValueError('指定ボディはメッシュではない')
            triangles = np.asarray(body.mesh.positions).reshape(-1,3)[np.asarray(body.mesh.indices).reshape(-1,3)]
            vertices, faces = weld_exact(triangles)
            source_info = dict(body=args.mesh_body, sha256=hashlib.sha256(triangles.tobytes()).hexdigest())
        if not (args.solid_body or args.solid_snapshot):
            report = evaluate(vertices, faces, preserve, exclude, check_constraints=args.check_constraints,
                              progress=lambda message: print(message, file=sys.stderr, flush=True))
        report.update(model=args.model, model_sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(), source=source_info)
    except Exception as error:
        report = finish(dict(checks={key: dict(label=label, status='unknown')
                                     for key, label in LABELS.items()
                                     if args.check_constraints or key in ('watertight', 'single_solid')},
                             error=f'{type(error).__name__}: {error}'))
    report['seconds'] = time.perf_counter()-started
    _record.write(args.out, report)
    for check in report.get('checks', {}).values():
        print(f"{check['status'].upper()}: {check['label']}")
    print(f"{report['status'].upper()}: {args.out}")
    if 'error' in report:
        print(report['error'])
    return report['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())

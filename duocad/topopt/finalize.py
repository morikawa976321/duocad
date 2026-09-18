"""平滑化後の表面に保持の和集合、除外の差集合を適用する。"""
import numpy as np

from .. import kernel
from ..kernel_brep import boolean, encode_brep, inspect_solid, is_valid
from ..mesh_geometry import topology, make_material
from . import trace


def solid_mesh(shape, deviation=0.2):
    payload = kernel.tessellate(shape, deviation=deviation)
    if payload['untriangulated']:
        raise ValueError('最終CADに三角形分割できない面がある')
    triangles = np.concatenate([
        np.asarray(face['positions']).reshape(-1, 3)[np.asarray(face['indices']).reshape(-1, 3)]
        for face in payload['faces']
    ])
    vertices, inverse = np.unique(triangles.reshape(-1, 3), axis=0, return_inverse=True)
    return vertices, inverse.reshape(-1, 3)


def _save_shape(name, shape):
    recorder = trace.current()
    if recorder is not None:
        text = encode_brep(shape)
        trace.snapshot(name, brep=text)


def _boolean(shape, tools, operation, name, progress):
    if not tools:
        return shape
    if progress:
        progress(name)
    result, diagnostics = boolean(shape, tools, operation)
    trace.snapshot(name+'-diagnostics', **diagnostics)
    if diagnostics['has_errors'] or diagnostics['has_warnings']:
        if not result.IsNull():
            _save_shape(name+'-rejected', result)
        raise ValueError(f'{name}: ブーリアン演算にエラーまたは警告がある: {diagnostics}')
    if not is_valid(result) or not kernel.is_solid(result):
        raise ValueError(f'{name}: 演算結果が空または不正')
    _save_shape(name, result)
    return result


@trace.stage('finalize')
def finalize(vertices, faces, preserve, exclude, progress=None):
    vertices, faces = np.asarray(vertices), np.asarray(faces)
    stats, components = topology(vertices, faces)
    if any(stats[k] for k in ('boundary_edges', 'nonmanifold_edges', 'inconsistent_edges',
                              'nonmanifold_vertices', 'degenerate_triangles', 'duplicate_triangles')):
        raise ValueError('平滑化後の表面に開口または不正な接続がある')
    if progress:
        progress('平滑化後の表面をCADに変換')
    # ブーリアンで置換される領域の干渉もありうるため、自己干渉は演算後の境界で判定する。
    shape, diagnostics = make_material(vertices, faces, components, check_intersections=False)
    if shape is None:
        raise ValueError(f'平滑化後の表面をソリッド化できない: {diagnostics}')
    _save_shape('before-boolean', shape)
    shape = _boolean(shape, preserve, 'fuse', 'preserve-union', progress)
    # 重複領域では最後に適用する除外が優先する。
    shape = _boolean(shape, exclude, 'cut', 'exclude-cut', progress)
    if progress:
        progress('最終CADの水密性・単一ソリッドを検査')
    checks = inspect_solid(shape)
    trace.snapshot('final-solid-checks', **checks)
    if not checks['watertight'] or not checks['single_solid']:
        raise ValueError(f'ブーリアン後の形状が水密・単一ソリッドの条件に不合格: {checks}')
    _save_shape('final-solid', shape)
    return shape

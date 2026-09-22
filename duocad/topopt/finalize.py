"""平滑化後の表面に保持・除外のブーリアンを適用する。重複領域は除外が残る。"""
import numpy as np

from .. import kernel
from ..kernel_brep import boolean, encode_brep, inspect_solid, is_valid
from ..mesh_geometry import topology, make_material
from . import trace


def solid_mesh(shape):
    payload = kernel.tessellate(shape, deviation=0.2)
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


def _boolean(shape, tools, operation, name, progress, allow_empty=False):
    if not tools:
        return shape
    if progress:
        progress(name)
    result, diagnostics = boolean(shape, tools, operation)
    trace.snapshot(name+'-diagnostics', **diagnostics)
    if diagnostics['has_errors']:
        if not result.IsNull():
            _save_shape(name+'-rejected', result)
        raise ValueError(f'{name}: ブーリアン演算にエラーがある: {diagnostics}')
    if not is_valid(result):
        raise ValueError(f'{name}: 演算結果が不正')
    if not kernel.is_solid(result):
        # 除外が保持を全部覆うcutは、警告もエラーも無いまま0ソリッドの有効な空形状を返す
        # (実測: BOPAlgo_BOPは had_errors=has_warnings=False, is_valid=True, is_solid=False)。
        # allow_emptyの呼び出し元(保持ごとのcut)ではこれを「保持の寄与ゼロ」として扱う。
        if allow_empty:
            return None
        raise ValueError(f'{name}: 演算結果が空または不正')
    if diagnostics['has_warnings']:
        # OCCTの警告(has_warnings)は、頂点・辺どうしが許容差内で接しているだけでも無条件に出る
        # (面が実際に交差・重なっているとは限らない。docs/TOPOPT_ACCEPTANCE.md「除外cutの警告の原因」
        # 「まとめてcut→保持だけ引き算→fuseへの置き換え」)。警告だけでは棄却せず、inspect_solidの
        # 面-面判定(面を含まない接触は数えない)で実際に壊れているか確かめてから決める。
        checks = inspect_solid(result)
        trace.snapshot(name+'-warning-checks', **checks)
        if not checks['watertight'] or not checks['single_solid']:
            _save_shape(name+'-rejected', result)
            raise ValueError(f'{name}: 警告があり、検査でも水密・単一ソリッドの条件に不合格: {diagnostics}, {checks}')
    _save_shape(name, result)
    return result


@trace.stage('finalize')
def finalize(vertices, faces, preserve, exclude, progress=None):
    vertices, faces = np.asarray(vertices), np.asarray(faces)
    preserve, exclude = list(preserve), list(exclude)
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
    # 保持と除外が同一面(体積の重なりはゼロ)を共有するモデルでは、保持の和集合→除外の差集合の順だと
    # 2回目のcutが同じ面をもう一度交差計算し、自己交差の警告になる(docs/TOPOPT_ACCEPTANCE.md
    # 「除外cutの警告の原因」)。保持と除外をまとめて1回のcutで引き、除外済みの保持は別途fuseで足す。
    shape = _boolean(shape, preserve + exclude, 'cut', 'material-cut', progress)
    excluded_preserve = [result for result in (
        _boolean(body, exclude, 'cut', f'preserve-{i}-cut', progress, allow_empty=True)
        for i, body in enumerate(preserve)
    ) if result is not None]
    # 重複領域では除外が優先する: 引いたのは除外済みの保持(除外に全部覆われた分はNoneで除去済み)であり、除外を打ち消さない。
    shape = _boolean(shape, excluded_preserve, 'fuse', 'preserve-fuse', progress)
    if progress:
        progress('最終CADの水密性・単一ソリッドを検査')
    checks = inspect_solid(shape)
    trace.snapshot('final-solid-checks', **checks)
    if not checks['watertight'] or not checks['single_solid']:
        raise ValueError(f'ブーリアン後の形状が水密・単一ソリッドの条件に不合格: {checks}')
    _save_shape('final-solid', shape)
    return shape

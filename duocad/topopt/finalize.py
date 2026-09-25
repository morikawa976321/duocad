"""平滑化後の表面に保持・除外のブーリアンを適用する。重複領域は除外が残る。"""
import numpy as np

from .. import kernel
from ..kernel_brep import boolean, encode_brep, inspect_solid, is_valid
from ..mesh_geometry import TOPOLOGY_DEFECTS, degenerate_triangles, make_material, topology, weld_exact
from ..mesh_intersections import triangle_intersections
from . import trace


#: 最終CADを三角形分割する偏差[mm]。結果はメッシュボディとして文書に入り、画面も STL もその三角形を
#: そのまま使う(`optimize_run` / `io_step`)ので、画面の偏差(`kernel.DEFAULT_DEVIATION`)とは結びつかない。
FINAL_MESH_DEVIATION_MM = 0.2


def solid_mesh(shape):
    """最終CADを `FINAL_MESH_DEVIATION_MM` で三角形分割し、成果物として合格した (vertices, faces) を返す。

    合格条件は `final_mesh_checks`。不合格は例外(黙って穴の空いたメッシュを返さない)。
    """
    payload = kernel.tessellate(shape, deviation=FINAL_MESH_DEVIATION_MM)
    boundary = payload['boundary']
    if payload['untriangulated'] or boundary['unresolved']:
        raise ValueError(
            f"最終CADに三角形分割できない面がある: untriangulated={payload['untriangulated'][:20]}, "
            f"boundary={boundary['termination']}, unresolved={boundary['unresolved'][:5]}")
    vertices, faces = weld_exact(np.concatenate([
        np.asarray(face['positions']).reshape(-1, 3)[np.asarray(face['indices']).reshape(-1, 3)]
        for face in payload['faces']
    ]))
    checks = final_mesh_checks(vertices, faces)
    trace.snapshot('final-mesh-checks', boundary=dict(termination=boundary['termination'], rounds=boundary['rounds'],
                                                     amplified_edges=boundary['amplified_edges'],
                                                     reinstated=boundary['reinstated']), **checks)
    if checks['failed']:
        raise ValueError(f"最終CADの三角形分割が成果物の条件に不合格: {checks['failed']}")
    return vertices, faces


def final_mesh_checks(vertices, faces):
    """最終メッシュの合否。戻り値は JSON 化できる辞書で、`failed` に不合格の項目名を並べる。

    - 接続6項目(`topology`)は float64 の頂点で見る(現行の契約)。
    - **float32 に丸めた座標**でも見る: STL(`meshbody.write_stl`)も画面(three.js の Float32Array)も
      float32 で消費するので、成果物の自己交差・縮退・頂点の一致は丸めた座標について判定する。
      float64 で正常でも丸めで別の頂点が同じ座標になったり三角形が潰れたりしうる。
    - 自己交差(`mesh_intersections`)は正当な共有辺・共有頂点の接触を除いた幾何的な貫通・接触。
      重い検査なので消費される float32 側だけで 1 回行う。
    """
    stats, _ = topology(vertices, faces)
    failed = [key for key in TOPOLOGY_DEFECTS if stats[key]]
    rounded = np.asarray(vertices, dtype=np.float64).astype(np.float32).astype(np.float64)
    merged = int(len(rounded) - len(np.unique(rounded, axis=0)))
    degenerate = int(degenerate_triangles(rounded, faces).sum())
    if merged:
        failed.append('float32_merged_vertices')
    if degenerate:
        failed.append('float32_degenerate_triangles')
    intersections = triangle_intersections(rounded, faces)
    if intersections['pairs']:
        failed.append('self_intersections')
    return dict(failed=failed, topology=stats,
                float32=dict(merged_vertices=merged, degenerate_triangles=degenerate),
                self_intersections=dict(counts=intersections['counts'], candidates=intersections['candidates'],
                                        pairs=intersections['pairs'][:50]))


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
    if any(stats[k] for k in TOPOLOGY_DEFECTS):
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

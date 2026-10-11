"""平滑化後の表面に保持・除外のメッシュ集合演算(manifold3d)を適用する。重複領域は除外が残る。

採用の判断と実測(hanger を含む3例での合格、偏差ごとの体積差・めり込み、`as_original()` が要る理由)は
docs/TOPOPT_ACCEPTANCE.md「メッシュ集合演算」。
"""
import time

import manifold3d
import numpy as np

from .. import kernel
from ..io_step import STL_DEVIATION
from ..mesh_geometry import TOPOLOGY_DEFECTS, degenerate_triangles, topology, weld_exact
from ..mesh_intersections import triangle_intersections
from . import trace

#: 保持・除外 CAD を三角形分割する線形偏差[mm]。STL 書き出しと同じ値で、角度偏差は
#: `kernel.DEFAULT_ANGULAR_DEFLECTION`。三角形は内接するので、円柱では半径によらず約 0.005 mm だけ弦が
#: 内側を通り、除外の穴の内面はその分だけ除外の中に残る(docs/TOPOPT_ACCEPTANCE.md「円柱の除外へのめり込み」)。
CAD_DEVIATION_MM = STL_DEVIATION

#: 集合演算の結果を簡略化するときの形状変化の許容値[mm]。欠陥を見逃すしきい値ではない。
#: `as_original()` で入力面の由来を捨ててからかけないと、float32 で潰れる細い三角形が STL に残る
#: (docs/TOPOPT_ACCEPTANCE.md「保存精度と簡略化」)。
SIMPLIFY_TOLERANCE_MM = 1e-5

#: float32 で交差する組の三角形を潰すときの最短辺の上限[mm]。辺1本を潰すと面はその長さだけ動く
#: (潰しが連鎖すると足し合わさり、移動量そのものは検査していない)。F2000_h2.0_w2 で要ったのは
#: 辺 3.05e-4 mm の針状の1本(docs/TOPOPT_ACCEPTANCE.md「float32 で裏返る針状の三角形」)。
FLOAT32_CROSSING_MAX_EDGE_MM = 1e-3

#: 交差の組を潰して検査し直す回数の上限。1回ごとに最終検査(数秒〜十数秒)をかけ直す。
_CROSSING_ROUNDS = 8

_NO_ERROR = manifold3d.Error.NoError


def tessellate_body(shape, *, deviation=CAD_DEVIATION_MM):
    """CAD 1体を三角形分割し、座標が完全一致する頂点だけを共有した (vertices, faces, boundary) を返す。

    三角形分割できない面・解消できない境界交差は例外(黙って穴の空いたメッシュを返さない)。
    `boundary` は面分割の直前の境界交差検査の要約(`kernel.tessellate`)。
    """
    payload = kernel.tessellate(shape, deviation, kernel.DEFAULT_ANGULAR_DEFLECTION)
    boundary = payload['boundary']
    if payload['untriangulated'] or boundary['unresolved']:
        raise ValueError(
            f"CADに三角形分割できない面がある: untriangulated={payload['untriangulated'][:20]}, "
            f"boundary={boundary['termination']}, unresolved={boundary['unresolved'][:5]}")
    vertices, faces = weld_exact(np.concatenate([
        np.asarray(face['positions']).reshape(-1, 3)[np.asarray(face['indices']).reshape(-1, 3)]
        for face in payload['faces']
    ]))
    summary = dict(termination=boundary['termination'], rounds=boundary['rounds'],
                   amplified_edges=boundary['amplified_edges'], reinstated=boundary['reinstated'])
    return vertices, faces, summary


def signed_volumes(vertices, faces, components):
    """接続成分ごとの符号付き体積[mm³]。外向きの閉殻は正、内向きの閉殻(空洞)は負。開いた成分には意味が無い。"""
    volumes = []
    for component in components:
        tri = vertices[faces[component]]
        tri = tri - tri[0, 0]
        volumes.append(float(np.einsum('ij,ij->i', tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6))
    return volumes


def invalid_shell_orientation(vertices, faces, components):
    """交差のない閉殻の包含深さと向きを照合する。接続・自己交差検査に合格した入力専用。"""
    triangles = [vertices[faces[component]] for component in components]
    bounds = [(tri.min(axis=(0, 1)), tri.max(axis=(0, 1))) for tri in triangles]
    volumes = signed_volumes(vertices, faces, components)
    for i, tri in enumerate(triangles):
        point = tri[0, 0]
        depth = 0
        for j, outer in enumerate(triangles):
            if i == j or np.any(point < bounds[j][0]) or np.any(point > bounds[j][1]):
                continue
            # 閉殻全体の立体角は内部で ±4π、外部で0。交差が無いので代表頂点1つで包含を判定できる。
            rays = outer - point
            rays /= np.linalg.norm(rays, axis=2)[..., None]
            a, b, c = rays[:, 0], rays[:, 1], rays[:, 2]
            numerator = np.einsum('ij,ij->i', a, np.cross(b, c))
            denominator = (1 + np.einsum('ij,ij->i', a, b) + np.einsum('ij,ij->i', b, c)
                           + np.einsum('ij,ij->i', c, a))
            winding = np.arctan2(numerator, denominator).sum() / (2 * np.pi)
            if not np.isfinite(winding):
                return True
            depth += abs(winding) > 0.5
        if volumes[i] == 0 or (volumes[i] > 0) != (depth % 2 == 0):
            return True
    return False


def input_mesh_checks(vertices, faces):
    """集合演算へ渡すメッシュ(平滑化後の表面・三角形化した保持・除外)の合否。

    manifold3d は閉じて向きが揃い交差の無い入力を前提にし、交差があっても止まらず結果に交差を残す
    (docs/TOPOPT_ACCEPTANCE.md「集合演算のみの結果」の偏差 0.002 mm)。接続6項目・向き
    (包含深さと閉殻の向きの一致)・自己交差を見る。戻り値は JSON 化できる辞書で、`failed` に不合格の項目名を並べる。
    """
    stats, components = topology(vertices, faces)
    failed = [key for key in TOPOLOGY_DEFECTS if stats[key]]
    volumes = [] if failed else signed_volumes(vertices, faces, components)
    if volumes and sum(volumes) <= 0:
        failed.append('inward_orientation')
    intersections = triangle_intersections(vertices, faces)
    if intersections['pairs']:
        failed.append('self_intersections')
    if not failed and invalid_shell_orientation(vertices, faces, components):
        failed.append('invalid_shell_orientation')
    return dict(failed=failed, topology=stats, volumes=volumes,
                self_intersections=dict(counts=intersections['counts'], candidates=intersections['candidates'],
                                        pairs=intersections['pairs'][:50]))


def final_mesh_checks(vertices, faces):
    """最終メッシュの合否。戻り値は JSON 化できる辞書で、`failed` に不合格の項目名を並べる。

    - 接続6項目(`topology`)は float64 の頂点で見る。
    - 単一ソリッド: 接続成分のうち符号付き体積が正のもの(材料)がちょうど1つ。負のもの(空洞)は数えて残し、
      0 のもの、および包含深さに対して向きが不正なものは不合格。
    - **float32 に丸めた座標**でも見る: STL(`meshbody.write_stl`)も画面(three.js の Float32Array)も
      float32 で消費するので、成果物の自己交差・縮退・頂点の一致は丸めた座標について判定する。
      float64 で正常でも丸めで別の頂点が同じ座標になったり三角形が潰れたりしうる。
    - 自己交差(`mesh_intersections`)は正当な共有辺・共有頂点の接触を除いた幾何的な貫通・接触。
      重い検査なので消費される float32 側だけで 1 回行う。
    """
    stats, components = topology(vertices, faces)
    failed = [key for key in TOPOLOGY_DEFECTS if stats[key]]
    volumes = signed_volumes(vertices, faces, components)
    solids = sum(volume > 0 for volume in volumes)
    cavities = sum(volume < 0 for volume in volumes)
    if solids != 1 or solids + cavities != len(volumes):
        failed.append('single_solid')
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
    if not failed and invalid_shell_orientation(rounded, faces, components):
        failed.append('invalid_shell_orientation')
    return dict(failed=failed, topology=stats, solids=solids, cavities=cavities, volume_mm3=float(sum(volumes)),
                float32=dict(merged_vertices=merged, degenerate_triangles=degenerate),
                self_intersections=dict(counts=intersections['counts'], candidates=intersections['candidates'],
                                        pairs=intersections['pairs'][:50]))


def collapse_float32_slivers(vertices, faces, *, max_edge_mm=SIMPLIFY_TOLERANCE_MM):
    """float32 に丸めると潰れる三角形(面積 0)の最短辺を、長さが `max_edge_mm` 未満なら潰す。

    manifold3d の `simplify` は許容値未満の辺でも残すことがある(実測: 辺 3.9e-6 mm のスライバーが
    1e-5・1e-4 で残り、1e-3 では別の条件で簡略化自身が縮退を作る。docs/TOPOPT_ACCEPTANCE.md
    「メッシュ集合演算での実測」)。潰すのは辺の両端点を一方へ寄せる操作で、面はその辺の長さまでしか動かない。
    最短辺が `max_edge_mm` 以上の三角形は潰さず残す(後段の `final_mesh_checks` が不合格にする)。
    戻り値は (vertices, faces, 潰した辺の数)。潰す三角形が無ければ入力をそのまま返す。
    """
    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces)
    collapsed = 0
    for _ in range(16):
        rounded = vertices.astype(np.float32).astype(np.float64)
        bad = np.flatnonzero(degenerate_triangles(rounded, faces))
        if not len(bad):
            break
        vertices, faces, merges = _collapse_shortest_edges(vertices, faces, bad, max_edge_mm)
        if not merges:
            break
        collapsed += merges
    return vertices, faces, collapsed


def collapse_float32_crossings(vertices, faces, pairs, *, max_edge_mm=FLOAT32_CROSSING_MAX_EDGE_MM):
    """float32 で交差・接触する組 `pairs`((i, j, kind) の並び)の三角形の最短辺を、`max_edge_mm` 未満なら潰す。

    float64 では交差しない形でも、高さが float32 の丸め幅より低い針状の三角形は丸めで裏返り、隣と交差する
    (面積は 0 にならないので `collapse_float32_slivers` では拾えない)。`pairs` に現れない三角形は触らないので、
    交差が無ければ何もしない。潰した後の合否は呼び出し側が検査し直す。戻り値は (vertices, faces, 潰した辺の数)。
    """
    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces)
    triangles = np.unique(np.asarray([(i, j) for i, j, _ in pairs], dtype=np.int64).reshape(-1))
    if not len(triangles):
        return vertices, faces, 0
    return _collapse_shortest_edges(vertices, faces, triangles, max_edge_mm)


def repair_float32_crossings(vertices, faces, checks, progress=None):
    """`final_mesh_checks` の結果 `checks` の交差の組を `collapse_float32_crossings` で潰し、検査し直す。

    `final_mesh_checks` は float32 側しか見ないので、float64 はここで見る(交差の組があるときだけ)。
    潰す前から float64 で交差があれば集合演算・簡略化の欠陥で丸めのせいではないので、潰して隠さずそのまま返す。
    潰した結果が float64 で交差を作ったら `failed` に `float64_self_intersections` を足す(float32 では消えて見えうる)。
    検査が返す組は先頭50組までなので、それより多い交差は次の周回で潰す。戻り値は (vertices, faces, checks, 記録用の辞書)。
    """
    collapsed = 0
    float64_intersections = None
    stale = False
    for _ in range(_CROSSING_ROUNDS):
        if not checks['self_intersections']['pairs']:
            break
        float64_intersections, stale = triangle_intersections(vertices, faces)['counts'], False
        if any(float64_intersections.values()):
            break
        vertices, faces, merges = collapse_float32_crossings(vertices, faces, checks['self_intersections']['pairs'])
        if not merges:
            break
        collapsed, stale = collapsed + merges, True
        if progress:
            progress('float32 で交差する三角形を潰して最終メッシュを検査し直す')
        checks = final_mesh_checks(vertices, faces)
    if stale:
        float64_intersections = triangle_intersections(vertices, faces)['counts']
    if collapsed and any(float64_intersections.values()):
        checks = dict(checks, failed=[*checks['failed'], 'float64_self_intersections'])
    return vertices, faces, checks, dict(collapsed_crossing_edges=collapsed,
                                         float64_self_intersections=float64_intersections)


def _collapse_shortest_edges(vertices, faces, triangles, max_edge_mm):
    """`triangles` の各三角形の最短辺を、`max_edge_mm` 未満なら一端へ寄せて潰す。(vertices, faces, 潰した辺の数)。"""
    parent = np.arange(len(vertices))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return int(i)

    merges = 0
    for t in triangles:
        tri = faces[t]
        points = vertices[tri]
        lengths = np.linalg.norm(points[[1, 2, 0]] - points, axis=1)
        k = int(np.argmin(lengths))
        if lengths[k] >= max_edge_mm:
            continue
        kept, dropped = find(int(tri[k])), find(int(tri[(k + 1) % 3]))
        if kept != dropped:
            parent[dropped] = kept
            merges += 1
    if not merges:
        return vertices, faces, 0
    roots = np.fromiter((find(i) for i in range(len(vertices))), dtype=np.int64, count=len(vertices))
    faces = roots[faces]
    faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 2] != faces[:, 0])]
    vertices, faces = weld_exact(vertices[faces])
    return vertices, faces, merges


def _manifold(name, vertices, faces):
    mesh = manifold3d.Mesh64(vert_properties=np.ascontiguousarray(vertices, dtype=np.float64),
                             tri_verts=np.ascontiguousarray(faces, dtype=np.uint64))
    solid = manifold3d.Manifold(mesh)
    if solid.status() != _NO_ERROR:
        raise ValueError(f'{name}: manifold3d が入力を受け付けない: {solid.status()}')
    return solid


@trace.stage('finalize')
def finalize(vertices, faces, preserve, exclude, progress=None):
    """平滑化後の表面に保持を足し、除外を引いた最終メッシュ (vertices, faces) を返す。

    合格条件は `final_mesh_checks`(接続6項目・単一ソリッド・float32 の縮退と頂点の一致・自己交差)。
    float32 だけで交差する組は `repair_float32_crossings` で潰して検査し直す。
    入力(平滑化後の表面、三角形化した保持・除外)にも `input_mesh_checks` をかける。どちらも不合格は例外
    (黙って穴・分断・交差のある形を返さない)。小さいから通す判定は無い。結果の三角形は文書・画面・STL が
    そのまま使う。
    """
    vertices, faces = np.asarray(vertices, dtype=np.float64), np.asarray(faces)
    preserve, exclude = list(preserve), list(exclude)
    if progress:
        progress('平滑化後の表面を検査')
    checks = input_mesh_checks(vertices, faces)
    trace.snapshot('smoothed-mesh-checks', **checks)
    if checks['failed']:
        raise ValueError(
            f"平滑化後の表面が集合演算の入力条件(開口・不正な接続・向き・自己交差)に不合格: {checks['failed']}, "
            f"topology={checks['topology']}, self_intersections={checks['self_intersections']['counts']}")

    solids = dict(preserve=[], exclude=[])
    records = []
    for role, label, shapes in (('preserve', '保持', preserve), ('exclude', '除外', exclude)):
        for index, shape in enumerate(shapes):
            if progress:
                progress(f'{label} {index + 1}/{len(shapes)} を三角形分割して検査')
            body_vertices, body_faces, boundary = tessellate_body(shape)
            body_checks = input_mesh_checks(body_vertices, body_faces)
            records.append(dict(role=role, index=index, triangles=int(len(body_faces)),
                                vertices=int(len(body_vertices)), boundary=boundary, **body_checks))
            if body_checks['failed']:
                trace.snapshot('cad-meshes', deviation=CAD_DEVIATION_MM, bodies=records)
                raise ValueError(
                    f"{label} {index} の三角形分割が集合演算の入力条件に不合格: {body_checks['failed']}, "
                    f"topology={body_checks['topology']}, "
                    f"self_intersections={body_checks['self_intersections']['counts']}")
            solids[role].append(_manifold(f'{role}-{index}', body_vertices, body_faces))
    trace.snapshot('cad-meshes', deviation=CAD_DEVIATION_MM, bodies=records)

    if progress:
        progress('メッシュの集合演算(保持を足して除外を引く)')
    started = time.perf_counter()
    result = manifold3d.Manifold.batch_boolean([_manifold('smoothed', vertices, faces), *solids['preserve']],
                                               manifold3d.OpType.Add)
    if solids['exclude']:
        result = result - manifold3d.Manifold.batch_boolean(solids['exclude'], manifold3d.OpType.Add)
    boolean_seconds = time.perf_counter() - started
    if result.status() != _NO_ERROR:
        raise ValueError(f'集合演算が失敗した: {result.status()}')
    if result.is_empty():
        raise ValueError('集合演算の結果が空(除外が材料を全部覆っている)')
    started = time.perf_counter()
    simplified = result.as_original().simplify(SIMPLIFY_TOLERANCE_MM)
    simplify_seconds = time.perf_counter() - started
    if simplified.status() != _NO_ERROR or simplified.is_empty():
        raise ValueError(f'簡略化が失敗した: {simplified.status()}')
    mesh = simplified.to_mesh64()
    positions = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    final_vertices, final_faces, collapsed = collapse_float32_slivers(
        *weld_exact(positions[np.asarray(mesh.tri_verts, dtype=np.int64)]))
    trace.snapshot('mesh-boolean', boolean_seconds=boolean_seconds, simplify_seconds=simplify_seconds,
                   simplify_tolerance_mm=SIMPLIFY_TOLERANCE_MM, collapsed_edges=collapsed,
                   raw_triangles=int(result.num_tri()), raw_volume_mm3=float(result.volume()),
                   simplified_triangles=int(simplified.num_tri()), simplified_volume_mm3=float(simplified.volume()),
                   triangles=int(len(final_faces)), vertices=int(len(final_vertices)))

    if progress:
        progress('最終メッシュの接続・単一ソリッド・float32・自己交差を検査')
    checks = final_mesh_checks(final_vertices, final_faces)
    final_vertices, final_faces, checks, repair = repair_float32_crossings(final_vertices, final_faces, checks,
                                                                          progress=progress)
    trace.snapshot('final-mesh-checks', **repair, **checks)
    if checks['failed']:
        raise ValueError(
            f"最終メッシュが成果物の条件(接続6項目・単一ソリッド・float32・自己交差)に不合格: {checks['failed']}, "
            f"solids={checks['solids']}, cavities={checks['cavities']}, topology={checks['topology']}, "
            f"float32={checks['float32']}, self_intersections={checks['self_intersections']['counts']}, "
            f"float64_self_intersections={repair['float64_self_intersections']}")
    return final_vertices, final_faces

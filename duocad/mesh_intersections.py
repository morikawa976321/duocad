"""三角形メッシュの自己交差を、正当な共有辺・共有頂点の接触を除いて検出する。OCP に依存しない。

`topology()`(mesh_geometry.py)は接続だけを見るので、辺を正しく共有したまま幾何的に貫通する三角形は
通してしまう。ここは座標で判定する:

- 頂点を共有しない組: 一方の辺が他方の三角形(閉集合)に触れるか貫くかを、向き判定(orient3d / orient2d)
  だけで決める。共面なら 2D に落として辺の交差と頂点の包含を見る。
- 頂点を 1 つ共有する組: 共有頂点の対辺が相手に触れるか、共有頂点から出る辺が相手の平面内で相手の
  楔(2 辺のなす角)に入るかを見る。
- 辺を共有する組: 共面で同じ側へ折り返しているときだけ不正(`fold`)。非共面なら共有辺だけで接する。

向き判定は浮動小数点で計算し、Shewchuk の誤差境界を下回る組だけ整数演算で厳密に評価する(`boundary_check` と同じ)。
固定 ε は使わない。判定が保証するのは「渡された浮動小数点座標の三角形」であって、元 CAD の厳密性ではない。
"""
from __future__ import annotations

import numpy as np

from .boundary_check import _integers, orient2d

_EPS = np.finfo(np.float64).eps
#: Shewchuk, orient3d の o3derrboundA。
_ORIENT3D_BOUND = (7.0 + 56.0 * _EPS) * _EPS
#: 1 度に評価する組の数(orient3d の一時配列を抑える)。
_CHUNK = 250_000

KINDS = ('cross', 'touch', 'coplanar', 'fold', 'duplicate')


def orient3d_exact(a, b, c, d) -> int:
    """d が平面 (a, b, c) のどちら側かの符号を整数演算で厳密に求める。"""
    ax, ay, az, bx, by, bz, cx, cy, cz, dx, dy, dz = _integers(
        (a[0], a[1], a[2], b[0], b[1], b[2], c[0], c[1], c[2], d[0], d[1], d[2]))
    adx, ady, adz = ax - dx, ay - dy, az - dz
    bdx, bdy, bdz = bx - dx, by - dy, bz - dz
    cdx, cdy, cdz = cx - dx, cy - dy, cz - dz
    det = (adx * (bdy * cdz - bdz * cdy) + bdx * (cdy * adz - cdz * ady) + cdx * (ady * bdz - adz * bdy))
    return (det > 0) - (det < 0)


def orient3d(a: np.ndarray, b: np.ndarray, c: np.ndarray, d: np.ndarray) -> np.ndarray:
    """行ごとの符号 (M,) int8。浮動小数点で確定しない行だけ厳密演算へ落とす。"""
    ad, bd, cd = a - d, b - d, c - d
    bdxcdy = bd[:, 0] * cd[:, 1]
    cdxbdy = cd[:, 0] * bd[:, 1]
    cdxady = cd[:, 0] * ad[:, 1]
    adxcdy = ad[:, 0] * cd[:, 1]
    adxbdy = ad[:, 0] * bd[:, 1]
    bdxady = bd[:, 0] * ad[:, 1]
    det = ad[:, 2] * (bdxcdy - cdxbdy) + bd[:, 2] * (cdxady - adxcdy) + cd[:, 2] * (adxbdy - bdxady)
    permanent = ((np.abs(bdxcdy) + np.abs(cdxbdy)) * np.abs(ad[:, 2])
                 + (np.abs(cdxady) + np.abs(adxcdy)) * np.abs(bd[:, 2])
                 + (np.abs(adxbdy) + np.abs(bdxady)) * np.abs(cd[:, 2]))
    sign = np.sign(det).astype(np.int8)
    # permanent == 0 は 6 つの積の因子のどれかが厳密に 0(座標が一致する軸を持つ 4 点)で、det は厳密に 0
    certain = (permanent == 0) | (np.abs(det) > _ORIENT3D_BOUND * permanent)
    for row in np.flatnonzero(~certain):
        sign[row] = orient3d_exact(a[row], b[row], c[row], d[row])
    return sign


# --------------------------------------------------------------------------------------
# 広域: 一様格子で外接箱が重なる組を集める
# --------------------------------------------------------------------------------------
def _candidate_pairs(lo: np.ndarray, hi: np.ndarray) -> tuple:
    """外接箱が重なる三角形の組 (i, j), i<j。"""
    count = len(lo)
    if count < 2:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    extent = (hi - lo).max(axis=1)
    cell = float(np.median(extent)) * 1.5
    if not cell > 0:
        cell = float(extent.max()) if extent.max() > 0 else 1.0
    origin = lo.min(axis=0)
    while True:
        ilo = np.floor((lo - origin) / cell).astype(np.int64)
        ihi = np.floor((hi - origin) / cell).astype(np.int64)
        spans = ihi - ilo + 1
        per_tri = spans.prod(axis=1)
        if per_tri.sum() <= 24 * count or cell >= extent.max() * 4:
            break
        cell *= 2.0
    dims = ihi.max(axis=0) + 1
    tri_index = np.repeat(np.arange(count), per_tri)
    local = np.arange(per_tri.sum()) - np.repeat(np.cumsum(per_tri) - per_tri, per_tri)
    sx, sy = spans[tri_index, 0], spans[tri_index, 1]
    cx = ilo[tri_index, 0] + local % sx
    cy = ilo[tri_index, 1] + (local // sx) % sy
    cz = ilo[tri_index, 2] + local // (sx * sy)
    cell_id = (cx * dims[1] + cy) * dims[2] + cz
    order = np.argsort(cell_id, kind='stable')
    cell_sorted = cell_id[order]
    tri_sorted = tri_index[order]
    boundaries = np.flatnonzero(np.diff(cell_sorted)) + 1
    starts = np.concatenate(([0], boundaries))
    sizes = np.diff(np.concatenate((starts, [len(cell_sorted)])))
    group_start = np.repeat(starts, sizes)
    position = np.arange(len(cell_sorted)) - group_start
    remaining = np.repeat(sizes, sizes) - position - 1
    total = int(remaining.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    first = np.repeat(np.arange(len(cell_sorted)), remaining)
    second = first + 1 + (np.arange(total) - np.repeat(np.cumsum(remaining) - remaining, remaining))
    i = tri_sorted[first]
    j = tri_sorted[second]
    swap = i > j
    i, j = np.where(swap, j, i), np.where(swap, i, j)
    key = np.unique(i * count + j)
    i, j = key // count, key % count
    overlap = (lo[j] <= hi[i]).all(axis=1) & (lo[i] <= hi[j]).all(axis=1)
    return i[overlap], j[overlap]


# --------------------------------------------------------------------------------------
# 狭域: 述語だけで決める
# --------------------------------------------------------------------------------------
def _drop_axis(normal_like: np.ndarray) -> np.ndarray:
    """共面の組を 2D に落とすとき捨てる軸(法線の最大成分)。"""
    return np.argmax(np.abs(normal_like), axis=1)


def _project(points: np.ndarray, axis: np.ndarray) -> np.ndarray:
    keep = np.ones((len(points), 3), dtype=bool)
    keep[np.arange(len(points)), axis] = False
    return points[keep].reshape(-1, 2)


def _segment_segment_2d(p1, p2, p3, p4) -> np.ndarray:
    """2D 線分の接触: 0 なし / 1 接触(端点が相手の上・共線の重なり) / 2 交差。"""
    o1 = orient2d(p3, p4, p1)
    o2 = orient2d(p3, p4, p2)
    o3 = orient2d(p1, p2, p3)
    o4 = orient2d(p1, p2, p4)
    cross = (o1 * o2 < 0) & (o3 * o4 < 0)
    inside = lambda pt, a, b: (pt >= np.minimum(a, b)).all(axis=1) & (pt <= np.maximum(a, b)).all(axis=1)
    touch = ((o1 == 0) & inside(p1, p3, p4)) | ((o2 == 0) & inside(p2, p3, p4)) \
        | ((o3 == 0) & inside(p3, p1, p2)) | ((o4 == 0) & inside(p4, p1, p2))
    return np.where(cross, 2, np.where(touch, 1, 0)).astype(np.int8)


def _point_in_triangle_2d(p, a, b, c) -> np.ndarray:
    """閉じた三角形に入っていれば True(辺上・頂点上も True)。"""
    s1 = orient2d(a, b, p)
    s2 = orient2d(b, c, p)
    s3 = orient2d(c, a, p)
    return ((s1 >= 0) & (s2 >= 0) & (s3 >= 0)) | ((s1 <= 0) & (s2 <= 0) & (s3 <= 0))


def _triangles_2d(t1: np.ndarray, t2: np.ndarray) -> np.ndarray:
    """共面の三角形どうし(頂点共有なし)の接触。0 なし / 1 あり。"""
    hit = np.zeros(len(t1), dtype=bool)
    for e1 in range(3):
        for e2 in range(3):
            hit |= _segment_segment_2d(t1[:, e1], t1[:, (e1 + 1) % 3], t2[:, e2], t2[:, (e2 + 1) % 3]) > 0
    for k in range(3):
        hit |= _point_in_triangle_2d(t1[:, k], t2[:, 0], t2[:, 1], t2[:, 2])
        hit |= _point_in_triangle_2d(t2[:, k], t1[:, 0], t1[:, 1], t1[:, 2])
    return hit


def _segment_triangle(p, q, u, v, w, sp=None, sq=None) -> np.ndarray:
    """線分 (p, q) と閉じた三角形 (u, v, w) の接触: 0 なし / 1 接触 / 2 貫通。

    `sp`, `sq` は p, q の平面 (u, v, w) に対する符号(計算済みなら渡す)。
    """
    if sp is None:
        sp = orient3d(u, v, w, p)
    if sq is None:
        sq = orient3d(u, v, w, q)
    result = np.zeros(len(p), dtype=np.int8)
    straddle = (sp * sq <= 0) & ~((sp == 0) & (sq == 0))
    if straddle.any():
        idx = np.flatnonzero(straddle)
        o1 = orient3d(p[idx], q[idx], u[idx], v[idx])
        o2 = orient3d(p[idx], q[idx], v[idx], w[idx])
        o3 = orient3d(p[idx], q[idx], w[idx], u[idx])
        positive = (o1 >= 0) & (o2 >= 0) & (o3 >= 0)
        negative = (o1 <= 0) & (o2 <= 0) & (o3 <= 0)
        inside = positive | negative
        strict = ((o1 > 0) & (o2 > 0) & (o3 > 0)) | ((o1 < 0) & (o2 < 0) & (o3 < 0))
        proper = strict & (sp[idx] != 0) & (sq[idx] != 0)
        result[idx[inside & ~proper]] = 1
        result[idx[proper]] = 2
    coplanar = (sp == 0) & (sq == 0)
    if coplanar.any():
        idx = np.flatnonzero(coplanar)
        axis = _drop_axis(np.cross(v[idx] - u[idx], w[idx] - u[idx]))
        P, Q = _project(p[idx], axis), _project(q[idx], axis)
        U, V, W = _project(u[idx], axis), _project(v[idx], axis), _project(w[idx], axis)
        hit = (_segment_segment_2d(P, Q, U, V) > 0) | (_segment_segment_2d(P, Q, V, W) > 0) \
            | (_segment_segment_2d(P, Q, W, U) > 0) | _point_in_triangle_2d(P, U, V, W) | _point_in_triangle_2d(Q, U, V, W)
        result[idx[hit]] = 1
    return result


def _in_wedge_2d(apex, ray1, ray2, point) -> np.ndarray:
    """apex を頂点とする閉じた楔 (ray1, ray2) に point が入るか(apex 自身は除く前提)。"""
    s = orient2d(apex, ray1, ray2)
    a = orient2d(apex, ray1, point)
    b = orient2d(apex, point, ray2)
    inside = ((s > 0) & (a >= 0) & (b >= 0)) | ((s < 0) & (a <= 0) & (b <= 0))
    # 楔が退化(共線)しているときは相手の三角形が退化しているので判定しない
    return inside & (s != 0)


def _no_shared(t1: np.ndarray, t2: np.ndarray) -> np.ndarray:
    """頂点を共有しない組。0 なし / 1 接触 / 2 貫通 / 3 共面で重なる。"""
    result = np.zeros(len(t1), dtype=np.int8)
    a, b, c = t1[:, 0], t1[:, 1], t1[:, 2]
    d, e, f = t2[:, 0], t2[:, 1], t2[:, 2]
    sd, se, sf = orient3d(a, b, c, d), orient3d(a, b, c, e), orient3d(a, b, c, f)
    apart = ((sd > 0) & (se > 0) & (sf > 0)) | ((sd < 0) & (se < 0) & (sf < 0))
    sa, sb, sc = orient3d(d, e, f, a), orient3d(d, e, f, b), orient3d(d, e, f, c)
    apart |= ((sa > 0) & (sb > 0) & (sc > 0)) | ((sa < 0) & (sb < 0) & (sc < 0))
    coplanar = (sd == 0) & (se == 0) & (sf == 0) & (sa == 0) & (sb == 0) & (sc == 0)
    idx = np.flatnonzero(coplanar)
    if len(idx):
        axis = _drop_axis(np.cross(b[idx] - a[idx], c[idx] - a[idx]))
        hit = _triangles_2d(np.stack([_project(t1[idx, k], axis) for k in range(3)], axis=1),
                            np.stack([_project(t2[idx, k], axis) for k in range(3)], axis=1))
        result[idx[hit]] = 3
    idx = np.flatnonzero(~apart & ~coplanar)
    if len(idx):
        best = np.zeros(len(idx), dtype=np.int8)
        signs1 = (sd[idx], se[idx], sf[idx])
        signs2 = (sa[idx], sb[idx], sc[idx])
        for k in range(3):
            best = np.maximum(best, _segment_triangle(t2[idx, k], t2[idx, (k + 1) % 3], a[idx], b[idx], c[idx],
                                                      signs1[k], signs1[(k + 1) % 3]))
            best = np.maximum(best, _segment_triangle(t1[idx, k], t1[idx, (k + 1) % 3], d[idx], e[idx], f[idx],
                                                      signs2[k], signs2[(k + 1) % 3]))
        result[idx] = best
    return result


def _one_shared(v, a, b, c, d) -> np.ndarray:
    """頂点 v を共有する組 (v, a, b) と (v, c, d)。0 なし / 1 接触 / 2 貫通。"""
    result = np.maximum(_segment_triangle(a, b, v, c, d), _segment_triangle(c, d, v, a, b))
    # 共有頂点から出る辺が相手の平面内にあるとき: 相手の楔に入れば重なる
    for point, ray1, ray2 in ((a, c, d), (b, c, d), (c, a, b), (d, a, b)):
        in_plane = np.flatnonzero(orient3d(v, ray1, ray2, point) == 0)
        if len(in_plane):
            axis = _drop_axis(np.cross(ray1[in_plane] - v[in_plane], ray2[in_plane] - v[in_plane]))
            inside = _in_wedge_2d(_project(v[in_plane], axis), _project(ray1[in_plane], axis),
                                  _project(ray2[in_plane], axis), _project(point[in_plane], axis))
            result[in_plane[inside]] = np.maximum(result[in_plane[inside]], 1)
    return result


def _two_shared(v, w, a, b) -> np.ndarray:
    """辺 (v, w) を共有する組。共面で同じ側なら折り返し(1)、それ以外は 0。"""
    result = np.zeros(len(v), dtype=np.int8)
    coplanar = np.flatnonzero(orient3d(v, w, a, b) == 0)
    if len(coplanar):
        axis = _drop_axis(np.cross(w[coplanar] - v[coplanar], a[coplanar] - v[coplanar]))
        V, W = _project(v[coplanar], axis), _project(w[coplanar], axis)
        sa = orient2d(V, W, _project(a[coplanar], axis))
        sb = orient2d(V, W, _project(b[coplanar], axis))
        result[coplanar[sa * sb > 0]] = 1
    return result


def triangle_intersections(vertices: np.ndarray, faces: np.ndarray) -> dict:
    """自己交差する三角形の組を返す。

    戻り値: `dict(pairs=[(i, j, kind), ...], candidates=int, counts={kind: n})`。
    `faces` の頂点添字が同じなら同じ頂点とみなす(座標一致で溶接済みの前提。溶接前の別添字の同一座標は
    共有とみなさず、接触として報告する)。
    """
    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces)
    if vertices.ndim != 2 or vertices.shape[1] != 3 or faces.ndim != 2 or faces.shape[1] != 3:
        raise ValueError('頂点・三角形の配列が不正')
    if not np.isfinite(vertices).all():
        raise ValueError('頂点に有限でない座標がある')
    tri = vertices[faces]
    lo, hi = tri.min(axis=1), tri.max(axis=1)
    i, j = _candidate_pairs(lo, hi)
    pairs = []
    counts = {kind: 0 for kind in KINDS}
    for start in range(0, len(i), _CHUNK):
        ci, cj = i[start:start + _CHUNK], j[start:start + _CHUNK]
        fi, fj = faces[ci], faces[cj]
        shared = (fi[:, :, None] == fj[:, None, :])
        shared_count = shared.any(axis=2).sum(axis=1)
        # 頂点共有なし
        sel = np.flatnonzero(shared_count == 0)
        if len(sel):
            code = _no_shared(tri[ci[sel]], tri[cj[sel]])
            _collect(pairs, counts, ci[sel], cj[sel], code, {1: 'touch', 2: 'cross', 3: 'coplanar'})
        # 1 頂点共有: 共有頂点を先頭に並べ替える
        sel = np.flatnonzero(shared_count == 1)
        if len(sel):
            si, sj = shared[sel].any(axis=2).argmax(axis=1), shared[sel].any(axis=1).argmax(axis=1)
            order_i = (si[:, None] + np.arange(3)) % 3
            order_j = (sj[:, None] + np.arange(3)) % 3
            ti = np.take_along_axis(tri[ci[sel]], order_i[:, :, None], axis=1)
            tj = np.take_along_axis(tri[cj[sel]], order_j[:, :, None], axis=1)
            code = _one_shared(ti[:, 0], ti[:, 1], ti[:, 2], tj[:, 1], tj[:, 2])
            _collect(pairs, counts, ci[sel], cj[sel], code, {1: 'touch', 2: 'cross'})
        # 辺共有
        sel = np.flatnonzero(shared_count == 2)
        if len(sel):
            free_i = np.argmin(shared[sel].any(axis=2), axis=1)
            free_j = np.argmin(shared[sel].any(axis=1), axis=1)
            fi_sel = fi[sel]
            rows = np.arange(len(sel))
            a = tri[ci[sel]][rows, free_i]
            b = tri[cj[sel]][rows, free_j]
            v_index = fi_sel[rows, (free_i + 1) % 3]
            w_index = fi_sel[rows, (free_i + 2) % 3]
            code = _two_shared(vertices[v_index], vertices[w_index], a, b)
            _collect(pairs, counts, ci[sel], cj[sel], code, {1: 'fold'})
        sel = np.flatnonzero(shared_count == 3)
        if len(sel):
            _collect(pairs, counts, ci[sel], cj[sel], np.ones(len(sel), dtype=np.int8), {1: 'duplicate'})
    return dict(pairs=pairs, candidates=int(len(i)), counts=counts)


def _collect(pairs, counts, i, j, code, names):
    for row in np.flatnonzero(code):
        kind = names[int(code[row])]
        counts[kind] += 1
        pairs.append((int(i[row]), int(j[row]), kind))

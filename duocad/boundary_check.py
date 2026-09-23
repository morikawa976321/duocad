"""面境界の折れ線(pcurve の離散点列)どうしの交差・接触・重なりを、固定 ε なしで判定する。

OCP に依存しない。入力は「面 → wire → 辺の利用箇所(use)」の階層に並べた 2D 点列で、
`pack` で 1 本の配列に詰め、`check` が全対を判定する。

判定の数値規律:

- 向き判定(orient2d)は浮動小数点で計算し、Shewchuk の誤差境界 (3+16ε)ε·(|ad·bc 項|の和) を
  下回って符号が確定しない組だけ整数演算(float は 2 進有理数なので厳密に整数へ写せる)へ進む。
  固定 ε の近似判定はしない。厳密演算が保証するのは「渡された浮動小数点座標の線分」に対する判定であり、
  元 CAD の厳密性ではない。
- 正当に端点を共有してよい線分の組は **wire の構造と CAD の頂点**で決める。座標が近いだけの点を
  同一視しない。(a) 同じ use で隣り合う線分、(b) wire で連続する use の接続端点(OCCT の healer が
  厳密一致させる)、(c) 同じ面の**別の wire** で同じ CAD 頂点に着く use の終端どうし(面の内側に置かれた
  辺が外周の頂点に着く場合など。healer はこれを揃えないので pcurve の評価誤差ぶん、例えば 1e-16 だけ
  ずれることがある。同じ wire の連続しない use の頂点共有は周期面の seam・u=0/2π の両端なので数えない)。(c) で座標が一致しない組は交差の対象から外し、件数と最大のずれを診断に残す。
  (b) の端点が厳密一致せず CAD 頂点も共有しなければ `open` として報告する(成功にしない)。
- 面積や角度の小ささを理由に交差を捨てない(OCCT の FaceChecker は捨てる。docs/TOPOPT_GAP_CAUSALITY.md)。
"""
from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np

_EPS = np.finfo(np.float64).eps
#: Shewchuk, "Adaptive Precision Floating-Point Arithmetic", orient2d の ccwerrboundA。
_ORIENT2D_BOUND = (3.0 + 16.0 * _EPS) * _EPS

#: 報告する種類。`open` 以外は共有辺の再分割で解消しうる(`open` は CAD 側の接続が無い)。
KINDS = ('cross', 'touch', 'overlap', 'fold', 'open')
FIXABLE_KINDS = frozenset(('cross', 'touch', 'overlap', 'fold'))


class Packed:
    """`check` が読む詰め込み済みの点列。`pack` か `from_arrays` で作る。

    points     : (N, 2) float64。全 use の点を use の順に連結したもの
    use_offset : (U+1,) int64。use u の点は points[use_offset[u]:use_offset[u+1]]
    use_face   : (U,) int64。use が属する面の番号(呼び出し側の付番をそのまま持つ)
    use_edge   : (U,) int64。CAD 辺の識別子(呼び出し側の付番)
    use_forward: (U,) bool。wire の走査で点列を先頭→末尾へ進むなら True(REVERSED なら False)
    use_internal:(U,) bool。INTERNAL の辺(接続の期待から外す)
    use_next   : (U,) int64。同じ wire で次に来る use(巡回。1 辺の wire では自分自身)
    use_vertex : (U, 2) int64。点列の先頭・末尾に当たる CAD 頂点の識別子(不明なら -1)
    use_wire   : (U,) int64。面内の wire 番号((c) の判定は別の wire どうしにだけ適用する)
    """

    def __init__(self, points, use_offset, use_face, use_edge, use_forward, use_internal, use_next, use_vertex,
                 use_wire):
        self.points = points
        self.use_offset = use_offset
        self.use_face = use_face
        self.use_edge = use_edge
        self.use_forward = use_forward
        self.use_internal = use_internal
        self.use_next = use_next
        self.use_vertex = use_vertex
        self.use_wire = use_wire
        if not np.isfinite(points).all():
            raise ValueError('境界の点列に有限でない座標がある')

    @classmethod
    def from_arrays(cls, chunks: Sequence[np.ndarray], use_face, use_edge, use_forward, use_internal, use_next,
                    use_vertex=None, use_wire=None):
        counts = np.fromiter((len(c) for c in chunks), dtype=np.int64, count=len(chunks))
        offsets = np.concatenate(([0], np.cumsum(counts)))
        points = np.concatenate(chunks) if len(chunks) else np.zeros((0, 2))
        vertex = np.full((len(counts), 2), -1, dtype=np.int64) if use_vertex is None \
            else np.asarray(use_vertex, dtype=np.int64).reshape(-1, 2)
        wire = np.zeros(len(counts), dtype=np.int64) if use_wire is None else np.asarray(use_wire, dtype=np.int64)
        return cls(np.ascontiguousarray(points, dtype=np.float64).reshape(-1, 2), offsets,
                   np.asarray(use_face, dtype=np.int64), np.asarray(use_edge, dtype=np.int64),
                   np.asarray(use_forward, dtype=bool), np.asarray(use_internal, dtype=bool),
                   np.asarray(use_next, dtype=np.int64), vertex, wire)

    @property
    def use_count(self) -> int:
        return len(self.use_face)

    def use_points(self, use: int) -> np.ndarray:
        return self.points[self.use_offset[use]:self.use_offset[use + 1]]


def pack(faces: Iterable[Sequence[Sequence[dict]]], face_ids: Sequence[int] | None = None) -> Packed:
    """`faces[f][w]` は wire w の use の列。各 use は `dict(edge, points, forward, internal=False, vertices=None)`。

    `points` は (n, 2) に変換できる列。`vertices` は点列の先頭・末尾の CAD 頂点の識別子(省略可)。
    `face_ids` を渡すと面番号にそれを使う(省略時は 0 始まりの位置)。
    """
    chunks, use_face, use_edge, use_forward, use_internal, use_next, use_vertex, use_wire = [], [], [], [], [], [], [], []
    for position, wires in enumerate(faces):
        face = position if face_ids is None else int(face_ids[position])
        for wire_index, wire in enumerate(wires):
            first = len(use_face)
            for use in wire:
                chunks.append(np.ascontiguousarray(use['points'], dtype=np.float64).reshape(-1, 2))
                use_face.append(face)
                use_wire.append(wire_index)
                use_edge.append(int(use['edge']))
                use_forward.append(bool(use['forward']))
                use_internal.append(bool(use.get('internal', False)))
                use_next.append(len(use_face))
                vertices = use.get('vertices')
                use_vertex.append((-1, -1) if vertices is None else (int(vertices[0]), int(vertices[1])))
            if len(use_face) > first:
                use_next[-1] = first
    return Packed.from_arrays(chunks, use_face, use_edge, use_forward, use_internal, use_next, use_vertex, use_wire)


# --------------------------------------------------------------------------------------
# 向き判定
# --------------------------------------------------------------------------------------
def _integers(values: Sequence[float]) -> list:
    """float の列を共通の 2 冪分母に揃えた整数の列にする(float は 2 進有理数なので損失なし)。"""
    ratios = [float(v).as_integer_ratio() for v in values]
    common = max(d for _, d in ratios)
    return [n * (common // d) for n, d in ratios]


def orient2d_exact(ax, ay, bx, by, cx, cy) -> int:
    """(a, b, c) の向きの符号を整数演算で厳密に求める(+1 反時計回り / -1 時計回り / 0 共線)。"""
    ax, ay, bx, by, cx, cy = _integers((ax, ay, bx, by, cx, cy))
    det = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return (det > 0) - (det < 0)


def orient2d(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """行ごとの向きの符号 (M,) int8。浮動小数点で確定しない行だけ厳密演算へ落とす。

    どちらかの積の因子が厳密に 0 なら(座標が一致する軸に沿った線分など)その積は厳密に 0 で、
    det の符号はもう一方の積の符号そのもの —— この場合は誤差境界を待たず確定させる。
    """
    bax, bay = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
    cax, cay = c[:, 0] - a[:, 0], c[:, 1] - a[:, 1]
    detleft = bax * cay
    detright = bay * cax
    det = detleft - detright
    sign = np.sign(det).astype(np.int8)
    left_zero = (bax == 0) | (cay == 0)
    right_zero = (bay == 0) | (cax == 0)
    certain = left_zero | right_zero | (np.abs(det) > _ORIENT2D_BOUND * (np.abs(detleft) + np.abs(detright)))
    for row in np.flatnonzero(~certain):
        sign[row] = orient2d_exact(a[row, 0], a[row, 1], b[row, 0], b[row, 1], c[row, 0], c[row, 1])
    return sign


# --------------------------------------------------------------------------------------
# 検査本体
# --------------------------------------------------------------------------------------
def _candidate_pairs(face_of_seg: np.ndarray, lo: np.ndarray, hi: np.ndarray):
    """外接矩形が重なる同じ面の線分の組 (i, j), i<j。x の掃引で候補を絞り、y で落とす。

    面ごとに x をずらして 1 本の掃引にする。同じ定数の加算は単調なので、ずらした後の比較が
    元の比較より狭くなることはない(候補の取りこぼしが起きない)。
    """
    span = float(hi[:, 0].max() - lo[:, 0].min()) + 1.0
    shift = face_of_seg.astype(np.float64) * span * 2.0
    lo_x = lo[:, 0] + shift
    hi_x = hi[:, 0] + shift
    order = np.argsort(lo_x, kind='stable')
    lo_sorted = lo_x[order]
    upper = np.searchsorted(lo_sorted, hi_x[order], side='right')
    base = np.arange(len(order))
    counts = np.maximum(upper - base - 1, 0)
    total = int(counts.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    second = np.repeat(base + 1, counts) + (np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts))
    i = order[np.repeat(base, counts)]
    j = order[second]
    keep = (face_of_seg[i] == face_of_seg[j]) & (lo[j, 1] <= hi[i, 1]) & (lo[i, 1] <= hi[j, 1])
    i, j = i[keep], j[keep]
    swap = i > j
    return np.where(swap, j, i), np.where(swap, i, j)


def _group_pairs(keys: np.ndarray):
    """同じ key を持つ要素の全組 (a, b), a<b(要素番号)。"""
    order = np.argsort(keys, kind='stable')
    sorted_keys = keys[order]
    starts = np.flatnonzero(np.concatenate(([True], sorted_keys[1:] != sorted_keys[:-1])))
    sizes = np.diff(np.concatenate((starts, [len(keys)])))
    position = np.arange(len(keys)) - np.repeat(starts, sizes)
    remaining = np.repeat(sizes, sizes) - position - 1
    total = int(remaining.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    first = np.repeat(np.arange(len(keys)), remaining)
    second = first + 1 + (np.arange(total) - np.repeat(np.cumsum(remaining) - remaining, remaining))
    a, b = order[first], order[second]
    return np.minimum(a, b), np.maximum(a, b)


def check(packed: Packed, faces: Iterable[int] | None = None) -> dict:
    """面ごとの境界折れ線を検査し、問題の一覧を返す。

    戻り値: `dict(issues=[...], faces_checked, segments, zero_length_segments, endpoint_mismatches, endpoint_gap_max)`。
    各 issue は `dict(kind, face, edges=[edge_a, edge_b], a=dict(use, segment), b=dict(use, segment), point=[x, y] | None)`
    (`segment` は use 内の 0 始まりの線分番号)。`open` の issue は `a`/`b` の代わりに `uses` を持つ。
    `endpoint_mismatches` は CAD 頂点を共有するのに座標が一致しない終端の組の数(交差の対象から外した数)。
    """
    if faces is None:
        uses = np.arange(packed.use_count)
        face_count = len(np.unique(packed.use_face))
    else:
        wanted = np.asarray(sorted(set(int(f) for f in faces)), dtype=np.int64)
        uses = np.flatnonzero(np.isin(packed.use_face, wanted))
        face_count = len(wanted)
    issues: list = []
    result = dict(issues=issues, faces_checked=face_count, segments=0, zero_length_segments=0,
                  endpoint_mismatches=0, endpoint_gap_max=0.0)
    if len(uses) == 0:
        return result
    offset = packed.use_offset
    n_points = offset[uses + 1] - offset[uses]

    # --- 線分(長さ 0 は除く)。use 内の線分 s は点 s, s+1 を結ぶ
    seg_counts = np.maximum(n_points - 1, 0)
    seg_use = np.repeat(uses, seg_counts)
    seg_index = np.arange(seg_counts.sum()) - np.repeat(np.cumsum(seg_counts) - seg_counts, seg_counts)
    first = np.repeat(offset[uses], seg_counts) + seg_index
    P_all = packed.points[first]
    Q_all = packed.points[first + 1]
    keep = (P_all != Q_all).any(axis=1)
    result['zero_length_segments'] = int((~keep).sum())
    seg_use, seg_index, P, Q = seg_use[keep], seg_index[keep], P_all[keep], Q_all[keep]
    result['segments'] = int(len(P))
    # 各 use の(長さ 0 を除いた)最初と最後の線分行
    first_row = np.full(packed.use_count, -1, dtype=np.int64)
    last_row = np.full(packed.use_count, -1, dtype=np.int64)
    if len(P):
        starts = np.flatnonzero(np.concatenate(([True], seg_use[1:] != seg_use[:-1])))
        ends = np.concatenate((starts[1:] - 1, [len(P) - 1]))
        first_row[seg_use[starts]] = starts
        last_row[seg_use[starts]] = ends

    # --- (b) wire で連続する use の接続
    u = uses[~packed.use_internal[uses]]
    v = packed.use_next[u]
    u = u[~packed.use_internal[v]]
    v = packed.use_next[u]
    n_u = offset[u + 1] - offset[u]
    n_v = offset[v + 1] - offset[v]
    short = (n_u < 2) | (n_v < 2)
    for k in np.flatnonzero(short):
        issues.append(dict(kind='open', face=int(packed.use_face[u[k]]), uses=[int(u[k]), int(v[k])],
                           edges=sorted({int(packed.use_edge[u[k]]), int(packed.use_edge[v[k]])}), reason='点が2つ未満の use'))
    u, v = u[~short], v[~short]
    end_u = np.where(packed.use_forward[u], offset[u + 1] - 1, offset[u])
    start_v = np.where(packed.use_forward[v], offset[v], offset[v + 1] - 1)
    vertex_end_u = np.where(packed.use_forward[u], packed.use_vertex[u, 1], packed.use_vertex[u, 0])
    vertex_start_v = np.where(packed.use_forward[v], packed.use_vertex[v, 0], packed.use_vertex[v, 1])
    equal = (packed.points[end_u] == packed.points[start_v]).all(axis=1)
    same_vertex = (vertex_end_u >= 0) & (vertex_end_u == vertex_start_v)
    for k in np.flatnonzero(~equal & ~same_vertex):
        issues.append(dict(kind='open', face=int(packed.use_face[u[k]]), uses=[int(u[k]), int(v[k])],
                           edges=sorted({int(packed.use_edge[u[k]]), int(packed.use_edge[v[k]])}),
                           gap=float(np.linalg.norm(packed.points[end_u[k]] - packed.points[start_v[k]])),
                           point=packed.points[end_u[k]].tolist()))
    connected = equal | same_vertex
    u, v, end_u, start_v, equal = u[connected], v[connected], end_u[connected], start_v[connected], equal[connected]
    ru = np.where(packed.use_forward[u], last_row[u], first_row[u])
    rv = np.where(packed.use_forward[v], first_row[v], last_row[v])
    conn_a = np.minimum(ru, rv)
    conn_b = np.maximum(ru, rv)
    conn_shared = np.where(equal, end_u, -1)
    conn_gap = np.where(equal, 0.0, np.linalg.norm(packed.points[end_u] - packed.points[start_v], axis=1))

    # --- (c) 同じ面で同じ CAD 頂点に着く use の終端どうし
    known = np.flatnonzero((packed.use_vertex[uses] >= 0).any(axis=1) & (n_points >= 2))
    if len(known):
        ends_use = np.concatenate((uses[known], uses[known]))
        ends_vertex = np.concatenate((packed.use_vertex[uses[known], 0], packed.use_vertex[uses[known], 1]))
        ends_point = np.concatenate((offset[uses[known]], offset[uses[known] + 1] - 1))
        ends_row = np.concatenate((first_row[uses[known]], last_row[uses[known]]))
        valid = (ends_vertex >= 0) & (ends_row >= 0)
        ends_use, ends_vertex, ends_point, ends_row = ends_use[valid], ends_vertex[valid], ends_point[valid], ends_row[valid]
        vertex_span = int(ends_vertex.max()) + 1 if len(ends_vertex) else 1
        a, b = _group_pairs(packed.use_face[ends_use] * vertex_span + ends_vertex)
        # 同じ wire の中は (b) が扱う。同じ wire で連続しない use が頂点を共有するのは周期面の seam や
        # u=0/2π の両端で、UV では周期ぶん離れていて接続ではない(閉じた辺の自分自身の両端も同じ)。
        different_wire = packed.use_wire[ends_use[a]] != packed.use_wire[ends_use[b]]
        a, b = a[different_wire], b[different_wire]
        equal_point = (packed.points[ends_point[a]] == packed.points[ends_point[b]]).all(axis=1)
        conn_a = np.concatenate((conn_a, np.minimum(ends_row[a], ends_row[b])))
        conn_b = np.concatenate((conn_b, np.maximum(ends_row[a], ends_row[b])))
        conn_shared = np.concatenate((conn_shared, np.where(equal_point, ends_point[a], -1)))
        conn_gap = np.concatenate((conn_gap, np.where(
            equal_point, 0.0, np.linalg.norm(packed.points[ends_point[a]] - packed.points[ends_point[b]], axis=1))))
    valid = (conn_a >= 0) & (conn_a != conn_b)
    conn_a, conn_b, conn_shared, conn_gap = conn_a[valid], conn_b[valid], conn_shared[valid], conn_gap[valid]
    if len(P) == 0:
        return result

    # --- 候補の組
    lo = np.minimum(P, Q)
    hi = np.maximum(P, Q)
    i, j = _candidate_pairs(packed.use_face[seg_use], lo, hi)
    n_rows = len(P) + 1
    # 正当に端点を共有する組: (a) 同じ use で(長さ 0 を除いて)隣り合う線分、(b)(c) 接続する終端線分どうし
    adjacent = (seg_use[i] == seg_use[j]) & (j - i == 1)
    pair_keys = i * n_rows + j
    conn_keys = conn_a * n_rows + conn_b
    shared_of = {}
    gap_of = {}
    for key, shared, gap in zip(conn_keys.tolist(), conn_shared.tolist(), conn_gap.tolist()):
        # 同じ組が (b) と (c) の両方から来たら、座標が一致している側の情報を残す
        if key not in shared_of or shared >= 0:
            shared_of[key] = shared
            gap_of[key] = gap
    connected_pair = np.isin(pair_keys, conn_keys) if len(conn_keys) else np.zeros(len(i), dtype=bool)
    legit = adjacent | connected_pair
    mismatched = [key for key, shared in shared_of.items() if shared < 0]
    result['endpoint_mismatches'] = len(mismatched)
    result['endpoint_gap_max'] = float(max((gap_of[key] for key in mismatched), default=0.0))

    # 1) 正当な共有端点の組: 折り返し(共線で同じ側へ戻る)だけを見る(座標が一致する組だけ)
    li, lj = i[legit], j[legit]
    if len(li):
        S = P[lj].copy()
        F1 = P[li].copy()
        F2 = Q[lj].copy()
        keys = pair_keys[legit]
        shared_index = np.full(len(li), -2, dtype=np.int64)
        connected_rows = np.flatnonzero(~adjacent[legit])
        shared_index[connected_rows] = [shared_of[int(key)] for key in keys[connected_rows]]
        conn_rows = np.flatnonzero(shared_index >= 0)
        if len(conn_rows):
            S[conn_rows] = packed.points[shared_index[conn_rows]]
            a_rows, b_rows = li[conn_rows], lj[conn_rows]
            F1[conn_rows] = np.where((P[a_rows] == S[conn_rows]).all(axis=1)[:, None], Q[a_rows], P[a_rows])
            F2[conn_rows] = np.where((P[b_rows] == S[conn_rows]).all(axis=1)[:, None], Q[b_rows], P[b_rows])
        testable = np.flatnonzero(shared_index != -1)
        collinear = testable[orient2d(S[testable], F1[testable], F2[testable]) == 0]
        if len(collinear):
            axis = np.where((F1[collinear, 0] != S[collinear, 0]) | (F2[collinear, 0] != S[collinear, 0]), 0, 1)
            d1 = F1[collinear, axis] - S[collinear, axis]
            d2 = F2[collinear, axis] - S[collinear, axis]
            for row in collinear[np.sign(d1) * np.sign(d2) > 0]:
                issues.append(_issue('fold', packed, seg_use, seg_index, li[row], lj[row], S[row]))

    # 2) それ以外の組: 交差・接触・重なり
    ni, nj = i[~legit], j[~legit]
    if len(ni):
        p1, p2, p3, p4 = P[ni], Q[ni], P[nj], Q[nj]
        o1 = orient2d(p3, p4, p1)
        o2 = orient2d(p3, p4, p2)
        o3 = orient2d(p1, p2, p3)
        o4 = orient2d(p1, p2, p4)
        cross = (o1 * o2 < 0) & (o3 * o4 < 0)
        collinear = (o1 == 0) & (o2 == 0) & (o3 == 0) & (o4 == 0)
        inside = lambda pt, a, b: (pt >= np.minimum(a, b)).all(axis=1) & (pt <= np.maximum(a, b)).all(axis=1)
        touch = ((o1 == 0) & inside(p1, p3, p4)) | ((o2 == 0) & inside(p2, p3, p4)) \
            | ((o3 == 0) & inside(p3, p1, p2)) | ((o4 == 0) & inside(p4, p1, p2))
        for row in np.flatnonzero(cross):
            issues.append(_issue('cross', packed, seg_use, seg_index, ni[row], nj[row],
                                 _crossing_point(p1[row], p2[row], p3[row], p4[row])))
        for row in np.flatnonzero(collinear):
            a, b = ni[row], nj[row]
            axis = 0 if lo[a, 0] != hi[a, 0] else 1
            start = max(lo[a, axis], lo[b, axis])
            end = min(hi[a, axis], hi[b, axis])
            if end > start:
                issues.append(_issue('overlap', packed, seg_use, seg_index, a, b, None))
            elif end == start:
                issues.append(_issue('touch', packed, seg_use, seg_index, a, b, None))
        for row in np.flatnonzero(touch & ~cross & ~collinear):
            issues.append(_issue('touch', packed, seg_use, seg_index, ni[row], nj[row], None))
    return result


def _crossing_point(p1, p2, p3, p4):
    """診断用の交点(浮動小数点の近似。判定には使わない)。"""
    d = (p2[0] - p1[0]) * (p4[1] - p3[1]) - (p2[1] - p1[1]) * (p4[0] - p3[0])
    if d == 0:
        return None
    t = ((p3[0] - p1[0]) * (p4[1] - p3[1]) - (p3[1] - p1[1]) * (p4[0] - p3[0])) / d
    return [float(p1[0] + t * (p2[0] - p1[0])), float(p1[1] + t * (p2[1] - p1[1]))]


def _issue(kind, packed, seg_use, seg_index, a, b, point):
    ua, ub = int(seg_use[a]), int(seg_use[b])
    return dict(kind=kind, face=int(packed.use_face[ua]),
                edges=sorted({int(packed.use_edge[ua]), int(packed.use_edge[ub])}),
                a=dict(use=ua, segment=int(seg_index[a])), b=dict(use=ub, segment=int(seg_index[b])),
                point=None if point is None else [float(point[0]), float(point[1])])


def fixable_edges(issues: Iterable[dict]) -> set:
    """再分割の対象にする CAD 辺の集合(`open` は含めない)。"""
    return {edge for issue in issues if issue['kind'] in FIXABLE_KINDS for edge in issue['edges']}

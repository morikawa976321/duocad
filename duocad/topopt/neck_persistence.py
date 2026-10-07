"""完成形のつなぎ目の幅の診断(#117)。保証ではない —— 表示は「幅不足の疑い / 未確定 / 疑いは観測されなかった」の3値で、「適合」は出さない。

定義と根拠は [TOPOPT_NECK_RULE_120.md](../../docs/TOPOPT_NECK_RULE_120.md) §3。要点:

- 完成形を標本化し、内側の各標本に境界までの距離 f を与え、材料が増える向き(r を大きい方から下げる)の上位集合 {f ≥ r} を立方複体の
  フィルトレーションにして持続ホモロジーを取る(gudhi、頂点に値を置く = 6連結の双対複体)。材料の外は入れない(値 ∞)。
- **材料の輪のくびれ**は、材料が増える向きで輪が閉じる半径に生まれて消えない組(本質類。外を入れないので死の値が ∞)になる。寿命の閾値は掛けない。
- **木のくびれ**(太い塊どうしを細い首がつなぐ)は、2成分が合流する有限の組の死になる。寿命 > 2ε のときだけ区間を出し、それ以外は「未確定」。
- 誤差の総量 ε: 二値 EDT(背景標本の中心までの距離)は真の距離より大きく、その差は s√3/2 以内(背景が標本1個ぶん以上の幅を持つとき)。
  標本化による上位集合のずれも s√3/2 以内。合わせて ε = √3·s。背景の細い隙間(標本1個未満)ではこの境界が成り立たないので、
  基準検査(tests/test_neck_persistence.py)で真値の包含を確かめた範囲でだけ使う。
- 保持ソリッドとその周り(保持セルを (2m−1) 立方で膨らませた範囲)は最初から入れる(値 −∞)。「保持経由は太い」(S(m) 以来の決定)。
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
from scipy import ndimage

from . import voxelize

#: 標本数の上限。超えるときは粗くせず「未確定」(仕様書 §3)。距離変換と gudhi の複体で 1 標本あたり数十バイト。
MAX_SAMPLES = 16_000_000
EPSILON_PER_SAMPLE = math.sqrt(3.0)
#: 「微小片」とみなす小さい側の体積(標本数)。(2s)³。
MICRO_PIECE_SAMPLES = 8
_KEEP_VALUE = -1.0e18


def inspect_mesh(vertices, faces, *, spacing: float, required_mm: float, min_width_cells: int,
                 keep: Optional[tuple] = None, voxel: Optional[tuple] = None) -> dict:
    """完成メッシュを探索格子の 1/2 で標本化して診断する。`keep=(mask, grid)` は探索格子の保持セル、`voxel=(mask, grid)` は探索が返した
    ボクセル形(端の探針に使う)。戻り値は `inspect_samples` と同じ形(JSON 化できる辞書)。"""
    vertices = np.asarray(vertices, dtype=float)
    faces = np.asarray(faces)
    s = float(spacing) / 2.0
    lo, hi = vertices.min(axis=0), vertices.max(axis=0)
    shape = tuple(int(math.ceil((b - a) / s)) + 4 for a, b in zip(lo, hi))
    if math.prod(shape) > MAX_SAMPLES:
        return _undetermined(required_mm, s, f'標本数 {math.prod(shape)} が上限 {MAX_SAMPLES} を超える')
    grid = voxelize.Grid(tuple(lo - 2 * s), s, shape)
    soup = voxelize.TriangleSoup(positions=vertices, triangles=faces, face_index=np.zeros(len(faces), dtype=np.int64))
    inside = voxelize.occupancy(soup, grid, allow_empty=True)[0]
    return inspect_samples(inside, grid, required_mm=required_mm, min_width_cells=min_width_cells, keep=keep, voxel=voxel)


def inspect_samples(inside: np.ndarray, grid: voxelize.Grid, *, required_mm: float, min_width_cells: int,
                    keep: Optional[tuple] = None, voxel: Optional[tuple] = None) -> dict:
    """標本の内外(外周は外)から診断する。

        {"status": "suspect" | "undetermined" | "not_observed",
         "required_mm", "sampling_mm", "epsilon_mm",
         "events": [{"kind": "loop_neck" | "tree_neck", "diameter_mm": [下端, 上端], "lifetime_mm": float|None,
                     "verdict": "suspect" | "undetermined", "reason": str|None, "position_mm": [x,y,z]}, ...],
         "micro_pieces": int,                  # 小さい側が MICRO_PIECE_SAMPLES 未満の合流(数だけ)
         "thinning_reference": [...],          # 輪の誕生(侵食で生まれる輪 = 板の中央の薄い所)。参考
         "end_probe_mm": float|None,           # 端の参考値(探針の最小の直径)。探針ゼロなら None
         "reason": str|None}
    """
    s = float(grid.spacing)
    eps = EPSILON_PER_SAMPLE * s
    d = float(required_mm)
    m = int(min_width_cells)
    f = ndimage.distance_transform_edt(inside) * s
    zone = _keep_zone(grid, inside.shape, keep, m) if keep is not None else None
    values = np.where(inside, -f, np.inf)
    if zone is not None:
        values[zone & inside] = _KEEP_VALUE

    import gudhi

    complex_ = gudhi.CubicalComplex(vertices=values)
    complex_.persistence(homology_coeff_field=2, min_persistence=0)
    regular, _ = complex_.vertices_of_persistence_pairs()
    flat = values.ravel(order='F')

    def position(index: int) -> list:
        ijk = np.array(np.unravel_index(int(index), values.shape, order='F'))
        return (np.asarray(grid.origin) + (ijk + 0.5) * s).tolist()

    events, thinning = [], []
    micro = 0
    labels_at: dict = {}
    for dim, pairs in enumerate(regular[:2]):
        for birth, death in np.asarray(pairs, dtype=np.int64).reshape(-1, 2):
            b, dv = flat[birth], flat[death]
            if b <= _KEEP_VALUE:
                continue
            r_birth = -b
            if dim == 1 and math.isinf(dv):
                # 材料の輪: 輪が閉じる半径 = くびれの半径。寿命の閾値なし。
                if 2 * (r_birth - eps) < d:
                    events.append(_event('loop_neck', r_birth, None, eps, d, position(birth)))
            elif dim == 1:
                r_death = -dv
                if 2 * r_death < d:
                    thinning.append(dict(diameter_mm=2 * r_death, position_mm=position(death)))
            elif dim == 0 and not math.isinf(dv) and dv > _KEEP_VALUE:
                r_death = -dv
                if 2 * (r_death - eps) >= d:
                    continue
                lifetime = r_birth - r_death
                if lifetime <= 2 * eps and _younger_volume(f, inside, zone, r_death, birth, labels_at) < MICRO_PIECE_SAMPLES:
                    micro += 1
                    continue
                events.append(_event('tree_neck', r_death, lifetime, eps, d, position(death)))
    if any(e['verdict'] == 'suspect' for e in events):
        status = 'suspect'
    elif events or micro:
        status = 'undetermined'
    else:
        status = 'not_observed'
    return dict(status=status, required_mm=d, sampling_mm=s, epsilon_mm=eps, events=events, micro_pieces=micro,
                thinning_reference=thinning, end_probe_mm=_end_probe(f, grid, voxel, d, keep), reason=None)


def _event(kind: str, r: float, lifetime: Optional[float], eps: float, d: float, position: list) -> dict:
    low, high = 2 * (r - eps), 2 * (r + eps)
    if lifetime is not None and lifetime <= 2 * eps:
        verdict, reason = 'undetermined', '寿命が 2ε 以下で雑音と区別できない'
    elif high < d:
        verdict, reason = 'suspect', None
    else:
        verdict, reason = 'undetermined', '区間が要求幅をまたぐ'
    return dict(kind=kind, diameter_mm=[low, high], lifetime_mm=lifetime, verdict=verdict, reason=reason, position_mm=position)


def _younger_volume(f, inside, zone, r_death, birth, cache) -> int:
    """合流の直前({f > r_death})に、`birth` の標本を含む成分の標本数。"""
    if r_death not in cache:
        present = inside & (f > r_death)
        if zone is not None:
            present |= zone & inside
        cache[r_death] = ndimage.label(present)[0]
    labels = cache[r_death]
    label = labels.ravel(order='F')[birth]
    return int((labels == label).sum()) if label else 0


def _keep_zone(grid, shape, keep, m):
    """探索格子の保持セルを (2m−1) 立方で膨らませ、標本格子へ写す。"""
    mask, coarse = keep
    grown = ndimage.binary_dilation(np.asarray(mask, dtype=bool), structure=np.ones((2 * m - 1,) * 3, dtype=bool))
    return _to_samples(grown, coarse, grid, shape)


def _to_samples(mask, coarse, grid, shape):
    axes = []
    for axis in range(3):
        centre = grid.origin[axis] + (np.arange(shape[axis]) + 0.5) * grid.spacing
        index = np.floor((centre - coarse.origin[axis]) / coarse.spacing).astype(np.int64)
        axes.append(index)
    valid = [(a >= 0) & (a < n) for a, n in zip(axes, mask.shape)]
    clipped = [np.clip(a, 0, n - 1) for a, n in zip(axes, mask.shape)]
    out = mask[np.ix_(*clipped)]
    return out & valid[0][:, None, None] & valid[1][None, :, None] & valid[2][None, None, :]


def _end_probe(f, grid, voxel, d, keep):
    """端の参考値: ボクセル形で半径 d/2 の球が入っていた点(ボクセル形の EDT ≥ d/2 + 標本の半分)での、完成形の 2f の最小。
    保持セルとその隣1セルは除く。探針ゼロなら None。端面から d/2 以内の先端部は探針に入らない。"""
    if voxel is None:
        return None
    mask, coarse = voxel
    shape = f.shape
    solid = _to_samples(np.asarray(mask, dtype=bool), coarse, grid, shape)
    voxel_f = ndimage.distance_transform_edt(solid) * grid.spacing
    probe = voxel_f >= d / 2 + grid.spacing / 2
    if keep is not None:
        near = ndimage.binary_dilation(np.asarray(keep[0], dtype=bool), structure=np.ones((3, 3, 3), dtype=bool))
        probe &= ~_to_samples(near, keep[1], grid, shape)
    if not probe.any():
        return None
    return float(2 * f[probe].min())


def _undetermined(required_mm, s, reason):
    return dict(status='undetermined', required_mm=float(required_mm), sampling_mm=s, epsilon_mm=EPSILON_PER_SAMPLE * s,
                events=[], micro_pieces=0, thinning_reference=[], end_probe_mm=None, reason=reason)

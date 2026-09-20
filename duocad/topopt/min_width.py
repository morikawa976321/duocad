"""最小幅の規則(全材料セルが m×m×m の材料だけのブロックに属する)の局所計算。

`binary_search` の共有状態には触らない部品: m 立方の opening、セルを空にしたとき被覆を失うセル(巻き添え)、
複数セルの一口を抜いてよいかの幾何判定。判定と巻き添えは同じ窓の opening から出す(食い違わないように)。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from scipy import ndimage

from . import acceptance


def cube_opening(mask: np.ndarray, m: int) -> np.ndarray:
    """m 立方の構造要素による opening。立方は3軸の直線の合成なので、軸ごとの1次元の erosion → dilation に分けて
    `binary_opening` と同じ結果を出す。直接だと費用が m³ に比例し(実測 100×60×60 格子で m=32: 14秒、m=48: 218秒)、
    分けると m に比例する(m=48: 0.03秒)。"""
    out = mask
    for op in (ndimage.binary_erosion, ndimage.binary_dilation):
        for axis in range(3):
            out = op(out, structure=np.ones(tuple(m if a == axis else 1 for a in range(3)), dtype=bool))
    return out


def lost_coverage(mask: np.ndarray, region: tuple, m: int, exempt: Optional[np.ndarray] = None,
                  refill: Optional[np.ndarray] = None) -> tuple[tuple, np.ndarray]:
    """`region`(スライスの3つ組)を材料から丸ごと空にしたと仮定して、m 立方の被覆を失う材料セル。
    `(origin, lost)`: `lost` は `region` の周り(距離 m-1 以内)の真偽値、`origin` はその (0,0,0) の `mask` での添字。

    `refill`(`region` と同じ形の真偽配列)を渡すと、空にする代わりに `region` の中をその値にして数える(段階3の置換。足した
    セルが与える被覆を数え、足したセルのうち m 立方に入らないものは `region` の中の `lost` になる)。

    `exempt`(保持ソリッド由来のセル)は被覆を問わない対象外だが、材料であれば他のセルの立方には数える。
    m 立方はそのセルから距離 m-1 以内にしか広がらないので、`region` から距離 2×(m-1) の
    材料データだけで判定できる(大域を再計算しない)。`mask` が既に規則を満たす(`exempt` を除く)ことが前提。
    """
    reach = m - 1
    pad = 2 * reach
    shape = mask.shape
    lo = tuple(max(int(s.start) - pad, 0) for s in region)
    hi = tuple(min(int(s.stop) + pad, n) for s, n in zip(region, shape))
    window = np.array(mask[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]], dtype=bool, copy=True)
    window[tuple(slice(int(s.start) - l, int(s.stop) - l) for s, l in zip(region, lo))] = (
        False if refill is None else refill)
    opened = cube_opening(window, m)
    zone = tuple(
        slice(max(int(s.start) - reach - l, 0), min(int(s.stop) + reach - l, h - l))
        for s, l, h in zip(region, lo, hi)
    )
    zone_mask = window[zone]
    if exempt is not None:
        zone_mask = zone_mask & ~exempt[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]][zone]
    return tuple(z.start + l for z, l in zip(zone, lo)), zone_mask & ~opened[zone]


def casualties(mask: np.ndarray, cell: tuple, m: int, exempt: Optional[np.ndarray] = None) -> np.ndarray:
    """`cell` を抜いたとき m 立方の被覆を失う材料セルの添字(n,3)。`exempt` のセルは含めない。
    `cell` と巻き添えを抜いた形は、`exempt` が無ければ `opening(mask − {cell})` に等しい(opening は冪等なので、巻き添えは1回で確定する)。
    `exempt` があるときは、その未被覆のセルだけが opening との差になる。"""
    region = tuple(slice(int(c), int(c) + 1) for c in cell)
    origin, lost = lost_coverage(mask, region, m, exempt)
    return np.argwhere(lost) + np.asarray(origin)


def bite_verdict(padded: np.ndarray, bite: np.ndarray,
                 structure: np.ndarray) -> tuple[Optional[str], bool]:
    """複数セルの一口 `bite`(添字 (n,3)。`padded` の内側の添字)を抜いてよいかを、一口の外接箱を1セル広げた窓だけで判定する。
    `padded` は外周に空を1セル足したマスク、`structure` は6近傍。

    `(理由, 窓の中で連結と言えたか)`。理由は落ちた判定の名前(`'surface'`・`'manifold'`・`'connectivity'`)で、通れば `None`。
    - 表面: 一口の6連結成分のそれぞれが、既存の空セルに6近傍で接する(接しなければ閉じた空洞ができる)。
    - 多様体: 抜いた後、窓の内側の頂点が全て正則。
    - 連結: 一口に6近傍で接する材料が窓の中で1成分なら、抜く前に連結だったマスクは抜いた後も連結。
      偽でも割れるとは限らない(呼び出し側が全体を数え直す)。一口に接する材料が無ければ `'connectivity'`。
    """
    lo = bite.min(0)
    hi = bite.max(0) + 3
    window = padded[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]].copy()
    inside = np.zeros_like(window)
    rel = bite - lo + 1
    inside[rel[:, 0], rel[:, 1], rel[:, 2]] = True

    labels, count = ndimage.label(inside, structure=structure)
    open_side = np.unique(labels[ndimage.binary_dilation(~window, structure=structure) & inside])
    if len(open_side) < count:
        return 'surface', False
    window[inside] = False
    irregular, _ = acceptance.irregular_vertices(window)
    if irregular[1:-1, 1:-1, 1:-1].any():
        return 'manifold', False
    neighbours = ndimage.binary_dilation(inside, structure=structure) & window
    if not neighbours.any():
        return 'connectivity', False
    rest, _ = ndimage.label(window, structure=structure)
    return None, len(np.unique(rest[neighbours])) == 1

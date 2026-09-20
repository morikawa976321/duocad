"""最小幅の規則(全材料セルが m×m×m の材料だけのブロックに属する)の局所計算。

`binary_search` の共有状態には触らない部品: m 立方の opening、セルを空にしたとき被覆を失うセル(巻き添え)、
追加したセルを m 立方へ膨らませる操作、複数セルの一口を抜いてよいかの幾何判定。判定と巻き添えは同じ窓の opening から出す(食い違わないように)。
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

    `exempt`(保持ソリッド由来のセル)は被覆を問わない対象外で、`mask` にあるかどうかに関わらず常に材料として他のセルの立方に数える
    (`region` を空にしても、その中の `exempt` は材料のまま)。最終形状では保持の実形状が union で戻る(`finalize.py`)ので、
    探索で削られた保持セルも幅を与える。
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
    fixed = None if exempt is None else exempt[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
    opened = cube_opening(window if fixed is None else window | fixed, m)
    zone = tuple(
        slice(max(int(s.start) - reach - l, 0), min(int(s.stop) + reach - l, h - l))
        for s, l, h in zip(region, lo, hi)
    )
    zone_mask = window[zone]
    if fixed is not None:
        zone_mask = zone_mask & ~fixed[zone]
    return tuple(z.start + l for z, l in zip(zone, lo)), zone_mask & ~opened[zone]


def casualties(mask: np.ndarray, cell: tuple, m: int, exempt: Optional[np.ndarray] = None) -> np.ndarray:
    """`cell` を抜いたとき m 立方の被覆を失う材料セルの添字(n,3)。`exempt` のセルは含めない。
    `cell` と巻き添えを抜いた形は、`exempt` が無ければ `opening(mask − {cell})` に等しい(opening は冪等なので、巻き添えは1回で確定する)。
    `exempt` があるときは、材料を `mask ∪ exempt` として opening を取り、`exempt` のセルは被覆を問わず残す。
    `cell` 自身が `exempt` なら材料は変わらないので、巻き添えは無い。"""
    region = tuple(slice(int(c), int(c) + 1) for c in cell)
    origin, lost = lost_coverage(mask, region, m, exempt)
    return np.argwhere(lost) + np.asarray(origin)


def _box_sums(values: np.ndarray, m: int) -> np.ndarray:
    """`values` の、各位置を最小の角とする m 立方の総和。配列に収まる位置だけ(形は各軸 n−m+1)。"""
    total = np.pad(values.astype(np.int32), ((1, 0),) * 3).cumsum(0).cumsum(1).cumsum(2)
    return (total[m:, m:, m:] - total[:-m, m:, m:] - total[m:, :-m, m:] - total[m:, m:, :-m]
            + total[:-m, :-m, m:] + total[:-m, m:, :-m] + total[m:, :-m, :-m] - total[:-m, :-m, :-m])


def cover_additions(child: np.ndarray, region: tuple, design: np.ndarray, m: int,
                    exempt: Optional[np.ndarray] = None) -> None:
    """`child`(親に `region` の追加を足した形)を、`region` の中の材料セルが m 立方に入る形へ**その場で**直す。
    段階3の追加を、規則の下で閉じた操作にする(親が規則を満たせば子も満たす)。

    `region` の中の材料セル(`exempt` を除く)で m 立方に入らないものごとに、`design`(と既存の材料)に収まる m 立方のうち、
    足すセルが最も少ないもの(同数なら最小の角が辞書順で先)を足して覆う。`exempt` は `child` に無くても材料に数えるので足さない。
    m 立方が収まらないセルは足さずに取り除く(設計空間を m 立方の opening で絞ってあれば起きない)。
    影響は `region` から距離 m-1 以内なので、その窓だけで判定する。`child` が `region` の外で規則を満たす(`exempt` を除く)ことが前提。
    """
    reach = m - 1
    lo = tuple(max(int(s.start) - reach, 0) for s in region)
    hi = tuple(min(int(s.stop) + reach, n) for s, n in zip(region, child.shape))
    window = tuple(slice(l, h) for l, h in zip(lo, hi))
    cells = child[window]  # child の view: 書き込みは child に届く
    fixed = None if exempt is None else np.asarray(exempt, dtype=bool)[window]
    material = cells.copy() if fixed is None else cells | fixed
    block = tuple(slice(int(s.start) - l, int(s.stop) - l) for s, l in zip(region, lo))
    needy = cells[block] & ~cube_opening(material, m)[block]
    if fixed is not None:
        needy &= ~fixed[block]
    if not needy.any():
        return
    volume = m ** 3
    have = _box_sums(material, m)
    fits = _box_sums(np.asarray(design, dtype=bool)[window] | material, m) == volume
    limit = np.array(cells.shape) - m
    for cell in np.argwhere(needy) + np.array([s.start for s in block]):
        first, last = np.maximum(cell - reach, 0), np.minimum(cell, limit)  # このセルを含む m 立方の最小の角の範囲
        if (last >= first).all():
            span = tuple(slice(a, b + 1) for a, b in zip(first, last))
            if (have[span] == volume).any():  # 先に足した立方が覆った
                continue
            cost = np.where(fits[span], volume - have[span], volume + 1)
            pick = np.unravel_index(int(cost.argmin()), cost.shape)
            if cost[pick] <= volume:
                corner = first + np.array(pick)
                cube = tuple(slice(a, a + m) for a in corner)
                cells[cube] |= ~material[cube]
                material[cube] = True
                near_first, near_last = np.maximum(corner - reach, 0), np.minimum(corner + reach, limit)
                have[tuple(slice(a, b + 1) for a, b in zip(near_first, near_last))] = _box_sums(
                    material[tuple(slice(a, b + m) for a, b in zip(near_first, near_last))], m)
                continue
        cells[tuple(cell)] = False  # 収まる m 立方が無いセルは足さない
        material[tuple(cell)] = False


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


_SIX = ndimage.generate_binary_structure(3, 1)


def core_cells(material: np.ndarray, m: int) -> np.ndarray:
    """芯 K: セル c を最小の角とする m 立方が全部材料のセル(立方が配列に収まらない c は含めない)。"""
    core = np.asarray(material, dtype=bool)
    if min(core.shape) < m:
        return np.zeros_like(core)
    for axis in range(3):
        n = core.shape[axis]
        acc = core.copy()
        for s in range(1, m):
            head = tuple(slice(0, n - s) if a == axis else slice(None) for a in range(3))
            tail = tuple(slice(s, n) if a == axis else slice(None) for a in range(3))
            acc[head] &= core[tail]
            acc[tuple(slice(n - s, n) if a == axis else slice(None) for a in range(3))] = False
        core = acc
    return core


def _spread_down(cells: np.ndarray, m: int) -> np.ndarray:
    """`cells` の各セル x から、各軸で −[0,m) へ広げた和集合(x を含む m 立方の最小の角の集合)。"""
    out = cells
    for axis in range(3):
        n = out.shape[axis]
        acc = out.copy()
        for s in range(1, min(m, n)):
            head = tuple(slice(0, n - s) if a == axis else slice(None) for a in range(3))
            tail = tuple(slice(s, n) if a == axis else slice(None) for a in range(3))
            acc[head] |= out[tail]
        out = acc
    return out


class CoreConnectivity:
    """規則 S(m): 侵食した芯 K の6連結成分が、`exempt`(保持ソリッド由来のセル)を介したつながりまで含めて1つにまとまる。

    K_i と `exempt` の6連結成分 E_j は、K_i のどれかの立方が E_j と重なるか6近傍で接するとき結ばれる。
    立方どうしが共有1セルの斜めのつなぎ目は、R(m) は許すが K が割れるので S(m) は許さない。
    `exempt` が無ければ「K が6連結」。K が空のときは(要求するつながりが無いので)満たす。
    `exempt` は探索の間動かないので、E_j ごとの「結ばれる K の候補セル」は作るときに1回だけ求める。
    """

    def __init__(self, m: int, exempt: Optional[np.ndarray] = None) -> None:
        self.m = int(m)
        self.exempt = None if exempt is None else np.asarray(exempt, dtype=bool)
        self._links: list[tuple[tuple, np.ndarray]] = []
        if self.exempt is None or not self.exempt.any():
            return
        labels, count = ndimage.label(self.exempt, structure=_SIX)
        for j, box in enumerate(ndimage.find_objects(labels), start=1):
            lo = tuple(max(s.start - self.m, 0) for s in box)  # E_j の外周1セル + 立方が下へ伸びる m-1
            crop = tuple(slice(l, s.stop + 1) for l, s in zip(lo, box))
            component = labels[crop] == j
            reach = component | ndimage.binary_dilation(component, structure=_SIX)
            self._links.append((lo, _spread_down(reach, self.m)))

    def _material(self, mask: np.ndarray) -> np.ndarray:
        return mask if self.exempt is None else mask | self.exempt

    def _linked(self, origin: np.ndarray, shape: tuple) -> list[np.ndarray]:
        """窓(格子座標 `origin` から `shape`)の中で、E_j ごとの「結ばれる K の候補セル」。窓と重ならない E_j は含めない。"""
        found = []
        for lo, link in self._links:
            start = np.maximum(np.asarray(lo), origin)
            stop = np.minimum(np.asarray(lo) + link.shape, origin + np.asarray(shape))
            if (stop <= start).any():
                continue
            out = np.zeros(shape, dtype=bool)
            out[tuple(slice(a, b) for a, b in zip(start - origin, stop - origin))] = link[
                tuple(slice(a, b) for a, b in zip(start - np.asarray(lo), stop - np.asarray(lo)))]
            found.append(out)
        return found

    def groups(self, mask: np.ndarray) -> int:
        """K の6連結成分を E で結んでまとめたときの塊の数(K が空なら 0)。S(m) を満たすのは 1 以下。"""
        core = core_cells(self._material(mask), self.m)
        labels, count = ndimage.label(core, structure=_SIX)
        if count <= 1:
            return count
        parent = list(range(count + 1))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for lo, link in self._links:
            box = tuple(slice(l, l + n) for l, n in zip(lo, link.shape))
            hit = np.unique(labels[box][link & core[box]])
            for other in hit[1:]:
                parent[find(int(other))] = find(int(hit[0]))
        return len({find(i) for i in range(1, count + 1)})

    def connected(self, mask: np.ndarray) -> bool:
        """`mask`(`exempt` は材料に数える)が S(m) を満たすか(大域判定)。"""
        return self.groups(mask) <= 1

    def window(self, changed_lo, changed_hi, shape: tuple) -> tuple[np.ndarray, np.ndarray, tuple]:
        """変更したセルの外接箱 `[changed_lo, changed_hi)` に対し、`proven` へ渡す窓を返す: `(原点, 終点, 切り出しの添字)`。
        切り出し(`ext`)は終点の外に m−1 の余白を持つ(芯は立方の最小の角で数えるので、窓の縁まで正確に出すため)。
        変更が抜いたセルだけなら、芯が変わるのは箱から −(m−1) までなので、その周り m を見る。"""
        m = self.m
        shape_arr = np.asarray(shape)
        lo = np.maximum(np.asarray(changed_lo) - (m - 1) - m, 0)
        hi = np.minimum(np.asarray(changed_hi) + m, shape_arr)
        ext_hi = np.minimum(hi + (m - 1), shape_arr)
        return lo, hi, tuple(slice(int(a), int(b)) for a, b in zip(lo, ext_hi))

    def proven(self, before: np.ndarray, after: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> bool:
        """`before` が S(m) を満たすとき、`after` も満たすと窓の中で言えるか。`before`・`after` は同じ窓(`window` の切り出し)の材料
        (`exempt` を除く)で、2つは変更範囲の外で一致する。偽は「言えなかった」で、満たさないとは限らない(呼び出し側が大域で数え直す)。

        抜かれた芯(`before` にあって `after` に無い)のすべての隣と、それが結ばれていた E が、窓の中で1つのつながりに残ること。
        加えて、増えた芯(`after` だけにある)の塊がどれも、残った芯のどれかとつながっていること(足した立方が孤立しない)。
        """
        material_before, material_after = self._material_windows(before, after, lo)
        crop = tuple(slice(0, int(b - a)) for a, b in zip(lo, hi))
        core_before = core_cells(material_before, self.m)[crop]
        core_after = core_cells(material_after, self.m)[crop]
        lost = core_before & ~core_after
        gained = core_after & ~core_before
        if not lost.any() and not gained.any():
            return True
        labels, count = ndimage.label(core_after, structure=_SIX)
        parent = list(range(count + 1))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        need = {int(v) for v in np.unique(labels[ndimage.binary_dilation(lost, structure=_SIX) & core_after])}
        for link in self._linked(lo, tuple(int(b - a) for a, b in zip(lo, hi))):
            hit = [int(v) for v in np.unique(labels[link & core_after])]
            for other in hit[1:]:
                parent[find(other)] = find(hit[0])
            if (link & lost).any():  # 抜かれた芯が結ばれていた E は、残りの芯へ結ばれたままでなければならない
                if not hit:
                    return False
                need.add(hit[0])
        if lost.any() and (not need or len({find(v) for v in need}) != 1):
            return False
        if gained.any():
            kept = {find(int(v)) for v in np.unique(labels[core_before & core_after])}
            if not {find(int(v)) for v in np.unique(labels[gained])} <= kept:
                return False
        return True

    def after_bite(self, inner: np.ndarray, bite, island_cells: int) -> bool:
        """`peel` の1手(`bite`: セルの添字1つ `(i,j,k)` か (n,3) の配列。切り離して一緒に消した島は `island_cells` セル)を抜いた後の
        `inner` が S(m) を満たすか。抜く前は満たす前提。島を消したときは大域で数える。島が無ければ一口の周りの窓で言えなければ大域で数える。"""
        if island_cells:
            return self.connected(inner)
        cells = np.atleast_2d(np.asarray(bite))
        lo, hi, ext = self.window(cells.min(0), cells.max(0) + 1, inner.shape)
        after = inner[ext]
        before = after.copy()
        rel = cells - np.array([s.start for s in ext])
        before[rel[:, 0], rel[:, 1], rel[:, 2]] = True
        return self.proven(before, after, lo, hi) or self.connected(inner)

    def child_keeps(self, parent: np.ndarray, child: np.ndarray, region: tuple) -> bool:
        """S(m) を満たす `parent` の子 `child`(`region`(スライスの3つ組)の周り距離 m-1 以内だけが親と違う)が S(m) を満たすか。
        窓で言えなければ大域で数える。"""
        reach = self.m - 1
        lo, hi, ext = self.window([s.start - reach for s in region], [s.stop + reach for s in region], child.shape)
        return self.proven(parent[ext], child[ext], lo, hi) or self.connected(child)

    def _material_windows(self, before: np.ndarray, after: np.ndarray, lo: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.exempt is None:
            return before, after
        fixed = self.exempt[tuple(slice(int(a), int(a) + n) for a, n in zip(lo, before.shape))]
        return before | fixed, after | fixed

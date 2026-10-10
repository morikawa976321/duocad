"""最小幅の規則(全材料セルが m×m×m の材料だけのブロックに属する)の局所計算。

`binary_search` の共有状態には触らない部品: m 立方の opening、セルを空にしたとき被覆を失うセル(巻き添え)、
複数セルの一口を抜いてよいかの幾何判定。判定と巻き添えは同じ窓の opening から出す(食い違わないように)。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from scipy import ndimage

from . import acceptance
from .acceptance import NEIGHBOURHOOD_STRUCTURE


def cube_opening(mask: np.ndarray, m: int) -> np.ndarray:
    """m 立方の構造要素による opening。立方は3軸の直線の合成なので、軸ごとの1次元の erosion → dilation に分けて
    `binary_opening` と同じ結果を出す。直接だと費用が m³ に比例し(実測 100×60×60 格子で m=32: 14秒、m=48: 218秒)、
    分けると m に比例する(m=48: 0.03秒)。"""
    out = mask
    for op in (ndimage.binary_erosion, ndimage.binary_dilation):
        for axis in range(3):
            out = op(out, structure=np.ones(tuple(m if a == axis else 1 for a in range(3)), dtype=bool))
    return out


def lost_coverage(mask: np.ndarray, region: tuple, m: int,
                  exempt: Optional[np.ndarray] = None) -> tuple[tuple, np.ndarray]:
    """`region`(スライスの3つ組)を材料から丸ごと空にしたと仮定して、m 立方の被覆を失う材料セル。
    `(origin, lost)`: `lost` は `region` の周り(距離 m-1 以内)の真偽値、`origin` はその (0,0,0) の `mask` での添字。

    `exempt`(保持ソリッド由来のセル)は被覆を問わない対象外で、`mask` にあるかどうかに関わらず常に材料として他のセルの立方に数える
    (`region` を空にしても、その中の `exempt` は材料のまま)。製品の探索では `exempt` は保持セルで、`mask` に常に入っている
    (`docs/TOPOPT_PRESERVE.md`)。`mask` の外の `exempt` を数えるのはこの関数を単独で呼ぶときの契約。
    m 立方はそのセルから距離 m-1 以内にしか広がらないので、`region` から距離 2×(m-1) の
    材料データだけで判定できる(大域を再計算しない)。`mask` が既に規則を満たす(`exempt` を除く)ことが前提。
    """
    reach = m - 1
    pad = 2 * reach
    shape = mask.shape
    lo = tuple(max(int(s.start) - pad, 0) for s in region)
    hi = tuple(min(int(s.stop) + pad, n) for s, n in zip(region, shape))
    window = np.array(mask[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]], dtype=bool, copy=True)
    window[tuple(slice(int(s.start) - l, int(s.stop) - l) for s, l in zip(region, lo))] = False
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


def bite_verdict(padded: np.ndarray, bite: np.ndarray) -> tuple[Optional[str], bool]:
    """複数セルの一口 `bite`(添字 (n,3)。`padded` の内側の添字)を抜いてよいかを、一口の外接箱を1セル広げた窓だけで判定する。
    `padded` は外周に空を1セル足したマスク。近傍は6近傍(`NEIGHBOURHOOD_STRUCTURE`)。

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

    labels, count = ndimage.label(inside, structure=NEIGHBOURHOOD_STRUCTURE)
    open_side = np.unique(labels[ndimage.binary_dilation(~window, structure=NEIGHBOURHOOD_STRUCTURE) & inside])
    if len(open_side) < count:
        return 'surface', False
    window[inside] = False
    irregular, _ = acceptance.irregular_vertices(window)
    if irregular[1:-1, 1:-1, 1:-1].any():
        return 'manifold', False
    neighbours = ndimage.binary_dilation(inside, structure=NEIGHBOURHOOD_STRUCTURE) & window
    if not neighbours.any():
        return 'connectivity', False
    rest, _ = ndimage.label(window, structure=NEIGHBOURHOOD_STRUCTURE)
    return None, len(np.unique(rest[neighbours])) == 1


#: 芯の隣接を26にする最小の m: 斜めの段を通る球の直径 √2(m−1) が m 以上になる最小の整数(m ≥ 2+√2)。
#: **格子の間隔が3軸で等しい前提**(`voxelize.Grid.spacing` がスカラー)。導出と実測は docs/TOPOPT_MIN_WIDTH.md 2.1節・4.2節。
CORE_26_MIN_M = 4
_FULL_26 = np.ones((3, 3, 3), dtype=bool)


def core_structure(m: int) -> np.ndarray:
    """m 立方の芯どうしの隣接(`ndimage` の構造要素): m ≥ `CORE_26_MIN_M` なら26、未満なら6。"""
    return _FULL_26 if int(m) >= CORE_26_MIN_M else NEIGHBOURHOOD_STRUCTURE


def core_cells(material: np.ndarray, m: int) -> np.ndarray:
    """芯 K: セル c を最小の角とする m 立方が全部材料のセル(立方が配列に収まらない c は含めない)。"""
    core = np.asarray(material, dtype=bool)
    if min(core.shape) < m:
        return np.zeros_like(core)
    return _fold_along_axes(core, m, np.logical_and, clear_tail=True)


def _spread_down(cells: np.ndarray, m: int) -> np.ndarray:
    """`cells` の各セル x から、各軸で −[0,m) へ広げた和集合(x を含む m 立方の最小の角の集合)。"""
    return _fold_along_axes(cells, m, np.logical_or, clear_tail=False)


def _fold_along_axes(cells: np.ndarray, m: int, combine, *, clear_tail: bool) -> np.ndarray:
    """各軸で s = 1..m−1 ずらした自分を `combine`(and / or)で重ねる(`core_cells` と `_spread_down` の同形部分)。
    `clear_tail` なら末尾 n−s..n を偽にする(m 立方が配列に収まらないセル)。入力は書き換えない。"""
    out = cells
    for axis in range(3):
        n = out.shape[axis]
        acc = out.copy()
        for s in range(1, min(m, n)):
            head = tuple(slice(0, n - s) if a == axis else slice(None) for a in range(3))
            tail = tuple(slice(s, n) if a == axis else slice(None) for a in range(3))
            acc[head] = combine(acc[head], out[tail])
            if clear_tail:
                acc[tuple(slice(n - s, n) if a == axis else slice(None) for a in range(3))] = False
        out = acc
    return out


class CoreConnectivity:
    """規則 S(m): 侵食した芯 K の連結成分(芯の隣接 `structure`。既定は `core_structure(m)`)が、`exempt`(保持ソリッド由来のセル)を
    介したつながりまで含めて1つにまとまる。

    K_i と `exempt` の6連結成分 E_j は、K_i のどれかの立方が E_j と重なるか6近傍で接するとき結ばれる(芯の隣接に依らず6)。
    立方どうしが共有1セルの斜めのつなぎ目(角のずれ m−1)は、R(m) は許すが芯が隣り合わないので S(m) は許さない。
    `exempt` が無ければ「K が連結」。K が空のときは(要求するつながりが無いので)満たす。
    `exempt` は探索の間動かないので、E_j ごとの「結ばれる K の候補セル」は作るときに1回だけ求める。
    `structure` を渡すと芯の隣接を固定する(6と26の比較・テスト用)。
    """

    def __init__(self, m: int, exempt: Optional[np.ndarray] = None, structure: Optional[np.ndarray] = None) -> None:
        self.m = int(m)
        self.structure = core_structure(self.m) if structure is None else np.asarray(structure, dtype=bool)
        self.exempt = None if exempt is None else np.asarray(exempt, dtype=bool)
        self._links: list[tuple[tuple, np.ndarray]] = []
        #: `after_bite` がどの経路で答えたかの回数(窓で「つながる」/ 窓で「割れる」/ 大域の数え直し)と、窓を広げた最大の倍率。
        self.stats = dict(window_true=0, window_false=0, global_count=0, max_margin=1)
        if self.exempt is None or not self.exempt.any():
            return
        labels, count = ndimage.label(self.exempt, structure=NEIGHBOURHOOD_STRUCTURE)
        for j, box in enumerate(ndimage.find_objects(labels), start=1):
            lo = tuple(max(s.start - self.m, 0) for s in box)  # E_j の外周1セル + 立方が下へ伸びる m-1
            crop = tuple(slice(l, s.stop + 1) for l, s in zip(lo, box))
            component = labels[crop] == j
            reach = component | ndimage.binary_dilation(component, structure=NEIGHBOURHOOD_STRUCTURE)
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
        """K の連結成分を E で結んでまとめたときの塊の数(K が空なら 0)。S(m) を満たすのは 1 以下。"""
        core = core_cells(self._material(mask), self.m)
        labels, count = ndimage.label(core, structure=self.structure)
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

    def window(self, changed_lo, changed_hi, shape: tuple, margin: int = 1) -> tuple[np.ndarray, np.ndarray, tuple]:
        """変更したセルの外接箱 `[changed_lo, changed_hi)` に対し、`verdict` へ渡す窓を返す: `(原点, 終点, 切り出しの添字)`。
        切り出し(`ext`)は終点の外に m−1 の余白を持つ(芯は立方の最小の角で数えるので、窓の縁まで正確に出すため)。
        変更が抜いたセルだけなら、芯が変わるのは箱から −(m−1) までなので、その周り m×`margin` を見る
        (`after_bite` は `margin` を 1, 2, 4, … と倍にして窓を広げる)。"""
        m = self.m
        shape_arr = np.asarray(shape)
        lo = np.maximum(np.asarray(changed_lo) - (m - 1) - m * int(margin), 0)
        hi = np.minimum(np.asarray(changed_hi) + m * int(margin), shape_arr)
        ext_hi = np.minimum(hi + (m - 1), shape_arr)
        return lo, hi, tuple(slice(int(a), int(b)) for a, b in zip(lo, ext_hi))

    def proven(self, before: np.ndarray, after: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> bool:
        """`before` が S(m) を満たすとき、`after` も満たすと窓の中で言えるか(`verdict` が真のときだけ真。偽は「言えなかった」で、
        満たさないとは限らない)。"""
        return self.verdict(before, after, lo, hi, None) is True

    def verdict(self, before: np.ndarray, after: np.ndarray, lo: np.ndarray, hi: np.ndarray,
                shape: Optional[tuple]) -> Optional[bool]:
        """`before` が S(m) を満たすとき、`after` も満たすかを窓の中だけで判定する。`before`・`after` は同じ窓(`window` の切り出し)の
        材料(`exempt` を除く)で、2つは変更範囲の外で一致する。真 = つながる、偽 = 確実に割れる、None = 窓の中では言えない。

        根拠: 芯(隣接 `structure`)と E の結びを辺とするグラフで、抜く前は1塊なので、抜いた後の各成分は必ず前線(抜かれた芯に
        隣り合う残りの芯)を含む。だから抜いた後も1塊であることと、**前線どうしが残った芯の中でつながること**は同値
        (前線の膨張と芯の label は同じ隣接でなければならない。片方だけ広いと、斜めだけで接していた成分が前線から漏れて真を返す)。
        窓の中で前線が1つにつながれば真。前線が2つ以上の塊に分かれ、そのうち1つでも**閉じている**(窓の面のうち格子の縁で
        ない面に触れず、窓の外へ広がる E の結び先にも触れない)塊があれば、それは他と別の成分なので偽。どの塊も開いていれば
        None(`after_bite` が窓を広げる)。`shape`(格子の形)が None なら閉じているかを見ず、偽を返さない(`proven`)。
        増えた芯(`after` だけにある)があれば None(抜くだけの手では起きない)。
        """
        material_before, material_after = self._material_windows(before, after, lo)
        crop = tuple(slice(0, int(b - a)) for a, b in zip(lo, hi))
        core_before = core_cells(material_before, self.m)[crop]
        core_after = core_cells(material_after, self.m)[crop]
        lost = core_before & ~core_after
        gained = core_after & ~core_before
        if not lost.any() and not gained.any():
            return True
        if gained.any():
            return None
        labels, count = ndimage.label(core_after, structure=self.structure)
        parent = list(range(count + 1))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        need = {int(v) for v in np.unique(labels[ndimage.binary_dilation(lost, structure=self.structure) & core_after])}
        open_labels: set[int] = set()
        crop_shape = tuple(int(b - a) for a, b in zip(lo, hi))
        for link, extends in self._linked_with_extent(lo, crop_shape):
            hit = [int(v) for v in np.unique(labels[link & core_after])]
            for other in hit[1:]:
                parent[find(other)] = find(hit[0])
            if extends:
                open_labels |= set(hit)
            if (link & lost).any():  # 抜かれた芯が結ばれていた E
                if hit:
                    need.add(hit[0])
                elif extends or shape is None:
                    return None  # 窓の外の芯へまだ結ばれているかもしれない
                # 結び先が窓の中に無く窓の外にも広がらない E は、もう何も結ばない
        if not need:
            return True  # 抜かれた芯に隣り合う芯が無い = 残った芯が無い(抜く前は1塊だったので)
        groups = {find(v) for v in need}
        if len(groups) == 1:
            return True
        if shape is None:
            return None
        # 前線が分かれたときだけ、窓の面(格子の縁でないもの)に触れる塊を数える(つながる手が大半なので、その経路には足さない)。
        for axis in range(3):
            if int(lo[axis]) > 0:
                face = tuple(0 if a == axis else slice(None) for a in range(3))
                open_labels |= {int(v) for v in np.unique(labels[face])}
            if int(hi[axis]) < int(shape[axis]):
                face = tuple(-1 if a == axis else slice(None) for a in range(3))
                open_labels |= {int(v) for v in np.unique(labels[face])}
        open_labels.discard(0)
        open_roots = {find(v) for v in open_labels}
        if any(g not in open_roots for g in groups):
            return False
        return None

    def _linked_with_extent(self, origin: np.ndarray, shape: tuple) -> list[tuple[np.ndarray, bool]]:
        """`_linked` に、その E の結び先の箱が窓 `[origin, origin+shape)` の外へ広がるか(真なら窓の外の芯へも結びうる)を添える。"""
        out = []
        origin = np.asarray(origin)
        for lo, link in self._links:
            start = np.maximum(np.asarray(lo), origin)
            stop = np.minimum(np.asarray(lo) + link.shape, origin + np.asarray(shape))
            if (stop <= start).any():
                continue
            clipped = np.zeros(shape, dtype=bool)
            clipped[tuple(slice(a, b) for a, b in zip(start - origin, stop - origin))] = link[
                tuple(slice(a, b) for a, b in zip(start - np.asarray(lo), stop - np.asarray(lo)))]
            extends = bool((np.asarray(lo) < origin).any() or (np.asarray(lo) + link.shape > origin + np.asarray(shape)).any())
            out.append((clipped, extends))
        return out

    def after_bite(self, inner: np.ndarray, bite, island_cells: int) -> bool:
        """`peel` の1手(`bite`: セルの添字1つ `(i,j,k)` か (n,3) の配列。切り離して一緒に消した島は `island_cells` セル)を抜いた後の
        `inner` が S(m) を満たすか。抜く前は満たす前提。島を消したときは大域で数える。島が無ければ一口の周りの窓で判定し
        (`verdict`)、言えなければ窓を倍に広げて繰り返し、窓が格子全体に達したら大域で数える。答えは大域の数え直しと同じ
        (窓の判定は真も偽も厳密)。hanger 1.875mm・m=4 の終盤では、以前の「窓で言えなければ大域」が 20,401 回・1 回 9 ms
        の数え直しをしていて、その 99.5% は割れる手だった(`docs/TOPOPT_STOP_RULE.md` §5)。"""
        if island_cells:
            self.stats['global_count'] += 1
            return self.connected(inner)
        cells = np.atleast_2d(np.asarray(bite))
        shape = inner.shape
        margin = 1
        while True:
            lo, hi, ext = self.window(cells.min(0), cells.max(0) + 1, shape, margin)
            after = inner[ext]
            before = after.copy()
            rel = cells - np.array([s.start for s in ext])
            before[rel[:, 0], rel[:, 1], rel[:, 2]] = True
            verdict = self.verdict(before, after, lo, hi, shape)
            if verdict is not None:
                self.stats['window_true' if verdict else 'window_false'] += 1
                self.stats['max_margin'] = max(self.stats['max_margin'], margin)
                return verdict
            # 窓が格子の 1/8 を超えたら、これ以上広げるより大域で数えるほうが安い(小さい格子では大域が 1 ms 未満。
            # 合成題材 52×36×36 で窓を広げ続けると peel が 36 → 47 秒に増えた実測)。
            if (lo == 0).all() and (hi == np.asarray(shape)).all() or 8 * int(np.prod(hi - lo)) >= int(np.prod(shape)):
                self.stats['global_count'] += 1
                return self.connected(inner)
            margin *= 2

    def _material_windows(self, before: np.ndarray, after: np.ndarray, lo: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.exempt is None:
            return before, after
        fixed = self.exempt[tuple(slice(int(a), int(a) + n) for a, n in zip(lo, before.shape))]
        return before | fixed, after | fixed

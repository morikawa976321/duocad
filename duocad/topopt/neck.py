"""探索の1手が「くびれ」を作るかの局所判定と、形のくびれの静的診断(#120)。

定義と根拠は [TOPOPT_NECK_RULE_120.md](../../docs/TOPOPT_NECK_RULE_120.md) §2。要点:

- R(m)・S(m) を満たしても、輪の中では2つの塊が1セル幅でしかつながらない形が残る(S(m) は芯が**大域で**つながるかしか見ない)。
- 1手(一口 + 巻き添え。島は除く)について、**材料の前線をつなぎ直す最小の窓の中で、芯の前線がつなぎ直されない**なら、その手はくびれを作る。
  材料が格子全体でもつなぎ直されない手(部材を切り切って本体から離す手)はこの判定の対象外。
- 芯どうしの結びは S(m) と同じ: 6連結 + 保持成分ごとの結び(`CoreConnectivity._linked`)。保持成分は大域のラベルなので、
  保持の中の経路が窓の外を通っても結ぶ(「保持経由は太い」の決定による例外)。
- `CoreConnectivity.verdict` は流用しない —— 窓の中で前線が1群でも、保持の結び先が窓外へ伸びると None を返す(Codex の反例、仕様書 §4)。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from scipy import ndimage

from .acceptance import NEIGHBOURHOOD_STRUCTURE
from .min_width import CoreConnectivity, core_cells

#: 判定窓の余白の初期値(m の倍数)。hanger S2 で、余白 m では折れ目3つをくびれと分類し、2m・3m では同じ分類(くびれ1・折れ目4)になった。
START_MARGIN_FACTOR = 2
_FULL_26 = np.ones((3, 3, 3), dtype=bool)


class NeckJudge:
    """`peel` の1手ごとに呼ぶ判定器。`core` は同じ探索の `CoreConnectivity`(保持の結びを共有する)。

    `stats`: `rejected`(くびれとして断った手)、`rejected_6_only`(6連結の芯では2群以上だが、26連結の芯なら1群だった手 ——
    芯の隣接を26にするかの判断材料。別 Issue)、`full_grid`(材料が格子全体まで広げないとつながらず、全格子でラベルした回数)。
    """

    def __init__(self, core: CoreConnectivity) -> None:
        self.core = core
        self.m = core.m
        self.stats = dict(rejected=0, rejected_6_only=0, full_grid=0)

    def creates_neck(self, mask: np.ndarray, removed) -> bool:
        """`removed`(添字 (n,3)。一口と巻き添え。島は含めない)を空にした**後**の `mask` について、その手がくびれを作るか。"""
        m = self.m
        cells = np.atleast_2d(np.asarray(removed))
        shape = np.asarray(mask.shape)
        fixed = self.core.exempt
        margin = START_MARGIN_FACTOR * m
        groups_seen: Optional[int] = None
        while True:
            # 角の窓 B: 消失芯(角が一口の手前 m−1 まで)を含み、各側に margin。材料の窓は B の角の立方が覆う範囲(終点側に m−1)。
            lo = np.maximum(cells.min(0) - (m - 1) - margin, 0)
            hi = np.minimum(cells.max(0) + 1 + margin, shape)
            whole = bool((lo == 0).all() and (hi == shape).all())
            if not whole and 8 * int(np.prod(hi - lo)) >= int(np.prod(shape)):
                lo, hi, whole = np.zeros(3, dtype=int), shape.copy(), True
            mat_hi = np.minimum(hi + (m - 1), shape)
            mat_win = tuple(slice(int(a), int(b)) for a, b in zip(lo, mat_hi))
            crop = tuple(slice(0, int(b - a)) for a, b in zip(lo, hi))
            after = np.array(mask[mat_win], dtype=bool)
            if fixed is not None:
                after |= fixed[mat_win]
            rel = cells - lo
            before = after.copy()
            before[rel[:, 0], rel[:, 1], rel[:, 2]] = True
            core_after = core_cells(after, m)[crop]
            lost = core_cells(before, m)[crop] & ~core_after
            if not lost.any():
                return False
            front = ndimage.binary_dilation(lost, structure=NEIGHBOURHOOD_STRUCTURE) & core_after
            if not front.any():
                return False
            if groups_seen is None:
                # 最初の窓で芯の前線が1群なら、窓を広げても群は合流するだけなので通す(材料を見なくてよい)。
                groups_seen = len(self._roots(core_after, front, lo, NEIGHBOURHOOD_STRUCTURE))
                if groups_seen <= 1:
                    return False
            removed_here = np.zeros_like(after)
            removed_here[rel[:, 0], rel[:, 1], rel[:, 2]] = True
            mat_front = ndimage.binary_dilation(removed_here, structure=NEIGHBOURHOOD_STRUCTURE) & after
            if not mat_front.any():
                return False
            labels, _ = ndimage.label(after, structure=NEIGHBOURHOOD_STRUCTURE)
            if len(np.unique(labels[mat_front])) == 1:
                if len(self._roots(core_after, front, lo, NEIGHBOURHOOD_STRUCTURE)) <= 1:
                    return False
                self.stats['rejected'] += 1
                if len(self._roots(core_after, front, lo, _FULL_26)) <= 1:
                    self.stats['rejected_6_only'] += 1
                return True
            if whole:
                self.stats['full_grid'] += 1
                return False  # 材料が格子全体でもつなぎ直されない = 本体から離す手(既存の連結判定の領分)
            margin *= 2

    def _roots(self, core_after: np.ndarray, front: np.ndarray, lo: np.ndarray, structure: np.ndarray) -> set:
        """窓の中で、`front` の芯が属する群(6連結 or 26連結 + 保持成分ごとの結び)の代表の集合。"""
        labels, count = ndimage.label(core_after, structure=structure)
        parent = list(range(count + 1))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for link in self.core._linked(lo, core_after.shape):
            hit = [int(v) for v in np.unique(labels[link & core_after])]
            for other in hit[1:]:
                parent[find(other)] = find(hit[0])
        return {find(int(v)) for v in np.unique(labels[front]) if v}


def static_necks(mask: np.ndarray, core: CoreConnectivity) -> dict:
    """形の静的診断: 6近傍の材料セルの組 (p, q)(どちらも保持でない)で、組の外接箱 ±2m の窓の中で、p を覆う芯と q を覆う芯が
    `NeckJudge` と同じ群の作り方で同じ群にならない組を数える。片側を覆う芯が空なら「つながらない」。

    両方を含む m 立方がある組は、その立方の角が両側を覆うので必ずつながる。だから候補は「共通の m 立方を持たない組」だけを調べれば足りる
    (斜めに1セルだけ重なるくびれは、どの組も共通の立方を持つので、この診断には出ない —— 探索の判定 `NeckJudge` は捉える)。
    戻り値 `{pairs_without_common_cube, disconnected_pairs, disconnected_cells: [[i,j,k], ...](最大20)}`。
    """
    m = core.m
    material = np.asarray(mask, dtype=bool) if core.exempt is None else (np.asarray(mask, dtype=bool) | core.exempt)
    k = core_cells(material, m)
    shape = np.asarray(material.shape)
    candidates = []
    for axis in range(3):
        covered = _spread_up(k, tuple(m - 1 if a == axis else m for a in range(3)))
        lead = tuple(slice(None, -1) if a == axis else slice(None) for a in range(3))
        tail = tuple(slice(1, None) if a == axis else slice(None) for a in range(3))
        pair = material[lead] & material[tail] & ~covered[lead]
        if core.exempt is not None:
            pair &= ~(core.exempt[lead] | core.exempt[tail])
        for p in np.argwhere(pair):
            q = p.copy()
            q[axis] += 1
            candidates.append((p, q))
    judge = NeckJudge(core)
    disconnected = []
    for p, q in candidates:
        lo = np.maximum(np.minimum(p, q) - (m - 1) - 2 * m, 0)
        hi = np.minimum(np.maximum(p, q) + 1 + 2 * m, shape)
        window = tuple(slice(int(a), int(b)) for a, b in zip(lo, hi))
        kw = k[window]
        roots = []
        for c in (p, q):
            cover = np.zeros_like(kw)
            a = np.maximum(c - (m - 1) - lo, 0)
            b = c + 1 - lo
            cover[a[0]:b[0], a[1]:b[1], a[2]:b[2]] = True
            roots.append(judge._roots(kw, cover & kw, lo, NEIGHBOURHOOD_STRUCTURE))
        if not (roots[0] & roots[1]):
            disconnected.append(p)
    return dict(pairs_without_common_cube=len(candidates), disconnected_pairs=len(disconnected),
                disconnected_cells=[[int(v) for v in p] for p in disconnected[:20]])


def _spread_up(cells: np.ndarray, lengths: tuple) -> np.ndarray:
    """`cells` の各角 c から、軸ごとに +[0, lengths[軸]) へ広げた和集合(角 c の立方が覆うセル)。"""
    out = cells
    for axis, length in enumerate(lengths):
        n = out.shape[axis]
        acc = out.copy()
        for s in range(1, min(length, n)):
            head = tuple(slice(0, n - s) if a == axis else slice(None) for a in range(3))
            tail = tuple(slice(s, n) if a == axis else slice(None) for a in range(3))
            acc[tail] |= out[head]
        out = acc
    return out

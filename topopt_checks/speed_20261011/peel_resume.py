"""試作: 同じ親マスクに対する peel の歩きを覚え、接頭辞と再開で同じ候補を作る(binary_search.peel と同じ判定を同じ順に行う)。

`PeelMemo(mask, anchor_ids, min_width_cells, exempt, protected)` を親ごとに作り、`memo.peel(order, count)` を
`binary_search.peel(mask, order, count, ...)` の代わりに呼ぶ。戻りは (候補マスク, picked, casualties, islands, picked_cells)。
"""
from __future__ import annotations

import time
from typing import Optional

import numpy as np
from scipy import ndimage

from duocad.topopt import min_width, neck
from duocad.topopt.binary_search import (_CORNERS, _WINDOW_NEIGHBOURS, NEIGHBOURHOOD_STRUCTURE,
                                         _mask_satisfies_min_width, _neighbours_stay_connected,
                                         _regular_after_removal)


class PeelMemo:
    def __init__(self, mask, anchor_ids, *, min_width_cells=1, exempt=None, protected=None):
        self.parent = np.asarray(mask, dtype=bool)
        self.anchor_ids = anchor_ids
        self.m = min_width_cells
        self.exempt = exempt
        self.protected = None if protected is None else np.asarray(protected, dtype=bool)
        if self.protected is not None and self.m > 1 and (exempt is None or (self.protected & ~exempt).any()):
            raise ValueError('m>1 の peel では protected は exempt に含まれること')
        shape = self.parent.shape
        self.shape = shape
        self.node_shape = tuple(n + 1 for n in shape)
        self.is_anchor = np.zeros(self.node_shape, dtype=bool)
        self.is_anchor.ravel()[anchor_ids] = True
        touches = np.zeros(shape, dtype=bool)
        for corner in _CORNERS:
            window = tuple(slice(c, c + n) for c, n in zip(corner, shape))
            touches |= self.is_anchor[window]
        self.touches = touches
        self.core = min_width.CoreConnectivity(self.m, exempt) if self.m > 1 else None
        self.necks = None if self.core is None else neck.NeckJudge(self.core)
        # 歩きの記録: order(保護セルを除いた後)・手の列・現在の状態
        self.order = None
        self.picks: list = []   # (order 内の位置, 起点セル, 消したセルの平坦添字配列, 累積で減った数)
        self.position = 0
        self.work = None
        self.support = None
        self.seconds = 0.0
        self.walked = 0

    # ---- 状態 ----
    def _fresh_state(self, upto_picks: int):
        mask = self.parent.copy()
        for _, _, removed, _ in self.picks[:upto_picks]:
            mask.ravel()[removed] = False
        support = np.zeros(self.node_shape, dtype=np.int32)
        for corner in _CORNERS:
            window = tuple(slice(c, c + n) for c, n in zip(corner, self.shape))
            support[window] += mask
        self.work = np.pad(mask, 1)
        self.support = support

    def peel(self, order: np.ndarray, count: int):
        started = time.perf_counter()
        if self.protected is not None:
            order = order[~self.protected.ravel()[order]]
        if self.order is None:
            self.order = order
            self.picks = []
            self.position = 0
            self._fresh_state(0)
        elif not (len(order) == len(self.order) and np.array_equal(order, self.order)):
            n = min(len(order), len(self.order))
            diff = np.flatnonzero(order[:n] != self.order[:n])
            q = int(diff[0]) if diff.size else n
            keep = sum(1 for p in self.picks if p[0] < q)
            self.picks = self.picks[:keep]
            self.order = order
            if q < self.position:
                self.position = q
                self._fresh_state(keep)
        # 既に記録した手で足りるか
        for index, (_, _, _, total) in enumerate(self.picks):
            if total >= count:
                return self._result(index + 1, started)
        self._walk(count)
        return self._result(len(self.picks), started)

    def _result(self, upto: int, started):
        mask = self.parent.copy()
        cells = []
        for _, cell, removed, _ in self.picks[:upto]:
            mask.ravel()[removed] = False
            cells.append(cell)
        self.seconds += time.perf_counter() - started
        total = self.picks[upto - 1][3] if upto else 0
        return mask, len(cells), np.asarray(cells, dtype=np.int64), total

    def _walk(self, count: int):
        shape = self.shape
        plane, row = shape[1] * shape[2], shape[2]
        node_plane, node_row = self.node_shape[1] * self.node_shape[2], self.node_shape[2]
        support_flat, anchor_flat, touches_flat = self.support.ravel(), self.is_anchor.ravel(), self.touches.ravel()
        touches = self.touches
        work = self.work
        inner = work[1:-1, 1:-1, 1:-1]
        exempt, protected, core, necks, m = self.exempt, self.protected, self.core, self.necks, self.m
        anchor_ids = self.anchor_ids
        total = self.picks[-1][3] if self.picks else 0
        order = self.order
        pos = self.position
        while pos < len(order):
            if total >= count:
                break
            cell = int(order[pos])
            pos += 1
            self.walked += 1
            i, rest = divmod(cell, plane)
            j, k = divmod(rest, row)
            if not work[i + 1, j + 1, k + 1]:
                continue
            corners = None
            if touches_flat[cell]:
                corners = [(i + a) * node_plane + (j + b) * node_row + (k + c) for a, b, c in _CORNERS]
                if any(anchor_flat[n] and support_flat[n] <= 1 for n in corners):
                    continue
            window = work[i:i + 3, j:j + 3, k:k + 3].copy()
            if all(window[p] for p in _WINDOW_NEIGHBOURS):
                continue
            bite = None
            extra = None
            if m > 1:
                extra = min_width.casualties(inner, (i, j, k), m, exempt)
                if len(extra):
                    bite = np.vstack(([[i, j, k]], extra))
            if bite is None:
                cleared = (i, j, k)
                window[1, 1, 1] = False
                if not _regular_after_removal(window):
                    continue
                connected = _neighbours_stay_connected(window)
                if connected is None:
                    continue
            else:
                cleared = tuple(bite.T)
                for ci, cj, ck in extra[touches[extra[:, 0], extra[:, 1], extra[:, 2]]]:
                    corners = (corners or []) + [(ci + a) * node_plane + (cj + b) * node_row + (ck + c)
                                                 for a, b, c in _CORNERS]
                if corners is not None:
                    dropped: dict = {}
                    for n in corners:
                        if anchor_flat[n]:
                            dropped[n] = dropped.get(n, 0) + 1
                    if any(support_flat[n] <= d for n, d in dropped.items()):
                        continue
                reason, connected = min_width.bite_verdict(work, bite)
                if reason is not None:
                    continue
            island_cells = 0
            islands = None
            inner[cleared] = False
            if not connected:
                labels, _ = ndimage.label(inner, structure=NEIGHBOURHOOD_STRUCTURE)
                holders = np.unique(labels[touches & inner])
                if anchor_ids.size:
                    keep = int(holders[0]) if holders.size == 1 else 0
                else:
                    keep = 1 if labels.max() == 1 else 0
                if keep == 0:
                    inner[cleared] = True
                    continue
                islands = (labels > 0) & (labels != keep)
                if protected is not None and (islands & protected).any():
                    inner[cleared] = True
                    continue
                island_cells = int(islands.sum())
                if exempt is not None and m > 2 and not _mask_satisfies_min_width(inner & ~islands, m, exempt):
                    inner[cleared] = True
                    continue
                inner[islands] = False
            if necks is not None and necks.creates_neck(inner, np.array([[i, j, k]]) if bite is None else bite):
                inner[cleared] = True
                if island_cells:
                    inner[islands] = True
                continue
            if core is not None and not core.after_bite(inner, (i, j, k) if bite is None else bite, island_cells):
                inner[cleared] = True
                if island_cells:
                    inner[islands] = True
                continue
            if corners is not None:
                for n in corners:
                    support_flat[n] -= 1
            if bite is None:
                removed = np.array([cell], dtype=np.int64)
            else:
                removed = np.ravel_multi_index(tuple(bite.T), shape).astype(np.int64)
            if island_cells:
                removed = np.concatenate([removed, np.flatnonzero(islands)])
            total += (1 if bite is None else len(bite)) + island_cells
            self.picks.append((pos - 1, cell, removed, total))
        self.position = pos

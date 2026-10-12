"""傷の毒の試作(Fable の案 a′、2026-10-13): 1手の候補が安全率で落ちたら、その一口(起点 + 巻き添え)の削除セルを「傷」として覚え、
以後の歩きで一口が傷と1セルでも重なる手を飛ばす。傷は今の毒と同じく、合格形の安全率が不足分以上に上がったら消す。製品のコードは変えない
(`PeelWalk._walk` を写して判定を足し、`binary_search.PeelWalk` を差し替える)。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\scar_experiment.py --model hanger --load 150 --h 3.29 --w 9.86 --gpu --trace

引数は `run_product.py` と同じ。出力は output/stress_gate_20261008/scar/product/(製品の記録を上書きしない)。
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

from duocad.topopt import binary_search as bs, min_width  # noqa: E402
from duocad.topopt.acceptance import NEIGHBOURHOOD_STRUCTURE  # noqa: E402,F401
from duocad.topopt.binary_search import (_CORNERS, _WINDOW_NEIGHBOURS, _Pick, _mask_satisfies_min_width,  # noqa: E402,F401
                                         _neighbours_stay_connected, _regular_after_removal)

pa.OUT = pa.OUT / "scar"
#: mask は傷の和集合(None は傷なし)、version は傷が変わるたびに増やす(歩きの使い回しを捨てる合図)。
SCAR = dict(mask=None, version=0, skips=0, scars=[], recorded=0, released=0)
LAST = dict(removed=None, picked=0, shape=None)


class ScarWalk(bs.PeelWalk):
    def take(self, order, count):
        if getattr(self, "_scar_version", None) != SCAR["version"]:
            self.order = None
            self.picks = []
            self._scar_version = SCAR["version"]
        candidate, stats = super().take(order, count)
        LAST.update(removed=np.flatnonzero(self.parent.ravel() & ~candidate.ravel()), picked=int(stats.picked),
                    shape=self.parent.shape)
        return candidate, stats

    def _walk(self, count: int) -> None:
        """今の状態から順位を歩き、消えた数の累計が `count` に届くか順位が尽きたら止める(`peel` の判定そのもの)。"""
        shape = self.parent.shape
        node_shape = self.node_shape
        order, work, touches, exempt, protected = self.order, self.work, self.touches, self.exempt, self.protected
        support_flat, anchor_flat, touches_flat = self.support.ravel(), self.is_anchor.ravel(), touches.ravel()
        plane, row = shape[1] * shape[2], shape[2]
        node_plane, node_row = node_shape[1] * node_shape[2], node_shape[2]
        inner = work[1:-1, 1:-1, 1:-1]
        m, core, necks, anchor_ids, tally = self.min_width_cells, self.core, self.necks, self.anchor_ids, self.count
        total = tally['picked'] + tally['casualties'] + tally['islands']
        while self.position < len(order) and total < count:
            position = self.position
            self.position += 1
            cell = int(order[position])
            i, rest = divmod(cell, plane)
            j, k = divmod(rest, row)
            if not work[i + 1, j + 1, k + 1]:
                tally['already_removed'] += 1
                continue
            corners = None
            if touches_flat[cell]:
                corners = [(i + a) * node_plane + (j + b) * node_row + (k + c) for a, b, c in _CORNERS]
                if any(anchor_flat[n] and support_flat[n] <= 1 for n in corners):
                    tally['skipped_support'] += 1
                    continue
            window = work[i:i + 3, j:j + 3, k:k + 3].copy()
            if all(window[p] for p in _WINDOW_NEIGHBOURS):
                tally['skipped_surface'] += 1
                continue
            # ここまでの判定は数えを動かさずに通ってきたので、これがこのセルを調べる直前の数え(この先は大域の数え直しと判定器が動かす)。
            before = self._counters()
            bite = None
            if m > 1:
                extra = min_width.casualties(inner, (i, j, k), m, exempt)
                if len(extra):
                    bite = np.vstack(([[i, j, k]], extra))
            if bite is None:
                cleared = (i, j, k)
                if SCAR['mask'] is not None and SCAR['mask'][i, j, k]:
                    SCAR['skips'] += 1
                    continue
                window[1, 1, 1] = False
                if not _regular_after_removal(window):
                    tally['skipped_manifold'] += 1
                    continue
                connected = _neighbours_stay_connected(window)
                if connected is None:
                    tally['skipped_connectivity'] += 1
                    continue
            else:
                cleared = tuple(bite.T)
                if SCAR['mask'] is not None and SCAR['mask'][cleared].any():
                    SCAR['skips'] += 1
                    continue
                for ci, cj, ck in extra[touches[extra[:, 0], extra[:, 1], extra[:, 2]]]:
                    corners = (corners or []) + [(ci + a) * node_plane + (cj + b) * node_row + (ck + c)
                                                 for a, b, c in _CORNERS]
                if corners is not None:
                    dropped: dict = {}
                    for n in corners:
                        if anchor_flat[n]:
                            dropped[n] = dropped.get(n, 0) + 1
                    if any(support_flat[n] <= d for n, d in dropped.items()):
                        tally['skipped_support'] += 1
                        continue
                reason, connected = min_width.bite_verdict(work, bite)
                if reason is not None:
                    tally[f'skipped_{reason}'] += 1
                    continue
            island_cells = 0
            inner[cleared] = False
            if not connected:
                tally['global_checks'] += 1
                labels, _ = ndimage.label(inner, structure=NEIGHBOURHOOD_STRUCTURE)
                holders = np.unique(labels[touches & inner])
                if anchor_ids.size:
                    keep = int(holders[0]) if holders.size == 1 else 0
                else:
                    keep = 1 if labels.max() == 1 else 0
                if keep == 0:
                    inner[cleared] = True
                    tally['skipped_connectivity'] += 1
                    continue
                islands = (labels > 0) & (labels != keep)
                if protected is not None and (islands & protected).any():
                    inner[cleared] = True
                    tally['skipped_connectivity'] += 1
                    continue
                island_cells = int(islands.sum())
                # 島と保持成分は m=2 では同じ m 立方に入らない(2×2×2 の全セルは互いに26近傍)。m≥3 は大域で確かめる。
                if exempt is not None and m > 2 and not _mask_satisfies_min_width(inner & ~islands, m, exempt):
                    inner[cleared] = True
                    tally['skipped_connectivity'] += 1
                    continue
                # 島は条件節点に接せず、保持成分と辺・頂点でも接しない(接すれば一口の頂点の正則判定で落ちている)。
                inner[islands] = False
            if necks is not None and necks.creates_neck(inner, np.array([[i, j, k]]) if bite is None else bite):
                inner[cleared] = True
                if island_cells:
                    inner[islands] = True
                tally['skipped_neck'] += 1
                continue
            if core is not None and not core.after_bite(inner, (i, j, k) if bite is None else bite, island_cells):
                inner[cleared] = True
                if island_cells:
                    inner[islands] = True
                tally['skipped_core'] += 1
                continue
            if corners is not None:
                for n in corners:
                    support_flat[n] -= 1
            removed = (np.array([cell], dtype=np.int64) if bite is None
                       else np.ravel_multi_index(tuple(bite.T), shape).astype(np.int64))
            if island_cells:
                removed = np.concatenate([removed, np.flatnonzero(islands)])
            tally['picked'] += 1
            tally['casualties'] += 0 if bite is None else len(bite) - 1
            tally['islands'] += island_cells
            total = tally['picked'] + tally['casualties'] + tally['islands']
            self.picks.append(_Pick(position=position, cell=cell, removed=removed, total=total,
                                    before=before, after=self._counters()))


def _rebuild():
    if not SCAR["scars"]:
        SCAR["mask"] = None
    else:
        mask = np.zeros(int(np.prod(LAST["shape"])), dtype=bool)
        for cells, _, _ in SCAR["scars"]:
            mask[cells] = True
        SCAR["mask"] = mask.reshape(LAST["shape"])
    SCAR["version"] += 1


def on_event(event):
    if event["outcome"] == "rejected" and "safety" in event["failed"] and LAST["picked"] == 1             and LAST["removed"] is not None and len(LAST["removed"]):
        cand = event["candidate_safety_factor"]
        target = event["target_safety_factor"]
        shortfall = target - cand if cand is not None and np.isfinite(cand) and cand < target else None
        SCAR["scars"].append((LAST["removed"].copy(), float(event["safety_factor"]), shortfall))
        SCAR["recorded"] += 1
        _rebuild()
    elif event["outcome"] == "accepted" and SCAR["scars"]:
        keep = [s for s in SCAR["scars"] if s[2] is None or event["safety_factor"] - s[1] < s[2]]
        if len(keep) != len(SCAR["scars"]):
            SCAR["released"] += len(SCAR["scars"]) - len(keep)
            SCAR["scars"] = keep
            _rebuild()


real_find = bs.find_best_feasible


def find(*args, **kwargs):
    outer = kwargs.get("on_evaluation")

    def both(event):
        on_event(event)
        if outer is not None:
            outer(event)

    kwargs["on_evaluation"] = both
    return real_find(*args, **kwargs)


bs.PeelWalk = ScarWalk
bs.find_best_feasible = find
import run_product  # noqa: E402

started = time.perf_counter()
code = run_product.main()
print({k: v for k, v in SCAR.items() if k not in ("mask", "scars")}, "active scars", len(SCAR["scars"]), flush=True)
sys.exit(code)

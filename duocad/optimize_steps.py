"""形状最適化の各ステップ(探索の合格形)を、走っている間も終わった後も見られ、選んで仕上げられるように持つ。

1回の実行に1つ。探索のスレッドが `append` で足し(`binary_search.find_best_feasible` の `on_accept`)、画面の求め
(イベントループのスレッド)が `meta` / `voxels` で読む。探索が終わると再仕上げ計画だけを参照する。完成化は実行器が別に行う。画面との約束は docs/contracts/ui-optimize-integration.md「ステップ」。
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .optimize_plan import FinishPlan
    from .topopt.binary_search import AcceptedShape
    from .topopt.voxelize import Grid

from scipy import ndimage


class OptimizationSteps:
    """1回の実行の合格形の列(`AcceptedShape`。1セル1ビット)と、画面に出す数値。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._shapes: list[AcceptedShape] = []
        self._grids: list[Grid] = []
        self._meta: list[dict] = []
        self._started = time.perf_counter()
        #: 実行の識別子(作った時刻の16桁の16進。文字列の大小が新旧の順)。一覧・進捗・形の各行に載せ、画面が前の実行の
        #: 遅れた返事を取り違えないようにする(別の接続への配信待ちで、前の実行の進捗が後から届くことがある)。
        self.run = f"{time.time_ns():016x}"
        self.plan: FinishPlan | None = None

    def __len__(self) -> int:
        with self._lock:
            return len(self._shapes)

    def append(self, shape: AcceptedShape, evaluation: int, *, grid: Grid) -> dict:
        """合格形を1つ足し、画面に出す数値を返す。"""
        cells = int(round(shape.material_mm3 / float(grid.element_volume)))
        with self._lock:
            meta = dict(run=self.run, index=len(self._shapes), evaluation=int(evaluation),
                        seconds=round(time.perf_counter() - self._started, 1), material_mm3=float(shape.material_mm3),
                        safety_factor=float(shape.safety_factor), cells=cells)
            self._shapes.append(shape)
            self._grids.append(grid)
            self._meta.append(meta)
            return dict(meta)

    def keep(self, count: int) -> None:
        """探索の戻り値(`SearchResult.accepted`)の数にそろえる(出る前の再検査に落ちると出発形状だけに縮む)。"""
        with self._lock:
            del self._shapes[count:]
            del self._grids[count:]
            del self._meta[count:]

    def latest(self) -> Optional[dict]:
        with self._lock:
            return dict(self._meta[-1]) if self._meta else None

    def meta(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._meta]

    def shapes(self) -> list[AcceptedShape]:
        with self._lock:
            return list(self._shapes)

    def voxels(self, index: int) -> dict:
        """`index` 番目の形の表面のセル(材料の無い6近傍を持つセル。内側は画面から見えない)を、デバッグ表示と同じ形で返す。"""
        from .optimize_voxels import DEBUG_VOXEL_LIMIT, debug_voxel_payload

        with self._lock:
            if not 0 <= index < len(self._shapes):
                raise IndexError(f"ステップ {index} は無い(0〜{len(self._shapes) - 1})")
            shape, grid, meta = self._shapes[index], self._grids[index], dict(self._meta[index])
        mask = shape.mask()
        surface = mask & ~ndimage.binary_erosion(mask, border_value=0)
        payload = debug_voxel_payload("step", "step", grid, surface, limit=DEBUG_VOXEL_LIMIT)
        return dict(payload, **meta)

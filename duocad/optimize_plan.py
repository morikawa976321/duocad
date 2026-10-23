"""探索を終えた後も保持する、再仕上げの入力と合格形。通知先や追加先の文書は保持しない。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

    from .topopt.binary_search import AcceptedShape, SearchConditions
    from .topopt.voxelize import Grid, TriangleSoup


@dataclass(frozen=True)
class MinimumWidth:
    requested_mm: float
    spacing_mm: float
    cells: int
    guaranteed_mm: float
    source: str

    def as_dict(self) -> dict:
        return dict(requested_mm=self.requested_mm, spacing_mm=self.spacing_mm, cells=self.cells,
                    guaranteed_mm=self.guaranteed_mm, source=self.source)


@dataclass(frozen=True)
class FinishingShape:
    mask: np.ndarray
    safety_factor: float
    material_mm3: float


@dataclass(frozen=True)
class CompletionInputs:
    """開始時の格子・検査規則・形状。探索後は読み取り専用として計画が所有する。"""

    space_mask: np.ndarray
    grid: Grid
    conditions: SearchConditions
    min_width: MinimumWidth
    target_safety_factor: float
    search_target_safety_factor: float
    keep_shapes: tuple
    exclude_shapes: tuple
    anchor_soup: TriangleSoup
    search_status: str
    search_termination: str | None


@dataclass(frozen=True)
class FinishPlan:
    """直近の探索記録が次の探索まで保つ計画。common は外部応答用の値だけを持つ。"""

    inputs: CompletionInputs
    accepted: tuple[AcceptedShape, ...]
    best: FinishingShape
    common: dict
    keep_room_warning: str
    unrepresented: list[dict]
    mapping: dict
    evaluations: int

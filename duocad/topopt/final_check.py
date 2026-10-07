"""完成形(平滑化・保持/除外の集合演算の後のメッシュ)の検査(#117)。製品経路 `optimize_run._run` が結果を文書へ足す前に1回呼ぶ。

- **強度**: 完成メッシュを探索の格子へ占有化し、探索と同じ拘束・荷重・材料で FEM を解き直す。**本来の目標安全率**(探索時の目標ではない)を
  割れば呼び出し側が失敗にする(2026-10-07 本人決定)。格子依存・固定面の応力特異点はこの1回では切り分けない(docs/TOPOPT_FINAL_WIDTH_117.md)。
- **つなぎ目の幅**: `neck_persistence.inspect_mesh` の診断(3値。適合は出さない)。

戻り値は JSON 化できる辞書(結果メッセージにそのまま載る):

    {"safety_factor": float|None,           # 完成形の安全率。測れなければ None
     "safety": acceptance.safety_check の戻り | None,
     "conditions": acceptance.condition_check の報告(拘束・荷重の節点が完成形に載ったか),
     "material_cells": int,
     "neck": neck_persistence の戻り}
"""
from __future__ import annotations

import numpy as np

from . import acceptance, neck_persistence, stress, voxelize


def inspect(final_vertices, final_faces, *, grid: voxelize.Grid, conditions, mask: np.ndarray, min_width: dict,
            target_safety_factor: float, keep_cells=None) -> dict:
    vertices = np.asarray(final_vertices, dtype=float)
    faces = np.asarray(final_faces)
    soup = voxelize.TriangleSoup(positions=vertices, triangles=faces, face_index=np.zeros(len(faces), dtype=np.int64))
    material = voxelize.occupancy(soup, grid, allow_empty=True)[0]
    report, fixed, load = acceptance.condition_check(material, conditions.table, conditions.fixed_faces,
                                                     conditions.load_faces, conditions.forces)
    safety = None
    if report['status'] == 'pass':
        safety = acceptance.safety_check(
            material, grid, fixed, load, target=float(target_safety_factor), allowable_mpa=conditions.allowable_mpa,
            youngs_modulus_mpa=conditions.youngs_modulus_mpa,
            percentile=conditions.percentile or stress.DEFAULT_PERCENTILE, solver=conditions.solver)
    keep = None if keep_cells is None or not np.asarray(keep_cells).any() else (np.asarray(keep_cells, dtype=bool), grid)
    neck = neck_persistence.inspect_mesh(
        vertices, faces, spacing=float(grid.spacing), required_mm=float(min_width['requested_mm']),
        min_width_cells=int(min_width['cells']), keep=keep, voxel=(np.asarray(mask, dtype=bool), grid))
    sf = None if safety is None else safety.get('safety_factor')
    return dict(safety_factor=None if sf is None else float(sf), safety=safety, conditions=report,
                material_cells=int(material.sum()), neck=_plain(neck))


def _plain(value):
    """numpy の数値を素の float/int へ(結果メッセージを `json.dumps` が通る形に保つ)。"""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def neck_warning(neck: dict) -> str:
    """つなぎ目の診断の一文(warning に載せる)。疑いがあるときだけ返す。"""
    suspects = [e for e in neck.get('events', []) if e['verdict'] == 'suspect']
    if not suspects:
        return ''
    worst = min(suspects, key=lambda e: e['diameter_mm'][1])
    low, high = worst['diameter_mm']
    x, y, z = worst['position_mm']
    return (f"つなぎ目: 幅不足の疑い {len(suspects)} か所(最も細い所の直径 {max(low, 0.0):.3g}〜{high:.3g} mm、"
            f"位置 ({x:.0f}, {y:.0f}, {z:.0f}) mm。要求 {neck['required_mm']:.3g} mm)")

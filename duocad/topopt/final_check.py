"""完成形(平滑化・保持/除外の集合演算の後のメッシュ)の検査(#117)。製品経路 `optimize_run._run` が結果を文書へ足す前に1回呼ぶ。

- **強度**: 完成メッシュを探索の格子の 1/`REFINE` へ占有化し、探索と同じ拘束・荷重・材料で FEM を解き直す。部材域
  (保持セル以外・拘束/荷重面の節点から探索格子の h 以内でない所。探索の判定と同じ物理範囲)の**表面の面中心の最大応力**で
  安全率を出し、**本来の目標安全率**(探索時の目標ではない)を割れば呼び出し側が失敗にする(2026-10-07 本人決定)。
  最大応力を細かい格子・平滑化後の形で測るのは、探索格子の階段の角と要素中心の評価ではピークが測れないため
  (docs/TOPOPT_STRESS_GATE_DILUTION.md 10章: 平滑化形の表面の最大は 1/3→1/4 でほぼ止まった)。拘束面の1層と保持ボディの
  最大は合否に使わず、参考値として返す(剛固定の理想化と利用者が決めた形の値で、探索では変えられない)。
- **つなぎ目の幅**: `neck_persistence.inspect_mesh` の診断(3値。適合は出さない)。

戻り値は JSON 化できる辞書(結果メッセージにそのまま載る):

    {"safety_factor": float|None,           # 完成形の安全率(部材域の表面の最大応力で)。測れなければ None
     "safety": 強度の報告(下の _strength) | None,
     "conditions": acceptance.condition_check の報告(拘束・荷重の節点が細かい格子の完成形に載ったか)と、
                   作り直した確定表の検証 `mapping_status`・`mapping_reasons`(`anchor.condition_mapping`),
     "material_cells": int,                 # 細かい格子での材料セル数
     "neck": neck_persistence の戻り}
"""
from __future__ import annotations

import math

import numpy as np

from . import acceptance, anchor, neck_persistence, stress, voxelize

#: 探索の格子を何分の1にして解き直すか。平滑化形の表面の最大は 1/3 と 1/4 で 0.4〜5% しか違わなかった(小型2題材の6形)。
REFINE = 3
#: 細かい格子の要素数の上限。`fem.Model` は格子の全要素ぶんの `edof`(要素数×24×8B)と応力の作業配列を持つので、
#: 上限を超える題材は分割数を下げる(hanger 2.4mm 格子の 1/3 は約2,400万要素で `edof` だけで 4.6GB)。
MAX_FINE_ELEMENTS = 8_000_000


def _crop(grid: voxelize.Grid, vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """完成メッシュの境界箱を覆う探索格子の要素範囲 [lo, hi)(1要素の余白つき、格子の内側に切る)。"""
    h = float(grid.spacing)
    origin = np.asarray(grid.origin, dtype=float)
    shape = np.asarray(grid.shape, dtype=np.int64)
    lo = np.floor((vertices.min(axis=0) - origin) / h).astype(np.int64) - 1
    hi = np.ceil((vertices.max(axis=0) - origin) / h).astype(np.int64) + 1
    return np.clip(lo, 0, shape), np.clip(hi, 0, shape)


def _refine_cells(cells: np.ndarray, lo: np.ndarray, hi: np.ndarray, factor: int) -> np.ndarray:
    sub = np.asarray(cells, dtype=bool)[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
    return sub.repeat(factor, axis=0).repeat(factor, axis=1).repeat(factor, axis=2)


def refine_factor(element_count: int) -> int:
    """`element_count`(切り出した探索格子の要素数)に対して使える分割数。`REFINE` から、上限に収まるまで下げる。"""
    factor = REFINE
    while factor > 1 and element_count * factor ** 3 > MAX_FINE_ELEMENTS:
        factor -= 1
    return factor


def _strength(material, region, keep, layer, fine, fixed, load, conditions, target) -> dict:
    solve_info: dict = {}
    if not acceptance.rigid_body_constrained(material, fixed):
        return dict(status='fail', reason='rigid_body_modes', message='剛体運動を拘束できない')
    model, _density, displacement, scale, relative = acceptance.solve_material(
        material, fine, fixed, load, youngs_modulus_mpa=conditions.youngs_modulus_mpa, solver=conditions.solver,
        solve_info=solve_info)

    def peak(cells):
        faces = stress.exposed_face_von_mises(model, displacement, material, scale, cells=cells)
        if not np.isfinite(faces).any():
            return None, None
        flat = int(np.nanargmax(faces))
        cell = np.unravel_index(flat // 6, material.shape)
        position = [float(fine.centers_1d(axis)[cell[axis]]) for axis in range(3)]
        return float(faces.reshape(-1)[flat]), position

    value, position = peak(region)
    keep_peak, _ = peak(keep & material)
    layer_peak, _ = peak(layer & material)
    common = dict(rule='member_surface_peak', allowable_mpa=float(conditions.allowable_mpa),
                  target_safety_factor=float(target), spacing_mm=float(fine.spacing),
                  material_cells=int(material.sum()), member_cells=int(region.sum()),
                  keep_peak_mpa=keep_peak, boundary_layer_peak_mpa=layer_peak, relative_residual=relative,
                  solver=solve_info.get('solver', conditions.solver))
    if value is None or not value > 0.0:
        return dict(status='unknown', reason='no_member_surface', safety_factor=None, peak_mpa=value, **common)
    sf = stress.safety_factor(value, conditions.allowable_mpa)
    return dict(status='pass' if sf >= target else 'fail', safety_factor=float(sf), peak_mpa=value,
                peak_position_mm=position, **common)


def inspect(final_vertices, final_faces, *, grid: voxelize.Grid, conditions, mask: np.ndarray, min_width: dict,
            target_safety_factor: float, anchor_soup: voxelize.TriangleSoup, keep_cells=None) -> dict:
    vertices = np.asarray(final_vertices, dtype=float)
    faces = np.asarray(final_faces)
    soup = voxelize.TriangleSoup(positions=vertices, triangles=faces, face_index=np.zeros(len(faces), dtype=np.int64))
    lo, hi = _crop(grid, vertices)
    factor = refine_factor(int(np.prod(hi - lo)))
    h = float(grid.spacing)
    fine = voxelize.Grid(origin=tuple(float(o) + h * int(i) for o, i in zip(grid.origin, lo)),
                         spacing=h / factor, shape=tuple(int(n) * factor for n in hi - lo))
    material = voxelize.occupancy(soup, fine, allow_empty=True)[0]
    table = anchor.build_anchors(anchor_soup, fine, allowed_nodes=acceptance.active_nodes(material))
    # 作り直した表が条件面を覆っているかを確かめる(`condition_check` は残った節点へ荷重を配り直すので、
    # 完成形が条件面の一部から離れても合格する)
    mapping = anchor.condition_mapping(table, conditions.fixed_faces, conditions.load_faces, conditions.forces,
                                       material=material)
    report, fixed, load = acceptance.condition_check(material, table, conditions.fixed_faces,
                                                     conditions.load_faces, conditions.forces)
    report = dict(report, mapping_status=mapping['status'],
                  mapping_reasons={row['condition']: row['reasons'] for row in mapping['faces'] if row['reasons']})
    # 部材域から外す範囲は探索格子で決める(探索の判定と同じ物理範囲。細かい格子の1層にすると特異点へ近づく)
    nodes = [anchor.face_node_ids(conditions.table, f) for f in tuple(conditions.fixed_faces) + tuple(conditions.load_faces)]
    layer_coarse = stress.boundary_layer_cells(grid.shape, np.concatenate(nodes) if nodes else np.zeros(0, np.int64))
    layer = _refine_cells(layer_coarse, lo, hi, factor)
    keep = (np.zeros(material.shape, dtype=bool) if keep_cells is None or not np.asarray(keep_cells).any()
            else _refine_cells(keep_cells, lo, hi, factor))
    region = material & ~layer & ~keep
    safety = None
    if report['status'] == 'pass' and mapping['status'] != 'pass':
        safety = dict(status='unknown', reason='boundary_mapping_failed', safety_factor=None, refine=int(factor))
    elif report['status'] == 'pass':
        try:
            safety = _strength(material, region, keep, layer, fine, fixed, load, conditions, target_safety_factor)
        except Exception as error:  # 解けない完成形は「測れなかった」として呼び出し側が警告にする
            safety = dict(status='unknown', reason='evaluation_error', error=f'{type(error).__name__}: {error}',
                          safety_factor=None)
        safety['refine'] = int(factor)
    keep_mask = None if keep_cells is None or not np.asarray(keep_cells).any() else (np.asarray(keep_cells, dtype=bool), grid)
    neck = neck_persistence.inspect_mesh(
        vertices, faces, spacing=h, required_mm=float(min_width['requested_mm']),
        min_width_cells=int(min_width['cells']), keep=keep_mask, voxel=(np.asarray(mask, dtype=bool), grid))
    sf = None if safety is None else safety.get('safety_factor')
    return dict(safety_factor=None if sf is None or not math.isfinite(sf) else float(sf), safety=_plain(safety),
                conditions=_plain(report), material_cells=int(material.sum()), neck=_plain(neck))


def peak_phrase(checked: dict) -> str:
    """強度の根拠の一句(警告文に添える)。"""
    safety = checked.get('safety') or {}
    if safety.get('peak_mpa') is None:
        return '最大応力は測れなかった'
    x, y, z = safety.get('peak_position_mm') or (float('nan'),) * 3
    return (f"部材域の表面の最大 {safety['peak_mpa']:.3g} MPa、位置 ({x:.0f}, {y:.0f}, {z:.0f}) mm、"
            f"格子 {safety['spacing_mm']:.3g} mm")


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

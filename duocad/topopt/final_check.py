"""完成形(平滑化・保持/除外の集合演算の後のメッシュ)の検査(#117)。製品経路 `optimize_run._run` が結果を文書へ足す前に1回呼ぶ。

- **強度**: 完成メッシュを間隔 `MEASURE_SPACING_MM` 以下の細かい格子(探索の格子の 1/k)へ占有化し、探索と同じ拘束・荷重・
  材料で FEM を解き直す。部材域(保持セル以外・拘束/荷重面の節点から探索格子の h 以内でない所)の**露出面の面中心の値を
  大きい順に並べ、面積の合計が `TOP_AREA_MM2` に達する位置の値**で安全率を出し、**本来の目標安全率**(探索時の目標ではない)を
  割れば呼び出し側が失敗にする。要素数の上限で `MEASURE_SPACING_MM` より粗くしか測れないときは、測った値に粗さの割り増しを
  掛けてから比べる(docs/TOPOPT_PEAK_GATE.md 4-11・6章)。
  拘束面の1層と保持ボディの最大は合否に使わず参考値として返す(探索では変えられない値)。
- **つなぎ目の幅**: `neck_persistence.inspect_mesh` の診断(3値。適合は出さない)。

戻り値は JSON 化できる辞書(結果メッセージにそのまま載る):

    {"safety_factor": float|None,           # 完成形の安全率(部材域の表面の面積の上位値 × 割り増しで)。測れなければ None
     "safety": 強度の報告(下の _strength) | None,
     "conditions": acceptance.condition_check の報告(拘束・荷重の節点が細かい格子の完成形に載ったか)と、
                   作り直した確定表の検証 `mapping_status`・`mapping_reasons`(`anchor.condition_mapping`),
     "material_cells": int,                 # 細かい格子での材料セル数
     "neck": neck_persistence の戻り}
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

from . import acceptance, anchor, neck_persistence, stress, voxelize

#: 合否に使う応力を測る細かい格子の間隔の上限 [mm]。探索の要素サイズの何分の1かでなく物理寸法で決める。表面の値は細かく
#: 測るほど上がり、要素サイズに比例させると同じ形でも要素サイズで合否が変わる(docs/TOPOPT_PEAK_GATE.md 4-10)。
MEASURE_SPACING_MM = 0.67
#: 合否に使う応力 = 部材域の露出面の面中心の値を大きい順に並べ、面積の合計がこの値 [mm²] に達する位置の値。1面の最大は
#: 階段の入隅で細分のたびに上がり続けて収束しない(k3 → k5 で 1.12〜1.34 倍。docs/TOPOPT_PEAK_GATE.md 4-7・4-9)。
TOP_AREA_MM2 = 4.0
#: `MEASURE_SPACING_MM` より粗くしか測れないとき、測った応力に掛ける割り増し (間隔 ÷ MEASURE_SPACING_MM) ^ この値。
#: 粗く測ると低く出る(危険側)。実測 48 組(間隔の比 1.25〜2.5)で最も低く出た組の 0.412 を覆う値で、比 2.5 より粗い所は
#: 延長(docs/TOPOPT_PEAK_GATE.md 4-11)。
COARSE_PENALTY_EXPONENT = 0.42
#: 細かい格子の要素数の上限(空きメモリに依らない)。`fem.Model` は格子の全要素ぶんの `edof`(要素数×24×8B)と応力の
#: 作業配列を持つ(hanger 2.4mm 格子の 1/3 は約2,400万要素で `edof` だけで 4.6GB)。超える題材は分割数を下げる。
MAX_FINE_ELEMENTS = 8_000_000
#: 検査のメモリの見積もり = 細かい格子の要素数 × FINE_ELEMENT_BYTES + 材料セル数 × MATERIAL_CELL_BYTES [B]。材料セル数は
#: 探索の形のセル数 × 分割数³ で見積もる(細かい格子の材料は実測でその 1.007〜1.025 倍)。係数は dilution・path の実測の
#: 確定メモリのピークを覆う値(docs/TOPOPT_PEAK_GATE.md 4-11)。剛性行列の組み立てが材料セルごとに 576 個の値を持つので
#: 材料の項が支配する。
FINE_ELEMENT_BYTES = 500
MATERIAL_CELL_BYTES = 21_000
#: 見積もりが今の空き物理メモリのこの割合を超える分割数は使わない(検査中に他のアプリが増える分の余白。置いた値で実測ではない)。
MEMORY_FRACTION = 0.5


def _available_memory_bytes():
    """今の空き物理メモリ [B]。取れなければ None。"""
    try:
        if sys.platform == 'win32':
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return int(status.ullAvailPhys)
        return int(os.sysconf('SC_AVPHYS_PAGES')) * int(os.sysconf('SC_PAGE_SIZE'))
    except (AttributeError, OSError, ValueError):
        return None


def estimated_bytes(element_count: int, material_count: int, factor: int) -> int:
    """探索の格子で `element_count` 要素・材料 `material_count` セルの範囲を、分割数 `factor` で検査するときのメモリの見積もり。"""
    return int((element_count * FINE_ELEMENT_BYTES + material_count * MATERIAL_CELL_BYTES) * factor ** 3)


def fits(element_count: int, material_count: int, factor: int, available_bytes) -> bool:
    """分割数 `factor` の細かい格子が `MAX_FINE_ELEMENTS` と空きメモリ(`available_bytes`。None なら見ない)に収まるか。"""
    if element_count * factor ** 3 > MAX_FINE_ELEMENTS:
        return False
    return available_bytes is None or estimated_bytes(element_count, material_count, factor) <= available_bytes * MEMORY_FRACTION


def coarse_penalty(spacing_mm: float) -> float:
    """間隔 `spacing_mm` で測った応力に掛ける割り増し(`MEASURE_SPACING_MM` 以下なら 1)。"""
    return max(1.0, float(spacing_mm) / MEASURE_SPACING_MM) ** COARSE_PENALTY_EXPONENT


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


def refine_factor(element_count: int, material_count: int, spacing_mm: float, available_bytes) -> int:
    """切り出した探索格子(`element_count` 要素・材料 `material_count` セル、間隔 `spacing_mm`)に対する分割数。間隔が
    `MEASURE_SPACING_MM` 以下になる最小の分割数から、`fits` になるまで下げる(1 で収まらなくても 1 を返す)。"""
    factor = max(1, math.ceil(float(spacing_mm) / MEASURE_SPACING_MM - 1e-9))
    while factor > 1 and not fits(element_count, material_count, factor, available_bytes):
        factor -= 1
    return factor


def _occupy(soup, grid: voxelize.Grid, lo: np.ndarray, hi: np.ndarray, material_count: int, available_bytes):
    """分割数を選んで完成メッシュを細かい格子へ占有化する。戻り値 `(factor, fine, material, material_count)`、
    分割数 1 でも収まらなければ `(None, None, None, material_count)`。

    `material_count` は探索の形のセル数で、探索の格子に載らない保持ボディの薄い部分などは数えていない。占有化した材料で
    数え直し、収まらなければ分割数を下げてやり直す(占有化そのものは要素数に比例し `MAX_FINE_ELEMENTS` で抑えてある)。
    """
    h = float(grid.spacing)
    elements = int(np.prod(hi - lo))
    factor = refine_factor(elements, material_count, h, available_bytes)
    while fits(elements, material_count, factor, available_bytes):
        fine = voxelize.Grid(origin=tuple(float(o) + h * int(i) for o, i in zip(grid.origin, lo)),
                             spacing=h / factor, shape=tuple(int(n) * factor for n in hi - lo))
        material = voxelize.occupancy(soup, fine, allow_empty=True)[0]
        counted = math.ceil(int(material.sum()) / factor ** 3)
        if fits(elements, counted, factor, available_bytes):
            return factor, fine, material, max(material_count, counted)
        material_count = max(material_count, counted)
        if factor == 1:
            break
        factor = min(factor - 1, refine_factor(elements, material_count, h, available_bytes))
    return None, None, None, material_count


def top_area_value(values: np.ndarray, area_mm2: float, spacing_mm: float) -> float:
    """面中心の値 `values`(1面の面積は spacing²)を大きい順に並べ、面積の合計が `area_mm2` に達する位置の値。
    面が足りなければ最小の値。"""
    ordered = np.sort(np.asarray(values, dtype=float))[::-1]
    count = max(1, math.ceil(area_mm2 / float(spacing_mm) ** 2 - 1e-9))
    return float(ordered[min(ordered.size, count) - 1])


def _strength(material, region, keep, layer, fine, fixed, load, conditions, target) -> dict:
    solve_info: dict = {}
    if not acceptance.rigid_body_constrained(material, fixed):
        return dict(status='unknown', reason='rigid_body_modes', message='剛体運動を拘束できない', safety_factor=None)
    model, _density, displacement, scale, relative = acceptance.solve_material(
        material, fine, fixed, load, youngs_modulus_mpa=conditions.youngs_modulus_mpa, solver=conditions.solver,
        solve_info=solve_info)

    def peak(cells):
        faces = stress.exposed_face_von_mises(model, displacement, material, scale, cells=cells)
        if not np.isfinite(faces).any():
            return None, None, None
        flat = int(np.nanargmax(faces))
        cell = np.unravel_index(flat // 6, material.shape)
        position = [float(fine.centers_1d(axis)[cell[axis]]) for axis in range(3)]
        face, half = flat % 6, 0.5 * float(fine.spacing)
        position[face // 2] += -half if face % 2 == 0 else half  # 面の順は (-x,+x,-y,+y,-z,+z)
        return float(faces.reshape(-1)[flat]), position, faces[np.isfinite(faces)]

    value, position, faces = peak(region)
    keep_peak, _, _ = peak(keep & material)
    layer_peak, _, _ = peak(layer & material)
    penalty = coarse_penalty(fine.spacing)
    common = dict(rule='member_surface_top_area', allowable_mpa=float(conditions.allowable_mpa),
                  target_safety_factor=float(target), spacing_mm=float(fine.spacing),
                  measure_spacing_mm=MEASURE_SPACING_MM, top_area_mm2=TOP_AREA_MM2, coarse_penalty=penalty,
                  material_cells=int(material.sum()), member_cells=int(region.sum()), peak_mpa=value,
                  peak_position_mm=position, keep_peak_mpa=keep_peak, boundary_layer_peak_mpa=layer_peak,
                  relative_residual=relative, solver=solve_info.get('solver', conditions.solver))
    if value is None or not value > 0.0:
        return dict(status='unknown', reason='no_member_surface', safety_factor=None, **common)
    top_area = top_area_value(faces, TOP_AREA_MM2, fine.spacing)
    judged = top_area * penalty
    sf = stress.safety_factor(judged, conditions.allowable_mpa)
    return dict(status='pass' if sf >= target else 'fail', safety_factor=float(sf), top_area_mpa=top_area,
                judged_mpa=judged, **common)


def inspect(final_vertices, final_faces, *, grid: voxelize.Grid, conditions, mask: np.ndarray, min_width: dict,
            target_safety_factor: float, anchor_soup: voxelize.TriangleSoup, keep_cells=None) -> dict:
    vertices = np.asarray(final_vertices, dtype=float)
    faces = np.asarray(final_faces)
    soup = voxelize.TriangleSoup(positions=vertices, triangles=faces, face_index=np.zeros(len(faces), dtype=np.int64))
    lo, hi = _crop(grid, vertices)
    h = float(grid.spacing)
    elements = int(np.prod(hi - lo))
    available = _available_memory_bytes()
    factor, fine, material, material_count = _occupy(
        soup, grid, lo, hi, int(np.asarray(mask, dtype=bool)[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]].sum()), available)
    neck_keep = None if keep_cells is None or not np.asarray(keep_cells).any() else (np.asarray(keep_cells, dtype=bool), grid)
    if factor is None:
        # 分割しなくても上限か空きメモリを超える。巨大な配列を確保して落ちる前に、測らなかったと返す
        neck = neck_persistence.inspect_mesh(
            vertices, faces, spacing=h, required_mm=float(min_width['requested_mm']),
            min_width_cells=int(min_width['cells']), keep=neck_keep, voxel=(np.asarray(mask, dtype=bool), grid))
        return dict(safety_factor=None, safety=dict(status='unknown', reason='grid_too_large', safety_factor=None,
                                                    elements=elements, limit=MAX_FINE_ELEMENTS,
                                                    estimated_bytes=estimated_bytes(elements, material_count, 1),
                                                    available_bytes=available),
                    conditions=dict(status='unknown', reason='grid_too_large'), material_cells=0, neck=_plain(neck))
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
    neck = neck_persistence.inspect_mesh(
        vertices, faces, spacing=h, required_mm=float(min_width['requested_mm']),
        min_width_cells=int(min_width['cells']), keep=neck_keep, voxel=(np.asarray(mask, dtype=bool), grid))
    sf = None if safety is None else safety.get('safety_factor')
    return dict(safety_factor=None if sf is None or not math.isfinite(sf) else float(sf), safety=_plain(safety),
                conditions=_plain(report), material_cells=int(material.sum()), neck=_plain(neck))


def peak_phrase(checked: dict) -> str:
    """強度の根拠の一句(警告文に添える)。"""
    safety = checked.get('safety') or {}
    if safety.get('judged_mpa') is None:
        return '表面の応力は測れなかった'
    x, y, z = safety.get('peak_position_mm') or (float('nan'),) * 3
    penalty = safety.get('coarse_penalty') or 1.0
    coarse = '' if penalty <= 1.0 else f"、{safety['measure_spacing_mm']:.3g} mm より粗いので {penalty:.3g} 倍に割り増し"
    return (f"部材域の表面で面積 {safety['top_area_mm2']:.3g} mm² に達する応力 {safety['top_area_mpa']:.3g} MPa"
            f"(格子 {safety['spacing_mm']:.3g} mm{coarse})、表面の最大 {safety['peak_mpa']:.3g} MPa の位置 "
            f"({x:.0f}, {y:.0f}, {z:.0f}) mm")


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

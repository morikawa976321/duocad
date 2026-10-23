"""文書・過去の密度場記録を、数値受入検査の入力へ変換する。"""
from __future__ import annotations

import numpy as np

from .topopt import anchor, voxelize
from .topopt.acceptance import inspect, _overall_status


def inspect_document(document, rho_clean, rho_before, grid, *, level=0.5, **kwargs):
    """過去記録(密度場)を読む**読み取りアダプター**。密度→bool変換をここに閉じ、
    boolマスクを直接受ける `inspect` を呼ぶ(`docs/contracts/topopt-binary-search.md`)。

    密度積算量(SIMP時代の指標。過去記録にしか存在しない)は、二値領域の体積とは別に
    `volumes.density_integral_*_mm3` として付記する(`docs/TOPOPT_ACCEPTANCE.md`
    「密度積算量と、この二値領域の体積は別々に報告する」)。
    """
    before = np.asarray(rho_before, dtype=float)
    clean = np.asarray(rho_clean, dtype=float)
    for field, field_name in ((before, 'rho_before'), (clean, 'rho_clean')):
        if field.shape != tuple(grid.shape) or not np.isfinite(field).all() or np.any((field < 0) | (field > 1)):
            raise ValueError(f'{field_name} の形・値が不正(格子と同じ形の有限な0〜1の密度であること)')
    if not 0 <= level < 1:
        raise ValueError('閾値levelが不正')
    material, original = clean > level, before > level
    from duocad.optimize_run import build_conditions
    kwargs.setdefault('bbox_cache', document.bbox_cache)
    bundle = build_conditions(document)
    soup = voxelize.triangles_from_shape(voxelize.unwrap(bundle.conditions.anchor),
                                         deviation=grid.spacing * voxelize.DEFAULT_DEVIATION_RATIO)
    # 「元の条件」は材料に対応付けた確定表(`docs/TOPOPT_ACCEPTANCE.md`)。過去記録には
    # 設計空間が無いので、島除去前の材料(候補が使えた材料の上限)に接する節点へ対応付ける。
    # 対応付けが不成立(対応不能標本など)なら、節点だけを見る条件5を合格にしない。
    table = anchor.build_anchors(soup, grid, allowed_nodes=anchor.active_nodes(original))
    mapping = anchor.condition_mapping(table, bundle.conditions.fixed_faces, bundle.conditions.load_faces,
                                       bundle.forces, material=original)
    setup = document.optimize
    report = inspect(material, original, grid, table=table,
                     fixed_faces=bundle.conditions.fixed_faces, load_faces=bundle.conditions.load_faces,
                     forces=bundle.forces,
                     preserves=[(name, document.get_body(name).shape) for name in setup.preserve_bodies],
                     excludes=[(name, document.get_body(name).shape) for name in setup.exclude_bodies], **kwargs)
    report['boundary_mapping'] = mapping
    if mapping['status'] != 'pass':
        report['checks']['conditions']['status'] = 'fail'
        report['checks']['conditions']['mapping_reasons'] = {
            row['condition']: row['reasons'] for row in mapping['faces'] if row['reasons']}
        report['status'] = _overall_status([check['status'] for check in report['checks'].values()])
        report['exit_code'] = {'pass': 0, 'fail': 1, 'unknown': 2}[report['status']]
    report['level'] = level
    report['threshold_rule'] = 'rho > level'
    report['volumes']['density_integral_before_mm3'] = float(before.sum() * grid.element_volume)
    report['volumes']['density_integral_after_mm3'] = float(clean.sum() * grid.element_volume)
    return report

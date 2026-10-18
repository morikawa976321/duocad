"""面の由来(duocad の `face_map` / `deleted` / `new_faces`)が各方式の履歴から取れるか、と再生の決定性。

    .venv/Scripts/python.exe -m tools.face_edit_probe.provenance            # 由来の取れ方
    .venv/Scripts/python.exe -m tools.face_edit_probe.provenance --sig NAME METHOD   # 面の並びの指紋(決定性の比較用)

由来の数え方は `duocad/kernel.py` の `_blend_provenance` と同じ(Modified があればそれ、消されていれば空、
どちらでもなければ同じ面がそのまま残っている)。`unresolved` は「残るはずの面が結果の中に見つからない」数で、
0 でなければ履歴だけでは `face_map` を作れない。
"""
from __future__ import annotations

import hashlib
import json
import sys

from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedMapOfShape

from . import geometry as g
from . import methods, shapes


def _map(shape):
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, m)
    return m


def successors(source, before, after):
    """`source` は Modified / IsDeleted(または IsRemoved)を持つもの。"""
    before_map, after_map = _map(before), _map(after)
    table = {}
    for i in range(1, before_map.Extent() + 1):
        face = before_map.FindKey(i)
        try:
            modified = list(source.Modified(face))
        except Exception:  # noqa: BLE001
            modified = []
        removed = False
        for name in ("IsRemoved", "IsDeleted"):
            if hasattr(source, name):
                try:
                    removed = bool(getattr(source, name)(face))
                except Exception:  # noqa: BLE001
                    removed = False
                break
        candidates = modified if modified else ([] if removed else [face])
        table[i] = sorted({after_map.FindIndex(c) for c in candidates} - {0})
        table[i] = (table[i], bool(modified), removed, len(candidates))
    mapped = {p for v in table.values() for p in v[0]}
    unresolved = [i for i, (pos, _, removed, n) in table.items() if not pos and not removed]
    new = [j for j in range(1, after_map.Extent() + 1) if j not in mapped]
    return table, unresolved, new


def signature(shape):
    rows = []
    for face in g.faces_of(shape):
        c = g.face_centroid(face)
        rows.append((g.surface_type(face), round(c[0], 6), round(c[1], 6), round(c[2], 6), round(g.face_area(face), 6)))
    return hashlib.sha1(json.dumps(rows).encode()).hexdigest()[:12], len(rows)


def run_method(case, method):
    from .worker import run

    return run(case, method)


def main():
    cases = {c.name: c for c in shapes.all_cases()}
    print("case                      method  faces before->after  removed  unresolved  new_faces")
    for name in ("d_fillet_1edge", "d_fillet_all12", "d_counterbore_cbore_only", "d_cross_holes_x", "d_step_fillet_all12"):
        case = cases[name]
        result, algo = methods.defeature(case.shape, case.faces)
        table, unresolved, new = successors(algo.History(), case.shape, result)
        removed = sum(1 for v in table.values() if v[2])
        print(f"{name:26s} A      {len(table):3d} -> {len(g.faces_of(result)):3d}           {removed:3d}      {len(unresolved):3d}        {len(new):3d}")
    from .worker import as_solid

    for name in ("m_box_top_up5", "m_hole_shrink1", "m_wedge_top_up4", "m_boss_grow1", "m_step_hole_shrink1"):
        case = cases[name]
        raw, algo = methods.offset_faces(case.shape, case.faces, case.distance)
        result = as_solid(raw)
        table, unresolved, new = successors(algo, case.shape, result)
        print(f"{name:26s} B0     {len(table):3d} -> {len(g.faces_of(result)):3d}           {sum(1 for v in table.values() if v[2]):3d}      {len(unresolved):3d}        {len(new):3d}"
              f"   (Modified non-empty for {sum(1 for v in table.values() if v[1])} faces)")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--sig"]:
        case = next(c for c in shapes.all_cases() if c.name == sys.argv[2])
        print(json.dumps(signature(run_method(case, sys.argv[3]))))
    else:
        main()

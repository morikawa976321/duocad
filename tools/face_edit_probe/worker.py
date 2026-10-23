"""1ケース × 1方式を回して JSON を1行出す(run.py が別プロセスで呼ぶ)。

    .venv/Scripts/python.exe -m tools.face_edit_probe.worker d_fillet_1edge A

どの方式の結果にも同じ関門(`gate`)を通す。これが実装での「成功として通してよいか」の判定の試作でもある。
"""
from __future__ import annotations

import json
import sys
import time

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
from OCP.BRepLib import BRepLib
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

from . import geometry as g
from . import methods, shapes


def as_solid(shape):
    """MakeOffset は穴やフィレットを含む形で SOLID でなく SHELL を返す(m_hole_shrink1 で実測)。閉じたシェル1枚なら包む。"""
    if g.solids_of(shape):
        return shape
    shells = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SHELL)
    while explorer.More():
        shells.append(TopoDS.Shell_s(explorer.Current()))
        explorer.Next()
    if len(shells) != 1:
        return shape
    solid = BRepBuilderAPI_MakeSolid(shells[0]).Solid()
    BRepLib.OrientClosedSolid_s(solid)
    return solid


def gate(case, result, following_blends=False):
    """成功として通してよいか。通らなければ理由の文字列、通れば None。"""
    if result is None or result.IsNull():
        return "empty"
    solids = g.solids_of(result)
    if len(solids) != 1:
        return f"solids={len(solids)}"
    if not g.is_valid(result):
        return "BRepCheck invalid"
    free = g.free_edge_count(result)
    if free:
        return f"free edges={free}"
    v_after = g.volume(result)
    if not v_after > 0:
        return f"volume={v_after:.6g}"
    faces_after = g.faces_of(result)
    tiny = [f for f in faces_after if g.face_area(f) < MIN_FACE_AREA]
    if tiny:
        return f"{len(tiny)} face(s) with area < {MIN_FACE_AREA:g} (a face vanished but stayed in the topology)"
    kept = [f for f in g.faces_of(case.shape) if not any(f.IsSame(x) for x in case.faces)]
    if case.kind == "delete":
        if any(f.IsSame(r) for f in case.faces for r in faces_after):
            return "a face to remove is still there"
        allowed = [_surface(f) for f in kept]
    else:
        change = v_after - g.volume(case.shape)
        if change * case.distance <= 0:
            return f"volume moved the wrong way ({change:+.6g} for distance {case.distance:+g})"
        if len(faces_after) != len(g.faces_of(case.shape)):
            return f"topology changed (faces {len(g.faces_of(case.shape))} -> {len(faces_after)})"
        allowed = [_surface(f) for f in kept] + [_surface(_offset(f, case.distance, case.shape)) for f in case.faces]
    skip = BLEND_KINDS if following_blends else ()
    if case.kind == "delete":
        # B-spline の隣を延ばすと曲面そのものが作り直され、元の曲面の範囲の外に乗る(hook_plate_screw で実測)
        skip = skip + FREEFORM_KINDS
    stray = [f for f in faces_after if g.surface_type(f) not in skip and not _on_any(f, allowed)]
    if stray:
        return f"{len(stray)} face(s) left their own surface (e.g. {g.surface_type(stray[0])} at {tuple(round(c, 3) for c in g.face_centroid(stray[0]))})"
    return None


BLEND_KINDS = ("cylinder", "torus", "sphere")
FREEFORM_KINDS = ("bspline", "bezier", "revolution", "extrusion", "offset", "other")
#: 面積0の面が位相に残る結果(m_step_riser_to_end の B0 で実測)を断るしきい値[mm²]。置いた値で、測った値ではない
MIN_FACE_AREA = 1e-6


def _surface(face):
    from OCP.BRep import BRep_Tool

    return BRep_Tool.Surface_s(face)


def _offset(face, distance, shape):
    from .custom import _offset_big_face, _size

    return _offset_big_face(face, distance, shape, _size(shape))


def _on_any(face, surfaces):
    """面が `surfaces` のどれかに乗っているか(面の内側の1点で見る)。隣の面が「自分の曲面のまま」延びたかの検査。"""
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf

    from .custom import _sample

    point, _ = _sample(face)
    for surface in surfaces:
        projector = GeomAPI_ProjectPointOnSurf(point, surface)
        if projector.NbPoints() and projector.LowerDistance() < 1e-6:
            return True
    return False


def run(case, method):
    if method == "A":
        result, _ = methods.defeature(case.shape, case.faces)
    elif method in ("B0", "B1"):
        result, _ = methods.offset_faces(case.shape, case.faces, case.distance, intersection=method == "B1")
        result = as_solid(result)
    elif method == "C":
        from . import custom

        if case.kind == "delete":
            result = custom.rebuild_delete(case.shape, case.faces)
        else:
            result = custom.rebuild_move(case.shape, case.faces, case.distance)
    elif method in ("DB", "DC"):
        from . import blend

        result = blend.move_following_blends(case.shape, case.faces, case.distance, mover=method[1])
    else:
        raise SystemExit(f"unknown method {method}")
    return result


def main(name, method):
    case = next(c for c in shapes.all_cases() if c.name == name)
    record = {"case": name, "method": method, "note": case.note, "tags": case.tags, "faces_before": len(g.faces_of(case.shape))}
    started = time.perf_counter()
    try:
        result = run(case, method)
    except Exception as error:  # noqa: BLE001  方式が断った(例外)
        record.update(verdict="refused", error=f"{type(error).__name__}: {error}", ms=1000 * (time.perf_counter() - started))
        return record
    record["ms"] = 1000 * (time.perf_counter() - started)
    reason = gate(case, result, following_blends=method in ("DB", "DC"))
    try:
        record.update(volume=g.volume(result), faces=len(g.faces_of(result)))
    except Exception:  # noqa: BLE001
        pass
    if reason:
        record.update(verdict="refused", error=f"gate: {reason}")
        return record
    if case.expected is None:
        record.update(verdict="wrong", error="passed although it should fail")
    elif case.expected is shapes.UNKNOWN:
        record.update(verdict="recorded")
    else:
        diff = g.symmetric_difference(result, case.expected)
        record.update(symdiff=diff, expected_faces=len(g.faces_of(case.expected)), expected_volume=g.volume(case.expected))
        tolerance = 1e-6 * max(1.0, g.volume(case.expected))
        record.update(verdict="match" if diff <= tolerance else "wrong")
    return record


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1], sys.argv[2]), ensure_ascii=False))

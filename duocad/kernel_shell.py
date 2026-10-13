"""シェル(中空化)の幾何(OCPを触るカーネル層)。

抜く面(開口)を選び、外形から内側へ一様な厚さの殻にする(`BRepOffsetAPI_MakeThickSolid`)。
面の数え方・検査・由来の射影は `kernel` の側にあり、ここはそれを読むだけ ——
依存は kernel_shell → kernel の一方向(`kernel_extrude` と同じ)。

戻り値の規律は kernel と同じ(JSON化できる素の値だけ)。例外は `"shape"`。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepOffset import BRepOffset_Mode
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
from OCP.GeomAbs import GeomAbs_JoinType
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopTools import TopTools_ListOfShape
from OCP.TopoDS import TopoDS, TopoDS_Shape  # noqa: F401  TopoDS_Shape は型注釈だけ

from .kernel import (
    _NO_CHANGE_RTOL,
    _checked_volume,
    _contains_identity,
    _face_at,
    _face_map,
    _face_map_projection,
    _lookup_by_identity,
    _positions_in,
    _require_valid,
    normalize_provenance,
    volume,
)
from .mesh_geometry import compound, indexed_map, subshapes

#: `MakeThickSolidByJoin` の許容差(mm)の上限。2026-10-13 の実測(docs/CAD_LIBRARY_PITFALLS.md)はこの値で取った。
#: 1e-6 に変えても、抜く面がフィレット面に接するときの失敗は変わらなかった。厚さがこれに近いと
#: `IsDone()=False` になる(箱の上面を抜いて厚さ 0.001 で実測)ので、薄い壁では厚さの 1/1000 まで下げる。
_TOLERANCE = 1.0e-3

#: 面どうしの距離が測れないとき、内壁の辺1本を何等分した点から測るか。
_EDGE_SAMPLES = 8

_FILLET_HINT = "抜く面に丸めた面が接しているなら、先にシェルを掛けてから丸める"


def shell_solid(shape: "TopoDS_Shape", face_indices: Sequence[int], thickness: float) -> dict:
    """`face_indices`(1始まり)の面を抜き、外形から内側へ `thickness` の殻にした**新しい形状**を返す。

    入力の `shape` は変更しない。**抜く面は1枚以上**(空だと OCCT は中空ではなく内側へ縮めた中身を返す)。

    **`IsDone()` も `BRepCheck` も真のまま何もしない・別物を返すことがある**(厚すぎる・抜く面が
    フィレット面に接する・出っ張りだけ薄い。docs/CAD_LIBRARY_PITFALLS.md)ので、Solid がちょうど1つ・
    体積が減った・内壁が外側から `thickness` 以上離れていることまで見る。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape": <TopoDS_Shape>, "provenance": {...}, "volume_before": float,
         "volume_after": float}
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot shell an empty shape")
    indices = [int(index) for index in face_indices]
    if not indices:
        raise ValueError("shell requires at least one face to open")
    if len(set(indices)) != len(indices):
        raise ValueError(f"shell got the same face twice: {indices}")
    value = float(thickness)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"thickness must be finite and > 0, got {thickness!r}")

    face_map = _face_map(shape)
    closing = TopTools_ListOfShape()
    for index in indices:
        closing.Append(_face_at(face_map, index))

    volume_before = volume(shape)
    maker = BRepOffsetAPI_MakeThickSolid()
    tolerance = min(_TOLERANCE, value * 1.0e-3)
    context = f"faces {indices}, thickness={value}"
    try:
        # 負の厚さ = 内側へ取る(外形寸法を保つ)。内側の角は `Intersection`(尖ったまま):
        # `Arc` は全辺を丸めた箱で BRepCheck 偽の形を返した(2026-10-13 実測)。
        maker.MakeThickSolidByJoin(
            shape,
            closing,
            -value,
            tolerance,
            BRepOffset_Mode.BRepOffset_Skin,
            False,
            False,
            GeomAbs_JoinType.GeomAbs_Intersection,
            False,
        )
        maker.Build()
    except Exception as error:  # noqa: BLE001  OCCTは例外でも失敗しうる
        raise ValueError(f"shell failed while building ({context}): {error}") from error
    if not maker.IsDone():
        raise ValueError(
            f"shell failed (IsDone is false) ({context}): "
            f"厚さが丸めた辺の半径以上か、壁どうしがぶつかる厚さ。{_FILLET_HINT}"
        )

    final = maker.Shape()
    if final is None or final.IsNull():
        raise ValueError(f"shell produced an empty shape ({context})")
    solids = indexed_map(final, TopAbs_ShapeEnum.TopAbs_SOLID).Extent()
    if solids != 1:
        raise ValueError(f"shell produced {solids} solids instead of one ({context})")
    volume_after = _checked_volume(final, "shell", context=context)
    if not volume_after < volume_before * (1.0 - _NO_CHANGE_RTOL):
        raise ValueError(
            f"shell did not hollow the body (volume {volume_before} -> {volume_after}, {context}): "
            f"厚さが大きすぎる。{_FILLET_HINT}"
        )
    _require_valid(final, "shell", context)
    provenance = _shell_provenance(maker, shape, final, indices, value)
    _require_wall_thickness(shape, indices, final, provenance["new_faces"], value, tolerance, context)

    return {
        "shape": final,
        "provenance": provenance,
        "volume_before": float(volume_before),
        "volume_after": float(volume_after),
    }


def _require_wall_thickness(
    shape: "TopoDS_Shape",
    opened: List[int],
    final: "TopoDS_Shape",
    inner_faces: List[int],
    thickness: float,
    tolerance: float,
    context: str,
) -> None:
    """内側にできた面と、抜かなかった外側の面との最短距離が `thickness` を割れば断る。

    厚さが出っ張りの半径を超えると、OCCT はそこだけ薄い壁のまま成功を返す(40×30×20 の箱に半径3の
    円柱を足し、底を抜いて厚さ4で、円柱の壁が2になった。2026-10-14 実測)。体積も BRepCheck も通る。
    抜いた面は内壁の縁と接するので外側に数えない。指定より厚い壁は検出しない。
    """
    shape_map = _face_map(shape)
    final_map = _face_map(final)
    outer = [shape_map.FindKey(index) for index in range(1, shape_map.Extent() + 1) if index not in opened]
    opened_surfaces = [BRep_Tool.Surface_s(TopoDS.Face_s(shape_map.FindKey(index))) for index in opened]
    inner = [
        final_map.FindKey(index)
        for index in inner_faces
        if not _lies_on(final_map.FindKey(index), opened_surfaces, tolerance)
    ]
    if not inner:
        raise ValueError(f"shell made no inner faces ({context})")
    thinnest = _distance(compound(inner), compound(outer))
    if thinnest is None:
        thinnest = min(_face_distance(face, other, context) for face in inner for other in outer)
    if thinnest < thickness * (1.0 - 1.0e-6) - Precision.Confusion_s():
        raise ValueError(
            f"shell left a wall thinner than requested (thinnest {thinnest:.4g} mm, {context}): "
            "厚さが出っ張り・細い部分の半径を超えている"
        )


def _lies_on(face: "TopoDS_Shape", surfaces: Sequence[Any], tolerance: float) -> bool:
    """`face` の縁の点がどれか1つの曲面に全部乗っているか(抜いた面の上に残る開口の縁)。

    縁は由来では抜いた面の後継として記録されることも、新しい面として記録されることもある
    (NURBS に変換した箱では後者。sol の指摘で実測)。外側の側面に接するので、内壁に数えると距離0で断ってしまう。
    """
    points = [BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertex)) for vertex in _boundary_points(face)]
    for surface in surfaces:
        if all(_on_surface(point, surface, tolerance) for point in points):
            return True
    return False


def _on_surface(point: Any, surface: Any, tolerance: float) -> bool:
    projection = GeomAPI_ProjectPointOnSurf(point, surface)
    return projection.NbPoints() > 0 and projection.LowerDistance() <= tolerance


def _distance(first: "TopoDS_Shape", second: "TopoDS_Shape") -> Optional[float]:
    """最短距離。OCCT が測れなければ None。

    同心の円筒どうし・球と円筒のように最短の組が無数にある面の対では `StdFail_NotDone` を投げる
    (丸めた箱の内壁と外壁で実測)。対を1つでも含む複合形状も同じく落ちる。
    """
    try:
        measure = BRepExtrema_DistShapeShape(first, second)
        if not measure.IsDone():
            return None
        return float(measure.Value())
    except Exception:  # noqa: BLE001  StdFail_NotDone は OCP の例外型で、型で絞れない
        return None


def _face_distance(face: "TopoDS_Shape", other: "TopoDS_Shape", context: str) -> float:
    """面どうしが測れなければ、`face` の縁の上の点(頂点と辺の等分点)から `other` までを測る。"""
    whole = _distance(face, other)
    if whole is not None:
        return whole
    distances = [_distance(point, other) for point in _boundary_points(face)]
    if not distances or any(distance is None for distance in distances):
        raise ValueError(f"shell could not measure the wall thickness ({context})")
    return min(distances)


def _boundary_points(face: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    points = []
    for edge_shape in subshapes(face, TopAbs_ShapeEnum.TopAbs_EDGE):
        edge = TopoDS.Edge_s(edge_shape)
        if BRep_Tool.Degenerated_s(edge):
            continue
        curve = BRepAdaptor_Curve(edge)
        first, last = curve.FirstParameter(), curve.LastParameter()
        for step in range(_EDGE_SAMPLES + 1):
            point = curve.Value(first + (last - first) * step / _EDGE_SAMPLES)
            points.append(BRepBuilderAPI_MakeVertex(point).Vertex())
    return points


def _shell_provenance(
    maker: Any, shape: "TopoDS_Shape", final: "TopoDS_Shape", indices: List[int], thickness: float
) -> dict:
    """由来を `face_map` / `deleted` / `new_faces` の射影へ落とす(`kernel._blend_provenance` と同じ形)。"""
    normalized = normalize_provenance(maker, [shape]) or {}
    modified = {entry["input"]: entry["results"] for entry in normalized.get("modified", [])}
    deleted_inputs = normalized.get("deleted", [])

    shape_map = _face_map(shape)
    final_map = _face_map(final)
    successors: dict = {}
    for index in range(1, shape_map.Extent() + 1):
        old_face = shape_map.FindKey(index)
        candidates = _lookup_by_identity(modified, old_face)
        if candidates is None:
            candidates = [] if _contains_identity(deleted_inputs, old_face) else [old_face]
        successors[str(index)] = _positions_in(final_map, candidates)

    return {
        "verb": "shell_body",
        "faces": list(indices),
        "thickness": thickness,
        **_face_map_projection(successors, final_map.Extent()),
    }

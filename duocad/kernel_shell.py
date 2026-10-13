"""シェル(中空化)の幾何(OCPを触るカーネル層)。

抜く面(開口)を選び、外形から内側へ一様な厚さの殻にする(`BRepOffsetAPI_MakeThickSolid`)。
面の数え方・検査・由来の射影は `kernel` の側にあり、ここはそれを読むだけ ——
依存は kernel_shell → kernel の一方向(`kernel_extrude` と同じ)。

戻り値の規律は kernel と同じ(JSON化できる素の値だけ)。例外は `"shape"`。
"""
from __future__ import annotations

import math
from typing import Any, List, Sequence

from OCP.BRepOffset import BRepOffset_Mode
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
from OCP.GeomAbs import GeomAbs_JoinType
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopTools import TopTools_ListOfShape
from OCP.TopoDS import TopoDS_Shape  # noqa: F401  型注釈だけ

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
from .mesh_geometry import indexed_map

#: `MakeThickSolidByJoin` の許容差(mm)。2026-10-13 の実測(docs/CAD_LIBRARY_PITFALLS.md)はこの値で取った。
#: 1e-6 に変えても、抜く面がフィレット面に接するときの失敗は変わらなかった。
_TOLERANCE = 1.0e-3


def shell_solid(shape: "TopoDS_Shape", face_indices: Sequence[int], thickness: float) -> dict:
    """`face_indices`(1始まり)の面を抜き、外形から内側へ `thickness` の殻にした**新しい形状**を返す。

    入力の `shape` は変更しない。**抜く面は1枚以上**(空だと OCCT は中空ではなく内側へ縮めた中身を返す)。

    **`IsDone()` も `BRepCheck` も真のまま何もしない・別物を返すことがある**(厚すぎる・抜く面が
    フィレット面に接する。docs/CAD_LIBRARY_PITFALLS.md)ので、Solid がちょうど1つ・体積が減ったことまで見る。

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
    context = f"faces {indices}, thickness={value}"
    try:
        # 負の厚さ = 内側へ取る(外形寸法を保つ)。内側の角は `Intersection`(尖ったまま):
        # `Arc` は全辺を丸めた箱で BRepCheck 偽の形を返した(2026-10-13 実測)。
        maker.MakeThickSolidByJoin(
            shape,
            closing,
            -value,
            _TOLERANCE,
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
            "厚さが丸めた辺の半径以上か、壁どうしがぶつかる厚さ"
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
            "厚さが大きすぎるか、抜く面に丸めた面が接している(そのときは先にシェルを掛けてから丸める)"
        )
    _require_valid(final, "shell", context)

    return {
        "shape": final,
        "provenance": _shell_provenance(maker, shape, final, indices, value),
        "volume_before": float(volume_before),
        "volume_after": float(volume_after),
    }


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

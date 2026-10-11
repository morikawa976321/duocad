"""押し出しの幾何(OCPを触るカーネル層)。

面の押し出し(`extrude_faces`)と、スケッチの領域の押し出し(`extrude_regions`)を持つ。
面の数え方(`_face_map` の1始まりの添字)・ブーリアンの検査(`_checked_volume` / `_require_valid`)・
由来の射影(`_target_successors` / `_face_map_projection`)は `kernel` の側にあり、ここはそれを読むだけ ——
依存は kernel_extrude → kernel の一方向。

戻り値の規律は kernel と同じ(JSON化できる素の値だけ)。例外は `"shape"`(TopoDS_Shape。
document.Body.shape になる値)。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.Precision import Precision
from OCP.TopTools import TopTools_IndexedMapOfShape  # noqa: F401  型注釈だけ
from OCP.TopoDS import TopoDS, TopoDS_Shape  # noqa: F401  TopoDS_Shape は型注釈だけ
from OCP.gp import gp_Vec

from .kernel import (
    _checked_volume,
    _face_at,
    _face_map,
    _face_map_projection,
    _face_normal,
    _face_props,
    _fuse_all,
    _require_valid,
    _target_successors,
    _unify_same_domain,
    classify_face,
    volume,
)

#: 面の同一性を「重心の一致」で判定するときの相対許容差(after_face_index の探索)。
_MATCH_TOL = 1.0e-6


def extrude_regions(
    faces: Sequence[Any],
    distance: float,
    mode: str,
    normal: Sequence[float],
    target_shape: Any = None,
) -> dict:
    """領域の面群を法線方向へ押し出し、join/cut/new した形状を返す。

    同一平面の面の統合(`_unify_same_domain`)は、join / new で柱を束ねたとき(柱が複数か、相手への join)にだけ掛ける。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape", "unified": bool, "volume_before": float | None, "volume_after": float,
         "provenance": {"verb": "extrude", "mode": str, "distance": float, "unified": bool,
                        "face_map": {"<旧添字>": [新添字, ...]}, "deleted": [...], "new_faces": [...]}}
    `face_map` の旧添字は `target_shape` の面(相手が無ければ空)。
    """
    if not faces:
        raise ValueError("extrude_regions requires at least one face")
    distance = float(distance)
    if distance == 0.0:
        raise ValueError("distance must not be 0 (押し出し量0は形状を変えない)")
    if mode not in ("join", "cut", "new"):
        raise ValueError(f"unknown extrude mode: {mode!r}")
    if mode == "cut" and (target_shape is None or target_shape.IsNull()):
        raise ValueError("cut requires a target shape")
    if mode == "new" and target_shape is not None and not target_shape.IsNull():
        # 相手を黙って無視すると「足したつもりが別ボディ」が成功として通る。
        raise ValueError("new must not take a target shape (新規ボディに行き先は無い)")

    nx, ny, nz = float(normal[0]), float(normal[1]), float(normal[2])
    offset = gp_Vec(nx * distance, ny * distance, nz * distance)

    prisms: List[TopoDS_Shape] = []
    for face in faces:
        prism = BRepPrimAPI_MakePrism(TopoDS.Face_s(face), offset)
        if not prism.IsDone():
            raise ValueError("failed to build a prism from a region face")
        prism_shape = prism.Shape()
        if prism_shape is None or prism_shape.IsNull():
            raise ValueError("prism from a region face is empty")
        # 押し出し方向が面の平面内に寝ると体積0の退化ソリッドができ、`IsDone()` も
        # `IsNull()` も `BRepCheck_Analyzer` も通る。体積を見るのが唯一の関門。
        if volume(prism_shape) <= 0:
            raise ValueError(
                "prism from a region face has no volume "
                "(押し出し方向が領域の平面に寝ている)"
            )
        prisms.append(prism_shape)

    combined = _fuse_all(prisms, "region prisms")

    volume_before: Optional[float] = None
    builder = None
    if mode in ("join", "new"):
        if target_shape is None or target_shape.IsNull():
            result = combined
        else:
            volume_before = volume(target_shape)
            builder = BRepAlgoAPI_Fuse(target_shape, combined)
            builder.Build()
            if not builder.IsDone():
                raise ValueError("join (fuse) failed")
            result = builder.Shape()
    else:
        volume_before = volume(target_shape)
        builder = BRepAlgoAPI_Cut(target_shape, combined)
        builder.Build()
        if not builder.IsDone():
            raise ValueError("cut failed")
        result = builder.Shape()

    volume_after = _checked_volume(result, mode, volume_before=volume_before)
    final, history, unified = result, None, False
    # 柱1本の new は束ねていないので統合しない。統合すると輪郭の分割で割れた側面までまとまり、
    # 後の操作が記録した面・辺の番号がずれる(実測: SVG を取り込んだ輪郭の柱1本で 38面 -> 26面)。
    if mode in ("join", "new") and (len(prisms) > 1 or builder is not None):
        final, history, unified = _unify_same_domain(result, volume_after)
    _require_valid(final, mode)
    final_map = _face_map(final)
    successors = {} if builder is None else _target_successors(builder, target_shape, history, final_map)

    return {
        "shape": final,
        "unified": unified,
        "volume_before": volume_before,
        "volume_after": volume(final),
        "provenance": {
            "verb": "extrude",
            "mode": mode,
            "distance": distance,
            "unified": unified,
            **_face_map_projection(successors, final_map.Extent()),
        },
    }


def extrude_faces(
    source_shape: "TopoDS_Shape",
    face_indices: Sequence[int],
    distance: float,
    mode: str,
    target_shape: Any = None,
) -> dict:
    """`source_shape` の**同一平面・同じ向きの平面の面**を `face_indices`(1始まり・選択順)で
    まとめて法線方向へ押し出し、`target_shape` へ join / cut する(`new` は柱だけを返す)。

    `distance > 0` が面の外向き法線方向。`mode` は符号と独立なので、面から削るには**負の距離で `cut`**
    (正で cut すると外側を削るだけで何も変わらず、「変化なし」の例外になる)。

    許容値は OCCT の `Precision`(kernel に押し出し用の定数は無い。`_MATCH_TOL` は重心の突き合わせ用で
    桁が緩い): 法線のずれは `Precision.Angular_s`、平面までの距離は `Precision.Confusion_s`。

    同一平面の面の統合は `_unify_same_domain`(コピーで試して採否を決める)を join / cut と、柱が複数の `new` に掛ける。
    掛けないと押すたびに面が分裂して増える。柱1本の `new` は束ねていないので統合しない。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape", "unified": bool, "normal": [nx,ny,nz], "distance": float,
         "volume_before": float | None, "volume_after": float,
         "after_face_indices": [int | None, ...],   # 押した面ごと。統合で消えたら None
         "provenance": {"verb": "extrude", "faces": [...], "mode": str, "distance": float,
                        "unified": bool, "face_map": {"<旧添字>": [新添字, ...]}, "deleted": [...],
                        "new_faces": [...], "after_face_indices": [...]}}
    `face_map` の旧添字は `target_shape` の面(`new` では空)。`faces` は `source_shape` の添字。
    """
    if source_shape is None or source_shape.IsNull():
        raise ValueError("cannot extrude faces of an empty shape")
    indices = [int(index) for index in face_indices]
    if not indices:
        raise ValueError("extrude_faces requires at least one face")
    if len(set(indices)) != len(indices):
        raise ValueError(f"face indices must be distinct, got {indices}")
    distance = float(distance)
    if distance == 0.0:
        raise ValueError("distance must not be 0 (押し出し量0は形状を変えない)")
    if mode not in ("join", "cut", "new"):
        raise ValueError(f"unknown extrude mode: {mode!r}")
    has_target = target_shape is not None and not target_shape.IsNull()
    if mode == "new" and has_target:
        raise ValueError("new must not take a target shape (新規ボディに行き先は無い)")
    if mode != "new" and not has_target:
        raise ValueError(f"{mode} requires a target shape")

    source_map = _face_map(source_shape)
    faces = [_face_at(source_map, index) for index in indices]
    normal, centroids = _check_coplanar_planes(faces, indices)

    prisms: List[TopoDS_Shape] = []
    for index, face in zip(indices, faces):
        prism = BRepPrimAPI_MakePrism(
            face, gp_Vec(normal[0] * distance, normal[1] * distance, normal[2] * distance)
        )
        if not prism.IsDone():
            raise ValueError(f"failed to build the prism for face {index} (distance={distance})")
        prism_shape = prism.Shape()
        if prism_shape is None or prism_shape.IsNull():
            raise ValueError(f"prism for face {index} is empty (distance={distance})")
        prisms.append(prism_shape)
    combined = _fuse_all(prisms, "face prisms")

    context = f"faces {indices}, distance={distance}"
    volume_before: Optional[float] = None
    builder = None
    if mode == "new":
        result = combined
    else:
        volume_before = volume(target_shape)
        builder = (
            BRepAlgoAPI_Fuse(target_shape, combined)
            if mode == "join"
            else BRepAlgoAPI_Cut(target_shape, combined)
        )
        builder.Build()
        if not builder.IsDone():
            raise ValueError(f"{mode} failed for faces {indices} (distance={distance})")
        result = builder.Shape()

    try:
        volume_result = _checked_volume(result, mode, volume_before=volume_before, context=context)
    except ValueError as error:
        if mode == "cut" and distance > 0 and "did not change" in str(error):
            raise ValueError(
                f"{error}: 正の距離は面の外向き(ボディの外側)なので cut では何も削れない。"
                "面から削るには負の距離を使う"
            ) from None
        raise

    if mode == "new" and len(prisms) == 1:
        final, history, unified = result, None, False
    else:
        # 順序は 無変化 → 統合 → BRepCheck(統合は形状を壊しうるので、検査は統合後に行う)。
        final, history, unified = _unify_same_domain(result, volume_result)
    _require_valid(final, mode, context)
    final_map = _face_map(final)

    successors = {} if builder is None else _target_successors(builder, target_shape, history, final_map)

    after_face_indices = [
        _find_translated_face(
            final_map,
            target=(
                centroid[0] + normal[0] * distance,
                centroid[1] + normal[1] * distance,
                centroid[2] + normal[2] * distance,
            ),
            normal=normal,
            scale=abs(distance),
        )
        for centroid in centroids
    ]
    provenance = {
        "verb": "extrude",
        "faces": indices,
        "mode": mode,
        "distance": distance,
        "unified": unified,
        **_face_map_projection(successors, final_map.Extent()),
        "after_face_indices": after_face_indices,
    }
    return {
        "shape": final,
        "unified": unified,
        "normal": list(normal),
        "distance": distance,
        "volume_before": volume_before,
        "volume_after": volume(final),
        "after_face_indices": after_face_indices,
        "provenance": provenance,
    }


def _check_coplanar_planes(faces: Sequence[Any], indices: Sequence[int]) -> tuple:
    """全部が平面・同じ外向き法線・同一平面上であることを確かめ、`(法線, 各面の重心)` を返す(基準は先頭の面)。違反は ValueError。"""
    normals: List[tuple] = []
    centroids: List[tuple] = []
    for position, (index, face) in enumerate(zip(indices, faces), start=1):
        kind = classify_face(face)
        if kind != "plane":
            raise ValueError(
                f"selected face #{position} (index {index}) is {kind}: 押し出せるのは平面の面だけ"
                "(曲面は掃引すると壊れた形状になることを実測済み。docs/CAD_LIBRARY_PITFALLS.md)"
            )
        normals.append(_face_normal(face))
        centroids.append(_face_props(face)[1])

    first_normal, first_centroid = normals[0], centroids[0]
    for position in range(2, len(faces) + 1):
        index, normal, centroid = indices[position - 1], normals[position - 1], centroids[position - 1]
        cross = math.dist(
            (
                first_normal[1] * normal[2] - first_normal[2] * normal[1],
                first_normal[2] * normal[0] - first_normal[0] * normal[2],
                first_normal[0] * normal[1] - first_normal[1] * normal[0],
            ),
            (0.0, 0.0, 0.0),
        )
        dot = sum(first_normal[axis] * normal[axis] for axis in range(3))
        where = f"selected face #{position} (index {index}) vs #1 (index {indices[0]})"
        if cross > Precision.Angular_s():
            raise ValueError(f"{where}: 平行でない(別の平面)。同一平面の面だけ押し出せる")
        if dot < 0:
            raise ValueError(f"{where}: 外向き法線が逆向き。同じ向きの面だけ押し出せる")
        offset = abs(sum((centroid[axis] - first_centroid[axis]) * first_normal[axis] for axis in range(3)))
        if offset > Precision.Confusion_s():
            raise ValueError(
                f"{where}: 同一平面上にない(平面までの距離 {offset:.6g} mm)"
            )
    return first_normal, centroids


def _find_translated_face(
    shape_map: "TopTools_IndexedMapOfShape",
    target: tuple,
    normal: tuple,
    scale: float,
) -> Optional[int]:
    """重心が `target`・法線が `normal` と同じ向きの面の添字(無ければNone)。

    押し出した面が新形状のどれになったかを幾何で突き合わせる。由来マップ(`Modified`)はブーリアンでは
    空を返すことが多い(実測: Fuseは押し出した面を `IsDeleted` にするだけ)ため。
    """
    tolerance = _MATCH_TOL * max(1.0, abs(scale), *(abs(value) for value in target))
    best: Optional[tuple] = None
    for index in range(1, shape_map.Extent() + 1):
        face = TopoDS.Face_s(shape_map.FindKey(index))
        candidate_normal = _face_normal(face)
        alignment = sum(candidate_normal[axis] * normal[axis] for axis in range(3))
        if alignment < 1.0 - _MATCH_TOL:
            continue
        _, centroid = _face_props(face)
        distance = math.dist(centroid, target)
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, index)
    return None if best is None else best[1]

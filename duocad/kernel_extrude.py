"""押し出しの幾何(OCPを触るカーネル層)。

面の押し出し(`extrude_face_on_shape`)と、スケッチの領域の押し出し(`extrude_regions`)を持つ。
面の数え方(`_face_map` の1始まりの添字)・ブーリアンの検査(`_checked_volume` / `_require_valid`)・
由来の射影(`_positions_in` / `_face_map_projection`)は `kernel` の側にあり、ここはそれを読むだけ ——
依存は kernel_extrude → kernel の一方向。

戻り値の規律は kernel と同じ(JSON化できる素の値だけ)。例外は `"shape"`(TopoDS_Shape。
document.Body.shape になる値)。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
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
    _is_removed,
    _positions_in,
    _require_valid,
    _shape_list,
    _unify_same_domain,
    classify_face,
    volume,
)

#: 面の同一性を「重心の一致」で判定するときの相対許容差(after_face_index の探索)。
_MATCH_TOL = 1.0e-6


# --------------------------------------------------------------------------------------
# extrude_face の幾何(実装計画書§1.1)
# --------------------------------------------------------------------------------------
def extrude_face_on_shape(
    shape: "TopoDS_Shape",
    face_index: int,
    distance: float,
) -> dict:
    """面 `face_index` を `distance` だけ押し出した**新しい形状**と、由来の射影を返す。

    符号の意味: `distance > 0` は面の**外向き法線方向へ肉を足す**(Fuse)、`distance < 0` は
    同じ向きへ**削る**(Cut)。どちらも面から直接プリズムを作ってブーリアンする。入力の
    `shape` は変更しない。

    **平面の面だけ押し出せる**(それ以外は ValueError)。曲面は「面の中央の法線」方向へ
    掃引しても意味のある操作にならず、**成功したまま壊れた形状を返す**
    (穴の円筒面を+1mm押し出すと `IsDone()` は真・体積も変わるが
    `BRepCheck_Analyzer.IsValid()` が偽になる。-1mmでは体積が 91018→38597 と激減する
    「有効だが無意味な」形状になる)。曲面の押し出し(オフセット)には対応しない。

    **黙って失敗させない**(OCCTのブーリアンは例外を投げずに失敗する):
    プリズム生成とブーリアンの `IsDone()`、結果が空でないこと、**体積が実際に変わったこと**、
    **最後に `BRepCheck_Analyzer` で形状が妥当であること**を毎回検査し、
    どれか1つでも欠ければ例外にして操作ごと捨てる。

    同一平面の面は `ShapeUpgrade_UnifySameDomain` で統合する。統合しないと押し出すたびに
    面が分裂して増え(実測: 20mm立方体の上面を+10 → 統合なし10面 / 統合後6面)、
    **同じ面を続けて押し出す**という計測の手続き自体が成立しなくなる。ただし統合は
    **入力の形状を壊す**うえ利得が無い場合があるので、コピーで試してから採否を決める
    (機序と実測値は `_unify_same_domain` のdocstring)。

    戻り値(`"shape"` 以外はすべてJSON化できる素の値):
        {
          "shape": <TopoDS_Shape>, "unified": bool,
          "face_index": int, "distance": float, "normal": [nx,ny,nz],
          "after_face_index": int | None,   # 押し出した面が新形状のどれになったか
          "volume_before": float, "volume_after": float,
          "provenance": {...}               # 下記
        }

    `provenance` は `Operation.provenance` にそのまま入れられる**JSON化可能な射影**
    (`normalize_provenance` の戻りは TopoDS_Shape を含むメモリ上の形なので `to_json()` で
    落ちる):

        {"verb": "extrude_face", "face_index": int, "distance": float, "unified": bool,
         "face_map": {"<旧添字>": [新添字, ...]}, "deleted": [旧添字, ...],
         "new_faces": [新添字, ...], "after_face_index": int | None}
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot extrude a face of an empty shape")
    distance = float(distance)
    if distance == 0.0:
        raise ValueError("distance must not be 0 (押し出し量0は形状を変えない)")

    shape_map = _face_map(shape)
    face = _face_at(shape_map, face_index)
    face_index = int(face_index)

    kind = classify_face(face)
    if kind != "plane":
        raise ValueError(
            f"face {face_index} is {kind}: M1で押し出せるのは平面の面だけ"
            "(曲面は掃引しても壊れた形状になることを実測済み)"
        )

    normal = _face_normal(face)
    _, centroid_before = _face_props(face)
    volume_before = volume(shape)

    prism = BRepPrimAPI_MakePrism(
        face, gp_Vec(normal[0] * distance, normal[1] * distance, normal[2] * distance)
    )
    if not prism.IsDone():
        raise ValueError(
            f"failed to build the prism for face {face_index} (distance={distance})"
        )
    prism_shape = prism.Shape()
    if prism_shape is None or prism_shape.IsNull():
        raise ValueError(f"prism for face {face_index} is empty (distance={distance})")

    # 正なら足す / 負なら削る。負のときプリズムは内側を向いているので、そのまま引けばよい。
    builder = (
        BRepAlgoAPI_Fuse(shape, prism_shape)
        if distance > 0
        else BRepAlgoAPI_Cut(shape, prism_shape)
    )
    builder.Build()
    operation = "fuse" if distance > 0 else "cut"
    context = f"face {face_index}, distance={distance}"
    if not builder.IsDone():
        raise ValueError(f"{operation} failed for face {face_index} (distance={distance})")
    result = builder.Shape()
    volume_result = _checked_volume(result, operation, volume_before=volume_before, context=context)

    # 順序は 無変化 → 統合 → BRepCheck。統合は入力の形状を壊しうるので、検査は統合後の `final` に対して行う。
    final, history, unified = _unify_same_domain(result, volume_result)
    _require_valid(final, operation, context)
    final_map = _face_map(final)

    successors: dict = {}
    for index in range(1, shape_map.Extent() + 1):
        old_face = shape_map.FindKey(index)
        candidates = _shape_list(builder, "Modified", old_face)
        if not candidates and not _is_removed(builder, old_face):
            candidates = [old_face]  # ブーリアンが触らなかった面は同じ形状として残る
        successors[str(index)] = _positions_in(
            final_map,
            (survivor for candidate in candidates for survivor in _through_history(history, candidate)),
        )

    # キーの順番は操作ログに保存される(`Operation.provenance`)ので変えない。
    provenance = {
        "verb": "extrude_face",
        "face_index": face_index,
        "distance": distance,
        "unified": unified,
        **_face_map_projection(successors, final_map.Extent()),
        "after_face_index": None,
    }

    after_face_index = _find_translated_face(
        final_map,
        target=(
            centroid_before[0] + normal[0] * distance,
            centroid_before[1] + normal[1] * distance,
            centroid_before[2] + normal[2] * distance,
        ),
        normal=normal,
        scale=abs(distance),
    )
    provenance["after_face_index"] = after_face_index

    return {
        "shape": final,
        "unified": unified,
        "face_index": face_index,
        "distance": distance,
        "normal": list(normal),
        "after_face_index": after_face_index,
        "volume_before": volume_before,
        "volume_after": volume(final),
        "provenance": provenance,
    }


def extrude_regions(
    faces: Sequence[Any],
    distance: float,
    mode: str,
    normal: Sequence[float],
    target_shape: Any = None,
) -> dict:
    """領域の面群を法線方向へ押し出し、join/cut/new した形状を返す。

    戻り値: {"shape": TopoDS_Shape, "volume_before": float | None, "volume_after": float}
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
        # `new` は**必ず独立したソリッドになる**。相手を渡されたのに黙って無視すると
        # 「足したつもりが別ボディになっていた」が成功として通る。
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
        # 押し出し方向が面の平面内に寝ていると**体積0の退化ソリッド**ができる。
        # `IsDone()` も `IsNull()` も `BRepCheck_Analyzer` も全部通ってしまうので、
        # 体積を直接見るのがここでの唯一の関門。
        if volume(prism_shape) <= 0:
            raise ValueError(
                "prism from a region face has no volume "
                "(押し出し方向が領域の平面に寝ている)"
            )
        prisms.append(prism_shape)

    combined = _fuse_all(prisms, "region prisms")

    volume_before: Optional[float] = None
    if mode in ("join", "new"):
        # `new` はここへ来る時点で `target_shape is None` が保証されている(上の検査)。
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

    # 無変化の検査は相手があるときだけ(`volume_before` が None なら飛ぶ)。
    volume_after = _checked_volume(result, mode, volume_before=volume_before)
    _require_valid(result, mode)

    return {
        "shape": result,
        "volume_before": volume_before,
        "volume_after": volume_after,
    }


def _through_history(history: Any, sub: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """`BRepTools_History` を1段たどる(消えたら空、触られていなければ自分自身)。"""
    if history is None:
        return [sub]
    modified = _shape_list(history, "Modified", sub)
    if modified:
        return modified
    if _is_removed(history, sub):
        return []
    return [sub]



def _find_translated_face(
    shape_map: "TopTools_IndexedMapOfShape",
    target: tuple,
    normal: tuple,
    scale: float,
) -> Optional[int]:
    """重心が `target`・法線が `normal` と同じ向きの面の添字(無ければNone)。

    押し出した面が新形状のどれになったかを幾何で突き合わせる。由来マップ(`Modified`)は
    ブーリアンでは空を返すことが多く(実測: Fuseは押し出した面を `IsDeleted` にするだけ)、
    「押し出した面を選択したまま続けて押す」という計測の手続きには足りないため。
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

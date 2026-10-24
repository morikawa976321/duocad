"""OCCT の履歴と面の同一性を、添字の由来へ対応させる。"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape

from . import (
    brep_topology as _brep_topology,
)


def target_successors(
    builder: Any, target_shape: "TopoDS_Shape", history: Any, final_map: "TopTools_IndexedMapOfShape"
) -> dict:
    """`target_shape` の各面が、ブーリアン(`builder`)と統合(`history`。統合しなければ None)を経て
    `final_map` の何番になったか(旧添字の文字列 → 新添字の列)。ツール側の面は鍵に入らない。"""
    target_map = _brep_topology._face_map(target_shape)
    successors: dict = {}
    for index in range(1, target_map.Extent() + 1):
        old_face = target_map.FindKey(index)
        candidates = shape_list(builder, "Modified", old_face)
        if not candidates and not is_removed(builder, old_face):
            candidates = [old_face]
        successors[str(index)] = positions_in(
            final_map,
            (survivor for candidate in candidates for survivor in through_history(history, candidate)),
        )
    return successors


def through_history(history: Any, sub: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """`BRepTools_History` を1段たどる(消えたら空、触られていなければ自分自身)。"""
    if history is None:
        return [sub]
    modified = shape_list(history, "Modified", sub)
    if modified:
        return modified
    if is_removed(history, sub):
        return []
    return [sub]


def positions_in(final_map: "TopTools_IndexedMapOfShape", candidates: Iterable[Any]) -> List[int]:
    """`candidates` が `final_map` の何番目かを重複なしで(最初に現れた順に)集める。"""
    resolved: List[int] = []
    for candidate in candidates:
        position = final_map.FindIndex(candidate)
        if position > 0 and position not in resolved:
            resolved.append(position)
    return resolved


def normalize_provenance(
    builder: Any,
    inputs: Iterable["TopoDS_Shape"] = (),
) -> Optional[dict]:
    """OCCTの由来マップを1つの形へ正規化して返す(`Operation.provenance` に入れる値)。

    **取り出し方が操作ごとに違う**のを吸収する —— ブーリアン(`BRepAlgoAPI_*`)は `History()`
    -> `BRepTools_History` を持ち、フィレット等の `BRepBuilderAPI_MakeShape` 系は自分自身が
    `Modified()` / `Generated()` / `IsDeleted()` を持つ。

    戻り値の形(由来が1件も取れなければ None):

        {
            "modified":  [{"input": <TopoDS_Shape>, "results": [<TopoDS_Shape>, ...]}, ...],
            "generated": [{"input": <TopoDS_Shape>, "results": [<TopoDS_Shape>, ...]}, ...],
            "deleted":   [<TopoDS_Shape>, ...],
        }

    戻り値は `TopoDS_Shape` を含む**メモリ上の形**で、JSON化できない(永続化は `_blend_provenance`)。
    """
    source = builder.History() if hasattr(builder, "History") else builder

    modified: List[dict] = []
    generated: List[dict] = []
    deleted: List[Any] = []

    for input_shape in inputs:
        if input_shape is None or _brep_topology.is_null(input_shape):
            continue
        for sub in iter_faces_and_edges(input_shape):
            for bucket, getter in (("modified", "Modified"), ("generated", "Generated")):
                results = shape_list(source, getter, sub)
                if results:
                    (modified if bucket == "modified" else generated).append(
                        {"input": sub, "results": results}
                    )
            if is_removed(source, sub):
                deleted.append(sub)

    if not (modified or generated or deleted):
        return None
    return {"modified": modified, "generated": generated, "deleted": deleted}


def iter_faces_and_edges(shape: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """由来を問い合わせる部分形状(面・辺)を重複なしで列挙する。"""
    out: List[TopoDS_Shape] = []
    for enum in (TopAbs_ShapeEnum.TopAbs_FACE, TopAbs_ShapeEnum.TopAbs_EDGE):
        shape_map = _brep_topology.indexed_map(shape, enum)
        out.extend(shape_map.FindKey(i) for i in range(1, shape_map.Extent() + 1))
    return out


def shape_list(source: Any, getter: str, sub: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """`Modified(sub)` / `Generated(sub)` の戻り(TopTools_ListOfShape)をPythonのlistへ。"""
    method = getattr(source, getter, None)
    if method is None:
        return []
    try:
        result = method(sub)
    except Exception:  # OCCTは対象外の部分形状で例外を投げることがある
        return []
    return list(result)


def is_removed(source: Any, sub: "TopoDS_Shape") -> bool:
    """削除されたか。`BRepTools_History` は IsRemoved、MakeShape系は IsDeleted を持つ。"""
    for name in ("IsRemoved", "IsDeleted"):
        method = getattr(source, name, None)
        if method is not None:
            try:
                return bool(method(sub))
            except Exception:
                return False
    return False


def lookup_by_identity(mapping: dict, key: Any) -> Optional[List[Any]]:
    """`TopoDS_Shape` は辞書の鍵として同一性で引けないので `IsSame` で探す。"""
    for candidate, results in mapping.items():
        if candidate.IsSame(key):
            return results
    return None


def contains_identity(shapes: Sequence[Any], key: Any) -> bool:
    return any(candidate.IsSame(key) for candidate in shapes)

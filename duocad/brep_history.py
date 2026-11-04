"""OCCT の履歴と面の同一性を、添字の由来へ対応させる。"""
from __future__ import annotations

from typing import Any, Iterable, List

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


def maker_successors(maker: Any, shape: "TopoDS_Shape", final_map: "TopTools_IndexedMapOfShape") -> dict:
    """`target_successors` の、フィレット・面取り・シェルのビルダ版。由来の取り出し方は操作ごとに違い、ブーリアンは `History()`
    (`BRepTools_History` の `IsRemoved`)、`BRepBuilderAPI_MakeShape` 系は自分の `Modified()` / `IsDeleted()` が持つ。"""
    source = maker.History() if hasattr(maker, "History") else maker
    return target_successors(source, shape, None, final_map)


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


def shape_list(source: Any, getter: str, sub: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """`Modified(sub)` / `Generated(sub)` の戻り(TopTools_ListOfShape)をPythonのlistへ。"""
    method = getattr(source, getter, None)
    if method is None:
        return []
    try:
        result = method(sub)
    except Exception:  # OCCTは対象外の部分形状で例外を投げることがある
        return []
    if isinstance(result, (list, tuple)):  # Python 側の履歴(`kernel_tight` の縫い合わせ)は list で返す
        return list(result)
    return _brep_topology.listed(result)


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

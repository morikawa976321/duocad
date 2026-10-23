"""位相の列挙・添字の引き当てと、整数だけの由来マップ射影。"""
from __future__ import annotations

from typing import Any, List

from OCP.BRep import BRep_Builder
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.TopAbs import TopAbs_ShapeEnum, TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape
from OCP.TopoDS import TopoDS, TopoDS_Shape, TopoDS_Compound


#: 部分形状の種別名 → (OCCT の列挙, エラー文で使う複数形)。`count_subshapes` と `_subshape_map` の唯一の表。
_SUBSHAPE_KINDS = {
    "face": (TopAbs_ShapeEnum.TopAbs_FACE, "faces"),
    "edge": (TopAbs_ShapeEnum.TopAbs_EDGE, "edges"),
    "vertex": (TopAbs_ShapeEnum.TopAbs_VERTEX, "vertices"),
}


def count_subshapes(shape: "TopoDS_Shape", kind: str) -> int:
    """重複を除いた部分形状の数(`TopExp.MapShapes_s`)。kind は "face" | "edge" | "vertex"。

    `TopExp_Explorer` で素朴に列挙すると隣接2面から同じ辺を2回拾う
    (shelf_attachment_GD.step 実測: 重複除去66 / 素朴な列挙132)。NULL 形状は 0(ValueError にしない)。
    """
    if kind not in _SUBSHAPE_KINDS:
        raise ValueError(f"unknown subshape kind: {kind!r}")
    return indexed_map(shape, _SUBSHAPE_KINDS[kind][0]).Extent()


def _face_map_projection(successors: dict, final_count: int) -> dict:
    """後継の表(旧添字 → 新添字の列)を `face_map` / `deleted` / `new_faces` の射影にする
    (押し出し・`boolean_bodies`・`_blend_provenance` が同じ形で保存する)。"""
    mapped = {position for positions in successors.values() for position in positions}
    return {
        "face_map": successors,
        "deleted": [int(key) for key, value in successors.items() if not value],
        "new_faces": [i for i in range(1, final_count + 1) if i not in mapped],
    }


def face_solid_indices(shape: "TopoDS_Shape") -> List[int]:
    """面(`face_count` の1始まりの並び)ごとに、それを含む SOLID の0始まりの番号。どの SOLID にも無い面は -1。

    まとめた形(`make_compound`)の三角形群を、ソリッドごとに閉じているかで検査するための対応表
    (`topopt.voxelize.inspect_soup`)。"""
    faces = _face_map(shape)
    solids = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    out = [-1] * faces.Extent()
    for solid in range(1, solids.Extent() + 1):
        own = indexed_map(solids.FindKey(solid), TopAbs_ShapeEnum.TopAbs_FACE)
        for face in range(1, own.Extent() + 1):
            index = faces.FindIndex(own.FindKey(face))
            if index > 0 and out[index - 1] == -1:
                out[index - 1] = solid - 1
    return out


def face_count(shape: "TopoDS_Shape") -> int:
    """重複を除いた面の数。**この数え方の1始まりの添字が、選択の識別子そのもの**。"""
    return _face_map(shape).Extent()


def face_of(shape: "TopoDS_Shape", face_index: int) -> Any:
    """形状の1始まり添字の面を返す。範囲外は ValueError。"""
    return _face_at(_face_map(shape), face_index)


def _face_at(shape_map: "TopTools_IndexedMapOfShape", face_index: int) -> Any:
    """既に作った面マップから引く。"""
    if not 1 <= int(face_index) <= shape_map.Extent():
        raise ValueError(
            f"face index out of range: {face_index} (1..{shape_map.Extent()})"
        )
    return TopoDS.Face_s(shape_map.FindKey(int(face_index)))


def edge_count(shape: "TopoDS_Shape") -> int:
    """重複を除いた辺の数。**この数え方の1始まりの添字が、辺の選択の識別子そのもの**(`face_count` と同じ)。"""
    return _edge_map(shape).Extent()


def edge_of(shape: "TopoDS_Shape", edge_index: int) -> Any:
    """形状の1始まり添字の辺を返す。範囲外は ValueError。"""
    return _edge_at(_edge_map(shape), edge_index)


def _edge_at(shape_map: "TopTools_IndexedMapOfShape", edge_index: int) -> Any:
    """既に作った辺マップから引く。"""
    if not 1 <= int(edge_index) <= shape_map.Extent():
        raise ValueError(
            f"edge index out of range: {edge_index} (1..{shape_map.Extent()})"
        )
    return TopoDS.Edge_s(shape_map.FindKey(int(edge_index)))


def _vertex_map(shape: "TopoDS_Shape") -> "TopTools_IndexedMapOfShape":
    """頂点の重複除去済みマップ(1始まり)。添字が選択参照の `index` そのもの。"""
    return _subshape_map(shape, "vertex")


def _edge_map(shape: "TopoDS_Shape") -> "TopTools_IndexedMapOfShape":
    """辺の重複除去済みマップ(1始まり)。添字が選択の識別子になるので数え方はここに固定する。"""
    return _subshape_map(shape, "edge")


def split_solids(shape: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """形状をSOLIDごとに分解する。

    `TopExp.MapShapes_s(shape, TopAbs_SOLID)` で重複除去済みのSOLID群を取り出し、
    **SOLIDが1つも無い形状(裸の面・シェルのみ)は形状全体を1要素のリストにして返す**
    (0個のBodyを黙って作らない。io_step.pyの契約)。列挙順は `TopExp.MapShapes_s` の走査順(決定的)。
    """
    if shape is None or is_null(shape):
        raise ValueError("cannot split an empty shape")
    shape_map = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    if shape_map.Extent() == 0:
        return [shape]
    return [
        TopoDS.Solid_s(shape_map.FindKey(index))
        for index in range(1, shape_map.Extent() + 1)
    ]


def _subshape_map(shape: "TopoDS_Shape", kind: str) -> "TopTools_IndexedMapOfShape":
    """`kind`("face" | "edge" | "vertex")の重複除去済みマップ(1始まり)。添字が選択の識別子になる。
    null は ValueError(`count_subshapes` と違って 0 を返さない)。"""
    enum, plural = _SUBSHAPE_KINDS[kind]
    if shape is None or is_null(shape):
        raise ValueError(f"cannot map the {plural} of an empty shape")
    return indexed_map(shape, enum)


def _face_map(shape: "TopoDS_Shape") -> "TopTools_IndexedMapOfShape":
    """面の重複除去済みマップ(1始まり)。"""
    return _subshape_map(shape, "face")


def _extrude_count(shape: Any, kind: Any) -> int:
    explorer = TopExp_Explorer(shape, kind)
    count = 0
    while explorer.More():
        count += 1
        explorer.Next()
    return count


def _extrude_edges(wire: Any) -> List[Any]:
    explorer = BRepTools_WireExplorer(TopoDS.Wire_s(wire))
    out = []
    while explorer.More():
        out.append(TopoDS.Edge_s(explorer.Current()))
        explorer.Next()
    return out


_face_edit_KINDS = {"face": TopAbs_ShapeEnum.TopAbs_FACE, "edge": TopAbs_ShapeEnum.TopAbs_EDGE}


def _face_edit_faces(shape: Any) -> List[Any]:
    found = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    return [TopoDS.Face_s(found.FindKey(i)) for i in range(1, found.Extent() + 1)]


def _face_edit_edges(shape: Any) -> List[Any]:
    found = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    return [TopoDS.Edge_s(found.FindKey(i)) for i in range(1, found.Extent() + 1)]


def _face_edit_solids(shape: Any) -> List[Any]:
    found = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]


def _face_edit_count(shape: Any, kind: str) -> int:
    return indexed_map(shape, _face_edit_KINDS[kind]).Extent()


def _face_edit_edge_faces(shape: Any) -> Any:
    found = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, found)
    return found


def _tight_faces(shape: TopoDS_Shape) -> list:
    faces = indexed_map(shape, TopAbs_FACE)
    return [faces.FindKey(i) for i in range(1, faces.Extent() + 1)]


def _tight_edges(shape: TopoDS_Shape) -> list:
    found = indexed_map(shape, TopAbs_EDGE)
    return [TopoDS.Edge_s(found.FindKey(i)) for i in range(1, found.Extent() + 1)]


def subshapes(shape, kind):
    """`kind`(`TopAbs_ShapeEnum`)の部分形状を `TopExp_Explorer` の走査順で列挙する。**重複を除かない**
    (隣接2面から同じ辺を2回拾う。重複なしは `indexed_map`)。"""
    result = []
    explorer = TopExp_Explorer(shape, kind)
    while explorer.More():
        result.append(explorer.Current())
        explorer.Next()
    return result


def indexed_map(shape, kind):
    """`kind`(`TopAbs_ShapeEnum`)の部分形状を**重複なし**で1始まりに並べた `TopTools_IndexedMapOfShape`
    (`TopExp.MapShapes_s`)。null の検査はしない(断る文面は呼び出し側が持つ)。"""
    result = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, result)
    return result


def compound(shapes):
    """`shapes` を1つの `TopoDS_Compound` にまとめる(空の列も許す。各形はそのまま `Add` する)。"""
    builder, result = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(result)
    for shape in shapes:
        builder.Add(result, shape)
    return result


def is_null(shape) -> bool:
    """OCCT の null 判定。None の扱いと失敗時の文面は呼び出し側が決める。"""
    return shape.IsNull()


def _overflow_solids(shape: TopoDS_Shape) -> list:
    found = indexed_map(shape, TopAbs_SOLID)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]

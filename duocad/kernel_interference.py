"""干渉チェックの幾何(OCPを触るカーネル層。Issue #186。方式と定義は docs/INTERFERENCE.md)。

2つの形から、組の関係(`pair_relation`)と最短距離(`min_distance`)を**素の値**で返す。分類(overlap / tiny_overlap /
contact / separated / unknown)としきい値は `duocad/interference.py` が持ち、ここは測った量だけを返す。

- 関係は2体の General Fuse(GF)の**像の共有**から読む。GF は一致する部分(重なりの立体・接する面・辺・点)を両方の像で
  同じ形状として共有する。
- GF は `SetNonDestructive(True)`。既定の破壊モードは入力の形状許容差を書き換える(PC ケースで 27 体中 11 体)。
- `BOPAlgo_BOP.PDS()` / `PPaveFiller().DS()` には触らない(Python から触るとプロセスが落ちる)。
- OCP からは OCCT の計算を途中で止められない。時間の上限は呼び出し側の子プロセスが持つ(`interference_worker.py`)。
"""
from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Sequence, Tuple

from OCP.BOPAlgo import BOPAlgo_Builder
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy, BRepBuilderAPI_MakeVertex
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.Message import Message_Gravity
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_Orientation, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_ListOfShape
from OCP.TopoDS import TopoDS, TopoDS_Shape

from . import (
    brep_geometry as _brep_geometry,
    brep_topology as _brep_topology,
)


#: 面の対ごとの距離が測れないとき、片方の面の縁の辺 1 本を何等分した点から測るか(`kernel_shell._EDGE_SAMPLES` と同じ考え)。
_EDGE_SAMPLES = 16

_KINDS = (TopAbs_SOLID, TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX)

Vec = Tuple[float, float, float]


def decode_brep_unchecked(text: str) -> TopoDS_Shape:
    """`kernel_brep.decode_brep` から `BRepCheck_Analyzer` を省いたもの。親が検査済みの形を子へ渡すときだけ使う(検査が支配的になる)。"""
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, io.BytesIO(text.encode("ascii")), BRep_Builder())
    if _brep_topology.is_null(shape):
        raise ValueError("BRep を読めない")
    return shape


# ------------------------------------------------------------------------------------------------
# 組の関係(2体 GF)
# ------------------------------------------------------------------------------------------------
def pair_relation(first: TopoDS_Shape, second: TopoDS_Shape, deviation: Optional[float] = None) -> Dict[str, Any]:
    """2体の General Fuse の像の共有から、組の幾何の関係を読む。入力の形は変えない。

    戻り値(`kind` で分かれる。素の値だけ):
        {"kind": "overlap", "volume", "thickness", "obb_min", "regions", "pieces", "center", "bbox", "warnings"}
        {"kind": "face", "area", "faces", "warnings"} / {"kind": "edge", "edges", ...} / {"kind": "vertex", "vertices", ...}
        {"kind": "none", "warnings"}                 … 許容差の範囲で何も共有しない
        {"kind": "unknown", "reason", "warnings"}    … GF がエラー、または面積・体積が不正
    `warnings` は BOP の警告の名前(警告は失敗にしない。PC ケースの実物で GF が警告を出しても体積は一致した)。
    `deviation` を渡すと `render` に画面へ描く形(重なりの立体・共有面の三角形、共有辺の折れ線、共有点)を足す。
    """
    builder = BOPAlgo_Builder()
    arguments = TopTools_ListOfShape()
    arguments.Append(first)
    arguments.Append(second)
    builder.SetArguments(arguments)
    builder.SetRunParallel(True)
    builder.SetNonDestructive(True)
    builder.Perform()
    warnings = _warning_names(builder)
    if builder.HasErrors():
        return {"kind": "unknown", "reason": "General Fuse がエラーを返した", "warnings": warnings}
    images = [_image(builder, shape) for shape in (first, second)]
    shared = {kind: _shared(images[0][kind], images[1][kind]) for kind in _KINDS}
    if shared[TopAbs_SOLID]:
        try:
            result = {"kind": "overlap", **overlap_measures(shared[TopAbs_SOLID])}
        except ValueError as error:
            return {"kind": "unknown", "reason": str(error), "warnings": warnings}
        render = {"solid": triangles(_brep_topology.compound(shared[TopAbs_SOLID]), deviation)} if deviation else None
    elif shared[TopAbs_FACE]:
        result = {"kind": "face", "area": sum(_brep_geometry._interference_area(face) for face in shared[TopAbs_FACE]), "faces": len(shared[TopAbs_FACE])}
        render = {"faces": triangles(_brep_topology.compound(shared[TopAbs_FACE]), deviation)} if deviation else None
    elif shared[TopAbs_EDGE]:
        result = {"kind": "edge", "edges": len(shared[TopAbs_EDGE])}
        render = {"edges": [_polyline(edge, deviation) for edge in shared[TopAbs_EDGE]]} if deviation else None
    elif shared[TopAbs_VERTEX]:
        result = {"kind": "vertex", "vertices": len(shared[TopAbs_VERTEX])}
        render = {"points": [_point(vertex) for vertex in shared[TopAbs_VERTEX]]} if deviation else None
    else:
        result, render = {"kind": "none"}, None
    result["warnings"] = warnings
    if render is not None:
        result["render"] = render
    return result


def _warning_names(builder: Any) -> List[str]:
    return sorted({str(alert.GetMessageKey()) for alert in builder.GetReport().GetAlerts(Message_Gravity.Message_Warning)})


def _image(builder: Any, shape: TopoDS_Shape) -> Dict[Any, Dict[int, List[TopoDS_Shape]]]:
    """入力の形の像(GF の後の立体片)を、種別ごとに hash で束ねて持つ。"""
    pieces: List[TopoDS_Shape] = []
    for solid in _brep_topology.subshapes(shape, TopAbs_SOLID):
        modified = list(builder.Modified(solid))
        if modified:
            pieces.extend(modified)
        elif not builder.IsDeleted(solid):
            pieces.append(solid)
    whole = _brep_topology.compound([piece for piece in pieces if piece.ShapeType() == TopAbs_SOLID])
    return {kind: _buckets(whole, kind) for kind in _KINDS}


def _buckets(shape: TopoDS_Shape, kind: Any) -> Dict[int, List[TopoDS_Shape]]:
    """hash ごとの形状の列。`TopoDS_Shape` の `hash` は同一性を返すが `__eq__` は Python の同一性なので、形状を辞書のキーにせず
    hash で束ねて `IsSame` で確かめる(キーにすると共有された破片が別物になり GF の体積が全件 0 になった)。同じ hash の別の形状も落とさない。"""
    out: Dict[int, List[TopoDS_Shape]] = {}
    explorer = TopExp_Explorer(shape, kind)
    while explorer.More():
        current = explorer.Current()
        bucket = out.setdefault(hash(current), [])
        if not any(current.IsSame(other) for other in bucket):
            bucket.append(current)
        explorer.Next()
    return out


def _shared(left: Dict[int, List[TopoDS_Shape]], right: Dict[int, List[TopoDS_Shape]]) -> List[TopoDS_Shape]:
    return [x for key in left.keys() & right.keys() for x in left[key] if any(x.IsSame(y) for y in right[key])]


def _regions(pieces: Sequence[TopoDS_Shape]) -> List[List[int]]:
    """面を共有する片をつないだ連結領域(片の添字の列)。"""
    parent = list(range(len(pieces)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owners: Dict[int, List[Tuple[int, TopoDS_Shape]]] = {}
    for index, piece in enumerate(pieces):
        for key, faces in _buckets(piece, TopAbs_FACE).items():
            for face in faces:
                for other_index, other_face in owners.get(key, []):
                    if other_face.IsSame(face):
                        parent[find(index)] = find(other_index)
                owners.setdefault(key, []).append((index, face))
    groups: Dict[int, List[int]] = {}
    for index in range(len(pieces)):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def _region_measures(pieces: Sequence[TopoDS_Shape]) -> Dict[str, float]:
    volume = sum(_brep_geometry._interference_volume(piece) for piece in pieces)
    faces: List[list] = []  # [面, 数えた回数]
    for piece in pieces:
        for bucket in _buckets(piece, TopAbs_FACE).values():
            for face in bucket:
                for slot in faces:
                    if slot[0].IsSame(face):
                        slot[1] += 1
                        break
                else:
                    faces.append([face, 1])
    area = sum(_brep_geometry._interference_area(face) for face, count in faces if count == 1)
    if not (area > 0) or not (volume >= 0):
        raise ValueError(f"重なりの面積・体積が不正: V={volume} A={area}")
    sides = _brep_geometry._interference_obb_sides(_brep_topology.compound(list(pieces)))
    return {"volume": volume, "thickness": 2 * volume / area, "obb_min": sides[0]}


def overlap_measures(pieces: Sequence[TopoDS_Shape]) -> Dict[str, Any]:
    """重なりの立体片を、面を共有する連結領域にまとめ、領域ごとに体積・外側の面積・2V/A・有向境界箱の最小辺を測る。

    2つの片に共有される面(GF の分割でできた内部の面)は面積から除く。除かないと同じ重なりでも分割のされ方で 2V/A が下がる
    (実測: 2.22 → 1.67)。有向境界箱も片ごとではなく領域ごと(厚い領域が薄い片に割れても薄く見せない)。
    `thickness` / `obb_min` は領域の中で最も厚いもの(どれか1つでも厚ければ tiny ではない)。
    """
    regions = [_region_measures([pieces[index] for index in group]) for group in _regions(pieces)]
    whole = _brep_topology.compound(list(pieces))
    low, high = _brep_geometry._interference_exact_bbox(whole)
    return {
        "center": _brep_geometry._interference_volume_center(whole),
        "bbox": {"min": low, "max": high},
        "volume": sum(region["volume"] for region in regions),
        "regions": len(regions),
        "pieces": len(pieces),
        "thickness": max(region["thickness"] for region in regions),
        "obb_min": max(region["obb_min"] for region in regions),
    }


# ------------------------------------------------------------------------------------------------
# 画面へ描く形
# ------------------------------------------------------------------------------------------------
def triangles(shape: TopoDS_Shape, deviation: float) -> Dict[str, List]:
    """形の三角形を `{"positions": [x,y,z,...], "indices": [...]}` で返す(面の巻き方向を揃える。節点は位置の変換を適用)。

    外部の罠: 三角形分割は写しの上で作る。GF の像は入力と面の実体(TShape)を共有するので、像をそのまま分割すると入力の
    面に三角形分割が書き込まれ、後の組の GF の結果が変わる(2026-10-22 実測: PC ケースの1組が、描画ありでは厚さ 4e-6 mm の
    微小重なり、描画なしでは面積 573 mm² の接触になった)。
    """
    shape = BRepBuilderAPI_Copy(shape, True, False).Shape()  # 幾何も写し、三角形分割は写さない(`kernel.copy_shape` と同じ)
    BRepMesh_IncrementalMesh(shape, deviation, False, 0.5, False)
    positions: List[float] = []
    indices: List[int] = []
    for face in _brep_topology.subshapes(shape, TopAbs_FACE):
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(TopoDS.Face_s(face), location)
        if triangulation is None:
            continue
        base = len(positions) // 3
        transformation = location.Transformation()
        for node_index in range(1, triangulation.NbNodes() + 1):
            node = triangulation.Node(node_index)
            if not location.IsIdentity():
                node = node.Transformed(transformation)
            positions.extend((node.X(), node.Y(), node.Z()))
        reverse = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
        for triangle_index in range(1, triangulation.NbTriangles() + 1):
            first, second, third = triangulation.Triangle(triangle_index).Get()
            if reverse:
                second, third = third, second
            indices.extend((base + first - 1, base + second - 1, base + third - 1))
    return {"positions": positions, "indices": indices}


def _polyline(edge: TopoDS_Shape, deviation: float) -> List[List[float]]:
    curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
    sampler = GCPnts_QuasiUniformDeflection(curve, deviation)
    if not sampler.IsDone():
        return [_point(vertex) for vertex in _brep_topology.subshapes(edge, TopAbs_VERTEX)]
    return [[p.X(), p.Y(), p.Z()] for p in (sampler.Value(i) for i in range(1, sampler.NbPoints() + 1))]


def _point(vertex: TopoDS_Shape) -> List[float]:
    point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertex))
    return [point.X(), point.Y(), point.Z()]


# ------------------------------------------------------------------------------------------------
# 最短距離
# ------------------------------------------------------------------------------------------------
def min_distance(first: TopoDS_Shape, second: TopoDS_Shape) -> Dict[str, Any]:
    """2つの形の最短距離。形全体 → 面の対ごと → 縁の点から、の順に落とす。

    戻り値: {"ok": True, "distance", "points": [p, q], "stage": "whole"|"faces", "upper_bound": bool}
            {"ok": False, "reason"}
    角を丸めた箱に丸めを同心にした中子を入れる形では、形全体の最短距離が例外で落ちる(丸めの面が同心に向かい合い、最短の組が
    無数にある)ので面の対ごとに測る。それでも落ちた対だけ片方の面の縁の点から相手の面へ測る。縁の点からの値は真の最小以上
    (同心の面なら一致)なので、縁の点に頼った組は `upper_bound` を真にする。
    """
    whole = _distance(first, second)
    if whole is not None:
        return {"ok": True, "distance": whole[0], "points": [whole[1], whole[2]], "stage": "whole", "upper_bound": False}
    return _facewise_distance(first, second)


def _distance(first: TopoDS_Shape, second: TopoDS_Shape) -> Optional[Tuple[float, List[float], List[float]]]:
    try:
        measure = BRepExtrema_DistShapeShape()
        measure.SetMultiThread(True)
        measure.LoadS1(first)
        measure.LoadS2(second)
        measure.Perform()
        if not measure.IsDone() or measure.NbSolution() == 0:
            return None
        p, q = measure.PointOnShape1(1), measure.PointOnShape2(1)
        return measure.Value(), [p.X(), p.Y(), p.Z()], [q.X(), q.Y(), q.Z()]
    except Exception:  # noqa: BLE001  OCP の例外は共通の基底を持たず StdFail_NotDone が Exception を直に継ぐ
        return None


def _facewise_distance(first: TopoDS_Shape, second: TopoDS_Shape) -> Dict[str, Any]:
    faces_a, faces_b = _brep_topology.subshapes(first, TopAbs_FACE), _brep_topology.subshapes(second, TopAbs_FACE)
    boxes_a, boxes_b = [_brep_geometry._interference_loose_bbox(f) for f in faces_a], [_brep_geometry._interference_loose_bbox(f) for f in faces_b]
    order = sorted(
        (box_gap(boxes_a[i], boxes_b[j]), i, j) for i in range(len(faces_a)) for j in range(len(faces_b))
    )
    best: Optional[Tuple[float, List[float], List[float]]] = None
    sampled = False
    for gap, i, j in order:
        if best is not None and gap > best[0]:
            break  # 以降の対は箱の距離がすでに今の最小より遠い
        found = _distance(faces_a[i], faces_b[j])
        if found is None:
            sampled = True
            candidates = [
                c for c in (
                    [_distance(p, faces_b[j]) for p in _boundary_points(faces_a[i])]
                    + [_distance(p, faces_a[i]) for p in _boundary_points(faces_b[j])]
                ) if c is not None
            ]
            found = min(candidates, key=lambda c: c[0]) if candidates else None
        if found is not None and (best is None or found[0] < best[0]):
            best = found
    if best is None:
        return {"ok": False, "reason": "最短距離が測れなかった(面の対ごとにも縁の点からも)"}
    return {"ok": True, "distance": best[0], "points": [best[1], best[2]], "stage": "faces", "upper_bound": sampled}


def box_gap(first: Tuple[Vec, Vec], second: Tuple[Vec, Vec]) -> float:
    """2つの軸整列箱の間の距離(重なっていれば 0)。"""
    total = 0.0
    for axis in range(3):
        gap = max(0.0, first[0][axis] - second[1][axis], second[0][axis] - first[1][axis])
        total += gap * gap
    return total ** 0.5


def _boundary_points(face: TopoDS_Shape) -> List[TopoDS_Shape]:
    points = []
    for edge_shape in _brep_topology.subshapes(face, TopAbs_EDGE):
        edge = TopoDS.Edge_s(edge_shape)
        if BRep_Tool.Degenerated_s(edge):
            continue
        curve = BRepAdaptor_Curve(edge)
        first, last = curve.FirstParameter(), curve.LastParameter()
        for step in range(_EDGE_SAMPLES + 1):
            points.append(BRepBuilderAPI_MakeVertex(curve.Value(first + (last - first) * step / _EDGE_SAMPLES)).Vertex())
    return points

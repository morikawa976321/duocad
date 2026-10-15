"""スケッチ平面の枠と、スケッチ図形からOCCTの辺・領域を作る幾何(OCPを触るカーネル層)。

平面の枠(`face_frame` / `sketch_plane_frame` / `offset_plane_frame`)、領域検出(`sketch_regions`)、
図形種別ごとの辺と外接矩形(`_KERNEL_ENTITY_KIND_TABLE`)を持つ。面の数え方(`_face_map` の
1始まりの添字)は `kernel` の側にあり、ここはそれを読むだけ —— 依存は kernel_sketch → kernel の一方向。

戻り値の規律は kernel と同じ(JSON化できる素の値だけ)。例外は `sketch_regions` の戻りに含まれる
領域の面(`kernel_extrude.extrude_regions` の入力になる TopoDS_Face)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Sequence, Tuple

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse, BRepAlgoAPI_Splitter
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_Copy,
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.Bnd import Bnd_Box
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffset
from OCP.Geom import Geom_BezierCurve
from OCP.GeomAbs import GeomAbs_Arc, GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum, TopAbs_State
from OCP.TopoDS import TopoDS, TopoDS_Shape  # noqa: F401  TopoDS_Shape は型注釈だけ
from OCP.TopTools import TopTools_ListOfShape
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.gp import gp_Ax2, gp_Ax3, gp_Circ, gp_Dir, gp_Pln, gp_Pnt

from .kernel import _BASE_PLANE_AXES, _face_map, _face_props, classify_face, face_of
from .mesh_geometry import compound, indexed_map


def require_planar_face(shape: "TopoDS_Shape", face_index: int) -> Any:
    """面 `face_index` が平面でなければ ValueError。平面ならその面を返す(`_face_map` の数え直しを避ける)。"""
    face = face_of(shape, face_index)
    kind = classify_face(face)
    if kind != "plane":
        raise ValueError(
            f"face {face_index} is {kind}: スケッチを貼れるのは平面の面だけ"
        )
    return face


def face_frame(shape: "TopoDS_Shape", face_index: int) -> dict:
    """面 `face_index` の**その時点の姿勢**を素の数値の枠として切り出す。

    戻り値は `sketch_plane_frame` と同じ形 —— {"origin", "u", "v", "normal"}。

    **これがスケッチの姿勢を「焼き付ける」入口である**。スケッチを貼った面の添字は、その後の
    面取り・フィレットで(スケッチと無関係な場所の操作でも)静かに別の面へずれる。ずれた添字で
    引き直すと、平面だったはずの参照が円錐面・円筒面になって領域計算ごと落ちる
    (実測: `face is cone: スケッチ平面にできるのは平面だけ`)。
    面が完全に消えてもスケッチはその場に残る、というのが CAD の期待される挙動なので
    (Fusion 360 実機で反証確認済み)、**姿勢は作成時に1回だけ解決して固定する**。

    平面でなければ ValueError(スケッチを貼れない面)。
    """
    return _frame_from_ax3(_ax3_from_face(require_planar_face(shape, face_index)))


def sketch_plane_normal(plane: dict, face: Any = None) -> Tuple[float, float, float]:
    """スケッチ平面の法線(単位)。押し出し方向の正の向き。"""
    ax3 = _ax3_from_plane(plane, face=face)
    direction = ax3.Direction()
    return (direction.X(), direction.Y(), direction.Z())


def sketch_plane_frame(plane: dict, face: Any = None) -> dict:
    """スケッチ平面の3Dの枠 {"origin", "u", "v", "normal"}(各 [x,y,z])を素の数値で返す。

    (u, v) と3D座標を行き来する規則そのもの(`p = origin + u*u_axis + v*v_axis`)。アプリ層が
    独自に持つと基準平面の軸の取り方と `ZReverse` の扱いがずれるので、**枠はコアが配る**。
    """
    return _frame_from_ax3(_ax3_from_plane(plane, face=face))


def offset_plane_frame(frame: dict, distance: float) -> dict:
    """`frame` を自身の法線方向へ `distance` だけ平行移動した枠(u/v/normalは不変)。

    `gp_Ax3` を経由しない数値計算。**往復検査はここでせず**、後で `_ax3_from_frame` を通るときに行う。
    """
    origin = frame["origin"]
    normal = frame["normal"]
    return {
        "origin": [float(origin[i]) + float(normal[i]) * float(distance) for i in range(3)],
        "u": [float(value) for value in frame["u"]],
        "v": [float(value) for value in frame["v"]],
        "normal": [float(value) for value in normal],
    }


def copy_face(face: Any) -> Any:
    """領域の面の写し(形も三角形分割も元と共有しない)。

    `region` は検出結果を入力の署名で共有する(`region._DETECTED`)。面は三角形分割を自分に持ち(`kernel.tessellate` が
    張り替える)、押し出したソリッドの底面にもそのまま入るので、共有した面を外へ渡すと、別の粗さの分割が混ざる・
    同じ領域を2回押し出した2つのボディが同じ面を共有してブーリアンの結果が変わる。外へ渡すのは必ずこの写し。
    """
    return TopoDS.Face_s(BRepBuilderAPI_Copy(face, True, False).Shape())


def sketch_regions(
    plane: dict, entities: list, face: Any = None
) -> List[dict]:
    """スケッチの閉領域を返す。

    戻り値: [{"index": int, "area": float, "face": <TopoDS_Face>, "centroid": [x,y,z]}, ...]
    添字は1始まり。順序は面積の降順、同値なら重心の (u, v) の昇順(決定的)。
    """
    if not entities:
        return []

    ax3 = _ax3_from_plane(plane, face=face)
    edges = []
    for entity in entities:
        edges.extend(_entity_edges(ax3, entity))
    if not edges:
        return []

    u_min, u_max, v_min, v_max = _entities_bbox_2d(entities)
    diagonal = math.hypot(u_max - u_min, v_max - v_min)
    margin = max(10.0, diagonal)
    aux = _plane_face(
        ax3,
        u_min - margin,
        u_max + margin,
        v_min - margin,
        v_max + margin,
    )

    arguments = TopTools_ListOfShape()
    arguments.Append(aux)
    tools = TopTools_ListOfShape()
    for edge in edges:
        tools.Append(edge)

    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    if not splitter.IsDone():
        raise ValueError("BRepAlgoAPI_Splitter failed (IsDone is false)")
    result = splitter.Shape()
    if result is None or result.IsNull():
        raise ValueError("BRepAlgoAPI_Splitter produced an empty shape")

    tol = 1.0e-6 * max(1.0, diagonal)
    kept: List[tuple] = []
    result_map = _face_map(result)
    for index in range(1, result_map.Extent() + 1):
        region_face = TopoDS.Face_s(result_map.FindKey(index))
        if not _face_vertices_inside_bbox_2d(
            region_face, ax3, u_min, u_max, v_min, v_max, tol
        ):
            continue
        area, centroid = _face_props(region_face)
        if area <= 0:
            continue
        u, v = _to_uv(ax3, centroid[0], centroid[1], centroid[2])
        # 第1キーは**丸めた**面積。生の面積だと等しい2領域でも1e-16級の積分残差で先に決着し、
        # docstringが約束するタイブレーク(重心の u,v)が発火しない(y=0で割った半円が
        # 628.3185307179587 と ...584 になる)。
        kept.append((-round(area, 6), u, v, area, centroid, region_face))

    kept.sort(key=lambda item: (item[0], item[1], item[2]))
    regions: List[dict] = []
    for position, item in enumerate(kept, start=1):
        _neg_area, _u, _v, area, centroid, region_face = item
        regions.append(
            {
                "index": position,
                "area": float(area),
                "face": region_face,
                "centroid": [float(centroid[0]), float(centroid[1]), float(centroid[2])],
            }
        )
    return regions


#: 包み込みの面の結合・境界判定に使う許容差[mm]。OCCTの既定(1e-7)では、R と d の円弧が
#: 同じ点で接するオフセット結果の結合を取りこぼしうるので、その10倍にする。
_ENVELOPE_TOLERANCE = 1.0e-6


def envelope_faces(
    plane: dict, faces: Sequence[Any], radius: float, offset: float
) -> List[Any]:
    """選択領域の面 `faces` を D(offset)( D(-radius)( D(radius)(和集合) ) ) で包んだ面の並び。

    D(r) は半径 r の円とのミンコフスキー和(r<0 は縮小)。角の接続は円弧(`GeomAbs_Arc`)で、
    出力の辺は元の直線・円弧と、radius・offset による円弧だけになる(近似はしない)。
    戻り値は**単一とは限らない**(分裂した数を判定するのは呼び出し側)。空リストはオフセットで
    領域が消えたこと。`radius` は 0 以上、`offset` は負も許す。
    """
    if not faces:
        return []
    ax3 = _ax3_from_plane(plane)
    pieces = list(faces)
    if radius > 0.0:
        pieces = _dilate_faces(ax3, pieces, radius)
        pieces = _erode_faces(ax3, pieces, radius)
    else:
        pieces = _union_faces(pieces)
    if offset > 0.0:
        pieces = _dilate_faces(ax3, pieces, offset)
    elif offset < 0.0:
        pieces = _erode_faces(ax3, pieces, -offset)
    return _shape_faces(_unified(compound(pieces)))


def _shape_faces(shape: Any) -> List[Any]:
    shape_map = _face_map(shape)
    return [TopoDS.Face_s(shape_map.FindKey(i)) for i in range(1, shape_map.Extent() + 1)]


def _unified(shape: Any) -> Any:
    """同一平面・同一曲線上で隣り合う面・辺を1つにまとめる(共線の線分や同心の円弧の分割を消す)。"""
    unify = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    unify.Build()
    return unify.Shape()


def _union_faces(faces: Sequence[Any]) -> List[Any]:
    """面を和集合にして、つながった成分ごとの面の並びにする(辺で接するだけの面も1枚に結合する)。"""
    if len(faces) <= 1:
        return list(faces)
    return _shape_faces(_boolean(BRepAlgoAPI_Fuse(), faces[:1], faces[1:]))


def _boolean(operation: Any, arguments: Sequence[Any], tools: Sequence[Any]) -> Any:
    """`arguments` と `tools` で2Dのブーリアンを取る(許容差つき)。"""
    argument_list = TopTools_ListOfShape()
    for shape in arguments:
        argument_list.Append(shape)
    tool_list = TopTools_ListOfShape()
    for shape in tools:
        tool_list.Append(shape)
    operation.SetArguments(argument_list)
    operation.SetTools(tool_list)
    operation.SetFuzzyValue(_ENVELOPE_TOLERANCE)
    operation.Build()
    if not operation.IsDone():
        raise ValueError("領域のブーリアン演算に失敗した")
    return operation.Shape()


def _erode_faces(ax3: "gp_Ax3", faces: Sequence[Any], distance: float) -> List[Any]:
    """面の和集合を `distance` だけ縮める(補集合を広げて引く)。消えたら空リスト。

    **負のオフセットを OCCT に直接頼まない**: 同心円で囲まれた面(座金の形)の内向きオフセットは
    どの距離でも `IsDone()=False` で失敗し、円が完全に潰れる距離(r 以上)でも同じ失敗になる。
    正のオフセットは同じ形で通るので、`縮小 = 十分大きな箱 − (箱 − 面) の拡大` で取る。
    """
    box_3d = Bnd_Box()
    for face in faces:
        BRepBndLib.Add_s(face, box_3d)
    x0, y0, z0, x1, y1, z1 = box_3d.Get()
    corners = [_to_uv(ax3, x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    u_min, u_max = min(c[0] for c in corners), max(c[0] for c in corners)
    v_min, v_max = min(c[1] for c in corners), max(c[1] for c in corners)
    margin = 2.0 * distance + 1.0
    box = _plane_face(ax3, u_min - margin, u_max + margin, v_min - margin, v_max + margin)
    complement = _shape_faces(_boolean(BRepAlgoAPI_Cut(), [box], list(faces)))
    grown = _dilate_faces(ax3, complement, distance)
    return _shape_faces(_boolean(BRepAlgoAPI_Cut(), [box], grown))


def _dilate_faces(ax3: "gp_Ax3", faces: Sequence[Any], distance: float) -> List[Any]:
    """面の和集合を `distance`(正)だけ広げ、つながった成分ごとの面の並びにする。

    `BRepOffsetAPI_MakeOffset` は面を渡してもワイヤの並びしか返さないので、外周と穴を
    `_faces_from_wires` で組み直す。
    """
    result: List[Any] = []
    for face in faces:
        maker = BRepOffsetAPI_MakeOffset(face, GeomAbs_Arc)
        maker.Perform(distance)
        if not maker.IsDone():
            raise ValueError(f"2Dオフセット(距離 {distance} mm)に失敗した")
        shape = maker.Shape()
        if shape is None or shape.IsNull():
            raise ValueError(f"2Dオフセット(距離 {distance} mm)が空の結果を返した")
        wire_map = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_WIRE)
        wires = [TopoDS.Wire_s(wire_map.FindKey(i)) for i in range(1, wire_map.Extent() + 1)]
        # 入れ子の数え上げは入力の面ごと(別の面のオフセット結果は重なりうる)。
        result.extend(_faces_from_wires(ax3, wires))
    return _union_faces(result)


def _faces_from_wires(ax3: "gp_Ax3", wires: Sequence[Any]) -> List[Any]:
    """ワイヤの集まりを、入れ子の深さ(偶数=外周・奇数=穴)で面(外周+穴)へ組む。

    `BRepBuilderAPI_MakeFace` はワイヤの向きによらず有界な面を作る(時計回りでも面積は正)ので、
    向きでは外周と穴を区別できない —— 他のワイヤの面が、上のワイヤの1点を含むかで数える。
    """
    pln = gp_Pln(ax3)
    entries: List[dict] = []
    for wire in wires:
        made = BRepBuilderAPI_MakeFace(pln, wire, True)
        if not made.IsDone():
            continue
        face = made.Face()
        edge = TopoDS.Edge_s(indexed_map(wire, TopAbs_ShapeEnum.TopAbs_EDGE).FindKey(1))
        curve = BRepAdaptor_Curve(edge)
        point = curve.Value((curve.FirstParameter() + curve.LastParameter()) / 2.0)
        entries.append({"face": face, "point": point, "area": _face_props(face)[0]})

    for entry in entries:
        entry["containers"] = [
            other
            for other in entries
            if other is not entry
            and BRepClass_FaceClassifier(other["face"], entry["point"], _ENVELOPE_TOLERANCE).State()
            == TopAbs_State.TopAbs_IN
        ]
        entry["depth"] = len(entry["containers"])

    holes_of: Dict[int, List[dict]] = {}
    for hole in (e for e in entries if e["depth"] % 2 == 1):
        owners = [c for c in hole["containers"] if c["depth"] % 2 == 0]
        if owners:
            owner = min(owners, key=lambda c: c["area"])
            holes_of.setdefault(id(owner), []).append(hole)

    faces: List[Any] = []
    for entry in sorted((e for e in entries if e["depth"] % 2 == 0), key=lambda e: -e["area"]):
        face = entry["face"]
        for hole in holes_of.get(id(entry), []):
            face = _shape_faces(_boolean(BRepAlgoAPI_Cut(), [face], [hole["face"]]))[0]
        faces.append(face)
    return faces


def face_boundary_entities(plane: dict, face: Any) -> List[dict]:
    """面の境界(外周と穴)を、スケッチ図形 `{"kind", "params"}` の並びにする。**線分・円弧・円だけ**。

    辺がそれ以外(ベジエなど)なら ValueError。ベジエの領域を包むと、オフセットは元の曲線の
    近似曲線を返し、線分と円弧だけという指定を満たせないため。
    """
    ax3 = _ax3_from_plane(plane)
    entities: List[dict] = []
    edge_map = indexed_map(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    for i in range(1, edge_map.Extent() + 1):
        curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge_map.FindKey(i)))
        first, last = curve.FirstParameter(), curve.LastParameter()
        kind = curve.GetType()

        def uv(parameter: float, curve: Any = curve) -> Tuple[float, float]:
            point = curve.Value(parameter)
            return _to_uv(ax3, point.X(), point.Y(), point.Z())

        if kind == GeomAbs_CurveType.GeomAbs_Line:
            (x0, y0), (x1, y1) = uv(first), uv(last)
            entities.append({"kind": "line", "params": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}})
        elif kind == GeomAbs_CurveType.GeomAbs_Circle:
            circle = curve.Circle()
            centre = circle.Location()
            cx, cy = _to_uv(ax3, centre.X(), centre.Y(), centre.Z())
            radius = circle.Radius()
            (sx, sy), (ex, ey) = uv(first), uv(last)
            if math.hypot(ex - sx, ey - sy) <= _ENVELOPE_TOLERANCE:
                entities.append({"kind": "circle", "params": {"cx": cx, "cy": cy, "radius": radius}})
                continue
            mx, my = uv((first + last) / 2.0)
            start = math.degrees(math.atan2(sy - cy, sx - cx))
            end = math.degrees(math.atan2(ey - cy, ex - cx))
            middle = math.degrees(math.atan2(my - cy, mx - cx))
            # 辺の向きと円の軸の向きは独立なので、始点→中点→終点の回り方から反時計回りの端点を決める。
            if (middle - start) % 360.0 > (end - start) % 360.0:
                start, end = end, start
            entities.append(
                {
                    "kind": "arc",
                    "params": {
                        "cx": cx,
                        "cy": cy,
                        "radius": radius,
                        "start_deg": start,
                        "end_deg": end,
                    },
                }
            )
        else:
            raise ValueError(
                f"包んだ結果に線分・円弧以外の辺({kind.name})が出る"
                "(ベジエ曲線を含む領域は包めない)"
            )
    return entities


def _ax3_from_plane(plane: dict, face: Any = None) -> "gp_Ax3":
    """PlaneRef から gp_Ax3 を作る。面参照で平面でなければ ValueError。

    **`kind` を問わず `plane["frame"]`(作成時に焼き付けた姿勢)が最優先**で、枠がある限り
    `face` は見ない(`face_frame` のdocstring)。`sketch.resolve_plane` が必ず `frame` を焼き付けて
    渡すので、新しい `kind` が増えても無改修で通る。枠を持たない面参照は `face` から解決する
    (古い操作ログ・手組みの PlaneRef のための後方互換)。
    """
    kind = plane.get("kind")
    if kind == "base":
        name = plane.get("name")
        if name not in _BASE_PLANE_AXES:
            raise ValueError(f"unknown base plane: {name!r}")
        origin, normal, xdir = _BASE_PLANE_AXES[name]
        return gp_Ax3(gp_Pnt(*origin), gp_Dir(*normal), gp_Dir(*xdir))
    frame = plane.get("frame")
    if frame is not None:
        return _ax3_from_frame(frame)
    if kind == "face":
        if face is None:
            raise ValueError("face plane requires a TopoDS_Face")
        return _ax3_from_face(face)
    raise ValueError(f"invalid sketch plane: {plane!r}")


def _frame_from_ax3(ax3: "gp_Ax3") -> dict:
    """gp_Ax3 を素の数値の枠へ(`_ax3_from_frame` の逆)。"""
    origin = ax3.Location()
    xdir = ax3.XDirection()
    ydir = ax3.YDirection()
    normal = ax3.Direction()
    return {
        "origin": [origin.X(), origin.Y(), origin.Z()],
        "u": [xdir.X(), xdir.Y(), xdir.Z()],
        "v": [ydir.X(), ydir.Y(), ydir.Z()],
        "normal": [normal.X(), normal.Y(), normal.Z()],
    }


def _ax3_from_frame(frame: Any) -> "gp_Ax3":
    """素の数値の枠から gp_Ax3 を復元する(`_frame_from_ax3` の逆)。

    **`v` を捨ててはいけない**。`gp_Ax3(location, direction, xdirection)` は必ず右手系を作る
    (`YDirection = Direction ^ XDirection`)のに対し、`_ax3_from_face` が反転面へ当てる
    `ZReverse()` は Direction だけを裏返して X/Y を据え置く(＝左手系)。origin/normal/u だけで
    組み直すと反転面のスケッチは v 軸が符号反転し、図形が鏡像になる(実測)。
    そこで `v` も保存し、必要なら `YReverse()` で左手系へ戻す。

    復元した軸が保存値と一致しなければ ValueError(黙って鏡像の平面を配らない)。
    """
    if not isinstance(frame, dict):
        raise ValueError(f"invalid sketch plane frame: {frame!r}")
    try:
        given = {
            name: [float(value) for value in frame[name]]
            for name in ("origin", "u", "v", "normal")
        }
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid sketch plane frame: {frame!r}") from error
    for name, vector in given.items():
        if len(vector) != 3 or not all(math.isfinite(value) for value in vector):
            raise ValueError(f"invalid sketch plane frame ({name}): {frame!r}")
    origin, u, v, normal = given["origin"], given["u"], given["v"], given["normal"]

    try:
        # 長さ0や normal と u が平行な枠は OCCT が Standard_ConstructionError で落とす。
        # 呼び出し側が「平面が不正」を1種類の例外で捕まえられるよう ValueError へ揃える。
        ax3 = gp_Ax3(gp_Pnt(*origin), gp_Dir(*normal), gp_Dir(*u))
    except Exception as error:
        raise ValueError(f"invalid sketch plane frame: {frame!r}") from error
    if _dot3(ax3.YDirection(), v) < 0.0:
        ax3.YReverse()

    restored = _frame_from_ax3(ax3)
    for name, vector in given.items():
        for expected, actual in zip(vector, restored[name]):
            if abs(expected - actual) > 1.0e-9 * max(1.0, abs(expected)):
                raise ValueError(
                    f"sketch plane frame is not orthonormal ({name}): {frame!r}"
                )
    return ax3


def _dot3(direction: "gp_Dir", vector: Sequence[float]) -> float:
    """gp_Dir と素の3成分の内積。"""
    return (
        direction.X() * float(vector[0])
        + direction.Y() * float(vector[1])
        + direction.Z() * float(vector[2])
    )


def _ax3_from_face(face: Any) -> "gp_Ax3":
    """平面の面から gp_Ax3 を取る。平面でなければ ValueError。REVERSED なら法線を反転。"""
    face = TopoDS.Face_s(face)
    adaptor = BRepAdaptor_Surface(face)
    if adaptor.GetType() != GeomAbs_SurfaceType.GeomAbs_Plane:
        raise ValueError(
            f"face is {classify_face(face)}: スケッチ平面にできるのは平面だけ"
        )
    # OCP には gp_Ax3(gp_Ax3) のコンストラクタが無いので位置・法線・X方向から組み直す。
    # 反転面は `ZReverse`(`ReverseZ` は存在しない)で法線だけを裏返す(X/Y は変わらない)。
    position = adaptor.Plane().Position()
    ax3 = gp_Ax3(position.Location(), position.Direction(), position.XDirection())
    if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
        ax3.ZReverse()
    return ax3


def _to_pnt(ax3: "gp_Ax3", u: float, v: float) -> "gp_Pnt":
    """スケッチ平面上の (u, v) を 3D 点へ。"""
    origin = ax3.Location()
    xdir = ax3.XDirection()
    ydir = ax3.YDirection()
    return gp_Pnt(
        origin.X() + u * xdir.X() + v * ydir.X(),
        origin.Y() + u * xdir.Y() + v * ydir.Y(),
        origin.Z() + u * xdir.Z() + v * ydir.Z(),
    )


def _to_uv(ax3: "gp_Ax3", x: float, y: float, z: float) -> Tuple[float, float]:
    """3D 点をスケッチ平面の (u, v) へ。"""
    origin = ax3.Location()
    xdir = ax3.XDirection()
    ydir = ax3.YDirection()
    dx = x - origin.X()
    dy = y - origin.Y()
    dz = z - origin.Z()
    u = dx * xdir.X() + dy * xdir.Y() + dz * xdir.Z()
    v = dx * ydir.X() + dy * ydir.Y() + dz * ydir.Z()
    return u, v


def _edges_line(ax3: "gp_Ax3", params: dict) -> List[Any]:
    p0 = _to_pnt(ax3, float(params["x0"]), float(params["y0"]))
    p1 = _to_pnt(ax3, float(params["x1"]), float(params["y1"]))
    maker = BRepBuilderAPI_MakeEdge(p0, p1)
    if not maker.IsDone():
        raise ValueError("failed to build a line edge")
    return [maker.Edge()]


def _edges_circle(ax3: "gp_Ax3", params: dict) -> List[Any]:
    center = _to_pnt(ax3, float(params["cx"]), float(params["cy"]))
    radius = float(params["radius"])
    if radius <= 0:
        raise ValueError(f"circle radius must be positive, got {radius}")
    axis = gp_Ax2(center, ax3.Direction(), ax3.XDirection())
    maker = BRepBuilderAPI_MakeEdge(gp_Circ(axis, radius))
    if not maker.IsDone():
        raise ValueError("failed to build a circle edge")
    return [maker.Edge()]


def _edges_arc(ax3: "gp_Ax3", params: dict) -> List[Any]:
    center = _to_pnt(ax3, float(params["cx"]), float(params["cy"]))
    radius = float(params["radius"])
    if radius <= 0:
        raise ValueError(f"arc radius must be positive, got {radius}")
    start_deg = float(params["start_deg"])
    end_deg = float(params["end_deg"])
    sweep_deg = (end_deg - start_deg) % 360.0
    start_rad = math.radians(start_deg)
    end_rad = start_rad + math.radians(sweep_deg)
    # gp_Ax2(center, N, X): U=0 がスケッチX、U増加が反時計回り(右手系)。
    axis = gp_Ax2(center, ax3.Direction(), ax3.XDirection())
    maker = BRepBuilderAPI_MakeEdge(gp_Circ(axis, radius), start_rad, end_rad)
    if not maker.IsDone():
        raise ValueError("failed to build an arc edge")
    return [maker.Edge()]


#: 3次ベジエの `params` のキー(**始点・制御点1・制御点2・終点の順**)。**この並びが唯一の正**で、
#: `sketch.BEZIER_KEYS` / `sketch._bezier_points` / `sketch` の表の `points` はここから導く。
BEZIER_POINT_KEYS = (("x0", "y0"), ("cx1", "cy1"), ("cx2", "cy2"), ("x1", "y1"))


def _edges_bezier(ax3: "gp_Ax3", params: dict) -> List[Any]:
    """3次ベジエを**厳密に**1本の辺にする(折れ線へ落とさない)。

    折れ線化した輪郭を押し出すと側面が線分の数だけ平面に割れる(実測: 制御点2本ぶんの形で
    面数4に対し130)ので、`Geom_BezierCurve` へ渡す。
    """
    points = TColgp_Array1OfPnt(1, 4)
    for index, (key_x, key_y) in enumerate(BEZIER_POINT_KEYS, start=1):
        points.SetValue(
            index, _to_pnt(ax3, float(params[key_x]), float(params[key_y]))
        )
    maker = BRepBuilderAPI_MakeEdge(Geom_BezierCurve(points))
    if not maker.IsDone():
        raise ValueError("failed to build a bezier edge")
    return [maker.Edge()]


def rectangle_angle(params: dict) -> float:
    """長方形の傾き(度・反時計回り)。**`angle` キーが無ければ 0**。

    `angle` を持たない時代に保存された操作ログの `add_rectangle` はこのキーを書いていない
    (`FORMAT_VERSION` は上げていない)。**再生時に埋めてはいけない** —— `register_value` は
    その時点で空いている最小の `d<n>` を振るので、キーを1つ増やすと以後のパラメータ名が
    ずれ、古いログの `set_parameter` が別の値を指す。
    """
    return float(params.get("angle", 0.0))


def rectangle_corners(params: dict) -> List[Tuple[float, float]]:
    """長方形の4隅を、基準の角 `(x, y)` から反時計回りに返す。

    基準の角から `angle` 度だけ回した局所軸に沿って `width`(第1軸)と `height`(第2軸)を
    測る。**`web/src/entityKinds.ts` の rectangle の `toPoints` / `snap` と同じ規則で
    なければならない** —— ここがずれると、ブラウザの幽霊と確定した形が別の場所に出る。
    """
    x = float(params["x"])
    y = float(params["y"])
    width = float(params["width"])
    height = float(params["height"])
    radians = math.radians(rectangle_angle(params))
    cos_a = math.cos(radians)
    sin_a = math.sin(radians)
    return [
        (x + ox * cos_a - oy * sin_a, y + ox * sin_a + oy * cos_a)
        for ox, oy in ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height))
    ]


def _edges_rectangle(ax3: "gp_Ax3", params: dict) -> List[Any]:
    corners = [_to_pnt(ax3, u, v) for u, v in rectangle_corners(params)]
    edges = []
    for index in range(4):
        maker = BRepBuilderAPI_MakeEdge(corners[index], corners[(index + 1) % 4])
        if not maker.IsDone():
            raise ValueError("failed to build a rectangle edge")
        edges.append(maker.Edge())
    return edges


def _bbox_line(params: dict) -> Tuple[float, float, float, float]:
    x0, y0, x1, y1 = float(params["x0"]), float(params["y0"]), float(params["x1"]), float(params["y1"])
    return (min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1))


def _bbox_circle(params: dict) -> Tuple[float, float, float, float]:
    cx, cy, radius = float(params["cx"]), float(params["cy"]), float(params["radius"])
    return (cx - radius, cx + radius, cy - radius, cy + radius)


def _bbox_arc(params: dict) -> Tuple[float, float, float, float]:
    # 安全側: 円と同じ外接箱で囲む（補助面が少し大きくなるだけ）。
    return _bbox_circle(params)


def _bbox_bezier(params: dict) -> Tuple[float, float, float, float]:
    """制御点の凸包で押さえる(真のbboxより広いことはあっても狭くはない。補助面の大きさにしか使わない)。"""
    xs = [float(params[key_x]) for key_x, _ in BEZIER_POINT_KEYS]
    ys = [float(params[key_y]) for _, key_y in BEZIER_POINT_KEYS]
    return (min(xs), max(xs), min(ys), max(ys))


def _bbox_rectangle(params: dict) -> Tuple[float, float, float, float]:
    corners = rectangle_corners(params)
    us = [u for u, _ in corners]
    vs = [v for _, v in corners]
    return (min(us), max(us), min(vs), max(vs))


@dataclass(frozen=True)
class _KernelEntityKindOps:
    """図形1種別ぶんのOCP依存演算(辺生成・2D bbox)。

    `duocad/sketch.py` の `EntityKindOps`(純幾何)の姉妹表。import境界(OCPはカーネル層だけ)のため
    1本化できない(README.md「モジュールとimport境界」)。
    """

    edges: Callable[["gp_Ax3", dict], List[Any]]
    bbox: Callable[[dict], Tuple[float, float, float, float]]


_KERNEL_ENTITY_KIND_TABLE: Dict[str, _KernelEntityKindOps] = {
    "line": _KernelEntityKindOps(_edges_line, _bbox_line),
    "circle": _KernelEntityKindOps(_edges_circle, _bbox_circle),
    "arc": _KernelEntityKindOps(_edges_arc, _bbox_arc),
    "rectangle": _KernelEntityKindOps(_edges_rectangle, _bbox_rectangle),
    "bezier": _KernelEntityKindOps(_edges_bezier, _bbox_bezier),
}


def _kernel_entity_kind_ops(kind: str) -> _KernelEntityKindOps:
    """種別ごとのOCP依存演算を引く。表に無い種別は ValueError。"""
    try:
        return _KERNEL_ENTITY_KIND_TABLE[kind]
    except KeyError:
        raise ValueError(f"unknown entity kind: {kind!r}") from None


def _entity_edges(ax3: "gp_Ax3", entity: dict) -> List[Any]:
    return _kernel_entity_kind_ops(entity["kind"]).edges(ax3, entity["params"])


def _entities_bbox_2d(entities: list) -> Tuple[float, float, float, float]:
    """図形群の 2D バウンディングボックス (u_min, u_max, v_min, v_max)。"""
    u_min = float("inf")
    u_max = float("-inf")
    v_min = float("inf")
    v_max = float("-inf")

    for entity in entities:
        ops = _kernel_entity_kind_ops(entity["kind"])
        eu_min, eu_max, ev_min, ev_max = ops.bbox(entity["params"])
        u_min = min(u_min, eu_min)
        u_max = max(u_max, eu_max)
        v_min = min(v_min, ev_min)
        v_max = max(v_max, ev_max)

    if u_min == float("inf"):
        return 0.0, 0.0, 0.0, 0.0
    return u_min, u_max, v_min, v_max


def _plane_face(
    ax3: "gp_Ax3", u0: float, u1: float, v0: float, v1: float
) -> Any:
    """スケッチ平面上の軸平行矩形の TopoDS_Face(補助面)。"""
    corners = [
        _to_pnt(ax3, u0, v0),
        _to_pnt(ax3, u1, v0),
        _to_pnt(ax3, u1, v1),
        _to_pnt(ax3, u0, v1),
    ]
    wire_maker = BRepBuilderAPI_MakeWire()
    for index in range(4):
        edge_maker = BRepBuilderAPI_MakeEdge(corners[index], corners[(index + 1) % 4])
        if not edge_maker.IsDone():
            raise ValueError("failed to build an auxiliary edge")
        wire_maker.Add(edge_maker.Edge())
    if not wire_maker.IsDone():
        raise ValueError("failed to build an auxiliary wire")
    face_maker = BRepBuilderAPI_MakeFace(wire_maker.Wire(), True)
    if not face_maker.IsDone():
        raise ValueError("failed to build an auxiliary face")
    face = face_maker.Face()
    if face is None or face.IsNull():
        raise ValueError("auxiliary face is empty")
    return face


def _face_vertices_inside_bbox_2d(
    face: Any,
    ax3: "gp_Ax3",
    u_min: float,
    u_max: float,
    v_min: float,
    v_max: float,
    tol: float,
) -> bool:
    """面の頂点がすべて 2D bbox(+tol) の内側なら True。"""
    shape_map = indexed_map(TopoDS.Face_s(face), TopAbs_ShapeEnum.TopAbs_VERTEX)
    if shape_map.Extent() < 1:
        return False
    for index in range(1, shape_map.Extent() + 1):
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(shape_map.FindKey(index)))
        u, v = _to_uv(ax3, point.X(), point.Y(), point.Z())
        if not (
            u_min - tol <= u <= u_max + tol and v_min - tol <= v <= v_max + tol
        ):
            return False
    return True

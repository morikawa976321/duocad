"""スケッチ平面の枠と、スケッチ図形からOCCTの辺・領域を作る幾何(OCPを触るカーネル層)。

平面の枠(`face_frame` / `sketch_plane_frame` / `offset_plane_frame`)、領域検出(`sketch_regions`)、
図形種別ごとの辺と外接矩形(`_KERNEL_ENTITY_KIND_TABLE`)を持つ。面の数え方(`_face_map` の
1始まりの添字)は `brep_topology`、平面と面集合の共通機構は `brep_planar` を参照する。

戻り値の規律は kernel と同じ(JSON化できる素の値だけ)。例外は `sketch_regions` の戻りに含まれる
領域の面(`kernel_extrude.extrude_regions` の入力になる TopoDS_Face)。
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Sequence, Tuple

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Splitter
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
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.Geom import Geom_BezierCurve, Geom_TrimmedCurve
from OCP.GeomAbs import GeomAbs_Arc, GeomAbs_CurveType, GeomAbs_Shape, GeomAbs_SurfaceType
from OCP.GeomConvert import GeomConvert_ApproxCurve, GeomConvert_BSplineCurveToBezierCurve
from OCP.TopAbs import TopAbs_ShapeEnum, TopAbs_State
from OCP.TopoDS import TopoDS, TopoDS_Shape  # noqa: F401  TopoDS_Shape は型注釈だけ
from OCP.TopTools import TopTools_ListOfShape
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.gp import gp_Ax2, gp_Ax3, gp_Circ, gp_Dir, gp_Elips, gp_Pln, gp_Pnt, gp_Vec

from . import (
    brep_geometry as _brep_geometry,
    brep_planar as _brep_planar,
    brep_topology as _brep_topology,
)


def require_planar_face(shape: "TopoDS_Shape", face_index: int) -> Any:
    """面 `face_index` が平面でなければ ValueError。平面ならその面を返す(`_face_map` の数え直しを避ける)。"""
    face = _brep_topology.face_of(shape, face_index)
    kind = _brep_geometry.classify_face(face)
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
    return _brep_planar._frame_from_ax3(_ax3_from_face(require_planar_face(shape, face_index)))


def sketch_plane_normal(plane: dict) -> Tuple[float, float, float]:
    """スケッチ平面の法線(単位)。押し出し方向の正の向き。"""
    ax3 = _brep_planar._ax3_from_plane(plane)
    direction = ax3.Direction()
    return (direction.X(), direction.Y(), direction.Z())


def sketch_plane_frame(plane: dict) -> dict:
    """スケッチ平面の3Dの枠 {"origin", "u", "v", "normal"}(各 [x,y,z])を素の数値で返す。

    (u, v) と3D座標を行き来する規則そのもの(`p = origin + u*u_axis + v*v_axis`)。アプリ層が
    独自に持つと基準平面の軸の取り方と `ZReverse` の扱いがずれるので、**枠はコアが配る**。
    """
    return _brep_planar._frame_from_ax3(_brep_planar._ax3_from_plane(plane))


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


def sketch_regions(plane: dict, entities: list) -> List[dict]:
    """スケッチの閉領域を返す。

    戻り値: [{"index": int, "area": float, "face": <TopoDS_Face>, "centroid": [x,y,z]}, ...]
    添字は1始まり。順序は面積の降順、同値なら重心の (u, v) の昇順(決定的)。比べる値は面積・u・v とも小数6桁に丸めたもの
    (1ulp の差で順序が変わらないように)。丸めの境目をまたぐ差は割れうる。
    """
    if not entities:
        return []

    ax3 = _brep_planar._ax3_from_plane(plane)
    edges = []
    for entity in entities:
        edges.extend(_entity_edges(ax3, entity))
    if not edges:
        return []

    u_min, u_max, v_min, v_max = _entities_bbox_2d(entities)
    diagonal = math.hypot(u_max - u_min, v_max - v_min)
    margin = max(10.0, diagonal)
    aux = _brep_planar._plane_face(
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
    if result is None or _brep_topology.is_null(result):
        raise ValueError("BRepAlgoAPI_Splitter produced an empty shape")

    tol = 1.0e-6 * max(1.0, diagonal)
    kept: List[tuple] = []
    result_map = _brep_topology._face_map(result)
    for index in range(1, result_map.Extent() + 1):
        region_face = TopoDS.Face_s(result_map.FindKey(index))
        if not _face_vertices_inside_bbox_2d(
            region_face, ax3, u_min, u_max, v_min, v_max, tol
        ):
            continue
        area, centroid = _brep_geometry._face_props(region_face)
        if area <= 0:
            continue
        u, v = _brep_planar._to_uv(ax3, centroid[0], centroid[1], centroid[2])
        # 3つのキーとも**丸めた**値。生の面積だと等しい2領域でも1e-16級の積分残差で先に決着し、
        # docstringが約束するタイブレーク(重心の u,v)が発火しない(y=0で割った半円が
        # 628.3185307179587 と ...584 になる)。重心の u も同じ: 同じ x に縦に積んだ同じ形の領域は u が
        # 1ulp ずれ、v より先に効いて順序が入れ替わる(`move_sketch_entities` の undo は座標が bit 単位では戻らない)。
        kept.append((-round(area, 6), round(u, 6), round(v, 6), area, centroid, region_face))

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
    ax3 = _brep_planar._ax3_from_plane(plane)
    pieces = list(faces)
    if radius > 0.0:
        pieces = _dilate_faces(ax3, pieces, radius)
        pieces = _erode_faces(ax3, pieces, radius)
    else:
        pieces = _brep_planar._union_faces(pieces)
    if offset > 0.0:
        pieces = _dilate_faces(ax3, pieces, offset)
    elif offset < 0.0:
        pieces = _erode_faces(ax3, pieces, -offset)
    return _brep_planar._shape_faces(_brep_planar._unified(_brep_topology.compound(pieces)))


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
    corners = [_brep_planar._to_uv(ax3, x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    u_min, u_max = min(c[0] for c in corners), max(c[0] for c in corners)
    v_min, v_max = min(c[1] for c in corners), max(c[1] for c in corners)
    margin = 2.0 * distance + 1.0
    box = _brep_planar._plane_face(ax3, u_min - margin, u_max + margin, v_min - margin, v_max + margin)
    complement = _brep_planar._shape_faces(_brep_planar._boolean(BRepAlgoAPI_Cut(), [box], list(faces)))
    grown = _dilate_faces(ax3, complement, distance)
    return _brep_planar._shape_faces(_brep_planar._boolean(BRepAlgoAPI_Cut(), [box], grown))


def _dilate_faces(ax3: "gp_Ax3", faces: Sequence[Any], distance: float) -> List[Any]:
    """面の和集合を `distance`(正)だけ広げ、つながった成分ごとの面の並びにする。

    `BRepOffsetAPI_MakeOffset` は面を渡してもワイヤの並びしか返さないので、外周と穴を
    `_faces_from_wires` で組み直す。
    """
    result: List[Any] = []
    for face in faces:
        try:
            grown = _dilate_face(ax3, face, distance)
        except ValueError:
            grown = None
        if grown is None or not _plausible_dilation(face, grown):
            retry = _offset_ready(ax3, face)
            if retry is face:
                if grown is None:
                    raise ValueError(f"2Dオフセット(距離 {distance} mm)に失敗した")
            else:
                grown = _dilate_face(ax3, retry, distance)
            if not _plausible_dilation(face, grown):
                raise ValueError(f"2Dオフセット(距離 {distance} mm)が壊れた形を返した")
        result.extend(grown)
    return _brep_planar._union_faces(result)


def _dilate_face(ax3: "gp_Ax3", face: Any, distance: float) -> List[Any]:
    maker = BRepOffsetAPI_MakeOffset(face, GeomAbs_Arc)
    maker.Perform(distance)
    if not maker.IsDone():
        raise ValueError(f"2Dオフセット(距離 {distance} mm)に失敗した")
    shape = maker.Shape()
    if shape is None or _brep_topology.is_null(shape):
        raise ValueError(f"2Dオフセット(距離 {distance} mm)が空の結果を返した")
    wire_map = _brep_topology.indexed_map(shape, TopAbs_ShapeEnum.TopAbs_WIRE)
    wires = [TopoDS.Wire_s(wire_map.FindKey(i)) for i in range(1, wire_map.Extent() + 1)]
    # 入れ子の数え上げは入力の面ごと(別の面のオフセット結果は重なりうる)。
    return _faces_from_wires(ax3, wires)


def _plausible_dilation(face: Any, grown: Sequence[Any]) -> bool:
    """つながった面を広げた結果としてありうるか(つながった1枚で、面積が減らない)。"""
    return len(grown) == 1 and _brep_geometry._face_props(grown[0])[0] >= _brep_geometry._face_props(face)[0] - _brep_planar._ENVELOPE_TOLERANCE


#: オフセットの前に楕円由来のオフセット曲線を置き換える多項式の B-スプラインの誤差の上限[mm]。出力の近似(`APPROXIMATION_TOLERANCE`)
#: より3桁小さく、最終の誤差にほとんど足さない。
_REOFFSET_TOLERANCE = 1.0e-6


def _offset_ready(ax3: "gp_Ax3", face: Any) -> Any:
    """広げると失敗した・壊れた形が返った面の、直線・円に乗らないオフセット曲線の辺を多項式の B-スプラインで近似し直した面。無ければ元の面。

    外部の罠: 楕円を広げて縮めた面(辺が楕円のオフセットのオフセット)を `BRepOffsetAPI_MakeOffset` でさらに広げると、例外も
    `IsDone()=False` も出さずに壊れたワイヤを返す(2026-10-22 実測: 20×10 の楕円を R=1, d=1.5 で包むと、面積 43.3 と −16.8 の
    2枚に分かれた。正しくは 236.81 の1枚)。同じ面で距離によっては `IsDone()=False` になる(角度 30 度の同じ楕円で R=1, d=2。
    近似し直すと 266.53 の1枚)。`BRepBuilderAPI_NurbsConvert` で有理 B-スプラインにすると access violation で
    プロセスごと落ちる。有理でない多項式の B-スプライン(C2 を頼むので5次)に近似し直すと正しく広がる。
    壊れたときだけ使う: 辺ごとに近似すると継ぎ目の接線がわずかに折れ、広げた結果の継ぎ目に短い円弧が挟まる。
    直線・円に乗るオフセット曲線(円と長方形の領域で出る)は触らない(最後に円弧へ直せなくなる)。
    """
    wire_map = _brep_topology.indexed_map(face, TopAbs_ShapeEnum.TopAbs_WIRE)
    rebuilt_wires: List[Any] = []
    changed = False
    for w in range(1, wire_map.Extent() + 1):
        builder = BRepBuilderAPI_MakeWire()
        explorer = BRepTools_WireExplorer(TopoDS.Wire_s(wire_map.FindKey(w)))
        while explorer.More():
            edge = explorer.Current()
            replacement = _reoffset_edge(ax3, edge)
            changed = changed or replacement is not None
            builder.Add(replacement if replacement is not None else edge)
            explorer.Next()
        if not builder.IsDone():
            return face
        rebuilt_wires.append(builder.Wire())
    if not changed:
        return face
    rebuilt = _faces_from_wires(ax3, rebuilt_wires)
    return rebuilt[0] if len(rebuilt) == 1 else face


def _reoffset_edge(ax3: "gp_Ax3", edge: Any) -> Any:
    """直線・円に乗らないオフセット曲線の辺を、多項式の B-スプラインの辺にしたもの。それ以外・近似できないなら None。"""
    curve = BRepAdaptor_Curve(edge)
    if curve.GetType() != GeomAbs_CurveType.GeomAbs_OffsetCurve:
        return None
    first, last = curve.FirstParameter(), curve.LastParameter()

    def uv(parameter: float) -> Tuple[float, float]:
        point = curve.Value(parameter)
        return _brep_planar._to_uv(ax3, point.X(), point.Y(), point.Z())

    if _entities_of_offset_curve(uv, first, last) is not None:
        return None
    approx = GeomConvert_ApproxCurve(
        Geom_TrimmedCurve(BRep_Tool.Curve_s(edge, first, last), first, last),
        _REOFFSET_TOLERANCE, GeomAbs_Shape.GeomAbs_C2, 200, 3,
    )
    if not approx.HasResult() or approx.MaxError() > _REOFFSET_TOLERANCE:
        return None
    maker = BRepBuilderAPI_MakeEdge(approx.Curve())
    return maker.Edge() if maker.IsDone() else None


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
        edge = TopoDS.Edge_s(_brep_topology.indexed_map(wire, TopAbs_ShapeEnum.TopAbs_EDGE).FindKey(1))
        curve = BRepAdaptor_Curve(edge)
        point = curve.Value((curve.FirstParameter() + curve.LastParameter()) / 2.0)
        entries.append({"face": face, "point": point, "area": _brep_geometry._face_props(face)[0]})

    for entry in entries:
        entry["containers"] = [
            other
            for other in entries
            if other is not entry
            and BRepClass_FaceClassifier(other["face"], entry["point"], _brep_planar._ENVELOPE_TOLERANCE).State()
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
            face = _brep_planar._shape_faces(_brep_planar._boolean(BRepAlgoAPI_Cut(), [face], [hole["face"]]))[0]
        faces.append(face)
    return faces


#: 正確な図形に直せない辺を3次ベジエの列で近似するときの、誤差の上限[mm]。図形の大きさに比例させない固定値
#: (3D プリントの精度より2桁小さい。区間の数は誤差がおよそ区間長の4乗に比例するので、大きさの 1/4 乗でしか増えない)。
APPROXIMATION_TOLERANCE = 1.0e-3

#: 近似の1辺あたりの区間の上限。これを超えるのは曲線が尖っている・微分が壊れているときで、黙って細切れの線を出さずに断る。
_MAX_APPROXIMATION_SEGMENTS = 512

#: 区間の誤差を測る標本の数(区間の内側。端点は一致する)。
_APPROXIMATION_SAMPLES = 15


def face_boundary_entities(plane: dict, face: Any, ellipses: Sequence[dict] = ()) -> List[dict]:
    """面の境界(外周と穴)を、スケッチ図形 `{"kind", "params"}` の並びにする(辺ごとの直し方は `edge_entities`)。

    `ellipses` は元の領域にあった楕円の図形の params。1周のループの辺がすべて同じ楕円に乗っていれば、ループを
    その楕円1つにする(楕円を R で広げて R で縮めると、元の楕円がずらし曲線の4辺になって返る)。
    """
    ax3 = _brep_planar._ax3_from_plane(plane)
    entities: List[dict] = []
    wire_map = _brep_topology.indexed_map(face, TopAbs_ShapeEnum.TopAbs_WIRE)
    for w in range(1, wire_map.Extent() + 1):
        edge_map = _brep_topology.indexed_map(wire_map.FindKey(w), TopAbs_ShapeEnum.TopAbs_EDGE)
        edges = [TopoDS.Edge_s(edge_map.FindKey(i)) for i in range(1, edge_map.Extent() + 1)]
        matched = next((params for params in ellipses if all(_edge_on_ellipse(ax3, e, params) for e in edges)), None)
        if matched is not None:
            entities.append({"kind": "ellipse", "params": dict(matched)})
            continue
        for edge in edges:
            entities.extend(edge_entities(ax3, edge))
    return entities


def edge_entities(ax3: "gp_Ax3", edge: Any) -> List[dict]:
    """平面 `ax3` の上にある辺1本を、スケッチ図形 `{"kind", "params"}` の並びにする(点に縮んだ辺は空)。

    線分・円/円弧・閉じた楕円・3次以下の多項式のベジエ/B-スプライン・直線や円に乗るオフセット曲線は**正確な**図形にする。
    それ以外(楕円弧・楕円のオフセット・有理 B-スプラインなど)は3次ベジエの列で近似し、その図形に `"approximated": True` を
    付ける(誤差は `APPROXIMATION_TOLERANCE` 以下)。ポリラインにはしない(押し出した側面が線分の数だけ平面に割れる。
    docs/DESIGN_DECISIONS.md)。
    """
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    kind = curve.GetType()

    def uv(parameter: float) -> Tuple[float, float]:
        point = curve.Value(parameter)
        return _brep_planar._to_uv(ax3, point.X(), point.Y(), point.Z())

    if kind == GeomAbs_CurveType.GeomAbs_Line:
        (x0, y0), (x1, y1) = uv(first), uv(last)
        if math.hypot(x1 - x0, y1 - y0) <= _brep_planar._ENVELOPE_TOLERANCE:
            return []
        return [{"kind": "line", "params": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]
    if kind == GeomAbs_CurveType.GeomAbs_Circle:
        circle = curve.Circle()
        centre = circle.Location()
        cx, cy = _brep_planar._to_uv(ax3, centre.X(), centre.Y(), centre.Z())
        return [_circular_entity(uv, first, last, cx, cy, circle.Radius())]
    if kind == GeomAbs_CurveType.GeomAbs_Ellipse and _closed_edge(uv, first, last):
        return [{"kind": "ellipse", "params": _ellipse_params(ax3, curve.Ellipse())}]
    if kind == GeomAbs_CurveType.GeomAbs_OffsetCurve:
        fitted = _entities_of_offset_curve(uv, first, last)
        if fitted is not None:
            return fitted
    if kind in (GeomAbs_CurveType.GeomAbs_BezierCurve, GeomAbs_CurveType.GeomAbs_BSplineCurve):
        exact = _polynomial_beziers(ax3, curve, kind, first, last)
        if exact is not None:
            return exact
    return _approximated_beziers(ax3, curve, first, last)


def _bezier_entity(points: Sequence[Tuple[float, float]], approximated: bool = False) -> dict:
    """制御点4つ(始点・制御点1・制御点2・終点)のスケッチ図形。"""
    params = {}
    for (key_x, key_y), (x, y) in zip(BEZIER_POINT_KEYS, points):
        params[key_x], params[key_y] = x, y
    entity: dict = {"kind": "bezier", "params": params}
    if approximated:
        entity["approximated"] = True
    return entity


def _polynomial_beziers(ax3: "gp_Ax3", curve: Any, kind: Any, first: float, last: float) -> Any:
    """3次以下の非有理ベジエ/B-スプラインの辺 `[first, last]` を、3次ベジエの図形へ**正確に**分ける。直せなければ None。"""
    if kind == GeomAbs_CurveType.GeomAbs_BezierCurve:
        # 写しを切る: `BRepAdaptor_Curve.Bezier()` は位置が恒等なら辺の曲線そのものを返し、`Segment` は辺の形を書き換える(実測)
        bezier = curve.Bezier().Copy()
        if bezier.IsRational() or bezier.Degree() > 3:
            return None
        if first > bezier.FirstParameter() or last < bezier.LastParameter():
            bezier.Segment(first, last)
        arcs = [bezier]
    else:
        bspline = curve.BSpline()
        if bspline.IsRational() or bspline.Degree() > 3:
            return None
        converter = GeomConvert_BSplineCurveToBezierCurve(bspline, first, last, 1.0e-12)
        arcs = [converter.Arc(index) for index in range(1, converter.NbArcs() + 1)]
    entities: List[dict] = []
    for arc in arcs:
        if arc.Degree() < 3:
            arc.Increase(3)
        poles = [arc.Pole(index) for index in range(1, 5)]
        points = [_brep_planar._to_uv(ax3, pole.X(), pole.Y(), pole.Z()) for pole in poles]
        if max(math.dist(points[0], point) for point in points) > _brep_planar._ENVELOPE_TOLERANCE:
            entities.append(_bezier_entity(points))
    return entities


#: 辺が元の楕円の上にあるとみなす距離[mm]。楕円を R で広げて R で縮めて戻った辺の実測の残差より十分大きく、
#: 近似の誤差の上限(`APPROXIMATION_TOLERANCE`)より十分小さい。
_ELLIPSE_MATCH_TOLERANCE = 1.0e-6


def _edge_on_ellipse(ax3: "gp_Ax3", edge: Any, params: dict) -> bool:
    """辺の標本点がすべて楕円 `params` の上にあるか。

    距離は、楕円の枠で同じ角の位置にある楕円上の点までの長さで測る(楕円上の点は 0、外れた点は真の最短距離以上)。
    """
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    cx, cy = float(params["cx"]), float(params["cy"])
    a, b = float(params["width"]) / 2.0, float(params["height"]) / 2.0
    if a <= 0.0 or b <= 0.0:
        return False
    angle = math.radians(float(params["angle"]))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    for k in range(_OFFSET_FIT_SAMPLES * 4 + 1):
        point = curve.Value(first + (last - first) * k / (_OFFSET_FIT_SAMPLES * 4))
        x, y = _brep_planar._to_uv(ax3, point.X(), point.Y(), point.Z())
        local_x = (x - cx) * cos_a + (y - cy) * sin_a
        local_y = -(x - cx) * sin_a + (y - cy) * cos_a
        theta = math.atan2(local_y / b, local_x / a)
        if math.hypot(local_x - a * math.cos(theta), local_y - b * math.sin(theta)) > _ELLIPSE_MATCH_TOLERANCE:
            return False
    return True


def _approximated_beziers(ax3: "gp_Ax3", curve: Any, first: float, last: float) -> List[dict]:
    """辺 `[first, last]` を3次ベジエの列で近似する(つなぎ目で接線が連続する)。

    各区間は3次エルミート(端点と、区間の内側から取った端点の1階微分の 区間長/3 を制御点に)。区間の内側の標本で、ベジエの点と
    同じ割合のパラメータにある曲線の点の距離を測り、`APPROXIMATION_TOLERANCE` を超えたら区間を二分する。標本は、区間の中にある
    曲線のノット(B-スプラインのノット、オフセット曲線なら元の曲線のノット)で区切った小区間ごとの等間隔の点(ノットの区間の中の
    細い山を飛び越えないため。ノットで区間そのものを割ると、ノットの多い滑らかな曲線が区間の上限に当たる)。
    誤差の上限は標本の点での値で、標本の間の保証ではない。
    OCCT の `GeomConvert_ApproxCurve` は使わない: 3次で接線を連続にする(C1)と区間の数はこれと同じで、C2 を頼むと黙って
    5次にする(2026-10-22 実測。20×10 の楕円を包んだ辺)。
    """
    def uv(parameter: float) -> Tuple[float, float]:
        point = curve.Value(parameter)
        return _brep_planar._to_uv(ax3, point.X(), point.Y(), point.Z())

    if max(math.dist(uv(first), uv(first + (last - first) * k / 8)) for k in range(1, 9)) <= _brep_planar._ENVELOPE_TOLERANCE:
        return []
    xdir, ydir = gp_Vec(ax3.XDirection()), gp_Vec(ax3.YDirection())

    def point_and_tangent(parameter: float, inside: float) -> Tuple[Tuple[float, float], Tuple[float, float]]:
        # 微分は区間の内側へ `inside` だけ寄せて取る: ノットの上では片側の微分しか返らず、G1 だが C1 でない継ぎ目(有理の
        # B-スプラインの円など)で区間の終わりの接線の大きさが食い違い、二分しても誤差が縮まない。
        vector = gp_Vec()
        curve.D1(parameter + inside, gp_Pnt(), vector)
        point = curve.Value(parameter)
        return _brep_planar._to_uv(ax3, point.X(), point.Y(), point.Z()), (vector.Dot(xdir), vector.Dot(ydir))

    entities: List[dict] = []
    knots = _brep_planar._smooth_breaks(curve, first, last)[1:-1]
    pending = [(first, last)]
    while pending:
        if len(entities) + len(pending) > _MAX_APPROXIMATION_SEGMENTS:
            raise ValueError(
                f"辺を3次ベジエで近似できない({_MAX_APPROXIMATION_SEGMENTS} 区間に割っても誤差が "
                f"{APPROXIMATION_TOLERANCE} mm に収まらない。曲線が尖っている)"
            )
        t0, t1 = pending.pop()
        nudge = (t1 - t0) * 1.0e-9
        (p0, d0), (p3, d3) = point_and_tangent(t0, nudge), point_and_tangent(t1, -nudge)
        third = (t1 - t0) / 3.0
        points = [p0, (p0[0] + d0[0] * third, p0[1] + d0[1] * third), (p3[0] - d3[0] * third, p3[1] - d3[1] * third), p3]
        inside = knots[bisect.bisect_right(knots, t0) : bisect.bisect_left(knots, t1)]
        if _arc_error(points, uv, t0, t1, inside) <= APPROXIMATION_TOLERANCE:
            entities.append(_bezier_entity(points, approximated=True))
        else:
            middle = (t0 + t1) / 2.0
            pending.extend([(middle, t1), (t0, middle)])
    return entities


def _arc_error(
    points: Sequence[Tuple[float, float]],
    uv: Callable[[float], Tuple[float, float]],
    t0: float,
    t1: float,
    knots: Sequence[float] = (),
) -> float:
    """ベジエ `points` と、曲線の `[t0, t1]` を同じ割合のパラメータで対応させたときの距離の最大。標本は、区間の中のノット
    `knots`(昇順)で区切った小区間ごとの内側の等間隔の点と、ノットそのもの(ノットの区間の中の細い山を見落とさない)。"""
    marks = [t0, *knots, t1]
    shares = [(knot - t0) / (t1 - t0) for knot in knots]
    for a, b in zip(marks, marks[1:]):
        shares += [((a + (b - a) * k / (_APPROXIMATION_SAMPLES + 1)) - t0) / (t1 - t0) for k in range(1, _APPROXIMATION_SAMPLES + 1)]
    worst = 0.0
    for s in shares:
        r = 1.0 - s
        weights = (r * r * r, 3.0 * r * r * s, 3.0 * r * s * s, s * s * s)
        bx = sum(w * p[0] for w, p in zip(weights, points))
        by = sum(w * p[1] for w, p in zip(weights, points))
        worst = max(worst, math.dist((bx, by), uv(t0 + (t1 - t0) * s)))
    return worst


def _circular_entity(
    uv: Callable[[float], Tuple[float, float]], first: float, last: float, cx: float, cy: float, radius: float
) -> dict:
    """中心と半径の円の、辺 `[first, last]` にあたる円弧(閉じた辺は円)のスケッチ図形。"""
    (sx, sy), (ex, ey) = uv(first), uv(last)
    if math.hypot(ex - sx, ey - sy) <= _brep_planar._ENVELOPE_TOLERANCE:
        return {"kind": "circle", "params": {"cx": cx, "cy": cy, "diameter": 2.0 * radius}}
    mx, my = uv((first + last) / 2.0)
    start = math.degrees(math.atan2(sy - cy, sx - cx))
    end = math.degrees(math.atan2(ey - cy, ex - cx))
    middle = math.degrees(math.atan2(my - cy, mx - cx))
    # 辺の向きと円の軸の向きは独立なので、始点→中点→終点の回り方から反時計回りの端点を決める。
    if (middle - start) % 360.0 > (end - start) % 360.0:
        start, end = end, start
    return {
        "kind": "arc",
        "params": {"cx": cx, "cy": cy, "radius": radius, "start_deg": start, "end_deg": end},
    }


#: オフセット曲線の辺を線分・円に直すとき、標本点が直線・円に乗っているとみなす距離[mm]。
#: 実測の残差は 1e-10 以下(基礎曲線が円を B-スプラインに直したもの)。
_OFFSET_FIT_TOLERANCE = 1.0e-8
_OFFSET_FIT_SAMPLES = 9


def _entities_of_offset_curve(uv: Callable[[float], Tuple[float, float]], first: float, last: float) -> Any:
    """`GeomAbs_OffsetCurve` の辺を、線分・円弧・円のスケッチ図形にする(点にまで縮んだ辺は図形なし)。直線にも円にも
    乗らなければ None(呼び出し側が近似する)。

    外部の罠: 円や直線を B-スプラインに直した辺(ブーリアンやオフセットの途中で出る)を、さらにオフセットすると、
    OCCT は辺を元の円・直線の型に戻さず `GeomAbs_OffsetCurve` のまま返す(2026-10-18 実測: 円と長方形だけの領域を
    R=0.5, d=-0.5 で包むと、円の頂点まわりの長さ 0.01〜0.1 mm の2辺が出た)。曲線そのものは元の円と同心の円で、
    標本点の円への残差は 1e-10 以下。標本点を直線・円へ当てはめ、残差が `_OFFSET_FIT_TOLERANCE` を超えるなら直さない。
    """
    params = [first + (last - first) * k / (_OFFSET_FIT_SAMPLES - 1) for k in range(_OFFSET_FIT_SAMPLES)]
    points = [uv(parameter) for parameter in params]
    start, middle, end = points[0], points[len(points) // 2], points[-1]
    extent = max(math.dist(start, point) for point in points)
    if extent <= _brep_planar._ENVELOPE_TOLERANCE:
        return []

    chord = math.dist(start, end)
    if chord > _brep_planar._ENVELOPE_TOLERANCE:
        along = ((end[0] - start[0]) / chord, (end[1] - start[1]) / chord)
        if all(abs((p[0] - start[0]) * along[1] - (p[1] - start[1]) * along[0]) <= _OFFSET_FIT_TOLERANCE for p in points):
            return [{"kind": "line", "params": {"x0": start[0], "y0": start[1], "x1": end[0], "y1": end[1]}}]

    fitted = _circle_through(start, middle, end)
    if fitted is not None:
        cx, cy, radius = fitted
        if all(abs(math.hypot(p[0] - cx, p[1] - cy) - radius) <= _OFFSET_FIT_TOLERANCE for p in points):
            return [_circular_entity(uv, first, last, cx, cy, radius)]
    return None


def _circle_through(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> Any:
    """3点を通る円 `(cx, cy, radius)`。共線なら None。

    最初の点を原点とする局所座標で解く。絶対座標の二乗を差し引く解き方だと、図形が原点から離れる(1000 mm など)と
    0.01〜0.1 mm の短い弧で桁が落ち、当てはめを検査する残差の許容を超えて包み込みを断ってしまう。
    """
    bx, by = b[0] - a[0], b[1] - a[1]
    px, py = c[0] - a[0], c[1] - a[1]
    d = 2.0 * (bx * py - by * px)
    if d == 0.0:
        return None
    sb, sc = bx * bx + by * by, px * px + py * py
    ux = (py * sb - by * sc) / d
    uy = (bx * sc - px * sb) / d
    return a[0] + ux, a[1] + uy, math.hypot(ux, uy)


def _ax3_from_face(face: Any) -> "gp_Ax3":
    """平面の面から gp_Ax3 を取る。平面でなければ ValueError。法線は面の外向きにそろえる。"""
    face = TopoDS.Face_s(face)
    adaptor = BRepAdaptor_Surface(face)
    if adaptor.GetType() != GeomAbs_SurfaceType.GeomAbs_Plane:
        raise ValueError(
            f"face is {_brep_geometry.classify_face(face)}: スケッチ平面にできるのは平面だけ"
        )
    # OCP には gp_Ax3(gp_Ax3) のコンストラクタが無いので位置・法線・X方向から組み直す。
    # 外向きと逆なら `ZReverse`(`ReverseZ` は存在しない)で法線だけを裏返す(X/Y は変わらない)。
    position = adaptor.Plane().Position()
    ax3 = gp_Ax3(position.Location(), position.Direction(), position.XDirection())
    # 外部の罠: 向きが REVERSED かどうかで反転を決めてはいけない。奇数回鏡映したボディの面は平面の Position が
    # 左手系で、REVERSED のときに Direction がすでに外を向いている(実測: 鏡映した直方体の6面すべてで法線が逆になった)。
    # `BRepGProp_Face` の外向き法線(`brep_geometry._face_normal`)と突き合わせる。
    if _brep_planar._dot3(ax3.Direction(), _brep_geometry._face_normal(face)) < 0.0:
        ax3.ZReverse()
    return ax3


def _edges_point(ax3: "gp_Ax3", params: dict) -> List[Any]:
    """点の図形は辺を持たない(領域を割らない)。"""
    return []


def _edges_line(ax3: "gp_Ax3", params: dict) -> List[Any]:
    p0 = _brep_planar._to_pnt(ax3, float(params["x0"]), float(params["y0"]))
    p1 = _brep_planar._to_pnt(ax3, float(params["x1"]), float(params["y1"]))
    maker = BRepBuilderAPI_MakeEdge(p0, p1)
    if not maker.IsDone():
        raise ValueError("failed to build a line edge")
    return [maker.Edge()]


def _curve_axis(ax3: "gp_Ax3", center: "gp_Pnt", angle_deg: float = 0.0) -> "gp_Ax2":
    """円・円弧・楕円の軸。第1軸はスケッチの X から `angle_deg` 度、パラメータが増える向きはスケッチの反時計回り。

    外部の罠: 面に貼ったスケッチの `ax3` は左手系でありうる(`ZReverse` は X/Y を据え置く。`_ax3_from_frame`)。
    `ax3.Direction()` を法線に使うと、`gp_Ax2` が Y を N×X で作り直すのでスケッチの Y と逆になり、
    円弧は時計回りに・楕円は角度が逆向きに出る(2026-10-17 実測: 立方体6面のうち5面で四分円の領域が閉じない)。
    法線はスケッチの X×Y から作る。
    """
    radians = math.radians(angle_deg)
    x_dir = ax3.XDirection()
    y_dir = ax3.YDirection()
    first = gp_Dir(
        math.cos(radians) * x_dir.X() + math.sin(radians) * y_dir.X(),
        math.cos(radians) * x_dir.Y() + math.sin(radians) * y_dir.Y(),
        math.cos(radians) * x_dir.Z() + math.sin(radians) * y_dir.Z(),
    )
    return gp_Ax2(center, x_dir.Crossed(y_dir), first)


def _edges_circle(ax3: "gp_Ax3", params: dict) -> List[Any]:
    center = _brep_planar._to_pnt(ax3, float(params["cx"]), float(params["cy"]))
    diameter = float(params["diameter"])
    if diameter <= 0:
        raise ValueError(f"circle diameter must be positive, got {diameter}")
    maker = BRepBuilderAPI_MakeEdge(gp_Circ(_curve_axis(ax3, center), diameter / 2.0))
    if not maker.IsDone():
        raise ValueError("failed to build a circle edge")
    return [maker.Edge()]


def _closed_edge(uv: Callable[[float], Tuple[float, float]], first: float, last: float) -> bool:
    (sx, sy), (ex, ey) = uv(first), uv(last)
    return math.hypot(ex - sx, ey - sy) <= _brep_planar._ENVELOPE_TOLERANCE


def _ellipse_params(ax3: "gp_Ax3", ellipse: "gp_Elips") -> dict:
    """OCCT の楕円を `ellipse` の params へ(長軸を `width` にする)。"""
    centre = ellipse.Location()
    cx, cy = _brep_planar._to_uv(ax3, centre.X(), centre.Y(), centre.Z())
    major = ellipse.XAxis().Direction()
    du = major.Dot(ax3.XDirection())
    dv = major.Dot(ax3.YDirection())
    return {
        "cx": cx,
        "cy": cy,
        "width": 2.0 * ellipse.MajorRadius(),
        "height": 2.0 * ellipse.MinorRadius(),
        "angle": math.degrees(math.atan2(dv, du)),
    }


#: 長半径と短半径の差がこれ以下の楕円は、辺を円にする[mm]。OCCT の `Precision::Confusion`(1e-7)の10倍。
_CIRCULAR_ELLIPSE_TOLERANCE = 1.0e-6


def _ellipse_gp(ax3: "gp_Ax3", params: dict) -> Any:
    """`ellipse` の params を OCCT の楕円(`gp_Elips`)へ。縦横がほぼ等しいときは円(`gp_Circ`)。

    外部の罠: `gp_Elips` は長半径 >= 短半径でないと `Standard_ConstructionError` になる
    (2026-10-17 実測: (5,10) で失敗、(5,5) は通る)。`height` の方が長いときは2つを入れ替え、軸を 90° 回して渡す。
    離心率0の `gp_Elips`(長半径と短半径の差が 5e-8 以下)の辺は、`BRepOffsetAPI_MakeOffset` に渡すと
    例外にならず access violation でプロセスごと落ちる(差が 1e-7 以上なら ValueError)。
    縦横が等しい楕円は docs/ELLIPSE.md が「真円になるだけ」と許すので、辺を円にする。
    """
    center = _brep_planar._to_pnt(ax3, float(params["cx"]), float(params["cy"]))
    semi_width = float(params["width"]) / 2.0
    semi_height = float(params["height"]) / 2.0
    if semi_width <= 0 or semi_height <= 0:
        raise ValueError(f"ellipse width/height must be positive, got {params['width']} x {params['height']}")
    if abs(semi_width - semi_height) <= _CIRCULAR_ELLIPSE_TOLERANCE:
        return gp_Circ(_curve_axis(ax3, center), (semi_width + semi_height) / 2.0)
    angle_deg = float(params["angle"])
    if semi_height > semi_width:
        semi_width, semi_height = semi_height, semi_width
        angle_deg += 90.0
    return gp_Elips(_curve_axis(ax3, center, angle_deg), semi_width, semi_height)


def _edges_ellipse(ax3: "gp_Ax3", params: dict) -> List[Any]:
    maker = BRepBuilderAPI_MakeEdge(_ellipse_gp(ax3, params))
    if not maker.IsDone():
        raise ValueError("failed to build an ellipse edge")
    return [maker.Edge()]


def ellipse_perimeter(params: dict) -> float:
    """楕円の周長[mm]。閉じた式が無いので OCCT に辺の長さを測らせる(許容差の罠は `brep_geometry.edge_length`)。"""
    return _brep_geometry.edge_length(_edges_ellipse(gp_Ax3(), params)[0])


def _edges_arc(ax3: "gp_Ax3", params: dict) -> List[Any]:
    center = _brep_planar._to_pnt(ax3, float(params["cx"]), float(params["cy"]))
    radius = float(params["radius"])
    if radius <= 0:
        raise ValueError(f"arc radius must be positive, got {radius}")
    start_deg = float(params["start_deg"])
    end_deg = float(params["end_deg"])
    sweep_deg = (end_deg - start_deg) % 360.0
    start_rad = math.radians(start_deg)
    end_rad = start_rad + math.radians(sweep_deg)
    maker = BRepBuilderAPI_MakeEdge(gp_Circ(_curve_axis(ax3, center), radius), start_rad, end_rad)
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
            index, _brep_planar._to_pnt(ax3, float(params[key_x]), float(params[key_y]))
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
    corners = [_brep_planar._to_pnt(ax3, u, v) for u, v in rectangle_corners(params)]
    edges = []
    for index in range(4):
        maker = BRepBuilderAPI_MakeEdge(corners[index], corners[(index + 1) % 4])
        if not maker.IsDone():
            raise ValueError("failed to build a rectangle edge")
        edges.append(maker.Edge())
    return edges


def _bbox_point(params: dict) -> Tuple[float, float, float, float]:
    cx, cy = float(params["cx"]), float(params["cy"])
    return (cx, cx, cy, cy)


def _bbox_line(params: dict) -> Tuple[float, float, float, float]:
    x0, y0, x1, y1 = float(params["x0"]), float(params["y0"]), float(params["x1"]), float(params["y1"])
    return (min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1))


def _bbox_around(cx: float, cy: float, half_u: float, half_v: float) -> Tuple[float, float, float, float]:
    return (cx - half_u, cx + half_u, cy - half_v, cy + half_v)


def _bbox_circle(params: dict) -> Tuple[float, float, float, float]:
    radius = float(params["diameter"]) / 2.0
    return _bbox_around(float(params["cx"]), float(params["cy"]), radius, radius)


def _bbox_ellipse(params: dict) -> Tuple[float, float, float, float]:
    a = float(params["width"]) / 2.0
    b = float(params["height"]) / 2.0
    radians = math.radians(float(params["angle"]))
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    return _bbox_around(
        float(params["cx"]),
        float(params["cy"]),
        math.hypot(a * cos_a, b * sin_a),
        math.hypot(a * sin_a, b * cos_a),
    )


def _bbox_arc(params: dict) -> Tuple[float, float, float, float]:
    # 安全側: 円全体の外接箱で囲む（補助面が少し大きくなるだけ）。
    radius = float(params["radius"])
    return _bbox_around(float(params["cx"]), float(params["cy"]), radius, radius)


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
    1本化できない(docs/DEVELOPMENT.md「モジュールとimport境界」)。
    """

    edges: Callable[["gp_Ax3", dict], List[Any]]
    bbox: Callable[[dict], Tuple[float, float, float, float]]


_KERNEL_ENTITY_KIND_TABLE: Dict[str, _KernelEntityKindOps] = {
    "point": _KernelEntityKindOps(_edges_point, _bbox_point),
    "line": _KernelEntityKindOps(_edges_line, _bbox_line),
    "circle": _KernelEntityKindOps(_edges_circle, _bbox_circle),
    "ellipse": _KernelEntityKindOps(_edges_ellipse, _bbox_ellipse),
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
    shape_map = _brep_topology.indexed_map(TopoDS.Face_s(face), TopAbs_ShapeEnum.TopAbs_VERTEX)
    if shape_map.Extent() < 1:
        return False
    for index in range(1, shape_map.Extent() + 1):
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(shape_map.FindKey(index)))
        u, v = _brep_planar._to_uv(ax3, point.X(), point.Y(), point.Z())
        if not (
            u_min - tol <= u <= u_max + tol and v_min - tol <= v <= v_max + tol
        ):
            return False
    return True

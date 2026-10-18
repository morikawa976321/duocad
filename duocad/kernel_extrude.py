"""押し出し・回転体・ロフトの幾何(OCPを触るカーネル層)。

面の押し出し(`extrude_faces`)、スケッチの領域の押し出し(`extrude_regions`)・回転体(`revolve_regions`)・
ロフト(`loft_regions`)を持つ。
面の数え方(`_face_map` の1始まりの添字)・ブーリアンの検査(`_checked_volume` / `_require_valid`)・
由来の射影(`_target_successors` / `_face_map_projection`)は `kernel` の側にあり、ここはそれを読むだけ ——
依存は kernel_extrude → kernel の一方向。

戻り値の規律は kernel と同じ(JSON化できる素の値だけ)。例外は `"shape"`(TopoDS_Shape。
document.Body.shape になる値)。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve

from OCP.BRepAlgoAPI import BRepAlgoAPI_Check, BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.Bnd import Bnd_Box
from OCP.GCPnts import GCPnts_AbscissaPoint
from OCP.GProp import GProp_GProps
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_REVERSED, TopAbs_WIRE
from OCP.TopExp import TopExp_Explorer
from OCP.TopTools import TopTools_IndexedMapOfShape  # noqa: F401  型注釈だけ
from OCP.TopoDS import TopoDS, TopoDS_Shape  # noqa: F401  TopoDS_Shape は型注釈だけ
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

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

#: 軸の両側にまたがるかの判定で、少ない側の面積が全体のこの割合を超えたら「横切る」(軸上の辺の共通部分は面積0)。
_AXIS_SIDE_TOL = 1.0e-9


#: 押し出し量の絶対値の下限・上限[mm]。下限は OCCT の `Precision.Confusion`(1e-7)の10倍で、これ以下は
#: `BRepSweep_*` が `Standard_ConstructionError` を投げる(1e-7 以下)か体積0(1.1e-7)になる。上限は 1 km で、
#: 1e300 級は `gp_Dir` が `Standard_ConstructionError` を投げる。
MIN_DISTANCE = 1e-6
MAX_DISTANCE = 1e6


def check_distance(distance: Any) -> float:
    """押し出し量を数にして検査する。0・非有限・下限未満・上限超過は ValueError。

    NaN / ±inf は `BRepPrimAPI_MakePrism` が NaN 座標の形を返し、直後の体積計算 `kernel.volume` が native のスタック
    オーバーフローで **プロセスごと落ちる**(Python の except では捕まらない)。入口の型検査 `catalog.check_arguments`
    が先に断るが、WebSocket の `preview` はそれを通らないので、ここでも断る。
    """
    value = float(distance)
    if not math.isfinite(value):
        raise ValueError(f"distance must be a finite number, got {distance!r}")
    if value == 0.0:
        raise ValueError("distance must not be 0 (押し出し量0は形状を変えない)")
    if not MIN_DISTANCE <= abs(value) <= MAX_DISTANCE:
        raise ValueError(f"|distance| must be between {MIN_DISTANCE} and {MAX_DISTANCE:g} mm, got {distance!r}")
    return value


def extrude_regions(
    faces: Sequence[Any],
    distance: float,
    mode: str,
    normal: Sequence[float],
    target_shape: Any = None,
) -> dict:
    """領域の面群を法線方向へ押し出し、join/cut/new した形状を返す(束ね方は `_combine_region_solids`)。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape", "unified": bool, "volume_before": float | None, "volume_after": float,
         "provenance": {"verb": "extrude", "mode": str, "distance": float, "unified": bool,
                        "face_map": {"<旧添字>": [新添字, ...]}, "deleted": [...], "new_faces": [...]}}
    `face_map` の旧添字は `target_shape` の面(相手が無ければ空)。
    """
    if not faces:
        raise ValueError("extrude_regions requires at least one face")
    distance = check_distance(distance)
    _check_region_mode(mode, target_shape)

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

    return _combine_region_solids(
        prisms, mode, target_shape, {"verb": "extrude", "mode": mode, "distance": distance}
    )


def revolve_regions(
    faces: Sequence[Any],
    axis_point: Sequence[float],
    axis_dir: Sequence[float],
    normal: Sequence[float],
    angle: float,
    mode: str,
    target_shape: Any = None,
    symmetric: bool = False,
) -> dict:
    """領域の面群を、スケッチ平面上の軸(`axis_point` を通り `axis_dir` 向き)のまわりに `angle` 度回し、
    join/cut/new した形状を返す(束ね方は `_combine_region_solids`)。

    `angle` の正は `axis_dir` まわりの右ねじの向き、負はその逆。`0 < |angle| <= 360`。`symmetric` なら
    スケッチ平面から両側へ `|angle|/2` ずつ回す(符号は効かない)。`normal` はスケッチ平面の法線で、
    軸が領域を横切るかの判定にだけ使う。

    戻り値は `extrude_regions` と同じ形。provenance の `verb` は `"revolve"`、`distance` の代わりに `angle`。
    """
    if not faces:
        raise ValueError("revolve_regions requires at least one face")
    angle = float(angle)
    if not math.isfinite(angle) or angle == 0.0 or abs(angle) > 360.0:
        raise ValueError(f"angle must satisfy 0 < |angle| <= 360 (got {angle})")
    _check_region_mode(mode, target_shape)

    direction = gp_Dir(*(float(value) for value in axis_dir))
    origin = gp_Pnt(*(float(value) for value in axis_point))
    axis = gp_Ax1(origin, direction)
    plane_normal = gp_Dir(*(float(value) for value in normal))
    if abs(direction.Dot(plane_normal)) > Precision.Angular_s():
        raise ValueError("revolve axis must lie in the sketch plane")

    sweep = math.radians(abs(angle) if symmetric else angle)
    full = abs(angle) == 360.0
    solids: List[TopoDS_Shape] = []
    for position, face in enumerate(faces, start=1):
        profile = TopoDS.Face_s(face)
        _require_one_side_of_axis(profile, origin, direction, plane_normal, position)
        if symmetric and not full:
            turn = gp_Trsf()
            turn.SetRotation(axis, -sweep / 2.0)
            profile = TopoDS.Face_s(BRepBuilderAPI_Transform(profile, turn, True).Shape())
        # 360° は角度なしの形で作る(角度 2π を渡すと始端と終端の面が重なった閉じていない形になりうる)。
        maker = BRepPrimAPI_MakeRevol(profile, axis) if full else BRepPrimAPI_MakeRevol(profile, axis, sweep)
        if not maker.IsDone():
            raise ValueError(f"failed to revolve region #{position}")
        solid = maker.Shape()
        if solid is None or solid.IsNull() or volume(solid) <= 0:
            raise ValueError(f"revolving region #{position} produced no volume")
        solids.append(solid)

    return _combine_region_solids(
        solids,
        mode,
        target_shape,
        {"verb": "revolve", "mode": mode, "angle": angle, "symmetric": bool(symmetric)},
    )


def _require_one_side_of_axis(
    face: Any, origin: "gp_Pnt", direction: "gp_Dir", normal: "gp_Dir", position: int
) -> None:
    """面が軸の片側だけにあることを確かめる(軸上の辺・点で接するのは可)。横切れば ValueError。

    軸の両側に半平面を置き、面との共通部分の面積で決める。境界を折れ線で拾う方法は、軸をまたいで
    わずかに膨らむ円弧を取りこぼす。横切った面の回転体は自己交差し、`IsDone()` も体積検査も通りうる。
    """
    total, _ = _face_props(face)
    box = Bnd_Box()
    BRepBndLib.Add_s(face, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    reach = 10.0 * (
        math.dist((xmin, ymin, zmin), (xmax, ymax, zmax))
        + math.dist((origin.X(), origin.Y(), origin.Z()), (xmin, ymin, zmin))
        + 1.0
    )
    along = gp_Vec(direction).Multiplied(reach)
    across = gp_Vec(normal).Crossed(gp_Vec(direction)).Multiplied(reach)
    sides = []
    for sign in (1.0, -1.0):
        corners = [
            origin.Translated(along.Reversed()),
            origin.Translated(along),
            origin.Translated(along).Translated(across.Multiplied(sign)),
            origin.Translated(along.Reversed()).Translated(across.Multiplied(sign)),
        ]
        polygon = BRepBuilderAPI_MakePolygon(*corners, True)
        half = BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()
        common = BRepAlgoAPI_Common(face, half)
        common.Build()
        if not common.IsDone():
            raise ValueError(f"failed to test region #{position} against the revolve axis")
        sides.append(_shape_area(common.Shape()))
    if min(sides) > _AXIS_SIDE_TOL * total:
        raise ValueError(
            f"revolve axis crosses region #{position}: 軸の両側にまたがる領域は回せない"
            "(回転体が自己交差する)。軸の片側の領域だけを選ぶか、軸を領域の外か辺の上へ置くこと"
        )


def _shape_area(shape: Any) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return props.Mass()


def _check_region_mode(mode: str, target_shape: Any) -> None:
    """領域の押し出し・回転体に共通する mode と相手の組の検査。"""
    if mode not in ("join", "cut", "new"):
        raise ValueError(f"unknown mode: {mode!r} (one of ['join', 'cut', 'new'])")
    if mode == "cut" and (target_shape is None or target_shape.IsNull()):
        raise ValueError("cut requires a target shape")
    if mode == "new" and target_shape is not None and not target_shape.IsNull():
        # 相手を黙って無視すると「足したつもりが別ボディ」が成功として通る。
        raise ValueError("new must not take a target shape (新規ボディに行き先は無い)")


def _combine_region_solids(
    solids: Sequence[TopoDS_Shape], mode: str, target_shape: Any, head: dict
) -> dict:
    """領域から作った立体(柱・回転体)を束ね、相手へ join / cut する(new は束ねたものを返す)。

    同一平面の面の統合(`_unify_same_domain`)は、join / new で立体を束ねたとき(複数か、相手への join)にだけ掛ける。
    戻り値は `extrude_regions` の docstring の形。`head` は provenance の先頭に置く(`verb` ほか)。
    """
    combined = _fuse_all(solids, "region solids")

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
    if mode in ("join", "new") and (len(solids) > 1 or builder is not None):
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
            **head,
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
    distance = check_distance(distance)
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

# ======================================================================================
# ロフト
# ======================================================================================
# 別々の平面にある断面を順につなぐ立体(`loft_regions`)。
#
# `BRepOffsetAPI_ThruSections` の自動の揃え(`CheckCompatibility`)には任せない。断面の向き・始点・辺の数を
# ここで揃えてから渡す(任せたときに壊れる条件の実測は docs/CAD_LIBRARY_PITFALLS.md「ロフト」)。
#
# - 向き: 各断面の法線を進む向き(隣の断面・点へ)に合わせ、外周の線をその法線について反時計回りにする
# - 始点: 前の断面の始点の向きを、前の法線 → この法線の最小回転で運び、それに最も近い向きの点を始点にする
# - 辺の数: 全断面を、どれかの断面の頂点がある向き(始点からの極角)すべてで割る。極角が線に沿って
#   単調でない断面(重心から見て裏に回り込む凹み)があれば、長さの割合で割る
# - `shifts`: 揃えた後の始点を断面ごとに頂点 k 個ぶん進める(負は戻す。頂点の無い閉曲線は 90° 単位)

Vec = Tuple[float, float, float]

#: 極角の比較(度)。これより近い向きは同じ頂点とみなす。
_ANGLE_TOL = 1.0e-4

#: 線に沿った極角を測る刻み(辺1本あたり)。始点の探索・単調性の判定・割る位置の二分法の初期区間に使う。
_SAMPLES_PER_EDGE = 64

#: 2つの平面を「同じ平面」とみなす距離(mm)。
_PLANE_TOL = 1.0e-7


def loft_regions(
    sections: Sequence[Tuple[Any, Sequence[float]]],
    mode: str,
    target_shape: Any = None,
    start_point: Optional[Sequence[float]] = None,
    end_point: Optional[Sequence[float]] = None,
    shifts: Optional[Sequence[int]] = None,
) -> dict:
    """断面(`(面, スケッチ平面の法線)` の並び)を順につないだ立体を作り、join/cut/new した形状を返す。

    `start_point` / `end_point` を渡すと、最初の断面の前・最後の断面の後をその点へ収束させる(尖らせる)。
    断面1つでも点が1つあればよい。穴のある面は拒む。`shifts` は断面ごとの整数(省略は全部0)。

    戻り値は `kernel_extrude.extrude_regions` と同じ形。provenance の `verb` は `"loft"`。
    """
    faces = [TopoDS.Face_s(face) for face, _ in sections]
    normals = [_unit(tuple(float(v) for v in normal)) for _, normal in sections]
    start = None if start_point is None else tuple(float(v) for v in start_point)
    end = None if end_point is None else tuple(float(v) for v in end_point)
    if not faces:
        raise ValueError("loft requires at least one section")
    if len(faces) + (start is not None) + (end is not None) < 2:
        raise ValueError("loft requires at least two sections, or one section and a point")
    shifts = [0] * len(faces) if shifts is None else [int(k) for k in shifts]
    if len(shifts) != len(faces):
        raise ValueError(f"shifts must have one entry per section ({len(faces)}), got {len(shifts)}")
    _check_region_mode(mode, target_shape)

    wires = []
    centroids = []
    for position, face in enumerate(faces, start=1):
        if _wire_count(face) > 1:
            raise ValueError(
                f"loft section #{position} has a hole: 穴のある領域は断面にできない"
                "(外周をロフトしてから、内側の領域を cut でロフトする)"
            )
        wires.append(BRepTools.OuterWire_s(face))
        centroids.append(_area_centroid(face))
    _require_separate_planes(centroids, normals, start, end)

    oriented = _oriented_normals(centroids, normals, start, end)
    aligned = _aligned_wires(wires, centroids, oriented, shifts)
    failure: Optional[ValueError] = None
    candidates = _matched_candidates(aligned, centroids, oriented)
    for matched, own in candidates:
        try:
            solid = _thru_sections(matched, start, end, own)
            _require_sound_loft(solid, faces)
        except ValueError as error:
            failure = failure or error
            continue
        break
    else:
        if len(candidates) > 1:
            raise ValueError(
                f"{failure} —— 重心から見て裏に回り込む凹みのある断面(U字など)は対応が決まらないことがある。"
                "shifts でその断面の始点をずらすか、断面を凹みの無い形に分けること"
            )
        raise failure

    return _combine_region_solids(
        [solid], mode, target_shape, {"verb": "loft", "mode": mode, "sections": len(faces)}
    )


def _thru_sections(wires: Sequence[Any], start: Optional[Vec], end: Optional[Vec], own: bool) -> Any:
    """断面(と端の点)を `ThruSections` でつなぐ。`own` なら揃えは済んでいる(OCCT の揃えを切る)。"""
    maker = BRepOffsetAPI_ThruSections(True, False, 1.0e-6)
    maker.CheckCompatibility(not own)
    if start is not None:
        maker.AddVertex(BRepBuilderAPI_MakeVertex(gp_Pnt(*start)).Vertex())
    for wire in wires:
        maker.AddWire(wire)
    if end is not None:
        maker.AddVertex(BRepBuilderAPI_MakeVertex(gp_Pnt(*end)).Vertex())
    maker.Build()
    if not maker.IsDone():
        raise ValueError("failed to loft the sections (OCCT ThruSections)")
    return maker.Shape()


def _require_sound_loft(solid: Any, faces: Sequence[Any]) -> None:
    """潰れ・裏返り・自己交差を拒む。

    実測: 始点がずれて潰れた・裏返ったロフトは `BRepCheck_Analyzer` を通す。`BRepAlgoAPI_Check` は第3引数
    `bTestSI`(自己干渉)を True にしたときだけ捕まえる(25〜80 ms)。体積は断面の最大面積^1.5 を目安に比べる。
    """
    if solid is None or solid.IsNull():
        raise ValueError("loft produced an empty shape")
    scale = max(_shape_area(face) for face in faces) ** 1.5
    if not volume(solid) > 1.0e-6 * scale:
        raise ValueError(
            "loft collapsed (体積が0か負): 断面どうしの対応がねじれて潰れている。"
            "shifts で断面の始点をずらすか、断面の間に断面を足すこと"
        )
    if not BRepCheck_Analyzer(solid).IsValid():
        raise ValueError("loft produced an invalid shape (BRepCheck)")
    if not BRepAlgoAPI_Check(solid, True, True).IsValid():
        raise ValueError(
            "loft intersects itself (自己交差): 断面の順番・置き場所か、shifts を見直すこと"
        )


# --------------------------------------------------------------------------------------
# 断面の置き場所
# --------------------------------------------------------------------------------------
def _require_separate_planes(
    centroids: Sequence[Vec], normals: Sequence[Vec], start: Optional[Vec], end: Optional[Vec]
) -> None:
    for i in range(len(centroids) - 1):
        if _same_plane(centroids[i], normals[i], centroids[i + 1], normals[i + 1]):
            raise ValueError(
                f"loft sections #{i + 1} and #{i + 2} lie on the same plane: 隣り合う断面は別の平面に置くこと"
            )
    for point, index, label in ((start, 0, "start_point"), (end, len(centroids) - 1, "end_point")):
        if point is not None and abs(_dot(_sub(point, centroids[index]), normals[index])) <= _PLANE_TOL:
            raise ValueError(f"loft {label} lies on the plane of section #{index + 1}: 点は断面の平面の外に置くこと")


def _same_plane(c1: Vec, n1: Vec, c2: Vec, n2: Vec) -> bool:
    if _length(_cross(n1, n2)) > 1.0e-9:
        return False
    return abs(_dot(_sub(c2, c1), n1)) <= _PLANE_TOL


def _oriented_normals(
    centroids: Sequence[Vec], normals: Sequence[Vec], start: Optional[Vec], end: Optional[Vec]
) -> List[Vec]:
    """各断面の法線を進む向き(次の断面・終点へ。最後の断面は前から来る向き)にそろえる。

    進む向きが平面に寝ていて決まらないときは、前の断面の法線と同じ側を取る。
    """
    count = len(centroids)
    out: List[Vec] = []
    for i in range(count):
        if i + 1 < count:
            travel = _sub(centroids[i + 1], centroids[i])
        elif end is not None:
            travel = _sub(end, centroids[i])
        elif i > 0:
            travel = _sub(centroids[i], centroids[i - 1])
        else:
            travel = _sub(centroids[i], start)
        side = _dot(normals[i], _unit(travel)) if _length(travel) > 0.0 else 0.0
        if abs(side) < 1.0e-6:
            side = _dot(normals[i], out[-1]) if out else 1.0
        out.append(normals[i] if side >= 0.0 else _scale(normals[i], -1.0))
    return out


# --------------------------------------------------------------------------------------
# 始点と向き
# --------------------------------------------------------------------------------------
def _aligned_wires(
    wires: Sequence[Any], centroids: Sequence[Vec], normals: Sequence[Vec], shifts: Sequence[int]
) -> List[Any]:
    """向きを法線に合わせ、始点を前の断面から運んだ向きに合わせ、`shifts` だけ進めた線の並び。

    辺の数が全断面で同じなら始点は頂点から選ぶ(角どうしがつながる)。違うなら運んだ向きのちょうどの点で
    辺を割って始点にする(三角形 → 正方形で、角を最寄りの角へ寄せると 45° ねじれる)。
    """
    same_count = len({len(_edges(wire)) for wire in wires}) == 1
    out: List[Any] = []
    reference: Optional[Vec] = None
    for i, wire in enumerate(wires):
        if _dot(_newell(_wire_points(wire)), normals[i]) < 0.0:
            wire = _reversed(wire)
        if i == 0:
            reference = _sub(_start(wire), centroids[0])
        else:
            reference = _rotate_between(reference, normals[i - 1], normals[i])
        wire = _start_toward(wire, centroids[i], normals[i], reference, shifts[i], vertex_only=same_count or i == 0)
        reference = _sub(_start(wire), centroids[i])
        out.append(wire)
    return out


def _start_toward(wire: Any, centre: Vec, normal: Vec, reference: Vec, shift: int, vertex_only: bool) -> Any:
    """`reference` の向き(`centre` から見た極角)に最も近い点を始点にし、そこから `shift` 頂点進めた線。"""
    edges = _edges(wire)
    if len(edges) == 1:
        turned = reference
        for _ in range(shift % 4):
            turned = _cross(normal, turned)
        return _closed_from(edges[0], _nearest_parameter(edges[0], centre, normal, turned))
    starts = [_point_on(edge, _traversal_parameters(edge)[0]) for edge in edges]
    nearest = min(range(len(edges)), key=lambda k: abs(_angle(_sub(starts[k], centre), normal, reference)))
    if not vertex_only and abs(_angle(_sub(starts[nearest], centre), normal, reference)) > _ANGLE_TOL:
        rotated = _split_at_direction(wire, centre, normal, reference)
        if rotated is not None:
            return _rolled(rotated, _shift_from_split(len(rotated), shift))
    return _rolled(edges, (nearest + shift) % len(edges))


def _shift_from_split(split_count: int, shift: int) -> int:
    """割って始点にした点から数えて shift 番目の元の頂点の添字(割った点は頂点に数えない)。

    割った線の辺は [割った点から, 元の頂点1から, 元の頂点2から, ..., 割った点の直前の元の頂点から] の順。
    """
    originals = split_count - 1
    if shift == 0:
        return 0
    if shift > 0:
        return (shift - 1) % originals + 1
    return originals - (-shift - 1) % originals


def _split_at_direction(wire: Any, centre: Vec, normal: Vec, reference: Vec) -> Optional[List[Any]]:
    """`reference` の向きの点で辺を割り、その点から始まる辺の並びを返す。その向きに線が無ければ None。"""
    edges = _edges(wire)
    for index, edge in enumerate(edges):
        params = _traversal_parameters(edge)
        angles = [_angle(_sub(_point_on(edge, t), centre), normal, reference) for t in params]
        for k in range(len(params) - 1):
            a, b = angles[k], angles[k + 1]
            if a <= 0.0 <= b and b - a < 90.0:
                t = _bisect_angle(edge, centre, normal, reference, params[k], params[k + 1], a, b, 0.0)
                head, tail = _split_edge(edge, [t])
                return [tail] + edges[index + 1:] + edges[:index] + [head]
    return None


def _rolled(edges: Sequence[Any], start: int) -> Any:
    return _make_wire(list(edges[start:]) + list(edges[:start]))


# --------------------------------------------------------------------------------------
# 辺の数をそろえる
# --------------------------------------------------------------------------------------
def _matched_candidates(
    wires: Sequence[Any], centroids: Sequence[Vec], normals: Sequence[Vec]
) -> List[Tuple[List[Any], bool]]:
    """辺の数をそろえた断面の並びの候補 `(線の並び, 自前で揃えたか)` を、試す順に返す。

    極角が単調でない断面(重心から見て裏に回り込む凹み)があるときだけ候補が2つになる: 長さの割合で割ったもの、
    だめなら OCCT の揃え(`CheckCompatibility`)。実測: U字 → 正方形は長さの割合だと側面が自己交差し OCCT の揃えなら
    通る。U字 → 円は逆で、OCCT の揃えは `IsDone()=False`。OCCT の揃えでは `shifts` が効かないことがある。
    """
    counts = {len(_edges(wire)) for wire in wires}
    if len(counts) == 1:
        return [(list(wires), True)]
    profiles = [_polar_profile(w, c, n) for w, c, n in zip(wires, centroids, normals)]
    if all(profile is not None for profile in profiles):
        angles = _merged([row[0][1] for profile in profiles for row in profile if row[0][1] > _ANGLE_TOL])
        matched = [
            _split_at_angles(w, c, n, profile, angles)
            for w, c, n, profile in zip(wires, centroids, normals, profiles)
        ]
        candidates = [(matched, True)]
    else:
        fractions = _merged([f for wire in wires for f in _vertex_fractions(wire)], tol=1.0e-9)
        candidates = [([_split_at_fractions(wire, fractions) for wire in wires], True), (list(wires), False)]
    usable = [(ws, own) for ws, own in candidates if not own or len({len(_edges(w)) for w in ws}) == 1]
    if not usable:
        raise ValueError("failed to match the number of edges between loft sections")
    return usable


def _polar_profile(wire: Any, centre: Vec, normal: Vec) -> Optional[List[List[Tuple[float, float]]]]:
    """辺ごとの `[(パラメータ, 始点からの極角(巻き戻し済み・0..360))]`(走査順)。単調でなければ None。"""
    reference = _sub(_start(wire), centre)
    out: List[List[Tuple[float, float]]] = []
    previous: Optional[float] = None
    for edge in _edges(wire):
        row = []
        for t in _traversal_parameters(edge):
            raw = _angle(_sub(_point_on(edge, t), centre), normal, reference) % 360.0
            if previous is None:
                value = 0.0 if raw > 360.0 - _ANGLE_TOL else raw
            else:
                value = raw + 360.0 * round((previous - raw) / 360.0)
                if value < previous - 1.0e-7:
                    value += 360.0
                if value < previous - 1.0e-7:
                    return None
            row.append((t, value))
            previous = value
        out.append(row)
    if previous is None or abs(previous - 360.0) > 1.0e-3:
        return None
    return out


def _split_at_angles(
    wire: Any, centre: Vec, normal: Vec, profile: Sequence[Sequence[Tuple[float, float]]], angles: Sequence[float]
) -> Any:
    reference = _sub(_start(wire), centre)
    vertices = [row[0][1] for row in profile] + [360.0]
    out: List[Any] = []
    for edge, row in zip(_edges(wire), profile):
        low, high = row[0][1], row[-1][1]
        cuts = [
            a for a in angles
            if low + _ANGLE_TOL < a < high - _ANGLE_TOL and all(abs(a - v) > _ANGLE_TOL for v in vertices)
        ]
        if not cuts:
            out.append(edge)
            continue
        params = []
        for a in cuts:
            k = next(k for k in range(len(row) - 1) if row[k][1] <= a <= row[k + 1][1])
            params.append(
                _bisect_angle(edge, centre, normal, reference, row[k][0], row[k + 1][0], row[k][1], row[k + 1][1], a)
            )
        out += _split_edge(edge, params)
    return _make_wire(out)


def _bisect_angle(
    edge: Any, centre: Vec, normal: Vec, reference: Vec, t_low: float, t_high: float, a_low: float, a_high: float,
    target: float,
) -> float:
    """辺の上で、極角が `target` になるパラメータを二分法で求める(区間の両端の極角は巻き戻し済み)。"""
    for _ in range(60):
        t_mid = (t_low + t_high) / 2.0
        guess = (a_low + a_high) / 2.0
        raw = _angle(_sub(_point_on(edge, t_mid), centre), normal, reference)
        value = raw + 360.0 * round((guess - raw) / 360.0)
        if value < target:
            t_low, a_low = t_mid, value
        else:
            t_high, a_high = t_mid, value
    return (t_low + t_high) / 2.0


def _vertex_fractions(wire: Any) -> List[float]:
    lengths = [_edge_length(edge) for edge in _edges(wire)]
    total = sum(lengths)
    out, acc = [], 0.0
    for length in lengths[:-1]:
        acc += length
        out.append(acc / total)
    return out


def _split_at_fractions(wire: Any, fractions: Sequence[float]) -> Any:
    """線を、始点からの長さの割合 `fractions` の位置で割る(既にある頂点の位置は割らない)。"""
    edges = _edges(wire)
    lengths = [_edge_length(edge) for edge in edges]
    total = sum(lengths)
    out: List[Any] = []
    acc = 0.0
    for edge, length in zip(edges, lengths):
        low, high = acc / total, (acc + length) / total
        inner = [f for f in fractions if low + 1.0e-6 < f < high - 1.0e-6]
        if not inner:
            out.append(edge)
        else:
            curve = BRepAdaptor_Curve(edge)
            first, last = curve.FirstParameter(), curve.LastParameter()
            if edge.Orientation() == TopAbs_REVERSED:
                params = [GCPnts_AbscissaPoint(curve, -(f - low) * total, last).Parameter() for f in inner]
            else:
                params = [GCPnts_AbscissaPoint(curve, (f - low) * total, first).Parameter() for f in inner]
            out += _split_edge(edge, params)
        acc += length
    return _make_wire(out)


def _merged(values: Sequence[float], tol: float = _ANGLE_TOL) -> List[float]:
    out: List[float] = []
    for value in sorted(values):
        if not out or value - out[-1] > tol:
            out.append(value)
    return out


# --------------------------------------------------------------------------------------
# 辺と線の小道具
# --------------------------------------------------------------------------------------
def _edges(wire: Any) -> List[Any]:
    explorer = BRepTools_WireExplorer(TopoDS.Wire_s(wire))
    out = []
    while explorer.More():
        out.append(TopoDS.Edge_s(explorer.Current()))
        explorer.Next()
    return out


def _make_wire(edges: Sequence[Any]) -> Any:
    builder = BRepBuilderAPI_MakeWire()
    for edge in edges:
        builder.Add(TopoDS.Edge_s(edge))
    if not builder.IsDone():
        raise ValueError("failed to rebuild a loft section wire")
    return builder.Wire()


def _reversed(wire: Any) -> Any:
    return _make_wire([TopoDS.Edge_s(edge.Reversed()) for edge in reversed(_edges(wire))])


def _traversal_parameters(edge: Any, count: int = _SAMPLES_PER_EDGE) -> List[float]:
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    params = [first + (last - first) * i / count for i in range(count + 1)]
    return params[::-1] if edge.Orientation() == TopAbs_REVERSED else params


def _point_on(edge: Any, t: float) -> Vec:
    p = BRepAdaptor_Curve(edge).Value(t)
    return (p.X(), p.Y(), p.Z())


def _start(wire: Any) -> Vec:
    edge = _edges(wire)[0]
    return _point_on(edge, _traversal_parameters(edge, 1)[0])


def _wire_points(wire: Any) -> List[Vec]:
    points: List[Vec] = []
    for edge in _edges(wire):
        points += [_point_on(edge, t) for t in _traversal_parameters(edge, 24)[:-1]]
    return points


def _split_edge(edge: Any, params: Sequence[float]) -> List[Any]:
    """辺を `params`(どの順でもよい)で割り、走査順の辺の並びを返す。"""
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    geometry = BRep_Tool.Curve_s(edge, first, last)
    bounds = [first] + sorted(params) + [last]
    pieces = [BRepBuilderAPI_MakeEdge(geometry, bounds[k], bounds[k + 1]).Edge() for k in range(len(bounds) - 1)]
    if edge.Orientation() == TopAbs_REVERSED:
        pieces = [TopoDS.Edge_s(piece.Reversed()) for piece in reversed(pieces)]
    return pieces


def _nearest_parameter(edge: Any, centre: Vec, normal: Vec, reference: Vec) -> float:
    params = _traversal_parameters(edge, 720)
    best = min(params, key=lambda t: abs(_angle(_sub(_point_on(edge, t), centre), normal, reference)))
    step = abs(params[1] - params[0])
    return _bisect_toward(edge, centre, normal, reference, best - step, best + step)


def _bisect_toward(edge: Any, centre: Vec, normal: Vec, reference: Vec, low: float, high: float) -> float:
    for _ in range(60):
        third = (high - low) / 3.0
        a = abs(_angle(_sub(_point_on(edge, low + third), centre), normal, reference))
        b = abs(_angle(_sub(_point_on(edge, high - third), centre), normal, reference))
        if a < b:
            high -= third
        else:
            low += third
    return (low + high) / 2.0


def _closed_from(edge: Any, t: float) -> Any:
    """閉じた1辺(周期曲線)を、パラメータ `t` の点から1周する辺の線に作り直す(向きは保つ)。"""
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    geometry = BRep_Tool.Curve_s(edge, first, last)
    if not geometry.IsPeriodic():
        # 周期でない閉曲線(閉じたベジエ等)は継ぎ目を動かせないので、その点で2本に割る。
        if abs(t - first) < 1.0e-9 or abs(t - last) < 1.0e-9:
            return _make_wire([edge])
        head, tail = _split_edge(edge, [t])
        return _make_wire([tail, head])
    period = geometry.Period()
    rebuilt = BRepBuilderAPI_MakeEdge(geometry, t, t + period).Edge()
    if edge.Orientation() == TopAbs_REVERSED:
        rebuilt = TopoDS.Edge_s(rebuilt.Reversed())
    return _make_wire([rebuilt])


def _edge_length(edge: Any) -> float:
    return GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edge))


def _wire_count(face: Any) -> int:
    explorer = TopExp_Explorer(face, TopAbs_WIRE)
    count = 0
    while explorer.More():
        count += 1
        explorer.Next()
    return count


def _area_centroid(face: Any) -> Vec:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    c = props.CentreOfMass()
    return (c.X(), c.Y(), c.Z())


# --------------------------------------------------------------------------------------
# ベクトル
# --------------------------------------------------------------------------------------
def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _length(a: Vec) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a: Vec) -> Vec:
    length = _length(a)
    if not length > 0.0:
        raise ValueError("loft section normal has zero length")
    return _scale(a, 1.0 / length)


def _newell(points: Sequence[Vec]) -> Vec:
    total = (0.0, 0.0, 0.0)
    for i in range(len(points)):
        c = _cross(points[i], points[(i + 1) % len(points)])
        total = (total[0] + c[0], total[1] + c[1], total[2] + c[2])
    return total


def _angle(vector: Vec, normal: Vec, reference: Vec) -> float:
    """法線 `normal` の平面で、`reference` から `vector` への反時計回りの角度(度、-180..180)。"""
    v = _sub(vector, _scale(normal, _dot(vector, normal)))
    r = _sub(reference, _scale(normal, _dot(reference, normal)))
    return math.degrees(math.atan2(_dot(_cross(r, v), normal), _dot(r, v)))


def _rotate_between(vector: Vec, n1: Vec, n2: Vec) -> Vec:
    """`n1` を `n2` へ運ぶ最小回転を `vector` に施す(ロドリゲス)。"""
    axis = _cross(n1, n2)
    s = _length(axis)
    c = _dot(n1, n2)
    if s < 1.0e-12:
        return vector if c > 0.0 else _scale(vector, -1.0)
    k = _scale(axis, 1.0 / s)
    kv = _cross(k, vector)
    kd = _dot(k, vector) * (1.0 - c)
    return (
        vector[0] * c + kv[0] * s + k[0] * kd,
        vector[1] * c + kv[1] * s + k[1] * kd,
        vector[2] * c + kv[2] * s + k[2] * kd,
    )

"""OCP(OCCT)を直接叩く層。

**duocad配下でOCP.*をimportしてよいのはカーネル層だけ**(verbが直接OCPを叩き始めるのを
防ぐための境界。許可する表の正は README「モジュールとimport境界」)。

戻り値はいずれも **JSON化できる素の値(float/int/str/list/dict)だけ**にする。OCPのオブジェクトを
kernel の外へ出さないための規律で、境界テスト(importの静的検査)では捕まえられない側の約束。
唯一の例外は `kernel_extrude.extrude_faces` / `kernel_extrude.extrude_regions` / `face_of`
(と `kernel_sketch.sketch_regions`)の戻りに含まれる TopoDS_*(document.Body.shape や領域押し出しの入力になる値)。

**ocp_tessellate は使わない**(この venv に入っておらず、かつ面ピッキングには面ごとの
三角形グループが要るので自前実装の方が制御できる)。
"""
from __future__ import annotations

import math
import os
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepAlgoAPI import (
    BRepAlgoAPI_Common,
    BRepAlgoAPI_Cut,
    BRepAlgoAPI_Fuse,
)
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy, BRepBuilderAPI_Transform
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.BRepTools import BRepTools
from OCP.GCPnts import GCPnts_AbscissaPoint, GCPnts_TangentialDeflection
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.STEPControl import (
    STEPControl_Reader,
    STEPControl_StepModelType,
    STEPControl_Writer,
)
from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape  # noqa: F401  型注釈だけ
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

from . import kernel_meshing, meshbody
from .mesh_geometry import compound, indexed_map, subshapes

#: describe_shape の faces_by_type が持つキー(この順序が正。query.py が初期化に使う)
FACE_KINDS = (
    "plane",
    "cylinder_full",
    "cylinder_partial",
    "torus",
    "cone",
    "bspline",
    "other",
)

#: 全周円筒(=穴)と見なすU範囲のしきい値[度](docs/DESIGN_DECISIONS.md「全周円筒のしきい値」)。
#: shelf_attachment_GD.step の円筒22面のU範囲は全て正確に360.0度で、しきい値付近に一件も無い
#: (実測)ため、この値の選び方に結果は依存しない。
FULL_CYLINDER_DEGREES = 350.0

#: 基準平面の (origin, normal, xdir)。gp_Ax3 で組み立てると v は表のとおりになる。
_BASE_PLANE_AXES = {
    "XY": ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),  # v -> +Y
    "XZ": ((0.0, 0.0, 0.0), (0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),  # v -> +Z
    "YZ": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),  # v -> +Z
}


# --------------------------------------------------------------------------------------
# STEP / STL
# --------------------------------------------------------------------------------------
def read_step(path: str) -> "TopoDS_Shape":
    """STEPファイルを読み、TopoDS_Shapeを返す。失敗時は例外を送出する(黙って失敗しない)。

    パスが存在しない -> FileNotFoundError / 中身がSTEPとして壊れている -> ValueError。

    **ReadFile()の戻り値を必ず先に検査する**。失敗した状態で TransferRoots()/OneShape() を
    呼ぶとPython例外にならずプロセスごと落ちる(0xC0000005)ため、ここが唯一の安全弁になる。
    存在検査は「どちらの例外を投げるか」を決めるためだけに使い、続行の判断には使わない
    (Path.exists() だけでは壊れたファイルを止められない)。
    """
    filename = os.fspath(path)
    reader = STEPControl_Reader()
    status = reader.ReadFile(filename)
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        # ここで必ず止まる。以降(TransferRoots/OneShape)は読み込み成功時のみ到達する。
        if not os.path.exists(filename):
            raise FileNotFoundError(f"STEP file not found: {filename} (ReadFile -> {status})")
        raise ValueError(f"not a readable STEP file: {filename} (ReadFile -> {status})")

    roots = reader.TransferRoots()
    if roots < 1:
        raise ValueError(f"STEP file has no transferable root: {filename}")

    shape = reader.OneShape()
    if shape is None or shape.IsNull():
        raise ValueError(f"STEP file produced an empty shape: {filename}")
    return shape


def read_step_text(text: str) -> "TopoDS_Shape":
    """**STEP本文の文字列**を読み、TopoDS_Shapeを返す。壊れていれば ValueError。

    操作ログに埋め込んだSTEP本文(verb `import_step`)を
    再生するための入口。**OCCTのSTEPリーダはファイルパスしか受け取らない**ので、
    一時ファイルへ書き出してから `read_step` を通す。

    **ReadFile()の戻り値検査は `read_step` のものをそのまま通る**(安全弁を二重に持たない)。
    ここで別経路のリーダを立てると、失敗した状態で TransferRoots() を呼んで
    **プロセスごと落ちる**(0xC0000005)経路が新しく1本できてしまう。

    一時ディレクトリは読み終えた時点で消す。**形状はOCCTのメモリ上に構築済み**で
    ファイルを参照し続けないので、戻り値は消した後も有効。

    書き出しは**バイト列で行う**(`write_text` は Windows で `\\n` を `\\r\\n` へ
    書き換えるため、`\\r\\n` を含む元のSTEPが `\\r\\r\\n` に化ける)。
    """
    if not isinstance(text, str):
        raise TypeError(f"STEP本文は文字列で渡すこと: {type(text).__name__}")
    if text.strip() == "":
        # OCCTへ渡す前に止める(空ファイルは ReadFile が失敗し、その先は read_step が
        # 止めるが、埋め込みが空だった、という原因のほうを言いたい)。
        raise ValueError("埋め込まれたSTEP本文が空")
    with tempfile.TemporaryDirectory(prefix="duocad-step-") as tmpdir:
        staging = os.path.join(tmpdir, "embedded.step")
        with open(staging, "wb") as handle:
            handle.write(text.encode("utf-8"))
        try:
            return read_step(staging)
        except (FileNotFoundError, ValueError) as error:
            # 一時パスをそのまま見せない(利用者にとって意味の無い場所を指すため)。
            raise ValueError(
                f"埋め込まれたSTEP本文をSTEPとして読めない({len(text)}文字): {error}"
            ) from error


def write_step(shape: "TopoDS_Shape", path: str) -> None:
    """形状をSTEPとして書き出す(モードは AsIs)。書き出しに失敗したらファイルを残さない。"""
    if shape is None or shape.IsNull():
        raise ValueError("cannot write an empty shape to STEP")

    filename = os.fspath(path)
    writer = STEPControl_Writer()
    # Transfer も IFSelect_ReturnStatus を返す。ここを捨てると「転送に失敗したのに
    # Write は成功した(=中身の無いSTEP)」を成功として通しうる(§7「例外を投げずに黙って失敗する」)。
    transfer_status = writer.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
    if transfer_status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise ValueError(f"failed to transfer shape to STEP: {filename} (Transfer -> {transfer_status})")

    status = writer.Write(filename)
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        _unlink_quietly(filename)
        raise ValueError(f"failed to write STEP: {filename} (Write -> {status})")


def _unlink_quietly(filename: str) -> None:
    """書き出し失敗時に半端なファイルを残さない(0バイトの成果物を作らない。§6予防6)。"""
    try:
        os.remove(filename)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# 計測・分類(query.py から使う。OCPを触るのでここに置く)
# --------------------------------------------------------------------------------------
def volume(shape: "TopoDS_Shape") -> float:
    """形状の体積(mm^3)。

    `BRepGProp.VolumeProperties_s(shape, props)` の**既定の積分**を使う(`Eps` を渡さない)。
    `Eps` 付きの適応積分にすると精度が落ち(Eps=1e-9で相対1.8e-10、Eps=1e-6で相対3.7e-8)、
    STEP往復の体積比較が体積の求め方に依存してしまう。

    STEP書き出し→読み込みを挟んだ体積は**bit-exactではない**(浮動小数点の積分順序に由来する
    丸め、実測相対誤差 約8e-16)。比較には `math.isclose(rel_tol=1e-6)` を使う —— 実測誤差の
    約10億倍のマージンで、面の欠落・重複などの実ジオメトリ破損は検出しつつ丸めノイズでは
    落ちない。面数は往復後も厳密一致(`==`)でよい。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot compute the volume of an empty shape")
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def centroid(shape: "TopoDS_Shape") -> List[float]:
    """形状の**体積重心**(mm)。`volume` と同じ `VolumeProperties_s` の既定積分を使う。

    `move_body` の回転の既定の中心がこれである。**境界箱の中心ではない** —— 境界箱は
    軸の向きに依存して動くので、同じボディを別の軸で回すと中心が変わってしまう。
    重心は形だけで決まる。

    体積が0の形状(面だけのシェル等)は ValueError —— `Mass()` が0だと `CentreOfMass()` は
    意味の無い点を返すので、黙って原点近くを中心に回さない。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot compute the centroid of an empty shape")
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    if not props.Mass() > 0.0:
        raise ValueError("cannot compute the centroid of a zero-volume shape")
    point = props.CentreOfMass()
    return [float(point.X()), float(point.Y()), float(point.Z())]


def classify_face(face: Any) -> str:
    """1つの面を FACE_KINDS のいずれかへ分類する。

    円筒は `BRepAdaptor_Surface` のU範囲(度)が FULL_CYLINDER_DEGREES を超えるかで
    全周(=穴)と部分に分ける(docs/DESIGN_DECISIONS.md「全周円筒のしきい値」)。
    """
    adaptor = BRepAdaptor_Surface(TopoDS.Face_s(face))
    surface_type = adaptor.GetType()

    if surface_type == GeomAbs_SurfaceType.GeomAbs_Plane:
        return "plane"
    if surface_type == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        span = math.degrees(adaptor.LastUParameter() - adaptor.FirstUParameter())
        return "cylinder_full" if span > FULL_CYLINDER_DEGREES else "cylinder_partial"
    if surface_type == GeomAbs_SurfaceType.GeomAbs_Torus:
        return "torus"
    if surface_type == GeomAbs_SurfaceType.GeomAbs_Cone:
        return "cone"
    if surface_type in (
        GeomAbs_SurfaceType.GeomAbs_BSplineSurface,
        GeomAbs_SurfaceType.GeomAbs_BezierSurface,
    ):
        return "bspline"
    return "other"


class _ShapeKey:
    """`TopoDS_Shape` を dict の鍵にするためのラッパ。

    **素の `TopoDS_Shape` は鍵にならない。** 同じ面を2回取り出すと `hash` は一致するのに
    `__eq__` が False を返すので、`dict` も `lru_cache` も**ヒット率0%のまま、遅いまま
    メモリだけ増える**(2026-09-07実測)。動いてはいるので気づかない。

    同一性は `IsSame`(TShape と Location の一致)で見る。`move_body` の
    `shape.Moved(TopLoc_Location(trsf))` は TShape を共有するが、Location が違うので
    別扱いになる(実測: 1000mm 動かすと `hash` 不一致・`IsSame` False)。

    **`IsSame` は Orientation(forward/reversed)を見ない。** 向きに依存しない値にしか
    使ってはいけない。
    """

    __slots__ = ("shape", "_hash")

    def __init__(self, shape: "TopoDS_Shape") -> None:
        self.shape = shape
        self._hash = hash(shape)

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ShapeKey) and bool(self.shape.IsSame(other.shape))


class _OrientedShapeKey(_ShapeKey):
    """`_ShapeKey` に Orientation の一致を足した鍵。向きに依存する値(体積の符号・ブーリアンの内外)を
    覚えるキャッシュ用で、`shape_key` が返す。"""

    __slots__ = ("orientation",)

    def __init__(self, shape: "TopoDS_Shape") -> None:
        super().__init__(shape)
        self.orientation = shape.Orientation()

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, _OrientedShapeKey) and self.orientation == other.orientation
                and bool(self.shape.IsSame(other.shape)))

    __hash__ = _ShapeKey.__hash__


def shape_key(shape: "TopoDS_Shape") -> Any:
    """形状を辞書の鍵にするための値(TShape・Location・Orientation の一致で同一とみなす)。
    素の `TopoDS_Shape` は鍵にならない(`_ShapeKey` の docstring)。"""
    return _OrientedShapeKey(shape)


class BoundingBoxCache:
    """`bounding_box` の結果を覚える、**1つの文書のあいだだけ生きる**メモ。

    `Document` が1つ持ち(`document.bbox_cache`)、文書を差し替えると `Document` ごと
    作り直される。モジュールに1つ置くと、長いセッションで別の文書の形状が積み上がる。

    **bbox 専用。** 鍵の同一性(`_ShapeKey`)は Orientation を見ないので、向きに依存する
    値をここへ載せると静かに壊れる。

    `AddOptimal` は繰り返しても速くならない(OCCTが内部で結果を持たない。実測: 11ボディで
    `Add` 0.09ms に対し `AddOptimal` 178ms、1形状あたりでは18〜60ms)。**キャッシュ無しでは
    `AddOptimal` は使えない。**
    """

    __slots__ = ("_extents",)

    def __init__(self) -> None:
        self._extents: Dict[_ShapeKey, Tuple[float, float, float, float, float, float]] = {}

    def __len__(self) -> int:
        return len(self._extents)

    def clear(self) -> None:
        self._extents.clear()

    def extents(self, shape: "TopoDS_Shape") -> Tuple[float, float, float, float, float, float]:
        """`(x_min, y_min, z_min, x_max, y_max, z_max)`。無ければ測って覚える。"""
        key = _ShapeKey(shape)
        found = self._extents.get(key)
        if found is None:
            found = _bounding_box_extents(shape)
            self._extents[key] = found
        return found


def _bounding_box_extents(
    shape: "TopoDS_Shape",
) -> Tuple[float, float, float, float, float, float]:
    """境界箱を OCCT から取る(`(x_min, y_min, z_min, x_max, y_max, z_max)`)。

    `BRepBndLib.AddOptimal_s(..., useTriangulation=False, useShapeTolerance=False)` を使う。
    **`Add_s` は面のトリムを見ず、下地のサーフェス全体で囲む** —— トリムされたトーラス面は
    ドーナツ丸ごとで囲まれるので、曲がったバーの部品では寸法が最大9倍に化ける
    (実測: `models/hanger_bar_attachment.json` の 26.6mm 角の部品が 241.3mm になる)。
    箱・円柱・平面では両者が一致するので、**曲面の種類によって壊れ方が変わる**。

    `useTriangulation=False` は、True(既定)だと**既存の三角形分割があればそれを使う**ため
    (同じ形状でも `tessellate` を呼んだ後だけ違う値になる)。`SetGap(0.0)` を入れても
    OCCT は内部許容差ぶん(実測 1e-7mm)膨らませた値を返す。
    """
    box = Bnd_Box()
    box.SetGap(0.0)
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        raise ValueError("bounding box is void (形状に頂点が無い)")
    return box.Get()


def bounding_box(
    shape: "TopoDS_Shape", *, cache: Optional[BoundingBoxCache] = None
) -> dict:
    """形状の境界箱(mm)。**幾何から求める**(三角形分割に依存しない)。

    戻り値: {"min":[x,y,z], "max":[x,y,z], "center":[x,y,z], "size":[dx,dy,dz], "diagonal": float}

    `cache` に `document.bbox_cache` を渡すと、同じ形状の測り直しを避ける(測り方の詳細と
    速さは `_bounding_box_extents` / `BoundingBoxCache`)。**戻り値の dict は毎回作り直す**
    ので、呼び出し側が書き換えても他の呼び出し側には波及しない。

    `kernel.tessellate` の戻りにある `bbox` は三角形の節点から作った別物で、
    あちらはアプリ層のカメラ合わせ用。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot compute the bounding box of an empty shape")
    if cache is None:
        x_min, y_min, z_min, x_max, y_max, z_max = _bounding_box_extents(shape)
    else:
        x_min, y_min, z_min, x_max, y_max, z_max = cache.extents(shape)
    return meshbody.bbox_payload([x_min, y_min, z_min], [x_max, y_max, z_max], size=True)


#: 部分形状の種別名 → (OCCT の列挙, エラー文で使う複数形)。`count_subshapes` と `_subshape_map` の唯一の表。
_SUBSHAPE_KINDS = {
    "face": (TopAbs_ShapeEnum.TopAbs_FACE, "faces"),
    "edge": (TopAbs_ShapeEnum.TopAbs_EDGE, "edges"),
    "vertex": (TopAbs_ShapeEnum.TopAbs_VERTEX, "vertices"),
}


def count_subshapes(shape: "TopoDS_Shape", kind: str) -> int:
    """重複を除いた部分形状の数(`TopExp.MapShapes_s`)。kind は "face" | "edge" | "vertex"。

    `TopExp_Explorer` で素朴に列挙すると隣接2面から同じ辺を2回拾う
    (shelf_attachment_GD.step 実測: 重複除去66 / 素朴な列挙132)。
    null の検査はしない(NULL 形状は 0。`_face_map` 等と違って ValueError にしない)。
    """
    if kind not in _SUBSHAPE_KINDS:
        raise ValueError(f"unknown subshape kind: {kind!r}")
    return indexed_map(shape, _SUBSHAPE_KINDS[kind][0]).Extent()


# --------------------------------------------------------------------------------------
# ブーリアンの結果の検証(extrude_faces / extrude_regions / boolean_bodies / fillet・chamfer が共有)
# --------------------------------------------------------------------------------------
#: 「ブーリアンは成功したのに体積が変わっていない」と見なす相対差。OCCT は「成功したが何も
#: 起きていない」を返せるので、ここで止めないと静かに空振りする。
_NO_CHANGE_RTOL = 1e-9


def _fuse_all(shapes: Sequence[Any], what: str) -> "TopoDS_Shape":
    """`shapes` を `BRepAlgoAPI_Fuse` で1つにする。**1つなら Fuse を呼ばずそのまま返す**。"""
    combined = shapes[0]
    for other in shapes[1:]:
        fused = BRepAlgoAPI_Fuse(combined, other)
        fused.Build()
        if not fused.IsDone():
            raise ValueError(f"failed to fuse {what}")
        combined = fused.Shape()
        if combined is None or combined.IsNull():
            raise ValueError(f"fusing {what} produced an empty shape")
    return combined


def _checked_volume(
    result: Any, label: str, *, volume_before: Optional[float] = None, context: str = ""
) -> float:
    """ブーリアンの結果を 空 → 体積>0 → 無変化 の順に検査して体積を返す。

    無変化の検査は `volume_before` を渡したときだけ(押し出しは相手があるときだけ見る。
    `boolean_bodies` と `_blend_edges` は見ない)。`context` は文の括弧に足す(`face 3, distance=5.0` 等)。
    """
    suffix = f", {context}" if context else ""
    if result is None or result.IsNull():
        raise ValueError(f"{label} produced an empty shape" + (f" ({context})" if context else ""))
    volume_after = volume(result)
    if volume_after <= 0:
        raise ValueError(f"{label} left nothing solid (volume={volume_after}{suffix})")
    if volume_before is not None and abs(volume_after - volume_before) <= _NO_CHANGE_RTOL * max(
        1.0, abs(volume_before)
    ):
        raise ValueError(
            f"{label} did not change the shape (volume {volume_before} -> {volume_after}{suffix})"
        )
    return volume_after


def _require_valid(shape: "TopoDS_Shape", label: str, context: str = "") -> None:
    """`BRepCheck_Analyzer` の最後の関門。ここまでの検査を全部通っても壊れていることがある
    (曲面の掃引で実測)。`_checked_volume` と分けてあるのは、`extrude_faces` の join / cut だけ
    間に `_unify_same_domain` が入るため(統合後の形に対して行う)。"""
    if not BRepCheck_Analyzer(shape).IsValid():
        suffix = f", {context}" if context else ""
        raise ValueError(f"{label} produced an invalid shape (BRepCheck{suffix})")


def _check_tessellation_args(
    shape: Any, deviation: float, angular_deflection: float, *, target: str = "an empty shape"
) -> None:
    """`tessellate` 系3本の引数検査(null・`deviation > 0`・`angular_deflection > 0`)。"""
    if shape is None or shape.IsNull():
        raise ValueError(f"cannot tessellate {target}")
    if not deviation > 0:
        raise ValueError(f"deviation must be positive (mm), got {deviation!r}")
    if not angular_deflection > 0:
        raise ValueError(f"angular_deflection must be positive, got {angular_deflection!r}")


def _point_and_direction(
    point: Sequence[float], direction: Sequence[float], *, names: Tuple[str, str], zero_reason: str
) -> Tuple[List[float], List[float]]:
    """3要素・有限の点と、零ベクトルでない方向を float のリストにして返す(`rotate_shape` /
    `mirror_shape` の共通の入口。`gp_Dir` は零ベクトルで例外を投げるが、そこまで持って行かずに手前で断る)。"""
    first, second = names
    origin = [float(value) for value in point]
    axis = [float(value) for value in direction]
    if len(origin) != 3 or len(axis) != 3:
        raise ValueError(f"{first} / {second} must have 3 numbers, got {point!r} / {direction!r}")
    if not all(math.isfinite(value) for value in origin + axis):
        raise ValueError(f"{first} / {second} must be finite, got {point!r} / {direction!r}")
    if not math.sqrt(sum(value * value for value in axis)) > 0.0:
        raise ValueError(f"{second} must not be the zero vector ({zero_reason})")
    return origin, axis


def _face_map_projection(successors: dict, final_count: int) -> dict:
    """後継の表(旧添字 → 新添字の列)を `face_map` / `deleted` / `new_faces` の射影にする
    (`extrude_faces` と `_blend_provenance` が同じ形で保存する)。"""
    mapped = {position for positions in successors.values() for position in positions}
    return {
        "face_map": successors,
        "deleted": [int(key) for key, value in successors.items() if not value],
        "new_faces": [i for i in range(1, final_count + 1) if i not in mapped],
    }


def _positions_in(final_map: "TopTools_IndexedMapOfShape", candidates: Iterable[Any]) -> List[int]:
    """`candidates` が `final_map` の何番目かを重複なしで(最初に現れた順に)集める。"""
    resolved: List[int] = []
    for candidate in candidates:
        position = final_map.FindIndex(candidate)
        if position > 0 and position not in resolved:
            resolved.append(position)
    return resolved


# --------------------------------------------------------------------------------------
# 由来(provenance)の正規化アダプタ — day1で置き場所だけ作る(§6予防2)
# --------------------------------------------------------------------------------------
def normalize_provenance(
    builder: Any,
    inputs: Iterable["TopoDS_Shape"] = (),
) -> Optional[dict]:
    """OCCTの由来マップを1つの形へ正規化して返す(`Operation.provenance` に入れる値)。

    **取り出し方が操作ごとに違う**のを吸収する —— ブーリアン(`BRepAlgoAPI_*`)は `History()`
    -> `BRepTools_History` を持ち、フィレット等の `BRepBuilderAPI_MakeShape` 系は自分自身が
    `Modified()` / `Generated()` / `IsDeleted()` を持つ。**正規化はこの1関数に閉じる**。

    戻り値の形(由来が1件も取れなければ None):

        {
            "modified":  [{"input": <TopoDS_Shape>, "results": [<TopoDS_Shape>, ...]}, ...],
            "generated": [{"input": <TopoDS_Shape>, "results": [<TopoDS_Shape>, ...]}, ...],
            "deleted":   [<TopoDS_Shape>, ...],
        }

    戻り値は `TopoDS_Shape` を直接含む**メモリ上の形**であり、そのままでは
    `OpLog.to_json()` でJSON化できない。呼び出し側で永続化用の射影に変換すること
    (`_blend_provenance` 参照)。
    """
    source = builder.History() if hasattr(builder, "History") else builder

    modified: List[dict] = []
    generated: List[dict] = []
    deleted: List[Any] = []

    for input_shape in inputs:
        if input_shape is None or input_shape.IsNull():
            continue
        for sub in _iter_faces_and_edges(input_shape):
            for bucket, getter in (("modified", "Modified"), ("generated", "Generated")):
                results = _shape_list(source, getter, sub)
                if results:
                    (modified if bucket == "modified" else generated).append(
                        {"input": sub, "results": results}
                    )
            if _is_removed(source, sub):
                deleted.append(sub)

    if not (modified or generated or deleted):
        return None
    return {"modified": modified, "generated": generated, "deleted": deleted}


def _iter_faces_and_edges(shape: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """由来を問い合わせる対象の部分形状(面・辺)を重複なしで列挙する。"""
    out: List[TopoDS_Shape] = []
    for enum in (TopAbs_ShapeEnum.TopAbs_FACE, TopAbs_ShapeEnum.TopAbs_EDGE):
        shape_map = indexed_map(shape, enum)
        out.extend(shape_map.FindKey(i) for i in range(1, shape_map.Extent() + 1))
    return out


def _shape_list(source: Any, getter: str, sub: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """`Modified(sub)` / `Generated(sub)` の戻り(TopTools_ListOfShape)をPythonのlistへ。"""
    method = getattr(source, getter, None)
    if method is None:
        return []
    try:
        result = method(sub)
    except Exception:  # OCCTは対象外の部分形状で例外を投げることがある
        return []
    return list(result)


def _is_removed(source: Any, sub: "TopoDS_Shape") -> bool:
    """削除されたか。`BRepTools_History` は IsRemoved、MakeShape系は IsDeleted を持つ。"""
    for name in ("IsRemoved", "IsDeleted"):
        method = getattr(source, name, None)
        if method is not None:
            try:
                return bool(method(sub))
            except Exception:
                return False
    return False


# --------------------------------------------------------------------------------------
# テセレーション — 面ごとの三角形群
# --------------------------------------------------------------------------------------
#: 既定の線形偏差(mm)。「絶対」の偏差(`tessellate` の isRelative=False)。
DEFAULT_DEVIATION = 0.2
#: 既定の角度偏差(**ラジアン**。度ではない — OCCTのBRepMesh_IncrementalMesh/GCPnts_TangentialDeflection
#: の角度偏差パラメータはラジアンで、既定は0.5rad(`IMeshTools_Parameters().Angle`で確認済み)。
#: 0.35radは約20度)。曲面(穴の円筒)が多角形に見えない程度に細かくする。
DEFAULT_ANGULAR_DEFLECTION = 0.35


def translate_shape(
    shape: "TopoDS_Shape", dx: float, dy: float, dz: float, *, copy: bool = False
) -> "TopoDS_Shape":
    """形状を (dx, dy, dz) だけ平行移動した形状を返す(元の形状は変えない)。

    **面・辺・頂点の添字が変わらないこと**がこの関数の要点である。`move_body` の後も
    選択に入っている参照がそのまま同じ実体を指し続けるので、選択をリセットしなくてよい
    (`catalog.Command.resets_selection` の注記)。

    - `copy=False` … `TopoDS_Shape.Moved` で**位置だけを差し替える**。下層の形状(TShape)を
      共有するので、面の数え上げの順序は原理的に不変で、再計算も走らない(数十MBの
      読み込みSTEPでも一瞬)。
    - `copy=True`  … `BRepBuilderAPI_Transform(..., Copy=True)` で**独立した形状**を作る。
      複製元と実体を共有すると、後から片方をフィレットしたときにもう片方の由来が壊れる。

    **移動の合成は厳密**である。`gp_Trsf` の平行移動どうしの積は並進ベクトルの和になり、
    `T(-d) * T(d)` は誤差なく単位変換に戻る —— `move_body` の undo が逆向きの平行移動で
    済むのはこのため(形状のスナップショットを控えなくてよい)。
    """
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(float(dx), float(dy), float(dz)))
    return _transformed(shape, trsf, copy=copy, what="平行移動")


def rotate_shape(
    shape: "TopoDS_Shape",
    axis_point: Sequence[float],
    axis_dir: Sequence[float],
    angle_deg: float,
    *,
    copy: bool = False,
) -> "TopoDS_Shape":
    """形状を軸(`axis_point` を通り `axis_dir` を向く直線)まわりに回した形状を返す。

    `translate_shape` の姉妹で、**変わるのは `gp_Trsf` の作り方だけ**である
    (`SetTranslation` -> `SetRotation`)。したがって次の2つも平行移動と同じ:

    - **面・辺・頂点の添字は変わらない**(`copy=False` は `TopoDS_Shape.Moved` で位置だけを
      差し替え、下層の形状を共有する)。`move_body` が回転の後も選択をリセットしなくてよい
      根拠はここにある。
    - `copy=True` は `BRepBuilderAPI_Transform(..., Copy=True)` で**独立した形状**を作る。

    **角度は度**(`gp_Trsf.SetRotation` はラジアンなのでここで変換する)。符号は軸の向きに
    対する**右ねじ**(Z軸まわり正の角度は、上から見て反時計回り)で、スケッチ側の
    `sketch.rotate_point`(u->v が正)と同じ向きである。

    **逆回転は元へ戻る**。`cos(-a) == cos(a)` と `sin(-a) == -sin(a)` は浮動小数でも厳密なので
    `R(-a)` は `R(a)` の転置そのもので、積は丸め誤差(相対1e-16程度)を除いて単位行列になる
    —— 幾何の許容差(mmの1e-9未満)より桁違いに小さい。`move_body` の undo が形状の
    スナップショットを控えず「逆向きに同じ角度」で済むのはこのためである。

    軸方向が零ベクトル・非有限なら ValueError(`gp_Dir` は零ベクトルで例外を投げるが、
    そこまで持って行かずに手前で断る)。
    """
    point, direction = _point_and_direction(
        axis_point, axis_dir, names=("axis_point", "axis_dir"), zero_reason="軸の向きが決まらない"
    )
    if not math.isfinite(float(angle_deg)):
        raise ValueError(f"angle must be a finite number of degrees, got {angle_deg!r}")

    trsf = gp_Trsf()
    trsf.SetRotation(
        gp_Ax1(gp_Pnt(*point), gp_Dir(*direction)), math.radians(float(angle_deg))
    )
    return _transformed(shape, trsf, copy=copy, what="回転")


def _transformed(
    shape: "TopoDS_Shape", trsf: "gp_Trsf", *, copy: bool, what: str
) -> "TopoDS_Shape":
    """剛体変換を1つ掛けた形状(`translate_shape` / `rotate_shape` の共通の尻尾)。"""
    if copy:
        builder = BRepBuilderAPI_Transform(shape, trsf, True)
        if not builder.IsDone():
            raise ValueError(f"形状の{what}(複製)に失敗した")
        return builder.Shape()
    return shape.Moved(TopLoc_Location(trsf))


def mirror_shape(
    shape: "TopoDS_Shape", point: Sequence[float], normal: Sequence[float], *, copy: bool = True
) -> "TopoDS_Shape":
    """形状を、点 `point` を通り法線 `normal` の平面に対して鏡映した形状を返す(元は変えない)。

    `translate_shape` / `rotate_shape` の姉妹だが、**鏡映は向きを反転する変換**
    (行列式が負)である。`mirror_body` は常に複製を作る(in-place反転の口を持たない)ので
    `copy` の既定は `True` —— `translate_shape`/`rotate_shape` と揃えて残してあるだけで、
    `copy=False`(`Moved` で位置だけ差し替え)も `gp_Trsf` が向きの反転を保持したまま動くので
    技術的には壊れない。

    点・法線が3要素でない/非有限、または法線が零ベクトルなら ValueError。
    """
    origin, axis = _point_and_direction(
        point, normal, names=("point", "normal"), zero_reason="鏡映面の向きが決まらない"
    )

    trsf = gp_Trsf()
    trsf.SetMirror(gp_Ax2(gp_Pnt(*origin), gp_Dir(*axis)))
    return _transformed(shape, trsf, copy=copy, what="鏡映")


def edge_axis(shape: "TopoDS_Shape", edge_index: int) -> tuple:
    """**まっすぐな辺**を回転軸 `([x, y, z], [dx, dy, dz])` にする(点は辺の上、向きは単位長)。

    添字は `edge_count` / `edge_of` と同じ1始まりの体系 —— つまり**辺の選択の識別子そのもの**
    で、`move_body(mode="rotate", axis_edge=...)` はこれを使って「この辺を軸に回す」を表す。

    まっすぐでない辺(円・spline)は ValueError。円い辺を軸と読んで中心線を勝手に作ると、
    人が指した辺と回る軸が食い違う(例外の出ない事故になる)。
    """
    edge = edge_of(shape, edge_index)
    if classify_edge(edge) != "line":
        raise ValueError(
            f"edge {int(edge_index)} is not a straight line "
            f"(kind={classify_edge(edge)!r}): 回転軸に使えるのはまっすぐな辺だけ"
        )
    line = BRepAdaptor_Curve(edge).Line()
    point = line.Location()
    direction = line.Direction()
    return (
        [float(point.X()), float(point.Y()), float(point.Z())],
        [float(direction.X()), float(direction.Y()), float(direction.Z())],
    )


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


def face_plane(shape: "TopoDS_Shape", face_index: int) -> tuple:
    """**平らな面**を「面積重心と外向き単位法線」`([x, y, z], [nx, ny, nz])` にする(測定用)。

    添字は `face_count` / `face_of` と同じ1始まりの体系(= 面の選択の識別子そのもの)。
    平面でない面(円筒・トーラス・spline)は ValueError —— 曲面に法線を1本あてて平面扱い
    すると、人が指した面と測る平面が食い違う(例外の出ない事故になる)。
    重心は `BRepGProp` の面積重心で、平面なら必ず面の上に乗る(`Face.center()` の既定
    (bbox中心)は傾いた平面では面から外れる —— `query.face_centroid` のdocstring)。
    """
    face = face_of(shape, face_index)
    kind = classify_face(face)
    if kind != "plane":
        raise ValueError(
            f"face {int(face_index)} is not planar (kind={kind!r}): 平面として測れるのは平らな面だけ"
        )
    _, centre = _face_props(face)
    normal = _face_normal(face)
    return ([float(v) for v in centre], [float(v) for v in normal])


def face_count(shape: "TopoDS_Shape") -> int:
    """重複を除いた面の数。**この数え方の1始まりの添字が、選択の識別子そのもの**。"""
    return _face_map(shape).Extent()


def tessellate(
    shape: "TopoDS_Shape",
    deviation: float = DEFAULT_DEVIATION,
    angular_deflection: float = DEFAULT_ANGULAR_DEFLECTION,
) -> dict:
    """形状を三角形分割し、**面ごとに分けた**三角形群を素の値だけで返す。

    面ごとに分けるのは、当たった三角形から面へ戻れるようにするため(1つの塊にまとめると
    戻れない)。添字は `face_count` と同じ `TopExp.MapShapes_s` の1始まりで、
    `extrude` の面の参照(`faces[].index`)とも一致する。

    三角形の巻き方向は、面の向きが `TopAbs_REVERSED` のとき入れ替える(外向きに揃える)。
    これをやらないと、アプリ側で法線が内側を向いた面が黒く落ちる。

    戻り値:
        {
          "deviation": float, "face_count": int,
          "bbox": {"min": [x,y,z], "max": [x,y,z], "center": [x,y,z], "diagonal": float},
          "faces": [{"index": int, "kind": str, "area": float, "centroid": [..],
                     "normal": [..], "positions": [x,y,z, ...], "indices": [i,j,k, ...]}],
          "untriangulated": [int, ...],   # 三角形が取れなかった面の添字(通常は空)
          "boundary": {...},              # 境界折れ線の交差検査と共有辺再分割の診断(`kernel_meshing.mesh_shape`)
        }

    `untriangulated` を戻り値に持たせているのは、**分割に失敗した面を黙って落とさない**ため。
    1面でも欠けると画面上はただ穴が空いて見え、原因の分からない不具合になる。
    全面が失敗した場合だけは形状として成立しないので例外にする。

    面分割の直前に境界折れ線の交差を独自に検査し、交差する辺を再分割してから分割する
    (`kernel_meshing`)。解消できなかった面は**分割せず** `untriangulated` に入れ、
    `boundary["unresolved"]` に理由(面・辺・種類)を残す —— 交差した境界を面分割へ渡すと
    穴や非多様体の三角形になり、`untriangulated` にも現れない(docs/TOPOPT_GAP_CAUSALITY.md)。
    """
    _check_tessellation_args(shape, deviation, angular_deflection)

    # 既存の三角形分割は `mesh_shape` が捨ててから測り直す(前回の粗いメッシュが残っていると
    # 偏差が効かない。STL書き出しは同じ形状を偏差を変えて何度も測る)。偏差は絶対mm
    # (Relative=False)。True にすると 0.2/0.05/0.01 のどれでも同じメッシュになる。
    # **build123d の既定の `export_stl` は True 側**なので、流用すると
    # 「deviationを小さくすると細かくなる」が落ちる。
    boundary = kernel_meshing.mesh_shape(shape, float(deviation), float(angular_deflection))

    shape_map = _face_map(shape)
    faces: List[dict] = []
    untriangulated: List[int] = []
    for index in range(1, shape_map.Extent() + 1):
        face = TopoDS.Face_s(shape_map.FindKey(index))
        payload = _describe_face(face, index)
        if not payload["indices"]:
            untriangulated.append(index)
        faces.append(payload)

    if faces and len(untriangulated) == len(faces):
        raise ValueError(
            f"三角形分割が1面も取れなかった(deviation={deviation}, angular={angular_deflection})"
        )

    return {
        "deviation": float(deviation),
        "face_count": len(faces),
        "bbox": _bounding_box(faces),
        "faces": faces,
        "untriangulated": untriangulated,
        "boundary": boundary,
    }


def _is_closed_shell(shape: "TopoDS_Shape") -> bool:
    """**閉じた立体か**(すべての辺が2面以上に共有されているか)。

    バックフェイスカリングの安全弁である。開いたシェル(板金の面1枚、STEPから来た
    面だけの部品)では**裏を向いた面が本当に見えている**ので、法線とカメラの内積だけで
    「裏だから粗くてよい」と決めてはいけない。閉じた立体でだけその判定を許す。

    数え方は**面ごとの出現回数**であって、辺の一意な隣接面の数ではない ——
    円筒のシーム辺は同じ1面に2回現れるので、一意な面で数えると閉じた円柱が
    「開いている」と判定される。退化辺(極)は数えない(面を持たない)。
    """
    if shape is None or shape.IsNull():
        return False
    edge_map = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    total_edges = edge_map.Extent()
    if total_edges == 0:
        return False
    face_map = _face_map(shape)
    if face_map.Extent() == 0:
        return False
    counts = [0] * (total_edges + 1)
    for index in range(1, face_map.Extent() + 1):
        # **面ごとの列挙は重複を除かない**(`subshapes`。`indexed_map` にしてはいけない —— シーム辺を2回数えるのが要点)
        for raw_edge in subshapes(face_map.FindKey(index), TopAbs_ShapeEnum.TopAbs_EDGE):
            edge = TopoDS.Edge_s(raw_edge)
            if not BRep_Tool.Degenerated_s(edge):
                slot = edge_map.FindIndex(edge)
                if slot > 0:
                    counts[slot] += 1
    return all(count == 0 or count >= 2 for count in counts[1:])


def _describe_face(face: Any, index: int) -> dict:
    """1面ぶんの配りもの(三角形・分類・面積・重心・法線)。`tessellate` と選択的版で共有する。"""
    positions, indices = _face_triangles(face)
    area, centroid = _face_props(face)
    return {
        "index": index,
        "kind": classify_face(face),
        "area": area,
        "centroid": list(centroid),
        "normal": list(_face_normal(face)),
        "positions": positions,
        "indices": indices,
    }


def tessellate_selective(
    shape: "TopoDS_Shape",
    fine_faces: Optional[Iterable[int]] = None,
    deviation: float = DEFAULT_DEVIATION,
    angular_deflection: float = DEFAULT_ANGULAR_DEFLECTION,
    coarse_deviation: Optional[float] = None,
    coarse_angular_deflection: Optional[float] = None,
) -> dict:
    """**面ごとに粗さを変えて**三角形分割する(視錐台/バックフェイスによる選択的高精度化)。

    `fine_faces` に入っている1始まりの面添字だけを `deviation` / `angular_deflection` で、
    それ以外を `coarse_deviation` / `coarse_angular_deflection` で測る。
    `fine_faces=None` は「全面を細かく」で、`tessellate()` と同じ測り方になる。

    戻り値は `tessellate()` と同じ形に次を足したもの:

        "deviation":        この配りものの中で**いちばん細かい**偏差[mm]
        "coarse_deviation": 粗い側の偏差[mm]
        "fine_face_count":  細かく測った面の数
        "closed":           閉じた立体か(`_is_closed_shell`。バックフェイス判定の可否)
        faces[i]["detail"]: "fine" | "coarse"

    **2パスで測る**。1回の面分割に面ごとの偏差を渡す口がOCCTに無いので、
    粗い面だけを集めたcompound・細かい面だけを集めたcompoundをそれぞれ測り、パスの間で
    三角形を抜き出す。`BRepTools.Clean_s` を**パスごとに**形状全体へ呼ぶのは、前のパスの三角形が
    残っているとOCCTが「もう十分細かい」と判断して測り直さないため。

    境界の交差検査・共有辺の再分割(`kernel_meshing`)はパスごとに compound の中で完結する。
    粗い面と細かい面が共有する辺は2つのパスで別々に離散化されるので、片方のパスで再分割しても
    もう片方には伝わらない(下記の「隙間」と同じ境目の話。`boundary["passes"]` に両方の診断を残す)。

    **分かっている代償(隙間)**: 細かい面と粗い面が**曲線の辺**を共有していると、辺の折れ線が
    パスごとに別々に作られるので、境目に粗い側の偏差ぶんの隙間が開きうる。境目は
    「視錐台の縁」と「シルエット」で、平面どうしの共有辺(直線)では起きない。
    塞ぐには境界の1リングを細かい側へ含めるなどが要るが、**画面の縁の話**なので入れていない。
    """
    _check_tessellation_args(shape, deviation, angular_deflection)

    shape_map = _face_map(shape)
    total = shape_map.Extent()
    coarse_dev = float(deviation if coarse_deviation is None else coarse_deviation)
    coarse_ang = float(
        angular_deflection if coarse_angular_deflection is None else coarse_angular_deflection
    )
    # **粗い側が細かい側を下回らない**。逆転すると「見えていない面の方が細かい」になる。
    coarse_dev = max(coarse_dev, float(deviation))
    coarse_ang = max(coarse_ang, float(angular_deflection))

    if fine_faces is None:
        fine = set(range(1, total + 1))
    else:
        fine = {int(index) for index in fine_faces if 1 <= int(index) <= total}

    # 1パスで済む場合は既存の道をそのまま通す(2つ目の経路を増やさない)。
    if not fine:
        result = tessellate(shape, coarse_dev, coarse_ang)
    elif len(fine) == total or (
        coarse_dev == float(deviation) and coarse_ang == float(angular_deflection)
    ):
        result = tessellate(shape, deviation, angular_deflection)
    else:
        result = _tessellate_split(
            shape, shape_map, fine, float(deviation), float(angular_deflection), coarse_dev, coarse_ang
        )

    for face in result["faces"]:
        face["detail"] = "fine" if face["index"] in fine else "coarse"
    result["deviation"] = float(deviation) if fine else coarse_dev
    result["coarse_deviation"] = coarse_dev
    result["fine_face_count"] = len(fine)
    result["closed"] = _is_closed_shell(shape)
    return result


def _tessellate_split(
    shape: "TopoDS_Shape",
    shape_map: "TopTools_IndexedMapOfShape",
    fine: set,
    deviation: float,
    angular_deflection: float,
    coarse_deviation: float,
    coarse_angular_deflection: float,
) -> dict:
    """`tessellate_selective` の2パス本体。**粗い側 → 細かい側**の順で測って抜き出す。"""
    total = shape_map.Extent()
    coarse = set(range(1, total + 1)) - fine
    described: Dict[int, dict] = {}
    untriangulated: List[int] = []
    passes: List[dict] = []

    for group_indices, dev, angle in (
        (coarse, coarse_deviation, coarse_angular_deflection),
        (fine, deviation, angular_deflection),
    ):
        if not group_indices:
            continue
        group = compound([shape_map.FindKey(index) for index in sorted(group_indices)])
        # **毎パスで形状全体を捨ててから測る**(残っていると「十分細かい」と見なされて測り直さない)。
        BRepTools.Clean_s(shape)
        passes.append(kernel_meshing.mesh_shape(group, float(dev), float(angle)))
        # **次のパスで Clean_s する前に**抜き出す。
        for index in sorted(group_indices):
            face = TopoDS.Face_s(shape_map.FindKey(index))
            payload = _describe_face(face, index)
            if not payload["indices"]:
                untriangulated.append(index)
            described[index] = payload

    faces = [described[index] for index in range(1, total + 1)]
    if faces and len(untriangulated) == len(faces):
        raise ValueError(
            f"三角形分割が1面も取れなかった(deviation={deviation}, coarse={coarse_deviation})"
        )
    return {
        "deviation": float(deviation),
        "face_count": len(faces),
        "bbox": _bounding_box(faces),
        "faces": faces,
        "untriangulated": sorted(untriangulated),
        "boundary": kernel_meshing.merge_diagnostics(passes),
    }


# --------------------------------------------------------------------------------------
# 面の添字引き(スケッチ平面と領域検出は kernel_sketch、押し出しは kernel_extrude)
# --------------------------------------------------------------------------------------
def face_of(shape: "TopoDS_Shape", face_index: int) -> Any:
    """形状の1始まり添字の面を返す。範囲外は ValueError。"""
    return _face_at(_face_map(shape), face_index)


def _face_at(shape_map: "TopTools_IndexedMapOfShape", face_index: int) -> Any:
    """既に作った面マップから引く(まとめて引くときにマップを作り直さないため。辺の `_edge_at` と同じ形)。"""
    if not 1 <= int(face_index) <= shape_map.Extent():
        raise ValueError(
            f"face index out of range: {face_index} (1..{shape_map.Extent()})"
        )
    return TopoDS.Face_s(shape_map.FindKey(int(face_index)))


def is_solid(shape: Any) -> bool:
    """形状に SOLID が1個以上含まれるか(`split_solids` と同じ数え方)。"""
    if shape is None or shape.IsNull():
        return False
    return indexed_map(shape, TopAbs_ShapeEnum.TopAbs_SOLID).Extent() > 0


def boolean_bodies(target_shape: Any, tool_shapes: Sequence[Any], mode: str) -> dict:
    """ターゲットとツール群で union/cut/intersect する。

    ツールは先に `BRepAlgoAPI_Fuse` で1つにまとめてからターゲットへ作用する
    (`extrude_regions` の領域融合と同じ)。intersect は `target ∩ (tool1 ∪ tool2 ∪ ...)`。

    戻り値: {"shape": TopoDS_Shape, "volume_before": float, "volume_after": float}
    """
    if target_shape is None or target_shape.IsNull():
        raise ValueError("boolean_bodies requires a target shape")
    if not tool_shapes:
        raise ValueError("boolean_bodies requires at least one tool shape")
    if mode not in ("union", "cut", "intersect"):
        raise ValueError(f"unknown boolean mode: {mode!r}")

    combined = _fuse_all(tool_shapes, "tool shapes")

    volume_before = volume(target_shape)
    if mode == "union":
        builder = BRepAlgoAPI_Fuse(target_shape, combined)
    elif mode == "cut":
        builder = BRepAlgoAPI_Cut(target_shape, combined)
    else:
        builder = BRepAlgoAPI_Common(target_shape, combined)
    builder.Build()
    if not builder.IsDone():
        raise ValueError(f"{mode} failed")
    result = builder.Shape()
    # 無変化は検査しない(`volume_before` を渡さない): 同じ体積で形だけ変わる intersect がある。
    volume_after = _checked_volume(result, mode)
    _require_valid(result, mode)

    return {
        "shape": result,
        "volume_before": volume_before,
        "volume_after": volume_after,
    }


# --------------------------------------------------------------------------------------
# フィレット・面取り・円弧の幾何
# --------------------------------------------------------------------------------------
#: 辺の種別(`classify_edge` が返す値。面の FACE_KINDS と同じ流儀で、判定はここに固定する)
EDGE_KINDS = ("line", "circle", "other")


def edge_count(shape: "TopoDS_Shape") -> int:
    """重複を除いた辺の数。**この数え方の1始まりの添字が、辺の選択の識別子そのもの**
    (面の `face_count` と同じ規律。`TopExp.MapShapes_s` を使う)。"""
    return _edge_map(shape).Extent()


def classify_edge(edge: Any) -> str:
    """1つの辺を EDGE_KINDS のいずれかへ分類する。

    円い辺のフィレットはトーラスを、円い辺の面取りは円錐を、まっすぐな辺の面取りは平面を作る。
    """
    adaptor = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
    curve_type = adaptor.GetType()
    if curve_type == GeomAbs_CurveType.GeomAbs_Line:
        return "line"
    if curve_type == GeomAbs_CurveType.GeomAbs_Circle:
        return "circle"
    return "other"


def edge_of(shape: "TopoDS_Shape", edge_index: int) -> Any:
    """形状の1始まり添字の辺を返す。範囲外は ValueError(面の `face_of` と同じ流儀)。"""
    return _edge_at(_edge_map(shape), edge_index)


def edge_index_of(shape: "TopoDS_Shape", edge: Any) -> int:
    """辺が形状の何番目か(1始まり)を `IsSame` で返す。無ければ ValueError。

    build123d の列挙順が `_edge_map` と一致する保証は無いので、`query.edges_of_face` は
    形状の同一性で引き当てる(既存の `edge_of` / `edge_count` のふるまいは変えない)。
    """
    shape_map = _edge_map(shape)
    for index in range(1, shape_map.Extent() + 1):
        if shape_map.FindKey(index).IsSame(edge):
            return index
    raise ValueError("edge is not part of the shape")


def tessellate_edges(
    shape: "TopoDS_Shape",
    deviation: float = DEFAULT_DEVIATION,
    angular_deflection: float = DEFAULT_ANGULAR_DEFLECTION,
) -> List[dict]:
    """辺を1始まりの添字つきの**折れ線**にする。

    `tessellate` の辺版。**辺も面と同じように配らないと人は辺を指せない**(画面に出ている
    輪郭線は three.js の `EdgesGeometry` による見た目であって、コアの辺の添字とは対応しない)。

    戻り値(素の値だけ):
        [{"index": int, "kind": "line"|"circle"|"other", "length": float,
          "closed": bool, "points": [x,y,z, x,y,z, ...]}, ...]

    `index` は `edge_count` / `edge_of` / `fillet(edges=[...])` と**同じ数え方**
    (`_edge_map` の1始まり)。ここがずれると「指した辺と違う辺が丸まる」という、例外も出ない
    静かな事故になるので、添字は必ず `_edge_map` から取る。

    折れ線は `GCPnts_TangentialDeflection` で `deviation`(mm、線形)と `angular_deflection`
    (ラジアン、角度)の**両方**を満たすまで分割する。**線形偏差だけでは足りない** ——
    小半径の円弧は角度の制約が無いと分割数が2〜4点まで落ち込んで多角形に見える
    (実測: 20mm立方体の全辺フィレットでr=0.5mmの円弧が2点=弦1本)。
    `angular_deflection` の既定値 `DEFAULT_ANGULAR_DEFLECTION` は `tessellate()` が
    `BRepMesh_IncrementalMesh` に渡すのと同じ値(ラジアン。OCCTの角度偏差パラメータは
    ラジアンで、既定0.5rad ≈ 28.6度。`IMeshTools_Parameters().Angle` の既定値で確認済み)
    なので、通常のソリッド表示と辺オーバーレイの粗さが揃う。
    `GCPnts_TangentialDeflection` のコンストラクタは `(adaptor, angular, linear,
    minNbPoints=2, ...)` の順で、`IsDone()` を持たない(常に何かしらの点列を作るか、
    退化した曲線では例外を投げる)。**分割に失敗した辺は端点だけの2点で返す**(黙って
    落とすと、その辺だけ画面から消えて「指せない辺」になり原因が分からない)。
    """
    _check_tessellation_args(
        shape, deviation, angular_deflection, target="the edges of an empty shape"
    )

    shape_map = _edge_map(shape)
    described: List[dict] = []
    for index in range(1, shape_map.Extent() + 1):
        edge = TopoDS.Edge_s(shape_map.FindKey(index))
        adaptor = BRepAdaptor_Curve(edge)
        points: List[float] = []
        try:
            sampler = GCPnts_TangentialDeflection(
                adaptor, float(angular_deflection), float(deviation)
            )
            n_points = sampler.NbPoints()
        except Exception:  # noqa: BLE001  OCCTは退化した曲線で例外を投げることがある
            n_points = 0
        if n_points >= 2:
            for number in range(1, n_points + 1):
                point = sampler.Value(number)
                points.extend([float(point.X()), float(point.Y()), float(point.Z())])
        else:
            for parameter in (adaptor.FirstParameter(), adaptor.LastParameter()):
                point = adaptor.Value(parameter)
                points.extend([float(point.X()), float(point.Y()), float(point.Z())])
        described.append(
            {
                "index": index,
                "kind": classify_edge(edge),
                "length": float(GCPnts_AbscissaPoint.Length_s(adaptor)),
                "closed": bool(edge.Closed()),
                "points": points,
            }
        )
    return described


def _edge_at(shape_map: "TopTools_IndexedMapOfShape", edge_index: int) -> Any:
    """既に作った辺マップから引く(まとめて引くときにマップを作り直さないため)。"""
    if not 1 <= int(edge_index) <= shape_map.Extent():
        raise ValueError(
            f"edge index out of range: {edge_index} (1..{shape_map.Extent()})"
        )
    return TopoDS.Edge_s(shape_map.FindKey(int(edge_index)))


# --------------------------------------------------------------------------------------
# 頂点(選択集合に入った頂点を画面に出すための座標)
# --------------------------------------------------------------------------------------
def _vertex_map(shape: "TopoDS_Shape") -> "TopTools_IndexedMapOfShape":
    """頂点の重複除去済みマップ(1始まり)。**`count_subshapes(shape, "vertex")` と同じ
    数え方**であり、その添字が `selection.select_vertex` の識別子そのものになる。"""
    return _subshape_map(shape, "vertex")


def vertex_positions(shape: "TopoDS_Shape") -> List[dict]:
    """頂点を1始まりの添字つきの3D座標で返す。

    戻り値(素の値だけ): `[{"index": int, "point": [x, y, z]}, ...]`

    添字がずれると**AIが指した頂点と違う点が画面に出る**(例外は出ない)ので、
    数え方は `_vertex_map` 1箇所に固定する。`tests/test_vertex_pick.py` が
    `count_subshapes(shape, "vertex")` との一致を機械で見ている。
    """
    shape_map = _vertex_map(shape)
    described: List[dict] = []
    for index in range(1, shape_map.Extent() + 1):
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(shape_map.FindKey(index)))
        described.append(
            {"index": index, "point": [float(point.X()), float(point.Y()), float(point.Z())]}
        )
    return described


def fillet_edges(shape: "TopoDS_Shape", radii: dict) -> dict:
    """辺(1始まりの添字)を丸めた**新しい形状**と由来の射影を返す。

    `radii` は `{辺の添字: 半径}`。入力の `shape` は変更しない。

    **必ず `IsDone()` を確認する**。OCCTのフィレットは例外を投げずに失敗し、しかも
    半径に対して単調でない(20mm立方体の全辺で実測: r=3.0 成功 / r=10.0
    `IsDone()=False` / r=10.5 成功 / r=30.0 `IsDone()=False`)。失敗は例外にして
    呼び出し側が操作ごと捨てられるようにする。

    **限界値ちょうどの半径は通らず、限界未満では微小な平面が残る**(20mm立方体の上面4辺で
    r=9.99 は成功して 0.02mm 角の面が残り、r=10.0 は `IsDone()=False`。20×10×20 の長辺2本でも
    r=4.999 成功 / 5.0 失敗。面取りも距離 4.99 成功 / 5.0 失敗)。**参照している面の面積を厳密に
    0にする状況はフィレット・面取りでは作れない** —— 回帰題材が要るなら切削(対象面より大きい
    領域で切り下げる)で作る。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape": <TopoDS_Shape>, "provenance": {...}, "volume_before": float,
         "volume_after": float}
    """
    return _blend_edges(shape, radii, verb="fillet")


def chamfer_edges(shape: "TopoDS_Shape", distances: dict) -> dict:
    """辺(1始まりの添字)を面取りした新しい形状と由来の射影を返す。

    `distances` は `{辺の添字: 距離}`。`fillet_edges` と同じ契約(`IsDone()` の確認・
    入力を変更しない・戻り値の形)。`BRepFilletAPI_MakeChamfer` は同じ系統なので、
    **黙って失敗する性質も同じ**。
    """
    return _blend_edges(shape, distances, verb="chamfer")


def _blend_edges(shape: "TopoDS_Shape", values: dict, verb: str) -> dict:
    """フィレットと面取りの共通実装(違うのはビルダの型だけ)。

    **同じ系統(`BRepFilletAPI_*`)なので失敗の仕方も由来の取り出し方も同じ**であり、
    2つに書き分けると片方だけ検査が抜ける。
    """
    if shape is None or shape.IsNull():
        raise ValueError(f"cannot {verb} an empty shape")
    if not values:
        raise ValueError(f"{verb} requires at least one edge")

    edge_map = _edge_map(shape)
    volume_before = volume(shape)
    maker = (
        BRepFilletAPI_MakeFillet(shape)
        if verb == "fillet"
        else BRepFilletAPI_MakeChamfer(shape)
    )
    edge_indices: List[int] = []
    for raw_index, raw_value in values.items():
        index = int(raw_index)
        maker.Add(float(raw_value), _edge_at(edge_map, index))
        edge_indices.append(index)

    try:
        maker.Build()
    except Exception as error:  # noqa: BLE001  OCCTは例外でも失敗しうる
        raise ValueError(f"{verb} failed while building (edges {edge_indices}): {error}") from error
    if not maker.IsDone():
        # **ここが最重要**: OCCTは例外を投げずに失敗する。半径/距離の範囲検査では代用できない
        raise ValueError(
            f"{verb} failed (IsDone is false) for edges {edge_indices} with {values}"
        )

    final = maker.Shape()
    context = f"edges {edge_indices}"
    volume_after = _checked_volume(final, verb, context=context)
    _require_valid(final, verb, context)

    return {
        "shape": final,
        "provenance": _blend_provenance(maker, shape, final, verb, edge_indices),
        "volume_before": float(volume_before),
        "volume_after": float(volume_after),
    }


def _blend_provenance(
    maker: Any,
    shape: "TopoDS_Shape",
    final: "TopoDS_Shape",
    verb: str,
    edge_indices: List[int],
) -> dict:
    """由来を**正規化アダプタ経由で**回収し、JSON化できる射影へ落とす。

    フィレット系は `History()` を持たず `Modified()` / `Generated()` / `IsDeleted()` だけを
    持ち、ブーリアンは `History()` を持つ ——**その差を吸収するのが `normalize_provenance` の
    存在理由**である。アダプタの戻りは `TopoDS_Shape` を含む**メモリ上の形**なので、そのままでは
    `OpLog.to_json()` で落ちる。ここで添字の射影に変換する(`extrude_faces` と同じ形)。
    """
    normalized = normalize_provenance(maker, [shape]) or {}
    modified = {}
    for entry in normalized.get("modified", []):
        modified[entry["input"]] = entry["results"]
    deleted_inputs = normalized.get("deleted", [])

    shape_map = _face_map(shape)
    final_map = _face_map(final)
    successors: dict = {}
    for index in range(1, shape_map.Extent() + 1):
        old_face = shape_map.FindKey(index)
        candidates = _lookup_by_identity(modified, old_face)
        if candidates is None:
            # アダプタが「触っていない」と言った面は、そのまま残っているはず
            candidates = [] if _contains_identity(deleted_inputs, old_face) else [old_face]
        successors[str(index)] = _positions_in(final_map, candidates)

    return {
        "verb": verb,
        "edges": list(edge_indices),
        **_face_map_projection(successors, final_map.Extent()),
    }


def _lookup_by_identity(mapping: dict, key: Any) -> Optional[List[Any]]:
    """`TopoDS_Shape` は辞書の鍵として同一性で引けないので `IsSame` で探す。"""
    for candidate, results in mapping.items():
        if candidate.IsSame(key):
            return results
    return None


def _contains_identity(shapes: Sequence[Any], key: Any) -> bool:
    return any(candidate.IsSame(key) for candidate in shapes)


def _edge_map(shape: "TopoDS_Shape") -> "TopTools_IndexedMapOfShape":
    """辺の重複除去済みマップ(1始まり)。添字が選択の識別子になるので数え方はここに固定する。"""
    return _subshape_map(shape, "edge")


# --------------------------------------------------------------------------------------
# ソリッド分解・複数形状の書き出し
# --------------------------------------------------------------------------------------
def split_solids(shape: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """形状をSOLIDごとに分解する。

    `TopExp.MapShapes_s(shape, TopAbs_SOLID)` で重複除去済みのSOLID群を取り出し、
    各SOLIDを個別の TopoDS_Shape として返す。**SOLIDが1つも無い形状(裸の面・シェルのみ)は
    形状全体を1要素のリストにして返す**(0個のBodyを黙って作らない。io_step.pyの契約)。

    列挙順は `TopExp.MapShapes_s` の走査順(同じ形状に対して決定的)。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot split an empty shape")
    shape_map = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    if shape_map.Extent() == 0:
        return [shape]
    return [
        TopoDS.Solid_s(shape_map.FindKey(index))
        for index in range(1, shape_map.Extent() + 1)
    ]


def make_compound(shapes: Sequence["TopoDS_Shape"]) -> "TopoDS_Shape":
    """複数の形状を1つの TopoDS_Compound へまとめる(複数Bodyを1つのSTEP/STLへ書き出すため)。"""
    if not shapes:
        raise ValueError("cannot build a compound from an empty sequence of shapes")
    return compound(shapes)


# --------------------------------------------------------------------------------------
# 内部ヘルパ
# --------------------------------------------------------------------------------------
def _subshape_map(shape: "TopoDS_Shape", kind: str) -> "TopTools_IndexedMapOfShape":
    """`kind`("face" | "edge" | "vertex")の重複除去済みマップ(1始まり)。**添字が選択の識別子になる**
    ので数え方はここ1箇所に固定する。null は ValueError(`count_subshapes` と違って 0 を返さない)。"""
    enum, plural = _SUBSHAPE_KINDS[kind]
    if shape is None or shape.IsNull():
        raise ValueError(f"cannot map the {plural} of an empty shape")
    return indexed_map(shape, enum)


def _face_map(shape: "TopoDS_Shape") -> "TopTools_IndexedMapOfShape":
    """面の重複除去済みマップ(1始まり)。"""
    return _subshape_map(shape, "face")


def _face_normal(face: Any) -> tuple:
    """面の中央での**外向き**単位法線。`BRepGProp_Face` は面の向きを見るので反転面でも外を向く
    (20mm立方体の6面で実測: x=0の面が (-1,0,0)、x=20の面が (1,0,0))。"""
    gface = BRepGProp_Face(TopoDS.Face_s(face))
    u_min, u_max, v_min, v_max = gface.Bounds()
    point = gp_Pnt()
    normal = gp_Vec()
    gface.Normal((u_min + u_max) / 2.0, (v_min + v_max) / 2.0, point, normal)
    if normal.Magnitude() <= 0:
        raise ValueError("face normal is degenerate (magnitude 0)")
    normal.Normalize()
    return (normal.X(), normal.Y(), normal.Z())


def _face_props(face: Any) -> tuple:
    """(面積, 重心)。`BRepGProp.SurfaceProperties_s` の既定積分を使う(volume と同じ流儀)。"""
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(TopoDS.Face_s(face), props)
    centre = props.CentreOfMass()
    return props.Mass(), (centre.X(), centre.Y(), centre.Z())


def _face_triangles(face: Any) -> tuple:
    """1面の三角形を (positions, indices) で返す。indices はこの面の中での0始まり。

    節点は面のローカル座標なので `TopLoc_Location` の変換を必ず適用する(単位変換のかかった
    STEPや配置された部品で位置がずれる)。巻き方向は `TopAbs_REVERSED` のとき入れ替える。
    """
    location = TopLoc_Location()
    triangulation = BRep_Tool.Triangulation_s(TopoDS.Face_s(face), location)
    if triangulation is None:
        return [], []

    is_identity = location.IsIdentity()
    transformation = location.Transformation()
    positions: List[float] = []
    for node_index in range(1, triangulation.NbNodes() + 1):
        node = triangulation.Node(node_index)
        if not is_identity:
            node = node.Transformed(transformation)
        positions.extend((node.X(), node.Y(), node.Z()))

    is_reversed = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
    indices: List[int] = []
    for triangle_index in range(1, triangulation.NbTriangles() + 1):
        first, second, third = triangulation.Triangle(triangle_index).Get()
        if is_reversed:
            second, third = third, second
        indices.extend((first - 1, second - 1, third - 1))
    return positions, indices


def _bounding_box(faces: List[dict]) -> dict:
    """三角形の節点から境界箱を作る(アプリ層のカメラ合わせ用)。面が無ければ原点の退化した箱。"""
    lows = [float("inf")] * 3
    highs = [float("-inf")] * 3
    for face in faces:
        positions = face["positions"]
        for offset in range(0, len(positions), 3):
            for axis in range(3):
                value = positions[offset + axis]
                lows[axis] = min(lows[axis], value)
                highs[axis] = max(highs[axis], value)
    if any(low == float("inf") for low in lows):
        lows, highs = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    return meshbody.bbox_payload(lows, highs)


def _unify_same_domain(shape: "TopoDS_Shape", volume_before: float) -> tuple:
    """同一平面の面を統合した形状・その履歴・採用したかを返す。

    採用しない場合は**統合を一度も通していない元の形状**と履歴 None を返す
    (由来の射影はブーリアンの結果へ直接張る)。

    **`ShapeUpgrade_UnifySameDomain` は入力の形状を壊す**(実測・最重要):

        fuse の結果        -> IsValid()=True
        unify.Build() した後、**同じ fuse の結果を測り直すと** IsValid()=False
        unify の前に取った BRepBuilderAPI_Copy は unify 後も IsValid()=True

    つまり「統合してみて、ダメだったら統合前に戻る」を素直に書くと、**戻り先が既に壊れている**。
    そのため必ず**コピーで試してから**本番を行う:

    1. コピーに対して統合を試し、(a)妥当 (b)体積が変わらない (c)**面が実際に減る** を確かめる
    2. 3つとも満たすときだけ、元の形状に対して本番の統合を行い、その出力と履歴を返す
       (由来の射影は「ブーリアンの結果の部分形状」を鍵に引くので、本番は必ず元の形状に対して
       行う必要がある。コピーに対して統合すると部分形状の同一性が切れて射影が壊れる)
    3. 満たさないときは元の形状をそのまま返す(統合を通していないので無傷)

    (c)を条件に入れているのは、面が減らない統合は利得ゼロで**入力を壊す危険だけが残る**ため。
    `shelf_attachment_GD.step` の面36・43を+2mmしたときが実際にこれで、面数は57のまま変わらず、
    統合後の形状だけが壊れていた。20mm立方体の上面+10では 10面 -> 6面 に減るので採用される。
    """
    trial = ShapeUpgrade_UnifySameDomain(
        BRepBuilderAPI_Copy(shape).Shape(), True, True, False
    )
    trial.Build()
    candidate = trial.Shape()
    if candidate is None or candidate.IsNull():
        return shape, None, False
    if abs(volume(candidate) - volume_before) > 1e-6 * max(1.0, abs(volume_before)):
        return shape, None, False
    if not BRepCheck_Analyzer(candidate).IsValid():
        return shape, None, False
    if _face_map(candidate).Extent() >= _face_map(shape).Extent():
        return shape, None, False

    unify = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    unify.Build()
    unified = unify.Shape()
    if unified is None or unified.IsNull() or not BRepCheck_Analyzer(unified).IsValid():
        # 試行では通ったのに本番で壊れた場合。入力(shape)はもう壊れているので操作ごと捨てる。
        raise ValueError("ShapeUpgrade_UnifySameDomain が試行と異なる結果を返した")
    return unified, unify.History(), True

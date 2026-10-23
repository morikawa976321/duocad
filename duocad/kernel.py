"""OCP(OCCT)を直接叩く層。

**duocad配下でOCP.*をimportしてよいのはカーネル層だけ**(verbが直接OCPを叩き始めるのを
防ぐための境界。許可する表の正は README「モジュールとimport境界」)。

戻り値はいずれも **JSON化できる素の値(float/int/str/list/dict)だけ**にする。OCPのオブジェクトを
kernel の外へ出さないための規律で、境界テスト(importの静的検査)では捕まえられない側の約束。
唯一の例外は `kernel_extrude.extrude_faces` / `kernel_extrude.extrude_regions` / `face_of`
(と `kernel_sketch.sketch_regions`)の戻りに含まれる TopoDS_*(document.Body.shape や領域押し出しの入力になる値)。

**ocp_tessellate は使わない**(この venv に入っておらず、面ごとの三角形グループが要るため自前実装)。
"""
from __future__ import annotations

import math
import os
import tempfile
import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

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
from OCP.BRepGProp import BRepGProp, BRepGProp_Domain, BRepGProp_Face, BRepGProp_Vinert
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
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import (
    TopTools_IndexedDataMapOfShapeListOfShape,
    TopTools_IndexedMapOfShape,
)
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

from . import kernel_chains, kernel_meshing, kernel_overflow, kernel_tight, meshbody
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
    return _load_step(os.fspath(path))[1]


def _load_step(filename: str) -> tuple:
    """STEPリーダを立てる**唯一の場所**。`(リーダ, 形)` を返す。検査の中身は `read_step` のdocstring。"""
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
    return reader, shape


def read_step_text(text: str) -> "TopoDS_Shape":
    """**STEP本文の文字列**を読み、TopoDS_Shapeを返す。壊れていれば ValueError。

    操作ログに埋め込んだSTEP本文(verb `import_step`)の再生用。**OCCTのSTEPリーダは
    ファイルパスしか受け取らない**ので、一時ファイルへ書き出して `read_step` を通す
    (別経路のリーダを立てると ReadFile の検査を飛ばして落ちる経路ができる)。
    一時ディレクトリは読み終えたら消す(形状はOCCTのメモリ上に構築済みで戻り値は有効)。

    書き出しは**バイト列で行う**(`write_text` は Windows で `\\n` を `\\r\\n` へ
    書き換えるため、`\\r\\n` を含む元のSTEPが `\\r\\r\\n` に化ける)。
    """
    return _load_step_text(text)[1]


def _load_step_text(text: str) -> tuple:
    """`_load_step` を、本文を一時ファイルへ書いて通す。`(リーダ, 形)`。"""
    if not isinstance(text, str):
        raise TypeError(f"STEP本文は文字列で渡すこと: {type(text).__name__}")
    if text.strip() == "":
        # 「埋め込みが空だった」という原因を言うため、OCCTへ渡す前に止める。
        raise ValueError("埋め込まれたSTEP本文が空")
    with tempfile.TemporaryDirectory(prefix="duocad-step-") as tmpdir:
        staging = os.path.join(tmpdir, "embedded.step")
        with open(staging, "wb") as handle:
            handle.write(text.encode("utf-8"))
        try:
            return _load_step(staging)
        except (FileNotFoundError, ValueError) as error:
            # 一時パスをそのまま見せない(利用者にとって意味の無い場所を指すため)。
            raise ValueError(
                f"埋め込まれたSTEP本文をSTEPとして読めない({len(text)}文字): {error}"
            ) from error


def step_import_problems(text: str) -> List[str]:
    """STEP本文を取り込むと**黙って欠ける**ものを、人が読む文の一覧にする。欠けなければ空。

    1. **転送に失敗した要素**: 一部の面・要素(例: 未対応の面種、存在しない面への参照)の転送が失敗しても、ルートが
       1つでも転送できれば `read_step` は成功を返し、面が落ちた開いたシェルが solid として入る。`NbRootsForTransfer` との
       比較では見えない(失敗はルートの中の面で起きる)。転送のチェック一覧(`TransientProcess.CheckList`)の失敗が空でなければ数える。
    2. **ソリッドの外の面**: ソリッドを1つでも含むと `split_solids` はソリッドしか返さず、ほかの面・シェルは捨てられる。
       全面数のうちソリッドに入らない面を数える(各ボディの面数の合計が分解前の全面数に合わない)。
       ソリッドが無い形は形全体を1ボディにするので数えない。ワイヤ・辺だけのものは面が無いので見えない。

    **取り込み(`io_step.read_step_source`)でだけ呼ぶ。再生は呼ばない**: 検査を足す前に取り込んだ形を含む保存済みの文書を、
    そのまま開けなければならない。形は読み直す(`read_step_text` と別の読み込みになる)。
    """
    reader, shape = _load_step_text(text)
    problems: List[str] = []

    failures = reader.WS().TransferReader().TransientProcess().CheckList(True)
    count, reasons = 0, []
    failures.Start()
    while failures.More():
        check = failures.Value()
        count += check.NbFails()
        reasons.extend(check.CFail(index) for index in range(1, check.NbFails() + 1))
        failures.Next()
    if count:
        shown = "; ".join(dict.fromkeys(reason.strip() for reason in reasons if reason.strip()))
        problems.append(f"STEP の要素 {count} 件を転送できず、取り込むと面が欠けた形になる({shown[:200]})")

    solids = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    if solids.Extent() > 0:
        faces = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_FACE)
        inside = set()
        for index in range(1, solids.Extent() + 1):
            solid_faces = indexed_map(solids.FindKey(index), TopAbs_ShapeEnum.TopAbs_FACE)
            inside.update(faces.FindIndex(solid_faces.FindKey(number)) for number in range(1, solid_faces.Extent() + 1))
        inside.discard(0)
        loose = faces.Extent() - len(inside)
        if loose > 0:
            problems.append(
                f"ソリッド {solids.Extent()} 個の外に面が {loose} 枚ある(全体 {faces.Extent()} 面のうちソリッドに入るのは "
                f"{faces.Extent() - loose} 面)。取り込むとそれらは黙って落ちる"
            )
    return problems


def write_step(shape: "TopoDS_Shape", path: str) -> None:
    """形状をSTEPとして書き出す(モードは AsIs)。書き出しに失敗したらファイルを残さない。"""
    if shape is None or shape.IsNull():
        raise ValueError("cannot write an empty shape to STEP")

    filename = os.fspath(path)
    writer = STEPControl_Writer()
    # Transfer の戻り値を捨てると「転送に失敗したのに Write は成功した(=中身の無いSTEP)」を通しうる。
    transfer_status = writer.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
    if transfer_status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise ValueError(f"failed to transfer shape to STEP: {filename} (Transfer -> {transfer_status})")

    status = writer.Write(filename)
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        _unlink_quietly(filename)
        raise ValueError(f"failed to write STEP: {filename} (Write -> {status})")


def _unlink_quietly(filename: str) -> None:
    """書き出し失敗時に半端なファイルを残さない。"""
    try:
        os.remove(filename)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# 計測・分類(query.py から使う。OCPを触るのでここに置く)
# --------------------------------------------------------------------------------------
#: 体積・重心を精度指定の適応積分で測る面: 曲面がこれらの種類か、境界の辺がこれらの種類の平面(`_needs_adaptive`)。既定の積分は
#: 細い楕円を傾けた B スプラインの側面と、B スプラインの曲線で囲まれた平面のふたで 3〜152% ずれ、適応積分は押し出し面
#: (楕円柱の側面)で重心を 0.02 mm ずらす(どちらも 2026-10-19 実測、sol レビュー)。
_ADAPTIVE_SURFACES = (
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface,
    GeomAbs_SurfaceType.GeomAbs_BezierSurface,
)
_ADAPTIVE_CURVES = (
    GeomAbs_CurveType.GeomAbs_BSplineCurve,
    GeomAbs_CurveType.GeomAbs_BezierCurve,
    GeomAbs_CurveType.GeomAbs_OffsetCurve,
    GeomAbs_CurveType.GeomAbs_OtherCurve,
)

#: 適応積分の相対精度。
_ADAPTIVE_VOLUME_EPS = 1.0e-9


def volume(shape: "TopoDS_Shape") -> float:
    """形状の体積(mm^3)。

    `BRepGProp.VolumeProperties_s(shape, props)` の**既定の積分**を使う(`Eps` を渡さない)。`Eps` 付きの適応積分に
    すると精度が落ちる(Eps=1e-9で相対1.8e-10、Eps=1e-6で相対3.7e-8)。**ただし B スプライン・ベジエの面か辺を含む形は、
    面ごとに積分を選んで足す**(`_needs_adaptive` の面だけ `Eps=1e-9` の適応積分。`_volume_props`)。

    STEP往復を挟んだ体積は**bit-exactではない**(実測相対誤差 約8e-16)。比較には
    `math.isclose(rel_tol=1e-6)` を使う。面数は往復後も厳密一致(`==`)でよい。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot compute the volume of an empty shape")
    return _volume_props(shape).Mass()


def _volume_props(shape: "TopoDS_Shape") -> "GProp_GProps":
    """体積の積分(質量 = 体積、重心)。適応積分の要る面が無ければ形全体を既定の積分で1回、あれば**閉じた殻ごとに**
    その殻の境界箱の中心を基準点として面ごとに積分を選び、寄与を足す(`BRepGProp.VolumeProperties_s` の内側と同じ組み立て。
    体積の積分は閉じた殻ごとに成り立つ)。

    - 面を1枚ずつ `VolumeProperties_s` に渡してはいけない: 基準点が面の位置変換に合わせて動き、寄与を足しても体積に
      ならない(傾けた楕円の上下の平面のふたが 0 を返した。実測)。
    - 基準点を原点や形全体の境界箱の中心にすると、そこから遠い薄い殻で桁が落ちる(Z=100 の 12×6 の楕円を 0.001 mm 押して
      体積 1.3% ずれ、離れた2つの殻を持つボディで重心が 0.24 mm ずれた。どちらも sol レビューで再現)。
    """
    faces = _faces_of(shape)
    if not any(_needs_adaptive(face) for face in faces):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props)
        return props
    shells = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SHELL)
    while explorer.More():
        shells.append(explorer.Current())
        explorer.Next()
    props = GProp_GProps(_box_centre(shape))
    for shell in shells or [shape]:
        centre = _box_centre(shell)
        part = GProp_GProps(centre)
        for face in _faces_of(shell):
            surface = BRepGProp_Face(face)
            domain = BRepGProp_Domain(face)
            if _needs_adaptive(face):
                part.Add(BRepGProp_Vinert(surface, domain, centre, _ADAPTIVE_VOLUME_EPS))
            else:
                part.Add(BRepGProp_Vinert(surface, domain, centre))
        props.Add(part)
    return props


def _faces_of(shape: Any) -> List[Any]:
    out = []
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    while explorer.More():
        out.append(TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    return out


def _box_centre(shape: Any) -> "gp_Pnt":
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return gp_Pnt((xmin + xmax) / 2.0, (ymin + ymax) / 2.0, (zmin + zmax) / 2.0)


def _needs_adaptive(face: Any) -> bool:
    """B スプライン・ベジエの曲面か、自由曲線で囲まれた平面(実測で既定の積分が外れた面)。押し出し面などの
    他の曲面は既定のまま(適応積分は楕円柱の側面で重心をずらす)。"""
    kind = BRepAdaptor_Surface(face).GetType()
    if kind in _ADAPTIVE_SURFACES:
        return True
    if kind != GeomAbs_SurfaceType.GeomAbs_Plane:
        return False
    explorer = TopExp_Explorer(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    while explorer.More():
        if BRepAdaptor_Curve(TopoDS.Edge_s(explorer.Current())).GetType() in _ADAPTIVE_CURVES:
            return True
        explorer.Next()
    return False


def centroid(shape: "TopoDS_Shape") -> List[float]:
    """形状の**体積重心**(mm)。`volume` と同じ積分(`_volume_props`)を使う。

    `move_body` の回転の既定の中心。境界箱の中心は軸の向きで動くので使わない。

    体積が0の形状(面だけのシェル等)は ValueError(`Mass()` が0だと `CentreOfMass()` は無意味な点を返す)。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot compute the centroid of an empty shape")
    props = _volume_props(shape)
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
    `__eq__` が False を返し、`dict` も `lru_cache` も**ヒット率0%のまま動き続ける**(2026-09-07実測)。

    同一性は `IsSame`(TShape と Location の一致)で見る。`move_body` の `Moved` は TShape を
    共有するが Location が違うので別扱いになる(実測: 1000mm 動かすと `hash` 不一致・`IsSame` False)。

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

    `Document` が1つ持ち(`document.bbox_cache`)、文書を差し替えると作り直される
    (モジュールに1つ置くと別の文書の形状が積み上がる)。

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

    `cache` に `document.bbox_cache` を渡すと測り直しを避ける。戻り値の dict は毎回作り直す。
    `kernel.tessellate` の戻りの `bbox` は三角形の節点から作った別物(カメラ合わせ用)。
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
    (shelf_attachment_GD.step 実測: 重複除去66 / 素朴な列挙132)。NULL 形状は 0(ValueError にしない)。
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
    """`BRepCheck_Analyzer` の最後の関門。体積検査を通っても壊れていることがある(曲面の掃引で実測)。
    `_checked_volume` と分けてあるのは、押し出しと union では間に `_unify_same_domain` が入るため。"""
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
    (押し出し・`boolean_bodies`・`_blend_provenance` が同じ形で保存する)。"""
    mapped = {position for positions in successors.values() for position in positions}
    return {
        "face_map": successors,
        "deleted": [int(key) for key, value in successors.items() if not value],
        "new_faces": [i for i in range(1, final_count + 1) if i not in mapped],
    }


def _target_successors(
    builder: Any, target_shape: "TopoDS_Shape", history: Any, final_map: "TopTools_IndexedMapOfShape"
) -> dict:
    """`target_shape` の各面が、ブーリアン(`builder`)と統合(`history`。統合しなければ None)を経て
    `final_map` の何番になったか(旧添字の文字列 → 新添字の列)。ツール側の面は鍵に入らない。"""
    target_map = _face_map(target_shape)
    successors: dict = {}
    for index in range(1, target_map.Extent() + 1):
        old_face = target_map.FindKey(index)
        candidates = _shape_list(builder, "Modified", old_face)
        if not candidates and not _is_removed(builder, old_face):
            candidates = [old_face]
        successors[str(index)] = _positions_in(
            final_map,
            (survivor for candidate in candidates for survivor in _through_history(history, candidate)),
        )
    return successors


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


def _positions_in(final_map: "TopTools_IndexedMapOfShape", candidates: Iterable[Any]) -> List[int]:
    """`candidates` が `final_map` の何番目かを重複なしで(最初に現れた順に)集める。"""
    resolved: List[int] = []
    for candidate in candidates:
        position = final_map.FindIndex(candidate)
        if position > 0 and position not in resolved:
            resolved.append(position)
    return resolved


# --------------------------------------------------------------------------------------
# 由来(provenance)の正規化アダプタ
# --------------------------------------------------------------------------------------
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
    """由来を問い合わせる部分形状(面・辺)を重複なしで列挙する。"""
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
#: 既定の線形偏差(mm)。絶対偏差(isRelative=False)。
DEFAULT_DEVIATION = 0.2
#: 既定の角度偏差(**ラジアン**。OCCTの角度偏差パラメータはラジアンで、OCCT既定は0.5rad)。0.35radは約20度。
DEFAULT_ANGULAR_DEFLECTION = 0.35


def translate_shape(
    shape: "TopoDS_Shape", dx: float, dy: float, dz: float, *, copy: bool = False
) -> "TopoDS_Shape":
    """形状を (dx, dy, dz) だけ平行移動した形状を返す(元の形状は変えない)。

    **面・辺・頂点の添字が変わらないこと**が要点。`move_body` の後も選択の参照が同じ実体を
    指し続ける(`catalog.Command.resets_selection` の注記)。

    - `copy=False` … `TopoDS_Shape.Moved` で**位置だけを差し替える**。TShape を共有するので
      面の数え上げの順序は不変で、再計算も走らない。
    - `copy=True`  … `BRepBuilderAPI_Transform(..., Copy=True)` で**独立した形状**を作る。
      実体を共有すると、後から片方をフィレットしたときにもう片方の由来が壊れる。

    平行移動の合成は厳密(`T(-d) * T(d)` は誤差なく単位変換)なので、`move_body` の undo は逆向きの平行移動で済む。
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

    `translate_shape` の姉妹(`copy` の意味・添字不変も同じ)。**角度は度**(`gp_Trsf.SetRotation` は
    ラジアンなのでここで変換する)。符号は軸の向きに対する**右ねじ**で、スケッチ側の
    `sketch.rotate_point`(u->v が正)と同じ向き。

    逆回転は元へ戻る(`R(-a)` は `R(a)` の転置で、積は丸め誤差 相対1e-16程度を除き単位行列)ので、
    `move_body` の undo は「逆向きに同じ角度」で済む。

    軸方向が零ベクトル・非有限なら ValueError。
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
    shape: "TopoDS_Shape", point: Sequence[float], normal: Sequence[float]
) -> "TopoDS_Shape":
    """形状を、点 `point` を通り法線 `normal` の平面に対して鏡映した形状を返す(元は変えない)。

    **鏡映は向きを反転する変換**(行列式が負)。`mirror_body` は常に複製を作るので、常に独立した形状を返す。

    点・法線が3要素でない/非有限、または法線が零ベクトルなら ValueError。
    """
    origin, axis = _point_and_direction(
        point, normal, names=("point", "normal"), zero_reason="鏡映面の向きが決まらない"
    )

    trsf = gp_Trsf()
    trsf.SetMirror(gp_Ax2(gp_Pnt(*origin), gp_Dir(*axis)))
    return _transformed(shape, trsf, copy=True, what="鏡映")


def edge_axis(shape: "TopoDS_Shape", edge_index: int) -> tuple:
    """**まっすぐな辺**を回転軸 `([x, y, z], [dx, dy, dz])` にする(点は辺の上、向きは単位長)。

    添字は `edge_count` / `edge_of` と同じ1始まり(辺の選択の識別子そのもの)。
    まっすぐでない辺(円・spline)は ValueError(中心線を勝手に作ると指した辺と回る軸が食い違う)。
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


def edge_circle(shape: "TopoDS_Shape", edge_index: int) -> tuple:
    """**円(円弧を含む)の辺**の `([cx, cy, cz], radius)`(ワールド座標[mm])。

    添字は `edge_count` / `edge_of` と同じ1始まり。円でない辺は ValueError。
    """
    edge = edge_of(shape, edge_index)
    if classify_edge(edge) != "circle":
        raise ValueError(f"edge {int(edge_index)} is not a circle (kind={classify_edge(edge)!r})")
    circle = BRepAdaptor_Curve(edge).Circle()
    center = circle.Location()
    return ([float(center.X()), float(center.Y()), float(center.Z())], float(circle.Radius()))


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

    添字は `face_count` / `face_of` と同じ1始まり。平面でない面は ValueError(曲面を平面扱いすると
    指した面と測る平面が食い違う)。重心は `BRepGProp` の面積重心で、平面なら必ず面と同じ平面に乗る。
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

    添字は `face_count` と同じ `TopExp.MapShapes_s` の1始まりで、`extrude` の面の参照(`faces[].index`)とも一致する。

    三角形の巻き方向は、面の向きが `TopAbs_REVERSED` のとき入れ替える(外向きに揃える。
    しないとアプリ側で法線が内側を向いた面が黒く落ちる)。

    戻り値:
        {
          "deviation": float, "face_count": int,
          "bbox": {"min": [x,y,z], "max": [x,y,z], "center": [x,y,z], "diagonal": float},
          "faces": [{"index": int, "kind": str, "area": float, "centroid": [..],
                     "normal": [..], "positions": [x,y,z, ...], "indices": [i,j,k, ...]}],
          "untriangulated": [int, ...],   # 三角形が取れなかった面の添字(通常は空)
          "boundary": {...},              # 境界折れ線の交差検査と共有辺再分割の診断(`kernel_meshing.mesh_shape`)
        }

    分割に失敗した面は黙って落とさず `untriangulated` に入れる。全面が失敗したときだけ例外。

    面分割の直前に境界折れ線の交差を検査し、交差する辺を再分割してから分割する(`kernel_meshing`)。
    解消できなかった面は**分割せず** `untriangulated` に入れ、`boundary["unresolved"]` に理由を残す ——
    交差した境界を面分割へ渡すと穴や非多様体の三角形になり、`untriangulated` にも現れない
    (docs/TOPOPT_GAP_CAUSALITY.md)。
    """
    _check_tessellation_args(shape, deviation, angular_deflection)

    # 既存の三角形分割は `mesh_shape` が捨ててから測り直す(残っていると偏差が効かない)。偏差は絶対mm
    # (Relative=False)。True にすると 0.2/0.05/0.01 のどれでも同じメッシュになる。
    # **build123d の既定の `export_stl` は True 側**なので流用しない。
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

    バックフェイスカリングの安全弁。開いたシェルでは裏向きの面が本当に見えているので、
    閉じた立体でだけ「裏だから粗くてよい」を許す。

    数え方は**面ごとの出現回数**であって、辺の一意な隣接面の数ではない(円筒のシーム辺は同じ
    1面に2回現れるので、一意な面で数えると閉じた円柱が「開いている」になる)。退化辺(極)は数えない。
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
        # `indexed_map` にしてはいけない(シーム辺を2回数えるのが要点)
        for raw_edge in subshapes(face_map.FindKey(index), TopAbs_ShapeEnum.TopAbs_EDGE):
            edge = TopoDS.Edge_s(raw_edge)
            if not BRep_Tool.Degenerated_s(edge):
                slot = edge_map.FindIndex(edge)
                if slot > 0:
                    counts[slot] += 1
    return all(count == 0 or count >= 2 for count in counts[1:])


def _describe_face(face: Any, index: int) -> dict:
    """1面ぶんの三角形・分類・面積・重心・法線。"""
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


def tessellate_faces(shape: "TopoDS_Shape", groups: Dict[Tuple[float, float], Iterable[int]]) -> dict:
    """指定した面だけを、`(偏差, 角度偏差)` の組ごとに三角形分割する(面単位のキャッシュ `view.DocumentView` の口)。

    戻り値: `{"faces": {面の添字: tessellate() の faces[i] と同じ形}, "untriangulated": [...], "boundary": {...},
    "closed": bool}`。組ごとに compound にして測る。組が全面なら形状そのものを測る(`tessellate()` と同じ結果)。
    組の間で共有する辺の分割は伝わらず、曲線の共有辺には粗い側の偏差ぶんの隙間が開きうる。
    """
    _check_tessellation_args(shape, *next(iter(groups), (DEFAULT_DEVIATION, DEFAULT_ANGULAR_DEFLECTION)))
    shape_map = _face_map(shape)
    total = shape_map.Extent()
    described: Dict[int, dict] = {}
    untriangulated: List[int] = []
    passes: List[dict] = []
    for (deviation, angular), indices in groups.items():
        wanted = sorted({int(index) for index in indices if 1 <= int(index) <= total})
        if not wanted:
            continue
        if len(wanted) == total:
            passes.append(kernel_meshing.mesh_shape(shape, float(deviation), float(angular)))
        else:
            group = compound([shape_map.FindKey(index) for index in wanted])
            BRepTools.Clean_s(shape)
            passes.append(_renumber_faces(kernel_meshing.mesh_shape(group, float(deviation), float(angular)), wanted))
        # 次の組の Clean_s で消える前に抜き出す。
        for index in wanted:
            payload = _describe_face(TopoDS.Face_s(shape_map.FindKey(index)), index)
            if not payload["indices"]:
                untriangulated.append(index)
            described[index] = payload
    return {
        "faces": described,
        "untriangulated": sorted(untriangulated),
        "boundary": kernel_meshing.merge_diagnostics(passes),
        "closed": _is_closed_shell(shape),
    }


def _renumber_faces(diagnostic: dict, wanted: List[int]) -> dict:
    """面を集めた compound を測った診断の面番号(compound の中で1始まり)を、元の形状の面番号へ直す。

    compound の面は `wanted` の順に並ぶ。直さないと、警告(`view._tessellate_body`)が別の面の番号を出す。
    """
    diagnostic["unresolved"] = [dict(entry, face=wanted[entry["face"] - 1]) for entry in diagnostic["unresolved"]]
    diagnostic["reinstated"] = [wanted[face - 1] for face in diagnostic["reinstated"]]
    return diagnostic


def is_closed_shell(shape: "TopoDS_Shape") -> bool:
    """閉じた立体か(画面でバックフェイスを隠してよいかの判定)。"""
    return _is_closed_shell(shape)


def bounding_box_of_faces(faces: List[dict]) -> dict:
    """`tessellate()` の `bbox` と同じものを、面の三角形の並びから求める(面単位のキャッシュから組み直すとき)。"""
    return _bounding_box(faces)


# --------------------------------------------------------------------------------------
# 面の添字引き(スケッチ平面と領域検出は kernel_sketch、押し出しは kernel_extrude)
# --------------------------------------------------------------------------------------
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


def is_solid(shape: Any) -> bool:
    """形状に SOLID が1個以上含まれるか(`split_solids` と同じ数え方)。"""
    if shape is None or shape.IsNull():
        return False
    return indexed_map(shape, TopAbs_ShapeEnum.TopAbs_SOLID).Extent() > 0


def boolean_bodies(target_shape: Any, tool_shapes: Sequence[Any], mode: str) -> dict:
    """ターゲットとツール群で union/cut/intersect する(`_boolean_bodies`。結果は `remembered`)。"""
    return remembered(
        "boolean", (target_shape, *tool_shapes), str(mode), lambda: _boolean_bodies(target_shape, tool_shapes, mode)
    )


def _boolean_bodies(target_shape: Any, tool_shapes: Sequence[Any], mode: str) -> dict:
    """ターゲットとツール群で union/cut/intersect する。

    ツールは先に `BRepAlgoAPI_Fuse` で1つにまとめてからターゲットへ作用する
    (`extrude_regions` の領域融合と同じ)。intersect は `target ∩ (tool1 ∪ tool2 ∪ ...)`。
    同一平面の面の統合(`_unify_same_domain`)は union にだけ掛ける。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape", "unified": bool, "volume_before": float, "volume_after": float,
         "provenance": {"verb": "boolean_body", "mode": str, "unified": bool,
                        "face_map": {"<旧添字>": [新添字, ...]}, "deleted": [...], "new_faces": [...]}}
    `face_map` の旧添字は `target_shape` の面。ツールの面から来た面は `new_faces` に入る。
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
    final, history, unified = result, None, False
    if mode == "union":
        final, history, unified = _unify_same_domain(result, volume_after)
    _require_valid(final, mode)
    final_map = _face_map(final)

    return {
        "shape": final,
        "unified": unified,
        "volume_before": volume_before,
        "volume_after": volume(final),
        "provenance": {
            "verb": "boolean_body",
            "mode": mode,
            "unified": unified,
            **_face_map_projection(
                _target_successors(builder, target_shape, history, final_map), final_map.Extent()
            ),
        },
    }


# --------------------------------------------------------------------------------------
# フィレット・面取り・円弧の幾何
# --------------------------------------------------------------------------------------
#: 辺の種別(`classify_edge` が返す値)
EDGE_KINDS = ("line", "circle", "other")


def edge_count(shape: "TopoDS_Shape") -> int:
    """重複を除いた辺の数。**この数え方の1始まりの添字が、辺の選択の識別子そのもの**(`face_count` と同じ)。"""
    return _edge_map(shape).Extent()


def edge_length(edge: Any) -> float:
    """辺の長さ[mm]。辺の長さを出すところは全部ここを通す(画面の辺・`measure`・述語の `length_lt` / `length_gt`)。

    外部の罠: `GCPnts_AbscissaPoint.Length_s` の既定の許容差も build123d の `Edge.length`
    (`BRepGProp.LinearProperties_s`)も、楕円・B-spline の辺で粗い(2026-10-17 実測: 楕円の柱の辺で
    直径 20×10 は +0.13%、直径 40×2 は +1.74%。ベジエの辺は 1e-5。円・直線は厳密)。許容差 1e-9 なら数値積分と
    1e-11 まで一致し、時間は細い楕円の辺1本で 0.04ms(ベジエ 0.004ms)。
    """
    return float(GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edge), 1.0e-9))


def classify_edge(edge: Any) -> str:
    """1つの辺を EDGE_KINDS のいずれかへ分類する。

    """
    adaptor = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
    curve_type = adaptor.GetType()
    if curve_type == GeomAbs_CurveType.GeomAbs_Line:
        return "line"
    if curve_type == GeomAbs_CurveType.GeomAbs_Circle:
        return "circle"
    return "other"


def edge_of(shape: "TopoDS_Shape", edge_index: int) -> Any:
    """形状の1始まり添字の辺を返す。範囲外は ValueError。"""
    return _edge_at(_edge_map(shape), edge_index)


def edge_index_of(shape: "TopoDS_Shape", edge: Any) -> int:
    """辺が形状の何番目か(1始まり)を `IsSame` で返す。無ければ ValueError。

    build123d の列挙順が `_edge_map` と一致する保証は無いので、`query.edges_of_face` は形状の同一性で引き当てる。
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

    `tessellate` の辺版(画面の輪郭線は three.js の `EdgesGeometry` で、コアの辺の添字とは対応しない)。

    戻り値(素の値だけ):
        [{"index": int, "kind": "line"|"circle"|"other", "length": float,
          "closed": bool, "points": [x,y,z, x,y,z, ...]}, ...]

    `index` は `edge_count` / `edge_of` / `fillet(edges=[...])` と**同じ数え方**(`_edge_map` の1始まり)。
    ずれると「指した辺と違う辺が丸まる」静かな事故になるので、添字は必ず `_edge_map` から取る。
    連なり(フィレット・面取りで1本として振る舞う辺の組)は入れない。大きな部品で折れ線の数十倍かかるので、
    画面は指した辺の分だけ別に取り寄せる(`kernel_chains.BlendChains`)。

    折れ線は `GCPnts_TangentialDeflection` で `deviation`(mm)と `angular_deflection`(ラジアン)の
    **両方**を満たすまで分割する。**線形偏差だけでは足りない** —— 小半径の円弧は分割数が2〜4点まで
    落ち込んで多角形に見える(実測: 20mm立方体の全辺フィレットでr=0.5mmの円弧が2点=弦1本)。
    既定の角度偏差は `tessellate()` と同じなので、ソリッド表示と辺オーバーレイの粗さが揃う。
    `GCPnts_TangentialDeflection` のコンストラクタは `(adaptor, angular, linear, minNbPoints=2, ...)`
    の順で、`IsDone()` を持たず、退化した曲線では例外を投げる。**分割に失敗した辺は端点だけの2点で返す**
    (落とすとその辺だけ画面から消えて「指せない辺」になる)。
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
                "length": edge_length(edge),
                "closed": bool(edge.Closed()),
                "points": points,
            }
        )
    return described


def edge_chain_finder(shape: "TopoDS_Shape") -> "kernel_chains.BlendChains":
    """形の辺の連なりを、頼まれた辺の分だけ求めて覚える器(`kernel_chains.BlendChains`)。形ごとに1つ作って使い回す。"""
    return kernel_chains.BlendChains(shape)


def _edge_at(shape_map: "TopTools_IndexedMapOfShape", edge_index: int) -> Any:
    """既に作った辺マップから引く。"""
    if not 1 <= int(edge_index) <= shape_map.Extent():
        raise ValueError(
            f"edge index out of range: {edge_index} (1..{shape_map.Extent()})"
        )
    return TopoDS.Edge_s(shape_map.FindKey(int(edge_index)))


# --------------------------------------------------------------------------------------
# 頂点(選択集合に入った頂点を画面に出すための座標)
# --------------------------------------------------------------------------------------
def _vertex_map(shape: "TopoDS_Shape") -> "TopTools_IndexedMapOfShape":
    """頂点の重複除去済みマップ(1始まり)。添字が選択参照の `index` そのもの。"""
    return _subshape_map(shape, "vertex")


def vertex_positions(shape: "TopoDS_Shape") -> List[dict]:
    """頂点を1始まりの添字つきの3D座標で返す。

    戻り値(素の値だけ): `[{"index": int, "point": [x, y, z]}, ...]`

    """
    shape_map = _vertex_map(shape)
    described: List[dict] = []
    for index in range(1, shape_map.Extent() + 1):
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(shape_map.FindKey(index)))
        described.append(
            {"index": index, "point": [float(point.X()), float(point.Y()), float(point.Z())]}
        )
    return described


#: 直前の重い形状演算の結果(`remembered`)。鍵は (演算, 入力の形の id 列, 引数)、値は (入力の形, 結果)。
_RESULTS: "OrderedDict[tuple, tuple]" = OrderedDict()
#: 覚える件数。プレビューの打ち直し(半径を変えるたびに1件)と、確定・undo・redo の行き来が収まる数。
_RESULTS_LIMIT = 8
_RESULTS_LOCK = threading.Lock()


def remembered(name: str, shapes: tuple, arguments: Any, compute: Callable[[], dict]) -> dict:
    """重い形状演算(フィレット・面取り・ブーリアン・シェル)の結果を、入力の形の同一性と引数が同じなら使い回す。

    プレビュー(`Session.preview`)は verb を投機的に掛けて形を測り、文書を戻す。確定(`apply`)は同じ形に同じ引数で
    同じ verb を掛けるので、ここで覚えた結果がそのまま当たる(PC ケースの最大ボディのフィレットで 0.88 秒。issue #169)。
    redo も undo で戻した同じ形へ掛け直すので当たる。入力の形は差し替えであって書き換えではない(`duocad/speculative.py`)
    ので、同じ物なら答えも同じ。id の使い回しで別の形に当たらないよう、入力の形そのものを持って `is` で比べる。
    失敗(例外)は覚えない。返す辞書は呼ぶたびに新しい(中の `shape` と `provenance` は同じ物)。
    """
    key = (name, tuple(id(shape) for shape in shapes), arguments)
    with _RESULTS_LOCK:
        hit = _RESULTS.get(key)
        if hit is not None and all(old is new for old, new in zip(hit[0], shapes)):
            _RESULTS.move_to_end(key)
            return dict(hit[1])
    result = compute()
    with _RESULTS_LOCK:
        _RESULTS[key] = (tuple(shapes), result)
        while len(_RESULTS) > _RESULTS_LIMIT:
            _RESULTS.popitem(last=False)
    return dict(result)


def copy_shape(shape: Any) -> Any:
    """形の深い写し(幾何も写し、三角形分割は写さない)。面・辺・頂点の添字の数え方は元と同じ
    (PC ケースの 27 部品すべてで面・辺・頂点の対応を確認。最大ボディ 233 面で 4 ms)。

    別スレッドで形を扱うとき(`Session.preview_job`)に、イベントループ側と同じ `TShape` を共有しないために使う
    (OCCT の三角形分割は面に書き込まれ、`BRepTools.Clean_s` + 面分割が同じ面を別スレッドから書き換えると壊れる)。
    """
    return BRepBuilderAPI_Copy(shape, True, False).Shape()


def adopt_results(pairs: Sequence[Tuple[Any, Any]]) -> None:
    """`(元の形, 写し)` の組について、写しを入力として覚えた結果(`remembered`)を、元の形を入力とした結果として覚え直す。

    プレビューはワーカースレッドで写しの上で計算する(`Session.preview_job`)。確定は元の形に同じ演算を掛けるので、
    覚え直さないと同じ計算をもう一度する(issue #169 の使い回しが効かなくなる)。写しと元の形は添字の数え方も幾何も
    同じなので、結果(新しい形・由来の添字)はそのまま元の形の結果として使える。
    """
    originals = {id(copy): (copy, original) for original, copy in pairs}

    def swap(shape: Any) -> Any:
        hit = originals.get(id(shape))
        return hit[1] if hit is not None and hit[0] is shape else shape

    with _RESULTS_LOCK:
        for key, (shapes, result) in list(_RESULTS.items()):
            swapped = tuple(swap(shape) for shape in shapes)
            if all(new is old for new, old in zip(swapped, shapes)):
                continue
            del _RESULTS[key]
            _RESULTS[(key[0], tuple(id(shape) for shape in swapped), key[2])] = (swapped, result)


def _value_key(values: dict) -> tuple:
    """`{辺の添字: 値}` を `remembered` の鍵へ。**順序を保つ**: OCCT は辺を足す順で結果が変わる(接線で続く辺。
    sol の再現: 同じ辺・半径の組でも [1, 5] と [5, 1] で体積が 23336.71 と 23409.17 mm³)。"""
    return tuple((int(index), float(value)) for index, value in values.items())


def fillet_edges(shape: "TopoDS_Shape", radii: dict) -> dict:
    """辺(1始まりの添字)を丸めた**新しい形状**と由来の射影を返す。

    `radii` は `{辺の添字: 半径}`。入力の `shape` は変更しない。

    **必ず `IsDone()` を確認する**。OCCTのフィレットは例外を投げずに失敗し、半径に対して単調でない
    (20mm立方体の全辺で実測: r=3.0 成功 / r=10.0 `IsDone()=False` / r=10.5 成功 / r=30.0 `IsDone()=False`)。

    **限界値ちょうどの半径は通らず、限界未満では微小な平面が残る**(20mm立方体の上面4辺で
    r=9.99 は成功して 0.02mm 角の面が残り、r=10.0 は `IsDone()=False`。20×10×20 の長辺2本でも
    r=4.999 成功 / 5.0 失敗。面取りも距離 4.99 成功 / 5.0 失敗)。**参照している面の面積を厳密に
    0にする状況はフィレット・面取りでは作れない** —— 回帰題材が要るなら切削(対象面より大きい
    領域で切り下げる)で作る。

    **薄肉では限界値ちょうどの半径が `IsDone()=False` でなく access violation になる**(`BRepPrimAPI_MakeBox` の箱を
    厚さ 2 で抜き、縁の辺を半径 2 で丸める。Python の例外にならず `except` で捕まらない)。辺から同じ平面の面の
    別の辺までの距離と半径が一致するフィレットは、元の形の `MakeFillet` を呼ばない(`kernel_chains.fillet_consumes_face`)。
    これで守れるのは実測した範囲だけで、他の形の同種の異常終了を防ぐ保証は無い。

    **OCCT が断るか、丸めた辺に関係の無い面まで変えたとき(別の形に当たった)は、はみ出しを吸収して作る**
    (`kernel_overflow.absorb`。Fusion 360 と同じく、はみ出しは崖の面で切れ、当たった形は貫通する。docs/FILLET_OVERFLOW.md)。
    吸収が引き受けない形(角で連なりが出会う選び方など)では、OCCT の結果か失敗をそのまま返す。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape": <TopoDS_Shape>, "provenance": {...}, "volume_before": float,
         "volume_after": float}
    """
    return remembered("fillet", (shape,), _value_key(radii), lambda: _blend_edges(shape, radii, verb="fillet"))


def chamfer_edges(shape: "TopoDS_Shape", distances: dict) -> dict:
    """辺(1始まりの添字)を面取りした新しい形状と由来の射影を返す。

    `distances` は `{辺の添字: 距離}`。`fillet_edges` と同じ契約(`MakeChamfer` も黙って失敗する)。
    """
    return remembered("chamfer", (shape,), _value_key(distances), lambda: _blend_edges(shape, distances, verb="chamfer"))


def _blend_edges(shape: "TopoDS_Shape", values: dict, verb: str) -> dict:
    """フィレットと面取りの共通実装(違うのはビルダの型だけ)。"""
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

    if verb != "fillet":
        return _built_blend(shape, maker, values, verb, edge_indices, volume_before)

    # フィレットは OCCT の結果を先に試し、失敗したとき・丸める辺に関係の無い面まで変えたとき(別の形に当たった)だけ、
    # はみ出しを吸収する作り方(`kernel_overflow`)へ回す。経緯と比べた案は docs/FILLET_OVERFLOW.md。
    radii = {int(i): float(v) for i, v in values.items()}
    native: Optional[dict] = None
    clash = kernel_chains.fillet_consumes_face(shape, edge_map, radii)
    if clash is not None:
        failure = ValueError(
            f"fillet radius {radii[clash[0]]:g} on edge {clash[0]} would consume the whole planar face next to it "
            f"(it reaches exactly as far as the face's edge {clash[1]}): 面を過不足なく食い尽くす半径は OCCT がプロセスごと"
            "落ちることがある。半径をわずかにずらすこと"
        )
    else:
        try:
            native = _built_blend(shape, maker, values, verb, edge_indices, volume_before)
        except ValueError as error:
            failure = error
        else:
            if not _blend_reaches_other_faces(maker, shape):
                return native
    declined = ""
    if native is None:
        # 辺の曲率半径を超える半径の、上面を囲む輪(issue #207)。引き受けない形ならはみ出しの吸収へ進む
        try:
            return _tight_fillet(shape, edge_map, radii, edge_indices, volume_before)
        except kernel_tight.Declined as reason:
            declined = f"; 曲率半径を超える丸めもできない: {reason}"
        except kernel_overflow.Unsupported:
            pass
    try:
        return _absorbed_fillet(shape, edge_map, radii, edge_indices, volume_before)
    except ValueError as reason:
        if native is not None:
            return native
        raise ValueError(f"{failure} (はみ出しの吸収もできない: {reason}{declined})") from failure


def _built_blend(
    shape: "TopoDS_Shape", maker: Any, values: dict, verb: str, edge_indices: List[int], volume_before: float
) -> dict:
    """辺を足し終えた `MakeFillet` / `MakeChamfer` を組み立てて検査し、`_blend_edges` の戻り値の形にする。"""
    try:
        maker.Build()
    except Exception as error:  # noqa: BLE001  OCCTは例外でも失敗しうる
        raise ValueError(f"{verb} failed while building (edges {edge_indices}): {error}") from error
    if not maker.IsDone():
        # OCCTは例外を投げずに失敗する。半径/距離の範囲検査では代用できない
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


def _blend_reaches_other_faces(maker: Any, shape: "TopoDS_Shape") -> bool:
    """組み立てたフィレットが、丸めた辺(連なりを含む)の頂点に触れない面まで変えたか。

    変えていれば、丸めの面が隣り合わない別の形に当たっている。OCCT はそこで球をその形にも触れさせた別の形を返す
    (円板に柱2本、隙間 2・半径 3 で、両方の柱に接するBスプライン面が2枚増える。issue #208 の実測)。
    辺の端の面(箱の1辺を丸めたときの側面)は頂点に触れるので数えない。

    全部の連なりの頂点でまとめて許すと、柱 A の根元の丸めが柱 B の側面に当たっても、B の上縁も選んでいれば B の側面は
    B の上縁の頂点に触れるので見逃す(Codex レビューの反例)。そこで、変わった面の近くにあるのにその面に触れない連なりを、
    1本ずつ組み直して見る(連なりの数で打ち切ると、離れた柱の上縁を 11 本足しただけで見逃しが戻った)。
    """
    contours = range(1, maker.NbContours() + 1)
    changed = _faces_changed_by(maker, shape)
    touching = {contour: _vertices_of_contour(maker, contour) for contour in contours}
    smooth = None
    suspects = set()
    for face in changed:
        corners = indexed_map(face, TopAbs_ShapeEnum.TopAbs_VERTEX)
        owners = {
            contour for contour, vertices in touching.items()
            if any(vertices.Contains(corners.FindKey(i)) for i in range(1, corners.Extent() + 1))
        }
        if not owners:
            if smooth is None:
                smooth = _coplanar_supports(maker, shape, contours)
            if not smooth.Contains(face):
                return True
            continue
        face_box = Bnd_Box()
        BRepBndLib.Add_s(face, face_box)
        for contour in contours:
            if contour not in owners and not _contour_box(maker, contour).IsOut(face_box):
                suspects.add(contour)
    for contour in sorted(suspects):
        if not maker.IsConstant(contour):
            continue
        alone = BRepFilletAPI_MakeFillet(shape)
        alone.Add(maker.Radius(contour), maker.Edge(contour, 1))
        try:
            alone.Build()
        except Exception:  # noqa: BLE001  OCCTは例外でも失敗しうる
            continue
        if alone.IsDone() and _changes_faces_away_from(alone, shape, range(1, alone.NbContours() + 1)):
            return True
    return False


def _faces_changed_by(maker: Any, shape: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """`maker` が変えた(結果に同じ面として残らなかった)入力の面。"""
    # 触られなかった面は結果に同じ面として残るので、結果に無い入力の面だけを見る(`Modified` を全面に問うと、
    # PC ケースの 233 面のボディで1回 112ms かかった。フィレットそのものは 18ms)。
    kept = _face_map(maker.Shape())
    face_map = _face_map(shape)
    return [face_map.FindKey(i) for i in range(1, face_map.Extent() + 1) if not kept.Contains(face_map.FindKey(i))]


def _vertices_of_contour(maker: Any, contour: int) -> "TopTools_IndexedMapOfShape":
    vertices = TopTools_IndexedMapOfShape()
    for number in range(1, maker.NbEdges(contour) + 1):
        edge = maker.Edge(contour, number)
        vertices.Add(TopExp.FirstVertex_s(edge))
        vertices.Add(TopExp.LastVertex_s(edge))
    return vertices


def _contour_box(maker: Any, contour: int) -> "Bnd_Box":
    """連なりの辺の外接箱を、丸めが届きうる幅(半径の 3 倍)だけ広げた箱。"""
    box = Bnd_Box()
    for number in range(1, maker.NbEdges(contour) + 1):
        BRepBndLib.Add_s(maker.Edge(contour, number), box)
    # 可変半径の連なりは半径を1つに決められないので、どの面とも重なる箱にする(組み直しの検査は一定半径だけ)。
    box.Enlarge(3.0 * maker.Radius(contour) if maker.IsConstant(contour) else 1.0e9)
    return box


def _changes_faces_away_from(maker: Any, shape: "TopoDS_Shape", contours: Iterable[int]) -> bool:
    """`maker` が、`contours` の辺の頂点に触れない入力の面を変えたか。"""
    rounded_vertices = TopTools_IndexedMapOfShape()
    for contour in contours:
        for number in range(1, maker.NbEdges(contour) + 1):
            edge = maker.Edge(contour, number)
            rounded_vertices.Add(TopExp.FirstVertex_s(edge))
            rounded_vertices.Add(TopExp.LastVertex_s(edge))
    smooth = None
    for face in _faces_changed_by(maker, shape):
        corners = indexed_map(face, TopAbs_ShapeEnum.TopAbs_VERTEX)
        if not any(rounded_vertices.Contains(corners.FindKey(i)) for i in range(1, corners.Extent() + 1)):
            if smooth is None:
                smooth = _coplanar_supports(maker, shape, contours)
            if not smooth.Contains(face):
                return True
    return False


def _coplanar_supports(maker: Any, shape: "TopoDS_Shape", contours: Iterable[int]) -> "TopTools_IndexedMapOfShape":
    """丸めた辺の両側の面と、そこから同じ平面に載る面だけをたどって行ける面(同じ平面を割った継ぎ目の先)。

    丸めが継ぎ目を越えて隣の面へ続くのは当たりではない(箱の上面を x=2 で同じ平面の2面に割り、x=0 の辺を半径 3 で
    丸めると隣の面まで変わる。当たりと見なして吸収へ回すと、継ぎ目を崖として肉を切り、削り残した。Codex レビューの反例)。
    接線連続の辺でたどると、既にある丸めの面を通って別の柱の側面まで届き、本当の当たりを見逃した(柱 B の根元と上縁を
    先に丸めておくと、柱 A の根元の丸めが B を消す形をそのまま返した。同じく反例)。
    """
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, TopAbs_ShapeEnum.TopAbs_FACE, edge_faces)
    reached = TopTools_IndexedMapOfShape()
    queue = []
    for contour in contours:
        for number in range(1, maker.NbEdges(contour) + 1):
            edge = maker.Edge(contour, number)
            if edge_faces.Contains(edge):
                for face in edge_faces.FindFromKey(edge):
                    if not reached.Contains(face):
                        reached.Add(face)
                        queue.append(face)
    while queue:
        face = queue.pop()
        edges = indexed_map(face, TopAbs_ShapeEnum.TopAbs_EDGE)
        for number in range(1, edges.Extent() + 1):
            edge = TopoDS.Edge_s(edges.FindKey(number))
            if not edge_faces.Contains(edge):
                continue
            for other in edge_faces.FindFromKey(edge):
                if reached.Contains(other) or other.IsSame(face):
                    continue
                if kernel_overflow.coplanar(face, other):
                    reached.Add(other)
                    queue.append(other)
    return reached


def _absorbed_fillet(
    shape: "TopoDS_Shape", edge_map: Any, radii: dict, edge_indices: List[int], volume_before: float
) -> dict:
    """`kernel_overflow.absorb` の形を検査し、由来をたどって `_blend_edges` の戻り値の形にする。"""
    absorbed = kernel_overflow.absorb(shape, edge_map, radii)
    context = f"edges {edge_indices}, absorbing overflow"
    # 凹の肉を足して凸の肉を引くと、体積が打ち消し合って変わらないことがある(形は変わっている)。
    unchanged_check = None if (absorbed.concave and absorbed.convex) else volume_before
    volume_after = _checked_volume(absorbed.shape, "fillet", volume_before=unchanged_check, context=context)
    if (absorbed.concave and not absorbed.convex and volume_after < volume_before) or (
        absorbed.convex and not absorbed.concave and volume_after > volume_before
    ):
        raise ValueError(f"fillet moved the volume the wrong way ({volume_before} -> {volume_after}, {context})")
    # 統合の前に断る: 壊れた形や離れたソリッドを統合に渡すと、仮想メモリを使い尽くすことがある(PC ケースの部品で 84GB)。
    if len(split_solids(absorbed.shape)) > len(split_solids(shape)):
        raise ValueError(f"fillet left the rounding material as a separate solid ({context})")
    _require_valid(absorbed.shape, "fillet", context)
    final, history, _ = _unify_same_domain(absorbed.shape, volume_after)
    _require_valid(final, "fillet", context)
    if len(split_solids(final)) > len(split_solids(shape)):
        raise ValueError(f"fillet left the rounding material as a separate solid ({context})")

    final_map = _face_map(final)
    shape_map = _face_map(shape)
    successors: dict = {}
    for index in range(1, shape_map.Extent() + 1):
        current = [shape_map.FindKey(index)]
        for builder in absorbed.builders:
            following = []
            for face in current:
                modified = _shape_list(builder, "Modified", face)
                following.extend(modified or ([] if _is_removed(builder, face) else [face]))
            current = following
        successors[str(index)] = _positions_in(
            final_map, (survivor for face in current for survivor in _through_history(history, face))
        )
    return {
        "shape": final,
        "provenance": {"verb": "fillet", "edges": list(edge_indices), **_face_map_projection(successors, final_map.Extent())},
        "volume_before": float(volume_before),
        "volume_after": float(volume(final)),
    }


def _tight_fillet(
    shape: "TopoDS_Shape", edge_map: Any, radii: dict, edge_indices: List[int], volume_before: float
) -> dict:
    """`kernel_tight.round_tight_loop`(辺の曲率半径を超える半径。issue #207)の形を検査し、由来をたどって `_blend_edges` の
    戻り値の形にする。上面の後継は丸めた後の上面、それ以外の面は本体を深さ R で切る引き算と冠との縫い合わせをたどる。"""
    tight = kernel_tight.round_tight_loop(shape, edge_map, radii)
    context = f"edges {edge_indices}, rounding past the edge curvature"
    volume_after = _checked_volume(tight.shape, "fillet", volume_before=volume_before, context=context)
    if volume_after >= volume_before:
        raise ValueError(f"fillet moved the volume the wrong way ({volume_before} -> {volume_after}, {context})")
    if len(split_solids(tight.shape)) != len(split_solids(shape)):
        raise ValueError(f"fillet changed the number of solids ({context})")
    _require_valid(tight.shape, "fillet", context)
    final, history, _ = _unify_same_domain(tight.shape, volume_after)
    _require_valid(final, "fillet", context)

    def through(builder: Any, face: "TopoDS_Shape") -> List["TopoDS_Shape"]:
        modified = _shape_list(builder, "Modified", face)
        return modified or ([] if _is_removed(builder, face) else [face])

    final_map = _face_map(final)
    shape_map = _face_map(shape)
    below, joined = tight.builders
    successors: dict = {}
    for index in range(1, shape_map.Extent() + 1):
        face = shape_map.FindKey(index)
        if face.IsSame(tight.top):
            current = [tight.lens]
        else:
            current = [after for before in through(below, face) for after in through(joined, before)]
        successors[str(index)] = _positions_in(
            final_map, (survivor for face in current for survivor in _through_history(history, face))
        )
    return {
        "shape": final,
        "provenance": {"verb": "fillet", "edges": list(edge_indices), **_face_map_projection(successors, final_map.Extent())},
        "volume_before": float(volume_before),
        "volume_after": float(volume(final)),
    }


def _blend_provenance(
    maker: Any,
    shape: "TopoDS_Shape",
    final: "TopoDS_Shape",
    verb: str,
    edge_indices: List[int],
) -> dict:
    """由来を `normalize_provenance` 経由で回収し、JSON化できる添字の射影へ落とす(`extrude_faces` と同じ形)。"""
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
    **SOLIDが1つも無い形状(裸の面・シェルのみ)は形状全体を1要素のリストにして返す**
    (0個のBodyを黙って作らない。io_step.pyの契約)。列挙順は `TopExp.MapShapes_s` の走査順(決定的)。
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
    """複数の形状を1つの TopoDS_Compound へまとめる。"""
    if not shapes:
        raise ValueError("cannot build a compound from an empty sequence of shapes")
    return compound(shapes)


# --------------------------------------------------------------------------------------
# 内部ヘルパ
# --------------------------------------------------------------------------------------
def _subshape_map(shape: "TopoDS_Shape", kind: str) -> "TopTools_IndexedMapOfShape":
    """`kind`("face" | "edge" | "vertex")の重複除去済みマップ(1始まり)。添字が選択の識別子になる。
    null は ValueError(`count_subshapes` と違って 0 を返さない)。"""
    enum, plural = _SUBSHAPE_KINDS[kind]
    if shape is None or shape.IsNull():
        raise ValueError(f"cannot map the {plural} of an empty shape")
    return indexed_map(shape, enum)


def _face_map(shape: "TopoDS_Shape") -> "TopTools_IndexedMapOfShape":
    """面の重複除去済みマップ(1始まり)。"""
    return _subshape_map(shape, "face")


def _face_normal(face: Any) -> tuple:
    """面の中央での**外向き**単位法線。`BRepGProp_Face` は面の向きを見るので反転面でも外を向く(20mm立方体の6面で実測)。"""
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
    """(面積, 重心)。`BRepGProp.SurfaceProperties_s` の既定積分。"""
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
    """三角形の節点から境界箱を作る(カメラ合わせ用)。面が無ければ原点の退化した箱。"""
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

    採用しない場合は**統合を一度も通していない元の形状**と履歴 None を返す。

    **`ShapeUpgrade_UnifySameDomain` は入力の形状を壊す**(実測):

        fuse の結果        -> IsValid()=True
        unify.Build() した後、**同じ fuse の結果を測り直すと** IsValid()=False
        unify の前に取った BRepBuilderAPI_Copy は unify 後も IsValid()=True

    したがって「統合してダメなら統合前へ戻る」は戻り先が既に壊れている。必ず**コピーで試してから**本番を行う:

    1. コピーに対して統合を試し、(a)妥当 (b)体積が変わらない (c)**面が実際に減る** を確かめる
    2. 3つとも満たすときだけ、元の形状に対して本番の統合を行い、その出力と履歴を返す
       (由来の射影は元の形状の部分形状を鍵に引くので、コピーに対して統合すると同一性が切れる)
    3. 満たさないときは元の形状をそのまま返す

    (c)は、面が減らない統合は利得ゼロで入力を壊す危険だけが残るため。`shelf_attachment_GD.step` の
    面36・43を+2mmしたときが実際にこれで、面数は57のまま統合後の形状だけが壊れていた
    (20mm立方体の上面+10では 10面 -> 6面 に減るので採用される)。
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

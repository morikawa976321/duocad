"""形状のコピー・結合・同一領域の統合。操作の試行順と失敗方針は呼び手が決める。"""
from __future__ import annotations

from typing import Any, Sequence

from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_ListOfShape

from . import (
    brep_checks as _brep_checks,
    brep_geometry as _brep_geometry,
    brep_topology as _brep_topology,
)


def fuse_all(shapes: Sequence[Any], what: str) -> "TopoDS_Shape":
    """`shapes` を `BRepAlgoAPI_Fuse` で1つにする。**1つなら Fuse を呼ばずそのまま返す**。"""
    combined = shapes[0]
    for other in shapes[1:]:
        fused = BRepAlgoAPI_Fuse(combined, other)
        fused.Build()
        if not fused.IsDone():
            raise ValueError(f"failed to fuse {what}")
        combined = fused.Shape()
        if combined is None or _brep_topology.is_null(combined):
            raise ValueError(f"fusing {what} produced an empty shape")
    return combined


def unify_same_domain(shape: "TopoDS_Shape", volume_before: float) -> tuple:
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
    if candidate is None or _brep_topology.is_null(candidate):
        return shape, None, False
    if abs(_brep_geometry.volume(candidate) - volume_before) > 1e-6 * max(1.0, abs(volume_before)):
        return shape, None, False
    if not _brep_checks.is_valid(candidate):
        return shape, None, False
    if _brep_topology._face_map(candidate).Extent() >= _brep_topology._face_map(shape).Extent():
        return shape, None, False

    unify = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    unify.Build()
    unified = unify.Shape()
    if unified is None or _brep_topology.is_null(unified) or not _brep_checks.is_valid(unified):
        # 試行では通ったのに本番で壊れた場合。入力(shape)はもう壊れているので操作ごと捨てる。
        raise ValueError("ShapeUpgrade_UnifySameDomain が試行と異なる結果を返した")
    return unified, unify.History(), True


def build_boolean(kind: str, first: TopoDS_Shape, second: TopoDS_Shape, fuzzy: float = 0.0):
    # 外部の罠: 2引数のコンストラクタはその場で演算まで実行する(`IsDone()` が `Build()` の前に真)。続けて `Build()` を呼ぶと
    # 同じ演算が2回走り(`_absorbed_fillet` 1 回が実測 4.0〜4.3 秒 → 2.8〜3.1 秒)、`SetFuzzyValue` は2回目にしか効かない。
    builder = {"fuse": BRepAlgoAPI_Fuse, "cut": BRepAlgoAPI_Cut, "common": BRepAlgoAPI_Common}[kind]()
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(first)
    tools.Append(second)
    builder.SetArguments(arguments)
    builder.SetTools(tools)
    if fuzzy:
        builder.SetFuzzyValue(fuzzy)
    builder.Build()
    return builder


def copy_shape(shape: Any) -> Any:
    """形の深い写し(幾何も写し、三角形分割は写さない)。面・辺・頂点の添字の数え方は元と同じ
    (PC ケースの 27 部品すべてで面・辺・頂点の対応を確認。最大ボディ 233 面で 4 ms)。

    別スレッドで形を扱うとき(`Session.preview_job`)に、イベントループ側と同じ `TShape` を共有しないために使う
    (OCCT の三角形分割は面に書き込まれ、`BRepTools.Clean_s` + 面分割が同じ面を別スレッドから書き換えると壊れる)。
    """
    return BRepBuilderAPI_Copy(shape, True, False).Shape()

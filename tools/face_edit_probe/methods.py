"""比べる方式。どれも「形と面の列(と距離)を受け取り、新しい形を返すか例外を投げる」。

- A `defeature`        — OCCT の `BRepAlgoAPI_Defeaturing`(面の削除専用)
- B `offset_faces`     — OCCT の `BRepOffset_MakeOffset` を全体 0・選んだ面だけ距離 d で(面の移動)
- C `rebuild_*`        — 自前: 面を外す/差し替え、隣の面を延ばし、`BOPAlgo_MakerVolume` で組み直す(custom.py)
- D `move_following_blends` — 丸めを外して(A)、面を動かして(B か C)、同じ半径で丸め直す(blend.py)
"""
from __future__ import annotations

from OCP.BOPAlgo import BOPAlgo_RemoveFeatures
from OCP.Message import Message_Gravity
from OCP.BRepOffset import BRepOffset_MakeOffset, BRepOffset_Mode
from OCP.GeomAbs import GeomAbs_JoinType
from OCP.TopTools import TopTools_ListOfShape


class FaceEditError(ValueError):
    pass


def defeature(shape, faces, *, parallel=False):
    """`BRepAlgoAPI_Defeaturing` の中身の `BOPAlgo_RemoveFeatures` を直接使う(OCP の
    `BRepAlgoAPI_Defeaturing` は警告の問い合わせ口 `HasWarnings` / `GetReport` を出していない)。

    **閉じられない面は例外にも IsDone()=false にもならず、警告だけ出して元の形を返す**(d_box_top で実測)。
    警告を失敗として扱う(`alerts`)。"""
    algo = BOPAlgo_RemoveFeatures()
    algo.SetShape(shape)
    faces_list = TopTools_ListOfShape()
    for face in faces:
        faces_list.Append(face)
    algo.AddFacesToRemove(faces_list)
    algo.SetRunParallel(parallel)
    algo.SetToFillHistory(True)
    algo.Perform()
    if algo.HasErrors():
        raise FaceEditError("RemoveFeatures: " + ", ".join(alerts(algo, Message_Gravity.Message_Fail)))
    warnings = alerts(algo, Message_Gravity.Message_Warning)
    if warnings:
        raise FaceEditError("RemoveFeatures warned: " + ", ".join(warnings))
    result = algo.Shape()
    if result is None or result.IsNull():
        raise FaceEditError("RemoveFeatures: empty result")
    return result, algo


def alerts(algo, gravity):
    return [alert.GetMessageKey() for alert in algo.GetReport().GetAlerts(gravity)]


def offset_faces(shape, faces, distance, *, intersection=False, tol=1e-7):
    """全体の距離を 0 にして、選んだ面だけ `distance` だけ法線方向へずらす。"""
    algo = BRepOffset_MakeOffset()
    algo.Initialize(
        shape,
        0.0,
        tol,
        BRepOffset_Mode.BRepOffset_Skin,
        intersection,
        False,
        GeomAbs_JoinType.GeomAbs_Intersection,
        False,
        False,
    )
    for face in faces:
        algo.SetOffsetOnFace(face, distance)
    algo.MakeOffsetShape()
    if not algo.IsDone():
        raise FaceEditError(f"MakeOffset: IsDone() is false (error {algo.Error()})")
    result = algo.Shape()
    if result is None or result.IsNull():
        raise FaceEditError("MakeOffset: empty result")
    return result, algo

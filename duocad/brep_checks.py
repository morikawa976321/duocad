"""形状の検査。検査を呼ぶ段と、操作固有の失敗の扱いは呼び出し側が持つ。"""
from __future__ import annotations

from typing import Any, Optional

from OCP.BOPAlgo import BOPAlgo_CheckerSI
from OCP.BRep import BRep_Tool
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopAbs import TopAbs_ShapeEnum, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopoDS import TopoDS, TopoDS_Shape

from .brep_geometry import volume
from .brep_topology import _face_edit_edge_faces, _face_map, indexed_map, split_solids, subshapes, is_null


#: 「ブーリアンは成功したのに体積が変わっていない」と見なす相対差。OCCT は「成功したが何も
#: 起きていない」を返せるので、ここで止めないと静かに空振りする。
_NO_CHANGE_RTOL = 1e-9


def _checked_volume(
    result: Any, label: str, *, volume_before: Optional[float] = None, context: str = ""
) -> float:
    """ブーリアンの結果を 空 → 体積>0 → 無変化 の順に検査して体積を返す。

    無変化の検査は `volume_before` を渡したときだけ(押し出しは相手があるときだけ見る。
    `boolean_bodies` と `_blend_edges` は見ない)。`context` は文の括弧に足す(`face 3, distance=5.0` 等)。
    """
    suffix = f", {context}" if context else ""
    if result is None or is_null(result):
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
    if shape is None or is_null(shape):
        raise ValueError(f"cannot tessellate {target}")
    if not deviation > 0:
        raise ValueError(f"deviation must be positive (mm), got {deviation!r}")
    if not angular_deflection > 0:
        raise ValueError(f"angular_deflection must be positive, got {angular_deflection!r}")


def _is_closed_shell(shape: "TopoDS_Shape") -> bool:
    """**閉じた立体か**(すべての辺が2面以上に共有されているか)。

    バックフェイスカリングの安全弁。開いたシェルでは裏向きの面が本当に見えているので、
    閉じた立体でだけ「裏だから粗くてよい」を許す。

    数え方は**面ごとの出現回数**であって、辺の一意な隣接面の数ではない(円筒のシーム辺は同じ
    1面に2回現れるので、一意な面で数えると閉じた円柱が「開いている」になる)。退化辺(極)は数えない。
    """
    if shape is None or is_null(shape):
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


def is_closed_shell(shape: "TopoDS_Shape") -> bool:
    """閉じた立体か(画面でバックフェイスを隠してよいかの判定)。"""
    return _is_closed_shell(shape)


def is_solid(shape: Any) -> bool:
    """形状に SOLID が1個以上含まれるか(`split_solids` と同じ数え方)。"""
    if shape is None or is_null(shape):
        return False
    return indexed_map(shape, TopAbs_ShapeEnum.TopAbs_SOLID).Extent() > 0


def _count_face_interferences(ds, pairs):
    """`pairs`(`BOPDS_Pair`の列)のうち、両端の形状が面であるものだけを数える。

    `BOPAlgo_CheckerSI` の `Interferences()` は種類混在の集計で、頂点/辺を含むペアは許容差が
    触れているだけで無条件にカウントされる。面-面だけは、OCCT自身が実際の交差の有無を確認してから
    数える。型だけで判定し、数値のしきい値は使わない。
    """
    return sum(1 for pair in pairs
               if all(ds.Shape(index).ShapeType() == TopAbs_FACE for index in pair.Indices()))


def inspect_solid(shape):
    solids = subshapes(shape, TopAbs_SOLID) if shape is not None and not is_null(shape) else []
    valid = bool(solids) and BRepCheck_Analyzer(shape).IsValid()
    shells = subshapes(shape, TopAbs_SHELL) if solids else []
    closed = bool(shells) and all(shell.Closed() for shell in shells)
    intersections = None
    if valid and closed:
        checker = BOPAlgo_CheckerSI()
        checker.AddArgument(shape)
        checker.SetLevelOfCheck(5)
        checker.Perform()
        if checker.HasErrors() or checker.HasWarnings():
            raise ValueError('最終ソリッドの自己干渉検査が完了しなかった')
        ds = checker.DS()
        intersections = _count_face_interferences(ds, ds.Interferences())
    return dict(watertight=valid and closed and intersections == 0,
                single_solid=len(solids) == 1, solids=len(solids),
                self_intersections=intersections, valid=valid, closed=closed)


def require_valid_solid(shape: "TopoDS_Shape") -> None:
    """`shape` が非空かつ `BRepCheck_Analyzer` 上妥当でなければ `ValueError`。"""
    if shape is None or is_null(shape) or not BRepCheck_Analyzer(shape).IsValid():
        raise ValueError("shape is null or not a valid solid")


def _face_edit_free_edge_count(shape: Any) -> int:
    """面が1枚しか付かない辺(継ぎ目の辺は同じ面が2回付くので数えない)。"""
    ancestors = _face_edit_edge_faces(shape)
    return sum(
        1
        for i in range(1, ancestors.Extent() + 1)
        if not BRep_Tool.Degenerated_s(TopoDS.Edge_s(ancestors.FindKey(i))) and ancestors.FindFromIndex(i).Size() < 2
    )


def is_valid(shape) -> bool:
    """既定の BRepCheck の合否だけを返す。null や体積の前置検査は加えない。"""
    return BRepCheck_Analyzer(shape).IsValid()

"""ブーリアンの入力に載った表示用の三角形分割が、結果を変えない(`brep_operations.build_boolean`)。

題材は合成の文書群(`tools/synthetic_corpus.py` の seed 1・文書 8・seq 29)で、画面へ配った後に MCP で押し出しの join を確定したときの
相手のボディ(三角形分割つき)と、足す柱。BinTools で三角形分割ごと書き出した。
"""
from __future__ import annotations

import io
from pathlib import Path

from OCP.BinTools import BinTools
from OCP.BRep import BRep_Tool
from OCP.TopAbs import TopAbs_FACE
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape

from duocad import brep_geometry, brep_operations, brep_topology

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> TopoDS_Shape:
    shape = TopoDS_Shape()
    BinTools.Read_s(shape, io.BytesIO((FIXTURES / name).read_bytes()))
    return shape


def _has_triangulation(shape: TopoDS_Shape) -> bool:
    return any(
        BRep_Tool.Triangulation_s(TopoDS.Face_s(face), TopLoc_Location()) is not None
        for face in brep_topology.subshapes(shape, TopAbs_FACE)
    )


def test_a_join_on_a_body_carrying_display_triangles_keeps_the_body():
    """落ちたら、画面へ配った後のボディ(三角形分割が載っている)に MCP で join を掛けると、ボディが消えて足した柱だけが残る。"""
    target = _load("join_with_display_triangles_target.bin")
    tool = _load("join_with_display_triangles_tool.bin")
    assert _has_triangulation(target)  # 題材が罠を踏む形のままであること
    result = brep_operations.build_boolean("fuse", target, tool).Shape()
    assert abs(brep_geometry._volume_props(result).Mass() - 1726.3) < 0.1
    assert len(brep_topology.subshapes(result, TopAbs_FACE)) == 26  # 統合の前(罠を踏むと柱の 6 面だけ)

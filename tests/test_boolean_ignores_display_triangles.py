"""表示用の三角形分割が文書の形に残らず(`kernel.tessellate`)、載っていてもブーリアンの結果を変えない(`brep_operations.build_boolean`)。

題材は合成の文書群(`tools/synthetic_corpus.py` の seed 1・文書 8・seq 29)で、画面へ配った後に MCP で押し出しの join を確定したときの
相手のボディ(三角形分割つき)と、足す柱。BinTools で三角形分割ごと書き出した。
"""
from __future__ import annotations

import io
from pathlib import Path

from OCP.BinTools import BinTools
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape

from duocad import brep_geometry, brep_operations, brep_topology, kernel
from duocad.server import Session

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


def _edge_boxes_read_polygons(shape: TopoDS_Shape) -> bool:
    """辺の外接箱(`BRepBndLib.Add` の既定)が、辺に載った三角形分割の折れ線を読んで幾何だけの箱と違うか。"""
    for edge in brep_topology.subshapes(shape, TopAbs_EDGE):
        default, geometric = Bnd_Box(), Bnd_Box()
        BRepBndLib.Add_s(edge, default)
        BRepBndLib.Add_s(edge, geometric, False)
        if default.Get() != geometric.Get():
            return True
    return False


def _carries_display_mesh(shape: TopoDS_Shape) -> bool:
    return _has_triangulation(shape) or _edge_boxes_read_polygons(shape)


def test_the_target_fixture_carries_the_display_mesh():
    """落ちたら、下のテストの判定(`_carries_display_mesh`)が三角形分割を見落としている。"""
    assert _carries_display_mesh(_load("join_with_display_triangles_target.bin"))


def test_measuring_faces_leaves_no_mesh_on_the_shape():
    """落ちたら、画面へ配るために測った三角形分割が形に残り、後の演算(和・干渉・外接箱)の結果が、配ったかどうかで変わる。"""
    shape = _load("join_with_display_triangles_target.bin")
    total = len(brep_topology.subshapes(shape, TopAbs_FACE))
    kernel.tessellate(shape, 0.2)
    assert not _carries_display_mesh(shape)
    measured = kernel.tessellate_faces(shape, {(0.5, 0.7): range(2, total + 1), (0.05, 0.35): [1]})
    assert sorted(measured["faces"]) == list(range(1, total + 1))
    assert not _carries_display_mesh(shape)


def test_a_delivered_and_ghosted_body_keeps_no_mesh():
    """落ちたら、画面への配信かゴーストの分割が、文書のボディの形(確定はゴーストの結果の形を使い回す)に残る。"""
    session = Session(0.2)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 30.0, 20.0)
    session.modify_selection([{"sketch": sketch_id, "index": 1}], [])
    session.extrude(10.0, mode="new")
    session.clear_selection()
    body = session.document.bodies[-1].id
    session.geometry(body)
    assert not _carries_display_mesh(session.document.get_body(body).shape)
    session.modify_selection([{"body": body, "kind": "edge", "index": 1}], [])
    job = session.preview_job("fillet", {"radius": 2.0})
    job.run()
    job.adopt()
    session.fillet(2.0)
    session.geometry(body)
    assert not _carries_display_mesh(session.document.get_body(body).shape)


def test_a_join_on_a_body_carrying_display_triangles_keeps_the_body():
    """落ちたら、画面へ配った後のボディ(三角形分割が載っている)に MCP で join を掛けると、ボディが消えて足した柱だけが残る。"""
    target = _load("join_with_display_triangles_target.bin")
    tool = _load("join_with_display_triangles_tool.bin")
    assert _has_triangulation(target)  # 題材が罠を踏む形のままであること
    result = brep_operations.build_boolean("fuse", target, tool).Shape()
    assert abs(brep_geometry._volume_props(result).Mass() - 1726.3) < 0.1
    assert len(brep_topology.subshapes(result, TopAbs_FACE)) == 26  # 統合の前(罠を踏むと柱の 6 面だけ)

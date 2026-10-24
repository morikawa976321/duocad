"""`duocad/topopt/finalize.py` と最適化の結果の verb のうち、形状最適化の凍結中も製品が使う部分。

`tessellate_body` は干渉チェックのメッシュ経路(`duocad/interference_mesh.py`)が使い、`add_solid_body` は
この verb を含む保存済み文書の再生のために残る。
"""
import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import kernel
from duocad.document import Document
from duocad.io_step import STL_DEVIATION
from duocad.topopt.finalize import CAD_DEVIATION_MM, final_mesh_checks, tessellate_body
from duocad.verbs import add_solid_body

from duocad import (
    brep_geometry as _brep_geometry,
)


def box(lo, hi):
    return BRepPrimAPI_MakeBox(gp_Pnt(*lo), gp_Pnt(*hi)).Shape()


def test_cad_deviation_equals_stl_export():
    """保持・除外の三角形分割は STL 書き出しと同じ粗さ(issue #95)。食い違うと穴の内面の寸法が STL と変わる。"""
    assert CAD_DEVIATION_MM == STL_DEVIATION == 0.01


def test_solid_operation_undo_redo_and_replay():
    doc = Document()
    shape = box((0, 0, 0), (2, 3, 4))
    body_id = add_solid_body(doc, shape, 'optimized')
    assert doc.get_body(body_id).kind == 'solid'
    doc.log.undo(doc)
    assert not doc.bodies
    doc.log.redo(doc)
    assert _brep_geometry.volume(doc.get_body(body_id).shape) == pytest.approx(24)
    replay = Document()
    doc.log.replay(replay)
    assert replay.get_body(body_id).name == 'optimized'
    assert _brep_geometry.volume(replay.get_body(body_id).shape) == pytest.approx(24)


def _fixture(name):
    from pathlib import Path
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, str(Path(__file__).resolve().parent / 'fixtures' / name), BRep_Builder())
    return shape


def test_tessellate_body_repairs_the_gap_reproduction_and_passes_every_check():
    """docs/TOPOPT_GAP_CAUSALITY.md の最小3面ソリッド: 以前は境界辺6・非多様体頂点7で黙って通っていた。"""
    vertices, faces, boundary = tessellate_body(_fixture('gap_minimal_solid.brep'))
    checks = final_mesh_checks(vertices, faces)
    assert checks['failed'] == []
    assert checks['topology']['boundary_edges'] == 0 and checks['topology']['nonmanifold_vertices'] == 0
    assert checks['self_intersections']['pairs'] == []
    assert set(boundary) == {'termination', 'rounds', 'amplified_edges', 'reinstated'}


def test_tessellate_body_refuses_a_shape_whose_boundary_cannot_be_repaired():
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    polygon = BRepBuilderAPI_MakePolygon()
    for point in ((0, 0, 0), (1, 1, 0), (1, 0, 0), (0, 1, 0)):
        polygon.Add(gp_Pnt(*point))
    polygon.Close()
    bowtie = BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()
    with pytest.raises(ValueError, match='三角形分割できない面'):
        tessellate_body(kernel.make_compound([bowtie, box((3, 3, 3), (4, 4, 4))]))

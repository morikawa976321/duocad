"""STL書き出し(MCPツール `export_stl`)は、画面が土台の粗さで配る三角形とビット単位で同じ。

落ちたら、書き出したSTLが画面で確かめた形と別の計算になっている(角度偏差・呼ぶ関数・
Bodyのまとめ方のどれかが画面の経路とずれた)。
"""
from __future__ import annotations

import struct

import numpy as np
import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from duocad import kernel
from duocad.document import Body
from duocad.mcp_server import DocumentTools
from duocad.server import Session


@pytest.mark.parametrize('existing', [False, True])
def test_unresolved_face_prevents_partial_stl_export(tmp_path, existing):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace
    from duocad.io_step import export_stl

    wire = BRepBuilderAPI_MakePolygon()
    for point in ((0, 0, 0), (1, 1, 0), (1, 0, 0), (0, 1, 0)):
        wire.Add(gp_Pnt(*point))
    wire.Close()
    broken = BRepBuilderAPI_MakeFace(wire.Wire(), True).Face()
    shape = kernel.make_compound([broken, BRepPrimAPI_MakeBox(5., 5., 5.).Shape()])
    session = Session(None, 0.2)
    session.document.add_body(Body(id='part', name='part', kind='solid', shape=shape))
    path = tmp_path / 'part.stl'
    if existing:
        path.write_bytes(b'existing file')
    with pytest.raises(ValueError, match='cannot write STL.*分割できない面'):
        export_stl(session.document, path, 0.2)
    if existing:
        assert path.read_bytes() == b'existing file'
    else:
        assert not path.exists()


@pytest.mark.parametrize('field', ['untriangulated', 'boundary'])
def test_each_incomplete_tessellation_diagnostic_blocks_export(tmp_path, monkeypatch, field):
    from duocad.io_step import export_stl

    shape = BRepPrimAPI_MakeBox(5., 5., 5.).Shape()
    payload = kernel.tessellate(shape, 0.2)
    payload['untriangulated'] = [1] if field == 'untriangulated' else []
    payload['boundary']['unresolved'] = [dict(face=1, issues=[])] if field == 'boundary' else []
    monkeypatch.setattr(kernel, 'tessellate', lambda *args: payload)
    session = Session(None, 0.2)
    session.document.add_body(Body(id='part', name='part', kind='solid', shape=shape))
    path = tmp_path / 'part.stl'
    with pytest.raises(ValueError, match='cannot write STL'):
        export_stl(session.document, path, 0.2)
    assert not path.exists()


def _holed_box():
    """曲面(円筒の穴)を持つ形。角度偏差が違うと三角形の枚数が変わる。"""
    box = BRepPrimAPI_MakeBox(40.0, 30.0, 10.0).Shape()
    hole = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(20.0, 15.0, -1.0), gp_Dir(0.0, 0.0, 1.0)), 6.0, 12.0
    ).Shape()
    return BRepAlgoAPI_Cut(box, hole).Shape()


def _read_stl(path) -> np.ndarray:
    """バイナリSTLの頂点を `(三角形数, 3, 3)` の float32 で返す(法線は見ない)。"""
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0]
    assert len(data) == 84 + 50 * count, "宣言した三角形数とファイル長が合わない"
    record = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", 9), ("attr", "<u2")])
    return np.frombuffer(data, dtype=record, count=count, offset=84)["vertices"].reshape(-1, 3, 3)


def _view_triangles(session: Session, body_id: str) -> np.ndarray:
    """画面へ配られる三角形を、面の順・三角形の順のまま float32 へ落とす。"""
    chunks = []
    for face in session.geometry(body_id)["faces"]:
        positions = np.asarray(face["positions"], dtype=np.float64).reshape(-1, 3)
        indices = np.asarray(face["indices"], dtype=np.int64).reshape(-1, 3)
        chunks.append(positions[indices])
    return np.concatenate(chunks).astype(np.float32)


@pytest.mark.parametrize("deviation", [0.2, 0.05])
def test_the_stl_holds_exactly_the_triangles_the_view_shows(tmp_path, deviation):
    session = Session(None, deviation)
    session.document.add_body(Body(id="part", name="part", kind="solid", shape=_holed_box()))
    view = _view_triangles(session, "part")

    path = tmp_path / "part.stl"
    DocumentTools(session).call("export_stl", {"path": str(path)})

    assert np.array_equal(_read_stl(path), view)


def test_several_b_rep_bodies_are_written_one_by_one_as_the_view_measures_them(tmp_path):
    """画面はBodyごとに測る。書き出しも1つのcompoundへまとめて測り直さない。"""
    session = Session(None, 0.2)
    session.document.add_body(Body(id="a", name="a", kind="solid", shape=_holed_box()))
    moved = kernel.translate_shape(_holed_box(), 60.0, 0.0, 0.0)
    session.document.add_body(Body(id="b", name="b", kind="solid", shape=moved))
    view = np.concatenate([_view_triangles(session, "a"), _view_triangles(session, "b")])

    path = tmp_path / "two.stl"
    DocumentTools(session).call("export_stl", {"path": str(path)})

    assert np.array_equal(_read_stl(path), view)


def test_stl_export_refuses_a_triangle_that_collapses_in_float32(tmp_path):
    """float64 では面積のある三角形でも float32 に丸めると2頂点が同じ値になる(1000 付近の刻みは約6e-5)。
    書き出しを理由つきで止め、既存のファイルには触らない。頂点を動かして通すことはしない。"""
    from duocad.document import make_mesh
    session = Session(None, 0.2)
    mesh = make_mesh([1000.0, 0.0, 0.0, 1000.00001, 0.0, 0.0, 1000.0, 1.0, 0.0], [0, 1, 2])
    session.document.add_body(Body(id="m", name="m", kind="mesh", mesh=mesh))
    path = tmp_path / "m.stl"
    path.write_bytes(b"previous")
    with pytest.raises(ValueError, match="float32"):
        DocumentTools(session).call("export_stl", {"path": str(path)})
    assert path.read_bytes() == b"previous"


def test_stl_export_reads_back_float32_that_passes_the_same_validation(tmp_path):
    session = Session(None, 0.2)
    session.document.add_body(Body(id="part", name="part", kind="solid", shape=_holed_box()))
    path = tmp_path / "part.stl"
    DocumentTools(session).call("export_stl", {"path": str(path)})
    from duocad.meshbody import validate_float32
    assert validate_float32(_read_stl(path).reshape(-1, 9).tolist()) == {}

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

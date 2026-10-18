"""STL書き出し(MCPツール `export_stl`)は**固定の粗さ**(`io_step.STL_DEVIATION`)で、
画面と同じ経路で測る —— 同じ粗さを土台に置けば三角形はビット単位で一致する。

落ちたら、書き出したSTLが画面で確かめた形と別の計算になっている(角度偏差・呼ぶ関数・
Bodyのまとめ方のどれかが画面の経路とずれた)か、粗さが土台に引きずられている。
"""
from __future__ import annotations

import struct

import numpy as np
import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from duocad import io_step, kernel, query
from duocad.boundary_check import degenerate_mask
from duocad.document import Body, Document
from duocad.mcp_server import DocumentTools
from duocad.server import Session
from duocad.verbs import fillet
from document_support import new_box


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
        export_stl(session.document, path)
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
        export_stl(session.document, path)
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


def test_the_stl_holds_exactly_the_triangles_the_view_shows(tmp_path):
    """**土台を書き出しの固定値に合わせれば**、画面の三角形とSTLはビット単位で同じ。"""
    session = Session(None, io_step.STL_DEVIATION)
    session.document.add_body(Body(id="part", name="part", kind="solid", shape=_holed_box()))
    view = _view_triangles(session, "part")

    path = tmp_path / "part.stl"
    DocumentTools(session).call("export_stl", {"path": str(path)})

    assert np.array_equal(_read_stl(path), view)


def test_the_stl_does_not_follow_the_session_baseline(tmp_path):
    """**粗さは固定**。土台(`--deviation`)を粗くしても、書き出しは固定値のまま細かい。

    落ちたら、書き出しが画面の設定を見に行っている(曲面のある形で三角形数が土台に追随する)。
    """
    session = Session(None, 0.2)
    session.document.add_body(Body(id="part", name="part", kind="solid", shape=_holed_box()))

    path = tmp_path / "part.stl"
    DocumentTools(session).call("export_stl", {"path": str(path)})

    assert len(_read_stl(path)) > len(_view_triangles(session, "part"))


def test_several_b_rep_bodies_are_written_one_by_one_as_the_view_measures_them(tmp_path):
    """画面はBodyごとに測る。書き出しも1つのcompoundへまとめて測り直さない。"""
    session = Session(None, io_step.STL_DEVIATION)
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


def _box_with_fillets(edge_selector) -> object:
    doc = Document()
    body_id = new_box(doc, 20.0)
    edges = query.describe_edges(doc.get_body(body_id).shape)
    fillet(doc, [edge["index"] for edge in edge_selector(edges)], 3.0, body_id=body_id)
    return doc.get_body(body_id).shape


def _edges_at_the_corner_of_the_origin(edges):
    """原点の角(0,0,0)に端点を持つ3辺。3辺が集まる角のフィレットは球面片(角)を作り、極の三角形を出す。"""
    return [e for e in edges if any(all(abs(c) < 1e-9 for c in e[end]) for end in ("start", "end"))]


_POLE_SHAPES = {
    "sphere": lambda: BRepPrimAPI_MakeSphere(5.0).Shape(),
    "cone_apex": lambda: BRepPrimAPI_MakeCone(6.0, 0.0, 10.0).Shape(),
    "all_twelve_edges_filleted": lambda: _box_with_fillets(lambda edges: edges),
    "three_edges_at_a_corner_filleted": lambda: _box_with_fillets(_edges_at_the_corner_of_the_origin),
}


def _directed_edge_pairs_are_complete(triangles: np.ndarray) -> bool:
    """頂点を座標で溶接したとき、どの辺も向きが逆の2枚の三角形が1組ずつ使う(閉じた2-多様体)。"""
    flat = triangles.reshape(-1, 3)
    _, welded = np.unique(flat, axis=0, return_inverse=True)
    corners = welded.reshape(-1, 3)
    directed: dict = {}
    for a, b, c in corners.tolist():
        for edge in ((a, b), (b, c), (c, a)):
            directed[edge] = directed.get(edge, 0) + 1
    return all(count == 1 and (edge[1], edge[0]) in directed for edge, count in directed.items())


@pytest.mark.parametrize("name", _POLE_SHAPES)
def test_a_b_rep_with_pole_triangles_is_written_without_the_zero_area_ones(tmp_path, name):
    """OCCT は球の極・円錐の頂点・角のフィレットの球面片に面積0の三角形を出す。落ちたら、それが float32 検査に
    拒まれてこれらの形が書き出せない(ファイルが作られない)か、除いたことで STL が閉じなくなっている。"""
    shape = _POLE_SHAPES[name]()
    session = Session(None, io_step.STL_DEVIATION)
    session.document.add_body(Body(id="part", name="part", kind="solid", shape=shape))
    view = _view_triangles(session, "part")
    zero_area = degenerate_mask(view.astype(np.float64))
    assert zero_area.any(), "この形が極の三角形を出さなくなった(題材を選び直す)"

    path = tmp_path / "part.stl"
    DocumentTools(session).call("export_stl", {"path": str(path)})

    written = _read_stl(path)
    assert np.array_equal(written, view[~zero_area]), "書いた三角形は画面の三角形から面積0のものだけを除いたもの"
    assert not degenerate_mask(written.astype(np.float64)).any()
    assert _directed_edge_pairs_are_complete(written)
    signed = np.einsum("ij,ij->i", written[:, 0].astype(np.float64), np.cross(written[:, 1], written[:, 2]).astype(np.float64)).sum() / 6.0
    assert signed == pytest.approx(kernel.volume(shape), rel=0.03), "面積0を除いても体積は変わらない(三角形の近似ぶんは残る)"


def test_stl_export_still_refuses_a_mesh_body_with_a_zero_area_triangle(tmp_path):
    """面積0の三角形を除くのは B-rep の分割結果だけ。メッシュボディは持っている三角形のまま検査し、断る。"""
    from duocad.document import make_mesh
    session = Session(None, 0.2)
    mesh = make_mesh([0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0], [0, 1, 2])
    session.document.add_body(Body(id="m", name="m", kind="mesh", mesh=mesh))
    path = tmp_path / "m.stl"
    with pytest.raises(ValueError, match="cannot write STL.*degenerate"):
        DocumentTools(session).call("export_stl", {"path": str(path)})
    assert not path.exists()


_ONE_TRIANGLE = [[((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))]]


@pytest.mark.parametrize("failing", ["open", "replace"])
def test_a_failed_write_leaves_the_previous_stl_and_no_temporary_file(tmp_path, monkeypatch, failing):
    """落ちたら、書き込みの失敗が保存先を消す・切り詰める(前回の正常な出力が失われる)か、一時ファイルが残る。"""
    from duocad import meshbody

    path = tmp_path / "x.stl"
    meshbody.write_stl(path, _ONE_TRIANGLE)
    previous = path.read_bytes()

    def refuse(*args, **kwargs):
        raise PermissionError("refused")

    if failing == "open":
        monkeypatch.setattr(meshbody, "open", refuse, raising=False)
    else:
        monkeypatch.setattr(meshbody.os, "replace", refuse)
    with pytest.raises(PermissionError):
        meshbody.write_stl(path, [[((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0))]])

    assert path.read_bytes() == previous
    assert [entry.name for entry in tmp_path.iterdir()] == ["x.stl"]

"""三角形メッシュのボディ(`Body.kind == "mesh"`)の受入検査。

**主張は「表示とSTL書き出しだけができて、編集は必ず断られる」**である。だから
「できること」(§2〜§5)と「断られること」(§6)を同じ重さで固定する —— 断りが緩むと、
数万面のシェルをOCCTへ入れる経路が黙って開き、選択1回が秒単位になる
(理由は `duocad/meshbody.py` のモジュールdocstring)。

B-repのSTLが画面と同じ三角形になることは `tests/test_stl_export.py` が見る。
"""
from __future__ import annotations

import struct

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import io_step, kernel, meshbody, optimize, predicates, selection, sketch
from duocad.document import MESH_KIND, Body, Document, make_mesh
from duocad.mcp_server import DocumentTools
from duocad.oplog import OpLog
from duocad.server import Session
from duocad.speculative import speculative
from duocad.verbs import add_mesh_body, chamfer, delete, extrude_faces, fillet, move_body
from duocad.verbs.extrude import _resolve_target
from document_support import replay_through_json
from session_support import session_with_box

SIDE = 20.0
DEVIATION = 0.2

#: 原点まわりの単位四面体(体積 1/6)。**巻き順は外向き**。
TETRA_POSITIONS = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
TETRA_INDICES = [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3]

#: 断り文に必ず入る「次に何ができるか」。**ここが落ちたら断りとして不合格**である。
EXPECTED_HINT = (
    "できるのは 表示 / STLへの書き出し(export_stl) / 削除(delete) / 表示の切り替え(set_visible)"
)


def _box_shape():
    return BRepPrimAPI_MakeBox(SIDE, SIDE, SIDE).Shape()


def _tetra_doc():
    doc = Document()
    body_id = add_mesh_body(doc, TETRA_POSITIONS, TETRA_INDICES, name="tetra")
    return doc, body_id


def _tetra_session():
    """メッシュボディを1つ持つ `Session`。**`Session` 越しに作る** —— `session.document` へ
    代入しても `session.view` は古い文書を握ったままで、配りものが別の文書を見る。"""
    session = Session(None, DEVIATION)
    body_id = session.add_mesh_body(TETRA_POSITIONS, TETRA_INDICES, "tetra")["body_id"]
    return session, body_id


def _stl_triangle_count(path) -> int:
    """バイナリSTLのヘッダが宣言する三角形数(**実体の長さでも検算する**)。"""
    data = path.read_bytes()
    count = struct.unpack("<I", data[80:84])[0]
    assert len(data) == 84 + 50 * count, "宣言した三角形数とファイル長が合わない"
    return count


def _box_triangle_count() -> int:
    """書き出しの固定値で測る(平面だけの箱なので粗さで枚数は変わらないが、口を1つに保つ)。"""
    tessellated = kernel.tessellate(_box_shape(), io_step.STL_DEVIATION)
    return sum(len(face["indices"]) // 3 for face in tessellated["faces"])


# ======================================================================== §1 データモデル
def test_mesh_rejects_a_ragged_or_out_of_range_triangle_list():
    """**壊れた三角形群は `Mesh` を作る時点で落ちる**(画面まで届かせない)。"""
    with pytest.raises(ValueError, match="divisible by 3"):
        make_mesh([0.0, 0.0], TETRA_INDICES)
    with pytest.raises(ValueError, match="divisible by 3"):
        make_mesh(TETRA_POSITIONS, [0, 1])
    with pytest.raises(ValueError, match="out of range"):
        make_mesh(TETRA_POSITIONS, [0, 1, 4])
    with pytest.raises(ValueError, match="no triangle"):
        make_mesh(TETRA_POSITIONS, [])


def test_a_body_carries_exactly_one_kind_of_geometry():
    """**形の持ち方はkindごとに1本**。混ぜられると `body.shape is None` の分岐が意味を失う。"""
    mesh = make_mesh(TETRA_POSITIONS, TETRA_INDICES)
    with pytest.raises(ValueError, match="needs a Mesh"):
        Body(id="b", name="b", kind=MESH_KIND)
    with pytest.raises(ValueError, match="must not carry a B-rep shape"):
        Body(id="b", name="b", kind=MESH_KIND, mesh=mesh, shape=_box_shape())
    with pytest.raises(ValueError, match="must not carry a mesh"):
        Body(id="b", name="b", kind="solid", shape=_box_shape(), mesh=mesh)


# ======================================================================== §2 作る経路
def test_add_mesh_body_puts_the_triangles_on_the_body_and_one_line_in_the_log():
    doc, body_id = _tetra_doc()
    body = doc.get_body(body_id)
    assert body.kind == MESH_KIND
    assert body.shape is None
    assert body.mesh.triangle_count == 4
    assert body.mesh.vertex_count == 4
    assert body.name == "tetra"
    assert [op.verb for op in doc.log.entries] == ["add_mesh_body"]


def test_a_b_rep_body_can_be_turned_into_a_mesh_body_through_the_kernel():
    """**要件7の経路** —— 既存のB-repをテッセレーションしてメッシュボディにする。

    元のボディは**消えない**(足すだけ)。体積が箱と合うことで、面をまたぐ頂点オフセットが
    正しいこと(`meshbody.from_tessellation`)まで見る —— オフセットを忘れると全ての面が
    1枚目の頂点を指し、体積が崩れる。
    """
    doc = Document()
    doc.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    mesh = meshbody.from_tessellation(kernel.tessellate(_box_shape(), DEVIATION))
    body_id = add_mesh_body(doc, mesh.positions, mesh.indices, name="box_mesh")
    assert [b.id for b in doc.bodies] == ["box", body_id]
    assert meshbody.volume(doc.get_body(body_id).mesh) == pytest.approx(SIDE**3, rel=1e-9)


# ======================================================================== §3 保存と復元
def test_a_mesh_body_survives_save_and_reopen():
    """**保存の実体は操作ログ**なので、ログを書いて読み直して再生する経路をそのまま踏む。"""
    doc, body_id = _tetra_doc()
    restored = replay_through_json(doc)
    body = restored.get_body(body_id)
    assert body.kind == MESH_KIND
    assert body.name == "tetra"
    assert list(body.mesh.positions) == TETRA_POSITIONS
    assert list(body.mesh.indices) == TETRA_INDICES


def test_undo_and_redo_move_the_mesh_body_in_and_out():
    doc, body_id = _tetra_doc()
    doc.log.undo(doc)
    assert [b.id for b in doc.bodies] == []
    doc.log.redo(doc)
    assert [b.id for b in doc.bodies] == [body_id]
    assert doc.get_body(body_id).mesh.triangle_count == 4


def test_a_mesh_body_survives_a_parameter_rebuild():
    """`parameters.rebuild()` は **`doc.bodies` を空にしてログを頭から再生する**別の経路。

    `OpLog.replay` の検査(上)とは通る道が違い、`set_parameter` を1回打つだけで踏む ——
    ここで復元されないと、寸法をひとつ直しただけで最適化結果が黙って消える。
    """
    session = session_with_box(SIDE, SIDE, SIDE)
    tools = DocumentTools(session)
    sketch_id = tools.call("sketch_create", {"base_plane": "XY"})["sketch_id"]
    tools.call(
        "add_rectangle",
        {"sketch_id": sketch_id, "corner": [0.0, 0.0], "width": 10.0, "height": 10.0},
    )
    added = tools.call("add_mesh_body", {"positions": TETRA_POSITIONS, "indices": TETRA_INDICES})
    name = next(iter(session.document.parameters))
    tools.call("set_parameter", {"name": name, "value": 14.0})
    body = session.document.get_body(added["body_id"])
    assert body.kind == MESH_KIND
    assert list(body.mesh.indices) == TETRA_INDICES


def test_speculative_restores_a_mesh_body():
    """`speculative` の控えから `mesh` が落ちていないこと(プレビューの土台)。"""
    doc, body_id = _tetra_doc()
    with speculative(doc):
        doc.remove_body(body_id)
    assert doc.get_body(body_id).mesh.triangle_count == 4


# ======================================================================== §4 画面へ配る
def test_the_geometry_payload_has_the_same_shape_as_a_tessellated_b_rep():
    """**`web/` を1行も触らずに描ける**ことの根拠 —— 配りものの形がB-repと同じであること。"""
    session, body_id = _tetra_session()
    payload = session.view.geometry(body_id)
    reference = kernel.tessellate_selective(_box_shape(), None, DEVIATION, 0.5)
    assert set(payload) == set(reference)
    assert set(payload["faces"][0]) == set(reference["faces"][0])
    assert set(payload["bbox"]) == set(reference["bbox"])


def test_the_mesh_is_delivered_as_exactly_one_face_with_the_stored_triangles():
    """三角形1枚を1面にしない(ドローコールが三角形の数だけ増えるため)。"""
    session, body_id = _tetra_session()
    payload = session.view.geometry(body_id)
    assert payload["face_count"] == 1
    assert len(payload["faces"]) == 1
    assert payload["faces"][0]["positions"] == TETRA_POSITIONS
    assert payload["faces"][0]["indices"] == TETRA_INDICES
    assert payload["untriangulated"] == []


def test_the_state_broadcast_does_not_carry_the_triangles_but_the_saved_log_does():
    """落ちたら、メッシュを1つ足した後の全操作の状態配信に三角形の全部が乗る(最適化の結果で1配信あたり数MB)。"""
    session, _ = _tetra_session()
    delivered = session.state_meta()["log"][-1]["args"]
    assert "配信しない" in delivered["positions"] and "配信しない" in delivered["indices"], delivered
    saved = OpLog.from_json(session.document.log.to_json()).entries[-1].args
    assert saved["positions"] == TETRA_POSITIONS and saved["indices"] == TETRA_INDICES


def test_body_meta_reports_one_face_and_the_mesh_volume():
    """`kernel.face_count(None)` を踏まずに軽量な状態が作れること(ドラッグ選択の律速点)。"""
    session, body_id = _tetra_session()
    meta = session.view.body_meta(session.document.get_body(body_id))
    assert meta["kind"] == MESH_KIND
    assert meta["face_count"] == 1
    assert meta["volume"] == pytest.approx(1.0 / 6.0)


def test_the_revision_moves_when_a_mesh_body_is_hidden():
    """版の鍵に `mesh` を入れ忘れると `id(None)` が固定され、版が二度と上がらない。"""
    session, body_id = _tetra_session()
    first = session.view.body_revision(body_id)
    selection.set_visible(session.document, body_id, False)
    assert session.view.body_revision(body_id) != first


def test_a_hidden_mesh_body_is_dropped_from_the_state_like_a_solid():
    session, body_id = _tetra_session()
    selection.set_visible(session.document, body_id, False)
    entry = next(b for b in session.state()["bodies"] if b["body_id"] == body_id)
    assert entry["geometry"] is None


# ======================================================================== §5 消す
def test_a_mesh_body_can_be_deleted_and_the_delete_can_be_undone():
    """**編集できないボディも消せる**(消せないと、間違えて入れた形を取り除けない)。"""
    doc, body_id = _tetra_doc()
    delete(doc, bodies=[body_id])
    assert [b.id for b in doc.bodies] == []
    doc.log.undo(doc)
    body = doc.get_body(body_id)
    assert body.kind == MESH_KIND
    assert body.mesh.triangle_count == 4


# ======================================================================== §6 断ること
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda doc, body_id: fillet(doc, edges=[1], radius=1.0, body_id=body_id),
            id="fillet",
        ),
        pytest.param(
            lambda doc, body_id: chamfer(doc, edges=[1], distance=1.0, body_id=body_id),
            id="chamfer",
        ),
        pytest.param(
            lambda doc, body_id: move_body(doc, body_ids=[body_id], dx=1.0, dy=0.0, dz=0.0),
            id="move_body",
        ),
        # 領域の押し出しは `_common.editable_body` を通らない専用の解決関数を持つので、
        # そこを直に叩く(領域を1つ作るには別のスケッチが要り、断りの検査には要らない)。
        pytest.param(
            lambda doc, body_id: _resolve_target(doc, "join", body_id),
            id="extrude(_resolve_target)",
        ),
    ],
)
def test_edit_verbs_refuse_a_mesh_body_and_say_what_is_possible(call):
    """**黙って落ちない・黙って壊さない。** 断り文には「次に何ができるか」が要る。"""
    doc, body_id = _tetra_doc()
    doc.set_active_body(body_id)
    with pytest.raises(ValueError) as error:
        call(doc, body_id)
    message = str(error.value)
    assert "is a mesh body" in message
    assert EXPECTED_HINT in message
    assert [op.verb for op in doc.log.entries] == ["add_mesh_body"], "断ったのにログが伸びた"


def test_extrude_refuses_the_faces_of_a_mesh_body():
    """面を受ける `extrude` も、メッシュボディの面は押し出せないと断る(面の解決は `resolve_shape_ref`)。"""
    doc, body_id = _tetra_doc()
    with pytest.raises(ValueError, match="メッシュボディの面は押し出せない"):
        extrude_faces(doc, [{"body": body_id, "index": 1}], 1.0)
    assert [op.verb for op in doc.log.entries] == ["add_mesh_body"], "断ったのにログが伸びた"


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda doc, body_id: selection.select_face(doc, body_id, 1), id="face"),
        pytest.param(lambda doc, body_id: selection.select_edge(doc, body_id, 1), id="edge"),
        pytest.param(lambda doc, body_id: selection.select_vertex(doc, body_id, 1), id="vertex"),
    ],
)
def test_faces_edges_and_vertices_of_a_mesh_body_cannot_be_selected(call):
    """**選択の粒度はボディ単位だけ**(メッシュには面の指紋が無い)。"""
    doc, body_id = _tetra_doc()
    with pytest.raises(ValueError, match="is a mesh body"):
        call(doc, body_id)


def test_the_bulk_selection_path_refuses_a_mesh_body_too():
    """一括選択は数え上げを減らすために `select_face` を通らないので、断りも写してある。"""
    doc, body_id = _tetra_doc()
    with pytest.raises(ValueError, match="is a mesh body"):
        selection.propose_selection(doc, refs=[{"body": body_id, "kind": "face", "index": 1}])
    assert selection.get_selection(doc).items == [], "断ったのに選択が変わった"


def test_a_mesh_body_can_be_selected_as_a_whole():
    doc, body_id = _tetra_doc()
    ref = selection.select_body(doc, body_id)
    assert ref == {"body": body_id, "kind": "body", "index": None, "provenance": None}


def test_edges_and_vertices_are_not_delivered_for_a_mesh_body():
    session, body_id = _tetra_session()
    with pytest.raises(ValueError, match="is a mesh body"):
        session.view.edges(body_id)
    with pytest.raises(ValueError, match="is a mesh body"):
        session.view.vertices(body_id)


# --------------------------------------------------- §6.5 述語・面を名指しする他の経路
#
# **「名指ししたら断る / 導出した集合からは黙って外す」**が、このタスクの門の一貫した線
# である(`predicates._target_bodies` のdocstring)。§6 が前半、ここが後半を見る。
def test_predicates_skip_mesh_bodies_when_no_body_was_named():
    """**メッシュだけの文書でも述語が落ちない**。

    走査集合は呼び出し側の主張ではなく導出なので、**黙って外す**(不可視Bodyと同じ扱い)。
    ここで断ると、最適化結果のメッシュが文書に1つあるだけで述語選択が丸ごと使えなくなる。
    """
    doc, _ = _tetra_doc()
    assert predicates.evaluate(doc, {"kind": "edge", "where": []}) == []
    assert predicates.evaluate(doc, {"kind": "face", "where": []}) == []


def test_predicates_still_see_the_b_rep_bodies_of_a_mixed_document():
    """混在した文書では**B-rep側が今までどおり全部出る**(メッシュのぶんだけ落とす)。"""
    doc, _ = _tetra_doc()
    doc.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    edges = predicates.evaluate(doc, {"kind": "edge", "where": []})
    faces = predicates.evaluate(doc, {"kind": "face", "where": []})
    assert len(edges) == 12 and len(faces) == 6, "立方体の辺12・面6が出ない"
    assert all(ref["body"] == "box" for ref in edges + faces), "メッシュボディが混ざった"


def test_on_boundary_of_face_still_works_when_a_mesh_body_is_visible():
    """**メッシュを外してから単一Body検査に掛ける**(後にすると可視のメッシュ1つで断られる)。"""
    doc, _ = _tetra_doc()
    doc.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    matched = predicates.evaluate(
        doc, {"kind": "edge", "where": [{"op": "on_boundary_of_face", "face": 1}]}
    )
    assert len(matched) == 4, "面1枚の外周辺4本が出ない"


def test_naming_a_mesh_body_in_a_predicate_is_refused():
    """名指しは断る —— 黙って0件を返すと「条件に合う辺が無かった」と読まれる。"""
    doc, body_id = _tetra_doc()
    with pytest.raises(ValueError) as error:
        predicates.evaluate(doc, {"kind": "edge", "body": body_id, "where": []})
    assert EXPECTED_HINT in str(error.value)


def test_the_face_fingerprint_of_a_mesh_body_is_refused():
    """形状最適化の条件(拘束面・荷重面)が名指しでメッシュの面を測ろうとする経路。"""
    doc, body_id = _tetra_doc()
    with pytest.raises(ValueError) as error:
        optimize.face_fingerprint(doc, body_id, 1)
    assert EXPECTED_HINT in str(error.value)


def test_a_sketch_cannot_be_pinned_to_a_mesh_body():
    """面に貼るスケッチの解決(`sketch.resolve_plane`)も名指しの面参照である。"""
    doc, body_id = _tetra_doc()
    with pytest.raises(ValueError) as error:
        sketch.resolve_plane(doc, {"kind": "face", "body": body_id, "index": 1})
    assert EXPECTED_HINT in str(error.value)


def test_measure_reports_a_mesh_body_selected_as_a_whole():
    """選択に入ったメッシュボディを `measure` が測れる(`body_meta` 経由なので落ちない)。"""
    session = session_with_box(SIDE, SIDE, SIDE)
    tools = DocumentTools(session)
    added = tools.call("add_mesh_body", {"positions": TETRA_POSITIONS, "indices": TETRA_INDICES})
    tools.call(
        "propose_selection",
        {"refs": [{"body": added["body_id"], "kind": "body", "index": None}]},
    )
    measured = tools.call("measure", {})
    entry = next(item for item in measured["items"] if item["body"] == added["body_id"])
    assert entry["kind"] == "body"
    assert entry["face_count"] == 1
    assert entry["volume"] == pytest.approx(1.0 / 6.0)


# ======================================================================== §7 書き出し
def test_a_mesh_only_document_writes_the_triangles_it_has(tmp_path):
    """**テッセレーションを通さない** —— 4三角形のメッシュは4三角形のSTLになる。"""
    doc, _ = _tetra_doc()
    path = tmp_path / "mesh.stl"
    io_step.export_stl(doc, path)
    assert _stl_triangle_count(path) == 4


def test_a_mixed_document_writes_both_into_one_stl(tmp_path):
    """混在は**両方書く**。片方を黙って落とさない。"""
    doc, _ = _tetra_doc()
    doc.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    path = tmp_path / "mixed.stl"
    io_step.export_stl(doc, path)
    assert _stl_triangle_count(path) == 4 + _box_triangle_count()


def test_a_hidden_mesh_body_is_left_out_of_the_stl(tmp_path):
    """可視性の規律(「見えないものは書き出さない」)がメッシュにも同じに効く。"""
    doc, body_id = _tetra_doc()
    doc.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    selection.set_visible(doc, body_id, False)
    path = tmp_path / "only-box.stl"
    io_step.export_stl(doc, path)
    assert io_step.export_skipped_hidden(doc) == [body_id]
    box_only = Document()
    box_only.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    direct = tmp_path / "box.stl"
    io_step.export_stl(box_only, direct)
    assert path.read_bytes() == direct.read_bytes()


def test_step_export_refuses_a_document_that_holds_a_mesh(tmp_path):
    """STEPは境界表現の形式で、三角形を書く先が無い。**ファイルも作らない。**"""
    doc, _ = _tetra_doc()
    doc.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    path = tmp_path / "mixed.step"
    with pytest.raises(ValueError, match="cannot export a mesh body to STEP"):
        io_step.export_step(doc, path)
    assert not path.exists()


def test_an_empty_mesh_never_reaches_the_stl_writer(tmp_path):
    path = tmp_path / "empty.stl"
    with pytest.raises(ValueError, match="empty mesh"):
        meshbody.write_stl(path, [])
    assert not path.exists()


# ======================================================================== §8 MCP
def test_the_mcp_tool_adds_a_mesh_body():
    session = session_with_box(SIDE, SIDE, SIDE)
    tools = DocumentTools(session)
    result = tools.call(
        "add_mesh_body",
        {"positions": TETRA_POSITIONS, "indices": TETRA_INDICES, "name": "tetra"},
    )
    body = session.document.get_body(result["body_id"])
    assert body.kind == MESH_KIND
    assert body.mesh.triangle_count == 4


def test_describe_document_survives_a_mesh_body():
    """`describe_document` はB-repを前提に `kernel.face_count` / `query.volume` を叩く。"""
    session = session_with_box(SIDE, SIDE, SIDE)
    tools = DocumentTools(session)
    tools.call("add_mesh_body", {"positions": TETRA_POSITIONS, "indices": TETRA_INDICES})
    kinds = {body["kind"] for body in tools.call("describe_document", {})["bodies"]}
    assert kinds == {"solid", MESH_KIND}

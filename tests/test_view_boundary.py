"""`Session` と `DocumentView` の継ぎ目そのもの。

配りもの(状態配信・版・キャッシュ)を `duocad/view.py` へ切り出したときに**新しく生まれた
壊れ方**だけをここで固定する。中身の正しさ(何を配るか・いつ剪定するか)は
`tests/test_body_cache_pruning.py` と `tests/test_body_meta_volume_cache.py` が見ている。

固定するのは3点:

1. **警告のリストは1つ**。`DocumentView` が積んだ警告が `Session.take_warnings()` から出る。
   `take_warnings` が `self.warnings = []` と**束ね直す**と、以後 `view` は誰も読まない
   古いリストへ積み続ける —— 例外も赤も出ない、静かに消えるだけの事故になる。
2. **`view` が古い文書を握らない**。`Session._replace_document` は `self.document` を丸ごと
   差し替えるので、`view.reset()` を呼び忘れると前の文書を配り続ける。
3. **プレビュー中は剪定しない**。「いまプレビュー中か」の持ち主は `Session` で、`view` へは
   引数で渡る。渡し忘れ・逆向きの受け取りをここで捕まえる。
"""
from __future__ import annotations

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad.document import Body
from duocad.region import region_ref, regions
from duocad.server import Session
from duocad.sketch import add_rectangle, sketch_create
from duocad.verbs import extrude

SIDE = 20.0


def _box_session(tmp_path) -> Session:
    """`tests/test_body_cache_pruning.py` と同じ作り(base bodyを1つ直に足すだけ)。"""
    session = Session(None, 0.2, models_dir=tmp_path)
    session.document.add_body(
        Body(
            id="box",
            name="box",
            kind="solid",
            shape=BRepPrimAPI_MakeBox(SIDE, SIDE, SIDE).Shape(),
        )
    )
    return session


def _extrude_a_second_body(session: Session) -> str:
    sketch_id = sketch_create(session.document, "XY")
    add_rectangle(session.document, sketch_id, (0.0, 0.0), 10.0, 10.0)
    boss = regions(session.document, sketch_id)
    operation = extrude(
        session.document, [region_ref(sketch_id, boss[0].index)], 5.0, mode="new"
    )
    return operation.args["body_id"]


def test_the_view_and_the_session_share_one_warnings_list(tmp_path) -> None:
    """`view` が積んだ警告が `Session.take_warnings()` から出る —— **吸い出した後も**。"""
    session = _box_session(tmp_path)
    assert session.view.warnings is session.warnings, "最初から別のリストになっている"

    session.view.warnings.append("view が積んだ警告")
    assert session.take_warnings() == ["view が積んだ警告"]
    assert session.warnings == []

    # ここが本題 —— 吸い出しでリストを束ね直していると、以後 `view` の警告は届かない。
    session.view.warnings.append("吸い出した後に view が積んだ警告")
    assert session.take_warnings() == ["吸い出した後に view が積んだ警告"]
    assert session.view.warnings is session.warnings


def test_replacing_the_document_repoints_the_view(tmp_path) -> None:
    """文書を差し替えたら `view` も新しい文書を見る(古い文書を配り続けない)。"""
    session = _box_session(tmp_path)
    _extrude_a_second_body(session)
    session.save_document("view-boundary.json")

    first = session.document
    session.new_document()
    assert session.view.document is session.document
    assert session.view.document is not first
    assert session.state_meta()["bodies"] == [], "新規文書なのに前の文書のBodyが配られている"

    session.open_document("view-boundary.json")
    assert session.view.document is session.document
    assert [body["body_id"] for body in session.state_meta()["bodies"]] != []


def test_the_view_does_not_prune_while_a_preview_is_running(tmp_path) -> None:
    """「プレビュー中か」は `Session` から引数で届く。届かないと投機的な生存集合で剪定する。

    `preview()` は `duocad.speculative` で文書を投機的に書き換えて必ず元へ戻す。その最中の
    生存集合でキャッシュを落とすと、戻した後に測り直しが走る(入れた目的そのものが消える)。
    ここは `view` の口を直に叩いて、旗の受け渡しだけを見る。
    """
    session = _box_session(tmp_path)
    created_id = _extrude_a_second_body(session)
    session.geometry(created_id)
    session.edges(created_id)
    session.vertices(created_id)
    session.body_meta(session.document.get_body(created_id))

    session.undo()
    assert created_id not in [body.id for body in session.document.bodies]

    session.view.state_meta(can_undo=False, applied_count=0, previewing=True)
    assert created_id in session.view._geometry_cache, "プレビュー中なのに剪定した"
    assert created_id in session.view._edge_cache
    assert created_id in session.view._vertex_cache
    assert created_id in session.view._volume_cache

    session.view.state_meta(can_undo=False, applied_count=0, previewing=False)
    assert created_id not in session.view._geometry_cache, "プレビューが明けても剪定しない"
    assert created_id not in session.view._edge_cache
    assert created_id not in session.view._vertex_cache
    assert created_id not in session.view._volume_cache


def test_state_meta_reports_the_sessions_can_undo_not_the_raw_log(tmp_path) -> None:
    """配る `can_undo` は `Session.can_undo`(土台の下限つき)であって、素のログではない。

    土台(部品の読み込み・編集用ボディ)の直後は `document.log.can_undo` が True でも
    `Session.undo()` は拒否する。`state_meta()` が素のログを配ると、**画面は取り消せると
    表示するのに押すと断られる**食い違いになる —— `Session.can_undo` の docstring が
    「MCPと同じ答えを返すための唯一の出どころ」と言っているのは、この経路のことでもある。

    `_setup_floor_seq` を直に置いているのは `Session.load_step()` がやることと同じ
    (STEPを読む代わり。この検査に部品ファイルは要らない)。
    """
    session = _box_session(tmp_path)
    _extrude_a_second_body(session)
    assert session.document.log.can_undo

    session._setup_floor_seq = session.document.log.entries[-1].seq
    assert session.document.log.can_undo, "素のログのほうは True のまま(でないと変異が見えない)"
    assert session.can_undo is False
    assert session.state_meta()["can_undo"] is False

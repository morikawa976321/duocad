"""Body索引キャッシュの剪定(20260827、CODE_SURVEY_ACTIONS 1.1)。

`_geometry_cache` / `_edge_cache` / `_vertex_cache` / `_volume_cache` は `body_id` 索きで、
文書からBodyが消えても(undoで)エントリが自然には落ちない。同じ文書を開いたまま
作成・undoを繰り返すと線形に溜まり続けるので、`Session.state_meta()`(WebSocket配信のたびに
呼ばれる)が生存集合との差分で剪定することを固定する。`sketches_state()` の剪定(同じ形)と
`tests/test_body_meta_volume_cache.py` の作り(`Session` を直に組んでキャッシュのdictを覗く)に揃える。

**キャッシュの持ち主は `session.view`(`duocad/view.py` の `DocumentView`)**。2026-08-28に
`Session` から切り出したので覗く先だけを移してある —— 呼ぶ口(`session.state_meta()` /
`session.geometry()` …)も見る中身も、切り出す前と同じである。

**`_revision_counter` は剪定しない**。剪定すると版が1へ戻り、同じ `body_id` を知っている
クライアントが「版が同じ=変わっていない」と判断して前の文書の形を描き続ける
(`DocumentView.reset` のコメントと同じ理由)。
"""
from __future__ import annotations

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad.document import Body
from duocad.region import region_ref, regions
from duocad.server import Session
from duocad.sketch import add_rectangle, sketch_create
from duocad.verbs import extrude

BOX_X, BOX_Y, BOX_Z = 20.0, 20.0, 20.0


def _box_session() -> Session:
    """`tests/test_body_meta_volume_cache.py` と同じ作り(base bodyを1つ直に足すだけ)。"""
    session = Session(None, 0.2)
    session.document.add_body(
        Body(
            id="box",
            name="box",
            kind="solid",
            shape=BRepPrimAPI_MakeBox(BOX_X, BOX_Y, BOX_Z).Shape(),
        )
    )
    return session


def _extrude_a_second_body(session: Session) -> str:
    """`mode="new"` でもう1つBodyを作る(undoすると `document.remove_body` で完全に消える経路。
    `duocad/verbs/extrude.py` の `_undo_extrude`)。作ったBodyのidを返す。
    """
    sketch_id = sketch_create(session.document, "XY")
    add_rectangle(session.document, sketch_id, (0.0, 0.0), 10.0, 10.0)
    boss = regions(session.document, sketch_id)
    operation = extrude(
        session.document, [region_ref(sketch_id, boss[0].index)], 5.0, mode="new"
    )
    return operation.args["body_id"]


def test_undo_prunes_the_gone_bodys_entry_from_all_four_caches_but_not_the_revision_counter():
    session = _box_session()
    created_id = _extrude_a_second_body(session)

    # 4つのキャッシュへ実際に載ることを先に確認する(載る前提が崩れたら以下は空振りする)。
    session.geometry(created_id)
    session.edges(created_id)
    session.vertices(created_id)
    session.body_meta(session.document.get_body(created_id))  # _volume_cache と _revision_counter を埋める
    assert created_id in session.view._geometry_cache
    assert created_id in session.view._edge_cache
    assert created_id in session.view._vertex_cache
    assert created_id in session.view._volume_cache
    assert created_id in session.view._revision_counter

    session.undo()
    assert created_id not in [body.id for body in session.document.bodies], (
        "テストの前提が崩れている: undoでBodyが消えていない"
    )

    # WebSocket配信のたびに呼ばれる state_meta() が剪定を走らせる。
    session.state_meta()

    assert created_id not in session.view._geometry_cache
    assert created_id not in session.view._edge_cache
    assert created_id not in session.view._vertex_cache
    assert created_id not in session.view._volume_cache
    # A2: _revision_counter は剪定しない(剪定しすぎの検知)。
    assert created_id in session.view._revision_counter


def test_pruning_does_not_touch_a_body_that_is_still_alive():
    """生きているBodyのキャッシュは剪定で消えない(過剰に落としていないことの確認)。"""
    session = _box_session()
    created_id = _extrude_a_second_body(session)

    session.geometry(created_id)
    session.edges(created_id)
    session.vertices(created_id)
    session.body_meta(session.document.get_body(created_id))

    session.state_meta()  # undoしていないので何も落ちないはず

    assert created_id in session.view._geometry_cache
    assert created_id in session.view._edge_cache
    assert created_id in session.view._vertex_cache
    assert created_id in session.view._volume_cache

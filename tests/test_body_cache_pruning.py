"""`Session.state_meta()` が、消えたBodyの `body_id` 索きキャッシュ(`session.view`)を生存集合で剪定すること。

落ちたら、作成・undoを繰り返すたびにキャッシュが溜まる。版 `_revision_counter` は剪定しない(理由は `DocumentView._prune_body_caches` の docstring)。
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
    `duocad/verbs/extrude.py` の `_undo_extrude`)。
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

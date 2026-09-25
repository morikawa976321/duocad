"""`Session.state_meta()` が、消えたBodyの `body_id` 索きキャッシュ(`session.view`)を生存集合で剪定すること。

落ちたら、作成・undoを繰り返すたびにキャッシュが溜まる。版 `_revision_counter` は剪定しない(理由は `DocumentView._prune_body_caches` の docstring)。
"""
from __future__ import annotations

from document_support import second_body
from session_support import session_with_box

BOX_X, BOX_Y, BOX_Z = 20.0, 20.0, 20.0


def test_undo_prunes_the_gone_bodys_entry_from_all_four_caches_but_not_the_revision_counter():
    session = session_with_box(BOX_X, BOX_Y, BOX_Z)
    created_id = second_body(session.document)

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
    session = session_with_box(BOX_X, BOX_Y, BOX_Z)
    created_id = second_body(session.document)

    session.geometry(created_id)
    session.edges(created_id)
    session.vertices(created_id)
    session.body_meta(session.document.get_body(created_id))

    session.state_meta()  # undoしていないので何も落ちないはず

    assert created_id in session.view._geometry_cache
    assert created_id in session.view._edge_cache
    assert created_id in session.view._vertex_cache
    assert created_id in session.view._volume_cache

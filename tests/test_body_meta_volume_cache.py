"""`Session.body_meta()` の体積キャッシュ(20260826-0124)。

`state_meta()` は接続のたびに全Bodyぶん `body_meta()` を呼ぶ(`Server._push`)。
`query.volume` は面ごとの数値積分で軽くない(PCケース27部品で693ms/回)ので、
形状が変わっていないBodyの体積を測り直さないことを固定する。

キャッシュの規律は `geometry()` と同じ —— 形状の**同一性**(`is`)で判定する(版ではない)。

**実体の持ち主は `session.view`(`duocad/view.py` の `DocumentView._body_volume` /
`_volume_cache`)**。2026-08-28に `Session` から切り出したが、ここが叩く口
(`session.state_meta()`)は委譲として残っているので、この検査は切り出す前と同じものを見ている。
"""
from __future__ import annotations

import math

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, query
from duocad.document import Body
from duocad.server import Session
from duocad.verbs import extrude_face

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


def test_state_meta_volume_updates_when_the_shape_changes():
    """形状が変われば体積が更新される(面を押し出した後の state_meta が新しい値を返す)。"""
    session = _box_session()
    body = session.document.active_body()
    assert body is not None

    before = session.state_meta()["bodies"][0]
    assert math.isclose(before["volume"], BOX_X * BOX_Y * BOX_Z, rel_tol=1e-9)

    top = next(
        face
        for face in kernel.tessellate(body.shape)["faces"]
        if face["normal"][2] > 0.999 and face["centroid"][2] > BOX_Z - 0.1
    )
    extrude_face(session.document, top["index"], 10.0)

    after = session.state_meta()["bodies"][0]
    assert math.isclose(after["volume"], BOX_X * BOX_Y * (BOX_Z + 10.0), rel_tol=1e-9)
    assert after["volume"] != before["volume"]


def test_state_meta_does_not_recompute_volume_for_an_unchanged_shape(monkeypatch):
    """2回目以降の `state_meta()` では、形状が変わっていないBodyの `query.volume` が
    呼ばれない(時間ではなく呼び出し回数で見るので、マシン負荷に振れない)。
    """
    session = _box_session()
    session.state_meta()  # 初回配信でキャッシュを埋める

    calls: list = []
    original_volume = query.volume

    def counting(shape):
        calls.append(shape)
        return original_volume(shape)

    monkeypatch.setattr(query, "volume", counting)

    session.state_meta()

    assert calls == [], "形状が変わっていないのに query.volume が呼ばれた"

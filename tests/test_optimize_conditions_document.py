"""形状最適化の条件が入った文書の契約(凍結中も、条件入りの文書は開いて使え、配信のたびに測り直さない)。

条件を置く verb そのものの契約は `tests/topopt/test_optimize_setup.py`(既定の実行では回さない)。
"""
from __future__ import annotations

from duocad import optimize
from duocad.server import Session
from session_support import SIDE, pad, select as _select

DEVIATION = 0.2


def _box(session: Session, corner=(0.0, 0.0)):
    """一辺 `SIDE` の立方体を新しいボディとして1つ作る。"""
    return pad(session, SIDE, SIDE, SIDE, corner)


def _body_refs(*bodies) -> list:
    return [{"body": body.id, "kind": "body"} for body in bodies]


def _preserve(session: Session, *bodies) -> None:
    """条件面を置く前に、そのボディを保持ソリッドにする。"""
    kept = list(session.document.optimize.preserve_bodies)
    refs = [{"body": body_id, "kind": "body"} for body_id in kept]
    refs += [ref for ref in _body_refs(*bodies) if ref["body"] not in kept]
    _select(session, refs)
    session.set_preserve_bodies()
    session.clear_selection()


def _face_refs(body, indices) -> list:
    return [{"body": body.id, "kind": "face", "index": index} for index in indices]


def _optimize(session: Session) -> dict:
    return session.state_meta()["optimize"]


def test_the_setup_survives_a_real_save_and_open(tmp_path) -> None:
    """落ちたら、`save_document` → `new_document` → `open_document` の実APIの往復(ファイルI/Oと
    文書の差し替えを通る)で条件が戻らないか、差し替え後に前の文書の割り当てを配っている。"""
    made = Session(DEVIATION, models_dir=tmp_path / "models")
    body = _box(made)
    _select(made, _body_refs(body))
    made.set_preserve_bodies()
    _select(made, _face_refs(body, [1, 2]))
    made.set_fixtures()
    _select(made, _face_refs(body, [3]))
    made.set_loads(120.0, [1.0, 0.0, 0.0])
    before = _optimize(made)
    assert len(before["fixtures"]) == 2 and before["load_specs"][0]["magnitude"] == 120.0

    made.save_document("optimized.json")
    made.new_document()
    # **差し替えでキャッシュごと落ちていること**(ここが緑でないと、次の open が
    # 前の文書の割り当てを配っていても気づけない)
    empty = _optimize(made)
    assert empty["preserve_bodies"] == [] and empty["fixtures"] == [] and empty["load_specs"] == []

    made.open_document("optimized.json")
    after = _optimize(made)
    assert after == before, {"開く前": before, "開いた後": after}


def test_clearing_every_role_survives_a_real_save_and_open(tmp_path) -> None:
    """**外す操作も操作ログの1手**なので、保存・再オープンで「空のまま」戻る。

    ここが落ちるなら、外した役割が開き直すたびに復活していることになる。
    ボディ・拘束は集合代入なので「空を代入した」が外し方、荷重だけは上書き代入なので
    `clear_loads` が外し方である(`duocad/verbs/optimize_setup.py`)。
    """
    made = Session(DEVIATION, models_dir=tmp_path / "models")
    body = _box(made)
    _select(made, _body_refs(body))
    made.set_preserve_bodies()
    _select(made, _face_refs(made.document.bodies[-1], [1]))
    made.set_fixtures()
    _select(made, _face_refs(body, [2]))
    made.set_loads(30.0, "normal")

    # 4つとも空にする(ボディ・拘束は空の選択で確定、荷重は `clear_loads`)。
    # **条件面を先に外す** —— 条件面は保持ソリッドの面にしか置けないので、条件が残ったまま
    # 保持を空にはできない(2026-09-18〜)。
    made.clear_selection()
    made.set_fixtures()
    made.clear_loads()
    made.set_preserve_bodies()
    made.set_exclude_bodies()
    assert _optimize(made)["fixtures"] == [] and _optimize(made)["load_specs"] == []

    made.save_document("cleared.json")
    made.new_document()
    made.open_document("cleared.json")

    after = _optimize(made)
    assert after["preserve_bodies"] == [] and after["exclude_bodies"] == []
    assert after["fixtures"] == [] and after["loads"] == [] and after["stale"] == []


def test_the_state_is_not_rebuilt_on_every_broadcast(session: Session) -> None:
    """**配信のたびに全ボディを走査しない**。"""
    body = _box(session)
    other = _box(session, corner=(50.0, 0.0))
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_fixtures()

    session.state_meta()  # 1回目は当然作る(条件を置いた直後なので)。数えるのはその後
    calls: list = []
    original = optimize.face_fingerprint

    def counted(doc, body_id, index):
        calls.append((body_id, index))
        return original(doc, body_id, index)

    optimize.face_fingerprint = counted
    try:
        session.state_meta()
        session.state_meta()
        assert calls == [], f"条件が変わっていないのに測り直している: {calls}"

        # **条件と関係の無いボディ**を変えても測り直さない
        _select(session, _face_refs(other, [1]))
        session.extrude(3.0, mode="join")
        session.state_meta()
        assert calls == [], f"無関係なボディの変更で測り直している: {calls}"

        # 条件が指しているボディの形が変われば、そこだけ測り直す
        _select(session, _face_refs(body, [2]))
        session.extrude(3.0, mode="join")
        session.state_meta()
        assert [item[0] for item in calls] == [body.id], calls
    finally:
        optimize.face_fingerprint = original


def test_the_log_broadcast_does_not_carry_every_fingerprint(session: Session) -> None:
    """状態配信は面の指紋の本文を配らない(**保存(`to_json`)は書く**)。"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1, 2, 3]))
    session.set_fixtures()

    delivered = session.state_meta()["log"][-1]["args"]
    assert "配信しない" in delivered["faces"], delivered
    assert len(session.document.log.entries[-1].args["faces"]) == 3

"""B契約(サーバ側): プレビュー(投機的実行)と、ブラウザから叩く選択の口。

タスクBの本体はUI(コマンド層の骨格)であり、**UIは `pytest` では完了判定できない**
(実機確認は `web/scripts/smoke.mjs`)。しかしBがコアとサーバに足した2つ——

1. **プレビュー** = verbを投機的に実行して形だけ測り、文書を元へ戻す
   (`duocad.speculative` + `Session.preview`)
2. **選択の口をWebSocketにも開ける** (`modify_selection` / `commit_selection` /
   `clear_selection`)

——は機械で判定できる。**ここが緑でないとUIの検証は意味を持たない**
(「Escで文書が変わらない」の実体は、プレビューが1行も残さないことだから)。

固定する契約:

- プレビューは**形を返す**(変わったBodyだけ・`geometry` は `state` と同じ形)
- プレビューは**何も残さない**(操作ログ・形状・体積・選択・`applied_count`)
- プレビューは**何も発火しない**(購読者が呼ばれない=他のクライアントへ配信されない)
- 失敗したプレビューも**何も残さない**
- プレビューできるverbは登録制(未知のverb・未知の引数は ValueError)
- 選択の3コマンドがWebSocketから踏め、`state.selection` に出る
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

# 押し出せる面を探すヘルパは可視性のテストと共有する(面の添字の決め打ちを避けるため)
from test_visibility import first_extrudable_face

from duocad import query
from duocad.server import PREVIEWABLE_VERBS, Server, Session

MODELS = Path(r"C:\Users\tm976\mf\3d-models")
SHELF = MODELS / "shelf_attachment_GD.step"

DEVIATION = 0.2
PREVIEW_MM = 3.0


@pytest.fixture()
def session() -> Session:
    """`shelf_attachment_GD.step` を読んだセッション(テストごとに作り直す)。"""
    if not SHELF.exists():
        pytest.skip(f"参照STEPが無い: {SHELF}")
    return Session(SHELF, DEVIATION)


def _editable(session: Session):
    """押し出せる(編集用の)Bodyと、その中で押し出せる面の添字。"""
    body = next(b for b in session.document.bodies if b.kind == "solid" and b.visible)
    return body, first_extrudable_face(body.shape)


def _fingerprint(session: Session) -> dict:
    """文書の「変わっていないこと」を1つの値にまとめる(比較を1箇所に集める)。"""
    return {
        "log": [(entry.seq, entry.verb, json.dumps(entry.args, sort_keys=True))
                for entry in session.document.log.entries],
        "can_redo": session.document.log.can_redo,
        "applied_count": session._applied_count,
        "bodies": [
            (body.id, body.name, body.kind, body.visible, id(body.shape),
             None if body.shape is None else round(query.volume(body.shape), 9))
            for body in session.document.bodies
        ],
        "active": session.document.active_body().id if session.document.active_body() else None,
        "selection": session.get_selection(),
        "sketches": len(session.document.sketches),
        "parameters": dict(session.document.parameters),
    }


# --------------------------------------------------------------------------------------
# プレビューが返すもの
# --------------------------------------------------------------------------------------
def test_preview_returns_the_shape_the_verb_would_produce(session: Session) -> None:
    """投機的に実行した結果の形が返る。**確定した本物と同じ体積**であること。"""
    body, face_index = _editable(session)
    volume_before = query.volume(body.shape)

    result = session.preview(
        "extrude_face",
        {"body_id": body.id, "face_index": face_index, "distance": PREVIEW_MM},
    )

    assert result["verb"] == "extrude_face"
    assert len(result["bodies"]) == 1, "押し出しで形が変わるのは1つのBodyだけ"
    previewed = result["bodies"][0]
    assert previewed["body_id"] == body.id
    assert previewed["volume"] != pytest.approx(volume_before), "プレビューの体積が押し出し前と同じ"
    # `geometry` は state/delta と同じ形(アプリ層が同じ道具で描けること)
    assert set(previewed["geometry"]) >= {"deviation", "face_count", "bbox", "faces"}
    assert previewed["geometry"]["faces"], "三角形が1つも無いプレビューは描けない"

    # 本物を実行すると、プレビューが言っていた体積になる
    session.extrude(face_index, PREVIEW_MM, body_id=body.id)
    assert query.volume(body.shape) == pytest.approx(previewed["volume"])


def test_preview_leaves_nothing_behind(session: Session) -> None:
    """**中断しても文書が一切変わらない**の実体。プレビューは1行も残さない。"""
    body, face_index = _editable(session)
    before = _fingerprint(session)

    session.preview(
        "extrude_face",
        {"body_id": body.id, "face_index": face_index, "distance": PREVIEW_MM},
    )

    assert _fingerprint(session) == before


def test_repeated_previews_leave_nothing_behind(session: Session) -> None:
    """数値を打ちながら何度もプレビューしても積み上がらない(実際の使われ方)。"""
    body, face_index = _editable(session)
    before = _fingerprint(session)

    for distance in (1.0, 2.0, 3.0, -1.0, 10.0):
        session.preview(
            "extrude_face",
            {"body_id": body.id, "face_index": face_index, "distance": distance},
        )

    assert _fingerprint(session) == before


def test_preview_does_not_emit_events(session: Session) -> None:
    """プレビューは購読者を呼ばない(=他のクライアントへ配信されない)。

    ここが漏れると、確定していない形が全接続へ配信され、直後の巻き戻しで消える
    (画面のちらつきだけでなく、MCPが読んだ状態が実在しない中間状態になる)。
    """
    body, face_index = _editable(session)
    seen: list = []
    session.add_listener(seen.append)

    session.preview(
        "extrude_face",
        {"body_id": body.id, "face_index": face_index, "distance": PREVIEW_MM},
    )
    assert seen == [], "プレビューで変更イベントが発火した"

    # 購読そのものは生きている(黙って外れていないこと)
    session.extrude(face_index, PREVIEW_MM, body_id=body.id)
    assert len(seen) == 1


def test_a_failed_preview_leaves_nothing_behind(session: Session) -> None:
    """失敗したプレビューも文書を変えない(半端な状態を残さない)。"""
    body, face_index = _editable(session)
    before = _fingerprint(session)

    with pytest.raises(Exception):
        session.preview(
            "extrude_face",
            {"body_id": body.id, "face_index": face_index, "distance": 0.0},
        )

    assert _fingerprint(session) == before


def test_preview_refuses_unknown_verbs_and_arguments(session: Session) -> None:
    """プレビューできるverbは登録制。ソケットから任意のメソッドを呼べる作りにしない。"""
    body, face_index = _editable(session)

    with pytest.raises(ValueError):
        session.preview("set_visible", {"body_id": body.id, "visible": False})
    with pytest.raises(ValueError):
        session.preview("os.system", {})
    with pytest.raises(ValueError):
        session.preview(
            "extrude_face",
            {"body_id": body.id, "face_index": face_index, "distance": 1.0, "client_op": 7},
        )

    # Bの範囲は extrude_face 1つ(C・Dが足すときはここも足す)
    assert set(PREVIEWABLE_VERBS) == {"extrude_face"}


def test_preview_does_not_poison_the_geometry_cache(session: Session) -> None:
    """プレビューの形がキャッシュに残らない(次の `state` が投機的な形を出さない)。"""
    body, face_index = _editable(session)
    real = session.geometry(body.id)

    session.preview(
        "extrude_face",
        {"body_id": body.id, "face_index": face_index, "distance": PREVIEW_MM},
    )

    assert session.geometry(body.id) is real, "プレビューがテセレーションのキャッシュを差し替えた"


# --------------------------------------------------------------------------------------
# WebSocket の口
# --------------------------------------------------------------------------------------
class FakeConnection:
    """`Server` が使うのは `send` だけ(配信の出口)。"""

    def __init__(self) -> None:
        self.payloads: list = []

    async def send(self, raw: str) -> None:
        self.payloads.append(json.loads(raw))

    def last(self, kind: str) -> dict:
        for payload in reversed(self.payloads):
            if payload.get("type") == kind:
                return payload
        raise AssertionError(f"{kind!r} の応答が無い: {[p.get('type') for p in self.payloads]}")


def _run(session: Session, commands: list) -> tuple:
    """コマンドを順に `_dispatch` へ流し、2本の接続が受け取ったものを返す。

    **2本つなぐ**(1本だけだと「2本目に届かない」実装でも緑になる。test_mcp.py と同じ判断)。
    """

    async def scenario() -> tuple:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        clients = [FakeConnection(), FakeConnection()]
        for client in clients:
            server.clients.add(client)
            await server._push(client, applied=None, only=client)
        for command in commands:
            await server._dispatch(clients[0], json.dumps(command))
        return clients

    return asyncio.run(scenario())


def test_the_browser_can_edit_the_shared_selection(session: Session) -> None:
    """人が直す経路(`modify_selection`)がWebSocketから踏め、全接続の `state` に出る。"""
    body, face_index = _editable(session)
    ref = {"body": body.id, "kind": "face", "index": face_index}
    log_before = len(session.document.log.entries)

    origin, other = _run(session, [
        {"cmd": "modify_selection", "add": [ref], "client_op": 1},
    ])

    assert session.get_selection()["count"] == 1
    for client in (origin, other):
        state = client.last("state")["state"]
        assert state["selection"]["count"] == 1
        assert state["selection"]["origin"] == "user"
        assert state["selection"]["items"][0]["index"] == face_index
    # 操作を出した側にだけ確認応答が付く(選択はverbではないので `change` で区別する)
    assert origin.last("state")["applied"]["change"] == "selection"
    assert origin.last("state")["applied"]["client_op"] == 1
    assert "applied" not in other.last("state")
    # 選択は文書を変えないので操作ログに載らない
    assert len(session.document.log.entries) == log_before
    assert all(entry.verb != "modify_selection" for entry in session.document.log.entries)


def test_the_browser_can_approve_and_clear_the_selection(session: Session) -> None:
    """AIの提案をUIから承認でき(`commit_selection`)、空にもできる(`clear_selection`)。

    **これが交互操作の人間側の1手**である。承認できないと、AIが提案した集合は
    `SelectionNotCommitted` のまま verb に渡せない(`duocad/selection.py`)。
    """
    body, face_index = _editable(session)
    session.propose_selection(refs=[{"body": body.id, "kind": "face", "index": face_index}])
    assert session.get_selection()["origin"] == "proposed"

    origin, other = _run(session, [{"cmd": "commit_selection", "client_op": 2}])
    assert session.get_selection()["origin"] == "user"
    assert other.last("state")["state"]["selection"]["origin"] == "user"

    origin, other = _run(session, [{"cmd": "clear_selection", "client_op": 3}])
    assert session.get_selection()["count"] == 0
    assert other.last("state")["state"]["selection"]["items"] == []


def test_preview_over_the_socket_answers_only_the_asking_client(session: Session) -> None:
    """`preview` は要求した接続にだけ返り、**状態を配信しない**。"""
    body, face_index = _editable(session)

    origin, other = _run(session, [
        {
            "cmd": "preview",
            "verb": "extrude_face",
            "preview_id": 42,
            "args": {"body_id": body.id, "face_index": face_index, "distance": PREVIEW_MM},
        },
    ])

    preview = origin.last("preview")
    assert preview["preview_id"] == 42
    assert preview["verb"] == "extrude_face"
    assert preview["bodies"][0]["body_id"] == body.id
    assert preview["bodies"][0]["geometry"]["faces"]
    # 他の接続には何も起きていない(接続直後の1枚だけ)
    assert [p.get("type") for p in other.payloads] == ["state"]
    # 文書も動いていない
    assert session._applied_count == 0
    assert session.get_selection()["count"] == 0


def test_a_broken_preview_is_reported_and_does_not_kill_the_connection(session: Session) -> None:
    """プレビューが失敗したら理由を画面へ返す(サーバは落ちない・文書は変わらない)。"""
    body, _ = _editable(session)
    before = _fingerprint(session)

    origin, _other = _run(session, [
        {
            "cmd": "preview",
            "verb": "extrude_face",
            "preview_id": 7,
            "args": {"body_id": body.id, "face_index": 10_000, "distance": PREVIEW_MM},
        },
    ])

    error = origin.last("error")
    assert error["where"] == "preview"
    assert error["message"]
    assert _fingerprint(session) == before

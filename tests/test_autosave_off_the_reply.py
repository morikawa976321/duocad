"""自動保存を操作の応答から外す(issue #173): 大きな操作の JSON を覚える `OpLog`、書き込みを回す書き手スレッド。

ファイルは今の文書の鏡のまま(1操作ごと・中身は配る前に確定)で、書き込みだけが応答の後ろへ回ることを見る。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import threading
from dataclasses import asdict

import pytest

from duocad.oplog import OpLog
from duocad.server import Session
from session_support import browser as _browser


def _reference(log: OpLog, created) -> str:
    """覚える仕組みの無い素朴な書き方(`to_json` が変える前の実装そのもの)。"""
    payload: dict = {"version": log.FORMAT_VERSION}
    if created is not None:
        payload["created"] = created
    if log._numbering:
        payload["numbering"] = dict(log._numbering)
    payload["operations"] = [asdict(operation) for operation in log.entries]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _log_with_a_big_operation() -> OpLog:
    log = OpLog()
    log.append("sketch_create", {"base_plane": "XY", "name": "スケッチ"})
    log.append("import_step", {"step": "ISO-10303-21;\n" + "x" * (OpLog.SERIALIZED_CACHE_MIN * 2), "names": ["部品"]})
    log.append("add_circle", {"sketch_id": "s1", "center": [0.0, 1.5], "diameter": 4.0})
    log.allocate_number("body")
    return log


@pytest.mark.parametrize("created", [None, 1792089000.25])
def test_the_saved_bytes_do_not_change(created, tmp_path) -> None:
    """落ちたら、保存の速さのために組み方を変えたせいでファイルの中身が変わった(差分が全行に出る・読めない)。"""
    for log in (OpLog(), _log_with_a_big_operation()):
        for _ in range(2):  # 2回目は覚えた JSON から組む
            assert log.to_json(created) == _reference(log, created)
            written = tmp_path / "written.json"
            written.write_text(_reference(log, created), encoding="utf-8")
            assert log.to_file_bytes(created) == written.read_bytes()


def test_a_remembered_operation_follows_its_provenance_and_undo() -> None:
    """落ちたら、覚えた JSON が古いまま使われ、再生で差し替わった由来や undo 後の操作がファイルに写らない。"""
    log = _log_with_a_big_operation()
    log.to_json()
    big = log.entries[1]
    big.provenance = {"faces": [1, 2, 3]}
    assert log.to_json() == _reference(log, None)
    log._cursor -= 1  # undo の後(`OpLog.undo` はカーソルを戻すだけ)
    assert log.to_json() == _reference(log, None)
    log._cursor = 1
    log.append("add_line", {"sketch_id": "s1"})  # redo の山が捨てられる
    assert log.to_json() == _reference(log, None)
    assert all(entry[0] in log._entries for entry in log._serialized.values())


def _ops_on_disk(session: Session) -> int:
    return len(json.loads((session.models_dir / session.current_name).read_text(encoding="utf-8"))["operations"])


def test_the_file_is_written_in_the_background_and_settles(session: Session) -> None:
    """落ちたら、書き手スレッドへ回した書き込みが着かない・別の文書の中身を書く・`saved` が戻らない。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]  # 無題の最初の保存はその場で書く(名前が配りものに載る)
    assert session.pending_write is None and _ops_on_disk(session) == 1
    with session.background_autosave():
        session.add_circle(sketch_id, [0.0, 0.0], 10.0)
        session.add_circle(sketch_id, [9.0, 0.0], 4.0)
    future = session.pending_write
    assert future is not None
    concurrent.futures.wait([future])
    assert session.settle_autosave() is True
    assert _ops_on_disk(session) == 3 and session.state_meta()["saved"] is True


def test_a_failed_background_write_marks_the_document_unsaved(session: Session, monkeypatch) -> None:
    """落ちたら、書き手スレッドで書けなかったのに「保存済み」のまま(画面が知らずに閉じられる)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("duocad.session.os.replace", broken)
    with session.background_autosave():
        applied = session.add_circle(sketch_id, [0.0, 0.0], 10.0)
    assert applied["verb"] == "add_circle"
    concurrent.futures.wait([session.pending_write])
    assert session.settle_autosave() is False
    assert session.state_meta()["saved"] is False
    assert any("自動保存できなかった" in line for line in session.warnings)


@pytest.mark.parametrize("act", ["save", "delete", "open_again", "copy"])
def test_file_operations_wait_for_the_queued_write(session: Session, monkeypatch, act) -> None:
    """落ちたら、遅れて着いた古い書き込みが新しい保存を上書きする・消したファイルを書き戻す・最後の変更の無いファイルを読む/写す。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    name = session.current_name
    gate = concurrent.futures.Future()
    real = Session._write_document_file

    def slow(target, payload):
        gate.result(timeout=10)  # 書き手スレッドを止めておき、ファイルの口が待つかを見る
        real(target, payload)

    monkeypatch.setattr(Session, "_write_document_file", staticmethod(slow))
    with session.background_autosave():
        session.add_circle(sketch_id, [0.0, 0.0], 10.0)
    queued = session.pending_write
    assert queued is not None and not queued.done()
    # 書き手を止めたままファイルの口へ入る。待たない口は先に済ませてしまい、後から着いた書き込みが結果を壊す。
    threading.Timer(0.3, gate.set_result, [None]).start()
    if act == "save":
        session.add_circle(sketch_id, [5.0, 5.0], 2.0)  # 画面の外(MCP)の操作はその場で書く
    elif act == "delete":
        session.delete_document(name)
    elif act == "open_again":
        session.open_document(name)
    else:
        session.copy_document(name, "copy.json")
    concurrent.futures.wait([queued])
    if act == "save":
        assert json.loads((session.models_dir / name).read_text(encoding="utf-8"))["operations"].__len__() == 3
    elif act == "delete":
        assert not (session.models_dir / name).exists()
    elif act == "open_again":
        assert len(session.document.log.entries) == 2
    else:
        assert len(json.loads((session.models_dir / "copy.json").read_text(encoding="utf-8"))["operations"]) == 2


def test_a_browser_verb_replies_before_the_write_and_the_file_follows(session: Session, monkeypatch) -> None:
    """落ちたら、画面の操作の応答(状態配信)が書き込みを待っている、または書き終える前に次の要求を捌く。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    order: list = []
    real = Session._write_document_file

    def watched(target, payload):
        order.append("write")
        real(target, payload)

    monkeypatch.setattr(Session, "_write_document_file", staticmethod(watched))

    async def script(send):
        reply = await send({"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "diameter": 6.0, "client_op": 7})
        order.append(f"after:{_ops_on_disk(session)}")
        return reply

    (reply, _clients) = _browser(session, script, connections=1)
    assert reply["type"] == "state" and reply["applied"]["verb"] == "add_circle"
    assert order == ["write", "after:2"], "要求を捌き終えた時点でファイルが今の文書の鏡になっていない"
    assert session.pending_write is None


def test_a_browser_verb_whose_write_fails_is_pushed_again_as_unsaved(session: Session, monkeypatch) -> None:
    """落ちたら、書き手スレッドで書けなかったことが画面へ届かない(保存済みの表示のまま)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("duocad.session.os.replace", broken)

    async def script(send):
        await send({"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "diameter": 6.0, "client_op": 8})
        return None

    (_reply, clients) = _browser(session, script, connections=1)
    states = clients[0].of_type("state")
    assert states[0]["state"]["saved"] is True, "応答は書き込みを待たずに出る"
    assert states[-1]["state"]["saved"] is False
    assert any("自動保存できなかった" in item["message"] for item in clients[0].of_type("warning"))


def test_a_browser_verb_whose_write_recovers_is_pushed_again_as_saved(session: Session, monkeypatch) -> None:
    """落ちたら、前に書けなかった文書が書けても、画面は次の配信まで「未保存」のまま(配ったのは書く前の状態)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    real = os.replace
    failures = [OSError("locked by another program")]

    def flaky(*args, **kwargs):
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)

    monkeypatch.setattr("duocad.session.os.replace", flaky)

    async def script(send):
        await send({"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "diameter": 6.0, "client_op": 9})
        await send({"cmd": "add_circle", "sketch_id": sketch_id, "center": [9.0, 0.0], "diameter": 2.0, "client_op": 10})
        return None

    (_reply, clients) = _browser(session, script, connections=1)
    assert clients[0].of_type("state")[-1]["state"]["saved"] is True
    assert _ops_on_disk(session) == 3


def test_the_bytes_written_match_what_write_text_wrote(tmp_path) -> None:
    """落ちたら、書き方をバイト列に変えたせいで改行(`os.linesep`)や文字コードが変わり、既存のファイルと差分が出る。"""
    session = Session(0.2, models_dir=tmp_path / "models")
    session.sketch_create("XY")
    on_disk = (session.models_dir / session.current_name).read_bytes()
    assert on_disk.count(os.linesep.encode()) == session.document.log.to_json(session._created).count("\n")

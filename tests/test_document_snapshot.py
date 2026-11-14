"""再生結果のスナップショット(`duocad/document_snapshot.py`、issue #309)。

落ちたら: 文書を開いたときの形・スケッチ・undo が、ログを再生して開いたときと違う(スナップショットが何かを落としている)か、
使ってはいけないスナップショット(ファイルが変わった・コードが違う・壊れている)を使っている。
"""
from __future__ import annotations

import io
import pickle
from pathlib import Path

import pytest

from duocad import document_snapshot, kernel, query
from duocad.oplog import OpLog
from duocad.server import Session
from session_support import box, cylinder, pad, select

from duocad import brep_topology as _brep_topology


@pytest.fixture(autouse=True)
def _every_document_is_worth_a_snapshot(monkeypatch) -> None:
    """小さなテスト用の文書でも作る(本番は再生に 0.5 秒以上かかる文書だけ)。"""
    monkeypatch.setattr(document_snapshot, "MIN_BUILD_SECONDS", 0.0)


def _edge(session: Session, body_id: str, *, start: tuple, end: tuple) -> dict:
    for edge in query.describe_edges(session.document.get_body(body_id).shape):
        points = (tuple(edge["start"]), tuple(edge["end"]))
        if all(any(pytest.approx(want, abs=1e-6) == got for got in points) for want in (start, end)):
            return {"body": body_id, "kind": "edge", "index": edge["index"]}
    raise AssertionError(f"辺が無い: {start} - {end}")


def _shapes(session: Session) -> list:
    """ボディごとの形の写し(体積・面数・面の種別ごとの枚数・外接箱)。"""
    state = []
    for body in session.document.bodies:
        described = query.describe_shape(body.shape)
        bounds = kernel.bounding_box(body.shape, cache=None)
        state.append((
            body.id, body.name, body.visible,
            round(query.volume(body.shape), 6),
            _brep_topology.face_count(body.shape),
            tuple(sorted((kind, count) for kind, count in described["faces_by_type"].items() if count)),
            tuple(round(v, 6) for v in bounds["min"]), tuple(round(v, 6) for v in bounds["max"]),
        ))
    return state


def _build_document(session: Session, name: str) -> None:
    """押し出し・ブーリアン・面取り・フィレット・穴・別ボディの円柱を含む文書を作って保存する。"""
    target, _ = box(session)
    tool = pad(session, 10.0, 10.0, 30.0, corner=(15.0, 5.0))
    select(session, [{"body": tool.id, "kind": "body", "index": None}])
    session.boolean_body({"body": target.id, "kind": "body", "index": None}, "union")
    select(session, [_edge(session, target.id, start=(0.0, 0.0, 0.0), end=(0.0, 0.0, 20.0))])
    session.chamfer(2.0)
    select(session, [_edge(session, target.id, start=(15.0, 15.0, 30.0), end=(25.0, 15.0, 30.0))])
    session.fillet(1.5)
    cylinder(session, 4.0, 20.0, center=(10.0, 10.0), mode="cut")
    cylinder(session, 6.0, 10.0, center=(100.0, 0.0), mode="new")
    session.clear_selection()
    session.save_document(name)


def _open(models_dir: Path, name: str) -> Session:
    fresh = Session(0.2, models_dir=models_dir)
    fresh.open_document(name)
    return fresh


def _snapshot_file(models_dir: Path, name: str) -> Path:
    return models_dir / ".snapshots" / f"{name}.snap"


@pytest.fixture()
def no_replay(monkeypatch):
    """スナップショットから開けているなら、ログの再生は呼ばれない。"""

    def refuse(self, document) -> None:
        raise AssertionError("ログを再生した(スナップショットから開けなかった)")

    monkeypatch.setattr(OpLog, "replay", refuse)


def test_a_document_opened_by_replay_leaves_a_snapshot_the_next_open_reads_without_replaying(
    session: Session, monkeypatch
) -> None:
    """最初の再生の開きがスナップショットを残し、次は再生せずに同じ形・同じスケッチ・同じ配りものになる。"""
    _build_document(session, "chain.json")
    models = session.models_dir
    # 保存した時点でも書いている。再生の開きが作るところを見たいので消す。
    _snapshot_file(models, "chain.json").unlink()
    replayed = _open(models, "chain.json")
    assert _snapshot_file(models, "chain.json").exists()

    with monkeypatch.context() as patch:
        patch.setattr(OpLog, "replay", lambda self, document: pytest.fail("再生した"))
        restored = _open(models, "chain.json")

    assert _shapes(restored) == _shapes(replayed)
    assert [vars(sketch) for sketch in restored.document.sketches] == [vars(sketch) for sketch in replayed.document.sketches]
    assert dict(restored.document.parameters) == dict(replayed.document.parameters)
    assert vars(restored.document.optimize) == vars(replayed.document.optimize)
    assert restored.document.log.numbering() == replayed.document.log.numbering()
    assert restored.state_meta() == replayed.state_meta()
    assert not restored.store.snapshot_failures


def test_undo_and_redo_after_opening_from_a_snapshot_match_a_replayed_document(session: Session) -> None:
    """スナップショットから開いた文書を最後まで undo・redo して、各段の形が再生で開いた文書と一致する(undo 用の履歴は遅れて作る)。"""
    _build_document(session, "chain.json")
    models = session.models_dir
    _snapshot_file(models, "chain.json").unlink()  # 保存が書いた分。比べる側は再生で開く
    replayed = _open(models, "chain.json")
    from_snapshot = _open(models, "chain.json")
    # 比べる対象を、確かにスナップショットから開いたものにする。
    from duocad.verbs._common import HYDRATORS

    assert from_snapshot.document in HYDRATORS
    assert replayed.document not in HYDRATORS

    steps = 0
    while replayed.can_undo:
        replayed.undo()
        from_snapshot.undo()
        steps += 1
        assert _shapes(from_snapshot) == _shapes(replayed), f"undo {steps} 回目"
    assert steps == len(session.document.log.entries)
    assert from_snapshot.document not in HYDRATORS, "履歴を作った後は口が外れる"
    while replayed.document.log.can_redo:
        replayed.redo()
        from_snapshot.redo()
        assert _shapes(from_snapshot) == _shapes(replayed)


def test_an_edit_after_opening_from_a_snapshot_is_saved_with_a_snapshot_and_can_be_undone(session: Session) -> None:
    """開いた後の編集も自動保存と一緒に写しが預けられ、開き直した文書で編集前までの履歴を undo できる。"""
    _build_document(session, "chain.json")
    models = session.models_dir
    opened = _open(models, "chain.json")
    post = opened.document.bodies[-1]
    top = next(
        edge for edge in query.describe_edges(post.shape)
        if edge["kind"] == "circle" and pytest.approx(edge["start"][2], abs=1e-6) == 10.0
    )
    select(opened, [{"body": post.id, "kind": "edge", "index": top["index"]}])
    before_edit = _shapes(opened)
    opened.fillet(0.5)
    opened.clear_selection()
    after_edit = _shapes(opened)
    assert after_edit != before_edit

    reopened = _open(models, "chain.json")
    assert _shapes(reopened) == after_edit
    reopened.undo()
    assert _shapes(reopened) == before_edit
    reopened.undo()
    assert len(reopened.document.log.entries) == len(opened.document.log.entries) - 2


def test_a_snapshot_is_not_used_after_the_file_changes(session: Session, monkeypatch) -> None:
    """スナップショットを作った後にファイルが変わっていれば使わず(古い形を出さない)、捨てて再生で開く。"""
    _build_document(session, "chain.json")
    models = session.models_dir
    path = models / "chain.json"
    assert _snapshot_file(models, "chain.json").exists()
    other = _open(models, "chain.json")
    # 別の主体がファイルだけを書き換えた(スナップショットは前のまま)。
    other.store.write_document_file(path, other.document.log.to_file_chunks(created=12345.0))

    replayed = []
    original = OpLog.replay

    def spy(self, document) -> None:
        replayed.append(True)
        original(self, document)

    monkeypatch.setattr(OpLog, "replay", spy)
    reopened = _open(models, "chain.json")

    assert replayed, "再生で開いた"
    assert _shapes(reopened) == _shapes(other)
    # 再生で開いた後は、新しいファイルに合わせたスナップショットに置き換わっている。
    assert _snapshot_file(models, "chain.json").exists()


def test_a_corrupt_or_foreign_snapshot_falls_back_to_replay(session: Session) -> None:
    """壊れたスナップショットは捨てて再生で開く(文書は必ず開ける)。"""
    _build_document(session, "chain.json")
    models = session.models_dir
    expected = _shapes(_open(models, "chain.json"))
    for garbage in (b"", b"not a snapshot", b"DUOCADSNAP\n" + b"\x01" * 40):
        _snapshot_file(models, "chain.json").write_bytes(garbage)
        reopened = _open(models, "chain.json")
        assert _shapes(reopened) == expected
        assert _snapshot_file(models, "chain.json").exists(), "再生で開いた後に作り直される"


def test_a_snapshot_made_by_other_code_is_not_used(session: Session, monkeypatch) -> None:
    """コード(や OCP)の版が違うスナップショットは使わない(形状演算の結果が変わりうる)。"""
    _build_document(session, "chain.json")
    models = session.models_dir
    monkeypatch.setattr(document_snapshot, "code_fingerprint", lambda: "another-version")
    replayed = []
    original = OpLog.replay
    monkeypatch.setattr(OpLog, "replay", lambda self, document: (replayed.append(True), original(self, document)))
    _open(models, "chain.json")
    assert replayed


def test_hidden_bodies_come_back_visible_like_a_replayed_document(session: Session) -> None:
    """表示・非表示は保存の対象外(ログに載らない)。スナップショットも載せず、開いた文書は全部表示。"""
    _build_document(session, "chain.json")
    models = session.models_dir
    from duocad import selection

    selection.set_visible(session.document, session.document.bodies[-1].id, False)
    session.autosave()
    session.autosaver.drain()
    reopened = _open(models, "chain.json")
    assert all(body.visible for body in reopened.document.bodies)


def test_renaming_carries_the_snapshot_and_deleting_drops_it(session: Session, monkeypatch) -> None:
    """改名でスナップショットも付いていき(再生せずに開ける)、削除では消える。"""
    _build_document(session, "chain.json")
    models = session.models_dir
    keeper = _open(models, "chain.json")
    keeper.rename_document("renamed.json")
    assert not _snapshot_file(models, "chain.json").exists()
    assert _snapshot_file(models, "renamed.json").exists()
    monkeypatch.setattr(OpLog, "replay", lambda self, document: pytest.fail("再生した"))
    _open(models, "renamed.json")
    monkeypatch.undo()
    keeper.delete_document("renamed.json")
    assert not _snapshot_file(models, "renamed.json").exists()


def test_small_documents_get_no_snapshot(session: Session, monkeypatch) -> None:
    """再生が速い文書(本番の閾値の下)にはスナップショットを作らない。"""
    monkeypatch.setattr(document_snapshot, "MIN_BUILD_SECONDS", 3600.0)
    _build_document(session, "chain.json")
    _open(session.models_dir, "chain.json")
    assert not _snapshot_file(session.models_dir, "chain.json").exists()


def test_the_reader_builds_only_duocad_classes(session: Session) -> None:
    """`models/` に置かれた細工済みのファイルから、任意の関数を走らせない(組み立てる型は `duocad` の自前のクラスだけ)。"""
    _build_document(session, "chain.json")
    captured = document_snapshot.capture(session.document, memo=document_snapshot.Memo(), build_seconds=1.0)
    evil = io.BytesIO()
    pickle.dump({"bodies": [], "sketches": [], "parameters": {}, "optimize": {}, "x": __import__("os").getcwd}, evil)
    captured.state = evil.getvalue()
    captured.blobs = []
    data = captured.pack("digest")
    with pytest.raises(document_snapshot.SnapshotError):
        document_snapshot.restore(data, file_sha256="digest", operation_count=len(session.document.log.entries))

"""起動後の先読み(`duocad/snapshot_prebuild.py`、issue #310)。

落ちたら: 先読みが文書の JSON を書き換えた・対象でない文書まで再生した・先読みで作ったスナップショットが開く側で使えない・
同じ文書を起動のたびに調べ直している。子プロセスを立てるので 1 回 5 秒前後かかる(import が主)。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from duocad import document_snapshot
from duocad.oplog import OpLog
from duocad.server import Session
from duocad.snapshot_prebuild import Prebuilder
from session_support import box, pad, select


def _write_document(models: Path, name: str, *, extra_pad: bool = False) -> Path:
    """押し出しを含む小さな文書を `models` に書く(別の `Session` で作って保存する)。"""
    maker = Session(0.2, models_dir=models)
    box(maker)
    if extra_pad:
        pad(maker, 10.0, 10.0, 5.0, corner=(40.0, 0.0))
    maker.rename_document(name)  # 自動保存の untitled-N.json を改名する(別のファイルを残さない)
    return models / name


def _status(store, path: Path) -> str:
    return store.snapshot_status(path, document_snapshot.file_digest(path.read_bytes()))


@pytest.fixture()
def models(tmp_path) -> Path:
    return tmp_path / "models"


def test_the_prebuilder_makes_snapshots_the_next_open_reads_without_replaying(models, monkeypatch) -> None:
    """起動後の先読みが、スナップショットの無い文書を子プロセスで再生して預ける。開く側は再生しない。文書の JSON は書き換えない。"""
    monkeypatch.setenv("DUOCAD_SNAPSHOT_MIN_BUILD_SECONDS", "0")  # 子にも継がれる
    paths = [_write_document(models, "a.json"), _write_document(models, "b.json", extra_pad=True)]
    before = {path: path.read_bytes() for path in paths}
    server = Session(0.2, models_dir=models, opened=False)
    assert all(_status(server.store, path) == "missing" for path in paths)

    prebuilder = Prebuilder(server.store, start_delay=0.0)
    try:
        prebuilder.run_once()
    finally:
        prebuilder.stop()

    assert prebuilder.built == 2 and prebuilder.failed == 0
    assert {path: path.read_bytes() for path in paths} == before, "先読みは文書の JSON を書かない"
    assert all(_status(server.store, path) == "ready" for path in paths)
    monkeypatch.setattr(document_snapshot, "MIN_BUILD_SECONDS", 0.0)
    monkeypatch.setattr(OpLog, "replay", lambda self, document: pytest.fail("再生した(先読みの結果を使えなかった)"))
    for name in ("a.json", "b.json"):
        opened = Session(0.2, models_dir=models, opened=False)
        opened.open_document(name)
        assert opened.document.bodies


def test_documents_that_replay_fast_get_a_light_marker_and_are_not_checked_again(models) -> None:
    """再生が速い文書は、スナップショットの代わりに印を預ける。次の起動では子を立てず、開くときは再生で開く(印は残る)。"""
    path = _write_document(models, "small.json")
    server = Session(0.2, models_dir=models, opened=False)

    first = Prebuilder(server.store, start_delay=0.0)
    try:
        first.run_once()
    finally:
        first.stop()
    assert first.built == 1
    assert _status(server.store, path) == "light"

    second = Prebuilder(server.store, start_delay=0.0)
    try:
        second.run_once()
    finally:
        second.stop()
    assert second.built == 0, "印のある文書は調べ直さない(子も立てない)"

    server.open_document("small.json")
    assert server.document.bodies
    assert _status(server.store, path) == "light", "再生で開いても印は消えない"


def test_the_prebuilder_skips_the_open_document_and_only_the_newest_ones(models, monkeypatch) -> None:
    """いま開いている文書は飛ばす。対象は更新日時の新しい `limit` 件だけ。"""
    monkeypatch.setenv("DUOCAD_SNAPSHOT_MIN_BUILD_SECONDS", "0")
    paths = [_write_document(models, f"d{number}.json") for number in range(3)]
    for number, path in enumerate(paths):
        os.utime(path, (1_000_000 + number, 1_000_000 + number))  # d2 が最も新しい
    server = Session(0.2, models_dir=models, opened=False)

    prebuilder = Prebuilder(server.store, current_name=lambda: "d2.json", limit=2, start_delay=0.0)
    try:
        assert prebuilder._candidates() == ["d2.json", "d1.json"]
        prebuilder.run_once()
    finally:
        prebuilder.stop()

    assert prebuilder.built == 1
    assert [_status(server.store, path) for path in paths] == ["missing", "ready", "missing"]


def test_a_changed_file_is_a_candidate_again_and_a_ready_one_is_not(models, monkeypatch) -> None:
    """ファイルが変われば古いスナップショットは鍵が合わず、また対象になる。合っているものは対象にしない。"""
    monkeypatch.setenv("DUOCAD_SNAPSHOT_MIN_BUILD_SECONDS", "0")
    path = _write_document(models, "a.json")
    server = Session(0.2, models_dir=models, opened=False)
    monkeypatch.setattr(document_snapshot, "MIN_BUILD_SECONDS", 0.0)
    opened = Session(0.2, models_dir=models, opened=False)
    opened.open_document("a.json")  # 再生で開き、スナップショットを預ける
    prebuilder = Prebuilder(server.store, start_delay=0.0)
    assert _status(server.store, path) == "ready"
    assert prebuilder._candidates() == []

    pad(opened, 10.0, 10.0, 5.0, corner=(40.0, 0.0))  # 自動保存がファイルを書き換える
    opened.autosaver.drain()
    assert _status(server.store, path) == "ready", "開いた側の自動保存が新しい鍵で預け直す"
    assert prebuilder._candidates() == []
    path.write_bytes(path.read_bytes() + b"\n")  # 外から1バイト変える
    assert prebuilder._candidates() == ["a.json"]


def test_stopping_before_the_delay_never_starts_a_child(models) -> None:
    """起動直後に止めれば、待ち時間のうちに終わり、子を立てない。"""
    _write_document(models, "a.json")
    server = Session(0.2, models_dir=models, opened=False)
    prebuilder = Prebuilder(server.store, start_delay=60.0)
    prebuilder.start()
    prebuilder.stop()
    prebuilder._thread.join(5.0)
    assert not prebuilder._thread.is_alive()
    assert prebuilder.built == 0 and prebuilder._worker is None

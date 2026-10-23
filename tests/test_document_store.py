"""文書の保存部品を、編集中の文書や保存キューなしで検査する。"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from duocad.document_name import MAX_NAME_UNITS
from duocad.document_store import DocumentStore
from duocad.oplog import OpLog


@pytest.fixture
def store(tmp_path):
    return DocumentStore(tmp_path / "models", tmp_path / "exports")


@pytest.mark.parametrize("name,message", [
    ("", "ファイル名が空"),
    (" a.json", "前後の空白"),
    ("../a.json", "区切り文字"),
    ("a\\b.json", "区切り文字"),
    ("D:a.json", "':'は不可"),
    ("a.json:stream", "':'は不可"),
    ("..", "ファイル名として使えない"),
    ("a.json.", "末尾のドット/空白"),
    ("con.json", "予約されたデバイス名"),
    ("a.step", "拡張子は.json"),
    ("a?.json", "ファイル名に"),
    ("a\n.json", "制御文字"),
])
def test_invalid_names_are_refused_before_any_file_is_written(store, name, message):
    with pytest.raises(ValueError, match=message):
        store.resolve_writable_name(name)
    assert not store.models_dir.exists()


def test_a_long_existing_name_can_be_resolved_but_cannot_be_written(store):
    name = "a" * (MAX_NAME_UNITS - 4) + ".json"
    assert store.resolve_document_name(name).name == name
    with pytest.raises(ValueError, match="長すぎる"):
        store.resolve_writable_name(name)


@pytest.mark.parametrize("chunks", [False, True])
def test_model_payloads_round_trip_without_changing_the_bytes(store, chunks):
    log = OpLog()
    log.append("sketch_create", {"name": "部品", "base_plane": "XY"})
    target = store.resolve_writable_name("a.json")
    payload = log.to_file_chunks(created=1.25) if chunks else log.to_json(created=1.25)
    store.write_document_file(target, payload)
    assert target.read_bytes() == b"".join(log.to_file_chunks(created=1.25))
    assert store.read_document_file(target) == json.loads(log.to_json(created=1.25))
    assert not target.with_suffix(".json.writing").exists()


@pytest.mark.parametrize("chunks", [False, True])
def test_a_failed_replace_preserves_the_previous_document(store, monkeypatch, chunks):
    target = store.resolve_writable_name("a.json")
    store.write_document_file(target, OpLog().to_json(created=1.0))
    before = target.read_bytes()

    def refuse(source, destination):
        raise PermissionError("別のプログラムが開いている")

    monkeypatch.setattr(os, "replace", refuse)
    payload = OpLog().to_file_chunks(created=2.0) if chunks else OpLog().to_json(created=2.0)
    with pytest.raises(PermissionError, match="別のプログラム"):
        store.write_document_file(target, payload)
    assert target.read_bytes() == before
    assert store.list_documents()["files"][0]["created"] == 1.0


def test_a_failure_while_writing_chunks_preserves_the_previous_document(store):
    target = store.resolve_writable_name("a.json")
    store.write_document_file(target, OpLog().to_json())
    before = target.read_bytes()
    with pytest.raises(TypeError):
        store.write_document_file(target, [b"incomplete", None])
    assert target.read_bytes() == before


def test_copy_refuses_an_existing_target_and_refreshes_only_the_copys_created_time(store):
    source = store.resolve_writable_name("a.json")
    target = store.resolve_writable_name("b.json")
    store.write_document_file(source, OpLog().to_json(created=1.0))
    store.write_document_file(target, OpLog().to_json(created=2.0))
    before = target.read_bytes()
    with pytest.raises(ValueError, match="b.json は既にある"):
        store.copy_document_file(source, target)
    assert target.read_bytes() == before
    store.delete_document_file(target)
    reply = store.copy_document_file(source, target)
    assert reply == {"action": "copy", "path": str(target), "name": "b.json", "source": "a.json"}
    assert store.read_document_file(source)["created"] == 1.0
    assert store.read_document_file(target)["created"] > 2.0


@pytest.mark.parametrize("operation,message", [
    ("read", "開けるファイルが無い"),
    ("copy", "複製元が無い: absent.json"),
    ("delete", "削除するファイルが無い: absent.json"),
])
def test_missing_documents_report_the_operation(store, operation, message):
    absent = store.resolve_document_name("absent.json")
    with pytest.raises(FileNotFoundError, match=message):
        if operation == "read":
            store.read_document_file(absent)
        elif operation == "copy":
            store.copy_document_file(absent, store.resolve_writable_name("b.json"))
        else:
            store.delete_document_file(absent)


def test_export_paths_stay_in_the_export_directory_and_reject_a_document_hard_link(store):
    assert store.resolve_export_path("sub/a.step", (".step",)) == store.exports_dir / "sub/a.step"
    with pytest.raises(ValueError, match="export/の外"):
        store.resolve_export_path("../a.step", (".step",))
    source = store.resolve_writable_name("a.json")
    store.write_document_file(source, OpLog().to_json())
    alias = store.exports_dir / "alias.step"
    os.link(source, alias)
    before = source.read_bytes()
    with pytest.raises(ValueError, match="保存済みの文書 a.json"):
        store.resolve_export_path(str(alias), (".step",))
    assert source.read_bytes() == before


def test_a_thumbnail_write_failure_reports_a_warning_and_preserves_the_previous_image(store, monkeypatch):
    source = store.resolve_writable_name("a.json")
    store.write_document_file(source, OpLog().to_json())
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"old").decode("ascii")
    assert store.save_thumbnail("a.json", png)["saved"] is True

    def refuse(source, destination):
        raise PermissionError("書けない")

    monkeypatch.setattr(os, "replace", refuse)
    other = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"new").decode("ascii")
    assert store.save_thumbnail("a.json", other)["saved"] is False
    assert store.get_thumbnail("a.json")["png"] == png
    assert store.warnings == ["サムネイルを書けなかった: 書けない"]


@pytest.mark.parametrize("explicit", [False, True])
def test_session_outputs_use_temporary_directories_even_with_default_paths(tmp_path, explicit):
    """落ちたら、既定パスの差し替えが保存部品へ届かず実 models/ や export/ に書き込む。"""
    from duocad.server import Session
    from session_support import box

    models = tmp_path / ("explicit-models" if explicit else "default-models")
    exports = tmp_path / ("explicit-export" if explicit else "default-export")
    session = Session(0.2, models_dir=models, exports_dir=exports) if explicit else Session(0.2)
    assert session.models_dir == models
    assert session.exports_dir == exports
    box(session)
    saved = session.save_document("a.json")
    assert Path(saved["path"]) == models / "a.json"
    assert Path(session.list_documents()["path"]) == models
    copied = session.copy_document("a.json", "b.json")
    assert Path(copied["path"]) == models / "b.json"
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")
    thumbnail = session.save_thumbnail("a.json", png)
    assert thumbnail["saved"] is True
    assert Path(thumbnail["path"]) == models / ".thumbnails/a.json.png"
    for suffix in ("step", "stl"):
        exported = getattr(session, f"export_{suffix}_file")(f"a.{suffix}")
        assert Path(exported["path"]) == exports / f"a.{suffix}"
        assert Path(exported["path"]).stat().st_size > 0

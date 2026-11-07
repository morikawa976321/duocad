"""文書ファイルの大文字小文字だけの改名。落ちたら、綴りの直しが「既にある」で断られるか、サムネイルが消える。"""
from __future__ import annotations

import base64

import pytest

from duocad.server import Session
from session_support import box

_PNG = base64.b64encode(bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00" * 16).decode()


def _case_insensitive(directory) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / "Probe.tmp"
    probe.write_text("x")
    try:
        return (directory / "probe.tmp").exists()
    finally:
        probe.unlink()


def test_a_case_only_rename_of_the_open_file_goes_through_and_keeps_its_thumbnail(session: Session) -> None:
    if not _case_insensitive(session.models_dir):
        pytest.skip("大文字小文字を区別するファイルシステムでは別のファイルになる")
    box(session)
    session.save_document("keyboard.json")
    assert session.save_thumbnail("keyboard.json", _PNG)["saved"] is True

    answer = session.rename_document("Keyboard.json")

    assert answer["name"] == "Keyboard.json" and session.current_name == "Keyboard.json"
    assert "Keyboard.json" in [path.name for path in session.models_dir.iterdir()]
    assert "Keyboard.json.png" in [path.name for path in (session.models_dir / ".thumbnails").iterdir()]


def test_renaming_to_the_same_spelling_is_still_refused(session: Session) -> None:
    box(session)
    session.save_document("same.json")

    with pytest.raises(ValueError, match="既にある"):
        session.rename_document("same.json")

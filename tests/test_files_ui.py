"""サーバ側の契約: ブラウザから開く/保存/書き出しを踏む口、`state.parameters`、`state.redo_count` の配信。
保存→開く→同じ形は再生経路が壊れていないことの検査そのもの。UIの実機確認は `web/scripts/smoke-files.mjs`。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from duocad import io_step, kernel, query
from duocad.server import Session
from duocad.session import NoDocument
from session_support import SIDE, box as _box, browser as _browser, cylinder as _cylinder, edit_parameter, select as _select, send_one

DEVIATION = 0.2


def test_saving_writes_the_operation_log_and_is_not_a_verb(session: Session, tmp_path: Path) -> None:
    """保存は操作ログのJSONを書くだけで、操作ログは伸びない。応答は `{"type":"file"}` の封筒で `applied` は付かない(verbではない)。
    その後に状態も配り直す(「いま編集しているファイル」= `state.current_name` が変わるため)。
    """
    _box(session)
    log_before = len(session.document.log.entries)

    async def script(send):
        return await send({"cmd": "save_document", "name": "model.json"})

    (_last, clients) = _browser(session, script)

    answer = clients[0].last("file")
    target = session.models_dir / "model.json"
    assert answer["type"] == "file" and answer["action"] == "save"
    assert Path(answer["path"]) == target
    assert answer["name"] == "model.json"
    assert "bytes" not in answer and "operations" not in answer, "ファイルのサイズ・操作数は返さない"
    assert len(session.document.log.entries) == log_before
    assert len(json.loads(target.read_text(encoding="utf-8"))["operations"]) == log_before, "ログ全体を書いていない"
    # # 状態の配り直しに `applied` は載らない(画面の `lastApplied` を動かさないため)。
    state = clients[0].last("state")
    assert "applied" not in state, "保存がverbの確定として配信されている"
    assert state["state"]["current_name"] == "model.json"
    # # 他の接続にも届く(現在ファイルはサーバの状態)。
    assert clients[1].last("state")["state"]["current_name"] == "model.json"


def test_saving_over_another_file_is_refused_and_leaves_it_untouched(session: Session) -> None:
    """編集中でないファイルは1バイトも書き換わらない。`save_document(name)` は新しい名前でのみ保存し、現在名を渡す道も塞ぐ。"""
    _box(session)
    session.save_document("other.json")
    other = session.models_dir / "other.json"
    untouched = other.read_bytes()
    session.new_document()
    _box(session)
    session.save_document("mine.json")

    with pytest.raises(ValueError) as refusal:
        session.save_document("other.json")

    assert other.read_bytes() == untouched, "編集中でないファイルが書き換わった"
    assert "other.json" in str(refusal.value) and "開け" in str(refusal.value)
    assert session.state_meta()["current_name"] == "mine.json", "現在ファイルが動いた"

    mine = session.models_dir / "mine.json"
    before = mine.read_bytes()
    with pytest.raises(ValueError) as same:
        session.save_document("mine.json")
    assert mine.read_bytes() == before
    assert "Ctrl+S" in str(same.value), "上書きの正しい道を教えていない"


def test_saving_without_a_name_writes_the_current_file_only(session: Session) -> None:
    """引数なしの `save_document()` は編集中のファイルへ上書きする。無ければ拒否(起動直後と `new_document` 直後だけ)。"""
    with pytest.raises(ValueError) as fresh_start:
        session.save_document()
    assert "編集中のファイルが無い" in str(fresh_start.value)

    _box(session)
    assert session.current_name == "untitled-1.json"
    session.save_document("mine.json")
    target = session.models_dir / "mine.json"
    operations_before = json.loads(target.read_text(encoding="utf-8"))["operations"]
    session.undo()
    autosaved = json.loads(target.read_text(encoding="utf-8"))["operations"]
    assert len(autosaved) == len(operations_before) - 1, "undo が自動保存されていない"

    answer = session.save_document()

    assert Path(answer["path"]) == target and answer["name"] == "mine.json"
    written = json.loads(target.read_text(encoding="utf-8"))["operations"]
    assert len(written) == len(operations_before) - 1, "上書きが効いていない"
    assert {item["name"] for item in session.list_documents()["files"]} == {
        "untitled-1.json",
        "mine.json",
    }, "引数なしの保存が別のファイルを作った"

    session.new_document()
    with pytest.raises(ValueError):
        session.save_document()
    assert json.loads(target.read_text(encoding="utf-8"))["operations"] == written, (
        "新規の直後の保存が、直前まで編集していたファイルを空の文書で潰した"
    )


def test_every_document_change_is_autosaved_and_undo_redo_follow(session: Session) -> None:
    """落ちたら: verb・undo・redo のどれかがディスクに写らない(ファイルが今の文書の鏡でない)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    assert session.current_name == "untitled-1.json"
    target = session.models_dir / "untitled-1.json"

    def ops() -> int:
        return len(json.loads(target.read_text(encoding="utf-8"))["operations"])

    assert ops() == 1 and session.state_meta()["saved"] is True
    session.add_circle(sketch_id, [0.0, 0.0], 10.0)
    assert ops() == 2
    session.undo()
    assert ops() == 1
    session.redo()
    assert ops() == 2
    session.set_sketch_visible(sketch_id, False)  # 可視性はファイルに載らない(書き直しも起きない)
    assert ops() == 2 and "visible" not in target.read_text(encoding="utf-8")


def test_an_untitled_document_gets_the_next_free_untitled_name(session: Session) -> None:
    """落ちたら: 既にある `untitled-1.json` を新しい無題の文書が潰す。"""
    session.models_dir.mkdir(parents=True)
    (session.models_dir / "untitled-1.json").write_text("{}", encoding="utf-8")

    session.sketch_create("XY")

    assert session.current_name == "untitled-2.json"
    assert (session.models_dir / "untitled-1.json").read_text(encoding="utf-8") == "{}"
    session.new_document()
    assert session.state_meta()["saved"] is True and session.current_name is None
    session.sketch_create("XZ")
    assert session.current_name == "untitled-3.json"


def test_a_failed_autosave_marks_the_document_unsaved_and_warns(
    session: Session, monkeypatch
) -> None:
    """落ちたら: 書けなかったのに「保存済み」のまま(画面が知らずに閉じられる)、または
    verb 自体が失敗する(文書の変更は確定しているので巻き戻せない)。"""
    session.sketch_create("XY")

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("duocad.session.os.replace", broken)
    applied = session.sketch_create("XZ")

    assert applied["verb"] == "sketch_create" and len(session.document.sketches) == 2
    assert session.state_meta()["saved"] is False
    assert any("自動保存できなかった" in line for line in session.warnings)

    monkeypatch.undo()
    session.save_document()
    assert session.state_meta()["saved"] is True


def test_rename_document_moves_the_current_file_and_refuses_existing_names(session: Session) -> None:
    """落ちたら: 改名で元のファイルが残る / 別のファイルを潰す / 編集中の名前が動かない。"""
    _box(session)
    session.save_document("other.json")
    untouched = (session.models_dir / "other.json").read_bytes()
    session.new_document()
    _box(session)
    assert session.current_name == "untitled-2.json"

    answer = session.rename_document("mine.json")

    assert answer["action"] == "rename" and answer["name"] == "mine.json"
    assert answer["previous"] == "untitled-2.json"
    assert session.current_name == "mine.json"
    assert not (session.models_dir / "untitled-2.json").exists(), "元の名前のファイルが残った"
    mine = session.models_dir / "mine.json"
    assert json.loads(mine.read_text(encoding="utf-8"))["operations"]

    with pytest.raises(ValueError) as refusal:
        session.rename_document("other.json")
    assert "other.json" in str(refusal.value) and "既にある" in str(refusal.value)
    assert (session.models_dir / "other.json").read_bytes() == untouched
    assert session.current_name == "mine.json"

    session.sketch_create("YZ")
    written = json.loads(mine.read_text(encoding="utf-8"))["operations"]
    assert len(written) == len(session.document.log.entries)


def test_rename_on_an_untitled_empty_document_creates_the_file(session: Session) -> None:
    """無題で何も変えていない文書(ファイルがまだ無い)を改名すると、その名前で空の文書が書かれる。"""
    answer = session.rename_document("fresh.json")

    assert answer["action"] == "rename" and answer["previous"] is None
    assert session.current_name == "fresh.json"
    assert json.loads((session.models_dir / "fresh.json").read_text(encoding="utf-8"))["operations"] == []


def test_rename_is_a_file_envelope_followed_by_a_state_push(session: Session) -> None:
    """`save_document` と同じ形: `file` の封筒 + 状態の配り直し。全接続へ届く。"""
    from duocad import catalog

    _box(session)

    async def script(send):
        return await send({"cmd": "rename_document", "name": "renamed.json"})

    (last, clients) = _browser(session, script)

    assert last["type"] == "state" and last["state"]["current_name"] == "renamed.json"
    assert last["state"]["saved"] is True
    assert clients[0].last("file")["action"] == "rename"
    assert clients[1].last("state")["state"]["current_name"] == "renamed.json"
    assert "rename_document" not in catalog.tool_names()


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "   ",
        "sub/model.json",
        "sub\\model.json",
        "..",
        "../model.json",
        "C:\\model.json",
        # # 実測: `Path.is_absolute()` が False を返すドライブ相対パス。pathlib の `/` は右辺のドライブ付きセグメントを
        # # 新しいアンカーにして `models_dir` を捨てる。
        "D:outside.json",
        # NTFS代替データストリーム。同じ"コロン"の入口を通る。
        "a.json:x",
        # Windowsの予約デバイス名(拡張子を付けても特別扱いされる)。
        "CON",
        "CON.json",
        "com1.json",
        # 末尾のドット/空白はWindowsが黙って落とすため、別名のつもりが同じ実体になる。
        "model.json.",
        "model.json ",
        " model.json",
        # 拡張子違いは一覧(*.jsonだけを見る)から二度と開けなくなる。
        "model",
        "model.txt",
    ],
)
def test_save_and_open_reject_anything_but_a_bare_filename(session: Session, bad_name: str) -> None:
    """ファイル名だけを受け取る。区切り文字・絶対パス・`..`・ドライブ相対・代替データストリーム・予約デバイス名・末尾のドット/空白・拡張子違いは拒否する。"""
    _box(session)
    with pytest.raises(ValueError):
        session.save_document(bad_name)
    with pytest.raises(ValueError):
        session.open_document(bad_name)


def test_a_drive_relative_escape_never_touches_the_drive(session: Session) -> None:
    """ドライブ相対パス(`D:outside.json`)は拒否され、実際に何も書かれない(models_dirと同じドライブへすり替えて確かめる)。"""
    _box(session)
    drive = session.models_dir.drive or "C:"
    escape_name = f"{drive}outside-escape-test.json"

    with pytest.raises(ValueError):
        session.save_document(escape_name)

    assert not (session.models_dir / "outside-escape-test.json").exists()


def test_resolved_path_must_stay_inside_models_dir_even_past_a_symlink(
    session: Session, tmp_path: Path
) -> None:
    """判定は解決後のパスで行う: `models_dir` 内の既存シンボリックリンクが外を指す場合を塞ぐ(`_resolve_document_name` の最終判定)。
    Windowsはシンボリックリンクに開発者モードか管理者権限が要るので、無ければ明示的にskipする。
    """
    _box(session)
    session.models_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = session.models_dir / "escape.json"
    try:
        link.symlink_to(outside / "target.json")
    except OSError as error:
        pytest.skip(f"このマシンではシンボリックリンクを作れない: {error}")

    with pytest.raises(ValueError):
        session.save_document("escape.json")

    assert not (outside / "target.json").exists(), "リンクの先(models_dirの外)に書けてしまった"

def test_opening_replays_a_log_that_contains_set_parameter(session: Session) -> None:
    """落ちたら: `set_parameter` を含むログが開けない(再生前にログを文書へ束ねていないと `parameters.rebuild` が拒否する)。"""
    _cylinder(session, 5.0, 12.0)
    diameter = next(name for name, value in session.document.parameters.items() if value == 10.0)
    edit_parameter(session, diameter, 14.0)
    volume = query.volume(session.document.bodies[-1].shape)
    session.save_document("with-parameters.json")

    fresh = Session(None, DEVIATION, models_dir=session.models_dir)
    fresh.open_document("with-parameters.json")

    assert query.volume(fresh.document.bodies[-1].shape) == pytest.approx(volume, rel=1e-9)
    assert fresh.document.parameters[diameter] == pytest.approx(14.0)


def test_opening_rebuilds_the_same_shape_and_reaches_every_connection(session: Session) -> None:
    """保存 → 開く → 同じ形。開いた文書は全接続へ届く。形は操作ログから再生されるので、再生経路が壊れていないことの検査でもある。"""
    body, _ = _box(session)
    volume = query.volume(body.shape)
    faces = kernel.face_count(body.shape)
    session.save_document("model.json")

    fresh = Session(None, DEVIATION, models_dir=session.models_dir)

    async def script(send):
        return await send({"cmd": "open_document", "name": "model.json", "client_op": 3})

    (answer, clients) = _browser(fresh, script)

    assert answer["type"] == "state"
    assert answer["applied"]["change"] == "document"
    assert answer["applied"]["name"] == "model.json"
    assert answer["applied"]["client_op"] == 3
    reopened = fresh.document.bodies[-1]
    assert kernel.face_count(reopened.shape) == faces
    assert query.volume(reopened.shape) == pytest.approx(volume, rel=1e-9)
    assert [entry.verb for entry in fresh.document.log.entries] == [
        entry.verb for entry in session.document.log.entries
    ]
    assert clients[1].payloads, "開いた文書が他の接続へ届いていない"


def test_opening_bumps_the_revision_even_when_the_body_id_repeats(session: Session) -> None:
    """落ちたら: 再生でBodyのidが再現しうるのに版まで元へ戻り、そのidを知るクライアントが前の文書の形を描き続ける。"""
    body, _ = _box(session)
    before = session.body_revision(body.id)
    session.save_document("model.json")

    session.open_document("model.json")

    reopened = session.document.bodies[-1]
    assert session.body_revision(reopened.id) > before


def test_a_failed_open_leaves_the_current_document_alone(session: Session) -> None:
    """**開くのに失敗しても今の文書を壊さない**(新しい文書の上で再生し切ってから差し替える)。"""
    body, _ = _box(session)
    volume = query.volume(body.shape)
    session.models_dir.mkdir(parents=True, exist_ok=True)
    broken = session.models_dir / "broken.json"
    # # 版は現行(2)にする(1だと旧形式で先に断られ、未知verbの再生失敗の経路に届かない)。
    broken.write_text(
        '{"version": 2, "operations": [{"seq": 1, "verb": "no_such_verb"}]}', encoding="utf-8"
    )

    with pytest.raises(Exception):
        session.open_document("broken.json")

    assert session.document.bodies[-1] is body
    assert query.volume(body.shape) == pytest.approx(volume)

    async def script(send):
        return await send({"cmd": "open_document", "name": "missing.json"})

    (answer, _clients) = _browser(session, script)
    assert answer["type"] == "error"
    assert query.volume(session.document.bodies[-1].shape) == pytest.approx(volume)


def test_reopening_the_current_document_is_refused_with_another_tab_connected(
    session: Session,
) -> None:
    """落ちたら: 2接続以上で今の文書を開き直せてしまい、別タブの未保存の編集が黙って消える(issue #16)。"""
    _box(session)
    session.save_document("model.json")
    session.open_document("model.json")
    body = session.document.bodies[-1]
    volume = query.volume(body.shape)

    async def script(send):
        return await send({"cmd": "open_document", "name": "model.json"})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "error"
    assert "model.json" in answer["message"] and "別のタブ" in answer["message"]
    assert session.document.bodies[-1] is body, "拒否されたのに文書が差し替わった"
    assert query.volume(session.document.bodies[-1].shape) == pytest.approx(volume)
    assert clients[1].payloads == [], "拒否された開くが他の接続へ配信された"


def test_reopening_the_current_document_is_allowed_with_only_one_connection(
    session: Session,
) -> None:
    """接続が1つだけなら開き直しは通る(保存前に戻す手立て)。"""
    _box(session)
    session.save_document("model.json")
    session.open_document("model.json")

    answer = send_one(session, {"cmd": "open_document", "name": "model.json"})

    assert answer["type"] == "state"
    assert answer["applied"]["action"] == "open"


def test_new_document_empties_the_document_and_reaches_every_connection(session: Session) -> None:
    """新規は空の文書へ差し替え、全接続へ届く。`name` / `path` は `None`。"""
    _box(session)
    assert session.document.bodies and session.document.log.entries

    async def script(send):
        return await send({"cmd": "new_document", "client_op": 7})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "state"
    assert answer["applied"]["change"] == "document"
    assert answer["applied"]["action"] == "new"
    assert answer["applied"]["name"] is None
    assert answer["applied"]["path"] is None
    assert answer["applied"]["client_op"] == 7
    assert session.document.bodies == []
    assert session.document.sketches == []
    assert answer["state"]["bodies"] == []
    assert clients[1].payloads, "新規にした文書が他の接続へ届いていない"


def test_new_document_is_not_a_verb_and_leaves_nothing_to_undo(session: Session) -> None:
    """新規は操作ログに載らず、undoでは戻らない(新しい文書のログは空)。"""
    _box(session)

    session.new_document()

    assert session.document.log.entries == []
    with pytest.raises(ValueError):
        session.undo()


def test_new_document_clears_the_selection(session: Session) -> None:
    """新規で選択は空になる(面・辺・領域の添字は文書が変われば意味を失う)。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    assert session.document.selection.items

    session.new_document()

    assert session.document.selection.items == []


def test_new_document_is_not_exposed_to_ai(session: Session) -> None:
    """新規はAIには開けていない(今の文書を丸ごと捨て、undoでも戻せないため)。"""
    from duocad import catalog

    assert "new_document" not in catalog.tool_names()
    assert "new_document" in catalog.websocket_command_names()


def test_copy_document_duplicates_the_file_and_leaves_the_document_alone(session: Session) -> None:
    """落ちたら: 複製が元と違う中身になる、または複製で編集中の文書/ファイル名が動く。"""
    _box(session)
    session.save_document("a.json")
    session.new_document()
    _box(session)
    session.save_document("mine.json")
    log_before = len(session.document.log.entries)

    async def script(send):
        return await send({"cmd": "copy_document", "source": "a.json", "dest": "b.json"})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "file" and answer["action"] == "copy"
    assert answer["name"] == "b.json" and answer["source"] == "a.json"
    copied = json.loads((session.models_dir / "b.json").read_text(encoding="utf-8"))
    original = json.loads((session.models_dir / "a.json").read_text(encoding="utf-8"))
    assert copied["operations"] == original["operations"]
    assert copied["created"] >= original["created"], "複製は新しいファイル(作成日時は複製した時刻)"
    assert session.state_meta()["current_name"] == "mine.json", "複製で編集中のファイルが動いた"
    assert len(session.document.log.entries) == log_before, "複製がverbとしてログに載った"
    assert clients[1].payloads == [], "複製は状態配信ではない(要求した接続にだけ返る)"
    # 無題の文書が最初の変更で自動保存した `untitled-<n>.json` も居る(意図どおり)。
    assert {item["name"] for item in session.list_documents()["files"]} == {
        "a.json", "b.json", "mine.json", "untitled-1.json", "untitled-2.json",
    }


def test_copy_document_refuses_an_existing_destination_and_bad_names(session: Session) -> None:
    """落ちたら: 複製で既存ファイルが上書きされる(1バイトでも変わる)、または models/ の外を指せる。"""
    _box(session)
    session.save_document("a.json")
    session.sketch_create("XY")
    session.save_document("b.json")
    untouched = (session.models_dir / "b.json").read_bytes()

    with pytest.raises(ValueError) as refusal:
        session.copy_document("a.json", "b.json")
    assert "b.json" in str(refusal.value) and "既にある" in str(refusal.value)
    assert (session.models_dir / "b.json").read_bytes() == untouched

    with pytest.raises(FileNotFoundError):
        session.copy_document("nope.json", "c.json")
    assert not (session.models_dir / "c.json").exists()
    with pytest.raises(ValueError):
        session.copy_document("a.json", "../outside.json")
    with pytest.raises(ValueError):
        session.copy_document("a.json", "a.json")


def test_delete_document_of_another_file_leaves_the_current_one_alone(session: Session) -> None:
    """落ちたら: 編集中でないファイルの削除で、編集中の文書が動くか閉じる。"""
    _box(session)
    session.save_document("old.json")
    session.new_document()
    _box(session)
    session.save_document("mine.json")
    log_before = len(session.document.log.entries)

    async def script(send):
        return await send({"cmd": "delete_document", "name": "old.json"})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "file" and answer["action"] == "delete" and answer["name"] == "old.json"
    assert not (session.models_dir / "old.json").exists()
    assert session.state_meta()["current_name"] == "mine.json"
    assert len(session.document.log.entries) == log_before
    assert clients[1].payloads == []
    with pytest.raises(FileNotFoundError):
        session.delete_document("old.json")


def test_deleting_the_open_document_closes_it_everywhere(session: Session) -> None:
    """落ちたら: 開いている文書を消しても中身が残り、次の操作の自動保存が消したはずの中身を
    `untitled-<n>.json` として復活させる。または他のタブが消えた文書を出したままになる。"""
    _box(session)
    session.rename_document("mine.json")

    async def script(send):
        return await send({"cmd": "delete_document", "name": "mine.json"})

    (_, clients) = _browser(session, script)

    envelope = next(payload for payload in clients[0].payloads if payload["type"] == "file")
    assert envelope["action"] == "delete" and envelope["closed"] is True
    assert not (session.models_dir / "mine.json").exists()
    assert session.document_open is False
    assert session.document.bodies == [] and session.document.log.entries == []
    for client in clients:
        pushed = [payload for payload in client.payloads if payload["type"] == "state"]
        assert pushed and pushed[-1]["state"]["document_open"] is False

    with pytest.raises(NoDocument):
        _box(session)
    assert list(session.models_dir.glob("*.json")) == []


def test_closed_document_refuses_edits_and_writes_nothing(session: Session) -> None:
    """落ちたら: トップ画面のまま操作が通り、「何も開いていない」が黙って無題の文書になる
    (verb・undo・名前を付ける、のどの口からでも)。"""
    _box(session)
    session.rename_document("mine.json")
    closed = session.close_document()

    assert closed["change"] == "document" and closed["action"] == "close"
    assert session.state_meta()["document_open"] is False
    assert session.current_name is None
    for edit in (
        lambda: _box(session),
        session.undo,
        lambda: session.rename_document("named.json"),
        lambda: session.save_document("named.json"),
    ):
        with pytest.raises(NoDocument):
            edit()
    assert sorted(path.name for path in session.models_dir.glob("*.json")) == ["mine.json"]

    session.new_document()
    assert session.document_open is True
    _box(session)


def test_copy_and_delete_are_not_exposed_to_ai(session: Session) -> None:
    """ファイルの出し入れは人の操作(`save_document` 等と同じ線)。削除は不可逆でもある。"""
    from duocad import catalog

    for name in ("copy_document", "delete_document"):
        assert name not in catalog.tool_names()
        assert name in catalog.websocket_command_names()


def test_list_documents_reports_names_and_dates_only(session: Session) -> None:
    """`list_documents` が `models_dir` の中身を名前・更新日時・作成日時・サムネイルの版で返す
    (件数・サイズ・画像そのものは返さない)。"""
    _box(session)
    session.save_document("a.json")
    session.new_document()  # a.json を編集中から外す(以後の自動保存が a.json へ行かないように)
    _box(session)
    session.save_document("b.json")

    async def script(send):
        return await send({"cmd": "list_documents"})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "file" and answer["action"] == "list"
    by_name = {item["name"]: item for item in answer["files"]}
    assert set(by_name) == {"a.json", "b.json", "untitled-1.json", "untitled-2.json"}
    assert set(by_name["a.json"]) == {"name", "mtime", "created", "thumb"}
    assert by_name["a.json"]["mtime"] > 0
    assert by_name["a.json"]["created"] > 0
    assert by_name["a.json"]["thumb"] is None, "撮っていない文書にサムネイルの版がある"
    assert clients[1].payloads == [], "一覧も状態配信ではない(要求した接続にだけ返る)"


def test_list_documents_creates_the_folder_and_skips_unreadable_files(session: Session) -> None:
    """一覧も**無ければ作る**(要件1)。壊れたファイルは例外で丸ごと落ちず、読める分だけ返す。"""
    assert not session.models_dir.exists()

    async def script(send):
        return await send({"cmd": "list_documents"})

    (answer, _clients) = _browser(session, script)
    assert answer["files"] == []
    assert session.models_dir.is_dir()

    (session.models_dir / "broken.json").write_text("not json at all", encoding="utf-8")
    _box(session)
    session.save_document("good.json")

    (answer, _clients) = _browser(session, script)
    assert [item["name"] for item in answer["files"]] == ["good.json", "untitled-1.json"]


def _created(session: Session, name: str):
    return json.loads((session.models_dir / name).read_text(encoding="utf-8")).get("created")


def test_created_is_stamped_once_and_survives_autosave_rename_and_reopen(session: Session) -> None:
    """落ちたら: 作成日時が保存のたびに書き換わる(=更新日時と同じになる)、または改名・開き直しで消える。"""
    _box(session)
    session.save_document("a.json")
    stamped = _created(session, "a.json")
    assert stamped is not None and stamped > 0

    session.sketch_create("XY")
    assert _created(session, "a.json") == stamped
    session.rename_document("b.json")
    assert _created(session, "b.json") == stamped
    session.new_document()
    session.open_document("b.json")
    session.sketch_create("XY")
    assert _created(session, "b.json") == stamped
    assert {item["name"]: item["created"] for item in session.list_documents()["files"]}["b.json"] == stamped


def test_an_untitled_document_is_stamped_by_its_first_autosave(session: Session) -> None:
    _box(session)
    assert session.current_name == "untitled-1.json"
    assert _created(session, "untitled-1.json") is not None


def test_a_file_without_created_keeps_none_instead_of_borrowing_the_mtime(session: Session) -> None:
    """落ちたら: 作成日時を持たない既存ファイルに、開いて保存しただけで作り物の作成日時が入る。"""
    _box(session)
    session.save_document("old.json")
    payload = json.loads((session.models_dir / "old.json").read_text(encoding="utf-8"))
    del payload["created"]
    (session.models_dir / "old.json").write_text(json.dumps(payload), encoding="utf-8")

    session.new_document()
    session.open_document("old.json")
    session.sketch_create("XY")
    assert _created(session, "old.json") is None
    by_name = {item["name"]: item for item in session.list_documents()["files"]}
    assert by_name["old.json"]["created"] is None


def test_a_copy_gets_its_own_created_instead_of_the_source_one(session: Session) -> None:
    """落ちたら: 複製が元のバイト列をそのまま写していて、作成日時が元の文書のままになる。"""
    _box(session)
    session.save_document("a.json")
    payload = json.loads((session.models_dir / "a.json").read_text(encoding="utf-8"))
    payload["created"] = 1.0
    (session.models_dir / "a.json").write_text(json.dumps(payload), encoding="utf-8")
    session.new_document()

    session.copy_document("a.json", "b.json")
    assert _created(session, "b.json") > 1.0
    assert _created(session, "a.json") == 1.0, "複製が元の作成日時を書き換えた"


_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode("ascii")


def _thumbnail(session: Session, name: str) -> Path:
    return session.models_dir / ".thumbnails" / f"{name}.png"


def test_a_thumbnail_round_trips_and_is_listed_by_version_only(session: Session) -> None:
    """落ちたら: 預けた絵が返らない、一覧に版が出ない、または付随ファイルが一覧に文書として混ざる。"""
    _box(session)
    session.save_document("a.json")
    log_before = len(session.document.log.entries)

    async def script(send):
        await send({"cmd": "save_thumbnail", "name": "a.json", "png": _PNG})
        return await send({"cmd": "get_thumbnail", "name": "a.json"})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "file" and answer["action"] == "thumbnail"
    assert answer["png"] == _PNG and answer["thumb"] > 0
    saved = clients[0].payloads[0]
    assert saved["action"] == "save_thumbnail" and saved["saved"] is True
    assert saved["thumb"] == answer["thumb"]
    assert clients[1].payloads == [], "サムネイルは状態配信ではない"
    by_name = {item["name"]: item for item in session.list_documents()["files"]}
    assert set(by_name) == {"a.json", "untitled-1.json"}
    assert by_name["a.json"]["thumb"] == answer["thumb"]
    assert len(session.document.log.entries) == log_before, "サムネイルがverbとしてログに載った"


def test_an_empty_png_removes_the_thumbnail(session: Session) -> None:
    _box(session)
    session.save_document("a.json")
    session.save_thumbnail("a.json", _PNG)
    assert session.save_thumbnail("a.json", "")["thumb"] is None
    assert not _thumbnail(session, "a.json").exists()
    assert session.get_thumbnail("a.json")["png"] is None


def test_a_thumbnail_must_be_a_small_png_inside_models_dir(session: Session) -> None:
    """落ちたら: PNG以外・巨大なデータ・models/ の外を指す名前がディスクへ書かれる。"""
    _box(session)
    session.save_document("a.json")
    with pytest.raises(ValueError):
        session.save_thumbnail("a.json", base64.b64encode(b"GIF89a....").decode("ascii"))
    with pytest.raises(ValueError):
        session.save_thumbnail("a.json", "not base64 !!")
    with pytest.raises(ValueError):
        huge = b"\x89PNG\r\n\x1a\n" + b"\x00" * (300 * 1024)
        session.save_thumbnail("a.json", base64.b64encode(huge).decode("ascii"))
    with pytest.raises(ValueError):
        session.save_thumbnail("../a.json", _PNG)
    assert not (session.models_dir / ".thumbnails").exists()


def test_a_thumbnail_for_a_missing_document_is_skipped_not_refused(session: Session) -> None:
    """落ちたら: 撮ってから送るまでに改名が挟まっただけで、人に通知が出る(または孤児の絵が残る)。"""
    answer = session.save_thumbnail("gone.json", _PNG)
    assert answer["saved"] is False
    assert not _thumbnail(session, "gone.json").exists()


def test_thumbnails_follow_rename_copy_and_delete(session: Session) -> None:
    """落ちたら: 改名・複製・削除の後に、絵が別の文書に付いたまま・消えたまま・残ったままになる。"""
    _box(session)
    session.save_document("a.json")
    session.save_thumbnail("a.json", _PNG)
    version = session.get_thumbnail("a.json")["thumb"]

    session.rename_document("b.json")
    assert not _thumbnail(session, "a.json").exists()
    assert session.get_thumbnail("b.json")["png"] == _PNG

    session.copy_document("b.json", "c.json")
    copied = session.get_thumbnail("c.json")
    assert copied["png"] == _PNG
    assert copied["thumb"] == version, "複製で版が変わると、画面が同じ絵を取り直す"

    session.delete_document("c.json")
    assert not _thumbnail(session, "c.json").exists()


def test_a_new_file_does_not_inherit_a_stale_thumbnail_of_the_same_name(session: Session) -> None:
    """落ちたら: 外で消された文書の絵が、同じ名前で新しく作った文書の絵として出る。"""
    _box(session)
    session.save_document("a.json")
    session.save_thumbnail("a.json", _PNG)
    session.new_document()
    (session.models_dir / "a.json").unlink()
    _box(session)
    session.rename_document("a.json")
    assert session.get_thumbnail("a.json")["png"] is None


def test_renaming_onto_a_stale_thumbnail_drops_it(session: Session) -> None:
    """落ちたら: 外で消された文書の絵が、別の文書をその名前へ改名したときに引き継がれる。"""
    _box(session)
    session.save_document("gone.json")
    session.save_thumbnail("gone.json", _PNG)
    session.new_document()
    (session.models_dir / "gone.json").unlink()
    _box(session)
    session.save_document("mine.json")
    session.rename_document("gone.json")
    assert session.get_thumbnail("gone.json")["png"] is None


def test_a_thumbnail_that_cannot_be_written_is_reported_not_raised(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """落ちたら: 付随ファイルを書けないだけで、人が何もしていないのにエラーの通知が出る。"""
    import duocad.session as session_module

    _box(session)
    session.save_document("a.json")

    def refuse(*_args, **_kwargs):
        raise PermissionError("別のプログラムが開いている")

    monkeypatch.setattr(session_module.os, "replace", refuse)
    answer = session.save_thumbnail("a.json", _PNG)
    assert answer["saved"] is False and answer["thumb"] is None
    assert any("サムネイル" in warning for warning in session.warnings)


def test_thumbnails_are_not_exposed_to_ai(session: Session) -> None:
    from duocad import catalog

    for name in ("save_thumbnail", "get_thumbnail"):
        assert name not in catalog.tool_names()
        assert name in catalog.websocket_command_names()


def test_relative_export_paths_go_under_exports_dir(session: Session) -> None:
    """相対の書き出し先は起動ディレクトリではなく `exports_dir` へ解決される。"""
    _box(session)

    async def script(send):
        return await send({"cmd": "export_step", "path": "nested/part.step"})

    (answer, _clients) = _browser(session, script)
    target = session.exports_dir / "nested" / "part.step"
    assert Path(answer["path"]) == target
    assert target.is_file() and target.stat().st_size > 0


def test_relative_export_paths_cannot_escape_exports_dir(session: Session) -> None:
    """`..` と別ドライブのドライブ相対パス(`Z:x.step`)が `exports_dir` の外へ書けてしまう。
    後者は `resolve()` の後に包含を判定する順序が崩れると通る。
    """
    _box(session)
    escapes = ["../outside.step"]
    if session.exports_dir.drive:
        other = "Z" if session.exports_dir.drive.upper() != "Z:" else "Y"
        escapes.append(f"{other}:outside.step")
    for path in escapes:
        with pytest.raises(ValueError, match="export/の外"):
            session.export_step_file(path)


def test_stl_export_always_uses_the_fixed_deviation(session: Session, tmp_path: Path) -> None:
    """落ちたら: 粗さを選ぶ口が復活している(`io_step.STL_DEVIATION` 固定)。曲面を持つ形で測ること: 立方体は
    平面だけで三角形が12枚のままファイルサイズが変わらず(684B == 684B)、検査にならない。
    """
    shape = _cylinder(session, 10.0, SIDE).shape
    written = tmp_path / "part.stl"

    async def script(send):
        # 偏差を送っても効かない(表に無い引数は `_dispatch` が拾わない)。
        return await send({"cmd": "export_stl", "path": str(written), "deviation": 0.2})

    (answer, _clients) = _browser(session, script)

    assert answer["action"] == "export_stl"
    assert answer["deviation"] == io_step.STL_DEVIATION
    fixed = _triangle_count(shape, io_step.STL_DEVIATION)
    assert fixed > _triangle_count(shape, 0.2), "固定値と0.2mmで三角形数が同じでは検査にならない"
    assert int.from_bytes(written.read_bytes()[80:84], "little") == fixed


def _triangle_count(shape, deviation: float) -> int:
    payload = kernel.tessellate(shape, deviation, kernel.DEFAULT_ANGULAR_DEFLECTION)
    return sum(len(face["indices"]) // 3 for face in payload["faces"])


def test_named_values_are_published_and_drive_the_shape(session: Session) -> None:
    """**名前つきの値が配られ、打ち直すと形が追随する**(タスクEの検証1の実体)。"""
    body = _cylinder(session, 5.0, 12.0)
    volume_before = query.volume(body.shape)

    published = session.state_meta()["parameters"]
    diameter_name = next(
        name for name, value in published.items() if value == pytest.approx(10.0)
    )

    edit_parameter(session, diameter_name, 16.0)

    assert session.state_meta()["parameters"][diameter_name] == pytest.approx(16.0)
    assert query.volume(session.document.bodies[-1].shape) > volume_before


def test_the_history_position_is_published(session: Session) -> None:
    """**どこまで戻れるか**が配られる(真偽だけでは3回undoと1回undoが同じに見える)。"""
    _box(session)
    session.undo()
    session.undo()

    state = session.state_meta()
    assert state["redo_count"] == 2
    assert state["can_redo"] is True

    session.redo()
    assert session.state_meta()["redo_count"] == 1


def _two_solid_step(tmp_path: Path) -> Path:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    box_a = BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape()
    box_b = BRepPrimAPI_MakeBox(gp_Pnt(50.0, 0.0, 0.0), 6.0, 6.0, 6.0).Shape()
    step_path = tmp_path / "parts.step"
    kernel.write_step(kernel.make_compound([box_a, box_b]), str(step_path))
    return step_path


def test_a_saved_document_opens_after_the_imported_step_is_renamed(
    session: Session, tmp_path: Path
) -> None:
    """読み込んだSTEPを改名しても保存した文書は開ける(STEP本文がログ内にあるため)。改名を挟まないと、パス依存が残っていても緑になる。"""
    step_path = _two_solid_step(tmp_path)
    session.import_step(str(step_path))
    before = [
        (body.name, body.kind, body.visible, kernel.face_count(body.shape),
         round(query.volume(body.shape), 9))
        for body in session.document.bodies
    ]
    assert len(before) == 2, "ソリッド2つぶんの編集用ボディ"
    session.save_document("imported.json")

    step_path.rename(tmp_path / "somewhere_else.step")
    assert not step_path.exists()

    fresh = Session(None, DEVIATION, models_dir=session.models_dir)
    fresh.open_document("imported.json")

    assert [
        (body.name, body.kind, body.visible, kernel.face_count(body.shape),
         round(query.volume(body.shape), 9))
        for body in fresh.document.bodies
    ] == before


def test_the_state_does_not_ship_the_embedded_step_text(session: Session, tmp_path: Path) -> None:
    """状態配信のログはSTEP本文を運ばず文字数だけを出す(PCケースのSTEPは4.6MBで、`state_meta()` は操作のたびに全部配られる)。
    保存(`to_json`)は本文を書く。
    """
    step_path = _two_solid_step(tmp_path)
    session.import_step(str(step_path))
    step_text = session.document.log.entries[-1].args["step"]

    entry = session.state_meta()["log"][-1]

    assert entry["verb"] == "import_step"
    assert entry["args"]["source_name"] == "parts.step"
    assert entry["args"]["step"] != step_text
    assert str(len(step_text)) in entry["args"]["step"], "何文字あるかは出す"
    assert session.document.log.entries[-1].args["step"] == step_text, "ログ本体を壊さない"

    session.save_document("with-step.json")
    saved = json.loads((session.models_dir / "with-step.json").read_text(encoding="utf-8"))
    assert saved["operations"][-1]["args"]["step"] == step_text, "保存側は本文を書くこと"

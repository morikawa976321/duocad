"""サーバ側の契約: ブラウザから**開く/保存/書き出し**を踏む口と、パネルが要る配信。

UI(パラメータ編集パネル・操作履歴パネル・ファイル操作)そのものは**`pytest` では完了判定
できない**(実機確認は `web/scripts/smoke-files.mjs`)。ここが固定するのはサーバ側の3つ ——
ファイル操作の口(`FILE_COMMANDS` + `open_document` + `list_documents`)、`state.parameters`
(名前つきの寸法値=打ち直しの対象)、`state.redo_count`(操作履歴パネルの位置)。とくに
**保存→開く→同じ形**は、UIを介さなくても検査できるうえに**再生経路が壊れていないことの
検査そのもの**なので、ここが正になる。

固定する契約:

- **保存/開くが受け取るのはファイル名だけ**。`models_dir` 配下に閉じ、区切り文字・絶対パス・
  `..` は拒否する
- **保存は「編集中のファイル」1つに閉じている**。`save_document()`(引数なし)は
  いま編集しているファイルへ上書きし、`save_document(name)` は**新しい名前でのみ**保存する
  (既存名は拒否。`name == 現在名` でも拒否する)。編集中のファイルが無ければ引数なしの保存も拒否
- 保存は操作ログのJSONを書き、**verbではない**(操作ログが伸びない)
- **開くと形が完全に戻る**(体積・面数・操作ログ・パラメータ)。開いた文書は**全接続へ**配る
- **開くのに失敗しても今の文書を壊さない**
- 版(revision)は開いた後に必ず上がる —— 同じidのBodyを知っているクライアントに
  古い形を描き続けさせない
- STEP/STL を書き出せ、**STEPは読み戻して体積が一致する**(相対許容差 1e-6)
  (書き出し/読み込みは `path` のまま。ファイル名だけの制約は保存/開くだけの契約)
- STLは**偏差を指定できる**(細かくすると必ずファイルが大きくなる)
- `state.parameters` に名前つきの値が出て、`set_parameter` で形が追随する
- **`list_documents` が `models_dir` の中身(名前/更新日時/サイズ/操作数)を返す**
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from duocad import kernel, query
from duocad.server import Server, Session

DEVIATION = 0.2
SIDE = 20.0


@pytest.fixture()
def session(tmp_path: Path) -> Session:
    # **`models_dir` はテストごとの使い捨てディレクトリ**。指定しないと
    # 既定(リポジトリ直下の `duocad/models/`)を触ってしまう —— pytestが実フォルダを
    # 汚さないための唯一の理由で、テストの意図とは無関係(`Session.__init__` 参照)。
    return Session(None, DEVIATION, models_dir=tmp_path / "models")


def _select(session: Session, refs: list) -> None:
    """選択を `refs` で置き換える。**verbの対象集合はここで決まる**(引数ではない)。"""
    session.clear_selection()
    session.modify_selection(refs, [])


def _box(session: Session):
    """スケッチ→長方形→領域押し出しで 20mm の立方体を作る(D の `_box` と同じ手順)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(SIDE, mode="join")
    session.clear_selection()
    return session.document.bodies[-1], sketch_id


class FakeConnection:
    def __init__(self) -> None:
        self.payloads: list = []

    async def send(self, raw: str) -> None:
        self.payloads.append(json.loads(raw))

    def last(self, kind: str) -> dict:
        for payload in reversed(self.payloads):
            if payload.get("type") == kind:
                return payload
        raise AssertionError(f"{kind!r} の応答が無い: {[p.get('type') for p in self.payloads]}")


def _browser(session: Session, script):
    """`script(send)` を1つのイベントループで走らせる(test_feature_ui.py と同じ harness)。"""

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        clients = [FakeConnection(), FakeConnection()]
        for client in clients:
            server.clients.add(client)
            await server._push(client, applied=None, only=client)
        for client in clients:
            client.payloads.clear()

        async def send(command: dict) -> dict:
            await server._dispatch(clients[0], json.dumps(command))
            return clients[0].payloads[-1]

        return await script(send), clients

    return asyncio.run(scenario())


# --------------------------------------------------------------------------------------
# 保存と再現(§3.1: 操作ログが正・形状は導出物)
# --------------------------------------------------------------------------------------
def test_saving_writes_the_operation_log_and_is_not_a_verb(session: Session, tmp_path: Path) -> None:
    """保存は操作ログのJSONを書くだけ。**操作ログは伸びない**(文書を変えていないので)。

    **受け取るのはファイル名だけ**(`models_dir` 配下に固定。要件2)。

    保存の応答は `{"type":"file"}` の封筒で、**`applied` は付かない**(verbではない)。
    ただしその後に**状態も配り直す** —— 「いま編集しているファイル」が
    サーバの状態(`state.current_name`)になり、名前を付けて保存すればそれが変わるため。
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
    assert answer["operations"] == log_before
    assert len(session.document.log.entries) == log_before
    assert json.loads(target.read_text(encoding="utf-8"))["operations"], "空のログを書いた"
    # **状態の配り直しに `applied` は載らない**(保存はverbではないので、画面の
    # `lastApplied` を動かしてはいけない)。載る値は現在ファイル名だけが変わる。
    state = clients[0].last("state")
    assert "applied" not in state, "保存がverbの確定として配信されている"
    assert state["state"]["current_name"] == "model.json"
    # **他の接続にも届く** —— 現在ファイルはサーバの状態なので、片方の画面だけが
    # 「model.json を編集中」で他方が「(無題)」という食い違いを作らない
    # (`open_document` と同じ線。`Server._dispatch` の save_document 分岐)。
    assert clients[1].last("state")["state"]["current_name"] == "model.json"


def test_saving_over_another_file_is_refused_and_leaves_it_untouched(session: Session) -> None:
    """**編集中でないファイルは1バイトも書き換わらない**(このタスクの存在理由)。

    `save_document(name)` は「**新しい名前でのみ保存**」を意味する。既存名を渡す道は
    UIから消えたが(一覧の行の「上書き」を撤去した)、契約としてもここで塞ぐ ——
    UIを後から足したときに黙って穴が開かないようにするためで、
    「`name` が現在のファイルなら通す」という例外条項も置かない
    (`Session.save_document` のdocstring)。
    """
    _box(session)
    session.save_document("other.json")          # 触られては困る側
    other = session.models_dir / "other.json"
    untouched = other.read_bytes()
    session.new_document()
    _box(session)                                 # 中身の違う別の文書を作る
    session.save_document("mine.json")            # ここからが「編集中」

    with pytest.raises(ValueError) as refusal:
        session.save_document("other.json")

    assert other.read_bytes() == untouched, "編集中でないファイルが書き換わった"
    # **どうすればできるかまで言う**(封筒が `{型名}: {文}` を前置するので、文だけで通じること)
    assert "other.json" in str(refusal.value) and "開け" in str(refusal.value)
    assert session.state_meta()["current_name"] == "mine.json", "現在ファイルが動いた"

    # **現在のファイルの名前を渡す道も塞がっている**(例外条項を置かない)。
    mine = session.models_dir / "mine.json"
    before = mine.read_bytes()
    with pytest.raises(ValueError) as same:
        session.save_document("mine.json")
    assert mine.read_bytes() == before
    assert "Ctrl+S" in str(same.value), "上書きの正しい道を教えていない"


def test_saving_without_a_name_writes_the_current_file_only(session: Session) -> None:
    """引数なしの `save_document()` は**編集中のファイルへ上書き**する。

    そして `new_document` の直後には編集中のファイルが無いので**拒否される** ——
    空の文書はどこにも書かれておらず、上書き先が存在しないのがこの操作の意味である。
    """
    _box(session)
    with pytest.raises(ValueError) as fresh_start:
        session.save_document()
    assert "編集中のファイルが無い" in str(fresh_start.value)

    session.save_document("mine.json")            # 名前を付けて保存 = 以後の上書き先
    target = session.models_dir / "mine.json"
    operations_before = json.loads(target.read_text(encoding="utf-8"))["operations"]
    session.undo()                                # 文書を動かす(上書きが効いたことを見るため)

    answer = session.save_document()

    assert Path(answer["path"]) == target and answer["name"] == "mine.json"
    written = json.loads(target.read_text(encoding="utf-8"))["operations"]
    assert len(written) == len(operations_before) - 1, "上書きが効いていない"
    assert [item["name"] for item in session.list_documents()["files"]] == ["mine.json"], (
        "引数なしの保存が別のファイルを作った"
    )

    session.new_document()
    with pytest.raises(ValueError):
        session.save_document()
    assert json.loads(target.read_text(encoding="utf-8"))["operations"] == written, (
        "新規の直後の保存が、直前まで編集していたファイルを空の文書で潰した"
    )


def test_models_dir_is_created_if_missing(session: Session) -> None:
    """**無ければアプリが作る**(要件1)。事前に存在しなくても保存できる。"""
    assert not session.models_dir.exists()
    _box(session)

    session.save_document("model.json")

    assert session.models_dir.is_dir()
    assert (session.models_dir / "model.json").exists()


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
        # 実測した脱出経路。
        # Path.is_absolute()がFalseを返す「ドライブ相対パス」で、素の絶対パス
        # 判定をすり抜けたうえ、pathlibの`/`が右辺のドライブ付きセグメントを
        # 新しいアンカーとして扱うため`models_dir`側が捨てられる。
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
    """**ファイル名だけを受け取る**(要件2)。区切り文字・絶対パス・`..` は拒否する。

    絶対パスをそのまま許すと、保存先が「その日サーバをどこで起動したか」で変わってしまう
    ので、ここを締める。**ドライブ相対パス・代替データストリーム・予約デバイス名・
    末尾のドット/空白・拡張子違いも同じ理由で拒否する**。
    """
    _box(session)
    with pytest.raises(ValueError):
        session.save_document(bad_name)
    with pytest.raises(ValueError):
        session.open_document(bad_name)


def test_a_drive_relative_escape_never_touches_the_drive(session: Session) -> None:
    """ドライブ相対パスは拒否され、実際に何も書かれない。

    `Path("D:outside.json").is_absolute()` は `False` を返すため絶対パス判定をすり抜け、
    `models_dir / "D:outside.json"` は pathlib の仕様で `models_dir` を捨てて
    `D:\\outside.json` に解決される(実測)。ここでは**models_dirと同じドライブ**にすり替えて、
    拒否されたことと同時にファイルが実際にはどこにも作られていないことを確かめる
    (拒否理由の文言だけを見て安心しない)。
    """
    _box(session)
    drive = session.models_dir.drive or "C:"
    escape_name = f"{drive}outside-escape-test.json"

    with pytest.raises(ValueError):
        session.save_document(escape_name)

    assert not (session.models_dir / "outside-escape-test.json").exists()


def test_resolved_path_must_stay_inside_models_dir_even_past_a_symlink(
    session: Session, tmp_path: Path
) -> None:
    """**正の判定は解決後のパスで行う**。

    文字列だけの拒否では、`models_dir` 内の**既存のシンボリックリンク**が外を指していた場合を
    塞げない。`_resolve_document_name` の最終判定(`resolved.is_relative_to(root)`)がここを
    見る唯一の場所であることを固定する。

    Windowsでシンボリックリンクを作るには開発者モードか管理者権限が要る —— 権限が無い環境では
    意味のある検査ができないので、その場合は明示的にskipする(「検査していないのに緑」を避ける)。
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
    """**打ち直した寸法を含むログが開ける**。

    パラメータの再生は「その操作までを作り直す」ために文書のログを頭から読むので、
    **再生前にログを文書へ束ねていない**と `parameters.rebuild` が拒否する。
    順序を間違えても他のverbだけのログは開けてしまうため、ここで固定する。
    """
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 5.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(10.0, mode="join")
    radius = next(name for name, value in session.document.parameters.items() if value == 5.0)
    session.set_parameter(radius, 7.0)
    volume = query.volume(session.document.bodies[-1].shape)
    session.save_document("with-parameters.json")

    fresh = Session(None, DEVIATION, models_dir=session.models_dir)
    fresh.open_document("with-parameters.json")

    assert query.volume(fresh.document.bodies[-1].shape) == pytest.approx(volume, rel=1e-9)
    assert fresh.document.parameters[radius] == pytest.approx(7.0)


def test_opening_rebuilds_the_same_shape_and_reaches_every_connection(session: Session) -> None:
    """**保存 → 開く → 同じ形**(タスクEの検証2)。開いた文書は全接続へ届く。

    形は操作ログから**再生される**ので、この検査は同時に「再生経路が壊れていないこと」の
    検査でもある(§3.1で保存形式をログにした理由そのもの)。
    """
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
    """開いた後の版は必ず上がる。

    Bodyのidは操作ログに記録され**再生で同じidが再現しうる**ので、版まで元へ戻すと、
    そのidを知っているクライアントが「変わっていない」と判断して前の文書の形を
    描き続ける(差分配信の前提が破れる)。
    """
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
    # **版は現行(2)にしておく** —— 1にすると「旧形式」で先に断られ、
    # このテストが見たい「未知のverbで再生が落ちる」経路まで届かない。
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
    """**今の文書と同じ名前を、2接続以上つながっている状態で開こうとしたら拒否する**(issue #16)。

    `open_document` はディスクから読み直して差し替えるので、素通しにすると
    別タブの未保存の編集が黙って消える。
    """
    _box(session)
    session.save_document("model.json")
    session.open_document("model.json")  # ディスクから開き直した後の文書を「今の文書」とする
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
    """**接続が1つだけ(別タブが無い)なら、開き直しは今までどおり通す**。

    「別タブ」が存在しない以上、issue #16の対象外——保存前に戻す手立てとして使われている。
    """
    _box(session)
    session.save_document("model.json")
    session.open_document("model.json")

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._dispatch(client, json.dumps({"cmd": "open_document", "name": "model.json"}))
        return client.payloads[-1]

    answer = asyncio.run(scenario())

    assert answer["type"] == "state"
    assert answer["applied"]["action"] == "open"


# --------------------------------------------------------------------------------------
# 新規(空の文書へ差し替える。`open_document` と同じ差し替えの経路)
# --------------------------------------------------------------------------------------
def test_new_document_empties_the_document_and_reaches_every_connection(session: Session) -> None:
    """**新規は空の文書へ差し替える**。開くのと同じく全接続へ届く。

    `name` / `path` は `None`(開いたファイルが無い)。画面はこれを見て「今どれを触って
    いるか」の表示を(未保存)へ戻す。
    """
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
    """新規は**操作ログに載らない**(差し替えであって操作ではない)。

    したがって**undoでは戻らない** —— 新しい文書のログは空で、取り消せる操作が1つも無い。
    捨てた文書を取り戻す手段は「保存してあれば開き直す」だけである。
    """
    _box(session)

    session.new_document()

    assert session.document.log.entries == []
    with pytest.raises(ValueError):
        session.undo()


def test_new_document_clears_the_selection(session: Session) -> None:
    """選択は `Document` が持つので、差し替えた時点で空になる。

    **面・辺・領域の添字は文書が変われば意味を失う**(形を変えるverbの後にリセットするのと
    同じ理由)。空にせず持ち越すと、次のverbが存在しないBodyを指す。
    """
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    assert session.document.selection.items

    session.new_document()

    assert session.document.selection.items == []


def test_new_document_is_not_exposed_to_ai(session: Session) -> None:
    """**AIには開けていない**(`save_document` / `open_document` / `list_documents` と同じ)。

    今の文書を丸ごと捨ててundoでも戻せない操作なので、捨てる判断は人がする。
    """
    from duocad import catalog

    assert "new_document" not in catalog.tool_names()
    assert "new_document" in catalog.websocket_command_names()


# --------------------------------------------------------------------------------------
# 一覧(要件3: 名前/更新日時/サイズ/操作数)
# --------------------------------------------------------------------------------------
def test_list_documents_reports_name_mtime_size_and_operation_count(session: Session) -> None:
    """`list_documents` が `models_dir` の中身を返す(名前/更新日時/サイズ/操作数)。"""
    _box(session)
    log_before = len(session.document.log.entries)
    session.save_document("a.json")
    session.set_parameter(next(iter(session.document.parameters)), 30.0)
    session.save_document("b.json")

    async def script(send):
        return await send({"cmd": "list_documents"})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "file" and answer["action"] == "list"
    by_name = {item["name"]: item for item in answer["files"]}
    assert set(by_name) == {"a.json", "b.json"}
    assert by_name["a.json"]["operations"] == log_before
    assert by_name["b.json"]["operations"] == log_before + 1
    assert by_name["a.json"]["size"] == (session.models_dir / "a.json").stat().st_size
    assert by_name["a.json"]["mtime"] > 0
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
    assert [item["name"] for item in answer["files"]] == ["good.json"]


def test_stl_export_honours_the_deviation(session: Session, tmp_path: Path) -> None:
    """**偏差を指定できる**(細かくすればファイルは必ず大きくなる。§7の実測と同じ性質)。

    **曲面を持つ形で測る**。立方体は平面だけなので、偏差をいくら細かくしても三角形は
    12枚のままでファイルサイズが1バイトも変わらない(立方体で書くと 684B == 684B のまま
    検査にならない)。STLの近似が効くのは曲面である。
    """
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 10.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(SIDE, mode="join")
    coarse = tmp_path / "coarse.stl"
    fine = tmp_path / "fine.stl"

    async def script(send):
        await send({"cmd": "export_stl", "path": str(coarse), "deviation": 0.2})
        return await send({"cmd": "export_stl", "path": str(fine), "deviation": 0.01})

    (answer, _clients) = _browser(session, script)

    assert answer["action"] == "export_stl" and answer["deviation"] == 0.01
    assert fine.stat().st_size > coarse.stat().st_size


def test_export_without_a_deviation_is_refused(session: Session, tmp_path: Path) -> None:
    """`deviation` は必須。既定で埋めると「粗いまま出した」が黙って通る。"""
    _box(session)

    async def script(send):
        return await send({"cmd": "export_stl", "path": str(tmp_path / "x.stl")})

    (answer, _clients) = _browser(session, script)
    assert answer["type"] == "error"


#--------------------------------------------------------------------------------------
# パネルが要る配信(パラメータ・履歴の位置)
# --------------------------------------------------------------------------------------
def test_named_values_are_published_and_drive_the_shape(session: Session) -> None:
    """**名前つきの値が配られ、打ち直すと形が追随する**(タスクEの検証1の実体)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 5.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(10.0, mode="join")
    body = session.document.bodies[-1]
    volume_before = query.volume(body.shape)

    published = session.state_meta()["parameters"]
    radius_name = next(
        name for name, value in published.items() if value == pytest.approx(5.0)
    )

    async def script(send):
        return await send({"cmd": "set_parameter", "name": radius_name, "value": 8.0, "client_op": 1})

    (answer, _clients) = _browser(session, script)

    assert answer["applied"]["verb"] == "set_parameter"
    assert session.state_meta()["parameters"][radius_name] == pytest.approx(8.0)
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


# --------------------------------------------------------------------------------------
# 自己完結: 保存ファイルは外部STEPに依存しない
# --------------------------------------------------------------------------------------
def _two_solid_step(tmp_path: Path) -> Path:
    """2ソリッド(体積1000と216)のSTEPを作って返す(`tests/test_io_step.py` と同じ作り)。"""
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
    """**読み込んだSTEPを改名しても、保存した文書は開ける**。

    STEP本文がログの中にあるので、開く側は外部ファイルを一切読まない。改名(=元の場所から
    ファイルが消える)を挟むのが要点 —— 挟まないと、パス依存が残っていても緑になってしまい、
    この検査は何も守らない。
    """
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
    """状態配信のログは**STEP本文を運ばない**(文字数だけを出す)。

    `state_meta()` は操作のたびにWebSocketで全部配られる。PCケースのSTEPは4.6MBあるので、
    素通しにすると1操作ごとに数MBが流れ、「27部品でも数KB」という前提が壊れる。
    履歴パネルが使うのは `source_name` と件数だけ。**保存(`to_json`)は本文を書く**ので、
    ここで伏せても自己完結性は変わらない。
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

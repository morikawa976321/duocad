"""E契約(サーバ側): ブラウザから**開く/保存/書き出し**を踏む口と、パネルが要る配信。

UI(パラメータ編集パネル・操作履歴パネル・ファイル操作)そのものは**`pytest` では
完了判定できない**(実機確認は `web/scripts/smoke-files.mjs`)。このファイルが固定するのは
サーバ側の3つ——

1. **ファイル操作の口**(`FILE_COMMANDS` + `open_document`)。保存形式は**操作ログのJSON**で、
   形状はそこから導出される(実装計画書§3.1「これが正」)
2. **`state.parameters`** — 名前つきの寸法値。打ち直しの対象そのもの
3. **`state.redo_count`** — 操作履歴パネルが「どこまで戻れるか」を出すための位置

——は機械で判定できる。とくに**保存→開く→同じ形**は、UIを介さなくても検査できるうえに
**再生経路が壊れていないことの検査そのもの**なので、ここが正になる。

固定する契約:

- 保存は操作ログのJSONを書き、**verbではない**(操作ログが伸びない)
- **開くと形が完全に戻る**(体積・面数・操作ログ・パラメータ)。開いた文書は**全接続へ**配る
- **開くのに失敗しても今の文書を壊さない**
- 版(revision)は開いた後に必ず上がる —— 同じidのBodyを知っているクライアントに
  古い形を描き続けさせない
- STEP/STL を書き出せ、**STEPは読み戻して体積が一致する**(相対許容差 1e-6。§7の訂正)
- STLは**偏差を指定できる**(細かくすると必ずファイルが大きくなる)
- `state.parameters` に名前つきの値が出て、`set_parameter` で形が追随する
- 親ディレクトリが無い保存先は**黙って作らない**
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import pytest

from duocad import kernel, query
from duocad.server import FILE_COMMANDS, Server, Session

DEVIATION = 0.2
SIDE = 20.0


@pytest.fixture()
def session() -> Session:
    return Session(None, DEVIATION)


def _select(session: Session, refs: list) -> None:
    """選択を `refs` で置き換える。**verbの対象集合はここで決まる**(引数ではない)。"""
    session.clear_selection()
    session.modify_selection(refs, [])


def _box(session: Session):
    """スケッチ→長方形→領域押し出しで 20mm の立方体を作る(D の `_box` と同じ手順)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude_regions(SIDE, mode="join")
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
    """保存は操作ログのJSONを書くだけ。**操作ログは伸びない**(文書を変えていないので)。"""
    _box(session)
    target = tmp_path / "model.json"
    log_before = len(session.document.log.entries)

    async def script(send):
        return await send({"cmd": "save_document", "path": str(target)})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "file" and answer["action"] == "save"
    assert Path(answer["path"]) == target
    assert answer["operations"] == log_before
    assert len(session.document.log.entries) == log_before
    assert json.loads(target.read_text(encoding="utf-8"))["operations"], "空のログを書いた"
    assert clients[1].payloads == [], "保存は状態配信ではない(要求した接続にだけ返る)"


def test_opening_replays_a_log_that_contains_set_parameter(
    session: Session, tmp_path: Path
) -> None:
    """**打ち直した寸法を含むログが開ける**。

    パラメータの再生は「その操作までを作り直す」ために文書のログを頭から読むので、
    **再生前にログを文書へ束ねていない**と `parameters.rebuild` が拒否する。
    順序を間違えても他のverbだけのログは開けてしまうため、ここで固定する。
    """
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 5.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude_regions(10.0, mode="join")
    radius = next(name for name, value in session.document.parameters.items() if value == 5.0)
    session.set_parameter(radius, 7.0)
    volume = query.volume(session.document.bodies[-1].shape)
    target = tmp_path / "with-parameters.json"
    session.save_document(target)

    fresh = Session(None, DEVIATION)
    fresh.open_document(target)

    assert query.volume(fresh.document.bodies[-1].shape) == pytest.approx(volume, rel=1e-9)
    assert fresh.document.parameters[radius] == pytest.approx(7.0)


def test_opening_rebuilds_the_same_shape_and_reaches_every_connection(
    session: Session, tmp_path: Path
) -> None:
    """**保存 → 開く → 同じ形**(タスクEの検証2)。開いた文書は全接続へ届く。

    形は操作ログから**再生される**ので、この検査は同時に「再生経路が壊れていないこと」の
    検査でもある(§3.1で保存形式をログにした理由そのもの)。
    """
    body, _ = _box(session)
    volume = query.volume(body.shape)
    faces = kernel.face_count(body.shape)
    target = tmp_path / "model.json"
    session.save_document(target)

    fresh = Session(None, DEVIATION)

    async def script(send):
        return await send({"cmd": "open_document", "path": str(target), "client_op": 3})

    (answer, clients) = _browser(fresh, script)

    assert answer["type"] == "state"
    assert answer["applied"]["change"] == "document"
    assert answer["applied"]["client_op"] == 3
    reopened = fresh.document.bodies[-1]
    assert kernel.face_count(reopened.shape) == faces
    assert query.volume(reopened.shape) == pytest.approx(volume, rel=1e-9)
    assert [entry.verb for entry in fresh.document.log.entries] == [
        entry.verb for entry in session.document.log.entries
    ]
    assert clients[1].payloads, "開いた文書が他の接続へ届いていない"


def test_opening_bumps_the_revision_even_when_the_body_id_repeats(
    session: Session, tmp_path: Path
) -> None:
    """開いた後の版は必ず上がる。

    Bodyのidは操作ログに記録され**再生で同じidが再現しうる**ので、版まで元へ戻すと、
    そのidを知っているクライアントが「変わっていない」と判断して前の文書の形を
    描き続ける(差分配信の前提が破れる)。
    """
    body, _ = _box(session)
    before = session.body_revision(body.id)
    target = tmp_path / "model.json"
    session.save_document(target)

    session.open_document(target)

    reopened = session.document.bodies[-1]
    assert session.body_revision(reopened.id) > before


def test_a_failed_open_leaves_the_current_document_alone(session: Session, tmp_path: Path) -> None:
    """**開くのに失敗しても今の文書を壊さない**(新しい文書の上で再生し切ってから差し替える)。"""
    body, _ = _box(session)
    volume = query.volume(body.shape)
    broken = tmp_path / "broken.json"
    broken.write_text('{"version": 1, "operations": [{"seq": 1, "verb": "no_such_verb"}]}', encoding="utf-8")

    with pytest.raises(Exception):
        session.open_document(broken)

    assert session.document.bodies[-1] is body
    assert query.volume(body.shape) == pytest.approx(volume)

    async def script(send):
        return await send({"cmd": "open_document", "path": str(tmp_path / "missing.json")})

    (answer, _clients) = _browser(session, script)
    assert answer["type"] == "error"
    assert query.volume(session.document.bodies[-1].shape) == pytest.approx(volume)


def test_saving_into_a_missing_directory_is_refused(session: Session, tmp_path: Path) -> None:
    """親ディレクトリを黙って作らない(打ち間違えたパスの下に木を作らない)。"""
    _box(session)
    with pytest.raises(ValueError):
        session.save_document(tmp_path / "no-such-dir" / "model.json")


# --------------------------------------------------------------------------------------
# 書き出し(STEP / STL)
# --------------------------------------------------------------------------------------
def test_a_step_round_trip_keeps_the_volume(session: Session, tmp_path: Path) -> None:
    """**STEPは読み戻して体積が一致する**(タスクEの検証4)。

    比較は**相対許容差 1e-6**。§7の訂正どおり `==` では成立しない(再読込で数値積分の
    順序が変わり、1ULP級の差が出る)。
    """
    body, _ = _box(session)
    volume = query.volume(body.shape)
    target = tmp_path / "out.step"

    async def script(send):
        return await send({"cmd": "export_step", "path": str(target)})

    (answer, _clients) = _browser(session, script)

    assert answer["type"] == "file" and answer["action"] == "export_step"
    assert answer["bytes"] > 0
    reread = kernel.read_step(str(target))
    assert query.volume(reread) == pytest.approx(volume, rel=1e-6)
    assert kernel.face_count(reread) == kernel.face_count(body.shape)


def test_stl_export_honours_the_deviation(session: Session, tmp_path: Path) -> None:
    """**偏差を指定できる**(細かくすればファイルは必ず大きくなる。§7の実測と同じ性質)。

    **曲面を持つ形で測る**。立方体は平面だけなので、偏差をいくら細かくしても三角形は
    12枚のままでファイルサイズが1バイトも変わらない(立方体で書くと 684B == 684B のまま
    検査にならない)。STLの近似が効くのは曲面である。
    """
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 10.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude_regions(SIDE, mode="join")
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


# --------------------------------------------------------------------------------------
# 参照部品(STEP読み込み → 編集可能にする)
# --------------------------------------------------------------------------------------
def test_the_browser_import_gives_editable_bodies_right_away(
    session: Session, tmp_path: Path
) -> None:
    """**画面から読み込んだSTEPは、追加操作なしに編集できる**。

    読み込みが参照ボディ(`kind="reference"`)だけを足す経路のままだと、編集するには
    `make_editable_copy` を1ボディずつ打つ必要がある —— PCケース27ソリッドなら27回。
    起動オプション `--step` を廃止する前提として、その非対称を無くすのがこの契約である。
    **操作ログは1行**(undo1回でまるごと消える)。
    """
    body, _ = _box(session)
    source = tmp_path / "part.step"
    session.export_step_file(source)
    log_before = len(session.document.log.entries)

    async def script(send):
        return await send({"cmd": "import_step_editable", "path": str(source), "client_op": 1})

    (imported, _clients) = _browser(session, script)

    applied = imported["applied"]
    assert applied["verb"] == "import_step_editable"
    assert len(session.document.log.entries) == log_before + 1, "読み込みは操作ログ1行"
    editable_ids = applied["body_ids"]
    assert editable_ids, "編集可能なボディを返していない"
    assert all(session.document.is_editable(body_id) for body_id in editable_ids)
    # 原本(参照ボディ)は隠したまま文書に残る
    references = [session.document.get_body(body_id) for body_id in applied["reference_ids"]]
    assert references and all(not reference.visible for reference in references)
    # **編集できる**ことを、実際にverbを通して確かめる(参照のままなら ValueError になる側)。
    copy_id = editable_ids[0]
    faces_before = kernel.face_count(session.document.get_body(copy_id).shape)
    _select(session, [{"body": copy_id, "kind": "edge", "index": 1}])
    session.fillet(1.0)
    assert kernel.face_count(session.document.get_body(copy_id).shape) > faces_before


def test_the_browser_can_still_make_an_editable_copy_of_a_reference(
    session: Session, tmp_path: Path
) -> None:
    """参照ボディだけを読む経路(`editable=False`)と、その続きの `make_editable_copy`。

    既定は編集可能側だが、参照だけ欲しい経路は残してある(コアのverb `import_step`)。
    UIの「編集用に複製」コマンドはこの続きに当たる。
    """
    body, _ = _box(session)
    source = tmp_path / "part.step"
    session.export_step_file(source)

    imported = session.import_step(source)  # 参照ボディだけを足す側
    reference = imported["body_ids"][0]

    async def script(send):
        return await send(
            {"cmd": "make_editable_copy", "source_body_id": reference, "client_op": 2}
        )

    (editable, _clients) = _browser(session, script)

    assert editable["applied"]["verb"] == "make_editable_copy"
    copy_id = editable["applied"]["body_id"]
    assert session.document.is_editable(copy_id)
    assert not session.document.is_editable(reference)


#--------------------------------------------------------------------------------------
# パネルが要る配信(パラメータ・履歴の位置)
# --------------------------------------------------------------------------------------
def test_named_values_are_published_and_drive_the_shape(session: Session) -> None:
    """**名前つきの値が配られ、打ち直すと形が追随する**(タスクEの検証1の実体)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 5.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude_regions(10.0, mode="join")
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


def test_a_pattern_of_holes_follows_one_named_value(session: Session) -> None:
    """**1箇所直すと反復で作った穴が全部追随する**(タスクEの検証1・M3の存在理由)。"""
    plate_sketch = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(plate_sketch, [0.0, 0.0], 60.0, 20.0)
    _select(session, [{"sketch": plate_sketch, "index": 1}])
    session.extrude_regions(5.0, mode="join")
    body = session.document.bodies[-1]

    holes = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_circle(holes, [10.0, 10.0], 2.0)["entity_id"]
    session.pattern_rectangular(holes, [entity_id], 15.0, 0.0, 3, 1)
    regions = [
        {"sketch": holes, "index": item["index"]}
        for item in session.state_meta()["sketches"][-1]["regions"]
    ]
    assert len(regions) == 3, "反復した3つの領域ができていない"
    _select(session, regions)
    session.extrude_regions(5.0, mode="cut")
    volume_small = query.volume(body.shape)

    published = session.state_meta()["parameters"]
    radius_name = next(name for name, value in published.items() if value == pytest.approx(2.0))
    session.set_parameter(radius_name, 3.0)

    volume_large = query.volume(session.document.bodies[-1].shape)
    removed_before = 3 * math.pi * 2.0 * 2.0 * 5.0
    removed_after = 3 * math.pi * 3.0 * 3.0 * 5.0
    assert volume_small - volume_large == pytest.approx(removed_after - removed_before, rel=1e-3)


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

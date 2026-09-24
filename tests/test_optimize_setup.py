"""形状最適化(第3のモード)の条件を置く4 verb の契約。

固定するのは6つ。**どれも、外れても例外が出ないまま静かに間違う**種類のずれである:

1. **集合代入** —— 「1つ足す」ではなく「その役割の集合を、確定選択そのものに置き換える」。
   **空の確定選択も正当な入力**で、その役割を全部外すことを意味する(だから解除専用の
   verbが要らない)。
2. **排他は先勝ち** —— 既に相手の役割が付いている対象を割り当てようとしたら失敗し、
   **どれが衝突したかを名指しする**。失敗した側は文書にもログにも何も残さない。
3. **指紋の不一致** —— 面の参照は添字だけでは足りない。後から形を変えて添字が別の面を
   指すようになったら、その条件は**使わずに** `stale` として名指しする(壊れないように
   するのではなく、壊れたと気づけるようにする)。
4. **保存 → 再オープンの往復** —— 条件は操作ログに載るので、`to_json` → `from_json` →
   `replay` で完全に戻る。
5. **undo / redo** —— 直前の集合へ戻り、やり直せる。
6. **3経路** —— コア(`Session`)・WebSocket・MCPのどこから踏んでも同じ結果になる
   (宣言は `duocad/catalog.py` の1箇所が正)。

**選択種別 `body` の新設**もここで押さえる —— ソリッドを丸ごと指す手段が無いと、
保持/除外ソリッドの対象を確定選択から取れない。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from duocad import optimize, selection
from duocad.document import Document
from duocad.mcp_server import DocumentTools, McpServer
from duocad.oplog import OpLog
from duocad.server import Server, Session

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_TS = REPO_ROOT / "web" / "src" / "session.ts"
COMMAND_TS = REPO_ROOT / "web" / "src" / "commands" / "command.ts"

DEVIATION = 0.2
SIDE = 20.0


@pytest.fixture()
def session() -> Session:
    return Session(None, DEVIATION)


def _select(session: Session, refs: list) -> None:
    """選択を `refs` で置き換える(人がクリックで選び直すのと同じ入口)。"""
    session.clear_selection()
    session.modify_selection(refs, [])


def _box(session: Session, corner=(0.0, 0.0)):
    """スケッチ→長方形→領域押し出しで 20mm の立方体を1つ作り、そのBodyを返す。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, list(corner), SIDE, SIDE)
    regions = len(session.state_meta()["sketches"][-1]["regions"])
    _select(session, [{"sketch": sketch_id, "index": regions}])
    session.extrude(SIDE, mode="new")
    session.clear_selection()
    return session.document.bodies[-1]


def _body_refs(*bodies) -> list:
    return [{"body": body.id, "kind": "body"} for body in bodies]


def _preserve(session: Session, *bodies) -> None:
    """条件面を置く前に、そのボディを保持ソリッドにする。

    **拘束面・荷重面は保持ソリッドの面にしか置けない**(2026-09-18〜)ので、面を指す前に通す。
    保持は集合代入なので、既に保持のボディも一緒に入れ直す。
    """
    kept = list(session.document.optimize.preserve_bodies)
    refs = [{"body": body_id, "kind": "body"} for body_id in kept]
    refs += [ref for ref in _body_refs(*bodies) if ref["body"] not in kept]
    _select(session, refs)
    session.set_preserve_bodies()
    session.clear_selection()


def _face_refs(body, indices) -> list:
    return [{"body": body.id, "kind": "face", "index": index} for index in indices]


def _ids(refs: list) -> list:
    return [ref["body"] for ref in refs]


def _indices(refs: list) -> list:
    return [ref["index"] for ref in refs]


def _optimize(session: Session) -> dict:
    return session.state_meta()["optimize"]


class FakeConnection:
    """`Server` が使うのは `send` だけ(`tests/test_feature_ui.py` と同じ形)。"""

    def __init__(self) -> None:
        self.payloads: list = []

    async def send(self, raw: str) -> None:
        self.payloads.append(json.loads(raw))


def _browser(session: Session, script):
    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)

        async def send(command: dict) -> dict:
            await server._dispatch(client, json.dumps(command))
            return client.payloads[-1]

        return await script(send)

    return asyncio.run(scenario())


# --------------------------------------------------------------------------------------
# 1. 選択種別 `body`
# --------------------------------------------------------------------------------------
def test_a_body_can_be_selected_whole(session: Session) -> None:
    """**ソリッドを丸ごと指せる**(面を全部入れる案は採っていない)。

    落ちるのは「`kind='body'` の参照が選択機構を通らない」とき。保持/除外ソリッドは
    確定選択から対象を取るので、通らなければverb自体が使えない。
    """
    body = _box(session)
    ref = selection.select_body(session.document, body.id)
    assert ref == {"body": body.id, "kind": "body", "index": None, "provenance": None}
    assert selection.ref_kind(ref) == selection.BODY_KIND

    _select(session, _body_refs(body))
    items = session.get_selection()["items"]
    # **面が展開されて入っていない**(2835面のボディで選択集合が膨れる案を採らないこと)
    assert items == [ref], items
    assert selection.require_committed_bodies(session.document) == [body.id]


def test_the_same_body_is_not_selected_twice(session: Session) -> None:
    """同じボディを2回 add しても重複しない(等価判定が添字を見ていないこと)。"""
    body = _box(session)
    session.clear_selection()
    session.modify_selection(_body_refs(body), [])
    session.modify_selection(_body_refs(body), [])
    assert len(session.get_selection()["items"]) == 1
    session.modify_selection([], _body_refs(body))
    assert session.get_selection()["items"] == []


# --------------------------------------------------------------------------------------
# 2. 集合代入
# --------------------------------------------------------------------------------------
def test_assigning_bodies_replaces_the_whole_set(session: Session) -> None:
    """**置き換えであって追加ではない。** 2回目の確定で1回目の中身が消える。

    ここが「足す」に変わると、verbの中で外したはずのものが役割に残り続ける ——
    解除専用のverbを作らない設計の前提そのものが崩れる。
    """
    first = _box(session)
    second = _box(session, corner=(50.0, 0.0))

    _select(session, _body_refs(first))
    session.set_preserve_bodies()
    assert _ids(_optimize(session)["preserve_bodies"]) == [first.id]

    _select(session, _body_refs(second))
    session.set_preserve_bodies()
    assert _ids(_optimize(session)["preserve_bodies"]) == [second.id]


def test_an_empty_selection_clears_the_role(session: Session) -> None:
    """**空の確定選択で確定すると、その役割が空になる**(解除専用のverbを作らない根拠)。

    ここが `SelectionEmpty` で拒まれると、一度付けた役割を外す手段がどこにも無くなる。
    """
    body = _box(session)
    _select(session, _body_refs(body))
    session.set_preserve_bodies()
    session.clear_selection()

    session.set_preserve_bodies()
    assert _optimize(session)["preserve_bodies"] == []
    # **ログには載る**(undoで戻せる1手である)
    assert session.document.log.entries[-1].verb == "set_preserve_bodies"


def test_assigning_faces_replaces_the_whole_set(session: Session) -> None:
    """面の側も集合代入(拘束・荷重とも)。"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1, 2]))
    session.set_fixtures()
    assert _indices(_optimize(session)["fixtures"]) == [1, 2]

    _select(session, _face_refs(body, [3]))
    session.set_fixtures()
    assert _indices(_optimize(session)["fixtures"]) == [3]


def test_a_proposed_selection_is_committed_by_running_the_verb(session: Session) -> None:
    """AIの提案のままでも、**人がverbを実行する行為そのものが承認**になる(WS経由)。

    宣言(`uses_selection`)と実体(`require_*` を呼ぶ)が揃っていないと、この自動確定から
    静かに漏れる —— 漏れると、AIが提案した状態で人がボタンを押したときだけ詰む。
    """
    body = _box(session)
    session.propose_selection(refs=_body_refs(body))
    assert session.get_selection()["origin"] == "proposed"

    async def script(send):
        return await send({"cmd": "set_preserve_bodies"})

    answer = _browser(session, script)
    assert answer["type"] == "state", answer
    assert session.get_selection()["origin"] == "user"
    assert _ids(_optimize(session)["preserve_bodies"]) == [body.id]


# --------------------------------------------------------------------------------------
# 3. 排他は先勝ち
# --------------------------------------------------------------------------------------
def test_a_preserved_body_cannot_be_excluded(session: Session) -> None:
    """**先勝ち**。しかも**どのボディが衝突したかを名指しする**(画面がトーストに出す)。

    失敗した側は文書にもログにも何も残さない —— 半分だけ割り当たると、画面の色と
    実際の割り当てが食い違ったまま気づけない。
    """
    body = _box(session)
    _select(session, _body_refs(body))
    session.set_preserve_bodies()

    before = len(session.document.log.entries)
    with pytest.raises(optimize.RoleConflict) as caught:
        session.set_exclude_bodies()

    assert [item["body"] for item in caught.value.conflicts] == [body.id]
    assert body.id in str(caught.value), caught.value
    assert len(session.document.log.entries) == before, "失敗したverbがログに残っている"
    assert _optimize(session)["exclude_bodies"] == []
    assert _ids(_optimize(session)["preserve_bodies"]) == [body.id]


def test_the_exclusion_is_symmetric(session: Session) -> None:
    """逆向き(除外 → 保持)も同じ。片方だけ守ると、順序で結果が変わる。"""
    body = _box(session)
    _select(session, _body_refs(body))
    session.set_exclude_bodies()
    with pytest.raises(optimize.RoleConflict):
        session.set_preserve_bodies()


def test_moving_a_body_between_roles_takes_two_steps(session: Session) -> None:
    """移すには「元の役割で外す → 新しい役割で付ける」の2手(**意図した仕様**)。"""
    body = _box(session)
    _select(session, _body_refs(body))
    session.set_preserve_bodies()

    session.clear_selection()
    session.set_preserve_bodies()  # 1手目: 元の役割から外す
    _select(session, _body_refs(body))
    session.set_exclude_bodies()  # 2手目: 新しい役割へ付ける

    state = _optimize(session)
    assert _ids(state["exclude_bodies"]) == [body.id]
    assert state["preserve_bodies"] == []


def test_a_fixed_face_cannot_be_loaded(session: Session) -> None:
    """面の側も先勝ち。**衝突した面を添字で名指しする**。"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1, 2]))
    session.set_fixtures()

    _select(session, _face_refs(body, [2, 3]))
    with pytest.raises(optimize.RoleConflict) as caught:
        session.set_loads(100.0, [0.0, 0.0, -1.0])
    assert [item["index"] for item in caught.value.conflicts] == [2]
    assert _optimize(session)["loads"] == []


def test_the_conflict_is_an_invalid_argument_over_mcp(session: Session) -> None:
    """MCPから見ると `invalid_argument`(**未知のコードを勝手に作らない**)。"""
    body = _box(session)
    tools = DocumentTools(session)
    _select(session, _body_refs(body))
    tools.set_preserve_bodies()

    with pytest.raises(optimize.RoleConflict) as caught:
        tools.set_exclude_bodies()
    envelope = tools.error_envelope("set_exclude_bodies", caught.value)
    assert envelope["code"] == "invalid_argument", envelope


# --------------------------------------------------------------------------------------
# 4. 荷重の how(大きさと向き)
# --------------------------------------------------------------------------------------
def test_a_load_direction_can_be_a_world_vector(session: Session) -> None:
    """**面法線だけでは足りない**(棚受けは世界の -Z へ受ける)。ベクトルは単位化して控える。"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    applied = session.set_loads(250.0, [0.0, 0.0, -4.0])

    assert applied["direction"] == "vector"
    assert applied["direction_vector"] == [0.0, 0.0, -1.0], "向きを単位化していない"
    specs = _optimize(session)["load_specs"]
    assert [
        (item["magnitude"], item["direction"], item["vector"]) for item in specs
    ] == [(250.0, "vector", [0.0, 0.0, -1.0])]


def test_a_load_direction_can_follow_the_face_normal(session: Session) -> None:
    """面法線とその逆向きも取れる(`vector` は持たない)。"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_loads(10.0, "-normal")
    specs = _optimize(session)["load_specs"]
    assert [(item["magnitude"], item["direction"], item["vector"]) for item in specs] == [
        (10.0, "-normal", None)
    ]


@pytest.mark.parametrize(
    "magnitude, direction",
    [
        (0.0, [0.0, 0.0, -1.0]),          # 大きさ0は「荷重が無い」ことを表さない
        (-5.0, [0.0, 0.0, -1.0]),
        (10.0, [0.0, 0.0, 0.0]),          # 長さ0のベクトルは向きを持たない
        (10.0, "sideways"),               # 知らないキーワードを黙って通さない
        (10.0, [0.0, 0.0]),               # 3成分でない
    ],
)
def test_a_bad_load_is_refused_without_touching_the_document(
    session: Session, magnitude, direction
) -> None:
    """**不正なhowは文書にもログにも何も残さない。**"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    before = len(session.document.log.entries)
    with pytest.raises(ValueError):
        session.set_loads(magnitude, direction)
    assert len(session.document.log.entries) == before
    assert _optimize(session)["loads"] == []


# --------------------------------------------------------------------------------------
# 5. 面の指紋
# --------------------------------------------------------------------------------------
def test_a_face_condition_carries_a_fingerprint(session: Session) -> None:
    """条件には**添字だけでなく重心・法線・面積**が焼き込まれている(操作ログにも載る)。"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_fixtures()

    stored = session.document.log.entries[-1].args["faces"][0]
    assert set(stored) == {"body", "index", "area", "normal", "center"}
    assert stored["area"] == pytest.approx(SIDE * SIDE)


def test_a_face_that_changed_is_reported_and_not_used(session: Session) -> None:
    """**形が変われば、その条件は使われず `stale` に名指しで出る。**

    ここが効かないと、拘束のつもりで指した面が別の場所へ移ったまま最適化へ渡る ——
    例外も出ないので、結果を見るまで誰も気づかない。

    形の変え方に辺1のフィレットを使うのは**検出を決定的にするため**である。実測で、
    面1は同じ面のまま面積が 400→360 に減り(面積で捕まる)、面2は法線が
    (0,1,0)→(0,0,-1) に変わる(=添字が別の面へ移った。これが指紋の本来の狙い)。
    """
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_fixtures()
    assert _optimize(session)["stale"] == []

    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    session.fillet(2.0)

    state = _optimize(session)
    assert state["fixtures"] == [], "指紋が合わないのに条件を使っている"
    assert len(state["stale"]) == 1
    stale = state["stale"][0]
    assert stale["role"] == "fixtures" and stale["index"] == 1
    assert stale["code"] == "fingerprint", stale
    assert stale["reason"], "**理由が空だと画面がトーストに出せない**"


def test_moving_a_body_does_not_invalidate_its_face_conditions(session: Session) -> None:
    """**平行移動では条件が外れない**。

    `move_body` は面の添字を変えず、指した面は同じ面のままなので、外れるのは誤検出である
    (指紋の一致判定が重心を見ない理由は `duocad/optimize.py` の
    `fingerprint_mismatch` のdocstringが正)。
    """
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_fixtures()

    _select(session, _face_refs(body, [1]))
    session.move_body(0.0, 0.0, 30.0)

    state = _optimize(session)
    assert state["stale"] == [], "平行移動しただけで条件が外れている"
    assert _indices(state["fixtures"]) == [1]


def test_a_stale_condition_comes_back_when_the_shape_comes_back(session: Session) -> None:
    """指紋が合わない条件は**文書からは消えない** —— 形を戻せばまた使われる。

    配信のときに落としているだけで、削除していないこと(操作ログが唯一の正)。
    """
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_fixtures()

    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    session.fillet(2.0)
    assert _optimize(session)["fixtures"] == []
    session.undo()

    state = _optimize(session)
    assert state["stale"] == []
    assert _indices(state["fixtures"]) == [1]


def test_a_hidden_body_drops_out_of_the_refs_but_says_why(session: Session) -> None:
    """**見えないものは指せない**ので、隠したボディの割り当ては配る参照から外れる。

    外すだけだと画面から黙って消えるので、`stale` に `code="hidden"` で出す ——
    人が部品を隠すたびに出る種類なので、**トーストに出す側ではない**(だからコードで
    区別できるようにしてある)。表示に戻せばそのまま戻る。
    """
    body = _box(session)
    _select(session, _body_refs(body))
    session.set_preserve_bodies()
    _select(session, _face_refs(body, [1]))
    session.set_fixtures()

    session.set_visible(body.id, False)
    state = _optimize(session)
    assert state["preserve_bodies"] == [] and state["fixtures"] == []
    assert sorted(item["code"] for item in state["stale"]) == ["hidden", "hidden"]
    # **可視性は形状の同一性を動かさない** —— 増分維持の鍵に入れ忘れると、隠しても
    # 前の配りものがそのまま返り続けてここが落ちる。

    session.set_visible(body.id, True)
    state = _optimize(session)
    assert _ids(state["preserve_bodies"]) == [body.id]
    assert _indices(state["fixtures"]) == [1]
    assert state["stale"] == []


def test_a_body_that_undo_removed_is_reported_as_missing(session: Session) -> None:
    """作成ごとundoで消えたボディも、配る参照から外して理由を出す。"""
    body = _box(session)
    _select(session, _body_refs(body))
    session.set_preserve_bodies()

    # 割り当て → ボディの作成 の順にundoすると、割り当てだけが残る形を作れる
    log = session.document.log
    log.undo(session.document)  # set_preserve_bodies
    redo_target = session.document.optimize.revision
    log.undo(session.document)  # extrude(ボディが消える)
    session.document.optimize.preserve_bodies = [body.id]
    session.document.optimize.touch()
    assert session.document.optimize.revision > redo_target

    state = _optimize(session)
    assert state["preserve_bodies"] == []
    assert [item["code"] for item in state["stale"]] == ["missing"]


def test_the_fingerprint_check_names_what_changed(session: Session) -> None:
    """照合そのもの(面積が違えば不一致・同じなら一致)。"""
    body = _box(session)
    condition = optimize.face_condition(
        session.document, {"body": body.id, "kind": "face", "index": 1}
    )
    assert optimize.fingerprint_mismatch(session.document, condition) is None

    tampered = dict(condition, area=condition["area"] * 2.0)
    reason = optimize.fingerprint_mismatch(session.document, tampered)
    assert reason is not None and "面積" in reason, reason


# --------------------------------------------------------------------------------------
# 6. 保存 → 再オープンの往復 / undo / redo
# --------------------------------------------------------------------------------------
def test_the_setup_survives_a_save_and_reopen(session: Session) -> None:
    """**保存の実体は操作ログのJSON**なので、条件も往復する。

    落ちるのは「条件をログではなくセッション状態に置いた」とき —— 開き直すと黙って消える。
    """
    body = _box(session)
    _select(session, _body_refs(body))
    session.set_preserve_bodies()
    _select(session, _face_refs(body, [1, 2]))
    session.set_fixtures()
    _select(session, _face_refs(body, [3]))
    session.set_loads(120.0, [1.0, 0.0, 0.0])
    before = _optimize(session)

    reopened = Document()
    OpLog.from_json(session.document.log.to_json()).replay(reopened)

    assert optimize.state_payload(reopened) == before


def test_the_setup_survives_a_real_save_and_open(tmp_path) -> None:
    """落ちたら、`save_document` → `new_document` → `open_document` の実APIの往復(ファイルI/Oと
    文書の差し替えを通る)で条件が戻らないか、差し替え後に前の文書の割り当てを配っている。"""
    made = Session(None, DEVIATION, models_dir=tmp_path / "models")
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
    made = Session(None, DEVIATION, models_dir=tmp_path / "models")
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


def test_undo_and_redo_walk_the_assignment_back(session: Session) -> None:
    """undoは**直前の集合**へ戻り、redoでやり直せる(集合代入なので差分ではない)。"""
    first = _box(session)
    second = _box(session, corner=(50.0, 0.0))

    _select(session, _body_refs(first))
    session.set_preserve_bodies()
    _select(session, _body_refs(second))
    session.set_preserve_bodies()
    assert _ids(_optimize(session)["preserve_bodies"]) == [second.id]

    session.undo()
    assert _ids(_optimize(session)["preserve_bodies"]) == [first.id]
    session.undo()
    assert _optimize(session)["preserve_bodies"] == []
    session.redo()
    assert _ids(_optimize(session)["preserve_bodies"]) == [first.id]
    session.redo()
    assert _ids(_optimize(session)["preserve_bodies"]) == [second.id]


def test_redo_never_hits_the_exclusion_it_already_passed(session: Session) -> None:
    """**戻してからやり直しても排他に当たらない**(やり直せない履歴を作らない)。

    undoは排他の検査を通さず「以前に成立していた集合」へ戻すだけで、redoは元と同じ順序で
    `_apply_*` を通す。したがってredoの時点で相手の役割が持っている中身は、最初に通った
    ときと同じである —— ここが崩れると、**戻した後にやり直せない履歴**ができて、例外が
    出るのは redo を押した瞬間になる(何が悪いのか人には見えない)。

    役割をまたぐ2手(外す → 付ける)を挟むのは、そこが唯一「相手の役割が動いた状態で
    同じボディを割り当て直す」形になるからである。
    """
    body = _box(session)
    _select(session, _body_refs(body))
    session.set_preserve_bodies()
    session.clear_selection()
    session.set_preserve_bodies()  # 保持から外す
    _select(session, _body_refs(body))
    session.set_exclude_bodies()  # 除外へ付け直す

    session.undo()
    session.undo()
    assert _ids(_optimize(session)["preserve_bodies"]) == [body.id]

    session.redo()
    session.redo()  # ここで RoleConflict が出たら履歴が壊れている
    state = _optimize(session)
    assert _ids(state["exclude_bodies"]) == [body.id]
    assert state["preserve_bodies"] == []


def test_a_new_verb_after_undo_drops_the_redo_tail(session: Session) -> None:
    """undoの後に別のverbを打つと、やり直せた末尾は捨てられる(`OpLog.append` の規律)。

    捨て損なうと、**同じ役割への代入が2本の枝に残って**redoで思わぬ集合が戻る。
    """
    first = _box(session)
    second = _box(session, corner=(50.0, 0.0))

    _select(session, _body_refs(first))
    session.set_preserve_bodies()
    session.undo()

    _select(session, _body_refs(second))
    session.set_exclude_bodies()
    assert not session.document.log.can_redo, "捨てられていない枝が残っている"
    state = _optimize(session)
    assert state["preserve_bodies"] == []
    assert _ids(state["exclude_bodies"]) == [second.id]


def test_undoing_a_load_restores_the_previous_how(session: Session) -> None:
    """荷重は集合だけでなく**面ごとの大きさ・向きも**直前へ戻る。"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_loads(100.0, [0.0, 0.0, -1.0])
    _select(session, _face_refs(body, [2]))
    session.set_loads(300.0, "normal")
    session.undo()

    specs = _optimize(session)["load_specs"]
    assert [(item["index"], item["magnitude"], item["direction"]) for item in specs] == [
        (1, 100.0, "vector")
    ]


def test_the_load_how_survives_hiding_the_body(session: Session) -> None:
    """**`load_specs` は文書が持つ how** で、面が一時的に使えなくなっても消えない。

    隠しただけで大きさと向きが画面から消えると、表示に戻したときに打ち直しになる。
    「いま荷重がかかる面」は `loads` の側が持つ。
    """
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_loads(400.0, "normal")
    session.set_visible(body.id, False)

    state = _optimize(session)
    assert state["loads"] == []
    assert [item["magnitude"] for item in state["load_specs"]] == [400.0]


def test_undoing_the_first_assignment_leaves_no_load(session: Session) -> None:
    """最初の割り当てをundoすると荷重は**無い**(0Nの荷重が残らない)。"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_loads(100.0, "normal")
    session.undo()
    assert _optimize(session)["load_specs"] == []
    assert _optimize(session)["loads"] == []


def _specs(session: Session) -> list:
    """`(面添字, 大きさ, 向き)` の並び(荷重の how を面ごとに読む)。"""
    return [
        (item["index"], item["magnitude"], item["direction"])
        for item in _optimize(session)["load_specs"]
    ]


def test_a_second_load_does_not_overwrite_the_first(session: Session) -> None:
    """**上書き代入**: 選択に入っていない荷重面は触られない。

    ここが落ちると「面Bの荷重を置いたら面Aの荷重が消えた/同じ値になった」になる。
    集合代入に戻すと必ず落ちる検査である。
    """
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_loads(50.0, [0.0, 0.0, -1.0])
    _select(session, _face_refs(body, [2]))
    session.set_loads(20.0, "normal")

    assert _specs(session) == [(1, 50.0, "vector"), (2, 20.0, "normal")]


def test_reassigning_a_face_replaces_only_that_face(session: Session) -> None:
    """同じ面をもう一度指すと、**その面だけ**新しい値になる(並びも動かない)。"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_loads(50.0, [0.0, 0.0, -1.0])
    _select(session, _face_refs(body, [2]))
    session.set_loads(20.0, "normal")
    _select(session, _face_refs(body, [1]))
    session.set_loads(70.0, "normal")

    assert _specs(session) == [(1, 70.0, "normal"), (2, 20.0, "normal")]


def test_setting_a_load_with_no_face_selected_is_refused(session: Session) -> None:
    """**0枚での確定は失敗**(「空で確定 = 全部外れる」と取り違えさせない)。

    上書き代入なので0枚は本来なにも起きない。黙って通すと、外したつもりの人が
    外れていないまま実行まで進む。
    """
    _box(session)
    session.clear_selection()
    with pytest.raises(ValueError, match="1枚も選ばれていない"):
        session.set_loads(50.0, "normal")


def test_clear_loads_drops_only_the_selected_faces(session: Session) -> None:
    """`clear_loads` は選んだ面だけ外す。空の選択なら全部外す。"""
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_loads(50.0, "normal")
    _select(session, _face_refs(body, [2]))
    session.set_loads(20.0, "normal")

    _select(session, _face_refs(body, [1]))
    session.clear_loads()
    assert _specs(session) == [(2, 20.0, "normal")]

    session.clear_selection()
    session.clear_loads()
    assert _specs(session) == []


def test_clearing_a_face_that_has_no_load_is_not_an_error(session: Session) -> None:
    """外れている面を混ぜても落ちない(**「外す」は結果を言っている**)。

    ここで例外を出すと、複数選択のうち1枚だけ既に外れていたときに全部落ちる。
    """
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1]))
    session.set_loads(50.0, "normal")
    _select(session, _face_refs(body, [1, 2]))
    session.clear_loads()
    assert _specs(session) == []


# --------------------------------------------------------------------------------------
# 7. 3経路(コア / WebSocket / MCP)と状態の配信
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "verb", ["set_preserve_bodies", "set_exclude_bodies", "set_fixtures", "set_loads"]
)
def test_every_verb_is_reachable_from_all_three_surfaces(verb: str) -> None:
    """**宣言は1箇所**(`catalog.py`)で、WSの表もMCPのツールもそこからの導出である。"""
    from duocad import catalog
    from duocad.server import FEATURE_COMMANDS

    command = catalog.BY_NAME[verb]
    assert command.uses_selection, f"{verb} が自動確定の対象から漏れている"
    assert not command.resets_selection, "形を変えるverbではないので選択をリセットしない"
    assert verb in FEATURE_COMMANDS, "WebSocketの表に無い"
    assert verb in catalog.tool_names(), "MCPのツールに無い"


#: verb名 -> (対象の選び方, WSへ送る引数, MCPラッパへ渡す引数, 結果を確かめる読み取り)。
#:
#: **4 verb 全部をWSとMCPの両方で実際に走らせる**ための表。宣言の照合(下の
#: `test_every_verb_is_reachable_from_all_three_surfaces`)だけでは、
#: **引数の受け渡しが繋がっているか**は分からない —— WSの表は
#: `**{key: message[key] for key in keys}` で機械的に渡すので、宣言の引数名と
#: `Session` のラッパの引数名が食い違えば `TypeError` になり、MCPのラッパは手書きなので
#: 綴りを間違えれば同じく落ちる。どちらも**その経路を1回走らせるまで誰も気づかない**。
#: 読み取りは**セッションに依らない値**へ均す(`body_id` は毎回振り直されるので、
#: 「選んだボディか」に置き換えてから2経路を突き合わせる)。
def _assigned_bodies(role):
    return lambda state, body: [
        "<選んだボディ>" if item == body.id else item for item in _ids(state[role])
    ]


def _assigned_faces(role):
    return lambda state, body: _indices(state[role])


_ROUTES = {
    "set_preserve_bodies": ("body", {}, _assigned_bodies("preserve_bodies")),
    "set_exclude_bodies": ("body", {}, _assigned_bodies("exclude_bodies")),
    "set_fixtures": ("face", {}, _assigned_faces("fixtures")),
    # 荷重だけは**howの値そのもの**も突き合わせる(面の集合だけ見ていると、
    # 大きさ・向きが片方の経路で落ちていても緑になる)。
    "set_loads": (
        "face",
        {"magnitude": 50.0, "direction": [0.0, 1.0, 0.0]},
        lambda state, body: (
            _indices(state["loads"]),
            [item["magnitude"] for item in state["load_specs"]],
            [item["direction"] for item in state["load_specs"]],
            [item["vector"] for item in state["load_specs"]],
        ),
    ),
}


@pytest.mark.parametrize("verb", sorted(_ROUTES))
def test_the_websocket_and_mcp_paths_both_run_the_verb(verb: str) -> None:
    """**4 verb とも、WS経由とMCP経由で実際に走って同じ結果になる。**

    宣言の照合ではなく**実行**で見る(引数の受け渡しは走らせないと繋がりが分からない)。
    同じ選択に対して2経路で走らせ、**どちらも同じものを割り当てる**ことまで確かめる ——
    片方だけが対象を取り違えていると、AIと人で結果が食い違う形で出る。
    """
    picker, arguments, read = _ROUTES[verb]

    def run(use_websocket: bool):
        made = Session(None, DEVIATION)
        body = _box(made)
        if picker == "face":
            _preserve(made, body)
        _select(
            made,
            _body_refs(body) if picker == "body" else _face_refs(body, [1, 2]),
        )
        if use_websocket:

            async def script(send):
                return await send({"cmd": verb, **arguments})

            answer = _browser(made, script)
            assert answer["type"] == "state", answer
        else:
            getattr(DocumentTools(made), verb)(**arguments)
        assert made.document.log.entries[-1].verb == verb, "操作ログに載っていない"
        return read(_optimize(made), body)

    over_ws = run(True)
    over_mcp = run(False)
    assert over_ws == over_mcp, {"WS": over_ws, "MCP": over_mcp}
    assert over_ws, "対象を1つも割り当てていない(確定選択から取れていない)"
    if verb == "set_loads":
        assert over_ws == (
            [1, 2],
            [50.0, 50.0],
            ["vector", "vector"],
            [[0.0, 1.0, 0.0], [0.0, 1.0, 0.0]],
        ), over_ws


def test_the_mcp_schema_takes_only_the_how(session: Session) -> None:
    """MCPのスキーマに**対象集合が現れない**(面もボディも確定選択から取る)。"""
    server = McpServer(DocumentTools(session))
    tools = {
        tool["name"]: tool["inputSchema"]
        for tool in server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
    }
    assert tools["set_preserve_bodies"]["properties"] == {}
    assert tools["set_fixtures"]["properties"] == {}
    assert set(tools["set_loads"]["properties"]) == {"magnitude", "direction"}


def test_the_delivered_refs_can_be_fed_straight_back_into_the_selection(
    session: Session,
) -> None:
    """**配る参照はそのまま `modify_selection` へ渡せる形**である。

    画面はverbを起動したときに、既にその役割が付いている対象をこれでプリセレクトする ——
    形が違うと画面側にもう1つ変換が生まれる。
    """
    body = _box(session)
    _select(session, _body_refs(body))
    session.set_preserve_bodies()
    _select(session, _face_refs(body, [1, 2]))
    session.set_fixtures()

    state = _optimize(session)
    session.clear_selection()
    session.modify_selection(state["preserve_bodies"] + state["fixtures"], [])
    assert session.get_selection()["count"] == 3
    assert selection.require_committed_bodies(session.document) == [body.id]
    assert len(selection.require_committed_faces(session.document)) == 2


def test_the_state_is_not_rebuilt_on_every_broadcast(session: Session) -> None:
    """**配信のたびに全ボディを走査しない**。

    照合が要るのは条件が指しているボディだけで、しかもその形が差し替わったときだけ。
    ここが緩むと、面を何枚か指しただけで**全配信に指紋の測り直しの税**がかかる。
    """
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
        session.extrude_face(3.0)
        session.state_meta()
        assert calls == [], f"無関係なボディの変更で測り直している: {calls}"

        # 条件が指しているボディの形が変われば、そこだけ測り直す
        _select(session, _face_refs(body, [2]))
        session.extrude_face(3.0)
        session.state_meta()
        assert [item[0] for item in calls] == [body.id], calls
    finally:
        optimize.face_fingerprint = original


def test_the_log_broadcast_does_not_carry_every_fingerprint(session: Session) -> None:
    """状態配信は面の指紋の本文を配らない(**保存(`to_json`)は書く**)。

    配ると、面を数十枚指した1操作の指紋が**以後の全配信に乗り続ける**。
    """
    body = _box(session)
    _preserve(session, body)
    _select(session, _face_refs(body, [1, 2, 3]))
    session.set_fixtures()

    delivered = session.state_meta()["log"][-1]["args"]
    assert "配信しない" in delivered["faces"], delivered
    assert len(session.document.log.entries[-1].args["faces"]) == 3


# --------------------------------------------------------------------------------------
# 8. 選択種別の顔ぶれがTS側の型と一致すること(**静的に読む**)
# --------------------------------------------------------------------------------------
def _ts_union_arms(path: Path, name: str) -> set:
    """`export type <name> = A | B | C` の右辺を集合で返す。"""
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^export type {name} = (.+)$", text, re.M)
    assert match, f"{name} の宣言を {path.name} から読めていない"
    return {arm.strip() for arm in match.group(1).split("|") if arm.strip()}


def test_the_typescript_selectable_kinds_match_the_core() -> None:
    """TSの `SelectableKind` の顔ぶれが、コアの `SELECTABLE_KINDS` と一致する。

    ここが古いままだと、`KIND_COUNTERS` のような**種別ごとの表に穴が開く**
    (件数表示が `undefined` になる形で、型検査も実行時例外も出ないまま画面に出る)。
    """
    shape_kinds = {arm.strip("'") for arm in _ts_union_arms(SESSION_TS, "ShapeKind")}
    declared = set()
    for arm in _ts_union_arms(COMMAND_TS, "SelectableKind"):
        declared |= shape_kinds if arm == "ShapeKind" else {arm.strip("'")}
    assert declared == set(selection.SELECTABLE_KINDS), {
        "TSのみ": sorted(declared - set(selection.SELECTABLE_KINDS)),
        "コアのみ": sorted(set(selection.SELECTABLE_KINDS) - declared),
    }

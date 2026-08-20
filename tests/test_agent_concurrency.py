"""H(同時編集の安全性)契約: 人とAIが**本当に並行して**同じ文書を触るときの2つの穴を塞ぐ。

## 背景

`duocad/selection.py` が明記するとおり「添字は現時点のものであって、形が変われば別の
実体を指す」(§3.5・§7)。MCP経路では「人が頼んで待つ」ので実質シリアルだったが、
アプリ内AI(タスクF・G)では本当に並行になる —— AIが数十秒〜数分考えている間に、
人が形を変えたり選択を作り直したりできる。

## このファイルが固定する2つ

1. **AIのターンをまたいで文書が変わったとき、AIのverb呼び出しが静かに別の対象へ効かない。**
   タスクC(`Session._reset_selection_if_needed`)が形を変えるverbの確定後に確定選択を
   全リセットする契約は**既に成り立っていた**(実機で確認・下の「A」節が固定する)。
   ここで新しく埋めたのは、その契約が届かない2つの経路:
   - `fillet` / `chamfer` / `extrude`(領域)は**選択を経由しない対象ボディ解決**(`body_id`
     の既定=アクティブ)を持ち、確定選択が生きていても対象ボディが隠されていれば
     従来は素通りしていた(「B」節)。`duocad/verbs/_common.py::editable_body` の
     `require_visible` を `fillet`/`chamfer` にも課し、`extrude` の `_resolve_target` /
     `_do_extrude` にも同じ検査を足した。
   - 選択をまったく経由しない verb(`set_visible` 等)は、そもそも `resets_selection` の
     対象外である。`AgentLoop.run()` に**楽観ロック**を足した(「D」節) ——
     モデルへの1往復を送る直前に文書の版(`current_seq()`)を控え、応答が返ったときに
     ずれていたら、そのターンのツール呼び出しを一切実行せずモデルへ選び直させる。
2. **確定済み選択の種別とverbが要求する種別の食い違いが、モデルに考えさせる前に
   機械的に検出されて理由が返る。** engine
   `01_inbox/2026-08-20-llm-agent-thinking-runaway-on-contradictory-task.md` の実測
   (矛盾した指示でモデルが確認ツールを5回繰り返し600秒応答が無かった)への予防。
   `SelectionEmpty` の**コードは変えない**(tests/README.md 決定3 —— 「何も選んでいない」
   と「違う種別を選んだ」は次の一手がどちらも「選び直す」で同じ、という既存の意図的な
   設計を尊重する)。変えたのは**文面**だけ —— 確定選択が空でないのに種別が違うだけなら、
   実際に確定しているものを名指しする(「C」節)。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import selection
from duocad.agent import AgentLoop
from duocad.agent.providers import Provider, ProviderReply, ToolCall
from duocad.document import Body
from duocad.mcp_server import DocumentTools
from duocad.server import Session

BOX_X, BOX_Y, BOX_Z = 30.0, 30.0, 40.0


@pytest.fixture()
def box_session(tmp_path: Path) -> Session:
    """30x30x40 の直方体を1つ持つセッション(面6・辺12)。"""
    made = Session(None, 0.2)
    made.document.add_body(
        Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(BOX_X, BOX_Y, BOX_Z).Shape())
    )
    return made


def commit_edge(session: Session, body_id: str = "box", index: int = 1) -> None:
    selection.propose_selection(session.document, refs=[{"body": body_id, "kind": "edge", "index": index}])
    selection.commit_selection(session.document)


def commit_face(session: Session, body_id: str = "box", index: int = 1) -> None:
    selection.propose_selection(session.document, refs=[{"body": body_id, "kind": "face", "index": index}])
    selection.commit_selection(session.document)


# ========================================================================================
# A. 契約の確認: 形を変えるverbの後、確定選択は全リセットされる(タスクC・実機で既に成立)
# ========================================================================================
def test_a_concurrent_shape_change_empties_the_confirmed_selection_and_the_next_verb_fails_loud(
    box_session: Session,
):
    """AIが辺を確定した後、別の主体(人)が**無関係な**形状変更を挟むと、
    AIの次のverb呼び出しは対象を黙って変えず `SelectionEmpty` で止まる。

    `extrude_face` は `resets_selection=True`(`duocad/catalog.py`)なので、確定した
    面を1つ押し出すだけで確定選択が全リセットされる —— AIが辺を選んでいたことは
    もう関係なくなる。
    """
    commit_edge(box_session)
    assert len(selection.get_selection(box_session.document).items) == 1

    # 「人」が割り込んで別の操作をする(AIのターンとは無関係)
    commit_face(box_session, index=2)
    box_session.extrude(1.0)  # Session.extrude は catalog 上の `extrude_face`

    assert len(selection.get_selection(box_session.document).items) == 0
    with pytest.raises(selection.SelectionEmpty):
        box_session.fillet(1.0)


def test_undo_between_commit_and_the_verb_call_also_empties_the_selection(box_session: Session):
    """undo/redo とAIターンの競合(タスクH `resets_selection` 対象の1つ)。

    人がAIの計画を先読みして操作を取り消しても、AIが持っていた確定選択は
    リセットされる —— `undo` は `resets_selection=True`。
    """
    commit_face(box_session, index=1)
    box_session.extrude(2.0)  # 何か1つログに積む(取り消せる操作を作る)
    commit_edge(box_session)
    assert len(selection.get_selection(box_session.document).items) == 1

    box_session.undo()

    assert len(selection.get_selection(box_session.document).items) == 0
    with pytest.raises(selection.SelectionEmpty):
        box_session.fillet(1.0)


# ========================================================================================
# B. 埋めた穴: 選択は生きているが、対象ボディが人に隠された(resets_selectionが届かない経路)
# ========================================================================================
def test_fillet_refuses_a_body_hidden_after_the_selection_was_confirmed(box_session: Session):
    """人が確定選択を消費させる**前に**そのボディを隠すと、`fillet` は静かに丸めない。

    2026-08-20実測: 修正前はこの手順で `fillet` が成功していた
    (`duocad/verbs/_common.py::editable_body` の `require_visible` を参照)。
    """
    commit_edge(box_session)
    box_session.set_visible("box", False)  # 人が隠す(選択は消えない = 選択の外の経路)
    assert len(selection.get_selection(box_session.document).items) == 1

    with pytest.raises(ValueError, match="hidden"):
        box_session.fillet(1.0)


def test_chamfer_refuses_a_body_hidden_after_the_selection_was_confirmed(box_session: Session):
    commit_edge(box_session)
    box_session.set_visible("box", False)

    with pytest.raises(ValueError, match="hidden"):
        box_session.chamfer(1.0)


def test_extrude_regions_refuses_the_active_body_when_it_was_hidden_meanwhile(
    box_session: Session,
):
    """`extrude`(領域)は対象ボディを**選択ではなく**引数/アクティブから取る
    (`duocad/catalog.py` の `body_id` は「名前であって対象集合ではない」)ので、
    `resets_selection` の全リセットが働いても対象ボディの可視性だけは別に守る必要がある。
    """
    box_session.document.set_active_body("box")
    sketch_id = box_session.sketch_create("XY")["sketch_id"]
    box_session.add_rectangle(sketch_id, [0, 0], 5, 5)
    selection.propose_selection(box_session.document, refs=[{"sketch": sketch_id, "index": 1}])
    selection.commit_selection(box_session.document)

    box_session.set_visible("box", False)  # 人が対象ボディを隠す

    with pytest.raises(ValueError, match="hidden"):
        box_session.extrude_regions(1.0, mode="join")


# ========================================================================================
# C. 予防: 確定選択の種別とverbが要求する種別の食い違いを、モデルに考えさせる前に返す
# ========================================================================================
def test_a_kind_mismatch_names_what_is_actually_confirmed(box_session: Session):
    """面が3枚確定しているのに `fillet`(辺が要る)を呼ぶと、理由に**両方**が入る。

    2026-08-20の実測(engine `01_inbox/...-thinking-runaway-...md`)は、この情報が
    無かったためにモデルが確認ツールを5回繰り返して止まったことへの予防 ——
    1回の呼び出しの返り値だけで「フィレットは辺を要求するが、確定しているのは面」と
    分かるようにする。コードは変えない(`selection_empty` のまま)。
    """
    selection.propose_selection(
        box_session.document,
        refs=[
            {"body": "box", "kind": "face", "index": 1},
            {"body": "box", "kind": "face", "index": 2},
            {"body": "box", "kind": "face", "index": 3},
        ],
    )
    selection.commit_selection(box_session.document)

    with pytest.raises(selection.SelectionEmpty) as excinfo:
        box_session.fillet(1.0)

    message = str(excinfo.value)
    assert "edge" in message
    assert "3 face" in message
    assert excinfo.value.kind == "edge"


def test_a_genuinely_empty_selection_keeps_the_short_message(box_session: Session):
    """何も選んでいないときは、機械的に間違った憶測(0 face等)を作らず短いままにする。"""
    with pytest.raises(selection.SelectionEmpty) as excinfo:
        box_session.fillet(1.0)
    assert str(excinfo.value) == "no edge in the confirmed selection"


def test_the_kind_mismatch_reaches_the_mcp_error_envelope(box_session: Session):
    """コアの文面がMCP封筒の `message` までそのまま届く(封筒を新設しない)。"""
    from duocad.mcp_server import McpServer

    server = McpServer(DocumentTools(box_session))
    selection.propose_selection(
        box_session.document, refs=[{"body": "box", "kind": "face", "index": 1}]
    )
    selection.commit_selection(box_session.document)

    response = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "fillet", "arguments": {"radius": 1.0}}}
    )
    import json

    payload = json.loads(response["result"]["content"][0]["text"])
    error = payload["error"]
    assert error["code"] == "selection_empty"  # コードは変えない(tests/README.md 決定3)
    assert "1 face" in error["message"]


# ========================================================================================
# D. AgentLoopの楽観ロック: モデルが考えている間に文書が変わったら実行しない
# ========================================================================================
def tool_use(name: str, arguments: dict, call_id: str = "call_1") -> ProviderReply:
    call = ToolCall(id=call_id, name=name, arguments=arguments)
    return ProviderReply(text="", tool_calls=(call,), stop_reason="tool_use", stop="tool_use")


def spoke(text: str) -> ProviderReply:
    return ProviderReply(text=text, stop_reason="end_turn", stop="end_turn")


class ConcurrentEditProvider(Provider):
    """`RecordedProvider`(test_agent_loop.py)と同じ形だが、応答を返す**直前**に
    副作用(=人の並行編集を模したコールバック)を挟める。

    `AgentLoop.run()` は `provider.complete()` の**前後**で文書の版を比べるので、
    ここで走らせる副作用が「モデルが考えている間に人が文書を変えた」を模す唯一の場所になる。
    """

    name = "concurrent-edit"

    def __init__(self, script) -> None:
        self.model = "concurrent-edit"
        self.script = list(script)
        self.requests: list[dict] = []

    def complete(self, *, system, turns, tools, max_tokens, timeout):
        self.requests.append({"turns": list(turns), "tools": list(tools)})
        reply, side_effect = self.script.pop(0)
        if side_effect is not None:
            side_effect()
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_the_loop_refuses_a_tool_call_decided_before_a_concurrent_edit_and_the_model_retries(
    box_session: Session,
):
    """`set_visible` は選択を経由しないので `resets_selection` の網に掛からない —— これが
    楽観ロックを足した理由そのもの(モジュールdocstring「D」節)。

    1往復目: モデルは「boxを見せて」のつもりで `set_visible(True)` を計画するが、
    その応答が返ってくる**間に**人が別の理由でboxを隠す(副作用)。ループはこれを検出し、
    ツールを実行せずに「文書が変わった」を返す —— **静かに人の意図の上から実行しない**。
    2往復目: モデルは(自己修復して)同じツールを呼び直し、今度は割り込みが無いので通る。
    """
    tools = DocumentTools(box_session)

    def concurrent_hide() -> None:
        box_session.set_visible("box", False)

    provider = ConcurrentEditProvider(
        [
            (tool_use("set_visible", {"body_id": "box", "visible": True}), concurrent_hide),
            (tool_use("set_visible", {"body_id": "box", "visible": True}), None),
            (spoke("表示しました"), None),
        ]
    )

    run = AgentLoop(tools, provider).run("boxを表示して")

    assert run.status == "completed"
    assert len(provider.requests) == 3, "1回止めて、モデルに選び直させ、成功して完了するはず"
    # 実行されなかった1回目は `tool_calls`(=実際に走ったツール)に載らない
    assert [outcome.name for outcome in run.tool_calls] == ["set_visible"]
    assert run.tool_calls[0].ok is True
    assert box_session.document.bodies[0].visible is True, "2回目の呼び出しは実行されている"

    # 1往復目のツール結果は「実行しなかった」構造化エラー(自己修復の材料)
    import json

    from duocad.agent.providers import ToolResultTurn

    second_request_turns = provider.requests[1]["turns"]
    stale_turn = next(t for t in second_request_turns if isinstance(t, ToolResultTurn))
    stale_result = stale_turn.results[0]
    assert stale_result.ok is False
    stale_payload = json.loads(stale_result.content)
    assert stale_payload["error"]["exception"] == "NotExecuted"
    assert "文書が変わった" in stale_payload["error"]["message"]


def test_the_optimistic_lock_does_not_trip_on_the_loops_own_actions(box_session: Session):
    """自分自身が同じ往復・同じターンで積んだ変更を「よそからの変更」と誤認しない。

    1つの応答に2つのツール呼び出し(`extrude_face` → `fillet`)が入っていても、版比較は
    **往復の前後**でしか行わない(往復の中で自分が動かした分はチェック対象外)ので、
    どちらも実行される —— 誤検出しないことを確かめる回帰。
    """
    tools = DocumentTools(box_session)
    commit_face(box_session, index=1)
    reply = ProviderReply(
        text="",
        tool_calls=(
            ToolCall(id="c1", name="extrude_face", arguments={"distance": 2.0}),
            ToolCall(id="c2", name="describe_document", arguments={}),
        ),
        stop_reason="tool_use",
        stop="tool_use",
    )
    provider = ConcurrentEditProvider([(reply, None), (spoke("できました"), None)])

    run = AgentLoop(tools, provider).run("面を押し出して教えて")

    assert run.status == "completed"
    assert [outcome.name for outcome in run.tool_calls] == ["extrude_face", "describe_document"]
    assert all(outcome.ok for outcome in run.tool_calls)

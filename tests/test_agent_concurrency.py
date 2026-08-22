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
     モデルへの1往復を送る直前に文書の版(`current_seq()`)を控え、ツールを**1件実行する
     直前ごとに**比べ、ずれていればそのツール呼び出し以降を実行せずモデルへ選び直させる。
   - **QA(2026-08-20)で2件のcriticalが見つかり、同日中に直した**: (a) 版に選択の
     「指紋」(`selection.fingerprint`)を混ぜていなかったので、人が**辺Aを外して同じ種別の
     辺Bへ選び直して確定する**(件数も種別も変わらない=`resets_selection`も
     `selection_empty`も発火しない)入れ替えを見逃していた(`current_seq()` は
     `(ログの本数, 選択の指紋)` のタプルへ改版)。(b) チェックが応答到着直後の1回だけで、
     1つの応答に束ねられた複数ツール呼び出しの**間**(1件目の実行後〜2件目の実行前)は
     見ていなかった(1件ごとのチェックへ変更)。
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
    assert "文書または選択が変わった" in stale_payload["error"]["message"]


def test_the_optimistic_lock_does_not_trip_on_the_loops_own_actions(box_session: Session):
    """自分自身が同じ往復で積んだ変更を「よそからの変更」と誤認しない。

    1つの応答に2つのツール呼び出し(`extrude_face` → `describe_document`)が入っている。
    チェックは**呼び出しごと**(1件実行するたびに次の基準値を取り直す)なので、
    1件目が版を動かしても2件目の直前チェックはその動いた後の値を基準にする ——
    どちらも実行される。誤検出しないことを確かめる回帰。
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


# ========================================================================================
# E. QA(2026-08-20)で見つかったcritical2件への回帰
# ========================================================================================
def test_reselecting_the_same_kind_between_commit_and_the_verb_call_is_detected(
    box_session: Session,
):
    """QA critical指摘1: ログの本数だけでは「選択の入れ替え」を見逃す。

    `propose_selection` / `commit_selection` は操作ログに一切乗らない
    (`duocad/selection.py` モジュールdocstring「操作ログとの関係」)ので、`current_seq()`
    が `len(log.entries)` だけを見ていた旧実装は、人が**辺1の選択を外して同じ種別
    (辺)の辺5へ選び直して確定する**手順を検出できなかった —— 件数も種別も変わらないので
    `resets_selection` の全リセットも `selection_empty` も発火しない。
    `current_seq()` に選択の指紋(`selection.fingerprint`)を混ぜたことで、この入れ替えも
    「版が動いた」として検出されることを固定する。
    """
    tools = DocumentTools(box_session)
    commit_edge(box_session, index=1)  # AIはこの時点の「辺1」を見て計画したはず

    def swap_the_confirmed_edge() -> None:
        # 人が辺1の確定を外し、同じ種別(辺)の辺5へ選び直して確定する。
        # ログの本数はこの手順の前後で変わらない。
        selection.propose_selection(
            box_session.document, refs=[{"body": "box", "kind": "edge", "index": 5}]
        )
        selection.commit_selection(box_session.document)

    provider = ConcurrentEditProvider(
        [
            (tool_use("fillet", {"radius": 2.0}), swap_the_confirmed_edge),
            (tool_use("fillet", {"radius": 2.0}), None),
            (spoke("丸めました"), None),
        ]
    )

    run = AgentLoop(tools, provider).run("さっき選んだ辺をフィレットして")

    assert run.status == "completed"
    assert len(provider.requests) == 3, "1回止めて選び直させ、2回目で成功して完了するはず"
    # 実行されたのは2回目だけ(1回目は「辺1のつもりで辺5」を黙って丸めることを防いだ)
    assert [outcome.name for outcome in run.tool_calls] == ["fillet"]
    assert run.tool_calls[0].ok is True


class InterferingTools:
    """`DocumentTools` を薄く覆い、**N回目の `current_seq()` 呼び出し**の直前に
    副作用を挟める(QA critical指摘2の再現専用)。

    `ConcurrentEditProvider` は往復(`provider.complete()`)単位でしか副作用を挟めないので、
    「1つの応答に束ねられた2件のツール呼び出しの間」という**呼び出し単位**の隙間は
    こちらでなければ作れない。副作用を `current_seq()` の呼び出し自体の直前で起こすのは、
    `AgentLoop.run()` が呼び出し直前にそれを読む構造そのものを利用して、
    「直前チェックの次の読み取りで初めて変化が見える」ことを保証するため。
    """

    def __init__(self, inner: DocumentTools, trigger_at_call: int, side_effect) -> None:
        self.inner = inner
        self._trigger_at_call = trigger_at_call
        self._side_effect = side_effect
        self._current_seq_calls = 0

    def descriptors(self):
        return self.inner.descriptors()

    def call(self, name, arguments):
        return self.inner.call(name, arguments)

    def error_envelope(self, tool, error):
        return self.inner.error_envelope(tool, error)

    def current_seq(self):
        self._current_seq_calls += 1
        if self._current_seq_calls == self._trigger_at_call:
            self._side_effect()
        return self.inner.current_seq()


def test_a_concurrent_edit_between_two_tool_calls_in_the_same_reply_is_detected(
    box_session: Session,
):
    """QA critical指摘2: 1つの応答に束ねられた複数のツール呼び出しの**間**も見る。

    以前は応答が届いた直後の1回だけ版を確かめ、残り全部を素通しで実行していた。
    本番経路(`_AiToolsOnLoop._run_on_loop`)は呼び出しのたびにイベントループへ制御を
    返すので、1件目の実行後・2件目の実行前にも人の操作が挟まりうる。ここでは
    `current_seq()` の4回目の呼び出し(=1件目の実行後の基準値取得のさらに次、
    つまり2件目の直前チェック)の直前に人の操作を割り込ませ、2件目が実行されない
    ことを固定する。
    """
    inner = DocumentTools(box_session)
    commit_face(box_session, index=1)

    def hide_the_box() -> None:
        box_session.set_visible("box", False)

    # current_seq() の呼び出し順: ①往復前の基準値 → ②1件目の直前チェック →
    # ③1件目実行後の基準値更新 → ④2件目の直前チェック。④の直前で割り込む。
    tools = InterferingTools(inner, trigger_at_call=4, side_effect=hide_the_box)

    reply = ProviderReply(
        text="",
        tool_calls=(
            ToolCall(id="c1", name="extrude_face", arguments={"distance": 2.0}),
            ToolCall(id="c2", name="set_visible", arguments={"body_id": "box", "visible": True}),
        ),
        stop_reason="tool_use",
        stop="tool_use",
    )
    provider = ConcurrentEditProvider([(reply, None), (spoke("できました"), None)])

    run = AgentLoop(tools, provider).run("面を押し出してからboxを表示して")

    assert run.status == "completed"
    # 1件目(extrude_face)は実行される。2件目(set_visible)は、その直前に人がboxを
    # 隠したこと(=版が動いたこと)を検出して実行されない。
    assert [outcome.name for outcome in run.tool_calls] == ["extrude_face"]
    assert run.tool_calls[0].ok is True
    assert box_session.document.bodies[0].visible is False, (
        "2件目は実行されていない(人が隠したままのはず。実行されていたら再びTrueになる)"
    )

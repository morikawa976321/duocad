"""同時編集の安全性 —— 人とAIが**本当に並行して**同じ文書を触るときの2つの穴を塞ぐ。

「添字は現時点のものであって、形が変われば別の実体を指す」ので、MCP経路(人が頼んで待つ=
実質シリアル)では問題にならなかったことが、アプリ内AIでは本当に並行になる。

固定するのは2つ:

1. **AIのターンをまたいで文書が変わったとき、AIのverb呼び出しが静かに別の対象へ効かない。**
   `Session._reset_selection_if_needed` の全リセットが届かない2つの経路を埋めてある ——
   (a) `fillet` / `chamfer` / `extrude` は**選択を経由しない対象ボディ解決**(`body_id` の
   既定=アクティブ)を持つので `editable_body(require_visible=True)` を課す(「B」節)、
   なお各テストの依頼文に寸法(10mm・R2)を書いてあるのは、**このファイルが見たいのが
   楽観ロックだから**である。書かないと寸法のガード
   (`AgentLoop._dimensions_nobody_asked_for`)が先に立ち、verbまで届かない。

   (b) 選択をまったく経由しないverb(`set_visible` 等)には `AgentLoop.run()` の**楽観ロック**
   (「D」節)。版は `(ログの本数, 選択の指紋)` で、チェックは**ツール1件を実行する直前ごと**
   —— 件数も種別も変わらない「選択の入れ替え」も、1つの応答に束ねられた呼び出しの**間**も
   これで見る(「E」節)。
2. **確定済み選択の種別とverbが要求する種別の食い違いが、モデルに考えさせる前に機械的に
   検出されて理由が返る。** 矛盾した指示でモデルが確認ツールを5回繰り返し600秒応答が
   無かった実測への予防(「C」節)。
"""
from __future__ import annotations


import pytest

from duocad import selection
from duocad.agent import AgentLoop
from duocad.agent.providers import Provider, ProviderReply, ToolCall
from duocad.mcp_server import DocumentTools
from duocad.server import Session
from agent_support import spoke, tool_use


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
    box_session.extrude_face(1.0)

    assert len(selection.get_selection(box_session.document).items) == 0
    with pytest.raises(selection.SelectionEmpty):
        box_session.fillet(1.0)


# ========================================================================================
# B. 埋めた穴: 選択は生きているが、対象ボディが人に隠された(resets_selectionが届かない経路)
# ========================================================================================
def test_fillet_refuses_a_body_hidden_after_the_selection_was_confirmed(box_session: Session):
    """人が確定選択を消費させる**前に**そのボディを隠すと、`fillet` は静かに丸めない。

    実測: `require_visible` を課す前はこの手順で `fillet` が成功していた。
    """
    commit_edge(box_session)
    box_session.set_visible("box", False)  # 人が隠す(選択は消えない = 選択の外の経路)
    assert len(selection.get_selection(box_session.document).items) == 1

    with pytest.raises(ValueError, match="hidden"):
        box_session.fillet(1.0)


def test_extrude_refuses_the_active_body_when_it_was_hidden_meanwhile(
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
        box_session.extrude(1.0, mode="join")


# ========================================================================================
# C. 予防: 確定選択の種別とverbが要求する種別の食い違いを、モデルに考えさせる前に返す
# ========================================================================================
def test_a_kind_mismatch_names_what_is_actually_confirmed(box_session: Session):
    """面が3枚確定しているのに `fillet`(辺が要る)を呼ぶと、理由に**両方**が入る。

    1回の呼び出しの返り値だけで「フィレットは辺を要求するが、確定しているのは面」と
    分かるようにする(モデルが確認ツールを繰り返して止まるのを防ぐ)。
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


class ConcurrentEditProvider(Provider):
    """`RecordedProvider`(`tests/agent_support.py`)と同じ形だが、応答を返す**直前**に
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

    run = AgentLoop(tools, provider).run("面を10mm押し出して教えて")

    assert run.status == "completed"
    assert [outcome.name for outcome in run.tool_calls] == ["extrude_face", "describe_document"]
    assert all(outcome.ok for outcome in run.tool_calls)


# ========================================================================================
# E. 版の比較が見逃しうる2つの経路への回帰
# ========================================================================================
def test_reselecting_the_same_kind_between_commit_and_the_verb_call_is_detected(
    box_session: Session,
):
    """ログの本数だけでは「選択の入れ替え」を見逃す。

    `propose_selection` / `commit_selection` は操作ログに一切乗らないので、人が**辺1の選択を
    外して同じ種別(辺)の辺5へ選び直して確定する**手順は件数も種別も変わらず、
    `resets_selection` も `selection_empty` も発火しない。`current_seq()` に選択の指紋を
    混ぜたことでこの入れ替えも「版が動いた」として検出されることを固定する。
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

    run = AgentLoop(tools, provider).run("さっき選んだ辺をR2でフィレットして")

    assert run.status == "completed"
    assert len(provider.requests) == 3, "1回止めて選び直させ、2回目で成功して完了するはず"
    # 実行されたのは2回目だけ(1回目は「辺1のつもりで辺5」を黙って丸めることを防いだ)
    assert [outcome.name for outcome in run.tool_calls] == ["fillet"]
    assert run.tool_calls[0].ok is True


class InterferingTools:
    """`DocumentTools` を薄く覆い、**N回目の `current_seq()` 呼び出し**の直前に副作用を挟める。

    `ConcurrentEditProvider` は往復(`provider.complete()`)単位でしか副作用を挟めないので、
    「1つの応答に束ねられた2件のツール呼び出しの間」という**呼び出し単位**の隙間はこちらで
    なければ作れない。副作用を `current_seq()` の呼び出し自体の直前で起こすのは、
    `AgentLoop.run()` が呼び出し直前にそれを読む構造そのものを利用して、「直前チェックの次の
    読み取りで初めて変化が見える」ことを保証するため。
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

    def call_and_seq(self, name, arguments):
        # 直後の版の読み取りもこの覆いの `current_seq` を通す(呼び出し回数の数え方を保つ)。
        payload, error, _ = self.inner.call_and_seq(name, arguments)
        return payload, error, self.current_seq()

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
    """1つの応答に束ねられた複数のツール呼び出しの**間**も見る。

    本番経路(`ToolsOnLoop._run_on_loop`)は呼び出しのたびにイベントループへ制御を返すので、
    1件目の実行後(版の取り直しの後)・2件目の実行前にも人の操作が挟まりうる。`current_seq()` の
    4回目の呼び出し(=2件目の直前チェック)の直前に人の操作を割り込ませ、2件目が実行されないことを
    固定する。実行の完了と版の取り直しの**間**に挟まる操作は、本番では1手にまとめてあるので起きない
    (`ToolsOnLoop.call_and_seq`。下の `test_a_human_edit_landing_right_after_a_tool_call_...`)。
    """
    inner = DocumentTools(box_session)
    commit_face(box_session, index=1)

    def hide_the_box() -> None:
        box_session.set_visible("box", False)

    # current_seq() の呼び出し順: ①往復前の基準値 → ②1件目の直前チェック →
    # ③1件目実行と同じ手での基準値更新(`call_and_seq`) → ④2件目の直前チェック。④の直前で割り込む。
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

    run = AgentLoop(tools, provider).run("面を10mm押し出してからboxを表示して")

    assert run.status == "completed"
    # 1件目(extrude_face)は実行される。2件目(set_visible)は、その直前に人がboxを
    # 隠したこと(=版が動いたこと)を検出して実行されない。
    assert [outcome.name for outcome in run.tool_calls] == ["extrude_face"]
    assert run.tool_calls[0].ok is True
    assert box_session.document.bodies[0].visible is False, (
        "2件目は実行されていない(人が隠したままのはず。実行されていたら再びTrueになる)"
    )


def test_a_human_edit_landing_right_after_a_tool_call_is_not_absorbed_into_the_next_baseline(
    box_session: Session,
):
    """落ちたら、ツール実行の完了と「実行直後の版」の読み取りが別々にイベントループへ投げられ、
    その間にループが処理した人の操作が次の直前チェックの基準に吸われて AI に見えない(issue #48)。

    本番の覆い(`ToolsOnLoop`)で、ツールの実行中に「直後に人が箱を隠す」をループへ予約する。
    1手で読んでいれば、返った版はその操作を含まず、次に読む版と食い違う(=次の直前チェックで検出できる)。
    """
    import asyncio

    from duocad.server import ToolsOnLoop

    inner = DocumentTools(box_session)

    async def scenario() -> tuple:
        loop = asyncio.get_running_loop()
        on_loop = ToolsOnLoop(inner, loop)
        original_call = inner.call

        def call_and_schedule_a_human_edit(name, arguments):
            loop.call_soon(lambda: box_session.set_visible("box", False))  # ループの次の番で走る
            return original_call(name, arguments)

        inner.call = call_and_schedule_a_human_edit
        # 実行器スレッドから(=`AgentLoop.run()` が走る側から)呼ぶ
        payload, error, seq_after = await loop.run_in_executor(
            None, on_loop.call_and_seq, "describe_document", {}
        )
        await asyncio.sleep(0)  # 予約した人の操作を走らせる
        return payload, error, seq_after, inner.current_seq()

    payload, error, seq_after, seq_now = asyncio.run(scenario())

    assert error is None and payload["body_count"] == 1
    assert box_session.document.get_body("box").visible is False, "人の操作が走っていない(検査の前提)"
    assert seq_after != seq_now, "実行直後の版に、その後に入った人の操作が吸われている"

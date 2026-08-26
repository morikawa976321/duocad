"""実機のllama-serverでエージェントループが回ること(**8080が無ければskip**)。

決定的な検査は `tests/test_agent_loop.py`(偽プロバイダ)が持つ。ここが見るのは
**本物のローカルLLMがこのツール定義を食べて、実際にツールを呼べるか**の1本だけである
(CIは無いので、サーバが立っていないマシンでは黙ってskipして緑になること)。

立て方の例(2026-08-20の実測構成):

    llama-server -m <Qwen3.8-27B-Q3_K_M> -c 16384 --port 8080 --jinja

`--jinja` が要る —— ツール呼び出しのテンプレートが有効にならないと、モデルは道具を
呼べずに本文で「呼びました」と言う。この検査はそれを落とす。

Claude APIを叩く実テストは**書かない**(課金が走る)。アダプタの整形は単体検査が固める。
"""
from __future__ import annotations

import socket

from pathlib import Path

import pytest

from duocad.agent import AgentLoop, Limits, OpenAICompatProvider
from duocad.mcp_server import DocumentTools
from duocad.server import Session
#: 実物のSTEP(リポジトリ外)。**このcanaryだけが私物モデルに触れる** —— 他のテストは
#: 他のテストは私物STEPに触らないので、ここがリポジトリ外への唯一の参照。
#: llama-serverが立っていて、かつこのファイルがあるときだけ走る(既定は二重skip)。
PC_CASE_FEET = Path(r"C:\Users\tm976\mf\3d-models\mATX_PC_case_20251122\PC_case_feet.step")

HOST = "127.0.0.1"
PORT = 8080


def _llama_server_is_up() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _llama_server_is_up(), reason=f"llama-server が {HOST}:{PORT} に居ない"
    ),
    pytest.mark.skipif(not PC_CASE_FEET.exists(), reason=f"STEPが無い: {PC_CASE_FEET}"),
]


def test_a_local_model_can_actually_call_the_document_tools():
    """5面6辺の実物を読み込んだ文書について、モデルが**ツールを呼んで**答えること。"""
    tools = DocumentTools(Session(None, 0.2))
    imported = tools.call("import_step", {"path": str(PC_CASE_FEET)})
    assert imported["body_ids"], "STEPの読み込みに失敗している(検査の前提)"

    calls: list[tuple[str, bool]] = []
    loop = AgentLoop(
        tools,
        OpenAICompatProvider(base_url=f"http://{HOST}:{PORT}/v1"),
        limits=Limits(max_turns=6, max_tokens=1024, request_timeout=120.0),
        progress=lambda name, args, result, ok: calls.append((name, ok)),
    )
    run = loop.run("いま開いている文書にあるボディの面の数を調べて、数字で報告して。")

    assert run.status == "completed", f"{run.status}: {run.detail}"
    assert calls, "モデルがツールを1つも呼んでいない(--jinja を付け忘れていないか)"
    assert any(ok for _, ok in calls), f"成功したツール呼び出しが無い: {calls}"
    assert any(
        name in ("describe_document", "describe_shape") for name, _ in calls
    ), f"問い合わせツールを踏んでいない: {calls}"

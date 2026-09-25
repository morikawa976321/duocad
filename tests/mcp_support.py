"""MCP の口(`McpServer.handle`)を JSON-RPC で直に叩くテストの道具。"""
from __future__ import annotations

import json

import pytest

from duocad.mcp_server import McpServer

#: ツールの失敗が返してよい `code` の語彙(顔ぶれは `MCP.md`「実装のメモ」が正。
#: 実装のソースとの一致は `tests/test_doc_code_agreement.py` が見る)。
ERROR_CODES = {
    "not_found",            # 存在しないBody / sketch / parameter(KeyError)
    "invalid_argument",     # 形・型・範囲が不正(ValueError)
    "selection_not_committed",  # 提案のまま verb を呼んだ(A1の門)
    "selection_empty",      # 確定選択が**丸ごと空**(要求した種別以前の問題)
    "selection_kind_mismatch",  # 空ではないが要求した種別が無い
    "invalid_predicate",    # 述語が不正(有効なopの一覧を必ず添える)
    "busy",                 # 形状最適化の実行中に文書を変えようとした(DocumentBusy)
    "internal",             # 上記以外(黙って握りつぶさないための受け皿)
    "dimension_not_given",  # アプリ内AIの経路だけ(人が量を言っていないのに寸法を埋めた。MCP は返さない)
}


def request(server: McpServer, method: str, params: dict | None = None) -> dict:
    message: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    response = server.handle(message)
    assert response is not None, f"{method} はレスポンスを返さなければならない"
    return response


def call(server: McpServer, name: str, arguments: dict | None = None) -> dict:
    """成功を期待してツールを呼び、返ってきたJSONを辞書で返す。"""
    response = request(server, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "error" not in response, response
    result = response["result"]
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def call_error(server: McpServer, name: str, arguments: dict | None = None) -> dict:
    """失敗を期待してツールを呼び、**構造化されたエラー本体**(`payload["error"]`)を返す。

    ここで `json.loads` に成功すること自体が契約である —— 例外のテキストをそのまま
    投げていれば(`"KeyError: no such body ..."`)ここで落ちる。

    **未知の引数・必須の欠落・型違いにはこのヘルパを使えない** —— それらは
    `_check_arguments` がツール本体の前に JSON-RPC のトップレベル `error`(-32602)で
    弾くので、下の assert で落ちる。`request()` を直接呼んで `response["error"]["code"]` を見る。
    """
    response = request(server, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "error" not in response, f"実行時エラーをプロトコルエラーにしている: {response}"
    result = response["result"]
    assert result["isError"] is True, f"エラーになるはずが成功した: {result}"
    text = result["content"][0]["text"]
    try:
        payload = json.loads(text)
    except ValueError:
        pytest.fail(f"失敗が機械可読でない(例外のテキストを素で返している): {text!r}")
    assert "error" in payload, payload
    error = payload["error"]
    assert error["code"] in ERROR_CODES, f"未知のエラーコード: {error['code']!r}"
    assert error["tool"] == name
    assert error["message"].strip()
    return error

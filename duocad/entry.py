"""verb の入口 —— **選択から対象を取り、verb を掛け、応答に載せるものを返す**関数の登録表。

verb の純関数(`duocad/verbs/*.py`・`sketch.py`・`parameters.py`)は対象を引数で受ける。人も AI も
対象を引数では渡さずコアの確定選択から取らせるので、その間をつなぐ関数が verb ごとに1つ要る。
それを verb と同じモジュールに `@entry("<verb名>")` で置き、`Session.apply` が名前で引いて呼ぶ。

WebSocket・MCP・アプリ内 AI はどれも `Session.apply` を通るので、**verb を足すときに書くのは
verb のモジュール(純関数と入口)と `duocad/catalog.py` の宣言の2箇所だけ**である。

入口の形は `(document, **引数) -> dict`。**引数名は宣言(`catalog.Param.name`)と同じ綴り**にする
(WebSocket と MCP は届いた引数を名前のまま渡す)。戻り値は変更イベント(`applied`)へそのまま載る。

**duocad の他のモジュールを import しない**(verb のモジュールがここを import するので、逆向きは循環になる)。
"""
from __future__ import annotations

from typing import Any, Callable, Dict

EntryFn = Callable[..., dict]

_ENTRIES: Dict[str, EntryFn] = {}


def entry(verb: str) -> Callable[[EntryFn], EntryFn]:
    """`verb` の入口として登録する(デコレータ)。同じ名前へ別の関数を登録すると ValueError。"""

    def register(function: EntryFn) -> EntryFn:
        existing = _ENTRIES.get(verb)
        if existing is not None and _identity(existing) != _identity(function):
            raise ValueError(f"verb {verb!r} の入口が既に登録されている: {_identity(existing)}")
        _ENTRIES[verb] = function
        return function

    return register


def _identity(function: Any) -> tuple:
    """モジュールの読み直しで関数オブジェクトが変わっても同じ入口と見なすための名前。"""
    return (function.__module__, function.__qualname__)


def entry_for(verb: str) -> EntryFn:
    """登録済みの入口を引く。未登録なら KeyError。"""
    try:
        return _ENTRIES[verb]
    except KeyError:
        raise KeyError(
            f"verb {verb!r} に入口が無い(登録済み: {sorted(_ENTRIES)})。"
            "verb を定義したモジュールに `@entry` を書くこと"
        ) from None


def entries() -> Dict[str, EntryFn]:
    """verb名 -> 入口(写し)。"""
    return dict(_ENTRIES)

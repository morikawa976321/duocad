"""文書の名前つきの値の表(`Document.parameters`)。`d<n>` の「最小の空き番号」を先頭から数え直さずに引く。"""
from __future__ import annotations

from typing import Any

#: パラメータ名の接頭辞(Fusion360の d1/d2 と同じ流儀。改名機能は未実装)
PARAMETER_PREFIX = "d"


def _number_of(name: Any) -> int:
    """`d<n>` の n。その形でなければ 0。"""
    if isinstance(name, str) and name.startswith(PARAMETER_PREFIX):
        digits = name[len(PARAMETER_PREFIX):]
        if digits.isascii() and digits.isdigit():
            return int(digits)
    return 0


class ParameterTable(dict):
    """名前 -> 値 の表。`d1` … `d<hint - 1>` がすべて入っていることを覚えておき、空き番号の探索をそこから始める。

    足す側(代入・`update`)は覚えを壊さない。取り除く側(`pop` `del` `popitem` `clear`)は、
    取り除いた番号が覚えより小さければ覚えを下げる。これを抜けると番号が飛び、再生で名前がずれる。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._hint = 1

    def first_free_name(self) -> str:
        number = self._hint
        while f"{PARAMETER_PREFIX}{number}" in self:
            number += 1
        self._hint = number
        return f"{PARAMETER_PREFIX}{number}"

    def _released(self, name: Any) -> None:
        number = _number_of(name)
        if 0 < number < self._hint:
            self._hint = number

    def __delitem__(self, name: Any) -> None:
        super().__delitem__(name)
        self._released(name)

    def pop(self, name: Any, *default: Any) -> Any:
        found = name in self
        value = super().pop(name, *default)
        if found:
            self._released(name)
        return value

    def popitem(self) -> Any:
        name, value = super().popitem()
        self._released(name)
        return name, value

    def clear(self) -> None:
        super().clear()
        self._hint = 1

    def __reduce_ex__(self, protocol: Any) -> Any:
        return (ParameterTable, (dict(self),))

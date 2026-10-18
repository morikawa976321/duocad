"""段階4の peel の実績(`binary_search.PeelStats`)を、測定スクリプトが集める定型。"""
from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import binary_search


@contextmanager
def collecting_peel_stats():
    """`with` の間に呼ばれた `PeelWalk.take` の `PeelStats` を、呼ばれた順にリストへ集める。

    段階4は `binary_search.peel()` を呼ばず `PeelWalk.take` を直接使う(`peel()` も使い捨ての `PeelWalk` 経由)。
    `peel` に包みを被せても一度も呼ばれず、`peel_seconds` や `skipped_neck` が常に 0 になる。
    """
    stats: list = []
    real_take = binary_search.PeelWalk.take

    def take(self, order, count):
        result = real_take(self, order, count)
        stats.append(result[1])
        return result

    with mock.patch.object(binary_search.PeelWalk, "take", take):
        yield stats

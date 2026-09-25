"""受入検査(`check_*.py`)の足場: 1件の判定の出力と集計、例外の期待、`main` の定型。

各検査は `CHECK = Checker()` を1つ持ち、`check = CHECK.check` / `FAILURES = CHECK.failures` /
`RESULTS = CHECK.results` をモジュール属性として残す(`check_trace.py` が `wiring.FAILURES` を読む)。
検査関数の中身はこのモジュールに依存しない(`check(name, ok, detail)` を呼ぶだけ)。
コンソールの文字コードの設定は `_bootstrap`(全スクリプトが import する)にある。
"""
from __future__ import annotations

import json
import time
import traceback
from typing import Callable, Iterable, Optional

import _bootstrap  # noqa: F401  コンソールの設定と sys.path


class Checker:
    """1件ずつの判定の出力と、不合格・実測値の集計。**落ちても即死せず全件出してから終わる。**"""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.results: dict[str, object] = {}

    def check(self, name: str, ok: bool, detail: str) -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
        if not ok:
            self.failures.append(f"{name}: {detail}")


def expect_raises(fn: Callable, exc: type | tuple, *args, **kwargs) -> Optional[BaseException]:
    """`fn(*args, **kwargs)` が `exc` を投げればその例外を返す(真)。投げなければ None。
    別の例外は印字して None(不合格として扱う)。文面を `detail` に載せたり `in` で見たりできるよう、
    真偽値ではなく例外そのものを返す。"""
    try:
        fn(*args, **kwargs)
    except exc as error:  # type: ignore[misc]
        return error
    except Exception as error:  # noqa: BLE001 — 別の例外なら不合格として扱う
        print(f"      (想定外の例外 {type(error).__name__}: {error})")
        return None
    return None


def run(
    checker: Checker,
    title: Optional[str],
    sections: Iterable[Callable[[], object]],
    *,
    catch: bool = False,
    timed: bool = False,
    measured: bool = True,
    total_seconds: bool = False,
) -> int:
    """見出し(`title` の各行を `=` の罫線で挟む。None なら出さない)→ 各セクション → MEASURED 行 →
    NG/OK と終了コード(1/0)。

    `catch`: セクションの例外も不合格として記録して続ける(最初の1件で止まると、何がどこまで壊れて
    いるかが1周では分からない)。`timed`: NG/OK 行に経過秒数を添える。`measured`: MEASURED 行を出す。
    `total_seconds`: `results["total_seconds"]` に経過秒数を記録する。
    """
    started = time.perf_counter()
    if title is not None:
        print("=" * 72)
        for line in title.splitlines():
            print(line)
        print("=" * 72)
    for section in sections:
        if not catch:
            section()
            continue
        try:
            section()
        except Exception as exc:  # noqa: BLE001 — 検査中の例外も不合格として記録する
            traceback.print_exc()
            name = getattr(section, "__name__", "section")
            checker.check(f"{name} が例外で落ちた", False, f"{type(exc).__name__}: {exc}")
    if total_seconds:
        checker.results["total_seconds"] = round(time.perf_counter() - started, 1)

    print("\n" + "=" * 72)
    if measured:
        print("MEASURED " + json.dumps(checker.results, ensure_ascii=False, default=float))
        print("=" * 72)
    elapsed = f" / {time.perf_counter() - started:.1f}秒" if timed else ""
    if checker.failures:
        print(f"NG: {len(checker.failures)}件が不合格{elapsed}")
        for failure in checker.failures:
            print(f"  - {failure}")
        return 1
    print(f"OK: 全検査に合格{elapsed}")
    return 0

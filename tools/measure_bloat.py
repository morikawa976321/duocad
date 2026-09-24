"""行数とコメント率の目盛り。門ではない —— 気づくために測る。

    python tools/measure_bloat.py            # グループ別 + 大きいファイル
    python tools/measure_bloat.py --files    # 全ファイルを行数順に

コメント率は「種別」ではなく「量」しか見ない。CLAUDE.md の規律は種別の基準なので、
**この数字を満たすために4種のコメントを消してはならない**。実測事実を残すのが仕事の
変更では、率が上がるのが正しい。
"""

from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GROUPS = {
    "duocad/": ("duocad", "*.py"),
    "tests/": ("tests", "*.py"),
    "topopt_checks/": ("topopt_checks", "*.py"),
    "web/src/": ("web/src", ("*.ts", "*.svelte")),
    "web/scripts/": ("web/scripts", "*.mjs"),
}

BIG_FILE_LINES = 1500


def count_python(src: str) -> tuple[int, int, int]:
    """総行数・空行・コメント行を返す。

    docstring は「モジュール/関数/クラスの本体の先頭にある文字列リテラル」だけを数える。
    `print(\"\"\"...\"\"\")` のような通常の三重引用符を含めると、検査スクリプトのように
    長いリテラルを持つファイルで4.8倍ずれる(実測)。
    """
    lines = src.splitlines()
    blank = sum(1 for line in lines if not line.strip())

    marked: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                marked.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return len(lines), blank, len(marked)

    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            marked.update(range(first.lineno, first.end_lineno + 1))

    return len(lines), blank, len(marked)


def count_web(src: str) -> tuple[int, int, int]:
    """総行数・空行・コメント行を返す。

    文字列リテラルの中の `//` を数えないよう、素朴な状態機械で舐める。正規表現リテラルは
    区別していないので、`/pattern/` の中に `//` があると誤検出する(実害が出たら直す)。
    """
    lines = src.splitlines()
    blank = sum(1 for line in lines if not line.strip())

    marked: set[int] = set()
    i, n, line = 0, len(src), 1
    mode, quote = "code", ""
    while i < n:
        ch = src[i]
        if ch == "\n":
            line += 1
            i += 1
        elif mode == "code":
            if ch in "\"'`":
                mode, quote = "str", ch
                i += 1
            elif src.startswith("<!--", i):
                mode = "html"
                marked.add(line)
                i += 4
            elif src.startswith("//", i):
                marked.add(line)
                while i < n and src[i] != "\n":
                    i += 1
            elif src.startswith("/*", i):
                mode = "block"
                marked.add(line)
                i += 2
            else:
                i += 1
        elif mode == "str":
            if ch == "\\":
                i += 2
            elif ch == quote:
                mode = "code"
                i += 1
            else:
                i += 1
        elif mode == "block":
            marked.add(line)
            if src.startswith("*/", i):
                mode = "code"
                i += 2
            else:
                i += 1
        else:  # html
            marked.add(line)
            if src.startswith("-->", i):
                mode = "code"
                i += 3
            else:
                i += 1

    return len(lines), blank, len(marked)


def collect() -> list[tuple[str, str, int, int, int]]:
    rows = []
    for group, (subdir, patterns) in GROUPS.items():
        if isinstance(patterns, str):
            patterns = (patterns,)
        for pattern in patterns:
            for path in sorted((ROOT / subdir).rglob(pattern)):
                src = path.read_text(encoding="utf-8")
                counter = count_python if path.suffix == ".py" else count_web
                total, blank, comment = counter(src)
                rel = path.relative_to(ROOT).as_posix()
                rows.append((group, rel, total, blank, comment))
    return rows


def ratio(total: int, blank: int, comment: int) -> float:
    nonblank = total - blank
    return comment / nonblank * 100 if nonblank else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", action="store_true", help="全ファイルを行数順に出す")
    args = parser.parse_args()

    rows = collect()
    if not rows:
        print("対象ファイルが見つからない", file=sys.stderr)
        return 1

    print(f"{'グループ':16}{'ファイル':>6}{'非空行':>9}{'コメント':>10}{'率':>8}")
    grand = [0, 0, 0, 0]
    for group in GROUPS:
        group_rows = [r for r in rows if r[0] == group]
        total = sum(r[2] for r in group_rows)
        blank = sum(r[3] for r in group_rows)
        comment = sum(r[4] for r in group_rows)
        print(
            f"{group:16}{len(group_rows):>6}{total - blank:>9}{comment:>10}"
            f"{ratio(total, blank, comment):>7.1f}%"
        )
        grand[0] += len(group_rows)
        grand[1] += total
        grand[2] += blank
        grand[3] += comment
    print(
        f"{'合計':16}{grand[0]:>6}{grand[1] - grand[2]:>9}{grand[3]:>10}"
        f"{ratio(grand[1], grand[2], grand[3]):>7.1f}%"
    )

    shown = sorted(rows, key=lambda r: -r[2])
    if not args.files:
        shown = [r for r in shown if r[2] >= BIG_FILE_LINES]
        print(f"\n{BIG_FILE_LINES}行以上（足す前に切れ目を探す）")
    else:
        print("\n全ファイル（行数順）")

    if not shown:
        print("  なし")
        return 0

    print(f"  {'ファイル':44}{'行':>7}{'コメント':>10}{'率':>8}")
    for _, rel, total, blank, comment in shown:
        print(f"  {rel:44}{total:>7}{comment:>10}{ratio(total, blank, comment):>7.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

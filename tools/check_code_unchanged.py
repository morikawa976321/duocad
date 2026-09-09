"""コメントだけを触ったことを機械で証明する（Python側）。

    python tools/check_code_unchanged.py <比較元のgit ref>
    python tools/check_code_unchanged.py HEAD          # 作業ツリー vs HEAD

コメントはASTに残らないので、**docstringを取り除いてから `ast.dump` を比べる**と、
識別子・文字列リテラル・制御構造が1つでも変われば必ず差が出る。逆に言えば、
差が出なければ動作コードは1文字も変わっていない。

フロントエンド(web/)側に同じ手は要らない —— `npm run build` の成果物が同一バイトになる
ことが同じ証明になる(`dist/assets/*.js` `*.css` `dist/index.html` が同一で、
`*.js.map` だけが変わるのが期待される署名)。バンドラは本番ビルドでコメントを落とすので、
コードを1行でも取り違えて消していればJSのハッシュが動く。

いずれも context-engine [[identical-build-output-proves-comment-only-change]] の実測による。
"""

from __future__ import annotations

import argparse
import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("duocad", "tests", "topopt_checks", "tools")


class StripDocstrings(ast.NodeTransformer):
    """モジュール/関数/クラスの先頭の文字列リテラルだけを落とす。

    通常の文字列リテラルは残す —— 落とすとコードの変更を見逃す。
    """

    HOLDERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def _strip(self, node: ast.AST) -> ast.AST:
        self.generic_visit(node)
        body = getattr(node, "body", None)
        if body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
        return node

    visit_Module = _strip
    visit_FunctionDef = _strip
    visit_AsyncFunctionDef = _strip
    visit_ClassDef = _strip


def projection(src: str) -> str:
    """docstringを除いたASTのdump。構文エラーは原文をそのまま返す(差として出る)。"""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return f"<SyntaxError: {exc}>"
    return ast.dump(ast.fix_missing_locations(StripDocstrings().visit(tree)))


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, check=False
    ).stdout.decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", help="比較元のgit ref（例: HEAD, be2444e）")
    args = parser.parse_args()

    changed = [
        line
        for line in git("diff", "--name-only", args.ref, "--", *TARGETS).split()
        if line.endswith(".py")
    ]
    if not changed:
        print(f"{args.ref} との差分に .py は無い")
        return 0

    broken = []
    for rel in changed:
        before = git("show", f"{args.ref}:{rel}")
        path = ROOT / rel
        after = path.read_text(encoding="utf-8") if path.exists() else ""
        if not before and not after:
            continue
        if projection(before) != projection(after):
            broken.append(rel)

    print(f"{len(changed)} ファイルを照合")
    if broken:
        print("\n動作コードが変わっている（コメント専用の改修ではない）:")
        for rel in broken:
            print(f"  {rel}")
        return 1

    print("すべてコメント/docstringのみの変更（AST射影が一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

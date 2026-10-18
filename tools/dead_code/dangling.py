"""`from X import name` の X に name が無いもの(= すでに動かないスクリプトやテスト)と、存在しないモジュールへの import を探す。

    .venv/Scripts/python.exe tools/dead_code/dangling.py [出力フォルダ]     # 既定は output/dead_code

結果は `dangling.json`。`areas.py` が担当領域ごとの brief に切り分ける。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "dead_code"
OUT.mkdir(parents=True, exist_ok=True)

PACKAGES = ("duocad", "tools", "topopt_checks", "tests")
files = [f for f in subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0") if f.endswith(".py")]
tree_cache: dict[str, ast.Module | None] = {}


def parse(path: Path):
    key = str(path)
    if key not in tree_cache:
        try:
            tree_cache[key] = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, FileNotFoundError):
            tree_cache[key] = None
    return tree_cache[key]


def top_names(tree: ast.Module) -> set[str]:
    """モジュール直下で束縛される名前。`import *` があれば `"*"` を含める(以後は名前の有無を判定しない)。"""
    names: set[str] = set()

    def walk(body):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    for s in ast.walk(t):
                        if isinstance(s, ast.Name):
                            names.add(s.id)
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    names.add("*" if a.name == "*" else (a.asname or a.name))
            elif isinstance(n, ast.Import):
                for a in n.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for attr in ("body", "orelse", "finalbody"):
                    walk(getattr(n, attr, []) or [])
                for h in getattr(n, "handlers", []):
                    walk(h.body)

    walk(tree.body)
    return names


def resolve(module: str, base_dirs: list[Path]):
    parts = module.split(".")
    for base in base_dirs:
        p = base.joinpath(*parts)
        if p.with_suffix(".py").exists():
            return p.with_suffix(".py")
        if (p / "__init__.py").exists():
            return p / "__init__.py"
        if p.is_dir():
            return p
    return None


problems = []
for f in files:
    path = ROOT / f
    tree = parse(path)
    if tree is None:
        continue
    here = path.parent
    bases = [ROOT, here, ROOT / "topopt_checks", ROOT / "tools", ROOT / "tests", ROOT / "tools" / "thread_sim"]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                pkg = here
                for _ in range(node.level - 1):
                    pkg = pkg.parent
                target = pkg.joinpath(*node.module.split(".")) if node.module else pkg
                if target.with_suffix(".py").exists():
                    tp = target.with_suffix(".py")
                elif (target / "__init__.py").exists():
                    tp = target / "__init__.py"
                else:
                    if node.module and not target.exists():
                        problems.append({"file": f, "line": node.lineno, "problem": "missing-module", "detail": "." * node.level + node.module})
                    continue
            else:
                top = node.module.split(".")[0] if node.module else ""
                # パッケージの中の `http.py` が標準の `http` を隠して見えるので、兄弟ファイルは __init__ の無いフォルダ(スクリプト置き場)でだけ見る。
                sibling = not (here / "__init__.py").exists() and ((here / (top + ".py")).exists() or (here / top).is_dir())
                if not (top in PACKAGES or sibling or (ROOT / "topopt_checks" / (top + ".py")).exists()):
                    continue
                tp = resolve(node.module, bases)
                if tp is None:
                    problems.append({"file": f, "line": node.lineno, "problem": "missing-module", "detail": node.module})
                    continue
            if tp.is_dir():
                continue
            ttree = parse(tp)
            if ttree is None:
                continue
            names = top_names(ttree)
            for alias in node.names:
                if alias.name == "*" or alias.name in names or "*" in names:
                    continue
                sub = tp.parent / alias.name
                if tp.name == "__init__.py" and (sub.with_suffix(".py").exists() or sub.is_dir()):
                    continue
                problems.append({"file": f, "line": node.lineno, "problem": "missing-name", "detail": f"{node.module or '.'}:{alias.name}"})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "duocad" and resolve(alias.name, [ROOT]) is None:
                    problems.append({"file": f, "line": node.lineno, "problem": "missing-module", "detail": alias.name})

(OUT / "dangling.json").write_text(json.dumps(problems, ensure_ascii=False, indent=1), encoding="utf-8")
print(len(problems), "problems")
for p in problems[:60]:
    print(p)

"""M0契約: 境界と規律(実装計画書 §6予防3・4、§2アーキテクチャ)。

day1の予防として「問い合わせ層をインタフェースにする」「OCPを叩くのはkernel.pyだけ」を
機械的に検査する。verbがOCP/build123dを直接叩き始めるのを防ぐための唯一の安全網であり、
これが一番大事(タスクファイル記載)。ソースを静的に走査するのでOCP/build123dの実行環境が
無くても走る。
"""
from __future__ import annotations

import ast
from pathlib import Path

MINICAD_DIR = Path(__file__).resolve().parent.parent / "minicad"


def _imported_module_roots(py_file: Path) -> set[str]:
    """py_file内のあらゆるimport文(関数内含む)が参照するトップレベルモジュール名の集合。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_only_kernel_module_imports_ocp():
    """minicad配下(サブパッケージ含む)でOCP.*をimportしてよいのはkernel.pyだけ
    (verbが直接OCPを叩き始めるのを防ぐ)。"""
    violations = []
    for py_file in sorted(MINICAD_DIR.rglob("*.py")):
        if py_file.name == "kernel.py":
            continue
        if "OCP" in _imported_module_roots(py_file):
            violations.append(py_file.name)
    assert violations == [], f"OCPを直接importしているファイル(kernel.py以外は禁止): {violations}"


def test_kernel_module_actually_imports_ocp():
    """kernel.pyは実際にOCPをimportしている(上の境界テストが機能するための前提)。"""
    assert "OCP" in _imported_module_roots(MINICAD_DIR / "kernel.py")


def test_only_query_module_imports_build123d():
    """verb側(サブパッケージ含む)はbuild123dを直接importしない
    (問い合わせはquery.py越しに行う。差し替え可能に保つ)。"""
    violations = []
    for py_file in sorted(MINICAD_DIR.rglob("*.py")):
        if py_file.name == "query.py":
            continue
        if "build123d" in _imported_module_roots(py_file):
            violations.append(py_file.name)
    assert violations == [], f"build123dを直接importしているファイル(query.py以外は禁止): {violations}"

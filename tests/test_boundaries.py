"""境界と規律 —— 「問い合わせ層をインタフェースにする」「OCPを叩くのはカーネル層だけ」。

verbがOCP/build123dを直接叩き始めるのを防ぐための唯一の安全網。ソースを静的に走査するので
OCP/build123dの実行環境が無くても走る。

検査の実効性について:
- `duocad/` 配下を **再帰的に** 走査する(サブパッケージを作って逃げられない)。
- `import OCP` / `import OCP.X` / `from OCP import X` / `from OCP.X import Y` に加え、
  **動的import**(`importlib.import_module("OCP.X")` / `__import__("OCP")`)も検出する。
  静的importだけを見ていると、境界を破る最も安直な逃げ道(文字列でのimport)が素通りする。
- 免除は **パッケージ直下からの相対パスで名指し**する(ファイル名一致ではない)。
  ファイル名だけで免除すると `duocad/verbs/kernel.py` を作れば境界を無効化できてしまう。
"""
from __future__ import annotations

import ast
from pathlib import Path

DUOCAD_DIR = Path(__file__).resolve().parent.parent / "duocad"

# 免除はここに名指しした相対パスだけ(DUOCAD_DIRからの相対パス)。**ディレクトリ単位では
# 免除しない** —— 新しく足したファイルが黙って境界の外へ出るのを防ぐため。
#
# `topopt/` は**B-repを直接読む**数値計算のサブシステム(voxelize は OCCT の厳密なbboxを、
# anchor/design_space は検証用の合成ソリッドを build123d で組む)。`kernel.bounding_box` へ
# 回すと `SetGap` の順序が違って値が 1e-7mm 動き、`check_voxelize.py` [7](辺長が要素サイズの
# 整数倍の箱は体積が厳密一致)が落ちるため、名指しの免除にしてある。幾何アクセスを
# kernel/query 越しにする移行は別タスクとして残っている。
#
# `kernel_brep.py`(生成CADのBRep保存・ブーリアン・最終ソリッドの幾何検査)と
# `mesh_geometry.py`(三角形境界の接続検査と、向き・空洞を保ったCADソリッド化)は
# **カーネル層の一部**であって verb 層ではない —— `kernel.py` が2,300行あるので
# 役割ごとに分けてあるだけである。この3本の外でOCPを叩いたら落ちる。
OCP_ALLOWED = frozenset(
    {"kernel.py", "kernel_brep.py", "mesh_geometry.py", "topopt/voxelize.py"}
)
BUILD123D_ALLOWED = frozenset(
    {"query.py", "topopt/voxelize.py", "topopt/anchor.py", "topopt/design_space.py"}
)
# 「実際に import している」ことを確かめる本体側の1本(境界検査が機能する前提)
OCP_CORE = "kernel.py"

_DYNAMIC_IMPORT_CALLS = {"__import__", "import_module", "importlib.import_module"}


def _dotted_name(node: ast.AST) -> str:
    """`importlib.import_module` のような呼び出し先の名前をドット表記で復元する。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _imported_module_roots(py_file: Path) -> set[str]:
    """py_fileが参照するトップレベルモジュール名の集合。

    関数内import・条件付きimport・動的import(importlib.import_module / __import__)を含む。
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            if _dotted_name(node.func) in _DYNAMIC_IMPORT_CALLS:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        roots.add(arg.value.split(".")[0])
    return roots


def _source_files() -> list[tuple[str, Path]]:
    """duocad配下の全.pyを(パッケージ相対パス, 実パス)で返す(サブパッケージ含む)。"""
    return [
        (py.relative_to(DUOCAD_DIR).as_posix(), py)
        for py in sorted(DUOCAD_DIR.rglob("*.py"))
    ]


def test_only_kernel_module_imports_ocp():
    """duocad配下(サブパッケージ含む)でOCP.*をimportしてよいのはパッケージ直下のkernel.pyだけ
    (verbが直接OCPを叩き始めるのを防ぐ。動的importでの迂回も検出する)。"""
    violations = [
        rel
        for rel, py_file in _source_files()
        if rel not in OCP_ALLOWED and "OCP" in _imported_module_roots(py_file)
    ]
    assert violations == [], f"OCPを直接importしているファイル(免除一覧に無いものは禁止): {violations}"


def test_kernel_module_actually_imports_ocp():
    """kernel.pyは実際にOCPをimportしている(上の境界テストが機能するための前提)。"""
    assert "OCP" in _imported_module_roots(DUOCAD_DIR / OCP_CORE)


def test_only_query_module_imports_build123d():
    """verb側(サブパッケージ含む)はbuild123dを直接importしない
    (問い合わせはquery.py越しに行う。差し替え可能に保つ。動的importでの迂回も検出する)。"""
    violations = [
        rel
        for rel, py_file in _source_files()
        if rel not in BUILD123D_ALLOWED and "build123d" in _imported_module_roots(py_file)
    ]
    assert violations == [], f"build123dを直接importしているファイル(免除一覧に無いものは禁止): {violations}"


def test_boundary_scan_detects_every_import_form(tmp_path):
    """境界検査そのものが有効であること(上の2テストが使う実関数を、既知の違反ソースで検査する)。

    静的な `import OCP` だけでなく、関数内import・`from OCP.X import Y`・
    `importlib.import_module("OCP.TopoDS")`・`__import__("build123d")` も検出できなければ、
    境界は簡単に破れる。ここが落ちたら上の2テストは「通っているが何も守っていない」状態。
    """
    offender = tmp_path / "offender.py"
    offender.write_text(
        "import importlib\n"
        "\n"
        "def a():\n"
        "    import OCP.TopoDS\n"
        "\n"
        "def b():\n"
        "    from OCP.STEPControl import STEPControl_Reader\n"
        "\n"
        "def c():\n"
        "    return importlib.import_module('OCP.BRepGProp')\n"
        "\n"
        "def d():\n"
        "    return __import__('build123d')\n",
        encoding="utf-8",
    )
    roots = _imported_module_roots(offender)
    assert "OCP" in roots
    assert "build123d" in roots

    clean = tmp_path / "clean.py"
    clean.write_text("from .kernel import read_step\nimport json\n", encoding="utf-8")
    clean_roots = _imported_module_roots(clean)
    assert "OCP" not in clean_roots and "build123d" not in clean_roots


def test_boundary_scan_covers_every_source_file():
    """走査対象がサブパッケージを含む全ソースであること(免除は名指しした相対パスのみ)。

    走査が0件になっていたら上の境界テストは常に緑で何も検査していないので、
    「実ファイルを1つ以上見ている」ことと「既知の5モジュールを取りこぼしていない」ことを固定する。
    """
    scanned = {rel for rel, _ in _source_files()}
    expected = {
        "__init__.py",
        "document.py",
        "oplog.py",
        "io_step.py",
        "query.py",
        "kernel.py",
    }
    assert expected <= scanned, f"走査から漏れているモジュールがある: {sorted(expected - scanned)}"

    # 免除一覧が黙って伸びないように、中身そのものを固定する。**増やすときはここも直す** ——
    # 直すこと自体が「境界を1つ緩めた」という判断の記録になる。
    assert OCP_ALLOWED == frozenset(
        {"kernel.py", "kernel_brep.py", "mesh_geometry.py", "topopt/voxelize.py"}
    )
    assert BUILD123D_ALLOWED == frozenset(
        {"query.py", "topopt/voxelize.py", "topopt/anchor.py", "topopt/design_space.py"}
    )
    assert OCP_ALLOWED <= scanned and BUILD123D_ALLOWED <= scanned

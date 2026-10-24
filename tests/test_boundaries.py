"""境界と規律 —— 「問い合わせ層をインタフェースにする」「OCPを叩くのはカーネル層だけ」。"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

DUOCAD_DIR = Path(__file__).resolve().parent.parent / "duocad"

# 免除はここに名指しした相対パスだけ(DUOCAD_DIRからの相対パス)。**ディレクトリ単位では
# 免除しない** —— 新しく足したファイルが黙って境界の外へ出るのを防ぐため。
#
# `topopt/` は**B-repを直接読む**数値計算のサブシステム(voxelize は OCCT の厳密なbboxを、
# anchor/design_space は検証用の合成ソリッドを build123d で組む)。`kernel.bounding_box` へ
# 回すと `SetGap` の順序が違って値が 1e-7mm 動き、`check_voxelize.py` [7](辺長が要素サイズの
# 整数倍の箱は体積が厳密一致)が落ちるため、名指しの免除にしてある。
#
# `kernel_brep.py`(生成CADのBRep保存・ブーリアン・最終ソリッドの幾何検査・箱/格子との共通体積)・
# `kernel_meshing.py`(OCCTの面分割の段階制御と境界交差の再分割)・
# `mesh_geometry.py`(三角形境界の接続検査とCADソリッド化。位相の列挙と compound の定型)・
# `kernel_sketch.py`(スケッチ平面の枠・図形の辺・領域検出)・
# `kernel_split.py`(平面でのボディの分割)・
# `kernel_extrude.py`(面と領域の押し出し)・
# `kernel_project.py`(ソリッドの辺・面・外形のスケッチ平面への投影)は
# **カーネル層の一部**であって verb 層ではない —— `kernel.py` が大きいので
# 役割ごとに分けてあるだけである。この一覧の外でOCPを叩いたら落ちる。
OCP_ALLOWED = frozenset(
    {
        "kernel.py",
        "kernel_brep.py",
        "kernel_meshing.py",
        "kernel_sketch.py",
        "kernel_extrude.py",
        "kernel_chains.py",
        "kernel_overflow.py",
        "kernel_tight.py",
        "kernel_tight_surface.py",
        "kernel_shell.py",
        "kernel_thread.py",
        "kernel_face_edit.py",
        "kernel_worker.py",
        "kernel_project.py",
        "kernel_split.py",
        "kernel_interference.py",
        "mesh_geometry.py",
        "brep_topology.py",
        "brep_geometry.py",
        "brep_checks.py",
        "brep_history.py",
        "brep_operations.py",
        "brep_planar.py",
        "topopt/voxelize.py",
    }
)
BUILD123D_ALLOWED = frozenset({"query.py"})
# 「実際に import している」ことを確かめる本体側の1本(境界検査が機能する前提)
OCP_CORE = "kernel.py"

_DYNAMIC_IMPORT_CALLS = {"__import__", "import_module", "importlib.import_module"}

BREP_DEPENDENCIES = {
    "brep_topology.py": set(),
    "brep_geometry.py": {"brep_topology"},
    "brep_checks.py": {"brep_topology", "brep_geometry"},
    "brep_history.py": {"brep_topology"},
    "brep_operations.py": {"brep_topology", "brep_geometry", "brep_checks"},
    "brep_planar.py": {"brep_topology"},
    "mesh_geometry.py": {"brep_topology", "brep_checks"},
}
BREP_COMMON = tuple(BREP_DEPENDENCIES)


def _dotted_name(node: ast.AST) -> str:
    """`importlib.import_module` のような呼び出し先の名前をドット表記で復元する。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _imported_modules(py_file: Path, *, include_type_checking: bool = True) -> set[str]:
    """静的・動的importのモジュール名と、from節で取り込む名前の集合。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    type_only_nodes = set() if include_type_checking else {
        node
        for guard in ast.walk(tree)
        if isinstance(guard, ast.If) and _dotted_name(guard.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}
        for statement in guard.body
        for node in ast.walk(statement)
    }
    modules: set[str] = set()
    for node in ast.walk(tree):
        if node in type_only_nodes:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}" if node.module else alias.name)
        elif isinstance(node, ast.Call):
            if _dotted_name(node.func) in _DYNAMIC_IMPORT_CALLS:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        modules.add(arg.value.lstrip("."))
    return modules


def _imported_module_roots(py_file: Path) -> set[str]:
    """py_fileが参照するトップレベルモジュール名の集合。"""
    return {name.split(".")[0] for name in _imported_modules(py_file)}


def _kernel_operation_imports(py_file: Path) -> set[str]:
    return {
        name for name in _imported_modules(py_file)
        if any(part == "kernel" or part.startswith("kernel_") for part in name.split("."))
    }


@pytest.mark.parametrize("module", BREP_COMMON)
def test_common_brep_modules_do_not_import_operations(module):
    """共通幾何機構が個別操作を参照すると、依存が逆流する。"""
    assert _kernel_operation_imports(DUOCAD_DIR / module) == set()


@pytest.mark.parametrize("module", BREP_COMMON)
def test_common_brep_dependency_direction(module):
    """共通層の参照先が増えると、層の順序や循環の有無を見直す必要がある。"""
    common = {Path(name).stem for name in BREP_COMMON}
    imported = {
        part for name in _imported_modules(DUOCAD_DIR / module)
        for part in name.split(".") if part in common
    }
    assert imported <= BREP_DEPENDENCIES[module]


def test_operations_do_not_import_common_functions_as_compatibility_aliases():
    """共通関数を操作モジュールへ再輸出すると、旧い差し替え点が復活する。"""
    common = {Path(name).stem for name in BREP_COMMON}
    violations = []
    for path in [*DUOCAD_DIR.glob("kernel*.py"), DUOCAD_DIR / "mesh_geometry.py"]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] in common:
                violations.append((path.name, node.lineno))
    assert violations == []


def _private_operation_references(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = (
                    f"{node.module}.{alias.name}" if node.module else alias.name
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )

    def resolved(node):
        first, *rest = _dotted_name(node).split(".")
        return ".".join([aliases.get(first, first), *rest])

    references = set(_imported_modules(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            references.add(resolved(node))
        elif isinstance(node, ast.Call) and _dotted_name(node.func) == "getattr" and len(node.args) > 1:
            if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                references.add(f"{resolved(node.args[0])}.{node.args[1].value}")
    return {
        name for name in references if name.split(".")[-1].startswith("_")
        and any(part == "kernel" or part.startswith("kernel_") for part in name.split("."))
        # 同じ曲率半径超えフィレットを分割した2ファイルなので、この向きの内部参照だけ許す。
        and not (path.name == "kernel_tight_surface.py"
                 and name.removeprefix("duocad.").rsplit(".", 1)[0] == "kernel_tight")
    }


def test_operations_do_not_share_private_operation_helpers():
    """操作間の内部関数共有は、共通層を経由しなければならない。"""
    assert {
        path.name: refs for path in DUOCAD_DIR.glob("kernel*.py")
        if (refs := _private_operation_references(path))
    } == {}


@pytest.mark.parametrize("source", [
    "from .kernel import _shared",
    "from duocad.kernel_overflow import _shared as helper",
    "from . import kernel as operation; operation._shared()",
    "import duocad.kernel; duocad.kernel._shared()",
    "import duocad.kernel as operation; operation._shared()",
    "from . import kernel; getattr(kernel, '_shared')",
])
def test_operation_helper_boundary_detects_aliases(tmp_path, source):
    """別名・完全修飾・属性文字列で、操作間の内部参照の検査を迂回できない。"""
    path = tmp_path / "operation.py"
    path.write_text(source, encoding="utf-8")
    assert _private_operation_references(path)


@pytest.mark.parametrize(("filename", "source", "allowed"), [
    ("kernel_tight_surface.py", "from . import kernel_tight as tight; tight._shared()", True),
    ("kernel_tight_surface.py", "from duocad.kernel_tight import _shared", True),
    ("kernel_tight_surface.py", "import duocad.kernel_tight as tight; tight._shared()", True),
    ("kernel_tight_surface.py", "from .kernel_overflow import _shared", False),
    ("kernel_tight.py", "from .kernel_tight_surface import _shared", False),
    ("kernel_overflow.py", "from .kernel_tight import _shared", False),
])
def test_operation_helper_exception_is_only_for_the_split_tight_operation(tmp_path, filename, source, allowed):
    """同じ操作の分割の例外を、別操作や逆向きの内部参照へ広げない。"""
    path = tmp_path / filename
    path.write_text(source, encoding="utf-8")
    assert bool(_private_operation_references(path)) is not allowed


@pytest.mark.parametrize("source", [
    "import duocad.kernel",
    "from .kernel import volume",
    "from . import kernel",
    "from duocad import kernel_overflow",
    "from .kernel_tight import Tight",
    "import importlib; importlib.import_module('duocad.kernel_extrude')",
    "__import__('duocad.kernel_face_edit')",
])
def test_common_brep_boundary_detects_operation_import_forms(tmp_path, source):
    """関数内・相対・動的 import でも共通層の境界を迂回できない。"""
    offender = tmp_path / "offender.py"
    offender.write_text("def load():\n    " + source + "\n", encoding="utf-8")
    assert _kernel_operation_imports(offender)


def test_brep_analyzer_is_confined_to_shape_checks():
    """操作ごとの例外と呼び出し順を残し、BRepCheck の本体だけを検査層へ集める。"""
    sources = [*DUOCAD_DIR.glob("kernel*.py"), *(DUOCAD_DIR / name for name in BREP_COMMON)]
    assert [
        path.name for path in sources
        if path.name != "brep_checks.py"
        and "OCP.BRepCheck.BRepCheck_Analyzer" in _imported_modules(path)
    ] == []


def _imports_optimization_executor(py_file: Path, *, allow_plan_type_imports: bool = False) -> bool:
    modules = {"optimize_run", "optimize_acceptance", "optimize_pipeline", "optimize_plan", "optimize_finish", "optimize_events"}
    forbidden = modules | {f"duocad.{name}" for name in modules}
    imported = _imported_modules(py_file)
    if allow_plan_type_imports:
        plan_modules = {"optimize_plan", "duocad.optimize_plan"}
        imported = {
            name for name in imported
            if not any(name == module or name.startswith(module + ".") for module in plan_modules)
        } | _imported_modules(py_file, include_type_checking=False)
    return any(name == module or name.startswith(module + ".")
               for name in imported for module in forbidden)


def _source_files() -> list[tuple[str, Path]]:
    """duocad配下の全.pyを(パッケージ相対パス, 実パス)で返す(サブパッケージ含む)。"""
    return [
        (py.relative_to(DUOCAD_DIR).as_posix(), py)
        for py in sorted(DUOCAD_DIR.rglob("*.py"))
    ]


def test_only_kernel_module_imports_ocp():
    """duocad配下(サブパッケージ含む)でOCP.*をimportしてよいのは `OCP_ALLOWED` に名指しした
    カーネル層だけ(verbが直接OCPを叩き始めるのを防ぐ。動的importでの迂回も検出する)。"""
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
    """走査対象がサブパッケージを含む全ソースであること(免除は名指しした相対パスのみ)。"""
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
    assert OCP_ALLOWED <= scanned and BUILD123D_ALLOWED <= scanned


def test_numerics_and_steps_do_not_import_optimization_executor():
    """数値層と表示処理は実行器を参照せず、ステップ記録は計画の型注釈だけを参照する。"""
    violations = [
        rel for rel, py_file in _source_files()
        if (rel.startswith("topopt/") or rel in {"optimize_steps.py", "optimize_voxels.py"})
        and _imports_optimization_executor(py_file, allow_plan_type_imports=(rel == "optimize_steps.py"))
    ]
    assert violations == [], f"実行器側へ逆参照しているファイル: {violations}"


def test_executor_boundary_scan_detects_every_import_form(tmp_path):
    """関数内・相対・from節・動的importでも実行器への参照を検出する。"""
    source = tmp_path / "source.py"
    imports = [
        "import duocad.optimize_run",
        "from duocad.optimize_run import build_conditions",
        "from duocad import optimize_run",
        "from .optimize_run import DEBUG_VOXEL_LIMIT",
        "from .. import optimize_run",
        "importlib.import_module('duocad.optimize_run')",
        "__import__('duocad.optimize_run')",
        "import_module('.optimize_run', 'duocad')",
        "from duocad.optimize_acceptance import inspect_document",
        "from duocad.optimize_pipeline import execute",
        "import duocad.optimize_plan",
        "from duocad.optimize_plan import FinishPlan",
        "from duocad import optimize_plan",
        "from .optimize_plan import FinishPlan",
        "from .. import optimize_plan",
        "importlib.import_module('duocad.optimize_plan')",
        "__import__('duocad.optimize_plan')",
        "import_module('.optimize_plan', 'duocad')",
        "from .optimize_finish import finish_shape",
        "from duocad import optimize_events",
    ]
    for statement in imports:
        source.write_text(f"def load():\n    {statement}\n", encoding="utf-8")
        assert _imports_optimization_executor(source), statement
        assert _imports_optimization_executor(source, allow_plan_type_imports=True), statement
    source.write_text("from .optimize_voxels import debug_voxel_payload\n", encoding="utf-8")
    assert not _imports_optimization_executor(source)


def test_executor_boundary_allows_only_type_checking_plan_imports(tmp_path):
    """計画の型参照の許可は、TYPE_CHECKING の本体だけに限り、他の実行器には広げない。"""
    source = tmp_path / "source.py"
    for header, guard in [
        ("from typing import TYPE_CHECKING", "TYPE_CHECKING"),
        ("import typing", "typing.TYPE_CHECKING"),
    ]:
        source.write_text(
            f"{header}\nif {guard}:\n    from duocad.optimize_plan import FinishPlan\n",
            encoding="utf-8",
        )
        assert _imports_optimization_executor(source)
        assert not _imports_optimization_executor(source, allow_plan_type_imports=True)

        source.write_text(
            f"{header}\nif {guard}:\n    from .optimize_plan import FinishPlan\n"
            "else:\n    from .optimize_plan import FinishPlan\n",
            encoding="utf-8",
        )
        assert _imports_optimization_executor(source, allow_plan_type_imports=True)

    source.write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
        "    from .optimize_finish import finish_shape\n",
        encoding="utf-8",
    )
    assert _imports_optimization_executor(source, allow_plan_type_imports=True)


@pytest.mark.parametrize(("relative_path", "type_only", "allowed"), [
    ("topopt/offender.py", False, False),
    ("topopt/offender.py", True, False),
    ("optimize_steps.py", False, False),
    ("optimize_steps.py", True, True),
    ("optimize_voxels.py", False, False),
    ("optimize_voxels.py", True, False),
])
def test_executor_boundary_applies_type_import_exception_only_to_steps(
    tmp_path, monkeypatch, relative_path, type_only, allowed,
):
    """一時ソースでも実際の境界検査を通し、計画の型参照の免除先をステップ記録だけに限る。"""
    source = tmp_path / relative_path
    source.parent.mkdir(parents=True, exist_ok=True)
    statement = "from duocad.optimize_plan import FinishPlan\n"
    code = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    " + statement if type_only else statement
    source.write_text(code, encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.DUOCAD_DIR", tmp_path)
    if allowed:
        test_numerics_and_steps_do_not_import_optimization_executor()
    else:
        with pytest.raises(AssertionError, match=relative_path):
            test_numerics_and_steps_do_not_import_optimization_executor()

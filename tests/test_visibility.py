"""可視性のverbと、**可視性が選択機構の一部であること**の契約。

PCケース(27部品・2835面)の実機テストではピック失敗12回/押し出し7回(失敗率63%)の原因が
**手前の部品が奥の面を隠していた**ことだった。可視性は見た目の設定ではなく**何を指せるかの
定義**であり、見えないものは指せないことをコアで保証する。

ShapeRef の形は `{"body": BodyId, "kind": "face", "index": int, "provenance": None}`。
"""
from __future__ import annotations

# `_source_files` / `_imported_module_roots` はM0の境界検査の実体(test_sketch.py・
# test_parameters.py と同じ流儀で借りる。tests/ は sys.path に入っている)。
from test_boundaries import DUOCAD_DIR, _imported_module_roots, _source_files

from duocad import kernel, selection
from duocad.document import Document
from duocad.io_step import import_step
from duocad.verbs import extrude_face, make_editable_copy, set_visible

#: M3.5のモジュール(import境界の走査対象に入っていることを固定する)
M35_MODULES = ("selection.py", "verbs/visibility.py")

#: 押し出し量(mm)。符号は正＝外向き法線方向へ足す。
EXTRUDE_MM = 1.0


# --------------------------------------------------------------------------------------
# 境界(M2・M3と同じ理由。新規ファイルが走査から漏れていないこと自体を固定する)
# --------------------------------------------------------------------------------------
def test_m35_modules_are_covered_by_the_import_boundary_scan():
    """M3.5の新規ファイルにもOCP/build123dのimport境界が効いていること。"""
    scanned = {rel for rel, _ in _source_files()}
    assert set(M35_MODULES) <= scanned, (
        f"走査から漏れている: {sorted(set(M35_MODULES) - scanned)}"
    )
    for rel in M35_MODULES:
        roots = _imported_module_roots(DUOCAD_DIR / rel)
        assert "OCP" not in roots, f"{rel} がOCPを直接importしている(kernel.py越しに呼ぶこと)"
        assert "build123d" not in roots, f"{rel} がbuild123dを直接importしている(query.py越し)"

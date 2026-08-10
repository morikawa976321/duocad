"""OCP(OCCT)を直接叩く層(実装計画書 §2 アーキテクチャ、§6予防4)。

**minicad配下でOCP.*をimportしてよいのはこのモジュールだけ**
(tests/test_boundaries.py が機械的に検査する。verbが直接OCPを叩き始めるのを防ぐための境界)。

M0の契約:
    read_step(path) -> TopoDS_Shape
        STEPファイルを読み、形状を返す。**ReadFile()の戻り値を必ず先に検査し**、
        失敗(IFSelect_RetDone以外)なら TransferRoots()/OneShape() を呼ぶ前に例外を送出すること。
        理由: 失敗した状態のままこれらを呼ぶと、Python例外ではなく**プロセスごと落ちる**
        (Windowsで 0xC0000005。パス無し=IFSelect_RetError / 中身が壊れている=IFSelect_RetFail の
        どちらでも同じ。2026-08-10実測・監査で再現確認。io_step.py のモジュールdocstring参照)。
    write_step(shape, path) -> None
    write_stl(shape, path, deviation) -> None
        deviation は**絶対**の線形偏差(mm)。BRepMesh_IncrementalMesh に isRelative=False で渡す
        (isRelative=Trueだと偏差を変えてもメッシュが変わらない。2026-08-10監査で実測)。
"""
from __future__ import annotations

from OCP.TopoDS import TopoDS_Shape  # noqa: F401  境界(OCPの実importはここだけ)を明示するための型


def read_step(path: str) -> "TopoDS_Shape":
    """STEPファイルを読み、TopoDS_Shapeを返す。失敗時は例外を送出する(黙って失敗しない)。"""
    raise NotImplementedError


def write_step(shape: "TopoDS_Shape", path: str) -> None:
    """形状をSTEPとして書き出す。"""
    raise NotImplementedError


def write_stl(shape: "TopoDS_Shape", path: str, deviation: float) -> None:
    """形状をSTLとしてテセレーションして書き出す。"""
    raise NotImplementedError

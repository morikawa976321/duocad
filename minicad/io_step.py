"""STEP読み込み・STEP/STL書き出し(実装計画書 §5 入出力、§7 落とし穴、§8 M0検証)。

M0の契約:
- import_step(doc, path) -> BodyId
    STEPを kind="reference" のボディとして doc.bodies へ追加し、
    doc.log に verb="import_step", args={"path": str(path), "body_id": <新BodyId>} を積んで
    そのBodyIdを返す。
- export_step(doc, path) -> None
    doc.active_body() の形状をSTEPとして書き出す。
- export_stl(doc, path, deviation) -> None
    doc.active_body() の形状をSTLとして書き出す。deviation(mm)は**絶対**の線形偏差。

失敗時の契約(黙って通さない。実装計画書 §6予防6):
- パスが存在しない            -> FileNotFoundError
- 存在するがSTEPとして壊れている -> ValueError
- 書き出す形状が無い(空の文書) -> ValueError。**ファイルを作らない**
いずれも doc.bodies / doc.log に変更を残さないこと。

**重要 — 実測済みの危険な落とし穴(2026-08-10・監査で再現確認)**:
OCPの STEPControl_Reader は ReadFile() が IFSelect_RetDone 以外を返した状態のまま
TransferRoots() を呼ぶと、Python例外にはならず **プロセスごと落ちる**
(Windowsで終了コード 0xC0000005 = アクセス違反)。存在しないパス(IFSelect_RetError)でも
中身が壊れたファイル(IFSelect_RetFail)でも同じ。これは実装計画書 §7 が警告する
「OCCTの操作は例外を投げず黙って失敗する」の中でも最悪の形(例外どころかプロセスが死ぬ)。
import_step の実装は **必ず ReadFile() の戻り値を先に検査し、失敗なら TransferRoots()/OneShape()
を呼ぶ前に例外を送出すること**。`Path.exists()` のチェックだけでは壊れたファイルを防げない。
詳細は tests/README.md 参照。

**deviation の意味**: BRepMesh_IncrementalMesh へ **isRelative=False**(絶対mm)で渡すこと。
isRelative=True にすると 0.2 / 0.05 / 0.01 のどれでも同じサイズのSTLになり、
「偏差を小さくすると細かくなる」という契約が成立しない(2026-08-10監査で実測)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

from .document import BodyId, Document

PathLike = Union[str, Path]


def import_step(doc: Document, path: PathLike) -> BodyId:
    """STEPを参照ボディとして読み込み、doc.bodiesへ追加してdoc.logに記録し、BodyIdを返す。

    失敗時は例外を送出し、documentにもログにも変更を残さないこと
    (パス無し=FileNotFoundError / 中身が壊れている=ValueError)。
    """
    raise NotImplementedError


def export_step(doc: Document, path: PathLike) -> None:
    """doc.active_body() の形状をSTEPとして書き出す。読み戻すと面数・体積が一致することが契約。

    書き出す形状が無ければ ValueError を送出し、ファイルを作らない。
    """
    raise NotImplementedError


def export_stl(doc: Document, path: PathLike, deviation: float) -> None:
    """doc.active_body() の形状をSTLとして書き出す。deviationが小さいほどファイルは大きくなる。

    deviation は**絶対**の線形偏差(mm)。BRepMesh_IncrementalMesh には isRelative=False で渡す。
    書き出す形状が無ければ ValueError を送出し、ファイルを作らない。
    """
    raise NotImplementedError

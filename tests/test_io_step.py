"""M0契約: STEP読み込み・STEP/STL書き出し(実装計画書 §7 落とし穴、§8 M0検証)。

face構成(56/34/22/0/0/0)は shelf_attachment_GD.step を実際にOCPで走査して実測済み
(BRepAdaptor_Surfaceの面種別 + 円筒のUVBounds U範囲>350度で全周判定)。
STEP往復の体積比較・STL偏差とファイルサイズの関係も同じ環境(cadquery-ocp-novtk 7.9.3.1.1 /
build123d 0.11.1)で実測しており、下記の許容誤差・アサーションは推測ではなく実測に基づく。
"""
from __future__ import annotations

import math

import pytest

from duocad.document import Document
from duocad.io_step import export_step, export_stl, import_step
from duocad.query import describe_shape, volume

def test_import_step_missing_file_raises_and_does_not_mutate_document(tmp_path):
    """存在しないSTEPパスはFileNotFoundErrorを送出し、documentにもログにも変更が残らない
    (失敗を黙って通さない。実装計画書§7・§6予防6)。

    実装時の注意: OCPのSTEPControl_Readerは、
    ReadFile()がIFSelect_RetDone以外を返した状態のまま TransferRoots()/OneShape() を呼ぶと、
    Python例外にならずプロセスごと落ちる(Windowsで終了コード0xC0000005 = アクセス違反)。
    import_stepの実装はReadFile()の戻り値を必ず先に検査し、失敗ならTransferRoots()を呼ぶ前に
    raiseすること(詳細はtests/README.md)。
    """
    doc = Document()
    bad_path = tmp_path / "does_not_exist.step"

    with pytest.raises(FileNotFoundError):
        import_step(doc, bad_path)

    assert doc.bodies == []
    assert doc.log.entries == []


def test_import_step_malformed_file_raises_value_error_and_does_not_mutate_document(tmp_path):
    """STEPとして壊れているファイル(存在はする)はValueErrorを送出し、documentにもログにも
    変更が残らない(失敗を黙って通さない。実装計画書§6予防6・§7)。

    このテストが上の「存在しないパス」のテストと別に要るのは、**存在チェックだけでは
    ReadFile()の戻り値検査を強制できない**ため。`Path.exists()` を見るだけの実装は上のテストを
    通してしまうが、中身が壊れたファイルではReadFile()がIFSelect_RetFailを返し、そのまま
    TransferRoots()を呼ぶと**プロセスごと落ちる**(存在しないパスは
    IFSelect_RetError、壊れた中身はIFSelect_RetFail、いずれもTransferRoots()で0xC0000005)。
    ReadFile()の戻り値を必ず検査させるのはこのテストの役目。
    """
    doc = Document()
    broken = tmp_path / "broken.step"
    broken.write_text("this is not a STEP file at all\njust plain text\n", encoding="utf-8")

    with pytest.raises(ValueError):
        import_step(doc, broken)

    assert doc.bodies == []
    assert doc.log.entries == []


def test_export_from_empty_document_raises_and_writes_no_file(tmp_path):
    """書き出す形状が無い文書からのexportは例外になり、半端なファイルを残さない
    (失敗を黙って通さない。実装計画書§6予防6)。

    黙って0バイトのSTEP/STLを吐くと、壊れた成果物が「成功」として残る。
    """
    doc = Document()
    step_path = tmp_path / "empty.step"
    stl_path = tmp_path / "empty.stl"

    with pytest.raises(ValueError):
        export_step(doc, step_path)
    assert not step_path.exists()

    with pytest.raises(ValueError):
        export_stl(doc, stl_path, 0.1)
    assert not stl_path.exists()



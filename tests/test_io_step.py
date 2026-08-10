"""M0契約: STEP読み込み・STEP/STL書き出し(実装計画書 §7 落とし穴、§8 M0検証)。

face構成(56/34/22/0/0/0)は shelf_attachment_GD.step を実際にOCPで走査して2026-08-10に実測済み
(BRepAdaptor_Surfaceの面種別 + 円筒のUVBounds U範囲>350度で全周判定)。
STEP往復の体積比較・STL偏差とファイルサイズの関係も同日に同じ環境(cadquery-ocp-novtk 7.9.3.1.1 /
build123d 0.11.1)で実測しており、下記の許容誤差・アサーションは推測ではなく実測に基づく。
"""
from __future__ import annotations

import math

import pytest

from duocad.document import Document
from duocad.io_step import export_step, export_stl, import_step
from duocad.query import describe_shape, volume

STEP_PATH = r"C:\Users\tm976\mf\3d-models\shelf_attachment_GD.step"


def test_import_step_face_breakdown_matches_measured_reference():
    """shelf_attachment_GD.step を読むと17個のBodyに分解される。各Bodyの内訳を合計すると
    面56/平面34/全周円筒22/トーラス0/円錐0/spline0 になる(2026-08-10 OCPで直接実測した値)。"""
    doc = Document()
    body_ids = import_step(doc, STEP_PATH)
    assert len(body_ids) == 17

    totals = {
        "plane": 0, "cylinder_full": 0, "cylinder_partial": 0,
        "torus": 0, "cone": 0, "bspline": 0, "other": 0,
    }
    face_count_total = 0
    for body in doc.bodies:
        desc = describe_shape(body.shape)
        face_count_total += desc["face_count"]
        for kind, count in desc["faces_by_type"].items():
            totals[kind] += count

    assert face_count_total == 56
    assert totals["plane"] == 34
    assert totals["cylinder_full"] == 22
    assert totals["cylinder_partial"] == 0
    assert totals["torus"] == 0
    assert totals["cone"] == 0
    assert totals["bspline"] == 0
    assert sum(totals.values()) == face_count_total


def test_import_step_creates_reference_body():
    """import_stepで読み込んだ全ボディのkindは"reference"(読み取り専用)である。"""
    doc = Document()
    body_ids = import_step(doc, STEP_PATH)
    assert body_ids
    assert all(b.kind == "reference" for b in doc.bodies)


def test_import_step_is_logged_and_undoable():
    """import_stepの読み込みは操作ログに1件載り、undoで全ボディが消える。"""
    doc = Document()
    import_step(doc, STEP_PATH)

    assert len(doc.log.entries) == 1
    assert doc.log.entries[0].verb == "import_step"

    doc.log.undo(doc)
    assert doc.bodies == []


def test_import_step_logs_path_and_body_id_in_args():
    """import_stepが積む操作のargsは{"path":.., "body_ids": [返した全BodyId]}を持つ
    (tests/README.md記載のundo/redo/replayの契約が依存する具体的なargs形を、テスト側でも縛る)。"""
    doc = Document()
    body_ids = import_step(doc, STEP_PATH)

    op = doc.log.entries[0]
    assert op.args["path"] == str(STEP_PATH)
    assert op.args["body_ids"] == body_ids


def test_import_step_missing_file_raises_and_does_not_mutate_document(tmp_path):
    """存在しないSTEPパスはFileNotFoundErrorを送出し、documentにもログにも変更が残らない
    (失敗を黙って通さない。実装計画書§7・§6予防6)。

    実装時の注意(2026-08-10実測・2026-08-10監査で再現確認): OCPのSTEPControl_Readerは、
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
    TransferRoots()を呼ぶと**プロセスごと落ちる**(2026-08-10監査で実測。存在しないパスは
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


def test_export_step_roundtrip_preserves_volume(tmp_path):
    """読んだ全ボディ(分解後)の体積の合計をexport_step→読み戻すと一致する。

    M3.5でimport_stepがソリッドごとに分解するようになったため、doc.active_body()だけを
    見ていた旧テストは1ソリッド(3〜4面)しか見なくなってしまう。export_step/export_stlは
    body_ids省略時に「形状を持つ全Body」をcompoundにして書き出す設計にしたので(io_step.py)、
    体積の合計は分解前と変わらず比較できる。
    """
    doc = Document()
    import_step(doc, STEP_PATH)
    original_volume = sum(volume(b.shape) for b in doc.bodies)

    out_path = tmp_path / "roundtrip.step"
    export_step(doc, out_path)

    doc2 = Document()
    import_step(doc2, out_path)
    roundtrip_volume = sum(volume(b.shape) for b in doc2.bodies)

    assert math.isclose(original_volume, roundtrip_volume, rel_tol=1e-6)


def test_export_step_roundtrip_preserves_face_count(tmp_path):
    """読んだ全ボディ(分解後)の面数の合計をexport_step→読み戻すと一致する(56面のまま)。"""
    doc = Document()
    import_step(doc, STEP_PATH)

    out_path = tmp_path / "roundtrip.step"
    export_step(doc, out_path)

    doc2 = Document()
    import_step(doc2, out_path)

    original_faces = sum(describe_shape(b.shape)["face_count"] for b in doc.bodies)
    roundtrip_faces = sum(describe_shape(b.shape)["face_count"] for b in doc2.bodies)
    assert original_faces == roundtrip_faces == 56


def test_export_stl_smaller_deviation_yields_larger_file(tmp_path):
    """deviationを指定でき、偏差を小さくするとファイルが大きくなる。

    2026-08-10実測(監査で独立に再現。この環境・このファイルでの参考値。厳密一致は求めない):
    バイナリSTLで 0.2mm→112084B(109.5KB) / 0.05mm→143884B(140.5KB) / 0.01mm→302684B(295.6KB)。
    絶対値は環境・実装で変わりうるため、ここで縛るのは「偏差を下げるとファイルサイズが
    単調に増える」という関係だけ(実装計画書§7の 30.7/47.2/170.6KB は別形状での実測で、
    この形状ではバイナリ・ASCIIどちらとも一致しない。ASCIIだと569/730/1537KB)。

    **実装上の落とし穴(2026-08-10監査で実測)**: deviationは**絶対**の線形偏差(mm)として
    BRepMesh_IncrementalMeshに渡すこと(isRelative=False)。isRelative=Trueにすると
    0.2/0.05/0.01のいずれでも同じ112084Bになり、**このテストは必ず落ちる**。
    build123dの `export_stl` は内部で isRelative=True を使うので、そのまま流用してはいけない。
    """
    doc = Document()
    import_step(doc, STEP_PATH)

    sizes = {}
    for deviation in (0.2, 0.05, 0.01):
        out_path = tmp_path / f"out_{deviation}.stl"
        export_stl(doc, out_path, deviation)
        sizes[deviation] = out_path.stat().st_size

    assert sizes[0.2] < sizes[0.05] < sizes[0.01]


def test_export_stl_works_from_reference_body(tmp_path):
    """参照ボディ(kind="reference")からもSTL書き出しができる。"""
    doc = Document()
    import_step(doc, STEP_PATH)
    assert doc.active_body().kind == "reference"

    out_path = tmp_path / "ref.stl"
    export_stl(doc, out_path, 0.1)
    assert out_path.exists()
    assert out_path.stat().st_size > 0
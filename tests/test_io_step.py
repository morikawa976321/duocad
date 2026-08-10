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
    """shelf_attachment_GD.step を読むと 面56/平面34/全周円筒22/トーラス0/円錐0/spline0 になる
    (2026-08-10 OCPで直接実測した値。cad_v1_implementation_plan_2026-08-10.md §0/§7と一致)。"""
    doc = Document()
    body_id = import_step(doc, STEP_PATH)
    body = next(b for b in doc.bodies if b.id == body_id)

    desc = describe_shape(body.shape)
    by_type = desc["faces_by_type"]
    assert desc["face_count"] == 56
    assert by_type["plane"] == 34
    assert by_type["cylinder_full"] == 22
    assert by_type["cylinder_partial"] == 0
    assert by_type["torus"] == 0
    assert by_type["cone"] == 0
    assert by_type["bspline"] == 0
    assert by_type["other"] == 0
    # 内訳の合計がface_countと一致すること(数え漏れ・二重計上・未知キー混入を検出できる)
    assert sum(by_type.values()) == desc["face_count"]


def test_import_step_creates_reference_body():
    """import_stepで読み込んだボディのkindは"reference"(読み取り専用)である。"""
    doc = Document()
    body_id = import_step(doc, STEP_PATH)
    body = next(b for b in doc.bodies if b.id == body_id)
    assert body.kind == "reference"


def test_import_step_is_logged_and_undoable():
    """import_stepの読み込みは操作ログに1件載り、undoでボディが消える。"""
    doc = Document()
    import_step(doc, STEP_PATH)

    assert len(doc.log.entries) == 1
    assert doc.log.entries[0].verb == "import_step"

    doc.log.undo(doc)
    assert doc.bodies == []


def test_import_step_logs_path_and_body_id_in_args():
    """import_stepが積む操作のargsは{"path": <読み込んだパス>, "body_id": <返したBodyId>}を持つ
    (tests/README.md記載のundo/redo/replayの契約が依存する具体的なargs形を、テスト側でも縛る)。"""
    doc = Document()
    body_id = import_step(doc, STEP_PATH)

    op = doc.log.entries[0]
    assert op.args["path"] == str(STEP_PATH)
    assert op.args["body_id"] == body_id


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
    """読んだ形状をexport_step→読み戻すと体積が一致する。

    2026-08-10実測(監査で独立に再現): shelf_attachment_GD.step(56面・体積91018.2223598035)を
    この環境でexport_step→再読込すると 91018.22235980343 になり、差は約7.276e-11
    (相対約8.0e-16、IEEE double 1ULP級)で、bit-exactな==は成立しなかった。2回試行しても
    同じ差(浮動小数点の積分順序に由来し、幾何欠損ではない)。同じファイルを2回読んだ体積は
    完全一致するので、差はSTEP往復そのものが原因である。
    実装計画書§7の「差 0.00e+00」は26面・体積23340.849375の**別形状**での実測であり、
    この受入形状には当てはまらない(§8の「体積が完全一致」も同様に文字どおりには成立しない)。
    そのため比較は相対誤差1e-6(実測誤差の約10億倍のマージン)で行う。1e-6を超える差は
    面の欠落・重複など実際のジオメトリ破損を意味するので、この閾値は緩めていない。
    """
    doc = Document()
    import_step(doc, STEP_PATH)
    original_volume = volume(doc.active_body().shape)

    out_path = tmp_path / "roundtrip.step"
    export_step(doc, out_path)

    doc2 = Document()
    import_step(doc2, out_path)
    roundtrip_volume = volume(doc2.active_body().shape)

    assert math.isclose(original_volume, roundtrip_volume, rel_tol=1e-6)


def test_export_step_roundtrip_preserves_face_count(tmp_path):
    """読んだ形状をexport_step→読み戻すと面数が一致する(56面のまま)。"""
    doc = Document()
    import_step(doc, STEP_PATH)

    out_path = tmp_path / "roundtrip.step"
    export_step(doc, out_path)

    doc2 = Document()
    import_step(doc2, out_path)

    original_faces = describe_shape(doc.active_body().shape)["face_count"]
    roundtrip_faces = describe_shape(doc2.active_body().shape)["face_count"]
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

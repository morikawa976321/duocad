"""STEP/STL の書き出しが、曲面を持つ部品(穴・フィレット・面取り・複数ボディ)でも形を保つ。

落ちたら: 書き出したファイルを読み直した形が、文書のボディと違う(体積・面の数と種別)か、STL が閉じていない。
直方体だけの照合は `test_io_step.py`、三角形の一致は `test_stl_export.py`。
"""
from __future__ import annotations

import struct
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from duocad import io_step, kernel, query
from duocad.document import Document
from duocad.region import region_ref
from duocad.sketch import add_circle, add_rectangle, base_plane, sketch_create
from duocad.verbs import chamfer, extrude, fillet
from duocad.verbs.import_editable import import_step
from document_support import edges_of_kind, kinds_of, new_box, new_box_with_hole

from duocad import brep_topology as _brep_topology


def _filleted_box_with_hole(doc: Document) -> str:
    """貫通穴の立方体(円筒面1・平面6)のまっすぐな辺を1本丸めた部品。"""
    body_id = new_box_with_hole(doc, 20.0, 4.0)
    straight = edges_of_kind(doc, body_id, "line")
    fillet(doc, straight[:1], 3.0, body_id=body_id)
    return body_id


def _chamfered_box_with_hole(doc: Document) -> str:
    """x=50 に立てた別ボディ(12mm 立方体の貫通穴の縁を1本面取り)。"""
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_rectangle(doc, sketch_id, (50.0, 0.0), 12.0, 12.0)
    body_id = extrude(doc, [region_ref(sketch_id, 1)], 12.0, mode="new").args["body_id"]
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (56.0, 6.0), 6.0)
    extrude(doc, [region_ref(sketch_id, 1)], 12.0, mode="cut", body_id=body_id)
    circular = edges_of_kind(doc, body_id, "circle")
    chamfer(doc, circular[:1], 1.0, body_id=body_id)
    return body_id


def _fingerprint(doc: Document, body_id: str) -> tuple:
    body = doc.get_body(body_id)
    return (_brep_topology.face_count(body.shape), kinds_of(doc, body_id), query.volume(body.shape))


def _curved_document() -> tuple[Document, list[str]]:
    doc = Document()
    first = _filleted_box_with_hole(doc)
    second = _chamfered_box_with_hole(doc)
    assert len(doc.bodies) == 2
    return doc, [first, second]


def _read_stl_vertices(path: Path) -> np.ndarray:
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0]
    assert len(data) == 84 + 50 * count, "宣言した三角形数とファイル長が合わない"
    record = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", 9), ("attr", "<u2")])
    return np.frombuffer(data, dtype=record, count=count, offset=84)["vertices"].reshape(-1, 3, 3).astype(np.float64)


def _mesh_volume(triangles: np.ndarray) -> float:
    """向きつきの四面体の体積の総和(外向きの法線で巻かれた閉じた面なら、囲む体積)。"""
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def test_a_curved_part_exported_to_step_reads_back_with_the_same_volume_faces_and_kinds(tmp_path: Path) -> None:
    """穴+フィレットと穴+面取りの2ボディを書き出して読み直すと、ボディごとの体積・面数・面の種別が元と一致する。"""
    doc, (first, second) = _curved_document()
    before = [_fingerprint(doc, first), _fingerprint(doc, second)]
    assert before[0][1]["cylinder_full"] == 1 and len(before[0][1]) >= 3, "前提: 1つ目に穴とフィレットの曲面がある"
    assert before[1][1].get("cone") == 1, "前提: 2つ目に面取りの円錐がある"

    path = tmp_path / "curved.step"
    io_step.export_step(doc, path)
    reread = Document()
    ids = import_step(reread, path)

    assert len(ids) == 2
    after = sorted((_fingerprint(reread, body_id) for body_id in ids), key=lambda fp: fp[2])
    for got, want in zip(after, sorted(before, key=lambda fp: fp[2])):
        assert got[0] == want[0]
        assert got[1] == want[1]
        assert got[2] == pytest.approx(want[2], rel=1e-6)


def test_a_curved_part_exported_to_stl_is_a_closed_mesh_enclosing_the_volume(tmp_path: Path) -> None:
    """穴+フィレットの部品を書き出した STL は、どの辺も2枚の三角形が共有する閉じた面で、囲む体積が元の体積に近い。
    落ちたら、曲面の継ぎ目で三角形が噛み合わず穴が開いている(スライサーが読めない STL になる)。"""
    doc = Document()
    body_id = _filleted_box_with_hole(doc)
    exact = query.volume(doc.get_body(body_id).shape)

    path = tmp_path / "part.stl"
    io_step.export_stl(doc, path)
    triangles = _read_stl_vertices(path)

    undirected = Counter()
    for triangle in triangles:
        keys = [tuple(vertex) for vertex in triangle]
        for start, end in ((0, 1), (1, 2), (2, 0)):
            undirected[frozenset((keys[start], keys[end]))] += 1
    open_edges = [edge for edge, uses in undirected.items() if uses != 2]
    assert not open_edges, f"2枚で共有されない辺が {len(open_edges)} 本ある"
    enclosed = _mesh_volume(triangles)
    assert enclosed == pytest.approx(exact, rel=5e-3)
    low, high = triangles.reshape(-1, 3).min(axis=0), triangles.reshape(-1, 3).max(axis=0)
    box = kernel.bounding_box(doc.get_body(body_id).shape, cache=None)
    assert low == pytest.approx(box["min"], abs=1e-3)
    assert high == pytest.approx(box["max"], abs=1e-3)


def test_two_curved_bodies_exported_to_stl_enclose_the_sum_of_their_volumes(tmp_path: Path) -> None:
    """複数ボディは1本の STL へまとめて書かれ、囲む体積は各ボディの体積の和に近い。"""
    doc, bodies = _curved_document()
    exact = sum(query.volume(doc.get_body(body_id).shape) for body_id in bodies)

    path = tmp_path / "two.stl"
    io_step.export_stl(doc, path)

    assert _mesh_volume(_read_stl_vertices(path)) == pytest.approx(exact, rel=5e-3)

"""`tools/synthetic_corpus.py`(再生の指紋を補う合成の文書群)の回帰。

落ちたら、同じ種から同じ文書ができない・作った文書が `replay_fingerprint.py` で開けない・前後の比較が添字の入れ替わりを見逃す、のどれか。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from duocad.document import Document
from duocad.oplog import OpLog
from duocad.sketch import add_rectangle, sketch_create
from duocad.verbs.extrude import extrude
from duocad.verbs.fillet import fillet
from duocad.region import region_ref
from script_support import load_tool


def _box_with_fillet(edge: int) -> str:
    """20×20×20 の立方体の辺 `edge`(1始まりの添字)を丸めた文書。立方体の辺は全部同じ長さ。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), 20.0, 20.0)
    body_id = extrude(document, [region_ref(sketch_id, 1)], 20.0, mode="new").args["body_id"]
    fillet(document, [edge], 2.0, body_id=body_id)
    return document.log.to_json()


def test_the_same_seed_writes_the_same_document_with_an_index_chain(monkeypatch, tmp_path) -> None:
    """落ちたら、同じ種から別のバイト列の文書ができる(親が前後で同じ題材を作り直せない)か、後の操作が前の結果を添字で指す連鎖が入っていない。"""
    corpus = load_tool(monkeypatch, "synthetic_corpus")

    first, summary = corpus.build_document(3, 0, tmp_path / "a", moves=3)
    second, _ = corpus.build_document(3, 0, tmp_path / "b", moves=3)

    assert first == second
    assert summary["chained"] >= 1
    assert summary["index_refs"] >= summary["chained"]
    assert json.loads(first)["operations"]


def test_generated_documents_open_in_replay_fingerprint(monkeypatch, tmp_path) -> None:
    """落ちたら、作った文書を `tools/replay_fingerprint.py` が開けない(最後の状態の比較に使えない)。"""
    corpus = load_tool(monkeypatch, "synthetic_corpus")
    replay = load_tool(monkeypatch, "replay_fingerprint")
    out = tmp_path / "corpus"
    corpus.main([
        "generate", "--out", str(out), "--seed", "5", "--count", "2", "--moves", "2",
    ])

    paths = replay.collect([str(out / "documents")])
    assert [p.name for p in paths] == ["corpus-5-0000.json", "corpus-5-0001.json"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    workdir = tmp_path / "replay"
    workdir.mkdir()
    for path in paths:
        result = replay.fingerprint(path, workdir)
        assert "error" not in result
        assert result["bodies"]
        assert manifest["documents"][path.name]["operations"] == len(json.loads(path.read_text(encoding="utf-8"))["operations"])


def test_steps_compare_points_at_the_operation_that_took_another_equal_edge(monkeypatch, tmp_path) -> None:
    """落ちたら、同じ長さの辺の入れ替わり(立方体の別の縦の辺を丸める)を、前後の比較が見逃すか、違いの出た操作を指せない。"""
    corpus = load_tool(monkeypatch, "synthetic_corpus")
    before_doc, after_doc = tmp_path / "before" / "doc.json", tmp_path / "after" / "doc.json"
    payload = json.loads(_box_with_fillet(1))
    for path, edge in ((before_doc, 1), (after_doc, 3)):
        next(op for op in payload["operations"] if op["verb"] == "fillet")["args"]["edges"] = [edge]
        path.parent.mkdir()
        path.write_text(json.dumps(payload), encoding="utf-8")
    cube = corpus.replay_steps(before_doc, tmp_path, triangles=False, detail=True)["steps"][2]
    edges = next(iter(cube["bodies"].values()))["detail"]["edges"]
    assert edges[0][1] == edges[2][1] == 20.0 and edges[0] != edges[2]

    for name in ("before", "after"):
        corpus.main(["steps", "--out", str(tmp_path / f"{name}.json"), str(tmp_path / name)])
    same = corpus.compare(tmp_path / "before.json", tmp_path / "before.json")
    differ = corpus.compare(tmp_path / "before.json", tmp_path / "after.json")

    assert same == 0
    assert differ == 1
    before = json.loads((tmp_path / "before.json").read_text(encoding="utf-8"))["documents"]["doc.json"]["steps"]
    after = json.loads((tmp_path / "after.json").read_text(encoding="utf-8"))["documents"]["doc.json"]["steps"]
    first = next(a["verb"] for a, b in zip(before, after) if corpus.step_difference(a, b)[0])
    assert first == "fillet"


def test_compare_tells_triangle_only_differences_from_shape_differences(monkeypatch, tmp_path, capsys) -> None:
    """落ちたら、三角形だけが変わった変更(親が別に判断する)と、形・添字が変わった変更(入れない)を比較が分けられない。"""
    corpus = load_tool(monkeypatch, "synthetic_corpus")
    folder = tmp_path / "doc"
    folder.mkdir()
    (folder / "doc.json").write_text(_box_with_fillet(1), encoding="utf-8")
    base = tmp_path / "base.json"
    corpus.main(["steps", "--triangles", "--out", str(base), str(folder)])
    data = json.loads(base.read_text(encoding="utf-8"))
    steps = data["documents"]["doc.json"]["steps"]
    assert steps[-1]["triangles"], "丸めた後のボディの三角形を指紋にしていない"

    mesh_only = copy.deepcopy(data)
    body_id, (count, _) = next(iter(mesh_only["documents"]["doc.json"]["steps"][-1]["triangles"].items()))
    mesh_only["documents"]["doc.json"]["steps"][-1]["triangles"][body_id] = [count, "0" * 16]
    shape = copy.deepcopy(data)
    shape["documents"]["doc.json"]["steps"][-1]["bodies"][body_id]["edges"] = "0" * 16
    for name, payload in (("mesh.json", mesh_only), ("shape.json", shape)):
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")

    assert corpus.compare(base, tmp_path / "mesh.json") == 2
    assert corpus.compare(base, tmp_path / "shape.json") == 1
    printed = capsys.readouterr().out
    assert "TRIANGLES  doc.json: seq 4 fillet" in printed
    assert "DIFFER     doc.json: seq 4 fillet" in printed


def test_meshing_between_operations_leaves_the_shape_prints_alone(monkeypatch, tmp_path) -> None:
    """落ちたら、`--triangles`(各操作の直後に画面の三角形分割を掛ける)で再生した形の指紋が、掛けない再生と食い違う
    (三角形分割が後の形状演算の結果を変えている、または指紋が三角形分割の有無に依る)か、再生の最後の状態が生成時と違う。"""
    corpus = load_tool(monkeypatch, "synthetic_corpus")
    out = tmp_path / "corpus"
    corpus.main(["generate", "--out", str(out), "--seed", "11", "--count", "1", "--moves", "3"])
    plain, meshed = tmp_path / "plain.json", tmp_path / "meshed.json"
    corpus.main(["steps", "--out", str(plain), str(out / "documents")])
    corpus.main(["steps", "--triangles", "--out", str(meshed), str(out / "documents")])

    assert corpus.compare(plain, meshed) == 0
    result = next(iter(json.loads(plain.read_text(encoding="utf-8"))["documents"].values()))
    assert "error" not in result
    assert len(result["steps"]) == result["operations"]
    assert result["matches_generated"] is True, "再生の最後の状態が、確定の経路で組んだときの状態と違う"


def test_replay_reads_the_document_the_same_way_as_opening_it(monkeypatch, tmp_path) -> None:
    """落ちたら、1操作ずつの再生が `OpLog.from_payload` と違う並びで操作を読んでいる(比べる指紋が、開いた文書の形と別物になる)。"""
    corpus = load_tool(monkeypatch, "synthetic_corpus")
    path = tmp_path / "doc.json"
    path.write_text(_box_with_fillet(2), encoding="utf-8")

    result = corpus.replay_steps(path, tmp_path, triangles=False, detail=True)

    verbs = [op.verb for op in OpLog.from_json(path.read_text(encoding="utf-8")).entries]
    assert [step["verb"] for step in result["steps"]] == verbs
    final = next(iter(result["steps"][-1]["bodies"].values()))
    assert final["counts"][0] == len(final["detail"]["faces"]) == 7
    assert Path(path).exists()

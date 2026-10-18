"""検査・測定・撮影のスクリプトが、本人の実 `models/` を汚さず、開いた文書へ結果を書き足さない契約。

落ちたら、スクリプトを回すたびに `models/` へ untitled-<n>.json が残る(文書一覧が件数に比例して遅くなる)、
または開いた文書に最適化の結果メッシュボディが書き足される。
"""
from __future__ import annotations

import importlib
import json
import shutil
import sys

from agent_support import RecordedProvider, spoke, tool_use
from duocad.paths import REPO_ROOT
from duocad.server import Session
from script_support import cp932_stdout, default_models_dir_is_untouched, load_tool, written

for folder in (REPO_ROOT / "topopt_checks", REPO_ROOT / "topopt_checks" / "idle_validation", REPO_ROOT / "web" / "scripts"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))


def test_wiring_case_is_built_in_a_scratch_models_dir(tmp_path) -> None:
    """落ちたら、受入検査(check_optimize_wiring / check_trace / probe_resolution_raycast)が題材を作るたびに、
    自動保存で既定の `models/` へ untitled-<n>.json が残る。"""
    wiring = importlib.import_module("check_optimize_wiring")

    session, *_ = wiring.build_case()

    assert session.models_dir != (tmp_path / "default-models").resolve()
    assert any(session.models_dir.glob("untitled-*.json")), "自動保存は使い捨ての置き場で起きている"
    assert default_models_dir_is_untouched(tmp_path)


def test_compact_models_check_saves_nothing(tmp_path, monkeypatch) -> None:
    """落ちたら、`--check`(保存せず要素数だけ見る)が、組み立て中の自動保存で既定の `models/` に文書を残す。"""
    compact = importlib.import_module("build_compact_models")
    monkeypatch.setattr(compact, "_check", lambda *args, **kwargs: None)
    out_dir = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["build_compact_models", "--check", "--out-dir", str(out_dir)])

    assert compact.main() == 0

    assert default_models_dir_is_untouched(tmp_path)
    assert not out_dir.exists()


def test_shoot_export_leaves_the_opened_document_alone(tmp_path, monkeypatch) -> None:
    """落ちたら、比較スクショの最適化が、開いた文書(既定は本人の実 `models/`)へ結果メッシュボディを書き足す。
    面ごとに元の文書から開き直すことも見る(前の面の結果が次の面に混ざらない)。"""
    export = importlib.import_module("shoot_topopt_export")
    models = tmp_path / "user-models"
    models.mkdir()
    maker = Session(None, 0.2, models_dir=tmp_path / "maker", exports_dir=tmp_path / "maker-export")
    sketch_id = maker.sketch_create("XY")["sketch_id"]
    maker.add_rectangle(sketch_id, [0.0, 0.0], 10.0, 10.0)
    maker.clear_selection()
    maker.modify_selection([{"sketch": sketch_id, "index": 1}], [])
    maker.extrude(10.0, mode="new")
    maker.save_document("subject.json")
    shutil.copy2(tmp_path / "maker" / "subject.json", models / "subject.json")
    original = (models / "subject.json").read_bytes()
    bodies_when_run: list[int] = []

    def run_optimization(self, *, element_size_mm=None, **options) -> dict:
        from duocad.verbs import add_mesh_body

        bodies_when_run.append(len(self.document.bodies))
        positions = [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]
        body_id = add_mesh_body(self.document, positions, [0, 2, 1, 0, 1, 3, 1, 2, 3, 0, 3, 2], name="result")
        result = dict(
            state="done", body_id=body_id, name="result", triangle_count=4, element_size_mm=2.0,
            grid=dict(shape=[2, 2, 2]), safety_factor=2.5,
        )
        return self.deliver_optimization_result(result)

    monkeypatch.setattr(Session, "optimize_conditions", lambda self: dict(ready=True, problems=[]))
    monkeypatch.setattr(Session, "run_optimization", run_optimization)
    out = tmp_path / "shots"
    out.mkdir()

    export.export_model("subject.json", models, [dict(label="a"), dict(label="b", element_mm=2.0)], out)

    assert (models / "subject.json").read_bytes() == original
    assert sorted(path.name for path in models.iterdir()) == ["subject.json"]
    assert bodies_when_run == [1, 1]
    assert [json.loads((out / f"panel-{i}.json").read_text())["label"] for i in (0, 1)] == ["a", "b"]


def test_llm_tasks_world_is_built_outside_the_default_models_dir(monkeypatch, tmp_path) -> None:
    """落ちたら、課題ごとに作る文書の自動保存が既定の `models/` に untitled-<n>.json を残す(実行のたびに数十個)。"""
    tasks = load_tool(monkeypatch, "llm_tasks")

    world = tasks.build_world()

    assert world.tools.session.models_dir != (tmp_path / "default-models").resolve()
    assert default_models_dir_is_untouched(tmp_path)


def test_llm_tasks_prints_the_failed_call_mark_through_a_cp932_pipe(monkeypatch, tmp_path) -> None:
    """落ちたら、標準出力が cp932 のパイプのとき、失敗した呼び出しの印「✗」で UnicodeEncodeError になり、
    その回の記録と最後の集計表が出ない。"""
    tasks = load_tool(monkeypatch, "llm_tasks")
    provider = RecordedProvider([tool_use("no_such_tool", {}), spoke("ボディは2つです。")])
    monkeypatch.setattr(tasks, "OpenAICompatProvider", lambda **options: provider)
    out = cp932_stdout()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(
        sys, "argv", ["llm_tasks", "--repeat", "1", "--only", "数える", "--out", str(tmp_path / "records.json")]
    )

    tasks.main()

    text = written(out)
    assert "✗no_such_tool" in text
    assert "合計" in text
    assert json.loads((tmp_path / "records.json").read_text(encoding="utf-8"))[0]["task"] == "数える"

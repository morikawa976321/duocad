"""測定のスクリプトが、本人の実 `models/` を汚さない契約。

落ちたら、スクリプトを回すたびに `models/` へ untitled-<n>.json が残る(文書一覧が件数に比例して遅くなる)。
形状最適化の検査・撮影のスクリプトの同じ契約は `tests/topopt/test_topopt_scripts_environment.py`。
"""
from __future__ import annotations

import json
import sys

from agent_support import RecordedProvider, spoke, tool_use
from script_support import cp932_stdout, default_models_dir_is_untouched, load_tool, written


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

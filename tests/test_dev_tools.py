"""`tools/` の開発用スクリプトの不具合の回帰(製品の経路ではなく、測定・保守の道具)。

落ちたら、その道具が本人の `models/` を汚す・端末の文字コードで落ちる・失敗を黙って成功にする、のどれかに戻っている。
"""
from __future__ import annotations

import json
import sys

from agent_support import RecordedProvider, spoke, tool_use
from duocad.agent import AgentLoop
from duocad.document import Document
from duocad.oplog import OpLog
from duocad.sketch import add_circle, sketch_create
from script_support import cp932_stdout, load_tool, written


# ---------------------------------------------------------------- llm_probe


def test_llm_probe_canned_tools_survive_the_agent_loop_calling_with_the_version(monkeypatch) -> None:
    """落ちたら、`AgentLoop` が版を渡して呼ぶ `call_and_seq(name, arguments, expected_seq)` を缶詰の道具が受けない
    (agent 場面が最初のツール呼び出しで TypeError になり、測り直せない)。"""
    probe = load_tool(monkeypatch, "llm_probe")
    provider = RecordedProvider([tool_use("describe_shape", {"body_id": "body-0003"}), spoke("面は6個です。")])

    run = AgentLoop(probe.CannedTools(), provider).run("body-0003 の面数を調べて。")

    assert run.status == "completed"
    assert [outcome.ok for outcome in run.tool_calls] == [True]


# ---------------------------------------------------------------- upgrade_models


def version_2_text() -> str:
    """版2の書き方(円は `radius`)の保存ファイル。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, (1.0, 2.0), 10.0)
    payload = json.loads(document.log.to_json())
    payload["version"] = 2
    for operation in payload["operations"]:
        if operation["verb"] == "add_circle":
            params = operation["args"]["params"]
            operation["args"]["params"] = {
                ("radius" if key == "diameter" else key): (value / 2.0 if key == "diameter" else value)
                for key, value in params.items()
            }
    return json.dumps(payload)


def test_upgrade_models_reports_a_broken_file_and_keeps_going(monkeypatch, tmp_path, capsys) -> None:
    """落ちたら、途中で切れた JSON 1つで全体が落ち、手前のファイルだけが書き換わって版が混ざったまま止まる。"""
    upgrade = load_tool(monkeypatch, "upgrade_models")
    (tmp_path / "a_cut_off.json").write_text('{"version": 2, "operations": ', encoding="utf-8")
    (tmp_path / "b_empty.json").write_text("", encoding="utf-8")
    (tmp_path / "c_old.json").write_text(version_2_text(), encoding="utf-8")

    assert upgrade.main([str(tmp_path), "--write"]) == 0

    output = capsys.readouterr().out
    assert "a_cut_off.json: 読めない" in output
    assert "b_empty.json: 読めない" in output
    assert json.loads((tmp_path / "c_old.json").read_text(encoding="utf-8"))["version"] == OpLog.FORMAT_VERSION
    assert (tmp_path / "c_old.json.v2.bak").is_file()


def test_upgrade_models_prints_an_unreadable_version_through_a_cp932_pipe(monkeypatch, tmp_path) -> None:
    """落ちたら、読めない版のファイルに当たった時点で、cp932 のパイプへ書く「——」で UnicodeEncodeError になる。"""
    upgrade = load_tool(monkeypatch, "upgrade_models")
    (tmp_path / "old.json").write_text('{"version": 1, "entries": []}', encoding="utf-8")
    (tmp_path / "later.json").write_text(version_2_text(), encoding="utf-8")
    out = cp932_stdout()
    monkeypatch.setattr(sys, "stdout", out)

    assert upgrade.main([str(tmp_path)]) == 0

    text = written(out)
    assert "old.json: 読めない" in text
    assert "later.json: version 2" in text


# ---------------------------------------------------------------- check_code_unchanged


def test_check_code_unchanged_fails_for_a_ref_that_does_not_resolve(monkeypatch, capsys) -> None:
    """落ちたら、打ち間違えた ref でも「差分に .py は無い」で終了コード0になり、コメントしか触っていない証明が偽の合格を返す。"""
    check = load_tool(monkeypatch, "check_code_unchanged")

    assert check.main(["nonexistent-ref-xyz"]) == 2

    captured = capsys.readouterr()
    assert "差分に .py は無い" not in captured.out
    assert "nonexistent-ref-xyz" in captured.err


def test_check_code_unchanged_still_compares_against_a_real_ref(monkeypatch) -> None:
    """落ちたら、解決できる ref まで断るようになっている(終了コード2は ref の不正だけ)。作業ツリーの差分の有無で 0 か 1。"""
    check = load_tool(monkeypatch, "check_code_unchanged")

    assert check.main(["HEAD"]) in (0, 1)


# ---------------------------------------------------------------- bench_server_ops


class RecordingSession:
    def __init__(self) -> None:
        self.calls: list[list[int]] = []

    def edge_chains(self, body_id: str, edge_indices: list) -> dict:
        self.calls.append(list(edge_indices))
        return {}


def test_bench_next_edge_asks_for_a_different_edge_each_time(monkeypatch) -> None:
    """落ちたら、「次の辺」の5回が同じ辺を呼び、2回目以降は形ごとのキャッシュに当たる(中央値が次の辺の費用にならない)。"""
    bench = load_tool(monkeypatch, "bench_server_ops")
    monkeypatch.setattr(bench, "ROWS", [])
    session = RecordingSession()

    bench.bench_edge_chains(session, "body-1", nedges=40)

    assert session.calls[0] == [1]
    next_edges = [call[0] for call in session.calls[1:]]
    assert len(next_edges) == 5
    assert len(set(next_edges)) == 5
    assert 1 not in next_edges
    assert all(2 <= edge <= 40 for edge in next_edges)

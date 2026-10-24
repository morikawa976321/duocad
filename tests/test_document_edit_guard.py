"""文書を変える `Session` のメソッドは `edits_document` の印を持つ(開いていないときの断りと、形状最適化の実行中の断り)。"""
from __future__ import annotations

from duocad import catalog
from duocad.server import Session

#: 文書を変えるが、実行中も通してよいもの(操作ログに載らず、最適化が読む条件にも形にも触れない)。
_ALLOWED_WHILE_RUNNING = {"set_visible", "set_sketch_visible", "modify_selection", "toggle_selection",
                          "clear_selection",
                          "set_selection",
                          # 作業空間(起動中のコマンド・モード)は選択と同じで文書に触れない。`apply_command` だけは中で
                          # `apply` を呼ぶので、そちらの断りが効く(`tests/topopt/test_optimize_document_lock.py` の
                          # `test_document_changes_are_refused_while_running`)。
                          "begin_command", "set_command_args", "end_command", "apply_command",
                          "enter_sketch", "leave_sketch", "enter_optimize", "leave_optimize"}
#: 文書を差し替えるもの。実行中も通す —— 実行は開始時の文書を持ったまま走り続ける。
_SWITCHES_DOCUMENT = {"open_document", "new_document", "close_document"}


def test_every_method_that_changes_the_document_is_guarded() -> None:
    """落ちたら、文書を変える verb を足したときに実行中の断りを付け忘れている(その verb だけ実行中に通る)。"""
    names = {command.session for command in catalog.COMMANDS
             if command.changes_document and command.session
             and command.session not in _ALLOWED_WHILE_RUNNING | _SWITCHES_DOCUMENT}
    names |= {"preview", "preview_job"}
    unguarded = sorted(name for name in names if not getattr(getattr(Session, name), "edits_document", False))
    assert not unguarded, unguarded

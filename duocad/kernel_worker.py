"""面の編集・フィレット・面取りを常駐の子プロセスで計算する口。方式と実測は docs/FACE_EDIT.md と docs/FILLET_OVERFLOW.md。

OCCT の `BOPAlgo_RemoveFeatures` は計算中ずっと GIL を握り(3.04 秒の計算で主スレッドが 3.09 秒止まった)、
`BRepOffset_MakeOffset` はプロセスごと落ちることがある(角ばった実物で 21 件)。同じプロセスで計算すると、その間サーバ全体
(画面・MCP)が止まる・落ちるので、子に計算させ、親は返事を待つ(待つ間は GIL を放す)。子の起動・線・時間の上限・作り直しは
`worker_process` の共通の部品で、ここは要求の形と子での計算だけを持つ。

- 子は1本で、初回の呼び出しで立て、計算が終わっても残す(起動に約 1.6 秒。干渉チェックは1回ごとに立てる)。
- 要求は {"op":"face_edit" または "blend_edges","verb":...,"arguments":{...},"shape":<BinTools の base64>}。返事の result は
  {"shape":<base64>,"provenance":{...},"volume_before":..,"volume_after":..} か、関門で断ったときの {"refused":"<断り文>"}。
  BinTools は往復で幾何が変わらず、面の添字の数え方も保つ(BRep の文字列は 1e-14 ずれる)。base64 の往復は
  PC ケースの最大のボディ(233 面、BinTools で 229 KB)で 1 ms 未満。
- 実行は同時に1件。期限切れ・異常終了・取り消しは子を止めて `ValueError`(呼び出し側は結果を採らず、文書もログも変えない)、
  次の呼び出しで立て直す。
"""
from __future__ import annotations

import atexit
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import io
import sys
import threading
import time
from typing import Any, Dict, Optional

from OCP.BinTools import BinTools, BinTools_FormatVersion
from OCP.TopoDS import TopoDS_Shape

from . import worker_process

#: 新しい入力(画面・MCP の確定・プレビュー・寸法変更)の期限[秒](要求待ち・子の起動・受け渡しを含む)。本人の決定(2026-10-23、docs/FACE_EDIT.md カード e)。
#: 実測: 合成形状の正当な計算の最長は 18 秒(穴100個を一度に消す)、角ばった実物の1件は最長約 2.3 秒。
#: フィレット・面取りも同じ値(本人の決定 2026-10-24。docs/FILLET_OVERFLOW.md「いつ使うか」)。
INTERACTIVE_TIMEOUT_S = 20.0

_CONDITION = threading.Condition()
_ACTIVE: Optional[threading.Event] = None
_ACTIVE_PREVIEW = False
_WAITING_FOREGROUND = 0
_PREVIEW: ContextVar[Optional[threading.Event]] = ContextVar("kernel_preview", default=None)
_NEW_INPUT: ContextVar[bool] = ContextVar("kernel_new_input", default=False)
_WORKER: Optional[worker_process.WorkerProcess] = None


@contextmanager
def preview_context(cancel: Optional[threading.Event] = None):
    """プレビューの印と取消しを、計算するスレッドの中だけ伝える。"""
    token = _PREVIEW.set(cancel if cancel is not None else threading.Event())
    try:
        yield
    finally:
        _PREVIEW.reset(token)


@contextmanager
def new_input():
    """寸法変更の試し再生は、記録済みの操作でも新しい入力として期限を掛ける。"""
    token = _NEW_INPUT.set(True)
    try:
        yield
    finally:
        _NEW_INPUT.reset(token)


def time_limit(interactive: bool) -> Optional[float]:
    """新しい入力(確定・プレビュー・寸法変更)は期限あり、記録済み入力の再生は期限なし。"""
    return INTERACTIVE_TIMEOUT_S if interactive or _NEW_INPUT.get() else None


def run(verb: str, shape: Any, arguments: dict, *, timeout: Optional[float], op: str = "face_edit") -> dict:
    """子の計算は1件。確定・再生・寸法変更を優先し、実行中のプレビューを中断する。

    新しいプレビューは実行中の計算を止めず待つ。接続ごとの未開始分の差し替えは呼び出し側で行う。
    期限は呼び出しから数え、符号化・待機・起動・受け渡しを含める。
    子を止めるのは所有スレッドだけ(次の要求の子を誤って止めない)。
    """
    global _WORKER, _ACTIVE, _ACTIVE_PREVIEW, _WAITING_FOREGROUND
    deadline = float("inf") if timeout is None else time.monotonic() + timeout
    preview = _PREVIEW.get()
    cancel = preview if preview is not None else threading.Event()

    def check():
        if cancel.is_set():
            raise ValueError(f"{verb}: プレビューを取り消した")
        if time.monotonic() >= deadline:
            raise ValueError(f"{verb}: {timeout:.0f} 秒以内に返事が無かった")

    with _CONDITION:
        check()
        if preview is None:
            _WAITING_FOREGROUND += 1
            if _ACTIVE_PREVIEW and _ACTIVE is not None:
                _ACTIVE.set()
        try:
            while True:
                check()
                if _ACTIVE is None and (preview is None or _WAITING_FOREGROUND == 0):
                    _ACTIVE, _ACTIVE_PREVIEW = cancel, preview is not None
                    break
                _CONDITION.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        finally:
            if preview is None:
                _WAITING_FOREGROUND -= 1
            _CONDITION.notify_all()
    try:
        message = {"op": op, "verb": verb, "arguments": arguments, "shape": _encode(shape)}
        check()
        if _WORKER is None:
            _WORKER = worker_process.WorkerProcess([sys.executable, "-c", "from duocad import kernel_worker; kernel_worker.child_main()"])
        try:
            remaining = None if timeout is None else max(0.0, deadline - time.monotonic())
            result = _WORKER.request(message, remaining, cancel=cancel)
            if cancel.is_set() or time.monotonic() >= deadline:
                _WORKER.restart()
            check()  # 返信と同時に取り消された結果も採らない
        except worker_process.WorkerRemoteError as error:
            raise ValueError(f"{verb}: OCCT raised {error.reason}") from None
        except worker_process.WorkerCancelled:
            _WORKER.restart()
            raise ValueError(f"{verb}: プレビューを取り消した") from None
        except worker_process.WorkerError as error:
            _WORKER.restart()
            if isinstance(error, worker_process.WorkerTimeout):
                raise ValueError(f"{verb}: {timeout:.0f} 秒以内に返事が無かった") from None
            raise ValueError(f"{verb}: {error.reason}") from None
    finally:
        with _CONDITION:
            _ACTIVE, _ACTIVE_PREVIEW = None, False
            _CONDITION.notify_all()
    if "refused" in result:
        raise ValueError(result["refused"])
    return {
        "shape": _decode(result["shape"]),
        "provenance": result["provenance"],
        "volume_before": result["volume_before"],
        "volume_after": result["volume_after"],
    }


def stop() -> None:
    """子を止める(終了時。次の `run` で立て直す)。"""
    with _CONDITION:
        while _ACTIVE is not None:
            _CONDITION.wait(timeout=0.05)
        if _WORKER is not None:
            _WORKER.close()


atexit.register(stop)


def _encode(shape: Any) -> str:
    stream = io.BytesIO()
    BinTools.Write_s(shape, stream, False, False, BinTools_FormatVersion.BinTools_FormatVersion_CURRENT)
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _decode(text: str) -> Any:
    shape = TopoDS_Shape()
    BinTools.Read_s(shape, io.BytesIO(base64.b64decode(text)))
    return shape


# --------------------------------------------------------------------------------------
# 子
# --------------------------------------------------------------------------------------
def _handle(message: Dict[str, Any]) -> Dict[str, Any]:
    from . import kernel, kernel_face_edit

    op = message.get("op")
    if op not in ("face_edit", "blend_edges"):
        raise ValueError(f"unknown op {op!r}")
    try:
        if op == "face_edit":
            result = kernel_face_edit.compute(message["verb"], _decode(message["shape"]), message["arguments"])
        else:
            verb = message["verb"]
            if verb not in ("fillet", "chamfer"):
                raise ValueError(f"unknown blend {verb!r}")
            values = {int(index): float(value) for index, value in message["arguments"]["values"]}
            # kernel の公開関数は同一プロセスの純粋な計算口。子を起こす verb 層の入口は呼ばない。
            compute = kernel.fillet_edges if verb == "fillet" else kernel.chamfer_edges
            result = compute(_decode(message["shape"]), values)
    except ValueError as error:
        return {"refused": str(error)}
    result["shape"] = _encode(result["shape"])
    return result


def child_main() -> None:
    worker_process.run_child(_handle)

"""面の削除・オフセットを常駐の子プロセスで計算する口。方式と実測は docs/FACE_EDIT.md「子プロセス」。

OCCT の `BOPAlgo_RemoveFeatures` は計算中ずっと GIL を握り(3.04 秒の計算で主スレッドが 3.09 秒止まった)、
`BRepOffset_MakeOffset` はプロセスごと落ちることがある(角ばった実物で 21 件)。同じプロセスで計算すると、その間サーバ全体
(画面・MCP)が止まる・落ちるので、子に計算させ、親は返事を待つ(待つ間は GIL を放す)。子の起動・線・時間の上限・作り直しは
`worker_process` の共通の部品で、ここは要求の形と子での計算だけを持つ。

- 子は1本で、初回の呼び出しで立て、計算が終わっても残す(起動に約 1.6 秒。干渉チェックは1回ごとに立てる)。
- 要求は {"op":"face_edit","verb":...,"arguments":{...},"shape":<BinTools の base64>}。返事の result は
  {"shape":<base64>,"provenance":{...},"volume_before":..,"volume_after":..} か、関門で断ったときの {"refused":"<断り文>"}。
  BinTools は往復で幾何が変わらず、面の添字の数え方も保つ(BRep の文字列は 1e-14 ずれる)。base64 の往復は
  PC ケースの最大のボディ(233 面、BinTools で 229 KB)で 1 ms 未満。
- 実行は同時に1件。期限切れ・異常終了・取り消しは子を止めて `ValueError`(呼び出し側は結果を採らず、文書もログも変えない)、
  次の呼び出しで立て直す。
"""
from __future__ import annotations

import atexit
import base64
import io
import sys
import threading
from typing import Any, Dict, Optional

from OCP.BinTools import BinTools, BinTools_FormatVersion
from OCP.TopoDS import TopoDS_Shape

from . import worker_process

#: 画面・MCP からの操作で、計算(子の起動と受け渡しを含む)を打ち切るまでの秒数。置いた値(docs/FACE_EDIT.md カード e は本人判断待ち)。
#: 実測の根拠は「合成形状の正当な計算の最長 18 秒(穴100個を一度に消す)」と「角ばった実物の1件は最長約 2.3 秒」だけ。
INTERACTIVE_TIMEOUT_S = 30.0

_LOCK = threading.Lock()
_WORKER: Optional[worker_process.WorkerProcess] = None


def time_limit(interactive: bool) -> Optional[float]:
    """画面・MCP からの操作なら期限[秒]、ログの再生なら None(再生に期限を掛けると、保存できた文書が負荷しだいで開けなくなる)。"""
    return INTERACTIVE_TIMEOUT_S if interactive else None


def run(verb: str, shape: Any, arguments: dict, *, timeout: Optional[float]) -> dict:
    """子で `kernel_face_edit.compute(verb, shape, arguments)` を走らせ、同じ形の辞書を返す(`"shape"` は親の形)。

    `timeout` は計算の期限[秒](None は期限なし。ログの再生)。前の計算の終わりを待つ時間は含めない。
    """
    global _WORKER
    message = {"op": "face_edit", "verb": verb, "arguments": arguments, "shape": _encode(shape)}
    with _LOCK:
        if _WORKER is None:
            # `-m duocad.kernel_worker` だと、パッケージの import が先にこのモジュールを読み、runpy が二重読みを警告する
            _WORKER = worker_process.WorkerProcess([sys.executable, "-c", "from duocad import kernel_worker; kernel_worker.child_main()"])
        try:
            result = _WORKER.request(message, timeout)
        except worker_process.WorkerRemoteError as error:
            raise ValueError(f"{verb}: OCCT raised {error.reason}") from None
        except worker_process.WorkerError as error:
            _WORKER.restart()
            raise ValueError(f"{verb}: {error.reason}") from None
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
    with _LOCK:
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
    from . import kernel_face_edit

    if message.get("op") != "face_edit":
        raise ValueError(f"unknown op {message.get('op')!r}")
    try:
        result = kernel_face_edit.compute(message["verb"], _decode(message["shape"]), message["arguments"])
    except ValueError as error:
        return {"refused": str(error)}
    result["shape"] = _encode(result["shape"])
    return result


def child_main() -> None:
    worker_process.run_child(_handle)

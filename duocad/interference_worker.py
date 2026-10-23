"""干渉チェックの子プロセス(Issue #186)。子の本体(`python -m duocad.interference_worker`)と、親側の口(`WorkerProcess`)。
子の起動・線の受け渡し・時間の上限・作り直しは `worker_process` の共通の部品で、ここは干渉チェックの要求の形と計算だけを持つ。

子で計算する理由: OCP からは OCCT の計算を途中で止められない(山形が一致したねじ山の2体 General Fuse は 240 秒で終わらなかった)ので、
時間の上限を掛けるには子プロセスごと止めるしかない。止めたら作り直し、形を送り直す(PC ケースで約 2 秒)。

## 線の形

1行 = 1つの JSON(`worker_process` の線。BRep の文字列は改行を含むので JSON の文字列で包む)。

    {"seq":1,"op":"load","bodies":[{"id":"b1","kind":"brep","brep":"..."} | {"id":"b2","kind":"mesh","positions":[..],"indices":[..]}]}
    {"seq":2,"op":"relation","a":"b1","b":"b2","search":1.0,"deviation":0.05|null}   → result は `kernel_interference.pair_relation`(か `interference_mesh.mesh_pair`)
    {"seq":3,"op":"distance","a":"b1","b":"b2"}                                      → result は `kernel_interference.min_distance`
    返事: {"seq":N,"ok":true,"result":{...}} か {"seq":N,"ok":false,"error":"..."}
"""
from __future__ import annotations

import sys
import threading
from typing import Any, Dict, List, Optional

from . import worker_process
from .worker_process import (  # noqa: F401  干渉チェックの呼び手とテストはここから import する
    WorkerCancelled,
    WorkerError,
    WorkerFailure,
    WorkerRemoteError,
    WorkerTimeout,
)


# ------------------------------------------------------------------------------------------------
# 親側
# ------------------------------------------------------------------------------------------------
class WorkerProcess(worker_process.WorkerProcess):
    """干渉チェックの子1つ。起動のたびに `load_message`(ボディの形)を送り直す。"""

    def __init__(
        self,
        load_message: Dict[str, Any],
        *,
        command: Optional[List[str]] = None,
        cancel: Optional[threading.Event] = None,
        startup_seconds: float = 60.0,
    ) -> None:
        super().__init__(
            command or [sys.executable, "-m", "duocad.interference_worker"],
            load=load_message,
            cancel=cancel,
            startup_seconds=startup_seconds,
        )


# ------------------------------------------------------------------------------------------------
# 子の本体
# ------------------------------------------------------------------------------------------------
class Handler:
    """要求 1 件を処理する。形と、メッシュ経路の manifold を覚える。"""

    def __init__(self) -> None:
        self._bodies: Dict[str, Dict[str, Any]] = {}
        self._manifolds: Dict[str, Any] = {}

    def handle(self, message: Dict[str, Any]) -> Dict[str, Any]:
        operation = message.get("op")
        if operation == "load":
            return self._load(message["bodies"])
        if operation == "relation":
            return self._relation(message["a"], message["b"], float(message["search"]), message.get("deviation"))
        if operation == "distance":
            from . import kernel_interference

            return kernel_interference.min_distance(self._shape(message["a"]), self._shape(message["b"]))
        raise ValueError(f"知らない op: {operation!r}")

    def _load(self, bodies: List[Dict[str, Any]]) -> Dict[str, Any]:
        from . import kernel_interference

        self._bodies, self._manifolds = {}, {}
        for body in bodies:
            if body["kind"] == "brep":
                self._bodies[body["id"]] = {"kind": "brep", "shape": kernel_interference.decode_brep_unchecked(body["brep"])}
            else:
                self._bodies[body["id"]] = {"kind": "mesh", "positions": body["positions"], "indices": body["indices"]}
        return {"loaded": len(self._bodies)}

    def _shape(self, body_id: str) -> Any:
        body = self._bodies[body_id]
        if body["kind"] != "brep":
            raise ValueError(f"{body_id} は B-rep ではない")
        return body["shape"]

    def _relation(self, first: str, second: str, search: float, deviation: Optional[float]) -> Dict[str, Any]:
        if self._bodies[first]["kind"] == "brep" and self._bodies[second]["kind"] == "brep":
            from . import kernel_interference

            return kernel_interference.pair_relation(self._bodies[first]["shape"], self._bodies[second]["shape"], deviation)
        from . import interference_mesh

        return interference_mesh.mesh_pair(self._manifold(first), self._manifold(second), search, deviation)

    def _manifold(self, body_id: str) -> Any:
        if body_id not in self._manifolds:
            from . import interference_mesh

            body = self._bodies[body_id]
            if body["kind"] == "brep":
                self._manifolds[body_id] = interference_mesh.manifold_of_shape(body["shape"])[0]
            else:
                self._manifolds[body_id] = interference_mesh.manifold_of_triangles(body["positions"], body["indices"])[0]
        return self._manifolds[body_id]


def serve(lines: Any, out: Any) -> None:
    worker_process.serve(Handler().handle, lines, out)


def main() -> None:
    worker_process.run_child(Handler().handle)


if __name__ == "__main__":
    main()

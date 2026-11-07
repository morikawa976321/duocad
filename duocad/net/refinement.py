"""背景の測り直し: カメラの要求で足りなくなった面を、ループの外で測る(`Server` が1つ持つ)。

測り直しはボディを丸ごと測ることもあり(733 面で 0.5〜0.9 秒)、ループの上で測るとその間のクリック・選択が待たされた。
写し(`kernel.copy_shape`。添字は元と同じ)の上で測るのは、三角形分割が面に書き込まれ、ループ側の演算と同じ面を取り合わない
ため(プレビューと同じ理由。`Session.preview_job`)。測っている間、そのボディの三角形は配らず(画面は前の三角形を出し続ける)、
測り終えたら元の形の面として覚えて全接続へ配る。測っている最中にまたカメラが動いたら、終わってから足りない分をもう一度測る。

**OCPを直接importしない**(境界は kernel.py だけ)。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Awaitable, Callable, Optional

from .. import kernel
from ..session import Session
from . import wire


def _measure_copies(copies: list) -> list:
    """ワーカーで、形の写しの面を測る。結果は元の形の面として覚える(添字は同じ)。"""
    return [
        (body_id, shape, groups, kernel.tessellate_faces(copy, groups, closed=closed))
        for body_id, shape, copy, groups, closed in copies
    ]


class BackgroundRefinement:
    """`push` は全接続への配信、`broadcast` は全接続へのイベント(どちらも `Server` のもの)。"""

    def __init__(
        self,
        session: Session,
        push: Callable[[], Awaitable[None]],
        broadcast: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._session = session
        self._push = push
        self._broadcast = broadcast
        self._worker: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._current: Optional[asyncio.Task] = None
        #: 後始末(測った面を覚えて配る)のタスク全部。`settled` はこれが空になるまで待つ
        self._tasks: set = set()
        self._refining: dict = {}  # body_id -> 測っている(測る予定の)形
        #: id(元の形) -> (元の形, 写し)。ワーカーだけが触る写しを持ち回り、ズームのたびに全ボディを写し直さない
        #: (27 部品で 1 回 60〜90 ms がループ上)。測った後の写しは `tessellate_faces` が三角形を消すので、元の形と同じ状態に戻っている。
        #: 文書から消えた・差し替わった形の写しは次の `schedule` で捨てる。
        self._copies: dict = {}
        self._again = False

    @property
    def active(self) -> bool:
        """送らずにおくボディがありうるか。"""
        return bool(self._refining)

    def schedule(self) -> None:
        """足りない面を測り始める(測り終えたら全接続へ配る)。"""
        if self._current is not None and not self._current.done():
            self._again = True
            for body_id, shape, _groups, _closed in self._session.refinement_jobs():
                self._refining[body_id] = shape  # 今の形(測っている最中に操作で形が替わっていれば、替わった方を待たせる)
            return
        jobs = self._session.refinement_jobs()
        if not jobs:
            return
        if self._worker is None:
            self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="duocad-refine")
        alive = {id(body.shape) for body in self._session.document.bodies if body.shape is not None}
        for key in [key for key in self._copies if key not in alive]:
            del self._copies[key]
        copies = [(body_id, shape, self._copy_of(shape), groups, closed) for body_id, shape, groups, closed in jobs]
        self._refining = {body_id: shape for body_id, shape, _groups, _closed in jobs}
        loop = asyncio.get_running_loop()
        computation = loop.run_in_executor(self._worker, _measure_copies, copies)
        self._current = loop.create_task(self._finish(computation))
        self._tasks.add(self._current)
        self._current.add_done_callback(self._tasks.discard)

    def _copy_of(self, shape: object) -> object:
        held = self._copies.get(id(shape))
        if held is None or held[0] is not shape:
            held = (shape, kernel.copy_shape(shape))
            self._copies[id(shape)] = held
        return held[1]

    async def _finish(self, computation: "asyncio.Future") -> None:
        try:
            results = await computation
        except Exception as error:  # noqa: BLE001  測れなかったボディは、次の配信がループの上で測る
            self._session.warnings.append(f"面の測り直しに失敗: {type(error).__name__}: {error}")
            results = []
        for body_id, shape, groups, measured in results:
            self._session.adopt_refinement(body_id, shape, groups, measured)
        self._refining = {}
        self._current = None  # 自分はもう測っていない(次を始められるように。`schedule` は実行中の測り直しがあれば待つ)
        again, self._again = self._again, False
        if again:
            self.schedule()
        try:
            await self._push()
        except Exception as error:  # noqa: BLE001  ループの上で測った配信の失敗と同じく、画面へ知らせる(タスクの外へ漏らさない)
            await self._broadcast({"type": "error", "where": "set_view_resolution", "message": f"{type(error).__name__}: {error}"})
            for warning in self._session.take_warnings():
                await self._broadcast({"type": "warning", "message": warning})

    async def settled(self) -> None:
        """測り直しと、それが終わって配る分が済むまで待つ。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def deferrable(self, meta: dict, delivered: "wire.Delivered") -> frozenset:
        """この接続へ今回は三角形を送らずにおくボディ(測っている最中で、画面が今の形を持っているもの)。"""
        current = {body["body_id"]: body for body in meta["bodies"]}
        return frozenset(
            body_id for body_id, shape in self._refining.items()
            if body_id in current and current[body_id]["visible"]
            and delivered.shape_revisions.get(body_id) == current[body_id]["shape_revision"]
            and self._session.document.get_body(body_id).shape is shape
        )

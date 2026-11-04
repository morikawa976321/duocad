"""undo / redo と配信の競合を確かめる: 復元した形・配った版・画面が持つ面(配信の記録)が食い違わないか。

    uv run python -X utf8 tools/probe_undo_delivery.py [--part small|big] [--only <場面の名前の一部>] [--fuzz 200] [--seed 1] [--out <json>]

同じプロセスで `Server.handle` を本物のまま回し、接続だけを偽物にする(受け取ったフレームを画面と同じ規則 `web/src/wire.ts` で
埋め戻す)。MCP の操作は、`ToolsOnLoop` と同じくループの上で `Session.apply` / `undo` / `redo` を素で呼ぶ(配信は
`_on_core_change` が後から起こす。続けて呼べば途中の配信は1つにまとまる)。カメラの測り直しのワーカーは門で止められる。

確かめること:

- フレームごと: 状態に載った可視のボディについて、画面が持っている三角形が、状態の `shape_revision` と同じ形の版で届いたもの
  か(送らずにおいたボディも含めて)。省かれた面・操作ログ・スケッチを埋め戻せたか
- 静まったとき: 画面のボディの並び・操作ログ・undo/redo の数がサーバと同じか。各面の三角形の面積と重心が、いまの形の
  その添字の面(`query`)と合うか(古い形・別の添字の面を描いていれば外れる)
- undo の後: 形が操作の前と同じか(体積・面の数・面ごとの面積)。最後に、操作ログを新しい文書へ再生した形と同じか
- 1手ごとの所要(操作 + 配信)。キャッシュの退避を超えた往復で測り直しが戻ってくるか
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import bench_all_ops as base  # noqa: E402
from duocad import kernel, query  # noqa: E402
from duocad.document import Document  # noqa: E402
from duocad.net import refinement, wire  # noqa: E402
from duocad.oplog import OpLog  # noqa: E402
from duocad.server import Server  # noqa: E402
from duocad.session import Session  # noqa: E402

now = time.perf_counter


# ====================================================================================== 画面の偽物
class Screen:
    """`Server.handle` に渡す接続。`send` は画面と同じく埋め戻し、`async for` で要求を渡す。"""

    def __init__(self, name: str, slow: float = 0.0) -> None:
        self.name = name
        self.slow = slow
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.state: Optional[dict] = None
        self.faces: dict = {}  # body_id -> {添字: 面}
        self.shape_of_faces: dict = {}  # body_id -> 三角形が届いたときの shape_revision
        self.log: Optional[list] = None
        self.sketches: dict = {}
        self.violations: list = []
        self.frames = 0
        self.acked = 0
        self.ack_event = asyncio.Event()
        self.errors: list = []
        self._ack = 0

    def __aiter__(self) -> "Screen":
        return self

    async def __anext__(self) -> Any:
        raw = await self.incoming.get()
        if raw is None:
            raise StopAsyncIteration
        return raw

    async def send(self, raw: Any) -> None:
        if self.closed:
            raise ConnectionError("closed")
        if self.slow:
            await asyncio.sleep(self.slow)
        payload = wire.decode(raw) if isinstance(raw, (bytes, bytearray)) else json.loads(raw)
        kind = payload.get("type")
        if kind == "state":
            self.frames += 1
            self._restore(payload)
        elif kind == "done":
            self.acked = max(self.acked, payload["ack_id"])
            self.ack_event.set()
        elif kind == "error":
            self.errors.append(payload.get("message"))

    def _violate(self, what: str) -> None:
        self.violations.append(f"{self.name} frame{self.frames}: {what}")

    def _restore(self, payload: dict) -> None:
        state = payload["state"]
        if "log" in state:
            self.log = state["log"]
        else:
            if self.log is None or state.get("log_keep", 0) > len(self.log):
                self._violate("受け取っていない操作ログを省かれた")
                return
            if "log_keep" in state:
                self.log = self.log[:state.pop("log_keep")] + state.pop("log_tail")
            state["log"] = self.log
        sketches = []
        for sketch in state["sketches"]:
            if sketch.get("unchanged"):
                if sketch["sketch_id"] not in self.sketches:
                    self._violate(f"受け取っていないスケッチを省かれた {sketch['sketch_id']}")
                    continue
                sketch = self.sketches[sketch["sketch_id"]]
            sketches.append(sketch)
        state["sketches"] = sketches
        self.sketches = {sketch["sketch_id"]: sketch for sketch in sketches}
        held = {face["serial"]: face for known in self.faces.values() for face in known.values() if face.get("serial") is not None}
        meta = {body["body_id"]: body for body in state["bodies"]}
        for body in payload["delta"]["bodies"]:
            geometry = body.get("geometry")
            if not geometry:
                self.faces.pop(body["body_id"], None)
                self.shape_of_faces.pop(body["body_id"], None)
                continue
            faces = []
            for face in geometry["faces"]:
                if "positions" not in face:
                    previous = held.get(face["serial"])
                    if previous is None:
                        self._violate(f"受け取っていない面を省かれた {body['body_id']} 面 {face['index']}")
                        continue
                    face = dict(previous, index=face["index"], detail=face["detail"])
                faces.append(face)
            self.faces[body["body_id"]] = {face["index"]: face for face in faces}
            self.shape_of_faces[body["body_id"]] = body["shape_revision"]
        for body_id in [key for key in self.faces if key not in meta]:
            del self.faces[body_id]
            self.shape_of_faces.pop(body_id, None)
        for body_id, body in meta.items():
            if not body["visible"]:
                continue
            if body_id not in self.faces:
                self._violate(f"可視の {body_id} の三角形を持っていない")
            elif self.shape_of_faces.get(body_id) != body["shape_revision"]:
                self._violate(f"{body_id} の三角形は形の版 {self.shape_of_faces.get(body_id)} のもので、状態は {body['shape_revision']}")
            elif len(self.faces[body_id]) != body["face_count"]:
                self._violate(f"{body_id} の面の数 {len(self.faces[body_id])} が状態の {body['face_count']} と違う")
        self.state = state

    def request(self, command: dict) -> int:
        self._ack += 1
        self.ack_event.clear()
        self.incoming.put_nowait(json.dumps(dict(command, client_op=self._ack, ack_id=self._ack)))
        return self._ack

    async def wait_ack(self, ack: int, timeout: float = 600.0) -> None:
        deadline = now() + timeout
        while self.acked < ack:
            self.ack_event.clear()
            left = deadline - now()
            if left <= 0:
                raise TimeoutError(f"{self.name}: done {ack} が来ない")
            try:
                await asyncio.wait_for(self.ack_event.wait(), left)
            except asyncio.TimeoutError:
                pass


# ====================================================================================== 正しさの物差し
_FACES_OF: dict = {}


def exact_faces(shape: Any) -> list:
    """いまの形の面ごとの (面積, 面積重心)。同じ形は1回だけ求める。"""
    held = _FACES_OF.get(id(shape))
    if held is not None and held[0] is shape:
        return held[1]
    described = query.describe_faces(shape)
    faces = [(face["area"], query.face_centroid(shape, face["index"])) for face in described]
    _FACES_OF[id(shape)] = (shape, faces)
    return faces


def mesh_area_centroid(face: dict) -> tuple:
    positions, indices = face["positions"], face["indices"]
    area = 0.0
    cx = cy = cz = 0.0
    for at in range(0, len(indices), 3):
        a, b, c = (indices[at + k] * 3 for k in range(3))
        ax, ay, az = positions[a], positions[a + 1], positions[a + 2]
        ux, uy, uz = positions[b] - ax, positions[b + 1] - ay, positions[b + 2] - az
        vx, vy, vz = positions[c] - ax, positions[c + 1] - ay, positions[c + 2] - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        piece = 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)
        area += piece
        cx += piece * (ax + positions[b] + positions[c]) / 3.0
        cy += piece * (ay + positions[b + 1] + positions[c + 1]) / 3.0
        cz += piece * (az + positions[b + 2] + positions[c + 2]) / 3.0
    if area <= 0:
        return 0.0, None
    return area, (cx / area, cy / area, cz / area)


def fingerprint(shape: Any) -> tuple:
    faces = exact_faces(shape)
    return (len(faces), round(query.volume(shape), 6), tuple(round(area, 5) for area, _ in faces))


def compare(session: Session, screen: Screen, diag: float) -> list:
    """静まった画面とサーバの食い違い(空なら一致)。"""
    problems: list = []
    state = screen.state
    if state is None:
        return [f"{screen.name}: 状態を受け取っていない"]
    document = session.document
    if [body["body_id"] for body in state["bodies"]] != [body.id for body in document.bodies]:
        problems.append(f"{screen.name}: ボディの並び {[b['body_id'] for b in state['bodies']]} != {[b.id for b in document.bodies]}")
    if [(entry["seq"], entry["verb"]) for entry in state["log"]] != [(op.seq, op.verb) for op in document.log.entries]:
        problems.append(f"{screen.name}: 操作ログが違う")
    if state["redo_count"] != document.log.redo_count or state["can_undo"] != document.log.can_undo:
        problems.append(f"{screen.name}: undo/redo の数が違う")
    tolerance = 0.002 * diag
    for body in document.bodies:
        if not body.visible or body.shape is None:
            continue
        held = screen.faces.get(body.id)
        if held is None:
            problems.append(f"{screen.name}: {body.id} の三角形が無い")
            continue
        exact = exact_faces(body.shape)
        if sorted(held) != list(range(1, len(exact) + 1)):
            problems.append(f"{screen.name}: {body.id} の面の添字 {len(held)} 個 != 形の {len(exact)} 面")
            continue
        for index, (area, centroid) in enumerate(exact, start=1):
            mesh_area, mesh_centroid = mesh_area_centroid(held[index])
            if mesh_centroid is None:
                continue  # 三角形にできなかった面(サーバも警告して空で配る)
            if abs(mesh_area - area) > 0.03 * area + 0.05:
                problems.append(f"{screen.name}: {body.id} 面 {index} の面積 {mesh_area:.3f} != {area:.3f}")
            elif math.dist(mesh_centroid, centroid) > tolerance + 0.02 * math.sqrt(area):
                problems.append(f"{screen.name}: {body.id} 面 {index} の重心が {math.dist(mesh_centroid, centroid):.3f} mm ずれている")
            if len(problems) > 12:
                return problems
    return problems


def replay_matches(session: Session) -> list:
    """操作ログを新しい文書へ再生した形が、いまの形と同じか(undo/redo が再生と食い違わないか)。"""
    document = Document()
    document.log = OpLog.from_json(session.document.log.to_json())
    document.log.replay(document)
    problems = []
    if [b.id for b in document.bodies] != [b.id for b in session.document.bodies]:
        return [f"再生したボディの並びが違う {[b.id for b in document.bodies]}"]
    for mine, replayed in zip(session.document.bodies, document.bodies):
        if mine.shape is None:
            continue
        if fingerprint(mine.shape) != fingerprint(replayed.shape):
            problems.append(f"{mine.id} の形が再生と違う")
    return problems


# ====================================================================================== 場面の道具
class Lab:
    def __init__(self, session: Session, server: Server, diag: float) -> None:
        self.session = session
        self.server = server
        self.diag = diag
        self.screens: list = []
        self.tasks: list = []
        self.results: list = []
        self.scene = ""
        self.gate = threading.Event()
        self.gate.set()

    async def connect(self, name: str, slow: float = 0.0) -> Screen:
        screen = Screen(name, slow)
        self.screens.append(screen)
        self.tasks.append(asyncio.ensure_future(self.server.handle(screen)))
        await self.quiet()
        return screen

    async def disconnect(self, screen: Screen) -> None:
        screen.incoming.put_nowait(None)
        screen.closed = True
        self.screens.remove(screen)
        await self.quiet()

    async def ws(self, screen: Screen, command: dict) -> None:
        await screen.wait_ack(screen.request(command))

    def mcp(self, verb: str, args: Optional[dict] = None) -> Any:
        if verb == "undo":
            return self.session.undo()
        if verb == "redo":
            return self.session.redo()
        if verb == "select":
            self.session.clear_selection()
            return self.session.modify_selection(add=args["refs"])
        return self.session.apply(verb, args or {})

    async def quiet(self, refinement_too: bool = True) -> None:
        """配信・背景の測り直し・受信待ちの要求が全部済むまで(門が閉じていれば測り直しは待たない)。"""
        for _ in range(10_000):
            await asyncio.sleep(0)
            pushes = [task for task in self.server._pending_pushes if not task.done()]
            if pushes:
                await asyncio.gather(*pushes, return_exceptions=True)
                continue
            if refinement_too and self.gate.is_set() and self.server._refinement._tasks:
                await self.server.settled()
                continue
            if any(not screen.incoming.empty() or screen.state is None for screen in self.screens):
                await asyncio.sleep(0.005)
                continue
            await asyncio.sleep(0.01)
            if not [task for task in self.server._pending_pushes if not task.done()]:
                return

    async def step(self, label: str, act: Any, *, check: bool = True, before: Optional[tuple] = None,
                   body_id: Optional[str] = None) -> dict:
        """1手: `act()`(コルーチンか普通の関数)を走らせ、静まってから確かめる。"""
        t0 = now()
        error = None
        try:
            result = act()
            if asyncio.iscoroutine(result):
                await result
        except Exception as failure:  # noqa: BLE001
            error = f"{type(failure).__name__}: {failure}"[:200]
        await self.quiet()
        elapsed = (now() - t0) * 1000.0
        problems: list = []
        if check:
            for screen in self.screens:
                problems += compare(self.session, screen, self.diag)
        if before is not None and body_id is not None and error is None:
            current = self.session.document.get_body(body_id).shape
            if fingerprint(current) != before:
                problems.append(f"{body_id} の形が操作の前に戻っていない")
        violations = [v for screen in self.screens for v in screen.violations]
        for screen in self.screens:
            screen.violations.clear()
        row = {"scene": self.scene, "step": label, "ms": round(elapsed, 1), "problems": problems, "violations": violations}
        if error:
            row["error"] = error
        self.results.append(row)
        flag = "OK" if not problems and not violations else "NG"
        print(f"  [{flag}] {self.scene:<14} {label:<40} {elapsed:8.1f} ms {error or ''} {(problems + violations)[:3]}", flush=True)
        return row

    def shape_of(self, body_id: str) -> Any:
        return self.session.document.get_body(body_id).shape

    def top_face(self, body_id: str) -> int:
        planes = base.faces_of_kind(self.session, body_id, "plane")
        return max(planes, key=lambda f: f["centroid"][2])["index"]

    def camera(self, zoom: float, focus: Optional[dict] = None) -> dict:
        command = {"cmd": "set_view_resolution", "world_per_pixel": self.diag / zoom, "size_hint": self.diag}
        if focus is not None:
            command["focus"] = focus
        return command


def gated_measurement(lab: Lab) -> None:
    """背景の測り直しを `lab.gate` が開くまで止める(ループの上で測る経路は止めない)。"""
    real = refinement._measure_copies

    def measured(copies: list) -> list:
        lab.gate.wait(600)
        return real(copies)

    refinement._measure_copies = measured


# ====================================================================================== 場面
async def scene_single(lab: Lab, part: str, edge: dict) -> None:
    lab.scene = "単発"
    a = lab.screens[0]
    await lab.step("ws select edge", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [edge], "remove": []}))
    before = fingerprint(lab.shape_of(part))
    await lab.step("ws fillet", lambda: lab.ws(a, {"cmd": "fillet", "radius": 0.5}))
    await lab.step("ws undo", lambda: lab.ws(a, {"cmd": "undo"}), before=before, body_id=part)
    await lab.step("ws redo", lambda: lab.ws(a, {"cmd": "redo"}))
    await lab.step("ws undo(2)", lambda: lab.ws(a, {"cmd": "undo"}), before=before, body_id=part)


async def scene_eviction(lab: Lab, part: str, count: int) -> None:
    """同じボディの形を `count` 回替えて全部 undo → 全部 redo → 全部 undo(形の控え 3・結果の使い回し 8・子の対 16 を超える)。"""
    lab.scene = "退避を超える往復"
    a = lab.screens[0]
    history: list = []
    for k in range(count):
        history.append(fingerprint(lab.shape_of(part)))
        if k % 2 == 0:
            top = lab.top_face(part)
            await lab.step(f"ws select top({top}) {k}", lambda top=top: lab.ws(a, {"cmd": "modify_selection", "add": [{"body": part, "kind": "face", "index": top}], "remove": []}), check=False)
            await lab.step(f"ws extrude face +0.2 {k}", lambda: lab.ws(a, {"cmd": "extrude", "distance": 0.2, "mode": "join"}))
        else:
            await lab.step(f"ws select body {k}", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [{"body": part, "kind": "body", "index": None}], "remove": []}), check=False)
            await lab.step(f"ws move dx=1.5 {k}", lambda: lab.ws(a, {"cmd": "move_body", "dx": 1.5, "dy": 0.0, "dz": 0.0}))
    for k in reversed(range(count)):
        await lab.step(f"ws undo → {k}", lambda: lab.ws(a, {"cmd": "undo"}), before=history[k], body_id=part)
    for k in range(count):
        await lab.step(f"ws redo {k}", lambda: lab.ws(a, {"cmd": "redo"}))
    for k in reversed(range(count)):
        await lab.step(f"mcp undo → {k}", lambda: lab.mcp("undo"), before=history[k], body_id=part)


async def scene_revive(lab: Lab, tool: str, copies: int) -> None:
    """棒を `copies` 個複製して全部消し、全部 undo → redo → undo(消えたボディの控え 6 を超える)。"""
    lab.scene = "削除と復活"
    a = lab.screens[0]
    made: list = []
    for k in range(copies):
        await lab.step(f"ws select bar {k}", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [{"body": tool, "kind": "body", "index": None}], "remove": []}), check=False)
        await lab.step(f"ws copy bar {k}", lambda k=k: lab.ws(a, {"cmd": "move_body", "dx": 0.0, "dy": 0.0, "dz": 12.0 * (k + 1), "copy": True}))
        made.append(lab.session.document.bodies[-1].id)
    setup = len(lab.session.document.log.entries)
    for body_id in made:
        await lab.step(f"ws select {body_id[-4:]}", lambda body_id=body_id: lab.ws(a, {"cmd": "modify_selection", "add": [{"body": body_id, "kind": "body", "index": None}], "remove": []}), check=False)
        await lab.step(f"ws delete {body_id[-4:]}", lambda: lab.ws(a, {"cmd": "delete"}))
    for k in range(len(made)):
        await lab.step(f"ws undo delete {k}", lambda: lab.ws(a, {"cmd": "undo"}))
    for k in range(len(made)):
        await lab.step(f"ws redo delete {k}", lambda: lab.ws(a, {"cmd": "redo"}))
    while len(lab.session.document.log.entries) > setup:
        await lab.step("mcp undo", lambda: lab.mcp("undo"))
    lab.scene = "削除と復活(連打)"

    def burst() -> None:
        for body_id in made[:3]:
            lab.mcp("select", {"refs": [{"body": body_id, "kind": "body", "index": None}]})
            lab.mcp("delete")
        lab.mcp("undo")
        lab.mcp("undo")
        lab.mcp("redo")
    await lab.step("mcp delete×3 undo×2 redo(配信を挟まない)", burst)
    while len(lab.session.document.log.entries) > setup:
        await lab.step("mcp undo", lambda: lab.mcp("undo"))


async def scene_mirror(lab: Lab, part: str) -> None:
    lab.scene = "鏡映"
    a = lab.screens[0]
    whole = {"body": part, "kind": "body", "index": None}
    await lab.step("ws select body", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [whole], "remove": []}), check=False)
    await lab.step("ws mirror YZ", lambda: lab.ws(a, {"cmd": "mirror_body", "base_plane": "YZ"}))
    mirrored = lab.session.document.bodies[-1].id
    await lab.step("ws select mirrored", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [{"body": mirrored, "kind": "body", "index": None}], "remove": []}), check=False)
    await lab.step("ws move mirrored", lambda: lab.ws(a, {"cmd": "move_body", "dx": 0.0, "dy": 0.0, "dz": 20.0}))
    await lab.step("ws undo move", lambda: lab.ws(a, {"cmd": "undo"}))
    await lab.step("ws undo mirror", lambda: lab.ws(a, {"cmd": "undo"}))
    await lab.step("ws redo mirror", lambda: lab.ws(a, {"cmd": "redo"}))
    await lab.step("ws redo move", lambda: lab.ws(a, {"cmd": "redo"}))
    await lab.step("mcp undo×2", lambda: (lab.mcp("undo"), lab.mcp("undo")))


async def scene_refinement(lab: Lab, part: str, edge: dict) -> None:
    """ズームの測り直しを門で止めている最中の undo / redo / 削除。"""
    lab.scene = "測り直し中"
    a = lab.screens[0]
    focus = {part: list(range(1, 31))}
    await lab.step("ws camera overview", lambda: lab.ws(a, lab.camera(1200.0)))
    await lab.step("ws select edge", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [edge], "remove": []}), check=False)
    before = fingerprint(lab.shape_of(part))
    await lab.step("ws fillet", lambda: lab.ws(a, {"cmd": "fillet", "radius": 0.5}))
    for variant in ("ws undo/redo", "mcp burst undo+redo", "delete/undo", "zoom out + undo"):
        lab.gate.clear()
        await lab.step(f"[{variant}] zoom in (門で止める)", lambda: lab.ws(a, lab.camera(12000.0, focus)))
        if variant == "ws undo/redo":
            await lab.step(f"[{variant}] undo", lambda: lab.ws(a, {"cmd": "undo"}), before=before, body_id=part)
            await lab.step(f"[{variant}] redo", lambda: lab.ws(a, {"cmd": "redo"}))
        elif variant == "mcp burst undo+redo":
            await lab.step(f"[{variant}] undo+redo", lambda: (lab.mcp("undo"), lab.mcp("redo")))
        elif variant == "delete/undo":
            await lab.step(f"[{variant}] select body", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [{"body": part, "kind": "body", "index": None}], "remove": []}), check=False)
            await lab.step(f"[{variant}] delete", lambda: lab.ws(a, {"cmd": "delete"}))
            await lab.step(f"[{variant}] undo delete", lambda: lab.ws(a, {"cmd": "undo"}))
        else:
            await lab.step(f"[{variant}] zoom out", lambda: lab.ws(a, lab.camera(1200.0)))
            await lab.step(f"[{variant}] undo", lambda: lab.ws(a, {"cmd": "undo"}), before=before, body_id=part)
            await lab.step(f"[{variant}] redo", lambda: lab.ws(a, {"cmd": "redo"}))
        lab.gate.set()
        await lab.step(f"[{variant}] 門を開ける", lambda: None)
        await lab.step(f"[{variant}] zoom out", lambda: lab.ws(a, lab.camera(1200.0)))
    await lab.step("ws undo fillet", lambda: lab.ws(a, {"cmd": "undo"}), before=before, body_id=part)


async def scene_reconnect(lab: Lab, part: str, edge: dict) -> None:
    lab.scene = "繋ぎ直し"
    a = lab.screens[0]
    focus = {part: list(range(1, 31))}
    await lab.step("ws select edge", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [edge], "remove": []}), check=False)
    await lab.step("ws fillet", lambda: lab.ws(a, {"cmd": "fillet", "radius": 0.5}))
    lab.gate.clear()
    await lab.step("zoom in (門で止める)", lambda: lab.ws(a, lab.camera(12000.0, focus)))
    await lab.step("A を切る", lambda: lab.disconnect(a), check=False)
    await lab.step("mcp undo(誰も繋がっていない)", lambda: lab.mcp("undo"), check=False)
    b = await lab.connect("B")
    await lab.step("B が繋ぐ", lambda: None)
    lab.gate.set()
    await lab.step("門を開ける", lambda: None)
    await lab.step("mcp redo", lambda: lab.mcp("redo"))
    c = await lab.connect("C", slow=0.03)
    await lab.step("C(遅い)が繋ぐ", lambda: None)
    await lab.step("B undo / mcp redo / B undo", lambda: _interleave(lab, b))
    await lab.step("C を切る", lambda: lab.disconnect(c))
    await lab.step("B zoom out", lambda: lab.ws(b, lab.camera(1200.0)))


async def _interleave(lab: Lab, b: Screen) -> None:
    """B の undo が掛かった直後(B への配信が遅い C の送信を待っている間)に MCP の redo を割り込ませる。"""
    redo_count = lab.session.document.log.redo_count
    ack = b.request({"cmd": "undo"})
    while lab.session.document.log.redo_count == redo_count:
        await asyncio.sleep(0)
    lab.mcp("redo")
    await b.wait_ack(ack)
    await lab.ws(b, {"cmd": "undo"})


async def scene_fuzz(lab: Lab, part: str, tool: str, steps: int, seed: int) -> None:
    """遅い接続を含む2接続で、画面の操作・MCP の連打・カメラ・可視性・繋ぎ直しを無作為に混ぜる。"""
    lab.scene = "無作為"
    rng = random.Random(seed)
    if len(lab.screens) < 2:
        await lab.connect("slow", slow=0.02)
    for k in range(steps):
        a = lab.screens[0]
        bodies = [body for body in lab.session.document.bodies if body.shape is not None]
        body = rng.choice(bodies)
        choice = rng.choice(["fillet", "move", "undo", "redo", "burst", "delete", "camera", "visible", "reconnect", "extrude", "copy"])
        label = f"{k} {choice} {body.id[-4:]}"
        if choice == "fillet" and body.visible:
            edges = [e for e in base.edges_of_kind(lab.session, body.id, "line", 3.0)]
            if not edges:
                continue
            e = rng.choice(edges)
            await lab.step(label + " select", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [{"body": body.id, "kind": "edge", "index": e["index"]}], "remove": []}), check=False)
            await lab.step(label, lambda: lab.ws(a, {"cmd": "fillet", "radius": rng.choice([0.3, 0.4, 0.5])}))
        elif choice in ("move", "copy") and body.visible:
            await lab.step(label + " select", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [{"body": body.id, "kind": "body", "index": None}], "remove": []}), check=False)
            await lab.step(label, lambda: lab.ws(a, {"cmd": "move_body", "dx": rng.choice([1.0, -2.0]), "dy": 0.0, "dz": 0.0, **({"copy": True} if choice == "copy" else {})}))
        elif choice == "extrude" and body.visible:
            top = lab.top_face(body.id)
            await lab.step(label + " select", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [{"body": body.id, "kind": "face", "index": top}], "remove": []}), check=False)
            await lab.step(label, lambda: lab.ws(a, {"cmd": "extrude", "distance": 0.3, "mode": "join"}))
        elif choice == "undo" and lab.session.document.log.can_undo and len(lab.session.document.log.entries) > lab.floor:
            await lab.step(label, lambda: lab.ws(a, {"cmd": "undo"}) if rng.random() < 0.5 else lab.mcp("undo"))
        elif choice == "redo" and lab.session.document.log.can_redo:
            await lab.step(label, lambda: lab.ws(a, {"cmd": "redo"}) if rng.random() < 0.5 else lab.mcp("redo"))
        elif choice == "burst":
            def burst() -> None:
                for _ in range(rng.randint(2, 5)):
                    if rng.random() < 0.5 and lab.session.document.log.can_undo and len(lab.session.document.log.entries) > lab.floor:
                        lab.mcp("undo")
                    elif lab.session.document.log.can_redo:
                        lab.mcp("redo")
            await lab.step(label, burst)
        elif choice == "delete" and body.id not in (part, tool):
            await lab.step(label + " select", lambda: lab.ws(a, {"cmd": "modify_selection", "add": [{"body": body.id, "kind": "body", "index": None}], "remove": []}), check=False)
            await lab.step(label, lambda: lab.ws(a, {"cmd": "delete"}))
        elif choice == "camera":
            lab.gate.clear() if rng.random() < 0.3 else lab.gate.set()
            zoom = rng.choice([1200.0, 6000.0, 12000.0])
            focus = {body.id: sorted(rng.sample(range(1, kernel_face_count(body) + 1), min(10, kernel_face_count(body))))} if zoom > 1200 else None
            await lab.step(label + f" zoom {zoom:.0f}", lambda: lab.ws(a, lab.camera(zoom, focus)), check=lab.gate.is_set())
            if not lab.gate.is_set() and rng.random() < 0.5:
                await lab.step(label + " (測り直し中に) undo", lambda: lab.mcp("undo") if len(lab.session.document.log.entries) > lab.floor else None, check=False)
            lab.gate.set()
            await lab.step(label + " 門を開ける", lambda: None)
        elif choice == "visible":
            await lab.step(label, lambda: lab.ws(a, {"cmd": "set_visible", "body_id": body.id, "visible": not body.visible}))
        elif choice == "reconnect":
            slow = [screen for screen in lab.screens if screen.name.startswith("slow")]
            if slow:
                await lab.step(label + " 遅い接続を切る", lambda: lab.disconnect(slow[0]), check=False)
            await lab.step(label + " 繋ぎ直す", lambda: lab.connect(f"slow{k}", slow=0.02))
    lab.gate.set()
    for body in lab.session.document.bodies:
        if not body.visible:
            await lab.step(f"show {body.id[-4:]}", lambda body=body: lab.ws(lab.screens[0], {"cmd": "set_visible", "body_id": body.id, "visible": True}))


def kernel_face_count(body: Any) -> int:
    from duocad import brep_topology
    return brep_topology.face_count(body.shape)


# ====================================================================================== 入口
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--part", choices=sorted(base.PARTS), default="small")
    ap.add_argument("--evictions", type=int, default=10)
    ap.add_argument("--copies", type=int, default=8)
    ap.add_argument("--fuzz", type=int, default=120)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path, default=REPO / "output" / "bench_request_paths" / "undo_delivery.json")
    args = ap.parse_args()

    async def run() -> list:
        models = Path(tempfile.mkdtemp(prefix="duocad-undo-"))
        session = Session(kernel.DEFAULT_DEVIATION, models_dir=models)
        part = base.build_part(session, **base.PARTS[args.part])
        session.apply("sketch_create", {"base_plane": "XZ"})
        sid = base.last_sketch(session)
        session.apply("add_rectangle", {"sketch_id": sid, "corner": [-70, 2], "width": 140, "height": 3})
        session.leave_sketch()
        base.select(session, [{"sketch": sid, "index": 1}])
        session.apply("extrude", {"distance": 30, "mode": "new"})
        tool = session.document.bodies[-1].id
        session.clear_selection()
        lines = base.blendable(session, part, base.edges_of_kind(session, part, "line", 3.0), 1)
        edge = {"body": part, "kind": "edge", "index": lines[0]["index"]}
        diag = session.geometry(part)["bbox"]["diagonal"]
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        lab = Lab(session, server, diag)
        lab.floor = len(session.document.log.entries)
        gated_measurement(lab)
        await lab.connect("A")
        scenes = [
            ("単発", lambda: scene_single(lab, part, edge)),
            ("退避", lambda: scene_eviction(lab, part, args.evictions)),
            ("削除", lambda: scene_revive(lab, tool, args.copies)),
            ("鏡映", lambda: scene_mirror(lab, part)),
            ("測り直し", lambda: scene_refinement(lab, part, edge)),
            ("繋ぎ直し", lambda: scene_reconnect(lab, part, edge)),
            ("無作為", lambda: scene_fuzz(lab, part, tool, args.fuzz, args.seed)),
        ]
        for name, scene in scenes:
            if args.only and args.only not in name:
                continue
            print(f"\n## {name}", flush=True)
            await scene()
            lab.scene = name + "(再生)"
            problems = replay_matches(session)
            lab.results.append({"scene": lab.scene, "step": "ログを新しい文書へ再生", "ms": 0.0, "problems": problems, "violations": []})
            print(f"  [{'OK' if not problems else 'NG'}] 再生と一致 {problems[:3]}", flush=True)
        for screen in list(lab.screens):
            await lab.disconnect(screen)
        await asyncio.gather(*lab.tasks, return_exceptions=True)
        return lab.results

    results = asyncio.run(run())
    bad = [row for row in results if row["problems"] or row["violations"]]
    errors = [row for row in results if row.get("error")]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"python": sys.version, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(results)} 手、食い違い {len(bad)} 手、操作の失敗 {len(errors)} 手。wrote {args.out}")


if __name__ == "__main__":
    main()

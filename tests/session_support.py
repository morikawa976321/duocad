"""`Session` と WebSocket の口を通すテストが共有する題材と、画面の代わりの接続。"""
from __future__ import annotations

import asyncio
import json

from duocad import parameters, query
from duocad.document import Body
from duocad.server import Server, Session
from document_support import BOX_X, BOX_Y, BOX_Z, box_shape

#: `box()` が作る立方体の一辺[mm]。
SIDE = 20.0


def session_with_box(dx: float = BOX_X, dy: float = BOX_Y, dz: float = BOX_Z, **options) -> Session:
    """`id="box"` の直方体を1つ直に置いたセッション(操作ログは空)。"""
    session = Session(None, 0.2, **options)
    session.document.add_body(Body(id="box", name="box", kind="solid", shape=box_shape(dx, dy, dz)))
    return session


def edit_parameter(session: Session, name: str, value: float) -> None:
    """名前つき寸法を書き換える。**`set_parameter` は公開面に無い**ので、コアの関数を直接呼ぶ
    (保存済みログの再生・再構築の検査用)。"""
    parameters.set_parameter(session.document, name, value)


def select(session: Session, refs: list) -> None:
    """選択を `refs` で置き換える(人がクリックで選び直すのと同じ入口)。"""
    session.clear_selection()
    session.modify_selection(refs, [])


def box(session: Session):
    """スケッチ→長方形→領域押し出し(join)で一辺 `SIDE` の立方体を作り、`(body, sketch_id)` を返す。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], SIDE, SIDE)
    select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(SIDE, mode="join")
    session.clear_selection()
    return session.document.bodies[-1], sketch_id


def pad(session: Session, width: float, depth: float, height: float, corner=(0.0, 0.0)):
    """長方形を**新しいボディとして**押し出し、そのBodyを返す。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, list(corner), width, depth)
    regions = len(session.state_meta()["sketches"][-1]["regions"])
    select(session, [{"sketch": sketch_id, "index": regions}])
    session.extrude(height, mode="new")
    session.clear_selection()
    return session.document.bodies[-1]


def cylinder(session: Session, radius: float, height: float, *, center=(0.0, 0.0), mode: str = "join"):
    """スケッチ→円→領域押し出しで円柱を作り、そのBodyを返す(半径は名前つきの値になる)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, list(center), radius)
    select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(height, mode=mode)
    session.clear_selection()
    return session.document.bodies[-1]


# --------------------------------------------------------------------------------------
# 形状最適化の題材
# --------------------------------------------------------------------------------------
def face_with_normal(body, nx: float, ny: float, nz: float) -> int:
    for face in query.describe_faces(body.shape):
        normal = face["normal"]
        if normal[0] * nx + normal[1] * ny + normal[2] * nz > 0.9:
            return int(face["index"])
    raise AssertionError("そのような法線の面が無い")


def two_pads(session: Session):
    """離れた2つのパッド。**条件面が別々のボディに載る**(compound の経路を必ず通る)。"""
    left = pad(session, 10.0, 40.0, 40.0)
    right = pad(session, 10.0, 40.0, 10.0, corner=(60.0, 0.0))
    select(session, [{"body": right.id, "kind": "body"}])
    session.move_body(0.0, 0.0, 30.0)
    session.clear_selection()
    return left, session.document.get_body(right.id)


def four_conditions(session: Session, magnitude: float = 500.0):
    """`two_pads` の両方を保持し、左の -x 面を拘束・右の +z 面に下向きの荷重を置く。
    `(left, right, 拘束面の添字, 荷重面の添字)` を返す。"""
    left, right = two_pads(session)
    fixed = face_with_normal(left, -1.0, 0.0, 0.0)
    load = face_with_normal(right, 0.0, 0.0, 1.0)
    select(session, [{"body": left.id, "kind": "body"}, {"body": right.id, "kind": "body"}])
    session.set_preserve_bodies()
    select(session, [{"body": left.id, "kind": "face", "index": fixed}])
    session.set_fixtures()
    select(session, [{"body": right.id, "kind": "face", "index": load}])
    session.set_loads(magnitude, [0.0, 0.0, -1.0])
    session.clear_selection()
    return left, right, fixed, load


# --------------------------------------------------------------------------------------
# 画面の代わり(WebSocket の接続)
# --------------------------------------------------------------------------------------
class FakeConnection:
    """`Server` が使うのは `send` だけ(配信の出口)。届いた封筒を `payloads` に溜める。"""

    def __init__(self) -> None:
        self.payloads: list = []

    async def send(self, raw: str) -> None:
        self.payloads.append(json.loads(raw))

    def of_type(self, type_: str) -> list:
        return [payload for payload in self.payloads if payload.get("type") == type_]

    def last(self, kind: str) -> dict:
        found = self.of_type(kind)
        assert found, f"{kind!r} の応答が無い: {[payload.get('type') for payload in self.payloads]}"
        return found[-1]


def browser(session: Session, script, *, connections: int = 2, initial_push: bool = True):
    """`script(send)` を1つのイベントループで走らせ、`(script の戻り値, 接続の並び)` を返す。

    `send(command)` は1本目の接続から送り、1本目に最後に届いた封筒を返す。接続直後の配信
    (`initial_push`)は数えない —— 数える件数が変わるテストは `initial_push=False` で配信そのものを省く。
    既定で**2本つなぐ** —— 1本だけだと「要求した接続にだけ返す」「全接続へ配る」の区別が付かない。
    """

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        clients = [FakeConnection() for _ in range(connections)]
        for client in clients:
            server.clients.add(client)
            if initial_push:
                await server._push(client, applied=None, only=client)
        for client in clients:
            client.payloads.clear()

        async def send(command: dict):
            await server._dispatch(clients[0], json.dumps(command))
            return clients[0].payloads[-1] if clients[0].payloads else None

        return await script(send), clients

    return asyncio.run(scenario())


def send_one(session: Session, command: dict) -> dict:
    """1本だけつないだ接続から1コマンドを送り、最後に届いた封筒を返す。"""
    return browser(session, lambda send: send(command), connections=1)[0]


def pushed_by_mcp(session: Session, act) -> list:
    """接続を1本つないで接続直後の配信を受けた後、`act()`(MCP 起点の変更)が起こす配信を待ち、
    届いた封筒を**接続直後の分も含めて**返す(`len(payloads) > 1` が「配信された」の印)。

    MCP 起点の配信は別タスクで走るので、`sleep(0)` で最大50回だけループへ番を譲って待つ。
    """

    async def scenario() -> list:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        before = len(client.payloads)
        act()
        for _ in range(50):
            await asyncio.sleep(0)
            if len(client.payloads) > before:
                break
        return client.payloads

    return asyncio.run(scenario())

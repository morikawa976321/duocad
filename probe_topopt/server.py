"""probe_topopt の HTTP 静的配信と WebSocket サーバ。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from websockets.asyncio.server import serve

import mesh
import simp

_ROOT = Path(__file__).resolve().parent
_WEB_DIR = _ROOT / "web"
_HOST = "127.0.0.1"
# **8765/8766 を使わない。** そこは duocad 本体が使っている
# (8765 = WebSocket、8766 = 静的配信 + /mcp。本体の README「ポート」節)。
# 同じ番号にすると、本体が動いている間はこのプローブが起動せず、逆に
# プローブを先に立てると**本体の起動が黙って失敗する**。
_HTTP_PORT = int(os.environ.get("PROBE_HTTP_PORT", "8775"))
_WS_PORT = int(os.environ.get("PROBE_WS_PORT", "8776"))


class _StaticHandler(SimpleHTTPRequestHandler):
    """web/ 配下の静的ファイルを配信する。"""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
    }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(_WEB_DIR), **kwargs)


class _AppState:
    """同時実行は1本まで。"""

    def __init__(self) -> None:
        self.running = False


def _start_message() -> dict[str, object]:
    preset = simp.PRESET
    return {
        "type": "start",
        "nelx": preset["nelx"],
        "nely": preset["nely"],
        "nelz": preset["nelz"],
        "max_iter": preset["max_iter"],
        "volfrac": preset["volfrac"],
    }


def _iter_message(it: simp.Iteration, xphys: np.ndarray) -> dict[str, object]:
    flat = xphys.ravel()
    occupied = np.flatnonzero(flat > 0.5).tolist()
    rho = [int(round(flat[i] * 255)) for i in occupied]
    return {
        "type": "iter",
        "it": it.it,
        "compliance": it.compliance,
        "volfrac": it.volfrac,
        "change": it.change,
        "seconds": it.seconds,
        "occupied": occupied,
        "rho": rho,
    }


def _build_mesh_payload(rho: np.ndarray) -> dict[str, object]:
    """完了時の密度場から、平滑化済みの面(頂点・三角形)をJSONで送れる形にする。

    浮島除去→パディング→marching cubes→Taubin平滑化は `mesh.build_surface` が持つ
    (詳細は `mesh.py`)。`mesh.py` が返す座標は角(0,0,0)基準(STL書き出しに素直な向き)
    だが、**ボクセル描画(`web/index.html` の `dummy.position`)は原点を中心に置く規約**
    (`ex+0.5-nelx/2` 等)なので、ここで同じだけ平行移動して座標系を合わせる
    (合わせないと、置き換わった瞬間にカメラ・バウンディングボックスに対して形状が
    半分ずれた位置に描かれる)。`nelx/nely/nelz` はどれも偶数なのでこの平行移動は
    整数量になり、marching cubesの補間比を見る検査(`check_mesh.py`)には影響しない
    (その検査は`mesh.build_surface`内部でオフセット適用前の生座標から計算済みの値を使う)。
    座標は小数4桁へ丸めてペイロードを縮める(表示品質には効かない)。
    """
    mr = mesh.build_surface(rho)
    nelx, nely, nelz = rho.shape
    center = np.asarray(mr.spacing, dtype=float) * (np.array([nelx, nely, nelz], dtype=float) / 2.0)
    verts = np.round(mr.verts_smooth - center, 4)
    return {
        "vertices": verts.ravel().tolist(),
        "faces": mr.faces.astype(np.int64).ravel().tolist(),
    }


async def _ws_handler(websocket, state: _AppState) -> None:
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") != "start":
                continue
            if state.running:
                try:
                    await websocket.send(
                        json.dumps({"type": "error", "message": "既に実行中"}, ensure_ascii=False)
                    )
                except Exception:
                    pass
                continue

            state.running = True
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

            def callback(it: simp.Iteration, xphys: np.ndarray) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, _iter_message(it, xphys))

            try:
                await websocket.send(json.dumps(_start_message(), ensure_ascii=False))
            except Exception:
                state.running = False
                continue

            async def sender() -> None:
                while True:
                    item = await queue.get()
                    if item is None:
                        return
                    try:
                        await websocket.send(json.dumps(item, ensure_ascii=False))
                    except Exception:
                        return

            sender_task = asyncio.create_task(sender())
            try:
                result = await asyncio.to_thread(simp.run_preset, callback=callback)
            except Exception as exc:
                await queue.put(None)
                await sender_task
                try:
                    await websocket.send(
                        json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)
                    )
                except Exception:
                    pass
            else:
                await queue.put(None)
                await sender_task
                last = result.history[-1] if result.history else None
                # **完了時にだけ**平滑化した面を作って一緒に送る(反復中は間に合わないので
                # やらない/やる必要が無い。ボクセルのままdoneを受け取った側=`?voxel=1`は
                # このmeshフィールドを無視すればよい)。失敗しても`done`自体は必ず送る
                # (画面が反復完了のまま固まらないようにする)。
                mesh_payload: dict[str, object] | None = None
                try:
                    mesh_payload = await asyncio.to_thread(_build_mesh_payload, result.rho)
                except Exception as exc:  # noqa: BLE001
                    print(f"平滑化メッシュの生成に失敗(ボクセル表示のまま終わる): {exc}")
                done_msg = {
                    "type": "done",
                    "iterations": len(result.history),
                    "seconds": result.seconds,
                    "compliance": last.compliance if last is not None else 0.0,
                    "volfrac": last.volfrac if last is not None else 0.0,
                    "stopped_by": result.stopped_by,
                    "mesh": mesh_payload,
                }
                try:
                    await websocket.send(json.dumps(done_msg, ensure_ascii=False))
                except Exception:
                    pass
            finally:
                state.running = False
    except Exception:
        pass


async def _run_servers(http_server: ThreadingHTTPServer, state: _AppState) -> None:
    async with serve(lambda ws: _ws_handler(ws, state), _HOST, _WS_PORT):
        try:
            await asyncio.Future()
        finally:
            http_server.shutdown()


def main() -> None:
    print(f"http://{_HOST}:{_HTTP_PORT}/  (WebSocket: ws://{_HOST}:{_WS_PORT})")
    state = _AppState()
    try:
        http_server = ThreadingHTTPServer((_HOST, _HTTP_PORT), _StaticHandler)
    except OSError as exc:
        # **黙って落ちると「ブラウザが開かない」だけが見える**ので、原因を名指しする
        raise SystemExit(
            f"ポート {_HTTP_PORT} を開けない({exc})。"
            f" 既に server.py が動いていないか確認するか、PROBE_HTTP_PORT で変える"
        ) from exc
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()
    # **既定でブラウザを開く。** READMEの起動手順を1コマンドで終わらせるため
    # (自動検査は headless Chrome を自前で立てるので `PROBE_NO_OPEN=1` で止める)。
    if os.environ.get("PROBE_NO_OPEN") != "1":
        try:
            webbrowser.open(f"http://{_HOST}:{_HTTP_PORT}/?ws={_WS_PORT}")
        except Exception as exc:  # noqa: BLE001
            # **ブラウザを開けないことでサーバまで死なせない。** URLは既に表示済みで、
            # 手で開けば使える。
            print(f"(ブラウザを自動で開けなかった: {exc} 上のURLを手で開いてください)")
    try:
        asyncio.run(_run_servers(http_server, state))
    except KeyboardInterrupt:
        http_server.shutdown()


if __name__ == "__main__":
    main()

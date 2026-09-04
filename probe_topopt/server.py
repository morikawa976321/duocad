"""probe_topopt の HTTP 静的配信と WebSocket サーバ。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import numpy as np
from websockets.asyncio.server import serve

# **依存の向きは probe → 本体の一方向。** 最適化のコードは 2026-09-05 に
# `duocad/topopt/` へ移設した(本体は probe を一切参照しない)。この実験場は素の
# `python server.py` で立てるので、リポジトリ直下を sys.path へ足してから import する。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from duocad.topopt import anchor  # noqa: E402
from duocad.topopt import design_space as ds  # noqa: E402
from duocad.topopt import mesh  # noqa: E402
from duocad.topopt import simp  # noqa: E402
from duocad.topopt import voxelize as vx  # noqa: E402

_ROOT = Path(__file__).resolve().parent
_WEB_DIR = _ROOT / "web"
_HOST = "127.0.0.1"
# **8765/8766 を使わない。** そこは duocad 本体が使っている
# (8765 = WebSocket、8766 = 静的配信 + /mcp。本体の README「ポート」節)。
# 同じ番号にすると、本体が動いている間はこのプローブが起動せず、逆に
# プローブを先に立てると**本体の起動が黙って失敗する**。
_HTTP_PORT = int(os.environ.get("PROBE_HTTP_PORT", "8775"))
_WS_PORT = int(os.environ.get("PROBE_WS_PORT", "8776"))

# **ボタンが解く問題(2026-09-04〜)。** 表示・確認用なので解像度・反復数は短くて良い。
# 22 は「4条件から連結性ガードまで通る」ことを実測で確かめてある解像度
# (`check_design_space.py` が 22 を使っていた頃に合わせた値。**検査側は 14 へ下がったので
# その関係はもう無い** —— 22 のまま据え置いてあるのは実測済みの値だからで、
# `voxelize.occupancy` のレイ縮退が解像度に対して単調でない以上、理由なく動かさない)。
# 実測は README「起動直後の4条件表示」節。
DEMO_ELEMENTS_LONG_AXIS = 22
DEMO_VOLFRAC = 0.3
DEMO_MAX_ITER = 30
DEMO_FORCE = (0.0, 0.0, -1.0)
# `run_topopt` の既定出力(`output/design_space_case1.stl`)や `check_anchor.py` の成果物
# (`output/bracket.stl`)を上書きしないよう、専用の出力先を明示する。
DEMO_STL_PATH = _ROOT / "output" / "server_conditions_demo.stl"

_demo_space_lock = threading.Lock()
_demo_space: ds.DesignSpace | None = None


def _get_demo_space() -> ds.DesignSpace:
    """既定表示(4条件)とボタンが解く問題を**同じオブジェクト**で共有する。

    起動直後の表示と最適化の入口を別々に導出すると、余白や格子が独立に決まりうる
    ——「見せている問題と解いている問題が別物」になる(このタスクの完了条件3)。初回アクセスで
    1回だけ導出し、以後はプロセスが生きている間キャッシュする(導出は毎回 B-rep から
    三角形分割をやり直すので、リクエストのたびに呼ぶのは無駄に遅い)。

    **導出は名指ししない**(`ds.derive_default`)。画面はその時点での最新の最適化処理を
    可視化する、という方針なので、既定が倒れたら画面も一緒に倒れるのが正しい
    (2026-09-04に既定は案5になった)。粗い段には**本番と同じ体積目標**を渡す ——
    別の量で解くと「別の形の周り」を膨らませた塊になる(`derive_from_coarse_run` の docstring)。
    """
    global _demo_space
    if _demo_space is None:
        with _demo_space_lock:
            if _demo_space is None:
                _demo_space = ds.derive_default(
                    ds.demo_conditions(),
                    elements_long_axis=DEMO_ELEMENTS_LONG_AXIS,
                    coarse_volfrac=DEMO_VOLFRAC,
                )
    return _demo_space


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
    """ボタンが解く問題(2026-09-04〜: 4条件から導出した塊)の反復開始通知。

    **`_get_demo_space()` と同じキャッシュを読む** —— ここで別の解像度を渡すと、
    `start` が予告する `nelx/nely/nelz` と実際に流れてくる `iter` メッセージの
    ボクセル形状が食い違う。
    """
    nelx, nely, nelz = _get_demo_space().grid.shape
    return {
        "type": "start",
        "nelx": int(nelx),
        "nely": int(nely),
        "nelz": int(nelz),
        "max_iter": DEMO_MAX_ITER,
        "volfrac": DEMO_VOLFRAC,
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


def _to_index_space(points: np.ndarray, grid: vx.Grid) -> np.ndarray:
    """B-repのmm座標 → ビューアの規約(要素1個=1単位、原点は格子の中心)。

    ボクセルの立方体は要素 `(ex,ey,ez)` の中心を `(ex+0.5-nelx/2, ...)` に置く
    (`index.html` の `updateInstances` / `showAnchors` と同じ規約)。B-rep由来の三角形を
    同じ場所に重ねるには mm座標を `(mm - grid.origin) / spacing - shape/2` へ変換する必要が
    ある —— ここを取り違えると、条件の見た目は正しいのに実際に解いている格子とは
    半セル〜1セルぶんずれる。
    """
    lo, _ = grid.world_box()
    shape = np.asarray(grid.shape, dtype=float)
    return (np.asarray(points, dtype=float) - lo) / float(grid.spacing) - shape / 2.0


def _mesh_of(positions_index_space: np.ndarray, triangles: np.ndarray) -> dict[str, object]:
    return {
        "positions": np.round(positions_index_space, 4).ravel().tolist(),
        "triangles": np.asarray(triangles, dtype=np.int64).ravel().tolist(),
    }


def _build_condition_payload(space: ds.DesignSpace) -> dict[str, object]:
    """起動直後の既定表示: 4条件を duocad 本体と同じ色分けで描く材料を送る。

    **`space` は `run_topopt` に渡すのと同じオブジェクト**であること(このタスクの完了条件3)。
    ここで別に `derive()` し直すと、見せている問題と解く問題が別物になりうる。

    塗り分け(duocad `web/src/viewport.ts` に合わせる。詳細はタスクの契約表):
    - 保持 = `conditions.keep` の三角形(拘束面ぶんは除く。**duocadは拘束面が勝つ**
      —— `applyFaceMaterials` の役割解決で `fixture` が `preserve` より優先される)。
    - 除外 = `conditions.obstacle` の三角形。**`space.model.obstacle`(ボクセルのマスク)は
      使わない** —— どちらの導出も除外を塊から引き切っているので、そのマスクは常に全 False
      (`model_from_design_mask` に `obstacle` を渡していない。モジュール docstring)。
    - 拘束面・荷重面は `soup.triangles_of_face(face)` で当該面だけを塗る/装飾する
      (面添字は `space.fixed_face` / `space.load_face`)。荷重面はduocad同様**面の色は
      変えず**(保持のまま)矢印とラベルだけを重ねる。
    """
    grid = space.grid
    soup = space.soup
    conditions = space.conditions
    if conditions.anchor is not conditions.keep:
        # デモは anchor_shape を省略している(= keep がそのまま面アンカーの対象)ので
        # space.soup がそのまま保持ソリッドの三角形分割になる。ここが崩れる呼び出しは
        # 想定していない(黙って別の形を「保持」として塗ることになるので早期に落とす)。
        raise ValueError(
            "conditions.anchor_shape が keep と異なる: 既定表示は space.soup を"
            "保持ソリッドの三角形分割として流用しているため、この経路は未対応"
        )
    fixed_face = space.fixed_face
    load_face = space.load_face
    fixed_tri = soup.triangles_of_face(fixed_face)
    keep_tri = np.setdiff1d(np.arange(soup.triangle_count), fixed_tri)
    positions = _to_index_space(soup.positions, grid)

    obstacle_mesh: Optional[dict[str, object]] = None
    if conditions.obstacle is not None:
        # 除外ソリッドは面アンカーの対象(`soup`)に含まれないので、自分で三角形分割する。
        # `deviation` は導出が塊の格子を決めるのに使った値と揃える(見た目だけの
        # 都合で、揃えなくても正しさには影響しない)。
        dev = float(space.stats["deviation"])
        obs_soup = vx.triangles_from_shape(vx._unwrap(conditions.obstacle), deviation=dev)
        obstacle_mesh = _mesh_of(_to_index_space(obs_soup.positions, grid), obs_soup.triangles)

    fixed_centroid = _to_index_space(
        np.asarray(soup.face_props[fixed_face]["centroid"], dtype=float).reshape(1, 3), grid
    )[0]
    fixed_normal = np.asarray(soup.face_props[fixed_face]["normal"], dtype=float)
    load_centroid = _to_index_space(
        np.asarray(soup.face_props[load_face]["centroid"], dtype=float).reshape(1, 3), grid
    )[0]
    force = np.asarray(DEMO_FORCE, dtype=float)
    norm = float(np.linalg.norm(force))
    # **矢印は実際に最適化へ渡す力の向き**(`DEMO_FORCE`)を描く。面法線ではない
    # —— duocad も `direction: 'vector' | '-normal' | 'normal'` を区別して持つのと同じ理由で、
    # 見た目と計算の入力を別々に決めると食い違いに気づけなくなる。
    direction = (force / norm).tolist() if norm > 0 else [0.0, 0.0, -1.0]

    nelx, nely, nelz = grid.shape
    return {
        "type": "conditions",
        "nelx": int(nelx),
        "nely": int(nely),
        "nelz": int(nelz),
        "max_iter": DEMO_MAX_ITER,
        "volfrac": DEMO_VOLFRAC,
        "keep": _mesh_of(positions, soup.triangles[keep_tri]),
        "obstacle": obstacle_mesh,
        "fixed": {
            "mesh": _mesh_of(positions, soup.triangles[fixed_tri]),
            "centroid": np.round(fixed_centroid, 4).tolist(),
            "normal": np.round(fixed_normal, 6).tolist(),
        },
        "load": {
            "centroid": np.round(load_centroid, 4).tolist(),
            "direction": [round(float(v), 6) for v in direction],
            "magnitude": norm,
        },
        # `?debug=space` のときだけクライアントが使う(本体のUX決定で塊は既定では見せない。
        # engine `topopt-design-space-never-shown-to-the-user`)。
        "space": {"design": np.flatnonzero(space.mask.ravel()).tolist()},
    }


# `?debug=anchors` のデモに使う解像度。**check_anchor.py の [7] と同じ 32** にしてあるので、
# 画面に出ている節点集合は受入検査が通したものとそのまま同じ。
ANCHOR_DEMO_ELEMENTS = 32


def _build_anchor_payload(elements_long_axis: int = ANCHOR_DEMO_ELEMENTS) -> dict[str, object]:
    """`?debug=anchors` 用: 設計空間のボクセルと、固定/荷重に割り当てた節点を送れる形にする。

    座標はビューアの規約に合わせる —— 要素 (ex,ey,ez) の中心が
    `(ex+0.5-nelx/2, ...)` なので、その角にある**節点 (i,j,k) は `(i-nelx/2, ...)`**。
    ここで規約を取り違えると、節点が形状から半セルずれた位置に描かれて
    「アンカーがずれている」ように見える(実際には合っているのに)。
    """
    model = vx.voxelize(anchor.demo_bracket(), elements_long_axis)
    table = anchor.build_anchors(model.soup, model.grid)
    fixed_face, load_face = anchor.demo_faces(model.soup)
    nelx, nely, nelz = model.grid.shape
    half = np.array([nelx, nely, nelz], dtype=float) / 2.0

    def nodes_of(face: int) -> list[float]:
        idx = table.nodes(face).astype(float)
        return np.round(idx - half, 4).ravel().tolist()

    return {
        "type": "anchors",
        "nelx": nelx,
        "nely": nely,
        "nelz": nelz,
        "spacing": model.grid.spacing,
        "design": np.flatnonzero(model.design.ravel()).tolist(),
        "fixed": nodes_of(fixed_face),
        "load": nodes_of(load_face),
        "fixed_face": fixed_face,
        "load_face": load_face,
        "fixed_area": table[fixed_face].total_area,
        "load_area": table[load_face].total_area,
        "seconds": table.stats["seconds"],
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

            if msg.get("mode") == "anchors":
                # **面アンカーの表示だけ**(最適化は回さない)。`?debug=anchors` 専用の
                # デモ(`anchor.demo_bracket()`)で、`design_space.demo_conditions()` とは別物。
                state.running = True
                try:
                    payload = await asyncio.to_thread(_build_anchor_payload)
                    await websocket.send(json.dumps(payload, ensure_ascii=False))
                except Exception as exc:  # noqa: BLE001
                    try:
                        await websocket.send(
                            json.dumps(
                                {"type": "error", "message": f"面アンカーの構築に失敗: {exc}"},
                                ensure_ascii=False,
                            )
                        )
                    except Exception:
                        pass
                finally:
                    state.running = False
                continue

            if msg.get("mode") == "conditions":
                # **起動直後の既定表示**(最適化は回さない)。完了条件1の実機確認はこの経路
                # (`shot_anchors.mjs` がボタンを押さずにこの応答だけで4色を数える)。
                state.running = True
                try:
                    payload = await asyncio.to_thread(_build_condition_payload, _get_demo_space())
                    await websocket.send(json.dumps(payload, ensure_ascii=False))
                except Exception as exc:  # noqa: BLE001
                    try:
                        await websocket.send(
                            json.dumps(
                                {"type": "error", "message": f"条件表示の構築に失敗: {exc}"},
                                ensure_ascii=False,
                            )
                        )
                    except Exception:
                        pass
                finally:
                    state.running = False
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
                # **ボタンは `design_space.run_topopt()` を回す(2026-09-04〜)。** 片持ち梁
                # プリセット(`simp.run_preset`)はもう呼ばない —— `check_simp.py` /
                # `check_mesh.py` / `check_anchor.py:173-182` がプリセットの既定挙動に
                # 依存しているので、プリセット自体(`simp.PRESET` / `Model` の既定境界条件)は
                # 消さずに残し、**呼ぶ先だけ**ここで切り替える。`_get_demo_space()` で
                # `_start_message()` と同じ `DesignSpace` を使う(表示と計算を一致させる完了条件3)。
                pipeline_result = await asyncio.to_thread(
                    ds.run_topopt,
                    _get_demo_space(),
                    volfrac=DEMO_VOLFRAC,
                    max_iter=DEMO_MAX_ITER,
                    force=DEMO_FORCE,
                    stl_path=DEMO_STL_PATH,
                    progress=callback,
                )
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
                result = pipeline_result["result"]  # simp.optimize() の生の Result
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

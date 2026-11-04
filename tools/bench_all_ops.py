"""文書を変える操作を全部、画面と同じ順(プレビュー → 確定 → 配信)と MCP と同じ順(確定 → 配信)で測る。

    .venv/Scripts/python.exe -X utf8 tools/bench_all_ops.py [--step <STEP>] [--only <名前の一部>] [--profile] [--out <json>]

文書は2つ: 合成の部品(板に穴・リブ・丸めを verb で作る。外部ファイル不要)と、`--step` を渡したときはその STEP の
最大のボディ。操作ごとに次を別々に記録する(すべてサーバ単体、WebSocket とブラウザの費用は `web/scripts/bench-basic-ops.mjs`):

- `preview`: 画面のゴーストが出るまでのサーバの仕事(`Session.preview_job` の写し作り + ワーカーの計算 + 三角形)
- `confirm`: Enter(`adopt` + `apply`。プレビューの結果を使い回す) / `push`: その後の配信1回(`state_meta` + `delta` + 省く規則 + `wire.encode`)
- `mcp`: プレビュー無しで確定(AI の経路。何も使い回さない) / `mcp_push`: その後の配信
- `undo` / `redo`: 操作 + 配信

`--profile` は全操作を cProfile で包み、関数ごとの累積時間を上位から出す(どの共通部品が全体に効いているか)。
"""
from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from duocad import kernel, kernel_worker  # noqa: E402
from duocad.net import wire  # noqa: E402
from duocad.session import Session  # noqa: E402

from duocad import brep_topology as _brep_topology  # noqa: E402

ROWS: list = []
PROFILE: Optional[cProfile.Profile] = None


def ms(fn: Callable[[], Any]) -> tuple:
    if PROFILE is not None:
        PROFILE.enable()
    t0 = time.perf_counter()
    try:
        result = fn()
    finally:
        elapsed = (time.perf_counter() - t0) * 1000.0
        if PROFILE is not None:
            PROFILE.disable()
    return elapsed, result


class Wire:
    """1つの接続(`Server._push` と同じ: 送り届けたものを覚えて省く)。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.delivered = wire.Delivered()

    def push(self) -> tuple:
        def deliver():
            meta = self.session.state_meta()
            delta = self.session.delta(self.delivered.revisions)
            frame = wire.encode({"type": "state", **self.delivered.compose(meta, delta["bodies"])})
            self.delivered.record(meta, delta["bodies"])
            return len(frame), len(delta["bodies"])
        elapsed, (size, bodies) = ms(deliver)
        return elapsed, size, bodies


def body_of(session: Session, body_id: str):
    return session.document.get_body(body_id)


def faces_of_kind(session: Session, body_id: str, kind: str) -> list:
    """`kind` で始まる種類の面(円柱は `cylinder_full` / `cylinder_partial`)を面積の大きい順に。"""
    geometry = session.geometry(body_id)
    return sorted((f for f in geometry["faces"] if f["kind"].startswith(kind)), key=lambda f: -f["area"])


def edges_of_kind(session: Session, body_id: str, kind: str, min_length: float = 0.0) -> list:
    return [e for e in session.edges(body_id)["edges"] if e["kind"] == kind and e["length"] > min_length]


def select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(add=refs)


def face_ref(body_id: str, index: int) -> dict:
    return {"body": body_id, "kind": "face", "index": int(index)}


def edge_ref(body_id: str, index: int) -> dict:
    return {"body": body_id, "kind": "edge", "index": int(index)}


def body_ref(body_id: str) -> dict:
    return {"body": body_id, "kind": "body", "index": None}


def last_sketch(session: Session) -> str:
    return session.state_meta()["sketches"][-1]["sketch_id"]


def run_case(doc_label: str, session: Session, link: Wire, name: str, verb: str, setup: Callable[[], dict]) -> None:
    """1操作を MCP の順と画面の順で測り、どちらの後も undo で元へ戻す。"""
    row: dict = {"doc": doc_label, "case": name, "verb": verb}
    before_count = len(session.document.log.entries)
    try:
        args = setup()
        selection_before = session.get_selection()
        row["mcp"], _ = ms(lambda: session.apply(verb, args))
        row["mcp_push"], row["mcp_bytes"], row["mcp_bodies"] = link.push()
        row["undo"], _ = ms(lambda: session.undo())
        row["undo_push"], row["undo_bytes"], _ = link.push()
        row["redo"], _ = ms(lambda: session.redo())
        row["redo_push"], row["redo_bytes"], _ = link.push()
        session.undo()
        link.push()
        # 画面の順: 選択を戻し、プレビュー(写しの上で計算)→ 採用 → 確定
        session.set_selection([dict(ref) for ref in selection_before["refs"]]) if selection_before.get("refs") else setup()
        args = setup()
        from duocad.session import PREVIEWABLE_VERBS
        if verb in PREVIEWABLE_VERBS:
            def preview():
                job = session.preview_job(verb, args)
                if job is None:
                    return None, session.preview(verb, args)
                with kernel_worker.preview_context():
                    return job, job.run()
            row["preview"], (job, result) = ms(preview)
            row["preview_bytes"] = len(wire.encode({"type": "preview", **result}))

            def confirm():
                if job is not None:
                    job.adopt()
                with session.background_autosave():
                    session.apply(verb, args)
            row["confirm"], _ = ms(confirm)
        else:
            def confirm_plain():
                with session.background_autosave():
                    session.apply(verb, args)
            row["confirm"], _ = ms(confirm_plain)
        pending = session.pending_write
        if pending is not None:
            pending.result()
        session.settle_autosave()
        row["push"], row["push_bytes"], row["push_bodies"] = link.push()
    except Exception as error:  # noqa: BLE001  1件の失敗で他を止めない
        row["error"] = f"{type(error).__name__}: {str(error)[:160]}"
    finally:
        while len(session.document.log.entries) > before_count:
            session.undo()
        session.clear_selection()
        link.push()
    ROWS.append(row)
    cells = " ".join(
        f"{key}={row[key]:.0f}" for key in ("preview", "confirm", "push", "mcp", "mcp_push", "undo", "undo_push", "redo", "redo_push")
        if isinstance(row.get(key), float)
    )
    print(f"  {doc_label:<5} {name:<34} {row.get('error') or cells}", flush=True)


#: `--part` の大きさ。big は 485 面(穴 384)で、QR1 のヒンジカバーの最大ボディ(733 面)の代わりに使う(リポジトリに STEP の題材が無い)。
#: 穴の縁まで丸めると、切り取り・丸め・分割が断られる形になった(実測)ので丸めない
PARTS = {"small": {"columns": 6, "rows": 4}, "big": {"columns": 24, "rows": 16}}


def build_part(session: Session, columns: int = 6, rows: int = 4) -> str:
    """板(120x80x8)に穴 columns×rows 個・リブ・外周の丸めを持つ合成の部品。返すのはボディの id。"""
    session.apply("sketch_create", {"base_plane": "XY"})
    sid = last_sketch(session)
    session.apply("add_rectangle", {"sketch_id": sid, "corner": [-60, -40], "width": 120, "height": 80})
    pitch_x, pitch_y = 108 / columns, 72 / rows
    diameter = min(6.0, 0.55 * min(pitch_x, pitch_y))
    for i in range(columns):
        for j in range(rows):
            session.apply("add_circle", {"sketch_id": sid, "center": [-54 + (i + 0.5) * pitch_x, -36 + (j + 0.5) * pitch_y], "diameter": diameter})
    session.leave_sketch()
    regions = session.state_meta()["sketches"][-1]["regions"]
    outer = max(regions, key=lambda r: r["area"])
    select(session, [{"sketch": sid, "index": outer["index"]}])
    session.apply("extrude", {"distance": 8, "mode": "new"})
    body_id = session.document.bodies[-1].id
    top = max((f for f in session.geometry(body_id)["faces"] if f["kind"] == "plane"), key=lambda f: f["centroid"][2])
    # 上面に貼ったスケッチでリブを立てる
    select(session, [face_ref(body_id, top["index"])])
    session.apply("sketch_create", {})
    rid = last_sketch(session)
    for k in range(3):
        session.apply("add_rectangle", {"sketch_id": rid, "corner": [-55, -35 + k * 30], "width": 110, "height": 3})
    session.leave_sketch()
    rib_regions = session.state_meta()["sketches"][-1]["regions"]
    select(session, [{"sketch": rid, "index": r["index"]} for r in rib_regions])
    session.apply("extrude", {"distance": 10, "mode": "join", "body_id": body_id})
    vertical = [
        e["index"] for e in edges_of_kind(session, body_id, "line")
        if abs(abs(e["points"][0]) - 60) < 1e-6 and abs(abs(e["points"][1]) - 40) < 1e-6 and abs(e["points"][0] - e["points"][3]) < 1e-9
    ]
    if vertical:
        select(session, [edge_ref(body_id, i) for i in vertical])
        try:
            session.apply("fillet", {"radius": 2.0})
        except Exception:  # noqa: BLE001  丸められなくても部品として使える
            pass
    session.clear_selection()
    return body_id


def blendable(session: Session, body_id: str, edges: list, count: int) -> list:
    """同じプロセスで丸めて通る辺を `count` 本まで(測る半径と違う値で試す: 覚えた結果に当たって測りが軽くならないように)。"""
    picked: list = []
    shape = body_of(session, body_id).shape
    for edge in edges:
        if len(picked) >= count:
            break
        try:
            kernel.fillet_edges(shape, {edge["index"]: 0.45})
            kernel.chamfer_edges(shape, {edge["index"]: 0.45})
        except Exception:  # noqa: BLE001
            continue
        picked.append(edge)
    return picked


def cases_for(session: Session, body_id: str, tool_body: Optional[str]) -> list:
    """(名前, verb, 選択して引数を返す関数) の並び。"""
    planes = faces_of_kind(session, body_id, "plane")
    cylinders = faces_of_kind(session, body_id, "cylinder_full")
    lines = blendable(session, body_id, edges_of_kind(session, body_id, "line", 3.0), 4)
    circles = blendable(session, body_id, edges_of_kind(session, body_id, "circle"), 1)
    top = max(planes, key=lambda f: f["centroid"][2])
    bottom = min(planes, key=lambda f: f["centroid"][2])
    side = next((f for f in planes if abs(f["normal"][2]) < 0.1), planes[0])
    bbox = session.geometry(body_id)["bbox"]
    cx, cy = bbox["center"][0], bbox["center"][1]
    zmax = bbox["max"][2]
    out: list = []

    def sketch_on_xy_above():
        session.apply("sketch_create", {"base_plane": "XY"})
        return last_sketch(session)

    def region_case(name: str, mode: str, distance: float, width: float = 6.0) -> None:
        def setup():
            sid = sketch_on_xy_above()
            session.apply("add_rectangle", {"sketch_id": sid, "corner": [cx - width / 2, cy - width / 2], "width": width, "height": width})
            session.leave_sketch()
            select(session, [{"sketch": sid, "index": 1}])
            return {"distance": distance, "mode": mode, **({"body_id": body_id} if mode != "new" else {})}
        out.append((name, "extrude", setup))

    def revolve_setup():
        sid = sketch_on_xy_above()
        session.apply("add_rectangle", {"sketch_id": sid, "corner": [cx - 3.0, cy + 2.0], "width": 6.0, "height": 4.0})
        session.leave_sketch()
        select(session, [{"sketch": sid, "index": 1}])
        return {"axis": "x", "angle": 90.0, "mode": "new"}

    out.append(("revolve 領域 new 90°", "revolve", revolve_setup))
    region_case("extrude 領域 new", "new", 5.0)
    region_case("extrude 領域 join(ボディを貫く)", "join", zmax + 5.0)
    region_case("extrude 領域 cut(貫通)", "cut", zmax + 5.0)
    out.append(("extrude 面 join 0.5", "extrude", lambda: (select(session, [face_ref(body_id, top["index"])]), {"distance": 0.5, "mode": "join"})[1]))
    out.append(("extrude 側面 join 1", "extrude", lambda: (select(session, [face_ref(body_id, side["index"])]), {"distance": 1.0, "mode": "join"})[1]))
    if lines:
        out.append(("fillet 辺1本 r=0.5", "fillet", lambda: (select(session, [edge_ref(body_id, lines[0]["index"])]), {"radius": 0.5})[1]))
        out.append(("fillet 辺4本 r=0.5", "fillet", lambda: (select(session, [edge_ref(body_id, e["index"]) for e in lines[:4]]), {"radius": 0.5})[1]))
        out.append(("chamfer 辺1本 0.5", "chamfer", lambda: (select(session, [edge_ref(body_id, lines[0]["index"])]), {"distance": 0.5})[1]))
    if circles:
        out.append(("fillet 穴の縁 r=0.3", "fillet", lambda: (select(session, [edge_ref(body_id, circles[0]["index"])]), {"radius": 0.3})[1]))
    out.append(("shell_body 底面を抜く t=0.8", "shell_body", lambda: (select(session, [face_ref(body_id, bottom["index"])]), {"thickness": 0.8})[1]))
    out.append(("offset_faces 側面 0.3", "offset_faces", lambda: (select(session, [face_ref(body_id, side["index"])]), {"distance": 0.3})[1]))
    if cylinders:
        hole = cylinders[-1]
        out.append(("delete_faces 穴1つ", "delete_faces", lambda: (select(session, [face_ref(body_id, hole["index"])]), {})[1]))
        out.append(("thread 穴 trapezoid30", "thread", lambda: (select(session, [face_ref(body_id, hole["index"])]), {"profile": "trapezoid30"})[1]))
    out.append(("move_body 平行移動", "move_body", lambda: (select(session, [body_ref(body_id)]), {"dx": 1.0, "dy": 0.0, "dz": 0.0})[1]))
    out.append(("move_body 複製", "move_body", lambda: (select(session, [body_ref(body_id)]), {"dx": 200.0, "dy": 0.0, "dz": 0.0, "copy": True})[1]))
    out.append(("mirror_body YZ", "mirror_body", lambda: (select(session, [body_ref(body_id)]), {"base_plane": "YZ"})[1]))
    out.append(("split_body YZ", "split_body", lambda: (select(session, [body_ref(body_id)]), {"base_plane": "YZ"})[1]))
    if tool_body is not None:
        out.append(("boolean_body cut", "boolean_body", lambda: (select(session, [body_ref(tool_body)]), {"target_ref": face_ref(body_id, top["index"]), "mode": "cut"})[1]))
    out.append(("delete ボディ", "delete", lambda: (select(session, [body_ref(body_id)]), {})[1]))

    def sketch_case(name: str, verb: str, make: Callable[[str], dict]) -> None:
        def setup():
            sid = sketch_on_xy_above()
            session.clear_selection()
            return make(sid)
        out.append((name, verb, setup))

    sketch_case("add_line(スケッチ内)", "add_line", lambda sid: {"sketch_id": sid, "p0": [0, 0], "p1": [10, 5]})
    sketch_case("add_circle(スケッチ内)", "add_circle", lambda sid: {"sketch_id": sid, "center": [cx, cy], "diameter": 4})
    sketch_case("sketch_create XY", "sketch_create", lambda sid: {"base_plane": "XZ"})
    return out


def camera_and_selection(doc_label: str, session: Session, link: Wire, body_id: str) -> None:
    """カメラ(粗さの決め直し)・選択・辺/頂点の取り寄せ。"""
    def record(name: str, value_ms: float, **extra) -> None:
        ROWS.append({"doc": doc_label, "case": name, "ms": value_ms, **extra})
        tail = " ".join(f"{k}={v}" for k, v in extra.items())
        print(f"  {doc_label:<5} {name:<34} {value_ms:.1f} ms {tail}", flush=True)

    total = _brep_topology.face_count(body_of(session, body_id).shape)
    bbox = session.geometry(body_id)["bbox"]
    diag = bbox["diagonal"]
    session.set_view_resolution(diag / 1200.0, diag)
    t, size, _ = link.push(); record("camera 全体表示 配信", t, bytes=size)
    focus = {body_id: list(range(1, min(total, 30) + 1))}
    session.set_view_resolution(diag / 12000.0, diag, focus)
    t, size, _ = link.push(); record("camera ズームイン(30面) 配信", t, bytes=size)
    focus = {body_id: list(range(31, min(total, 60) + 1)) or [1]}
    session.set_view_resolution(diag / 12000.0, diag, focus)
    t, size, _ = link.push(); record("camera 回転(別の30面) 配信", t, bytes=size)
    session.set_view_resolution(diag / 1200.0, diag)
    t, size, _ = link.push(); record("camera 全体表示へ戻る 配信", t, bytes=size)
    t, _ = ms(lambda: (session.modify_selection(add=[face_ref(body_id, 1)]), link.push())); record("select 面1つ + 配信", t)
    t, _ = ms(lambda: (session.clear_selection(), link.push())); record("clear_selection + 配信", t)
    t, _ = ms(lambda: session.begin_command("fillet")); record("begin_command fillet", t)
    session.end_command()
    session.view._edge_cache.clear()
    t, _ = ms(lambda: session.edges(body_id)); record("edges 初回", t)
    session.view._vertex_cache.clear()
    t, _ = ms(lambda: session.vertices(body_id)); record("vertices 初回", t)


def bench_doc(doc_label: str, session: Session, body_id: str, tool_body: Optional[str], only: str) -> None:
    link = Wire(session)
    t, size, _ = link.push()
    ROWS.append({"doc": doc_label, "case": "初回配信", "ms": t, "bytes": size})
    print(f"  {doc_label:<5} {'初回配信':<34} {t:.0f} ms bytes={size}", flush=True)
    camera_and_selection(doc_label, session, link, body_id)
    for name, verb, setup in cases_for(session, body_id, tool_body):
        if only and only not in name and only != verb:
            continue
        run_case(doc_label, session, link, name, verb, setup)


def main() -> None:
    global PROFILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=Path, default=None)
    ap.add_argument("--only", default="")
    ap.add_argument("--docs", default="part,step")
    ap.add_argument("--part", choices=sorted(PARTS), default="small")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--top", type=int, default=45)
    ap.add_argument("--out", type=Path, default=REPO / "output" / "bench_basic_ops" / "all_ops.json")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="duocad-bench-all-"))
    print(f"models_dir={tmp}  python={sys.version.split()[0]}")
    if args.profile:
        PROFILE = cProfile.Profile()
    try:
        if "part" in args.docs:
            print("\n## 合成の部品")
            s = Session(kernel.DEFAULT_DEVIATION, models_dir=tmp)
            t0 = time.perf_counter()
            body_id = build_part(s, **PARTS[args.part])
            print(f"  (部品を作るのに {(time.perf_counter() - t0) * 1000:.0f} ms、面 {_brep_topology.face_count(body_of(s, body_id).shape)})")
            # ブーリアンの相手(板を横切る棒)
            s.apply("sketch_create", {"base_plane": "XZ"})
            sid = last_sketch(s)
            s.apply("add_rectangle", {"sketch_id": sid, "corner": [-70, 2], "width": 140, "height": 3})
            s.leave_sketch()
            select(s, [{"sketch": sid, "index": 1}])
            s.apply("extrude", {"distance": 30, "mode": "new"})
            tool = s.document.bodies[-1].id
            s.clear_selection()
            bench_doc("part", s, body_id, tool, args.only)
        if "step" in args.docs and args.step is not None and args.step.exists():
            print(f"\n## STEP: {args.step.name}")
            s = Session(kernel.DEFAULT_DEVIATION, models_dir=tmp)
            t, _ = ms(lambda: s.apply("import_step", {"path": str(args.step)}))
            ROWS.append({"doc": "step", "case": "import_step", "ms": t})
            print(f"  step  {'import_step':<34} {t:.0f} ms")
            big = max(s.document.bodies, key=lambda b: _brep_topology.face_count(b.shape))
            others = [b.id for b in s.document.bodies if b is not big]
            print(f"  (最大のボディ {big.name}: 面 {_brep_topology.face_count(big.shape)})")
            bench_doc("step", s, big.id, others[0] if others else None, args.only)
            name = s.current_name
            s2 = Session(kernel.DEFAULT_DEVIATION, models_dir=tmp, opened=False)
            t, _ = ms(lambda: s2.open_document(name))
            ROWS.append({"doc": "step", "case": "open_document(再生)", "ms": t})
            print(f"  step  {'open_document(再生)':<34} {t:.0f} ms")
    finally:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"rows": ROWS, "python": sys.version}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {args.out}")
        if PROFILE is not None:
            stats = pstats.Stats(PROFILE)
            stats.sort_stats("cumulative").print_stats(args.top)
            stats.sort_stats("tottime").print_stats(args.top)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

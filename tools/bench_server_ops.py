"""サーバ単体(ネットワーク無し)で、基本操作1回ぶんのサーバ側の費用を測る。

    .venv/Scripts/python.exe tools/bench_server_ops.py [--step <STEP>] [--doc <models/*.json>] [--out <json>] [--only ABCD]

測るのは `Server._push` が1配信で払うもの(`state_meta()` + `delta(known)` + `json.dumps`)と、その前に走る verb /
選択 / プレビュー / 解像度の決め直し / 自動保存。WebSocket とブラウザの費用は `web/scripts/bench-basic-ops.mjs` が別に測る。
結果は標準出力に表で出し、`--out` に JSON で残す(`output/` は git 管理外)。
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from duocad import kernel, kernel_sketch  # noqa: E402
from duocad.session import Session  # noqa: E402

PC_CASE = Path(r"C:\Users\tm976\mf\3d-models\mATX_PC_case_20251122\PC_case_20251122.step")
ROWS: list[dict] = []


def record(label: str, first_ms: float, median_ms=None, **extra) -> dict:
    row = {"label": label, "first_ms": round(first_ms, 2), "median_ms": None if median_ms is None else round(median_ms, 2), **extra}
    ROWS.append(row)
    med = "" if median_ms is None else f"  median {median_ms:>9.2f} ms"
    tail = "  " + " ".join(f"{k}={v}" for k, v in extra.items()) if extra else ""
    print(f"  {label:<60} first {first_ms:>9.2f} ms{med}{tail}")
    return row


def timed(label: str, fn, repeat: int = 3, **extra):
    """fn を repeat 回呼び、最初の1回と中央値[ms]を記録する。戻り値は最後の結果。"""
    samples = []
    result = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    record(label, samples[0], statistics.median(samples), **extra)
    return result


def once(label: str, fn, **extra):
    t0 = time.perf_counter()
    result = fn()
    record(label, (time.perf_counter() - t0) * 1000.0, None, **extra)
    return result


def jsize(payload) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def push_payload(session: Session, known: dict) -> dict:
    """`Server._push` が1接続へ送るものと同じ形。"""
    meta = session.state_meta()
    delta = session.delta(known)
    return {"type": "state", "state": meta, "delta": {"bodies": delta["bodies"]}}


def known_of(session: Session) -> dict:
    return {b["body_id"]: b["revision"] for b in session.state_meta()["bodies"]}


def push_after(label: str, session: Session, known: dict, action) -> dict:
    """操作1回とその後の配信を別々に記録し、新しい known を返す。"""
    t0 = time.perf_counter()
    action()
    act_ms = (time.perf_counter() - t0) * 1000.0
    t1 = time.perf_counter()
    payload = push_payload(session, known)
    push_ms = (time.perf_counter() - t1) * 1000.0
    record(f"{label} 操作", act_ms)
    record(f"{label} 後の配信 state_meta+delta", push_ms, bytes=jsize(payload), bodies_sent=len(payload["delta"]["bodies"]))
    return known_of(session)


def section(title: str) -> None:
    print(f"\n## {title}")


def sketch_id_of(session: Session) -> str:
    return session.state_meta()["sketches"][-1]["sketch_id"]


def bench_small_box(models_dir: Path) -> None:
    section("A. 小さな文書(20mm 角の箱を作ってフィレット)")
    s = Session(None, kernel.DEFAULT_DEVIATION, models_dir=models_dir)
    s.apply("sketch_create", {"base_plane": "XY"})
    sid = sketch_id_of(s)
    s.apply("add_rectangle", {"sketch_id": sid, "corner": [0, 0], "width": 20, "height": 20})
    s.modify_selection(add=[{"sketch": sid, "index": 1}])
    once("A extrude(20mm箱) apply (verb+autosave)", lambda: s.apply("extrude", {"distance": 20, "mode": "new"}))
    s.leave_sketch()
    s.clear_selection()
    body = s.document.bodies[-1]
    s.set_view_resolution(0.05, 35.0)
    full = once("A 初回配信 state_meta+delta({}) (全テセレーション)", lambda: push_payload(s, {}))
    ROWS[-1]["bytes"] = jsize(full)
    known = known_of(s)
    timed("A 変化なし配信 state_meta+delta(known)", lambda: push_payload(s, known), repeat=5, bytes=jsize(push_payload(s, known)))
    timed("A json.dumps(配信1回・変化なし)", lambda: json.dumps(push_payload(s, known), ensure_ascii=False), repeat=5)
    face_ref = {"body": body.id, "kind": "face", "index": 1}
    timed("A modify_selection(面1) + 配信", lambda: (s.modify_selection(add=[face_ref]), push_payload(s, known)), repeat=5)
    s.clear_selection()
    once("A edges(body) 初回", lambda: s.edges(body.id)); ROWS[-1]["bytes"] = jsize(s.edges(body.id))
    timed("A edges(body) キャッシュ", lambda: s.edges(body.id), repeat=5)
    once("A vertices(body) 初回", lambda: s.vertices(body.id))
    edge_ref = {"body": body.id, "kind": "edge", "index": 1}
    s.modify_selection(add=[edge_ref])
    pv = timed("A preview(fillet r=2) 1辺", lambda: s.preview("fillet", {"radius": 2.0}), repeat=5)
    ROWS[-1]["bytes"] = jsize(pv)
    known = push_after("A fillet r=2", s, known, lambda: s.apply("fillet", {"radius": 2.0}))
    timed("A autosave() 単体", lambda: s.autosave(), repeat=5, file_bytes=(models_dir / s.current_name).stat().st_size)
    known = push_after("A undo", s, known, lambda: s.undo())
    for wpp in (0.005, 0.05):
        changed = s.set_view_resolution(wpp, 35.0)
        payload = once(f"A set_view_resolution(wpp={wpp}) 後の配信", lambda: push_payload(s, known))
        ROWS[-1].update(changed=changed, bytes=jsize(payload), bodies_sent=len(payload["delta"]["bodies"]))
        known = known_of(s)


def bench_sketch_heavy(models_dir: Path, n_rect: int = 150) -> None:
    section(f"B. スケッチが重い文書(長方形 {n_rect} 個 = 線 {n_rect * 4} 本)")
    s = Session(None, kernel.DEFAULT_DEVIATION, models_dir=models_dir)
    s.apply("sketch_create", {"base_plane": "XY"})
    sid = sketch_id_of(s)
    t0 = time.perf_counter()
    for i in range(n_rect):
        s.apply("add_rectangle", {"sketch_id": sid, "corner": [(i % 15) * 12, (i // 15) * 12], "width": 10, "height": 10})
    total = (time.perf_counter() - t0) * 1000.0
    record(f"B add_rectangle x{n_rect} 合計 (verb+autosave 各回)", total, total / n_rect)
    meta = timed("B state_meta() (スケッチ入り)", lambda: s.state_meta(), repeat=5)
    ROWS[-1].update(bytes=jsize(meta), sketch_bytes=jsize(meta["sketches"]), log_bytes=jsize(meta["log"]), log_entries=len(meta["log"]))
    print(f"      bytes={ROWS[-1]['bytes']} sketches={ROWS[-1]['sketch_bytes']} log={ROWS[-1]['log_bytes']} log_entries={ROWS[-1]['log_entries']}")
    timed("B json.dumps(state_meta)", lambda: json.dumps(s.state_meta(), ensure_ascii=False), repeat=5)
    timed("B add_rectangle 1回 apply (verb+autosave)", lambda: s.apply("add_rectangle", {"sketch_id": sid, "corner": [500, 500], "width": 1, "height": 1}), repeat=3)
    timed("B autosave() 単体", lambda: s.autosave(), repeat=5, file_bytes=(models_dir / s.current_name).stat().st_size)
    timed("B modify_selection(領域1) 単体", lambda: s.modify_selection(add=[{"sketch": sid, "index": 1}]), repeat=5)
    timed("B 選択変更の直後の state_meta()", lambda: (s.modify_selection(add=[{"sketch": sid, "index": 2}]), s.state_meta()), repeat=5)
    timed("B 連続2回目の state_meta()", lambda: s.state_meta(), repeat=5)
    detections: list = []
    real_detect = kernel_sketch.sketch_regions
    kernel_sketch.sketch_regions = lambda *a, **k: detections.append(1) or real_detect(*a, **k)
    try:
        for index in range(3, 8):
            s.modify_selection(add=[{"sketch": sid, "index": index}])
            s.state_meta()
    finally:
        kernel_sketch.sketch_regions = real_detect
    record("B 領域検出の回数(領域クリック5回+各回の配信)", float(len(detections)))
    s.clear_selection()
    n_sel = min(n_rect, 60)
    s.modify_selection(add=[{"sketch": sid, "index": i} for i in range(1, n_sel + 1)])
    once(f"B extrude(領域{n_sel}) apply (verb+autosave)", lambda: s.apply("extrude", {"distance": 5, "mode": "new"})); ROWS[-1]["bodies"] = len(s.document.bodies)
    s.set_view_resolution(0.2, 200.0)
    full = once("B 初回配信 state_meta+delta({})", lambda: push_payload(s, {}))
    ROWS[-1].update(bytes=jsize(full), bodies_sent=len(full["delta"]["bodies"]))
    known = known_of(s)
    timed("B 変化なし配信 state_meta+delta(known)", lambda: push_payload(s, known), repeat=5, bytes=jsize(push_payload(s, known)))


def bench_pc_case(models_dir: Path, step: Path) -> None:
    section(f"C. PCケース STEP(27部品 2835面): {step.name}")
    s = Session(None, kernel.DEFAULT_DEVIATION, models_dir=models_dir)
    once("C import_step apply (読込+autosave)", lambda: s.apply("import_step", {"path": str(step)})); ROWS[-1]["bodies"] = len(s.document.bodies)
    timed("C autosave() 単体", lambda: s.autosave(), repeat=3, file_bytes=(models_dir / s.current_name).stat().st_size)
    # 全体表示の解像度(1280x860 で対角 640mm ≒ 0.75mm/px)
    s.set_view_resolution(0.75, 640.0)
    full = once("C 初回配信 state_meta+delta({}) (全27部品テセレーション)", lambda: push_payload(s, {}))
    ROWS[-1].update(
        bytes=jsize(full),
        triangles=sum(len(f["indices"]) // 3 for b in full["delta"]["bodies"] for f in b["geometry"]["faces"]),
    )
    print(f"      bytes={ROWS[-1]['bytes']} triangles={ROWS[-1]['triangles']}")
    timed("C json.dumps(初回配信)", lambda: json.dumps(full, ensure_ascii=False), repeat=3)
    known = known_of(s)
    timed("C 変化なし配信 state_meta+delta(known)", lambda: push_payload(s, known), repeat=5, bytes=jsize(push_payload(s, known)))
    timed("C state_meta() 単体", lambda: s.state_meta(), repeat=5)
    timed("C json.dumps(state_meta)", lambda: json.dumps(s.state_meta(), ensure_ascii=False), repeat=5)
    big = max(s.document.bodies, key=lambda b: kernel.face_count(b.shape))
    nfaces = kernel.face_count(big.shape)
    nedges = kernel.edge_count(big.shape)
    print(f"  最大ボディ: {big.name} faces={nfaces} edges={nedges}")
    timed("C kernel.tessellate(最大ボディ, dev=0.26)", lambda: kernel.tessellate(big.shape, 0.26, kernel.DEFAULT_ANGULAR_DEFLECTION), repeat=3)
    face_ref = {"body": big.id, "kind": "face", "index": 1}
    timed("C modify_selection(面1) + 配信", lambda: (s.modify_selection(add=[face_ref]), push_payload(s, known)), repeat=5)
    s.clear_selection()
    all_faces = [{"body": b.id, "kind": "face", "index": i} for b in s.document.bodies for i in range(1, kernel.face_count(b.shape) + 1)]
    timed(f"C modify_selection(全面 {len(all_faces)}) + 配信", lambda: (s.modify_selection(add=all_faces), push_payload(s, known)), repeat=3)
    ROWS[-1]["bytes"] = jsize(push_payload(s, known))
    s.clear_selection()
    timed("C clear_selection + 配信", lambda: (s.clear_selection(), push_payload(s, known)), repeat=5)
    once("C edges(最大ボディ) 初回", lambda: s.edges(big.id)); ROWS[-1]["bytes"] = jsize(s.edges(big.id))
    timed("C edges(最大ボディ) キャッシュ", lambda: s.edges(big.id), repeat=5)
    once("C edge_chains(最大ボディ 辺1本) 初回", lambda: s.edge_chains(big.id, [1]))
    timed("C edge_chains(最大ボディ 辺1本) 次の辺", lambda: s.edge_chains(big.id, [len(ROWS) % nedges + 1]), repeat=5)
    once("C edge_chains(最大ボディ 全辺) 初回", lambda: s.edge_chains(big.id, list(range(1, nedges + 1))))
    once("C vertices(最大ボディ) 初回", lambda: s.vertices(big.id)); ROWS[-1]["bytes"] = jsize(s.vertices(big.id))
    per_body = []
    for b in s.document.bodies:
        if b is big:
            continue
        t0 = time.perf_counter(); s.edges(b.id); per_body.append(((time.perf_counter() - t0) * 1000.0, b.name, kernel.edge_count(b.shape)))
    worst = max(per_body)
    record("C edges 残り26部品 初回 合計", sum(p[0] for p in per_body), worst_ms=round(worst[0], 1), worst_body=worst[1], worst_edges=worst[2])
    # ズーム: 最大ボディの20面だけ細かく、他は粗く
    focus = {b.id: [] for b in s.document.bodies}
    focus[big.id] = list(range(1, 21))
    changed = s.set_view_resolution(0.02, 640.0, focus)
    payload = once("C ズームイン set_view_resolution(wpp=0.02, focus 20面) 配信", lambda: push_payload(s, known))
    ROWS[-1].update(changed=changed, bytes=jsize(payload), bodies_sent=len(payload["delta"]["bodies"]))
    known = known_of(s)
    # 回転: 同じズームで focus の面が入れ替わる
    focus[big.id] = list(range(21, 41))
    changed = s.set_view_resolution(0.02, 640.0, focus)
    payload = once("C 回転 focus 入れ替え(20面→別の20面) 配信", lambda: push_payload(s, known))
    ROWS[-1].update(changed=changed, bytes=jsize(payload), bodies_sent=len(payload["delta"]["bodies"]))
    known = known_of(s)
    # 回転で別のボディが視界に入る(5ボディが粗→細 10面ずつ)
    for b in [b for b in s.document.bodies if b is not big][:5]:
        focus[b.id] = list(range(1, min(10, kernel.face_count(b.shape)) + 1))
    changed = s.set_view_resolution(0.02, 640.0, focus)
    payload = once("C 回転 別の5ボディが視界に入る 配信", lambda: push_payload(s, known))
    ROWS[-1].update(changed=changed, bytes=jsize(payload), bodies_sent=len(payload["delta"]["bodies"]))
    known = known_of(s)
    changed = s.set_view_resolution(0.75, 640.0, None)
    payload = once("C ズームアウト 全体表示へ戻る 配信", lambda: push_payload(s, known))
    ROWS[-1].update(changed=changed, bytes=jsize(payload), bodies_sent=len(payload["delta"]["bodies"]))
    known = known_of(s)
    # プレビュー: 最大ボディの直線の辺1本にフィレット
    edges = s.edges(big.id)["edges"]
    target = None
    for e in (e for e in edges if e["kind"] == "line" and e["length"] > 5):
        s.clear_selection()
        s.modify_selection(add=[{"body": big.id, "kind": "edge", "index": e["index"]}])
        try:
            s.preview("fillet", {"radius": 0.5})
            target = e
            break
        except Exception:
            continue
    if target is not None:
        pv = timed(f"C preview(fillet r=0.5) 最大ボディ 辺#{target['index']}", lambda: s.preview("fillet", {"radius": 0.5}), repeat=3)
        ROWS[-1]["bytes"] = jsize(pv)
        known = push_after("C fillet r=0.5 最大ボディ", s, known, lambda: s.apply("fillet", {"radius": 0.5}))
        known = push_after("C undo", s, known, lambda: s.undo())
    else:
        print("  フィレットできる辺が見つからなかった")
    s.clear_selection()
    s.modify_selection(add=[{"body": big.id, "kind": "body", "index": None}])
    try:
        pv = timed("C preview(move_body translate 1mm) 最大ボディ", lambda: s.preview("move_body", {"mode": "translate", "vector": [1, 0, 0]}), repeat=3)
        ROWS[-1]["bytes"] = jsize(pv)
    except Exception as error:  # noqa: BLE001
        print(f"  move_body preview skipped: {error}")
    s.clear_selection()


def bench_mesh_doc(models_dir: Path, doc: Path) -> None:
    section(f"D. メッシュボディ入りの文書を開く: {doc.name} ({doc.stat().st_size / 1e6:.1f} MB)")
    shutil.copy(doc, models_dir / doc.name)
    s = Session(None, kernel.DEFAULT_DEVIATION, models_dir=models_dir, opened=False)
    once("D open_document (ログ再生)", lambda: s.open_document(doc.name))
    s.set_view_resolution(0.3, 200.0)
    full = once("D 初回配信 state_meta+delta({})", lambda: push_payload(s, {}))
    ROWS[-1]["bytes"] = jsize(full)
    timed("D json.dumps(初回配信)", lambda: json.dumps(full, ensure_ascii=False), repeat=3)
    known = known_of(s)
    timed("D 変化なし配信 state_meta+delta(known)", lambda: push_payload(s, known), repeat=5, bytes=jsize(push_payload(s, known)))
    timed("D autosave() 単体", lambda: s.autosave(), repeat=3, file_bytes=(models_dir / s.current_name).stat().st_size)

    def ui_verb():
        with s.background_autosave():
            s.apply("sketch_create", {"base_plane": "XY"})

    timed("D 画面の verb 1回(書き込みは書き手スレッドへ。応答まで)", ui_verb, repeat=3)
    s._drain_writes()
    timed("D 同じ verb をその場で保存(MCP の経路)", lambda: s.apply("sketch_create", {"base_plane": "XY"}), repeat=3)
    solid = next((b for b in s.document.bodies if b.shape is not None and b.visible), None)
    if solid is not None:
        timed("D modify_selection(面1 of solid) + 配信", lambda: (s.modify_selection(add=[{"body": solid.id, "kind": "face", "index": 1}]), push_payload(s, known)), repeat=5)
        s.clear_selection()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=Path, default=PC_CASE)
    ap.add_argument("--doc", type=Path, default=REPO / "models" / "hanger_bar_attachment.json")
    ap.add_argument("--out", type=Path, default=REPO / "output" / "bench_basic_ops" / "server_ops.json")
    ap.add_argument("--only", default="ABCD")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="duocad-bench-"))
    print(f"models_dir={tmp}  python={sys.version.split()[0]}")
    try:
        if "A" in args.only:
            bench_small_box(tmp)
        if "B" in args.only:
            bench_sketch_heavy(tmp)
        if "C" in args.only and args.step.exists():
            bench_pc_case(tmp, args.step)
        if "D" in args.only and args.doc.exists():
            bench_mesh_doc(tmp, args.doc)
    finally:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"rows": ROWS, "python": sys.version}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {args.out}")
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

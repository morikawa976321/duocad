"""ループを塞ぐ3つの原因を、サーバを立てずに単体で測る(`tools/bench_request_paths.py` の結果の裏付け)。

    uv run python -X utf8 tools/probe_loop_blockers.py [--part small|big] [--step <STEP>] [--only gil|deflate|roundtrip] [--out <json>]

- gil: ワーカースレッドが OCCT の三角形分割をしている間に、主スレッドが 1 ms おきに起きられるか(起きられなかった最長の間)。
  OCP は OCCT の呼び出しの間 GIL を放さないので、背景の測り直し・プレビューのワーカーでも主スレッド(ループ)は止まる
- deflate: 状態の配信1回ぶんのフレームを、websockets が既定で結ぶ permessage-deflate と同じ設定(窓 12 ビット・memLevel 5)で
  圧縮する時間と大きさ。圧縮は送信(`connection.send`)の中、ループの上で走る
- roundtrip: 形を BinTools で往復(子プロセスへ渡すのと同じ `kernel_worker._encode` / `_decode`)させてから分割した三角形が、
  写し(`kernel.copy_shape`)を分割したものとビット単位で同じか、と往復の時間
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import bench_all_ops as base  # noqa: E402
from duocad import brep_topology, kernel, kernel_worker  # noqa: E402
from duocad.net import wire  # noqa: E402
from duocad.session import Session  # noqa: E402

now = time.perf_counter


def part_session(part: str, step: "Path | None" = None) -> tuple:
    """合成の部品か、STEP の面の最も多いボディ。"""
    session = Session(kernel.DEFAULT_DEVIATION, models_dir=Path(tempfile.mkdtemp(prefix="duocad-blockers-")))
    if step is not None:
        session.apply("import_step", {"path": str(step)})
        body_id = max(session.document.bodies, key=lambda body: brep_topology.face_count(body.shape)).id
    else:
        body_id = base.build_part(session, **base.PARTS[part])
    diag = session.geometry(body_id)["bbox"]["diagonal"]
    return session, body_id, diag


def recipes(diag: float) -> list:
    """全体表示とズームインの、`view` が使う (偏差, 角度偏差)。"""
    from duocad import resolution
    return [resolution.view_resolution(diag / 1200.0, diag), resolution.view_resolution(diag / 12000.0, diag)]


def measure_gil(session: Session, body_id: str, diag: float) -> list:
    shape = session.document.get_body(body_id).shape
    faces = list(range(1, min(brep_topology.face_count(shape), 30) + 1))
    rows = []
    for recipe in recipes(diag):
        copy = kernel.copy_shape(shape)
        done = threading.Event()
        spent: list = []

        def work() -> None:
            t0 = now()
            kernel.tessellate_faces(copy, {recipe: faces})
            spent.append(now() - t0)
            done.set()

        gaps = []
        worker = threading.Thread(target=work)
        last = now()
        worker.start()
        while not done.is_set():
            time.sleep(0.001)
            tick = now()
            gaps.append(tick - last)
            last = tick
        worker.join()
        rows.append({"recipe": recipe, "faces": len(faces), "worker_ms": round(spent[0] * 1000, 1),
                     "main_longest_gap_ms": round(max(gaps) * 1000, 1), "main_ticks": len(gaps)})
        print(f"  gil: recipe={tuple(round(v, 4) for v in recipe)} 面 {len(faces)}: ワーカー {rows[-1]['worker_ms']} ms、"
              f"主スレッドが起きられなかった最長 {rows[-1]['main_longest_gap_ms']} ms", flush=True)
    return rows


def measure_deflate(session: Session, body_id: str, diag: float) -> list:
    rows = []
    for recipe in recipes(diag)[:1]:
        session.set_view_resolution(diag / 1200.0, diag)
        meta = session.state_meta()
        frame = wire.encode({"type": "state", **wire.Delivered().compose(meta, session.delta({})["bodies"])})
        for level in (-1, 1, 0):
            compressor = zlib.compressobj(level=level, wbits=-12, memLevel=5)
            t0 = now()
            packed = compressor.compress(frame) + compressor.flush(zlib.Z_SYNC_FLUSH)
            spent = now() - t0
            rows.append({"frame_bytes": len(frame), "level": level, "ms": round(spent * 1000, 1), "bytes": len(packed)})
            print(f"  deflate: フレーム {len(frame) / 1e6:.2f} MB、level {level}: {spent * 1000:.1f} ms → {len(packed) / 1e6:.2f} MB "
                  f"({len(packed) / len(frame):.0%})", flush=True)
        inflater = zlib.decompressobj(wbits=-12)
        compressor = zlib.compressobj(level=-1, wbits=-12, memLevel=5)
        packed = compressor.compress(frame) + compressor.flush(zlib.Z_SYNC_FLUSH)
        t0 = now()
        inflater.decompress(packed)
        rows.append({"inflate_ms": round((now() - t0) * 1000, 1)})
        print(f"  deflate: 戻す側 {rows[-1]['inflate_ms']} ms", flush=True)
    return rows


def measure_roundtrip(session: Session, body_id: str, diag: float) -> list:
    shape = session.document.get_body(body_id).shape
    total = brep_topology.face_count(shape)
    t0 = now()
    text = kernel_worker._encode(shape)
    encoded = now() - t0
    t0 = now()
    back = kernel_worker._decode(text)
    decoded = now() - t0
    t0 = now()
    copy = kernel.copy_shape(shape)
    copied = now() - t0
    rows = [{"bintools_bytes": len(text), "encode_ms": round(encoded * 1000, 1), "decode_ms": round(decoded * 1000, 1),
             "copy_shape_ms": round(copied * 1000, 1)}]
    print(f"  roundtrip: BinTools+base64 {len(text) / 1e6:.2f} MB、書き出し {rows[0]['encode_ms']} ms・読み込み {rows[0]['decode_ms']} ms"
          f"(`copy_shape` は {rows[0]['copy_shape_ms']} ms)", flush=True)
    faces = list(range(1, min(total, 60) + 1))
    for recipe in recipes(diag):
        here = kernel.tessellate_faces(copy, {recipe: faces})
        there = kernel.tessellate_faces(back, {recipe: faces})
        same = all(here["faces"][i]["positions"] == there["faces"][i]["positions"]
                   and here["faces"][i]["indices"] == there["faces"][i]["indices"] for i in here["faces"])
        triangles = sum(len(face["indices"]) // 3 for face in here["faces"].values())
        rows.append({"recipe": recipe, "faces": len(faces), "triangles": triangles, "identical": same})
        print(f"  roundtrip: recipe={tuple(round(v, 4) for v in recipe)} 面 {len(faces)} 三角形 {triangles}: 同じ={same}", flush=True)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=sorted(base.PARTS), default="small")
    ap.add_argument("--only", default="")
    ap.add_argument("--step", type=Path, default=None, help="この STEP の最大のボディで測る(--part より優先)")
    ap.add_argument("--out", type=Path, default=REPO / "output" / "bench_request_paths" / "loop_blockers.json")
    args = ap.parse_args()
    session, body_id, diag = part_session(args.part, args.step)
    label = str(args.step) if args.step is not None else args.part
    print(f"part={label} 面 {brep_topology.face_count(session.document.get_body(body_id).shape)} python={sys.version.split()[0]}", flush=True)
    result: dict = {"part": label, "python": sys.version}
    for name, measure in (("gil", measure_gil), ("deflate", measure_deflate), ("roundtrip", measure_roundtrip)):
        if args.only and args.only != name:
            continue
        result[name] = measure(session, body_id, diag)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

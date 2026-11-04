"""保存された文書を再生し、ボディごとの形の指紋(面・辺・頂点の数、体積、添字順の面と辺の性質)を JSON に書く。

    .venv/Scripts/python.exe -X utf8 tools/replay_fingerprint.py --out <json> <文書のフォルダ or .json> ...
    .venv/Scripts/python.exe -X utf8 tools/replay_fingerprint.py --compare <前.json> <後.json>

形状演算の書き方を変える前後で同じ文書の群を再生し、指紋が一致することを確かめる(操作ログは面・辺の添字で対象を指すので、
添字の並びが変わると後の操作が別の面に掛かる)。文書は一時フォルダへ写してから開く(元のファイルには書かない)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from duocad import kernel, query  # noqa: E402
from duocad.session import Session  # noqa: E402

from duocad import brep_geometry as _brep_geometry  # noqa: E402
from duocad import brep_topology as _brep_topology  # noqa: E402


def _rounded(values, digits: int) -> list:
    return [round(float(v), digits) for v in values]


def body_fingerprint(body) -> dict:
    if body.mesh is not None:
        return {"id": body.id, "mesh": True}
    shape = body.shape
    face_map = _brep_topology._face_map(shape)
    faces = []
    for index in range(1, face_map.Extent() + 1):
        face = face_map.FindKey(index)
        area, centroid = _brep_geometry._face_props(face)
        faces.append([_brep_geometry.classify_face(face), round(area, 5), *_rounded(centroid, 4)])
    edges = [[e["kind"], round(e["length"], 5)] for e in query.describe_edges(shape)]
    digest = hashlib.sha1(json.dumps([faces, edges]).encode("utf-8")).hexdigest()
    return {
        "id": body.id,
        "faces": face_map.Extent(),
        "edges": _brep_topology.edge_count(shape),
        "vertices": len(kernel.vertex_positions(shape)),
        "volume": round(_brep_geometry.volume(shape), 4),
        "digest": digest,
        "detail": {"faces": faces, "edges": edges},
    }


def fingerprint(path: Path, workdir: Path) -> dict:
    shutil.copy(path, workdir / path.name)
    session = Session(kernel.DEFAULT_DEVIATION, models_dir=workdir, opened=False)
    t0 = time.perf_counter()
    try:
        session.open_document(path.name)
    except Exception as error:  # noqa: BLE001  開けない文書も「開けない」という結果として比べる
        return {"error": f"{type(error).__name__}: {str(error)[:200]}"}
    elapsed = (time.perf_counter() - t0) * 1000.0
    return {
        "open_ms": round(elapsed, 1),
        "warnings": len(session.take_warnings()),
        "bodies": [body_fingerprint(body) for body in session.document.bodies if body.shape is not None or body.mesh is not None],
    }


def collect(paths: list) -> list:
    out = []
    for raw in paths:
        path = Path(raw)
        out.extend(sorted(path.glob("*.json")) if path.is_dir() else [path])
    return out


def compare(before_path: Path, after_path: Path) -> int:
    before = json.loads(before_path.read_text(encoding="utf-8"))["documents"]
    after = json.loads(after_path.read_text(encoding="utf-8"))["documents"]
    differ = 0
    slower = []
    for name, old in before.items():
        new = after.get(name)
        if new is None:
            print(f"MISSING {name}")
            differ += 1
            continue
        strip = lambda doc: {**doc, "open_ms": None, "bodies": [{k: v for k, v in b.items() if k != "detail"} for b in doc.get("bodies", [])]}
        if strip(old) != strip(new):
            differ += 1
            print(f"DIFFER  {name}")
            for a, b in zip(old.get("bodies", []), new.get("bodies", [])):
                if a.get("digest") != b.get("digest"):
                    fa, fb = a["detail"]["faces"], b["detail"]["faces"]
                    first = next((i for i, (x, y) in enumerate(zip(fa, fb)) if x != y), None)
                    print(f"    body {a['id']}: faces {a.get('faces')}->{b.get('faces')} volume {a.get('volume')}->{b.get('volume')} first differing face index {None if first is None else first + 1}")
            if old.get("error") != new.get("error"):
                print(f"    error: {old.get('error')} -> {new.get('error')}")
        if old.get("open_ms") and new.get("open_ms"):
            slower.append((new["open_ms"] - old["open_ms"], name, old["open_ms"], new["open_ms"]))
    total_before = sum(d.get("open_ms") or 0 for d in before.values())
    total_after = sum(d.get("open_ms") or 0 for d in after.values())
    print(f"{len(before)} documents, {differ} differ. open total {total_before:.0f} ms -> {total_after:.0f} ms")
    return 1 if differ else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--compare", nargs=2, type=Path)
    args = ap.parse_args()
    if args.compare:
        sys.exit(compare(*args.compare))
    documents = {}
    workdir = Path(tempfile.mkdtemp(prefix="duocad-replay-"))
    try:
        for path in collect(args.paths):
            documents[str(path)] = fingerprint(path, workdir)
            state = documents[str(path)].get("error") or f"{len(documents[str(path)]['bodies'])} bodies {documents[str(path)]['open_ms']} ms"
            print(f"{path.name}: {state}", flush=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"documents": documents}, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

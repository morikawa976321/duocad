"""操作ログの合成の文書群を種から作り、1操作ずつ再生した各操作の直後の指紋を取って、前後のコードで比べる。

    .venv/Scripts/python.exe -X utf8 tools/synthetic_corpus.py generate --out <フォルダ> [--seed 1] [--count 300]
    .venv/Scripts/python.exe -X utf8 tools/synthetic_corpus.py steps --out <json> [--triangles] [--detail] <フォルダ>/documents ...
    .venv/Scripts/python.exe -X utf8 tools/synthetic_corpus.py compare <前.json> <後.json>

`generate` は全 verb を使う文書を書く(`Session.apply` の経路で作るので、画面・MCP が積むのと同じ形のログになる)。
どの文書にも、前の操作が作った面・辺を後の操作が添字で指す連鎖が入る。同じ種からは同じバイト列の文書ができる。
文書は `<フォルダ>/documents/` に、内訳(土台・verb の数・連鎖の数・断られた操作)は `<フォルダ>/manifest.json` に書く。
文書は `tools/replay_fingerprint.py --out <json> <フォルダ>/documents` でもそのまま読める(最後の状態の比較)。

`steps` は文書を1操作ずつ再生し(`OpLog.replay` と同じ順で verb を掛ける)、各操作の直後に、全ボディの面・辺・頂点の
添字順の性質(辺は端点と中点の位置まで見るので、同じ長さの辺の入れ替わりも拾う)、スケッチの領域、形状最適化の条件を指紋にする。
`--triangles` を付けると、各操作の直後に画面へ配る三角形(`DocumentView.delta`)も指紋にする。`compare` は形・添字の違いと、
三角形だけの違いを分けて出す(終了コード 1 = 形・添字が違う、2 = 三角形だけが違う、0 = 一致)。

再生する duocad はこのファイルのあるチェックアウトのもの。環境変数 `DUOCAD_REPO` に別のチェックアウトを渡すとそちらを読む
(この道具の無い基準のコミットを、同じ道具で測る)。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional

REPO = Path(os.environ.get("DUOCAD_REPO") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, str(REPO))

from OCP.BRep import BRep_Tool  # noqa: E402
from OCP.BRepAdaptor import BRepAdaptor_Curve  # noqa: E402
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut  # noqa: E402
from OCP.BRepGProp import BRepGProp  # noqa: E402
from OCP.BRepPrimAPI import (  # noqa: E402
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeSphere,
    BRepPrimAPI_MakeTorus,
)
from OCP.GProp import GProp_GProps  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt  # noqa: E402

from duocad import document as _document_module  # noqa: E402
from duocad import kernel, kernel_worker, parameters, query, region  # noqa: E402
from duocad import sketch as _sketch_module  # noqa: E402
from duocad.document import Document  # noqa: E402
from duocad.oplog import Operation, OpLog, verb_handler  # noqa: E402
from duocad.session import Session  # noqa: E402
from duocad.verbs.add_solid import add_solid_body  # noqa: E402

from duocad import brep_geometry as _brep_geometry  # noqa: E402
from duocad import brep_topology as _brep_topology  # noqa: E402

STEPS_FORMAT = "duocad-corpus-steps/1"

#: 生成で使い分ける土台。文書の番号で順に回すので、数十本あれば全部の土台が入る。
BASES = ("plate", "holey_plate", "regions", "shaft", "revolved", "lofted", "primitive", "imported", "svg", "symmetric", "pins")


# ======================================================================================
# 指紋
# ======================================================================================
def _r(value: float, digits: int) -> float:
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def _digest(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def _point(p) -> list:
    return [_r(p.X(), 4), _r(p.Y(), 4), _r(p.Z(), 4)]


def face_rows(shape) -> list:
    """添字順の面: [種別, 面積, 重心 xyz, 外向き法線 xyz]。アプリのキャッシュ(`kernel._face_traits`)を通さずに測る。"""
    face_map = _brep_topology._face_map(shape)
    rows = []
    for index in range(1, face_map.Extent() + 1):
        face = face_map.FindKey(index)
        area, centre = _brep_geometry._face_props(face)
        try:
            normal = [_r(v, 3) for v in _brep_geometry._face_normal(face)]
        except Exception:  # noqa: BLE001  縮退した面は法線が無いことを指紋にする
            normal = None
        rows.append([_brep_geometry.classify_face(face), _r(area, 4), *[_r(v, 4) for v in centre], normal])
    return rows


def edge_rows(shape) -> list:
    """添字順の辺: [種別, 長さ, 始点, 中点, 終点]。縮退辺は ["degenerated", 頂点]。"""
    edge_map = _brep_topology._edge_map(shape)
    rows = []
    for index in range(1, edge_map.Extent() + 1):
        edge = TopoDS.Edge_s(edge_map.FindKey(index))
        if BRep_Tool.Degenerated_s(edge):
            rows.append(["degenerated"])
            continue
        curve = BRepAdaptor_Curve(edge)
        first, last = curve.FirstParameter(), curve.LastParameter()
        rows.append([
            _brep_geometry.classify_edge(edge),
            _r(_brep_geometry.edge_length(edge), 4),
            _point(curve.Value(first)),
            _point(curve.Value((first + last) / 2.0)),
            _point(curve.Value(last)),
        ])
    return rows


def vertex_rows(shape) -> list:
    return [[_r(v, 4) for v in item["point"]] for item in kernel.vertex_positions(shape)]


def shape_volume(shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return _r(props.Mass(), 3)


def body_print(body, detail: bool) -> dict:
    """1ボディの指紋。`detail` なら面・辺・頂点の行そのものも持つ(比べたときに最初に違う添字を出せる)。"""
    out: dict = {"name": body.name, "kind": body.kind}
    if body.mesh is not None:
        out["mesh"] = _digest([[_r(v, 4) for v in body.mesh.positions], list(body.mesh.indices)])
        return out
    if body.shape is None:
        return out
    faces, edges, vertices = face_rows(body.shape), edge_rows(body.shape), vertex_rows(body.shape)
    out.update({
        "counts": [len(faces), len(edges), len(vertices)],
        "volume": shape_volume(body.shape),
        "faces": _digest(faces),
        "edges": _digest(edges),
        "vertices": _digest(vertices),
    })
    if detail:
        out["detail"] = {"faces": faces, "edges": edges, "vertices": vertices}
    return out


def sketch_print(document: Document, item) -> list:
    """[領域の数, 添字順の領域(面積・重心)の要約]。領域を計算できないスケッチは ["error", 例外名]。"""
    try:
        rows = [[_r(r.area, 4), *[_r(v, 4) for v in r.centroid]] for r in region.regions(document, item.id)]
    except Exception as error:  # noqa: BLE001
        return ["error", type(error).__name__]
    return [len(rows), _digest([item.name, rows])]


def optimize_print(document: Document) -> str:
    setup = document.optimize
    rows = [setup.preserve_bodies, setup.exclude_bodies, setup.fixtures, setup.loads]
    return _digest(json.loads(json.dumps(rows, default=str)))


class _BodyPrints:
    """形の同一性で指紋を覚える(スケッチの操作の後で、変わっていないボディを測り直さない)。"""

    def __init__(self, detail: bool) -> None:
        self.detail = detail
        self._held: dict = {}

    def of(self, body) -> dict:
        source = body.mesh if body.mesh is not None else body.shape
        key = (body.id, id(source), body.name)
        held = self._held.get(key)
        if held is None or held[0] is not source:
            held = (source, body_print(body, self.detail))
            self._held = {k: v for k, v in self._held.items() if k[0] != body.id}
            self._held[key] = held
        return held[1]


_STATE_KEYS = ("bodies", "order", "active", "sketches", "parameters", "optimize")


def state_digest(step: dict) -> str:
    return _digest({key: step[key] for key in _STATE_KEYS})


def state_print(document: Document, prints: _BodyPrints) -> dict:
    return {
        "bodies": {body.id: prints.of(body) for body in document.bodies},
        "order": [body.id for body in document.bodies],
        "active": document._active_body_id,
        "sketches": {item.id: sketch_print(document, item) for item in document.sketches},
        "parameters": _digest(sorted((k, _r(v, 6)) for k, v in document.parameters.items())),
        "optimize": optimize_print(document),
    }


def triangle_print(session: Session, known: dict) -> dict:
    """前回から版が変わったボディの、配る三角形の指紋 {body: [三角形の数, 面ごとの三角形の要約]}。"""
    delta = session.view.delta(known)
    out = {}
    for body in delta["bodies"]:
        known[body["body_id"]] = body["revision"]
        geometry = body.get("geometry")
        if not geometry:
            out[body["body_id"]] = None
            continue
        faces = geometry.get("faces")
        if faces is None:
            out[body["body_id"]] = [len(geometry.get("indices", [])) // 3, _digest([[_r(v, 4) for v in geometry.get("positions", [])], list(geometry.get("indices", []))])]
            continue
        rows = [[f["index"], [_r(v, 4) for v in f["positions"]], list(f["indices"])] for f in faces]
        out[body["body_id"]] = [sum(len(f["indices"]) // 3 for f in faces), _digest(rows)]
    return out


def replay_steps(path: Path, workdir: Path, *, triangles: bool, detail: bool) -> dict:
    """文書を1操作ずつ再生し、各操作の直後の指紋の並びを返す。開けない・途中で落ちた文書はそこまでの並びと `error`。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    log = OpLog.from_payload(payload)
    session = Session(kernel.DEFAULT_DEVIATION, models_dir=workdir) if triangles else None
    document = session.document if session is not None else Document()
    # 束ねてから再生する(`Session.open_document` と同じ。`set_parameter` は文書のログを頭から読む)。
    document.log = log
    prints = _BodyPrints(detail)
    known: dict = {}
    steps: list = []
    out: dict = {"operations": len(log.entries), "steps": steps}
    t0 = time.perf_counter()
    for operation in log.entries:
        started = time.perf_counter()
        try:
            verb_handler(operation.verb).do(document, operation)
        except Exception as error:  # noqa: BLE001  落ちた操作も「そこで落ちた」という結果として比べる
            out["error"] = f"seq {operation.seq} {operation.verb}: {type(error).__name__}: {str(error)[:200]}"
            break
        elapsed = round((time.perf_counter() - started) * 1000.0, 2)
        step = {"seq": operation.seq, "verb": operation.verb, "ms": elapsed, **state_print(document, prints)}
        if session is not None:
            try:
                step["triangles"] = triangle_print(session, known)
            except Exception as error:  # noqa: BLE001
                step["triangles"] = {"error": f"{type(error).__name__}: {str(error)[:120]}"}
        steps.append(step)
    out["replay_ms"] = round(sum(step["ms"] for step in steps), 1)
    out["fingerprint_ms"] = round((time.perf_counter() - t0) * 1000.0 - out["replay_ms"], 1)
    return out


def peak_memory_mb() -> Optional[dict]:
    """このプロセスと子プロセス(丸め・面の編集の常駐の子)の最大常駐量[MB]。測れない OS(Windows)では None。"""
    try:
        import resource
    except ImportError:
        return None
    scale = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return {
        "self": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale, 1),
        "children": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / scale, 1),
    }


def collect(paths: list) -> list:
    out = []
    for raw in paths:
        path = Path(raw)
        out.extend(sorted(p for p in path.glob("*.json") if p.name != "manifest.json") if path.is_dir() else [path])
    return out


def _generated_states(path: Path) -> dict:
    """`generate` の内訳にある、文書を組んだときの最後の状態(無ければ空)。"""
    manifest = path.parent.parent / "manifest.json"
    if not manifest.exists():
        return {}
    documents = json.loads(manifest.read_text(encoding="utf-8")).get("documents", {})
    return {name: d["final_state"] for name, d in documents.items() if "final_state" in d}


def steps_main(args) -> None:
    documents = {}
    workdir = Path(tempfile.mkdtemp(prefix="duocad-corpus-steps-"))
    t0 = time.perf_counter()
    generated: dict = {}
    live_differ: list = []
    try:
        for path in collect(args.paths):
            if path.parent not in generated:
                generated[path.parent] = _generated_states(path)
            result = replay_steps(path, workdir, triangles=args.triangles, detail=args.detail)
            expected = generated[path.parent].get(path.name)
            if expected is not None and result["steps"] and not result.get("error"):
                result["matches_generated"] = state_digest(result["steps"][-1]) == expected
                if not result["matches_generated"]:
                    live_differ.append(path.name)
            documents[path.name] = result
            state = result.get("error") or f"{len(result['steps'])} steps"
            mark = {True: "", False: " (differs from the generated state)", None: ""}[result.get("matches_generated")]
            print(f"{path.name}: {state} {result['replay_ms']:.0f} ms{mark}", flush=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    total = time.perf_counter() - t0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "format": STEPS_FORMAT,
        "triangles": bool(args.triangles),
        "detail": bool(args.detail),
        "seconds": round(total, 1),
        "peak_memory_mb": peak_memory_mb(),
        "documents": documents,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    failed = sum(1 for d in documents.values() if d.get("error"))
    checked = sum(1 for d in documents.values() if "matches_generated" in d)
    print(f"{len(documents)} documents ({failed} stopped with an error) in {total:.1f} s. wrote {args.out}")
    if checked:
        print(f"replayed final state vs the state built by `generate`: {checked - len(live_differ)} / {checked} match" + (f"; differ: {', '.join(live_differ)}" if live_differ else ""))


# ======================================================================================
# 比べる
# ======================================================================================
_SHAPE_KEYS = ("name", "kind", "counts", "volume", "faces", "edges", "vertices", "mesh")


def _first_row_difference(old: dict, new: dict) -> Optional[str]:
    if "detail" not in old or "detail" not in new:
        return None
    for part in ("faces", "edges", "vertices"):
        a, b = old["detail"][part], new["detail"][part]
        for index, (x, y) in enumerate(zip(a, b), start=1):
            if x != y:
                return f"{part[:-1]} {index}: {x} -> {y}"
        if len(a) != len(b):
            return f"{part}: {len(a)} -> {len(b)}"
    return None


def step_difference(old: dict, new: dict) -> tuple:
    """(形・添字の違いの説明の並び, 三角形の違いの説明の並び)。"""
    shape: list = []
    for key in ("order", "active", "parameters", "optimize"):
        if old.get(key) != new.get(key):
            shape.append(f"{key} {old.get(key)} -> {new.get(key)}")
    for body_id in sorted(set(old["bodies"]) | set(new["bodies"])):
        a, b = old["bodies"].get(body_id), new["bodies"].get(body_id)
        if a is None or b is None:
            shape.append(f"body {body_id} {'appeared' if a is None else 'missing'}")
            continue
        parts = [key for key in _SHAPE_KEYS if a.get(key) != b.get(key)]
        if parts:
            row = _first_row_difference(a, b)
            shape.append(f"body {body_id} {','.join(parts)} counts {a.get('counts')}->{b.get('counts')} volume {a.get('volume')}->{b.get('volume')}" + (f" first {row}" if row else ""))
    for sketch_id in sorted(set(old["sketches"]) | set(new["sketches"])):
        if old["sketches"].get(sketch_id) != new["sketches"].get(sketch_id):
            shape.append(f"sketch {sketch_id} regions {old['sketches'].get(sketch_id)} -> {new['sketches'].get(sketch_id)}")
    mesh: list = []
    if "triangles" in old and "triangles" in new and old["triangles"] != new["triangles"]:
        for body_id in sorted(set(old["triangles"]) | set(new["triangles"])):
            a, b = old["triangles"].get(body_id), new["triangles"].get(body_id)
            if a != b:
                mesh.append(f"triangles {body_id} {a} -> {b}")
    return shape, mesh


def verb_times(data: dict) -> Counter:
    times: Counter = Counter()
    for document in data["documents"].values():
        for step in document["steps"]:
            times[step["verb"]] += step.get("ms", 0.0)
    return times


def print_verb_times(before: dict, after: dict, top: int = 12) -> None:
    """再生の時間を verb ごとに(同じ機械で測った前後どうしでだけ意味がある)。"""
    old, new = verb_times(before), verb_times(after)
    print("replay ms by verb (before -> after):")
    for verb, value in old.most_common(top):
        print(f"  {verb:<24} {value:9.0f} -> {new.get(verb, 0.0):9.0f}")


def compare(before_path: Path, after_path: Path, limit: int = 3) -> int:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    for data, path in ((before, before_path), (after, after_path)):
        if data.get("format") != STEPS_FORMAT:
            raise SystemExit(f"{path}: not a `steps` output ({data.get('format')!r})")
    shape_differ, mesh_differ, missing = [], [], []
    for name, old in before["documents"].items():
        new = after["documents"].get(name)
        if new is None:
            missing.append(name)
            continue
        found_shape: list = []
        found_mesh: list = []
        for a, b in zip(old["steps"], new["steps"]):
            shape, mesh = step_difference(a, b)
            if shape and not found_shape:
                found_shape = [f"seq {a['seq']} {a['verb']}"] + shape[:limit]
            if mesh and not found_mesh:
                found_mesh = [f"seq {a['seq']} {a['verb']}"] + mesh[:limit]
        if len(old["steps"]) != len(new["steps"]) or old.get("error") != new.get("error"):
            found_shape = found_shape or [f"steps {len(old['steps'])} -> {len(new['steps'])}"]
            found_shape.append(f"error {old.get('error')} -> {new.get('error')}")
        if found_shape:
            shape_differ.append(name)
            print(f"DIFFER     {name}: " + " | ".join(found_shape))
        elif found_mesh:
            mesh_differ.append(name)
            print(f"TRIANGLES  {name}: " + " | ".join(found_mesh))
    for name in missing:
        print(f"MISSING    {name}")
    steps_total = sum(len(d["steps"]) for d in before["documents"].values())
    print_verb_times(before, after)
    print(
        f"{len(before['documents'])} documents ({steps_total} steps): {len(shape_differ) + len(missing)} differ in shape/indices, "
        f"{len(mesh_differ)} differ only in triangles. whole run {before.get('seconds')} s -> {after.get('seconds')} s"
    )
    if shape_differ or missing:
        return 1
    return 2 if mesh_differ else 0


# ======================================================================================
# 生成
# ======================================================================================
#: 生成で1操作に掛けてよい時間。子プロセスで計算する操作はこれで打ち切る(シェルは期限なしなので、打ち切らないと
#: 数分かかる形が文書に入り、親の再生も同じだけ待つ)。
OPERATION_BUDGET_S = 20.0
#: 要約の `slow` に残す操作の下限。
SLOW_OPERATION_MS = 3000.0


class CorruptDocument(RuntimeError):
    """断った verb がログを残した(生成した文書を信用できない)。"""


@contextlib.contextmanager
def seeded_ids(seed: str) -> Iterator[None]:
    """ボディ・スケッチ・図形の ID を種から振る。"""
    rng = random.Random(seed)

    def fake_uuid4() -> uuid.UUID:
        return uuid.UUID(int=rng.getrandbits(128), version=4)

    # 意図的な変則: ID は `uuid4` 由来でログに載るので、同じ種から同じバイト列の文書を作るために、生成の間だけ差し替える。
    saved = (_document_module.uuid4, _sketch_module.uuid4)
    _document_module.uuid4 = _sketch_module.uuid4 = fake_uuid4
    try:
        yield
    finally:
        _document_module.uuid4, _sketch_module.uuid4 = saved


class Builder:
    """1つの文書を `Session.apply` の経路で組む。対象は形をその場で測って添字で選ぶ(乱数は `rng` だけ)。"""

    def __init__(self, rng: random.Random, workdir: Path) -> None:
        self.rng = rng
        self.workdir = workdir
        self.session = Session(kernel.DEFAULT_DEVIATION, models_dir=workdir / "models")
        self.refused: Counter = Counter()
        self.verb_ms: Counter = Counter()
        self.refusal_examples: dict = {}
        self.slow: list = []
        #: 予算を超えた verb(この文書では二度と試さない)。
        self.over_budget: set = set()
        self._faces: dict = {}

    @property
    def doc(self) -> Document:
        return self.session.document

    # ------------------------------------------------------------------ 操作
    def apply(self, verb: str, args: Optional[dict] = None, refs: Optional[list] = None) -> bool:
        if verb in self.over_budget:
            self.refused[f"{verb}:over_budget"] += 1
            return False
        before = len(self.doc.log.entries)
        self.session.clear_selection()
        t0 = time.perf_counter()
        cancel = threading.Event()
        timer = threading.Timer(OPERATION_BUDGET_S, cancel.set)
        timer.daemon = True
        timer.start()
        ok = False
        try:
            if refs:
                self.session.modify_selection(add=refs)
            # 子プロセスの計算(丸め・面の編集・シェル)を予算で打ち切れるよう、取り消しの口を持つプレビューの印の下で確定する。
            # 印を読むのは `kernel_worker.run` だけで、計算そのものは確定と同じ。
            with kernel_worker.preview_context(cancel):
                self.session.apply(verb, dict(args or {}))
            ok = True
        except Exception as error:  # noqa: BLE001  断られた操作はログに残らない(残ったら下で止める)
            if len(self.doc.log.entries) != before:
                raise CorruptDocument(f"{verb} was refused but left the log at {len(self.doc.log.entries)} entries")
            self.note_refusal(verb, error)
            self.session.clear_selection()
            return False
        finally:
            timer.cancel()
            if cancel.is_set():
                self.over_budget.add(verb)
            elapsed = (time.perf_counter() - t0) * 1000.0
            self.verb_ms[verb] += elapsed
            if elapsed >= SLOW_OPERATION_MS:
                self.slow.append([verb, round(elapsed), ok])
        self.session.clear_selection()
        return True

    def note_refusal(self, verb: str, error: Exception) -> None:
        key = f"{verb}:{type(error).__name__}"
        self.refused[key] += 1
        self.refusal_examples.setdefault(key, str(error)[:160])

    def replay_only(self, verb: str, args: dict) -> bool:
        """公開面に無い再生専用の verb(`extrude_face`)を、確定と同じ順(掛けてからログへ)で積む。"""
        try:
            verb_handler(verb).do(self.doc, Operation(seq=len(self.doc.log.entries) + 1, verb=verb, args=dict(args)))
        except Exception as error:  # noqa: BLE001
            self.note_refusal(verb, error)
            return False
        self.doc.log.append(verb, args)
        return True

    def set_parameter(self, name: str, value: float) -> bool:
        before = len(self.doc.log.entries)
        try:
            parameters.set_parameter(self.doc, name, value)
        except Exception as error:  # noqa: BLE001
            if len(self.doc.log.entries) != before:
                raise CorruptDocument("set_parameter was refused but left the log")
            self.note_refusal("set_parameter", error)
            return False
        return True

    def pick(self, items: list, k: int = 1) -> list:
        items = list(items)
        if not items:
            return []
        return self.rng.sample(items, min(k, len(items)))

    def one(self, items: list) -> Any:
        items = list(items)
        return self.rng.choice(items) if items else None

    # ------------------------------------------------------------------ スケッチ
    def sketch(self, base_plane: Optional[str] = None, face: Optional[tuple] = None, offset: Optional[float] = None) -> Optional[str]:
        refs = [face_ref(*face)] if face is not None else None
        args: dict = {} if base_plane is None else {"base_plane": base_plane}
        verb = "sketch_create"
        if offset is not None:
            verb, args = "sketch_create_offset", {**args, "distance": float(offset)}
        if not self.apply(verb, args, refs):
            return None
        self.session.leave_sketch()
        return self.doc.sketches[-1].id

    def entity(self, verb: str, sketch_id: str, **args) -> Optional[str]:
        if not self.apply(verb, {"sketch_id": sketch_id, **args}):
            return None
        return _sketch_module.get_sketch(self.doc, sketch_id).entities[-1].id

    def regions(self, sketch_id: str) -> list:
        try:
            return region.regions(self.doc, sketch_id)
        except Exception:  # noqa: BLE001
            return []

    def frame(self, sketch_id: str) -> dict:
        return region.plane_frame(self.doc, sketch_id)

    def local(self, sketch_id: str, point) -> tuple:
        frame = self.frame(sketch_id)
        d = [point[i] - frame["origin"][i] for i in range(3)]
        return (sum(d[i] * frame["u"][i] for i in range(3)), sum(d[i] * frame["v"][i] for i in range(3)))

    # ------------------------------------------------------------------ 形を測る(画面の三角形分割を通さない)
    def solids(self) -> list:
        return [b for b in self.doc.bodies if b.shape is not None and b.mesh is None]

    def body(self, body_id: str):
        return self.doc.get_body(body_id)

    def faces(self, body_id: str) -> list:
        shape = self.body(body_id).shape
        held = self._faces.get(body_id)
        if held is not None and held[0] is shape:
            return held[1]
        face_map = _brep_topology._face_map(shape)
        rows = []
        for index in range(1, face_map.Extent() + 1):
            face = face_map.FindKey(index)
            area, centre = _brep_geometry._face_props(face)
            try:
                normal = _brep_geometry._face_normal(face)
            except Exception:  # noqa: BLE001
                normal = (0.0, 0.0, 0.0)
            rows.append({"index": index, "kind": _brep_geometry.classify_face(face), "area": area, "centroid": centre, "normal": normal})
        self._faces[body_id] = (shape, rows)
        return rows

    def edges(self, body_id: str) -> list:
        return query.describe_edges(self.body(body_id).shape)

    def vertex_count(self, body_id: str) -> int:
        return len(kernel.vertex_positions(self.body(body_id).shape))

    def bbox(self, body_id: str) -> tuple:
        box = kernel.bounding_box(self.body(body_id).shape)
        return box["min"], box["max"]

    def planar_faces(self, body_id: str, min_area: float = 4.0) -> list:
        return [f for f in self.faces(body_id) if f["kind"] == "plane" and f["area"] >= min_area]

    def cylinders(self, body_id: str) -> list:
        return [f for f in self.faces(body_id) if f["kind"] == "cylinder_full"]


def face_ref(body_id: str, index: int) -> dict:
    return {"body": body_id, "kind": "face", "index": int(index)}


def edge_ref(body_id: str, index: int) -> dict:
    return {"body": body_id, "kind": "edge", "index": int(index)}


def vertex_ref(body_id: str, index: int) -> dict:
    return {"body": body_id, "kind": "vertex", "index": int(index)}


def body_ref(body_id: str) -> dict:
    return {"body": body_id, "kind": "body", "index": None}


def region_refs(sketch_id: str, indices: list) -> list:
    return [{"sketch": sketch_id, "index": int(i)} for i in indices]


def entity_ref(sketch_id: str, entity_id: str) -> dict:
    return {"sketch": sketch_id, "entity": entity_id, "point": None}


def _size(b: Builder, lo: float, hi: float, step: float = 5.0) -> float:
    return float(b.rng.randrange(int(lo / step), int(hi / step) + 1) * step)


def _extrude_new(b: Builder, sketch_id: str, indices: list, distance: float, **extra) -> Optional[str]:
    if not indices or not b.apply("extrude", {"distance": distance, "mode": "new", **extra}, region_refs(sketch_id, indices)):
        return None
    return b.doc.bodies[-1].id


# ------------------------------------------------------------------ 土台(最初のボディを作る)
def base_plate(b: Builder, holes: bool = False) -> None:
    """板。正方形のときは同じ長さの辺が4組できる。穴は円1つを矩形反復で並べる。"""
    width = _size(b, 20, 100 if holes else 60)
    height = width if b.rng.random() < 0.4 else _size(b, 20, 80 if holes else 60)
    centered = b.rng.random() < 0.5
    sid = b.sketch("XY")
    corner = [-width / 2, -height / 2] if centered else [0.0, 0.0]
    b.apply("add_rectangle", {"sketch_id": sid, "corner": corner, "width": width, "height": height})
    if holes:
        nx, ny = (b.rng.randint(8, 12), b.rng.randint(6, 8)) if b.rng.random() < 0.2 else (b.rng.randint(2, 8), b.rng.randint(2, 6))
        pitch_x, pitch_y = width / (nx + 1), height / (ny + 1)
        diameter = round(min(pitch_x, pitch_y) * b.rng.choice([0.3, 0.45, 0.6, 0.7]), 2)
        cid = b.entity("add_circle", sid, center=[corner[0] + pitch_x, corner[1] + pitch_y], diameter=diameter)
        if cid is not None:
            b.apply("pattern_rectangular", {"sketch_id": sid, "entities": [cid], "dx": pitch_x, "dy": pitch_y, "nx": nx, "ny": ny})
    regions = b.regions(sid)
    if not regions:
        return
    outer = max(regions, key=lambda r: r.area)
    _extrude_new(b, sid, [outer.index], float(b.rng.choice([4, 5, 6, 8, 10, 12])))


def base_regions(b: Builder) -> None:
    """重なる図形で領域を多数作り、そのうち何個かを添字で選んで1回で押し出す。図形の移動・鏡映・円形反復もここで使う。"""
    sid = b.sketch(b.one(["XY", "XZ", "YZ"]))
    size = _size(b, 30, 60)
    b.apply("add_rectangle", {"sketch_id": sid, "corner": [0, 0], "width": size, "height": size * 0.75})
    axis = b.entity("add_line", sid, p0=[size / 2, -5], p1=[size / 2, size])
    shapes = []
    for _ in range(b.rng.randint(2, 5)):
        kind = b.one(["circle", "rectangle", "ellipse"])
        x, y = b.rng.uniform(0, size * 0.5), b.rng.uniform(0, size * 0.75)
        x, y = round(x, 1), round(y, 1)
        if kind == "circle":
            shapes.append(b.entity("add_circle", sid, center=[x, y], diameter=round(b.rng.uniform(4, size / 3), 1)))
        elif kind == "rectangle":
            shapes.append(b.entity("add_rectangle", sid, corner=[x, y], width=round(b.rng.uniform(4, size / 3), 1), height=round(b.rng.uniform(4, size / 3), 1), angle=b.one([0.0, 15.0, 30.0])))
        else:
            shapes.append(b.entity("add_ellipse", sid, center=[x, y], width=round(b.rng.uniform(6, size / 3), 1), height=round(b.rng.uniform(3, size / 5), 1), angle=b.one([0.0, 45.0])))
    shapes = [s for s in shapes if s]
    if shapes and axis and b.rng.random() < 0.7:
        b.apply("mirror_sketch_entities", {"axis": entity_ref(sid, axis)}, [entity_ref(sid, s) for s in b.pick(shapes, 2)])
    if shapes and b.rng.random() < 0.5:
        b.apply("move_sketch_entities", {"dx": 2.0, "dy": 1.0, "copy": b.rng.random() < 0.5}, [entity_ref(sid, b.one(shapes))])
    if shapes and b.rng.random() < 0.4:
        center = [size / 4, size * 0.375]
        b.apply("pattern_circular", {"sketch_id": sid, "entities": [b.one(shapes)], "center": center, "count": b.rng.randint(3, 6), "total_angle": 360.0})
    if b.rng.random() < 0.5:
        b.apply("add_arc", {"sketch_id": sid, "center": [size * 0.75, size * 0.375], "radius": size / 8, "start_deg": 0.0, "end_deg": 180.0})
        b.apply("add_point", {"sketch_id": sid, "point": [size * 0.1, size * 0.1]})
    regions = b.regions(sid)
    if not regions:
        return
    count = b.rng.randint(max(1, len(regions) // 3), len(regions))
    chosen = sorted(r.index for r in b.pick(regions, count))
    taper = b.one([0.0, 0.0, 0.0, 3.0, -3.0])
    if _extrude_new(b, sid, chosen, float(b.rng.choice([5, 8, 10])), taper=taper) is None and taper:
        _extrude_new(b, sid, chosen, 8.0)


def base_shaft(b: Builder) -> None:
    """円柱(全周の円筒面)。上面に貼ったスケッチでボスか穴を足し、ねじの対象を作る。"""
    sid = b.sketch("XY")
    diameter = float(b.rng.choice([10, 12, 16, 20, 24]))
    b.apply("add_circle", {"sketch_id": sid, "center": [0, 0], "diameter": diameter})
    body_id = _extrude_new(b, sid, [1], float(b.rng.choice([10, 15, 20])))
    if body_id is None:
        return
    top = max(b.planar_faces(body_id), key=lambda f: f["centroid"][2])
    sid2 = b.sketch(face=(body_id, top["index"]))
    if sid2 is None:
        return
    u, v = b.local(sid2, top["centroid"])
    inner = round(diameter * b.rng.choice([0.3, 0.5]), 1)
    b.apply("add_circle", {"sketch_id": sid2, "center": [u, v], "diameter": inner})
    regions = sorted(b.regions(sid2), key=lambda r: r.area)
    if not regions:
        return
    if b.rng.random() < 0.5:
        b.apply("extrude", {"distance": -float(b.rng.choice([4, 6, 8])), "mode": "cut", "body_id": body_id}, region_refs(sid2, [regions[0].index]))
    else:
        b.apply("extrude", {"distance": float(b.rng.choice([4, 6])), "mode": "join", "body_id": body_id}, region_refs(sid2, [regions[0].index]))


def base_revolved(b: Builder) -> None:
    """回転体(トーラス・円錐・円筒の面)。軸は原点の軸か、スケッチの線分。"""
    sid = b.sketch("XZ")
    inner, width, height = float(b.rng.choice([4, 6, 8])), float(b.rng.choice([4, 6, 10])), float(b.rng.choice([5, 10, 15]))
    b.apply("add_rectangle", {"sketch_id": sid, "corner": [inner, 0], "width": width, "height": height})
    if b.rng.random() < 0.5:
        b.apply("add_circle", {"sketch_id": sid, "center": [inner + width, height / 2], "diameter": min(width, height) * 0.8})
    angle = b.one([360.0, 180.0, 90.0])
    regions = b.regions(sid)
    if not regions:
        return
    chosen = [r.index for r in b.pick(regions, b.rng.randint(1, len(regions)))]
    if b.rng.random() < 0.5:
        line = b.entity("add_line", sid, p0=[0, -5], p1=[0, height + 5])
        regions = b.regions(sid)
        chosen = [r.index for r in b.pick(regions, b.rng.randint(1, len(regions)))]
        if line and b.apply("revolve", {"axis_line": {"sketch": sid, "entity": line}, "angle": angle, "mode": "new"}, region_refs(sid, chosen)):
            return
    b.apply("revolve", {"axis": "y", "angle": angle, "mode": "new", "symmetric": b.rng.random() < 0.3}, region_refs(sid, chosen))


def base_lofted(b: Builder) -> None:
    """ロフト(四角から丸、尖らせる点つき)。"""
    sid = b.sketch("XY")
    size = float(b.rng.choice([10, 16, 20]))
    b.apply("add_rectangle", {"sketch_id": sid, "corner": [-size / 2, -size / 2], "width": size, "height": size})
    sid2 = b.sketch("XY", offset=float(b.rng.choice([10, 15, 20])))
    if sid2 is None:
        return
    b.apply("add_circle", {"sketch_id": sid2, "center": [0, 0], "diameter": size * b.rng.choice([0.6, 1.0])})
    args: dict = {"mode": "new"}
    if b.rng.random() < 0.3:
        sid3 = b.sketch("XY", offset=40.0)
        point = b.entity("add_point", sid3, point=[0, 0]) if sid3 else None
        if point:
            args["end_point"] = {"sketch": sid3, "entity": point, "point": "cx"}
    if not b.apply("loft", args, region_refs(sid, [1]) + region_refs(sid2, [1])):
        b.apply("loft", {"mode": "new"}, region_refs(sid, [1]) + region_refs(sid2, [1]))


def _primitive(b: Builder):
    kind = b.one(["box", "cylinder", "sphere", "torus", "cone"])
    origin = gp_Ax2(gp_Pnt(b.rng.choice([0, 5, -10]), 0, 0), gp_Dir(0, 0, 1))
    if kind == "box":
        return kind, BRepPrimAPI_MakeBox(origin, 20.0, 20.0, float(b.rng.choice([10, 20]))).Shape()
    if kind == "cylinder":
        return kind, BRepPrimAPI_MakeCylinder(origin, 8.0, 20.0).Shape()
    if kind == "sphere":
        return kind, BRepPrimAPI_MakeSphere(origin, 10.0).Shape()
    if kind == "torus":
        return kind, BRepPrimAPI_MakeTorus(origin, 12.0, 4.0).Shape()
    return kind, BRepPrimAPI_MakeCone(origin, 10.0, 4.0, 15.0).Shape()


def base_primitive(b: Builder) -> None:
    """B-rep 本文を持つソリッド(`add_solid_body`。画面・MCP には無く、生成物の保存に使う verb)と、三角形のボディ。"""
    kind, shape = _primitive(b)
    add_solid_body(b.doc, shape, f"prim-{kind}")
    if b.rng.random() < 0.5:
        offset = 60.0
        positions = [offset, 0, 0, offset + 10, 0, 0, offset, 10, 0, offset, 0, 10]
        b.apply("add_mesh_body", {"positions": positions, "indices": [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3]})


@contextlib.contextmanager
def _quiet_stdout() -> Iterator[None]:
    """外部の罠: OCCT の STEP 書き出しは C++ 側から標準出力へ進み具合を書く(Python の sys.stdout を替えても止まらない)。"""
    sys.stdout.flush()
    saved = os.dup(1)
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), 1)
        try:
            yield
        finally:
            os.dup2(saved, 1)
            os.close(saved)


def base_imported(b: Builder) -> None:
    """STEP の読み込み(本文がログに入る)。板に穴を開けた形を書き出して読み込む。"""
    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 30.0, 20.0, 8.0).Shape()
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(10, 10, -1), gp_Dir(0, 0, 1)), 3.0, 10.0).Shape()
    shape = BRepAlgoAPI_Cut(box, hole).Shape()
    if b.rng.random() < 0.5:
        _, other = _primitive(b)
        shape = kernel.make_compound([shape, kernel.translate_shape(other, 50.0, 0.0, 0.0, copy=True)])
    path = b.workdir / "part.step"
    with _quiet_stdout():
        kernel.write_step(shape, str(path))
    text = path.read_text(encoding="utf-8", errors="replace")
    # 外部の罠: STEP の見出し(FILE_NAME)は書き出した時刻を持つ。同じ種から同じ文書を作るため固定の時刻へ置き換える。
    text = re.sub(r"'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}'", "'2026-01-01T00:00:00'", text)
    path.write_text(text, encoding="utf-8")
    b.apply("import_step", {"path": str(path)})


def base_svg(b: Builder) -> None:
    """SVG の取り込み(図形がログに入る)。"""
    sid = b.sketch("XY")
    w, h = _size(b, 20, 40), _size(b, 20, 40)
    r = round(min(w, h) / 6, 1)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">'
        f'<rect x="0" y="0" width="{w}" height="{h}"/><circle cx="{w / 3}" cy="{h / 2}" r="{r}"/>'
        f'<circle cx="{2 * w / 3}" cy="{h / 2}" r="{r}"/></svg>'
    )
    path = b.workdir / "shape.svg"
    path.write_text(svg, encoding="utf-8")
    if not b.apply("import_svg", {"sketch_id": sid, "path": str(path)}):
        b.apply("add_rectangle", {"sketch_id": sid, "corner": [0, 0], "width": w, "height": h})
    regions = b.regions(sid)
    if regions:
        _extrude_new(b, sid, [max(regions, key=lambda r: r.area).index], 6.0)


def base_pins(b: Builder) -> None:
    """板の上面(添字で指す)に貼ったスケッチの多数の円を、1回の押し出しでまとめて足す(柱を順に結合する経路。最大 60 本)。"""
    base_plate(b)
    solids = b.solids()
    if not solids:
        return
    body_id = solids[-1].id
    top = max(b.planar_faces(body_id), key=lambda f: (f["centroid"][2], f["area"]))
    sid = b.sketch(face=(body_id, top["index"]))
    if sid is None:
        return
    low, high = b.bbox(body_id)
    nx, ny = b.one([(3, 2), (5, 4), (6, 5), (10, 6)])
    corner = b.local(sid, [low[0], low[1], high[2]])
    other = b.local(sid, [high[0], high[1], high[2]])
    pitch_x, pitch_y = (other[0] - corner[0]) / (nx + 1), (other[1] - corner[1]) / (ny + 1)
    diameter = round(min(abs(pitch_x), abs(pitch_y)) * 0.5, 2)
    first = b.entity("add_circle", sid, center=[round(corner[0] + pitch_x, 3), round(corner[1] + pitch_y, 3)], diameter=diameter)
    if first is None or not b.apply("pattern_rectangular", {"sketch_id": sid, "entities": [first], "dx": round(pitch_x, 3), "dy": round(pitch_y, 3), "nx": nx, "ny": ny}):
        return
    regions = b.regions(sid)
    b.apply("extrude", {"distance": float(b.one([2, 4])), "mode": "join", "body_id": body_id}, region_refs(sid, [r.index for r in regions]))


def base_symmetric(b: Builder) -> None:
    """対称な形: 正方形の板の中央に円を置き、鏡映で写しを作ってから結合する。"""
    base_plate(b)
    solids = b.solids()
    if not solids:
        return
    body_id = solids[-1].id
    plane = b.one(["XZ", "YZ"])
    b.apply("mirror_body", {"base_plane": plane}, [body_ref(body_id)])
    if len(b.solids()) >= 2:
        b.apply("boolean_body", {"target_ref": body_ref(body_id), "mode": "union"}, [body_ref(b.solids()[-1].id)])


BASE_FUNCTIONS: dict = {
    "plate": base_plate,
    "holey_plate": lambda b: base_plate(b, holes=True),
    "regions": base_regions,
    "shaft": base_shaft,
    "revolved": base_revolved,
    "lofted": base_lofted,
    "primitive": base_primitive,
    "imported": base_imported,
    "svg": base_svg,
    "symmetric": base_symmetric,
    "pins": base_pins,
}


# ------------------------------------------------------------------ 続きの操作(前の結果の面・辺・頂点を添字で指す)
def _target(b: Builder) -> Optional[str]:
    """多くは直前の操作が変えたボディ(同じボディに添字の操作を重ねて連鎖を作る)、ときどき別のボディ。"""
    solids = b.solids()
    if not solids:
        return None
    last = next((op.args.get("body_id") for op in reversed(b.doc.log.entries) if isinstance(op.args.get("body_id"), str)), None)
    if last in {s.id for s in solids} and b.rng.random() < 0.7:
        return last
    return b.one(solids).id


def move_blend(b: Builder, verb: str) -> bool:
    body_id = _target(b)
    if body_id is None:
        return False
    edges = [e for e in b.edges(body_id) if e["kind"] in ("line", "circle") and e["length"] > 3.0]
    for _ in range(3):
        chosen = b.pick(edges, b.one([1, 1, 2, 3]))
        if not chosen:
            return False
        value = float(b.one([0.5, 1.0, 1.5]))
        key = "radius" if verb == "fillet" else "distance"
        if b.apply(verb, {key: value}, [edge_ref(body_id, e["index"]) for e in chosen]):
            return True
    return False


def move_sketch_on_face(b: Builder) -> bool:
    """面に貼ったスケッチ(ログには面の添字が載る)で、ボスか穴を足す。"""
    body_id = _target(b)
    if body_id is None:
        return False
    for _ in range(3):
        face = b.one(b.planar_faces(body_id, 16.0))
        if face is None:
            return False
        sid = b.sketch(face=(body_id, face["index"]))
        if sid is None:
            continue
        u, v = b.local(sid, face["centroid"])
        size = round(max(1.0, min(6.0, math.sqrt(face["area"]) / 3)), 1)
        if b.rng.random() < 0.5:
            b.apply("add_circle", {"sketch_id": sid, "center": [round(u, 3), round(v, 3)], "diameter": size})
        else:
            b.apply("add_rectangle", {"sketch_id": sid, "corner": [round(u - size / 2, 3), round(v - size / 2, 3)], "width": size, "height": size})
        regions = sorted(b.regions(sid), key=lambda r: r.area)
        if not regions:
            continue
        cut = b.rng.random() < 0.5
        distance = -float(b.one([1, 2, 3])) if cut else float(b.one([1, 2, 4]))
        if b.apply("extrude", {"distance": distance, "mode": "cut" if cut else "join", "body_id": body_id}, region_refs(sid, [regions[0].index])):
            return True
    return False


def move_extrude_faces(b: Builder) -> bool:
    body_id = _target(b)
    if body_id is None:
        return False
    for _ in range(3):
        face = b.one(b.planar_faces(body_id))
        if face is None:
            return False
        distance = float(b.one([1.0, 2.0, -0.5, -1.0]))
        mode = "join" if distance > 0 else "cut"
        if b.apply("extrude", {"distance": distance, "mode": mode}, [face_ref(body_id, face["index"])]):
            return True
    return False


def move_extrude_face_legacy(b: Builder) -> bool:
    body_id = _target(b)
    if body_id is None:
        return False
    face = b.one(b.planar_faces(body_id))
    return face is not None and b.replay_only("extrude_face", {"body_id": body_id, "face_index": face["index"], "distance": float(b.one([2.0, -1.0]))})


def move_face_edit(b: Builder, verb: str) -> bool:
    body_id = _target(b)
    if body_id is None:
        return False
    faces = b.faces(body_id)
    for _ in range(3):
        if verb == "delete_faces":
            pool = [f for f in faces if f["kind"].startswith("cylinder") or f["kind"] in ("torus", "cone", "bspline")] or faces
            args: dict = {}
        elif verb == "offset_faces":
            pool = faces
            args = {"distance": float(b.one([0.3, 0.5, -0.3]))}
        else:
            pool = [f for f in faces if f["kind"] == "plane"]
            args = {"thickness": float(b.one([0.6, 1.0]))}
        face = b.one(pool)
        if face is None:
            return False
        if b.apply(verb, args, [face_ref(body_id, face["index"])]):
            return True
    return False


def move_thread(b: Builder) -> bool:
    for body in b.pick(b.solids(), 3):
        cylinders = [f for f in b.cylinders(body.id)]
        for face in b.pick(cylinders, 2):
            args: dict = {"profile": b.one(["trapezoid30", "trapezoid45"])}
            if b.apply("thread", args, [face_ref(body.id, face["index"])]):
                return True
    return False


def move_split(b: Builder) -> bool:
    body_id = _target(b)
    if body_id is None:
        return False
    low, high = b.bbox(body_id)
    keep = b.one(["both", "both", "positive", "negative"])
    choice = b.one(["base", "face", "sketch"])
    if choice == "face":
        face = b.one(b.planar_faces(body_id))
        if face is not None and b.apply("split_body", {"plane_face": face_ref(body_id, face["index"]), "keep": keep}, [body_ref(body_id)]):
            return True
    if choice == "sketch":
        sid = b.sketch("XY", offset=round((low[2] + high[2]) / 2, 3))
        if sid is not None and b.apply("split_body", {"sketch_id": sid, "keep": keep}, [body_ref(body_id)]):
            return True
    for plane in b.pick(["XY", "XZ", "YZ"], 3):
        if b.apply("split_body", {"base_plane": plane, "keep": keep}, [body_ref(body_id)]):
            return True
    return False


def move_mirror(b: Builder) -> bool:
    body_id = _target(b)
    if body_id is None:
        return False
    if b.rng.random() < 0.5:
        face = b.one(b.planar_faces(body_id))
        if face is not None and b.apply("mirror_body", {"plane_face": face_ref(body_id, face["index"])}, [body_ref(body_id)]):
            return True
    return b.apply("mirror_body", {"base_plane": b.one(["XY", "XZ", "YZ"])}, [body_ref(body_id)])


def move_body(b: Builder) -> bool:
    body_id = _target(b)
    if body_id is None:
        return False
    copy = b.rng.random() < 0.4
    style = b.one(["translate", "edge", "vertex"])
    if style == "edge":
        lines = [e for e in b.edges(body_id) if e["kind"] == "line" and e["length"] > 1.0]
        edge = b.one(lines)
        if edge is not None and b.apply("move_body", {"mode": "rotate", "angle": float(b.one([90, 45, 180])), "axis_edge": edge_ref(body_id, edge["index"]), "copy": copy}, [body_ref(body_id)]):
            return True
    if style == "vertex":
        count = b.vertex_count(body_id)
        if count and b.apply("move_body", {"mode": "rotate", "angle": 90.0, "axis_dir": [0, 0, 1], "center_vertex": vertex_ref(body_id, b.rng.randint(1, count)), "copy": copy}, [body_ref(body_id)]):
            return True
    return b.apply("move_body", {"dx": float(b.one([5, 30, -20])), "dy": float(b.one([0, 3])), "dz": 0.0, "copy": copy}, [body_ref(body_id)])


def _tool_body(b: Builder, target_id: str) -> Optional[str]:
    """ターゲットを横切る新しいボディ(箱か円柱)。"""
    low, high = b.bbox(target_id)
    sid = b.sketch("XY", offset=round(low[2] - 1.0, 3))
    if sid is None:
        return None
    cx, cy = round((low[0] + high[0]) / 2 + b.rng.uniform(-3, 3), 2), round((low[1] + high[1]) / 2, 2)
    span = max(2.0, round(min(high[0] - low[0], high[1] - low[1]) * b.one([0.3, 0.5]), 2))
    if b.rng.random() < 0.5:
        b.apply("add_circle", {"sketch_id": sid, "center": [cx, cy], "diameter": span})
    else:
        b.apply("add_rectangle", {"sketch_id": sid, "corner": [cx - span / 2, cy - span * 2], "width": span, "height": span * 4})
    return _extrude_new(b, sid, [1], round(high[2] - low[2] + 2.0, 3))


def move_boolean(b: Builder) -> bool:
    target_id = _target(b)
    if target_id is None:
        return False
    others = [s.id for s in b.solids() if s.id != target_id]
    tool_id = b.one(others) if others and b.rng.random() < 0.5 else _tool_body(b, target_id)
    if tool_id is None:
        return False
    tool_ids = [tool_id]
    if b.rng.random() < 0.35:
        # ツールが複数のとき、ツールどうしの並び(選んだ順)が結果の面の並びに効きうる(並列化の検討の題材)。
        extra = _tool_body(b, target_id)
        if extra is not None:
            tool_ids.append(extra)
    mode = b.one(["cut", "cut", "union", "intersect"])
    faces = b.faces(target_id)
    target = face_ref(target_id, b.one(faces)["index"]) if faces and b.rng.random() < 0.5 else body_ref(target_id)
    refs = []
    for tool in tool_ids:
        tool_faces = b.faces(tool)
        refs.append(face_ref(tool, b.one(tool_faces)["index"]) if tool_faces and b.rng.random() < 0.3 else body_ref(tool))
    return b.apply("boolean_body", {"target_ref": target, "mode": mode}, refs)


def move_project(b: Builder) -> bool:
    body_id = _target(b)
    if body_id is None:
        return False
    low, high = b.bbox(body_id)
    sid = b.sketch("XY", offset=round(high[2] + 2.0, 3))
    if sid is None:
        return False
    if b.rng.random() < 0.5:
        if not b.apply("project_outline", {"sketch_id": sid}, [body_ref(body_id)]):
            return False
    else:
        faces = b.faces(body_id)
        edges = b.edges(body_id)
        refs = [face_ref(body_id, f["index"]) for f in b.pick(faces, 2)] + [edge_ref(body_id, e["index"]) for e in b.pick(edges, 3)]
        if not b.apply("project_to_sketch", {"sketch_id": sid}, refs):
            return False
    regions = b.regions(sid)
    if not regions:
        return True
    chosen = sorted(r.index for r in b.pick(regions, b.rng.randint(1, min(3, len(regions)))))
    _extrude_new(b, sid, chosen, 3.0)
    return True


def move_envelope(b: Builder) -> bool:
    sid = b.sketch("XY", offset=-20.0)
    if sid is None:
        return False
    for k in range(b.rng.randint(2, 4)):
        b.apply("add_circle", {"sketch_id": sid, "center": [k * 12.0, b.one([0.0, 4.0])], "diameter": float(b.one([6, 8]))})
    regions = b.regions(sid)
    if len(regions) < 2 or not b.apply("envelope_regions", {"radius": float(b.one([2, 4])), "offset": float(b.one([0.0, 1.0]))}, region_refs(sid, [r.index for r in regions])):
        return False
    regions = b.regions(sid)
    _extrude_new(b, sid, [max(regions, key=lambda r: r.area).index], 2.0)
    return True


def move_optimize_setup(b: Builder) -> bool:
    """形状最適化の条件(拘束・荷重は面の添字で指し、保持ソリッドの面にしか置けない)。"""
    body_id = _target(b)
    if body_id is None or not b.apply("set_preserve_bodies", {}, [body_ref(body_id)]):
        return False
    faces = b.faces(body_id)
    b.apply("set_fixtures", {}, [face_ref(body_id, f["index"]) for f in b.pick(faces, 2)])
    b.apply("set_loads", {"magnitude": float(b.one([10, 50])), "direction": b.one(["normal", "-normal", [0, 0, -1]])}, [face_ref(body_id, b.one(faces)["index"])])
    if b.rng.random() < 0.3:
        b.apply("clear_loads", {}, [])
    others = [s.id for s in b.solids() if s.id != body_id]
    if others and b.rng.random() < 0.5:
        b.apply("set_exclude_bodies", {}, [body_ref(b.one(others))])
    return True


def move_rename(b: Builder) -> bool:
    items = [body.id for body in b.doc.bodies] + [s.id for s in b.doc.sketches]
    target = b.one(items)
    return target is not None and b.apply("rename", {"target": target, "name": f"part-{b.rng.randint(1, 99)}"})


def move_delete(b: Builder) -> bool:
    if b.rng.random() < 0.5 and b.doc.sketches:
        return b.apply("delete_items", {"sketches": [b.one(b.doc.sketches).id]})
    if len(b.doc.bodies) < 2:
        return False
    return b.apply("delete", {}, [body_ref(b.doc.bodies[-1].id)])


def move_parameter(b: Builder) -> bool:
    """寸法の値を変える(ログを頭から再生し直す。後の操作の添字は、変わった形にそのまま掛かる)。"""
    names = sorted(b.doc.parameters)
    for name in b.pick(names, 3):
        value = float(b.doc.parameters[name])
        if value <= 1.0:
            continue
        if b.set_parameter(name, round(value * b.one([0.9, 1.1]), 3)):
            return True
    return False


#: 続きの操作と重み。重みは連鎖を作る操作(面・辺・頂点を添字で指す)を厚くしてある。
MOVES: list = [
    ("fillet", 5, lambda b: move_blend(b, "fillet")),
    ("chamfer", 4, lambda b: move_blend(b, "chamfer")),
    ("sketch_on_face", 6, move_sketch_on_face),
    ("extrude_faces", 4, move_extrude_faces),
    ("extrude_face", 1, move_extrude_face_legacy),
    ("offset_faces", 3, lambda b: move_face_edit(b, "offset_faces")),
    ("shell_body", 2, lambda b: move_face_edit(b, "shell_body")),
    ("delete_faces", 3, lambda b: move_face_edit(b, "delete_faces")),
    ("thread", 3, move_thread),
    ("split_body", 3, move_split),
    ("mirror_body", 2, move_mirror),
    ("move_body", 3, move_body),
    ("boolean_body", 4, move_boolean),
    ("project", 2, move_project),
    ("envelope_regions", 1, move_envelope),
    ("optimize_setup", 1, move_optimize_setup),
    ("rename", 1, move_rename),
    ("delete", 1, move_delete),
    ("set_parameter", 1, move_parameter),
]


def index_refs(args: dict) -> int:
    """再生で添字のまま形に掛かる参照の数(辺・面の添字、面に貼ったスケッチの面、形状最適化の条件の面)。

    投影・鏡映・分割・回転の軸は解いた値(図形・平面・軸)をログに載せるので数えない。
    """
    count = 0
    if "body_id" in args:
        count += len(args.get("edges") or []) if isinstance(args.get("edges"), list) else 0
        count += len(args.get("faces") or []) if isinstance(args.get("faces"), list) else 0
    count += 1 if isinstance(args.get("face"), int) else 0
    count += 1 if "face_index" in args else 0
    plane = args.get("plane")
    while isinstance(plane, dict):
        count += 1 if plane.get("kind") == "face" else 0
        plane = plane.get("base")
    for key in ("fixtures", "loads"):
        count += len(args.get(key) or []) if isinstance(args.get(key), list) else 0
    return count


def _plane_body(args: dict) -> Optional[str]:
    plane = args.get("plane")
    while isinstance(plane, dict):
        if plane.get("kind") == "face":
            return plane.get("body")
        plane = plane.get("base")
    return None


def chain_count(entries: list) -> int:
    """添字で指した結果の形を、後の操作がまた添字で指した回数(面に貼ったスケッチから押し出した形も、添字に依る形に数える)。"""
    dependent: set = set()
    face_sketches: set = set()
    chained = 0
    for operation in entries:
        args = operation.args
        refs = index_refs(args)
        if operation.verb in ("sketch_create", "sketch_create_offset"):
            if refs:
                face_sketches.add(args.get("sketch_id"))
                chained += 1 if _plane_body(args) in dependent else 0
            continue
        target = args.get("body_id")
        regions = [r for r in (args.get("regions") or []) + (args.get("sections") or []) if isinstance(r, dict)]
        via_sketch = any(r.get("sketch") in face_sketches for r in regions)
        if refs and target in dependent:
            chained += 1
        if (refs or via_sketch) and target:
            dependent.add(target)
    return chained


def build_document(seed: int, number: int, workdir: Path, moves: int = 0) -> tuple:
    """1本の文書(ログの JSON 文字列)と、その要約。"""
    rng = random.Random(f"duocad-corpus/{seed}/{number}")
    base = BASES[number % len(BASES)]
    workdir = workdir / f"{number:04d}"
    workdir.mkdir(parents=True, exist_ok=True)
    with seeded_ids(f"ids/{seed}/{number}"):
        b = Builder(rng, workdir)
        BASE_FUNCTIONS[base](b)
        wanted = moves or rng.randint(4, 10)
        names = [name for name, _, _ in MOVES]
        weights = [weight for _, weight, _ in MOVES]
        table = dict((name, fn) for name, _, fn in MOVES)
        done: list = []
        attempts = 0
        while (len(done) < wanted or chain_count(b.doc.log.entries) < 1) and attempts < wanted * 4:
            attempts += 1
            name = rng.choices(names, weights)[0]
            if not b.solids() and name != "envelope_regions":
                base_plate(b)
                continue
            if table[name](b):
                done.append(name)
    entries = b.doc.log.entries
    summary = {
        "base": base,
        "moves": done,
        "operations": len(entries),
        "verbs": dict(Counter(op.verb for op in entries)),
        "index_refs": sum(index_refs(op.args) for op in entries),
        "chained": chain_count(entries),
        "refused": dict(sorted(b.refused.items())),
        "verb_ms": {k: round(v, 1) for k, v in sorted(b.verb_ms.items())},
        "refusal_examples": b.refusal_examples,
        "slow": b.slow,
        "bodies": len(b.doc.bodies),
        # 確定の経路で組んだ最後の状態。`steps` が再生の最後の状態と比べる(違えば確定と再生で形が割れている)。
        "final_state": state_digest(state_print(b.doc, _BodyPrints(False))),
    }
    text = b.doc.log.to_json()
    shutil.rmtree(workdir, ignore_errors=True)
    return text, summary


def generate_main(args) -> None:
    documents = args.out / "documents"
    documents.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="duocad-corpus-gen-"))
    manifest: dict = {"seed": args.seed, "count": args.count, "documents": {}}
    t0 = time.perf_counter()
    try:
        for number in range(args.first, args.first + args.count):
            name = f"corpus-{args.seed}-{number:04d}.json"
            started = time.perf_counter()
            try:
                text, summary = build_document(args.seed, number, workdir, args.moves)
            except CorruptDocument as error:
                manifest["documents"][name] = {"error": str(error)}
                print(f"{name}: CORRUPT {error}", flush=True)
                continue
            summary["generate_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
            (documents / name).write_text(text, encoding="utf-8")
            manifest["documents"][name] = summary
            print(f"{name}: {summary['base']:<12} {summary['operations']:>3} ops {summary['index_refs']:>3} refs {summary['chained']:>2} chained {summary['generate_ms']:.0f} ms", flush=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    verbs: Counter = Counter()
    refused: Counter = Counter()
    verb_ms: Counter = Counter()
    for summary in manifest["documents"].values():
        verbs.update(summary.get("verbs", {}))
        refused.update(summary.get("refused", {}))
        verb_ms.update(summary.get("verb_ms", {}))
    manifest["verbs"] = dict(sorted(verbs.items()))
    manifest["refused"] = dict(sorted(refused.items()))
    manifest["verb_ms"] = {k: round(v, 1) for k, v in sorted(verb_ms.items())}
    examples: dict = {}
    for summary in manifest["documents"].values():
        for key, text in summary.pop("refusal_examples", {}).items():
            examples.setdefault(key, text)
    manifest["refusal_examples"] = dict(sorted(examples.items()))
    manifest["seconds"] = round(time.perf_counter() - t0, 1)
    manifest["peak_memory_mb"] = peak_memory_mb()
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {args.count} documents to {documents} in {manifest['seconds']} s")
    print("verbs: " + ", ".join(f"{k}={v}" for k, v in manifest["verbs"].items()))


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--seed", type=int, default=1)
    gen.add_argument("--count", type=int, default=300)
    gen.add_argument("--first", type=int, default=0, help="最初の文書の番号(作り直したい番号だけを作る)")
    gen.add_argument("--moves", type=int, default=0, help="続きの操作の数(0 = 文書ごとに 4〜10)")
    steps = sub.add_parser("steps")
    steps.add_argument("paths", nargs="+")
    steps.add_argument("--out", type=Path, required=True)
    steps.add_argument("--triangles", action="store_true")
    steps.add_argument("--detail", action="store_true")
    cmp = sub.add_parser("compare")
    cmp.add_argument("before", type=Path)
    cmp.add_argument("after", type=Path)
    args = ap.parse_args(argv)
    if args.command == "generate":
        generate_main(args)
        return 0
    if args.command == "steps":
        steps_main(args)
        return 0
    return compare(args.before, args.after)


if __name__ == "__main__":
    sys.exit(main())

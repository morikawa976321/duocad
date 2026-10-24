"""干渉チェック(Issue #186)—— 対象の決め方・組の分類・結果の組み立て。方式と定義は docs/INTERFERENCE.md。

verb ではない(文書もログも変えない・undo の対象にならない)。MCP の `check_interference` と WebSocket の `check_interference` が
同じ `run` を通るので、AI が読む値と画面に出る値は同じ計算から出る。

## 流れ

1. `take_snapshot`(イベントループのスレッド): 文書の版・対象のボディ・形の写しを1つに確定する。以後、文書が変わっても結果は開始時のもの。
2. `run`(ワーカースレッド): 境界箱で絞る → 組ごとに子プロセスで2体 General Fuse(メッシュを含む組は manifold3d)→
   触れていない近い組だけ最短距離。時間の上限(`Limits`)を超えた組は子を止めて作り直し、残りは未着手として部分結果を返す。

OCP を直には import しない(幾何は `kernel_interference` / 子プロセスから素の値で受け取る)。
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import kernel, kernel_brep, kernel_interference, selection
from .document import Document
from .interference_worker import (
    WorkerCancelled,
    WorkerError,
    WorkerFailure,
    WorkerProcess,
    WorkerRemoteError,
    WorkerTimeout,
)

from . import (
    brep_geometry as _brep_geometry,
)

#: tiny_overlap にする厚さ[mm]。重なりを面でつながる領域に分け、**すべての領域で**厚さの目安 2V/A と有向境界箱の最小辺が**どちらも**これ以下。
#: 仮置き(根拠は PC ケースの実測だけ。FDM の精度とも形状許容差とも結び付けていない)。
THICKNESS_MM = 1e-3

#: 距離の比較帯[mm]。距離の保証精度ではない(隙間 0.2 で設計した組は 0.19999999999908〜0.2 と測れる)。仮置き。
GAP_BAND_MM = 1e-6

#: 結果の厚さ・比較帯の基準を結果に載せるときの名前。
THRESHOLDS = {"thickness_mm": THICKNESS_MM, "gap_band_mm": GAP_BAND_MM}

APPROXIMATE = "B-rep を 0.01 mm で三角形分割した近似"

#: 子が続けてこの回数壊れたら、残りの組は試さずに未着手として返す。
MAX_CONSECUTIVE_FAILURES = 3

_TOUCHING = ("overlap", "tiny_overlap", "contact")
_RELATIONS = ("overlap", "tiny_overlap", "contact", "separated", "unknown")


@dataclass(frozen=True)
class Limits:
    """時間の上限[秒]。精度ではなく待ち時間の運用値(仮置き)。全体には起動・受け渡し・作り直し・三角形分割・返送も含める。"""

    gf_seconds: float = 30.0
    distance_seconds: float = 5.0
    total_seconds: float = 120.0


class InterferenceCancelled(Exception):
    """要求した接続が検査を取り消した(結果は描かない)。"""


# ------------------------------------------------------------------------------------------------
# 引数と対象
# ------------------------------------------------------------------------------------------------
def check_clearance(value: Any) -> float:
    """隙間の引数。有限で 0 以上の数だけ通す(NaN は比較がすべて偽になり、無限大は境界箱の絞り込みを無効にする)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"clearance は 0 以上の有限の数(mm)で指定する: {value!r}")
    return float(value)


@dataclass(frozen=True)
class Targets:
    mode: str  # "visible_all" | "one_vs_rest" | "selected_set"
    selected: Tuple[str, ...]
    bodies: Tuple[str, ...]
    pairs: Tuple[Tuple[str, str], ...]
    excluded_hidden: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "selected": list(self.selected),
            "bodies": list(self.bodies),
            "excluded_hidden": self.excluded_hidden,
            "pairs": len(self.pairs),
        }


def resolve_targets(document: Document) -> Targets:
    """調べる組を決める。選択が無ければ**見えているボディ**の総当たり、1つだけなら**その1つ 対 ほかの見えている全ボディ**、
    2つ以上なら選んだものどうし。面・辺・頂点を選んでいれば持ち主のボディ。組が1つもできなければ `ValueError`。"""
    candidates = [body for body in document.bodies if body.shape is not None or body.mesh is not None]
    visible = [body.id for body in candidates if body.visible]
    hidden = len(candidates) - len(visible)
    owners: List[str] = []
    for item in selection.get_selection(document).items:
        owner = item.get("body")
        if owner is not None and owner in visible and owner not in owners:
            owners.append(owner)
    owners = [body_id for body_id in visible if body_id in owners]  # 文書の並び順
    if not owners:
        mode, pairs = "visible_all", list(combinations(visible, 2))
    elif len(owners) == 1:
        mode, pairs = "one_vs_rest", _in_order(visible, [(owners[0], other) for other in visible if other != owners[0]])
    else:
        mode, pairs = "selected_set", list(combinations(owners, 2))
    if not pairs:
        raise ValueError(_no_pairs_message(mode, len(visible), hidden))
    involved = [body_id for body_id in visible if any(body_id in pair for pair in pairs)]
    return Targets(mode, tuple(owners), tuple(involved), tuple(pairs), hidden)


def _in_order(order: Sequence[str], pairs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    rank = {body_id: index for index, body_id in enumerate(order)}
    return [tuple(sorted(pair, key=rank.__getitem__)) for pair in pairs]  # type: ignore[misc]


def _no_pairs_message(mode: str, visible: int, hidden: int) -> str:
    if mode == "one_vs_rest":
        return "干渉チェックの相手が無い: 選んだボディのほかに見えているボディが無い(隠したボディは調べない)"
    return f"干渉チェックする組が作れない: 見えているボディが {visible} 個しかない(隠した {hidden} 個は調べない。2個以上要る)"


@dataclass(frozen=True)
class BodyInput:
    id: str
    name: str
    kind: str  # "solid" | "mesh"
    shape: Any = None  # 形の写し(kind == "solid")
    positions: Tuple[float, ...] = ()
    indices: Tuple[int, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    revision: str
    targets: Targets
    bodies: Dict[str, BodyInput]


def take_snapshot(document: Document, revision: str) -> Snapshot:
    """イベントループのスレッドで呼ぶ。形は深い写し(別スレッドで読んでも、三角形分割を書き込む元の形と `TShape` を共有しない)。"""
    targets = resolve_targets(document)
    wanted = set(targets.bodies)
    bodies: Dict[str, BodyInput] = {}
    for body in document.bodies:
        if body.id not in wanted:
            continue
        if body.mesh is not None:
            bodies[body.id] = BodyInput(body.id, body.name, "mesh", positions=tuple(body.mesh.positions), indices=tuple(body.mesh.indices))
        else:
            bodies[body.id] = BodyInput(body.id, body.name, "solid", shape=kernel.copy_shape(body.shape))
    return Snapshot(revision, targets, bodies)


# ------------------------------------------------------------------------------------------------
# 分類(子プロセスが返した素の値 → 区分)
# ------------------------------------------------------------------------------------------------
def _finite(*values: Any) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def classify_relation(relation: Dict[str, Any]) -> Dict[str, Any]:
    """幾何の関係(`relation`)と、その値の欄。知らない種類・面積や体積が不正なものは unknown と理由にする(separated や 0 にしない)。"""
    kind = relation.get("kind")
    if kind == "overlap":
        if not _finite(relation.get("volume"), relation.get("thickness"), relation.get("obb_min")) or not relation["volume"] > 0:
            return {"relation": "unknown", "reason": f"重なりの体積・厚さが不正: {relation.get('volume')!r}"}
        thin = relation["thickness"] <= THICKNESS_MM and relation["obb_min"] <= THICKNESS_MM
        out = {"relation": "tiny_overlap" if thin else "overlap"}
        for key in ("volume", "thickness", "obb_min", "regions", "center", "bbox"):
            if key in relation:
                out[key] = relation[key]
        return out
    if kind == "face":
        if not _finite(relation.get("area")) or not relation["area"] > 0:
            return {"relation": "unknown", "reason": f"接触面積が不正: {relation.get('area')!r}"}
        return {"relation": "contact", "contact": "face", "area": relation["area"]}
    if kind in ("edge", "vertex", "contact"):
        return {"relation": "contact", "contact": {"edge": "edge", "vertex": "vertex", "contact": "mesh"}[kind]}
    if kind == "none":
        return {"relation": "separated"}
    if kind == "unknown":
        return {"relation": "unknown", "reason": str(relation.get("reason") or "不明")}
    return {"relation": "unknown", "reason": f"知らない関係の種類: {kind!r}"}


def classify_clearance(relation: str, distance: Optional[float], clearance: float) -> Optional[str]:
    """隙間の要求(`clearance > 0`)に対する判定。触れている組は short。離れている組・GF が答えなかった組は最短距離で決める
    (`distance` が無ければ None)。`short`: d < clearance − ε / `at`: |d − clearance| ≤ ε / `ok`: d > clearance + ε。"""
    if relation in _TOUCHING:
        return "short"
    if distance is None:
        return None
    if relation == "unknown" and distance <= GAP_BAND_MM:
        return "short"
    if distance < clearance - GAP_BAND_MM:
        return "short"
    if distance <= clearance + GAP_BAND_MM:
        return "at"
    return "ok"


# ------------------------------------------------------------------------------------------------
# 実行
# ------------------------------------------------------------------------------------------------
@dataclass
class Job:
    """開始時に確定した検査1件(`make_job`)。`run(render)` はワーカースレッドで、`cancel()` はどのスレッドからでも呼べる。"""

    run: Callable[[bool], Dict[str, Any]]
    cancel: Callable[[], None]


def make_job(document: Document, revision: str, clearance: Any, *, deviation: float, request_id: Any = None) -> Job:
    """イベントループのスレッドで呼ぶ。引数の検査・対象の確定・形の写しをここで済ませる(失敗はここで `ValueError`)。"""
    value = check_clearance(0.0 if clearance is None else clearance)
    snapshot = take_snapshot(document, revision)
    stop = threading.Event()

    def execute(render: bool = False) -> Dict[str, Any]:
        return run(snapshot, value, cancel=stop, render_deviation=deviation if render else None, request_id=request_id)

    return Job(execute, stop.set)


WorkerFactory = Callable[[Dict[str, Any], Optional[threading.Event]], Any]


def default_worker(load_message: Dict[str, Any], cancel: Optional[threading.Event]) -> Any:
    return WorkerProcess(load_message, cancel=cancel)


def _prepare(snapshot: Snapshot) -> Tuple[Dict[str, Any], Dict[str, Tuple[Any, Any]]]:
    """子へ渡す `load` と、ボディごとの境界箱(ワーカースレッドで。BRep の文字列化と箱の計算が重い)。"""
    payload, boxes = [], {}
    for body_id in snapshot.targets.bodies:
        body = snapshot.bodies[body_id]
        if body.kind == "mesh":
            payload.append({"id": body_id, "kind": "mesh", "positions": list(body.positions), "indices": list(body.indices)})
            xs, ys, zs = body.positions[0::3], body.positions[1::3], body.positions[2::3]
            boxes[body_id] = ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))
        else:
            payload.append({"id": body_id, "kind": "brep", "brep": kernel_brep.encode_brep(body.shape)})
            boxes[body_id] = _brep_geometry._interference_loose_bbox(body.shape)
    return {"op": "load", "bodies": payload}, boxes


def run(
    snapshot: Snapshot,
    clearance: float = 0.0,
    *,
    limits: Limits = Limits(),
    worker_factory: Optional[WorkerFactory] = None,
    cancel: Optional[threading.Event] = None,
    render_deviation: Optional[float] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """干渉チェックを走らせて、結果の辞書を返す(形は docs/INTERFERENCE.md「返すもの」)。

    `render_deviation` を渡すと、組ごとに `render`(画面へ描く三角形・折れ線・点)を足す。MCP は渡さない。
    取り消されたら `InterferenceCancelled`。
    """
    clearance = check_clearance(clearance)
    started = time.monotonic()
    deadline = started + limits.total_seconds
    load_message, boxes = _prepare(snapshot)
    targets = snapshot.targets
    # 帯 ε は clearance 0 でも足す(メッシュの組は「距離が ε 以下なら接触」なので、箱が ε だけ離れた組を箱の段で落とさない)
    reach = clearance + GAP_BAND_MM
    mesh_ids = {body_id for body_id, body in snapshot.bodies.items() if body.kind == "mesh"}

    plans: List[_Plan] = []
    for first, second in targets.pairs:
        gap = kernel_interference.box_gap(boxes[first], boxes[second])
        uses_mesh = first in mesh_ids or second in mesh_ids
        plan = _Plan(first, second, gap, uses_mesh)
        if gap > reach:
            plan.relation = {"kind": "none"}
            plan.outside = True
        elif gap > 0 and not uses_mesh:
            plan.relation = {"kind": "none"}  # 箱は形状許容差を含むので、箱が離れていれば GF の接触にならない
        plans.append(plan)

    state = _RunState(limits, deadline, cancel, render_deviation, clearance, max(1.0, 2 * reach))
    with (worker_factory or default_worker)(load_message, cancel) as worker:
        for plan in plans:
            if plan.needs_relation:
                state.relation(worker, plan)
        for plan in plans:
            if plan.needs_distance(clearance):
                state.distance(worker, plan)
        state.restarts += getattr(worker, "revived", 0)
    return _assemble(snapshot, plans, clearance, request_id, time.monotonic() - started, state, render_deviation is not None)


class _Plan:
    """組1つの計画と、測れた値。"""

    def __init__(self, first: str, second: str, gap: float, uses_mesh: bool) -> None:
        self.first, self.second, self.gap, self.uses_mesh = first, second, gap, uses_mesh
        self.relation: Optional[Dict[str, Any]] = None
        self.outside = False
        self.not_started = False
        self.no_distance_reason: Optional[str] = None
        self.distance: Optional[Dict[str, Any]] = None
        self.distance_reason: Optional[str] = None
        self.distance_skipped = False

    @property
    def needs_relation(self) -> bool:
        return self.relation is None

    def needs_distance(self, clearance: float) -> bool:
        """B-rep どうしで、触れていない(か GF が答えなかった)組。隙間を問われていなければ GF が答えなかった組だけ。"""
        if self.uses_mesh or self.outside or self.not_started or self.no_distance_reason is not None:
            return False
        kind = classify_relation(self.relation or {"kind": "unknown"})["relation"]
        if kind == "unknown":
            return True
        return kind == "separated" and clearance > 0


class _RunState:
    def __init__(self, limits: Limits, deadline: float, cancel: Optional[threading.Event],
                 deviation: Optional[float], clearance: float, search: float) -> None:
        self.limits, self.deadline, self.cancel = limits, deadline, cancel
        self.deviation, self.clearance, self.search = deviation, clearance, search
        self.restarts = 0
        self.timeouts: List[Dict[str, Any]] = []
        self.failures = 0  # 続けて子が壊れた回数。続けて失敗するなら残りを試さない(作り直しのたびに起動の時間がかかる)

    def _gave_up(self) -> bool:
        return self.failures >= MAX_CONSECUTIVE_FAILURES

    def _broken(self, worker: Any) -> None:
        worker.restart()
        self.restarts += 1
        self.failures += 1

    def _seconds(self, limit: float) -> float:
        return min(limit, self.deadline - time.monotonic())

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise InterferenceCancelled()

    def relation(self, worker: Any, plan: _Plan) -> None:
        self._check_cancel()
        seconds = self._seconds(self.limits.gf_seconds)
        if seconds <= 0 or self._gave_up():
            reason = "子プロセスが続けて失敗したので試さなかった" if self._gave_up() else f"全体の上限 {self.limits.total_seconds:g} 秒に達した"
            plan.relation = {"kind": "unknown", "reason": f"未着手({reason})"}
            plan.not_started = True
            return
        message = {"op": "relation", "a": plan.first, "b": plan.second, "search": self.search, "deviation": self.deviation}
        try:
            plan.relation = worker.request(message, seconds)
            self.failures = 0
        except WorkerCancelled:
            raise InterferenceCancelled() from None
        except WorkerTimeout:
            self._broken(worker)
            self.failures = 0  # 難しい形で時間を使い切ったのは子の故障ではない
            self.timeouts.append({"a": plan.first, "b": plan.second, "phase": "relation"})
            plan.relation = {"kind": "unknown", "reason": f"{seconds:.0f} 秒以内に終わらず打ち切った"}
        except WorkerRemoteError as error:
            self.failures = 0
            plan.relation = {"kind": "unknown", "reason": f"計算が失敗した: {error.reason}"}
        except WorkerError as error:
            self._broken(worker)
            plan.relation = {"kind": "unknown", "reason": error.reason}
            plan.no_distance_reason = error.reason

    def distance(self, worker: Any, plan: _Plan) -> None:
        self._check_cancel()
        seconds = self._seconds(self.limits.distance_seconds)
        if seconds <= 0 or self._gave_up():
            plan.distance_skipped = True
            plan.distance_reason = "最短距離は未着手(" + ("子プロセスが続けて失敗した" if self._gave_up() else f"全体の上限 {self.limits.total_seconds:g} 秒に達した") + ")"
            return
        try:
            plan.distance = worker.request({"op": "distance", "a": plan.first, "b": plan.second}, seconds)
            self.failures = 0
        except WorkerCancelled:
            raise InterferenceCancelled() from None
        except WorkerTimeout:
            self._broken(worker)
            self.failures = 0
            self.timeouts.append({"a": plan.first, "b": plan.second, "phase": "distance"})
            plan.distance_reason = f"最短距離が {seconds:.0f} 秒以内に終わらず打ち切った"
        except WorkerRemoteError as error:
            self.failures = 0
            plan.distance_reason = f"最短距離の計算が失敗した: {error.reason}"
        except WorkerError as error:
            self._broken(worker)
            plan.distance_reason = error.reason


def _assemble(snapshot: Snapshot, plans: List[_Plan], clearance: float, request_id: Optional[str],
              seconds: float, state: _RunState, with_render: bool) -> Dict[str, Any]:
    counts = {"overlap": 0, "tiny_overlap": 0, "contact": 0, "separated": 0, "unknown": 0,
              "short": 0, "at": 0, "ok": 0, "clearance_null": 0}
    pairs: List[Dict[str, Any]] = []
    not_started = 0
    for plan in plans:
        record = _record(snapshot, plan, clearance, with_render)
        counts[record["relation"]] += 1
        if plan.not_started:
            not_started += 1
        if clearance > 0:
            counts["clearance_null" if record.get("clearance") is None else record["clearance"]] += 1
        if not (record["relation"] == "separated" and record.get("clearance", "ok") == "ok"):
            pairs.append(record)
    incomplete = any(plan.not_started or plan.distance_skipped for plan in plans)
    return {
        "request_id": request_id or uuid.uuid4().hex,
        "document_revision": snapshot.revision,
        "units": "mm",
        "target": snapshot.targets.to_dict(),
        "clearance": clearance,
        "method": "gf2+distance" + ("+mesh" if any(p.uses_mesh for p in plans) else ""),
        "seconds": round(seconds, 3),
        "complete": not incomplete,
        "not_started": not_started,
        "child_restarts": state.restarts,
        "timeouts": state.timeouts,
        "thresholds": dict(THRESHOLDS),
        "counts": counts,
        "pairs": pairs,
    }


def _record(snapshot: Snapshot, plan: _Plan, clearance: float, with_render: bool) -> Dict[str, Any]:
    relation = plan.relation or {"kind": "unknown", "reason": "不明"}
    classified = classify_relation(relation)
    record: Dict[str, Any] = {
        "a": plan.first, "b": plan.second,
        "a_name": snapshot.bodies[plan.first].name, "b_name": snapshot.bodies[plan.second].name,
        **classified,
        "warnings": list(relation.get("warnings") or []),
    }
    if plan.uses_mesh:
        record["approximate"] = APPROXIMATE
    distance, points, upper = _distance_of(plan, relation)
    if classified["relation"] in ("separated", "unknown") and distance is not None:
        record["distance"] = distance
        if points is not None:
            record["points"] = points
        if upper:
            record["distance_upper_bound"] = True
        if relation.get("distance_lower_bound"):
            record["distance_lower_bound"] = True
    if clearance > 0:
        judged = classify_clearance(classified["relation"], distance, clearance)
        record["clearance"] = judged
        if judged is None:
            record["clearance_reason"] = plan.distance_reason or plan.no_distance_reason or "最短距離が測れなかった"
    if with_render and isinstance(relation.get("render"), dict):
        record["render"] = relation["render"]
    return record


def _distance_of(plan: _Plan, relation: Dict[str, Any]) -> Tuple[Optional[float], Optional[list], bool]:
    if isinstance(relation.get("distance"), (int, float)):  # メッシュ経路は関係と同時に距離が出る
        return float(relation["distance"]), None, False
    if plan.outside:
        return float(plan.gap), None, False
    measured = plan.distance
    if measured and measured.get("ok") and _finite(measured.get("distance")):
        return float(measured["distance"]), measured.get("points"), bool(measured.get("upper_bound"))
    if measured and not measured.get("ok"):
        plan.distance_reason = plan.distance_reason or str(measured.get("reason") or "最短距離が測れなかった")
    return None, None, False

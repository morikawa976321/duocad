"""verb `extrude` — スケッチの**領域**、または**同一平面・同一ボディのソリッドの面群**を押し出す。

領域は `extrude(doc, regions, ...)`、面は `extrude_faces(doc, faces, ...)`。どちらも同じ verb `extrude`
として記録し、ログの args が `regions` か `faces` かで再生を分ける。

- `"join"` … 相手のボディへ足す。相手が無ければ新しいソリッドを作る(最初の板)。作った場合も
  `args["body_id"]` にそのIDを記録する(再生で同じIDが再現できないと選択が壊れる)。
- `"cut"` … 相手のボディから引く。相手が無ければ ValueError。距離の正は面の外向き法線側なので、
  面から削るには負の距離で `cut`。
- `"new"` … 何に触れていても常に新しいソリッドを作る(これが無いと既存部品がいる文書で2つ目の板が
  作れない)。`body_id` は受け付けない。

相手(`body_id` を省略したとき)は、押し出す立体に**触れる・重なる、見えている編集できるソリッドの全部**
(`_place`)。join は全部を1つに融合して先頭(文書の並び順)のボディに残し、他は文書から消す。cut は全部を
削る(触れても削れなかったボディは相手にしない)。決まった相手はログに残し(`body_id` と `other_bodies`)、
redo / 再生は探し直さない(前の操作の形が変わると相手が入れ替わる)。`body_id` を渡せばそのボディだけが相手。

`taper`(度)は側面の傾き。正で押し出すにつれて太り、負で細る(Autodesk Fusion と同じ符号。穴は外周と逆に動く)。
領域を複数選ぶと、隣り合う領域は1つの輪郭にまとめて傾ける(領域の境目には傾いた壁を作らない)。
0 は今までの押し出しで、ログの args には 0 でないときだけ `taper` を記録する(無ければ 0 として再生する)。

失敗(領域が空・添字が範囲外・`mode` が未知・距離0・ブーリアン失敗・テーパーで断面が潰れる)は例外を送出し、documentにも
ログにも変更を残さない。「切り取ったのに穴が1つも開いていない」を成功として通さないこと。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import brep_checks, kernel, kernel_extrude, kernel_sketch, naming
from ..document import Body, BodyId, Document, new_body_id
from ..oplog import Operation, register_verb
from .. import selection
from ..entry import entry
from ._common import (
    detach_bodies,
    editable_body,
    editable_sketch,
    reattach_bodies,
    recall,
    remember,
    replayed_body,
    resolve_shape_ref,
)
from ..region import RegionRef, _regions_detailed
from ..sketch import get_sketch

VERB_EXTRUDE = "extrude"

#: 押し出しの種別(join | cut ＋ new)
MODES = ("join", "cut", "new")

#: join で相手へ融合して文書から消えたボディの undo 用の退避(`detach_bodies` の戻り)を覚える鍵
_MERGED_KEY = "extrude:merged"

#: `(相手の形 | None, 相手と一緒に融合する形)` から kernel の結果(`shape` と `provenance`)を返す
Build = Callable[[Any, Sequence[Any]], dict]


def extrude(
    doc: Document,
    regions: Sequence[RegionRef],
    distance: float,
    mode: str = "join",
    body_id: Optional[BodyId] = None,
    taper: float = 0.0,
) -> Operation:
    distance = kernel_extrude.check_distance(distance)
    taper = kernel_extrude.check_taper(taper)
    if mode not in MODES:
        raise ValueError(f"unknown extrude mode: {mode!r} (one of {list(MODES)})")

    refs = list(regions)
    if not refs:
        raise ValueError("extrude requires at least one region")

    for ref in refs:
        editable_sketch(doc, ref.sketch)
    faces, normal = _resolve_faces_and_normal(doc, refs)
    placement = _place(
        doc,
        mode,
        body_id,
        VERB_EXTRUDE,
        _region_build(faces, distance, mode, normal, taper),
        lambda: kernel_extrude.extrude_regions(faces, distance, "new", normal, taper=taper)["shape"],
    )
    operation = doc.log.append(
        VERB_EXTRUDE,
        {
            "regions": [ref.to_dict() for ref in refs],
            "distance": distance,
            **_taper_arg(taper),
            "mode": mode,
            **placement.args(),
        },
    )
    placement.record(doc, operation)
    return operation


def extrude_faces(
    doc: Document,
    faces: Sequence[dict],
    distance: float,
    mode: str = "join",
    body_id: Optional[BodyId] = None,
    taper: float = 0.0,
) -> Operation:
    """同一ボディの平面の面群(`{"body", "index"}` の並び)を押し出す。

    失敗時は例外を送出し、**documentにもログにも変更を残さない**(幾何計算をログの append より先に済ませる)。
    """
    distance = kernel_extrude.check_distance(distance)
    taper = kernel_extrude.check_taper(taper)
    if mode not in MODES:
        raise ValueError(f"unknown extrude mode: {mode!r} (one of {list(MODES)})")

    source, indices = _resolve_face_source(doc, faces, require_visible=True)
    placement = _place(
        doc,
        mode,
        body_id,
        VERB_EXTRUDE,
        _face_build(source.shape, indices, distance, mode, taper),
        lambda: kernel_extrude.extrude_faces(source.shape, indices, distance, "new", taper=taper)["shape"],
        unreached_hint=kernel_extrude.OUTWARD_CUT_HINT if distance > 0 else "",
    )
    operation = doc.log.append(
        VERB_EXTRUDE,
        {
            "faces": [{"body": source.id, "index": index} for index in indices],
            "distance": distance,
            **_taper_arg(taper),
            "mode": mode,
            **placement.args(),
        },
    )
    placement.record(doc, operation)
    return operation


def _region_build(faces: Sequence[Any], distance: float, mode: str, normal: Sequence[float], taper: float) -> Build:
    """領域の押し出しの計算。相手のボディがあるときは結果を覚え、プレビュー・確定・redo で使い回す(`kernel.remembered`。
    相手の無い `new` は軽いので覚えない)。"""
    def build(target_shape: Any, merged: Sequence[Any]) -> dict:
        def compute() -> dict:
            return kernel_extrude.extrude_regions(
                faces, distance, mode, normal, target_shape=target_shape, taper=taper, merged=merged
            )

        if target_shape is None:
            return compute()
        arguments = (kernel.shapes_digest(faces), distance, mode, tuple(float(v) for v in normal), taper)
        return kernel.remembered("extrude_regions", (target_shape, *merged), arguments, compute)

    return build


def _face_build(source_shape: Any, indices: Sequence[int], distance: float, mode: str, taper: float) -> Build:
    """面の押し出しの計算(`_region_build` と同じく、相手があるときだけ覚える)。"""
    def build(target_shape: Any, merged: Sequence[Any]) -> dict:
        def compute() -> dict:
            return kernel_extrude.extrude_faces(
                source_shape, indices, distance, mode, target_shape=target_shape, taper=taper, merged=merged
            )

        if target_shape is None:
            return compute()
        return kernel.remembered(
            "extrude_faces", (source_shape, target_shape, *merged), (tuple(indices), distance, mode, taper), compute
        )

    return build


def _taper_arg(taper: float) -> dict:
    """ログの args へ足すテーパー。0 は記録しない(テーパーの無い古いログと同じ args になる)。"""
    return {"taper": taper} if taper else {}


def _resolve_face_source(
    doc: Document, faces: Sequence[Any], *, require_visible: bool
) -> tuple[Body, List[int]]:
    """面の参照から (面を持つボディ, 添字の並び) を取る。**別のボディにまたがる選択は ValueError**。
    新しい押し出しは面の持ち主に「見えないものは指せない」を課す(`editable_body` と同じ理由)。redo / 再生は課さない
    (`replayed_body`)。"""
    refs = list(faces)
    if not refs:
        raise ValueError("extrude requires at least one face")
    source: Optional[Body] = None
    indices: List[int] = []
    for position, ref in enumerate(refs, start=1):
        body, index = resolve_shape_ref(
            doc, ref, kind="face", what="extrude face", mesh_hint="(メッシュボディの面は押し出せない)"
        )
        if source is None:
            source = body
        elif body.id != source.id:
            raise ValueError(
                f"selected face #{position} belongs to body {body.id!r} but #1 belongs to "
                f"{source.id!r}: 別のボディにまたがる面は押し出せない(同一ボディの面だけ)"
            )
        indices.append(index)
    assert source is not None
    if require_visible and not source.visible:
        raise ValueError(
            f"body {source.id!r} is hidden: 見えないものは指せない(set_visible で表示すること)"
        )
    return source, indices


@dataclass
class _Placement:
    """押し出し・回転体・ロフトの結果を文書へ置いたもの(ログの args と undo の記録に使う)。"""

    body_id: BodyId
    provenance: dict
    #: 先頭の相手と一緒に変わったボディ(join は融合して消えた、cut は一緒に削った)
    others: List[BodyId] = field(default_factory=list)
    numbered: dict = field(default_factory=dict)
    #: body_id -> 前の形(新しく作ったボディは None)
    previous: Dict[BodyId, Any] = field(default_factory=dict)
    detached: Optional[dict] = None

    def args(self) -> dict:
        return {"body_id": self.body_id, **({"other_bodies": list(self.others)} if self.others else {}), **self.numbered}

    def record(self, doc: Document, operation: Operation) -> None:
        if operation.provenance is None:
            operation.provenance = self.provenance
        for body_id, shape in self.previous.items():
            remember(doc, body_id, operation.seq, shape)
        if self.detached is not None:
            remember(doc, _MERGED_KEY, operation.seq, self.detached)


def _place(
    doc: Document,
    mode: str,
    body_id: Optional[BodyId],
    what: str,
    build: Build,
    tool: Callable[[], Any],
    *,
    unreached_hint: str = "",
) -> _Placement:
    """相手を決めて計算し、文書へ置く。計算が全部通ってから文書を変える(失敗したら何も残さない)。

    `body_id` を省略した join / cut の相手は、`tool()`(相手の無い立体)に触れる・重なる、見えている編集できる
    ソリッドの全部(隠したボディを黙って変えない。`editable_body` と同じ理由)。
    """
    if mode == "new":
        if body_id is not None:
            raise ValueError(
                f"new does not take a body_id (got {body_id!r}): "
                "新規ボディに行き先は無い。足すなら mode='join' を使うこと"
            )
        return _place_new(doc, build)
    if body_id is not None:
        target = _editable_target(doc, body_id, what)
        return _place_on(doc, [(target, build(target.shape, ()))])

    candidates = [body for body in doc.bodies if body.visible and doc.is_editable(body.id) and body.shape is not None]
    hits = [candidates[position] for position in kernel.touching_shapes(tool(), [body.shape for body in candidates])]
    if mode == "join":
        if not hits:
            return _place_new(doc, build)
        return _place_merged(doc, hits[0], hits[1:], build(hits[0].shape, [body.shape for body in hits[1:]]))

    reached = []
    for body in hits:
        try:
            reached.append((body, build(body.shape, ())))
        except brep_checks.Unchanged:
            continue  # 触れているだけで削れない(面で接する)ボディは相手にしない
    if not reached:
        raise ValueError(
            "cut reaches no visible body: 切り取る立体が、見えているどのボディにも食い込まない(隠したボディは切らない)"
            + (f"。{unreached_hint}" if unreached_hint else "")
        )
    return _place_on(doc, reached)


def _place_new(doc: Document, build: Build) -> _Placement:
    result = build(None, ())
    created = new_body_id()
    numbered = _add_numbered_body(doc, created, result["shape"])
    return _Placement(created, result["provenance"], numbered=numbered, previous={created: None})


def _place_on(doc: Document, results: Sequence[tuple]) -> _Placement:
    """相手ごとに計算した結果(`(ボディ, 結果)` の並び)を置く。2つ以上は cut だけ(join は `_place_merged`)。"""
    previous = {}
    for body, result in results:
        previous[body.id] = body.shape
        body.shape = result["shape"]
    first = results[0][1]["provenance"]
    provenance = first if len(results) == 1 else {
        **first,
        "other_bodies": {body.id: result["provenance"] for body, result in results[1:]},
    }
    return _Placement(results[0][0].id, provenance, others=[body.id for body, _ in results[1:]], previous=previous)


def _place_merged(doc: Document, target: Body, merged: Sequence[Body], result: dict) -> _Placement:
    """join: `target` に `merged` ごと融合した結果を置き、`merged` を文書から消す。

    消したボディが active だったら `target` を active にする(`boolean_body` と同じ。undo は退避した active を戻す)。"""
    previous = {target.id: target.shape}
    target.shape = result["shape"]
    detached = None
    if merged:
        merged_ids = [body.id for body in merged]
        detached = detach_bodies(doc, merged_ids)
        if detached["active"] in merged_ids:
            doc.set_active_body(target.id)
    return _Placement(
        target.id, result["provenance"], others=[body.id for body in merged], previous=previous, detached=detached
    )


def replay_placement(document: Document, operation: Operation, build: Build, what: str) -> None:
    """redo / 再生: 記録した相手(`body_id` と `other_bodies`)へ掛け直す(探し直さない)。ログには触らない。"""
    args = operation.args
    body_id = args["body_id"]
    mode = args["mode"]
    others = [str(other) for other in args.get("other_bodies") or []]
    # 存在検査(無いことは join の「新しく作る」への分岐条件で、失敗ではない)。`get_body` は KeyError なので使わない。
    existing = next((candidate for candidate in document.bodies if candidate.id == body_id), None)
    if mode == "new" and existing is not None:
        raise ValueError(f"cannot replay new: body {body_id!r} already exists")
    if existing is None and (mode == "cut" or others):
        raise ValueError(f"cannot replay {mode} without target body {body_id!r}")
    if mode == "new" or existing is None:
        result = build(None, ())
        _add_new_body(document, body_id, result["shape"], _recorded_name(document, args))
        placement = _Placement(body_id, result["provenance"], previous={body_id: None})
    elif mode == "join":
        target = _replayed_target(document, body_id, what)
        merged = [_replayed_target(document, other, what) for other in others]
        placement = _place_merged(document, target, merged, build(target.shape, [body.shape for body in merged]))
    else:
        bodies = [_replayed_target(document, target_id, what) for target_id in [body_id, *others]]
        placement = _place_on(document, [(body, build(body.shape, ())) for body in bodies])
    placement.record(document, operation)


def undo_placement(document: Document, operation: Operation) -> None:
    """undo: 相手の形を戻し(新しく作ったボディなら取り除き)、join で消えたボディを元の添字と active ごと戻す。"""
    args = operation.args
    body_id = args["body_id"]
    others = [str(other) for other in args.get("other_bodies") or []]
    previous = recall(document, body_id, operation.seq)
    if previous is None:
        document.remove_added_body(body_id)
        return
    document.get_body(body_id).shape = previous
    if args["mode"] == "join":
        if others:
            reattach_bodies(document, recall(document, _MERGED_KEY, operation.seq))
        return
    for other in others:
        document.get_body(other).shape = recall(document, other, operation.seq)


def changed_bodies(operation: Operation) -> dict:
    """入口の応答に載せる、形の変わったボディと消えたボディ。"""
    args = operation.args
    others = list(args.get("other_bodies") or [])
    if args["mode"] == "join":
        return {"changed_bodies": [args["body_id"]], "removed_bodies": others}
    return {"changed_bodies": [args["body_id"], *others], "removed_bodies": []}


def _editable_target(doc: Document, body_id: BodyId, what: str = VERB_EXTRUDE) -> Body:
    """押し出し・回転体の対象にできるボディ(編集可・可視・形状あり。メッシュは「足す/引く対象にする」ことを断る)。"""
    return editable_body(doc, body_id, what=what, reject_as=_reject_as(what))


def _replayed_target(doc: Document, body_id: BodyId, what: str = VERB_EXTRUDE) -> Body:
    """redo / 再生で足す・引く対象。`_editable_target` から可視性の門だけを外したもの(`_common.replayed_body`)。"""
    return replayed_body(doc, body_id, what=what, reject_as=_reject_as(what))


def _reject_as(what: str) -> str:
    return "押し出しの対象にする" if what == VERB_EXTRUDE else "足す/引く対象にする"


def _add_numbered_body(doc: Document, body_id: BodyId, shape: Any) -> dict:
    """押し出した形を新しいソリッドとして、通し番号の名前で足す。戻り値はログの args へ足すもの。"""
    number = naming.allocate(doc, naming.BODY)
    _add_new_body(doc, body_id, shape, naming.label(naming.BODY, number))
    return {"number": number}


def _add_new_body(doc: Document, body_id: BodyId, shape: Any, name: str) -> None:
    """押し出した形を新しいソリッドとして足す(`join` で対象が無いとき・`new`・その再生)。"""
    doc.add_body(Body(id=body_id, name=name, kind="solid", shape=shape))


def _recorded_name(document: Document, args: dict) -> str:
    """redo / 再生で作り直すボディの名前(番号の記録が無い古いログは "solid")。"""
    return naming.recorded(document, naming.BODY, args.get("number"), "solid")


def _resolve_faces_and_normal(
    doc: Document, refs: Sequence[RegionRef], what: str = VERB_EXTRUDE
) -> tuple[List[Any], tuple]:
    """領域参照から面と、スケッチ平面の法線を取り出す。範囲外は ValueError。

    **1回の押し出しが対象にできるのは1つのスケッチの領域だけ**。向きはスケッチ平面の法線で決まり、
    別平面の領域を混ぜると最初のスケッチの法線で全部押すことになって、方向がその面に寝た領域は
    体積0の退化ソリッドになる。`IsDone()` も `BRepCheck_Analyzer` も通ってしまうので、ここで弾くのが
    唯一の関門。回転体(`revolve`)も軸がそのスケッチの平面上にあるので同じ制約を持つ。
    """
    sketch_ids = {ref.sketch for ref in refs}
    if len(sketch_ids) > 1:
        raise ValueError(
            f"{what} takes regions of a single sketch, got {sorted(sketch_ids)!r} "
            "(向きと軸はスケッチ平面で決まるため)"
        )

    sketch_id = refs[0].sketch
    detailed = _regions_detailed(doc, sketch_id)  # Splitterは1スケッチにつき1回で足りる
    faces: List[Any] = []
    for ref in refs:
        match = next((item for item in detailed if item["index"] == ref.index), None)
        if match is None:
            raise ValueError(
                f"region index out of range: {ref.index} (sketch {ref.sketch!r})"
            )
        faces.append(match["face"])

    sketch = get_sketch(doc, sketch_id)
    # 面に貼ったスケッチでもライブの面は引かない。法線は作成時に `plane["frame"]` へ焼き付けてある
    # (`kernel_sketch.face_frame`)。添字で引き直すと、貼った先のボディを後から面取りしただけで
    # 押し出し方向が別の面の法線へ移りうる。
    normal = kernel_sketch.sketch_plane_normal(sketch.plane)
    return faces, normal



# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_extrude(document: Document, operation: Operation) -> None:
    """redo / replay 時の再適用。**ログには触らない**(触ると二重記録になる)。"""
    args = operation.args
    mode = args["mode"]
    distance = float(args["distance"])
    taper = float(args.get("taper", 0.0))
    if "faces" in args:
        # 記録した添字で面を引き直す(形が決定的なので同じ面を指す)。
        source, indices = _resolve_face_source(document, args["faces"], require_visible=False)
        build = _face_build(source.shape, indices, distance, mode, taper)
    else:
        refs = [RegionRef.from_dict(item) for item in args["regions"]]
        faces, normal = _resolve_faces_and_normal(document, refs)
        build = _region_build(faces, distance, mode, normal, taper)
    replay_placement(document, operation, build, VERB_EXTRUDE)


register_verb(VERB_EXTRUDE, do=_do_extrude, undo=undo_placement)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_EXTRUDE)
def _enter_extrude(
    document: Document,
    distance: float,
    mode: str = "join",
    body_id: Optional[BodyId] = None,
    taper: float = 0.0,
) -> dict:
    """選択の領域、または同一平面・同一ボディの面を押し出す(混在は ValueError)。

    `body_id` だけは引数のまま(どの Body へ足す/から引くかであって、対象集合ではない)。
    """
    kind, items = selection.require_extrude_targets(document)
    if kind == "face":
        operation = extrude_faces(
            document, items, float(distance), mode=str(mode), body_id=body_id, taper=float(taper)
        )
    else:
        refs = [RegionRef.from_dict(item) for item in items]
        operation = extrude(document, refs, float(distance), mode=str(mode), body_id=body_id, taper=float(taper))
    return {
        "body_id": operation.args["body_id"],
        "distance": float(distance),
        "mode": str(mode),
        **changed_bodies(operation),
    }

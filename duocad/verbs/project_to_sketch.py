"""verb `project_to_sketch` / `project_outline` — ソリッドの辺・面・ボディの外形を、スケッチの平面へ写した図形として足す(issue #184)。

- `project_to_sketch`: 選択の**辺**(1本ずつ)と**面**(外周と穴のループ)を、スケッチの平面へ正投影する。
- `project_outline`: 選択が属する**ボディ**(面・辺・頂点・ボディの参照から逆引き)を、平面の法線方向から見た影の境界にする
  (外周と、向こうが透けて見える穴。止まり穴の底などの内側の線は出さない)。
- 写した図形は普通の図形で、元の形に**追従しない**(写した時点で固定。パラメトリックを持たない。docs/DESIGN_DECISIONS.md)。
- 点に潰れるもの(平面に垂直な辺)は図形を作らない。全部が潰れて図形が1つもできなければ ValueError で、文書もログも変えない。
- 1回の呼び出しの中で同じ図形は1つにする(箱の上面と下面を写すと同じ長方形)。既にスケッチにある図形との重複は除かない。
- **ログには解決済みの図形の数値を載せる**。redo / replay は形を引き直さず、記録した図形を記録した ID で足す
  (面・辺の添字は後の編集で指す先が変わりうるため。`envelope_regions` と同じ)。
- メッシュのボディは断る(B-rep が無い)。

**OCP / build123d を直接importしない**(幾何は `kernel_project`)。
"""
from __future__ import annotations

from typing import Any, List, Sequence

from .. import kernel, kernel_project, selection
from ..document import Document
from ..entry import entry
from ..oplog import Operation, register_verb
from ..sketch import add_entities, get_sketch, new_entity_id, remove_entities, require_valid_params

VERB_PROJECT_TO_SKETCH = "project_to_sketch"
VERB_PROJECT_OUTLINE = "project_outline"

_MESH_HINT = "(メッシュボディは写せない。B-rep のボディを選ぶ)"
_COLLAPSED = "写すと点に潰れて図形ができない(スケッチの平面に垂直な辺だけを選んでいる)"


def _brep(doc: Document, body_id: str) -> Any:
    body = doc.get_body(str(body_id))
    if body.shape is None:
        raise ValueError(f"ボディ {body.id!r} を写せない{_MESH_HINT}")
    return body.shape


def projected_entities(doc: Document, sketch_id: str, targets: Sequence[dict]) -> List[dict]:
    """辺・面の参照 `targets` をスケッチ `sketch_id` の平面へ写した図形(文書は変えない)。"""
    plane = get_sketch(doc, sketch_id).plane
    edges: List[Any] = []
    for ref in targets:
        kind = selection.ref_kind(ref)
        shape = _brep(doc, ref["body"])
        if kind == "edge":
            edges.append(kernel.edge_of(shape, int(ref["index"])))
        elif kind == "face":
            edges.extend(kernel_project.face_edges(kernel.face_of(shape, int(ref["index"]))))
        else:
            raise ValueError(f"project_to_sketch が写せるのは辺と面だけ(got {kind}。ボディの外形は project_outline)")
    return kernel_project.edge_entities(plane, edges)


def outline_entities(doc: Document, sketch_id: str, body_ids: Sequence[str]) -> List[dict]:
    """ボディ `body_ids` の影の境界をスケッチ `sketch_id` の平面に描く図形(文書は変えない)。"""
    plane = get_sketch(doc, sketch_id).plane
    return kernel_project.outline_entities(plane, [_brep(doc, body_id) for body_id in body_ids])


def _add_projected(doc: Document, verb: str, sketch_id: str, entities: List[dict], recorded: dict) -> Operation:
    if not entities:
        raise ValueError(_COLLAPSED)
    for entity in entities:
        require_valid_params(entity["kind"], entity["params"])
    sketch = get_sketch(doc, sketch_id)
    created = add_entities(
        doc,
        [(sketch, new_entity_id(), entity["kind"], dict(entity["params"])) for entity in entities],
    )
    return doc.log.append(
        verb,
        {
            "sketch_id": sketch_id,
            **recorded,
            "entities": [{"kind": e["kind"], "params": dict(e["params"])} for e in entities],
            "created": created,
            "approximated": sum(1 for e in entities if e.get("approximated")),
        },
    )


def project_to_sketch(doc: Document, sketch_id: str, targets: Sequence[dict]) -> Operation:
    """辺・面 `targets` をスケッチ `sketch_id` へ写した図形を足す。失敗したときは文書にもログにも何も残さない。"""
    refs = [dict(ref) for ref in targets]
    if not refs:
        raise ValueError("project_to_sketch requires at least one edge or face")
    return _add_projected(doc, VERB_PROJECT_TO_SKETCH, sketch_id, projected_entities(doc, sketch_id, refs), {"targets": refs})


def project_outline(doc: Document, sketch_id: str, body_ids: Sequence[str]) -> Operation:
    """ボディ `body_ids` の影の境界をスケッチ `sketch_id` へ足す。失敗したときは文書にもログにも何も残さない。"""
    bodies = [str(body_id) for body_id in body_ids]
    if not bodies:
        raise ValueError("project_outline requires at least one body")
    return _add_projected(doc, VERB_PROJECT_OUTLINE, sketch_id, outline_entities(doc, sketch_id, bodies), {"bodies": bodies})


def _do_project(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された図形を、記録されたIDで足し直す(ログには触らない)。"""
    args = operation.args
    sketch = get_sketch(document, args["sketch_id"])
    add_entities(
        document,
        [
            (sketch, entity_id, entity["kind"], dict(entity["params"]))
            for entity_id, entity in zip(args["created"], args["entities"])
        ],
    )


def _undo_project(document: Document, operation: Operation) -> None:
    """undo: 足した図形だけを消す。"""
    remove_entities(document, operation.args["created"], sketch_id=operation.args["sketch_id"])


register_verb(VERB_PROJECT_TO_SKETCH, do=_do_project, undo=_undo_project)
register_verb(VERB_PROJECT_OUTLINE, do=_do_project, undo=_undo_project)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
def selected_targets(document: Document) -> List[dict]:
    """選択の辺と面(確定とプレビューが同じ取り方をする)。どちらも無ければ `SelectionEmpty` / `SelectionKindMismatch`。"""
    items = [item for item in selection.selected_items(document) if selection.ref_kind(item) in ("edge", "face")]
    if not items:
        selection.require_selected(document, "edge")
    return items


def selected_bodies(document: Document) -> List[str]:
    """選択の面・辺・頂点・ボディの参照が属するボディ(最初に現れた順、重複なし)。"""
    return selection.require_bodies(document)


def _response(args: dict) -> dict:
    return {"created": list(args["created"]), "sketch_ids": [args["sketch_id"]], "approximated": args["approximated"]}


@entry(VERB_PROJECT_TO_SKETCH)
def _enter_project_to_sketch(document: Document, sketch_id: str) -> dict:
    return _response(project_to_sketch(document, sketch_id, selected_targets(document)).args)


@entry(VERB_PROJECT_OUTLINE)
def _enter_project_outline(document: Document, sketch_id: str) -> dict:
    return _response(project_outline(document, sketch_id, selected_bodies(document)).args)

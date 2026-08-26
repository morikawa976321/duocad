"""verb `import_svg` — SVGを**1枚のスケッチ**として取り込む。

`svg_import.read_svg_entities` が翻訳した図形群を、新しいスケッチへまとめて入れる。
戻り値は作った SketchId(`sketch_create` と同じく「新しいものを作るverbは新しいIDを返す」)。

**ログ1行・undo1回・MCP1呼び出しに収める**。`add_line` / `add_bezier` をN回発行する形に
してはならない —— ロゴ1枚が数百行のログになり、取り込みを取り消すのに Ctrl+Z を数百回
押すことになる(`import_step` と同じ理由)。

**作った全IDを `args` に記録する**(`sketch_id` / 各 `entity_id`)。記録が無いと再生
(redo / `open_document`)で別のIDが振られ、保存した選択集合や押し出しの参照先を失う。

**形はログの中にある**: `args["entities"]` に翻訳済みの図形をそのまま入れるので、**再生は
外部ファイルもSVGパーサも通らない**。SVG本文を埋めて再生時に読み直す形にはしていない ——
svgelementsの版が変われば同じログから別の形が出てくることになり、「保存したものを開くと
同じ形になる」が黙って壊れる。`args["source_name"]` は元のファイル名でパスではない。

**取り込んだ図形の数値は名前つきの値に登録しない**(素の数値のまま持つ)。制御点の座標は
人が打ち直す寸法ではなく、`register_value` に通すとロゴ1枚で数千個の `d1` が生まれ、
SketchPanelの図形一覧が埋まる。`parameters.resolve` は素の数値をそのまま返すので、この形は
元から通る(`sketch.Entity` のdocstring)。

失敗は黙って通さない: パス無し=FileNotFoundError / 壊れている・図形が無い・多すぎる=ValueError
/ 平面が平面でない=ValueError。いずれも `doc.sketches` にもログにも何も残さない。
"""
from __future__ import annotations

from typing import Any, List
from uuid import uuid4

from ..document import Document
from ..oplog import Operation, register_verb
from ..sketch import (
    Entity,
    Sketch,
    SketchId,
    require_valid_params,
    resolve_plane,
)
from ..svg_import import PathLike, read_svg_entities

VERB_IMPORT_SVG = "import_svg"


def import_svg(
    doc: Document,
    path: PathLike,
    plane: Any = "XY",
    scale: float = 1.0,
) -> SketchId:
    """SVGを読み、図形を全部載せた新しいスケッチを作って SketchId を返す。

    `plane` は基準平面名("XY"/"XZ"/"YZ")か `sketch.face_plane()` が返す面への参照。
    `scale` は解決後のmmへさらに掛ける倍率(既定1.0。単位の合わないSVG用)。
    """
    plane_ref = resolve_plane(doc, plane)  # 平面でない面ならここで例外、doc/logは無傷
    source_name, shapes = read_svg_entities(path, scale=scale)

    # **先に全部検査する**。半分入れてから弾くと、以後 `regions()` が毎回失敗する
    # スケッチが残り、図形の削除verbが無いのでundo以外に復旧手段が無い。
    for shape in shapes:
        require_valid_params(shape["kind"], shape["params"])

    sketch_id = f"sketch-{uuid4().hex[:12]}"
    entities = [
        {
            "entity_id": f"entity-{uuid4().hex[:12]}",
            "kind": shape["kind"],
            "params": dict(shape["params"]),
        }
        for shape in shapes
    ]
    _apply_to(doc, sketch_id, plane_ref, entities)
    doc.log.append(
        VERB_IMPORT_SVG,
        {
            "source_name": source_name,
            "scale": float(scale),
            "plane": dict(plane_ref),
            "sketch_id": sketch_id,
            "entities": entities,
        },
    )
    return sketch_id


def _apply_to(
    doc: Document, sketch_id: SketchId, plane_ref: dict, entities: List[dict]
) -> None:
    """指定IDでスケッチと図形を作る(ログには触らない)。

    **モデルとログには別のdictを渡す** —— 同じ参照を共有すると、図形を触った瞬間に
    唯一の正であるログが黙って書き換わる(`sketch._add_entity` と同じ理由)。
    """
    if any(sketch.id == sketch_id for sketch in doc.sketches):
        raise ValueError(f"sketch id already exists: {sketch_id!r}")
    sketch = Sketch(id=sketch_id, plane=dict(plane_ref))
    sketch.entities = [
        # params は**素の数値のまま**(名前つきの値へ登録しない。モジュールdocstring)。
        Entity(
            id=entity["entity_id"],
            kind=entity["kind"],
            params=dict(entity["params"]),
        )
        for entity in entities
    ]
    doc.sketches.append(sketch)


def _do_import_svg(document: Document, operation: Operation) -> None:
    """redo / replay: **ログに記録された図形**からそのまま復元する(SVGを読み直さない)。

    公開APIの `import_svg` を呼び直してはいけない(ログに二重記録される)。
    """
    args = operation.args
    _apply_to(document, args["sketch_id"], args["plane"], args["entities"])


def _undo_import_svg(document: Document, operation: Operation) -> None:
    """スケッチごと消す。**パラメータの解放は要らない** —— 取り込んだ図形は名前つきの値を
    持たないので、`release_param_refs` に渡すものが無い。"""
    sketch_id = operation.args["sketch_id"]
    document.sketches = [
        sketch for sketch in document.sketches if sketch.id != sketch_id
    ]


register_verb(VERB_IMPORT_SVG, do=_do_import_svg, undo=_undo_import_svg)

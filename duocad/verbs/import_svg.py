"""verb `import_svg` — SVGの図形を**いま開いているスケッチへ**取り込む。

`svg_import.read_svg_entities` が翻訳した図形群を、既存のスケッチ(`sketch_id`)へ足す。
CADの「アクティブなスケッチに挿入する」に相当する形で、貼る先は**呼び出し時に指定した
スケッチの平面**で決まる —— 基準平面(XY/XZ/YZ)や面を引数で受ける道は無い(2026-08-28、
「新しいスケッチを1枚作って図形を全部そこへ入れる」形から変更した。ユーザーが提示された
3案のうち「開いているスケッチへ取り込む」を選択)。スケッチの外から使いたい場面は、先に
`sketch_create` でスケッチを作ってからこのverbを呼べばよい。

**戻り値は SketchId ではない**(`sketch_create` の「新しいものを作るverbは新しいIDを返す」の
対象から外れる)。新しいスケッチを作らないので新しいIDが無い —— 代わりに、足した全
entity_id のリストを返す(`add_line` が単数の EntityId を返すのと同じ流儀の複数版)。

**ログ1行・undo1回・MCP1呼び出しに収める**。`add_line` / `add_bezier` をN回発行する形に
してはならない —— ロゴ1枚が数百行のログになり、取り込みを取り消すのに Ctrl+Z を数百回
押すことになる(`import_step` と同じ理由)。

**追加した全IDを `args` に記録する**(`sketch_id` / 各 `entity_id`)。記録が無いと再生
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
/ **貼る先のスケッチ(`sketch_id`)が実在しない=KeyError**(`sketch.get_sketch` と同じ流儀。
`add_line` 等が対象スケッチ不明で落ちるのと同じ形)。いずれの場合も `doc.sketches` にも
ログにも何も残さない(半分入れて弾かない)。
"""
from __future__ import annotations

from typing import List
from uuid import uuid4

from ..document import Document
from ..oplog import Operation, register_verb
from ..sketch import Entity, EntityId, SketchId, get_sketch, require_valid_params
from ..svg_import import PathLike, read_svg_entities

VERB_IMPORT_SVG = "import_svg"


def import_svg(
    doc: Document,
    sketch_id: SketchId,
    path: PathLike,
    scale: float = 1.0,
) -> List[EntityId]:
    """SVGを読み、**`sketch_id` が指す既存のスケッチへ**図形を足して entity_id の一覧を返す。

    `sketch_id` は実在するスケッチでなければならない(無ければ KeyError)。貼る先が
    平面かどうかの検査はもう要らない —— 既にスケッチとして存在するものへ足すだけである。
    `scale` は解決後のmmへさらに掛ける倍率(既定1.0。単位の合わないSVG用)。
    """
    get_sketch(doc, sketch_id)  # 実在しなければここで KeyError。doc/logはまだ無傷
    source_name, shapes = read_svg_entities(path, scale=scale)

    # **先に全部検査する**。半分入れてから弾くと、以後 `regions()` が毎回失敗する
    # スケッチが残り、図形の削除verbが無いのでundo以外に復旧手段が無い。
    for shape in shapes:
        require_valid_params(shape["kind"], shape["params"])

    entities = [
        {
            "entity_id": f"entity-{uuid4().hex[:12]}",
            "kind": shape["kind"],
            "params": dict(shape["params"]),
        }
        for shape in shapes
    ]
    _apply_to(doc, sketch_id, entities)
    doc.log.append(
        VERB_IMPORT_SVG,
        {
            "source_name": source_name,
            "scale": float(scale),
            "sketch_id": sketch_id,
            "entities": entities,
        },
    )
    return [entity["entity_id"] for entity in entities]


def _apply_to(doc: Document, sketch_id: SketchId, entities: List[dict]) -> None:
    """既存スケッチ `sketch_id` へ図形を足す(ログには触らない)。

    **モデルとログには別のdictを渡す** —— 同じ参照を共有すると、図形を触った瞬間に
    唯一の正であるログが黙って書き換わる(`sketch._add_entity` と同じ理由)。
    """
    sketch = get_sketch(doc, sketch_id)
    sketch.entities.extend(
        # params は**素の数値のまま**(名前つきの値へ登録しない。モジュールdocstring)。
        Entity(id=entity["entity_id"], kind=entity["kind"], params=dict(entity["params"]))
        for entity in entities
    )


def _do_import_svg(document: Document, operation: Operation) -> None:
    """redo / replay: **ログに記録された図形**からそのまま復元する(SVGを読み直さない)。

    公開APIの `import_svg` を呼び直してはいけない(ログに二重記録される)。
    """
    args = operation.args
    _apply_to(document, args["sketch_id"], args["entities"])


def _undo_import_svg(document: Document, operation: Operation) -> None:
    """このverbが足した図形だけを取り除く(スケッチごとは消さない —— スケッチは
    このverbが作ったものではなく、他の図形が残っているかもしれない)。

    **パラメータの解放は要らない** —— 取り込んだ図形は名前つきの値を持たないので、
    `release_param_refs` に渡すものが無い。
    """
    args = operation.args
    added_ids = {entity["entity_id"] for entity in args["entities"]}
    sketch = get_sketch(document, args["sketch_id"])
    sketch.entities = [
        entity for entity in sketch.entities if entity.id not in added_ids
    ]


register_verb(VERB_IMPORT_SVG, do=_do_import_svg, undo=_undo_import_svg)

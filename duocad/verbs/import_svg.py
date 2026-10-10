"""verb `import_svg` — SVGの図形を**いま開いているスケッチへ**取り込む。

`svg_import.read_svg_entities` が翻訳した図形群を、既存のスケッチ(`sketch_id`)へ足す。貼る先は
呼び出し時に指定したスケッチの平面で決まり、基準平面や面を引数で受ける道は無い(先に `sketch_create`
でスケッチを作る)。

**戻り値は SketchId ではなく、足した全 entity_id のリスト**(新しいスケッチを作らないので新しいIDが無い)。

**ログ1行・undo1回・MCP1呼び出しに収める**(`add_line` / `add_bezier` をN回発行すると、ロゴ1枚が
数百行のログになり Ctrl+Z を数百回押すことになる。`import_step` と同じ理由)。追加した全IDを
`args` に記録する(`sketch_id` / 各 `entity_id`。無いと再生で別のIDが振られ、選択集合や押し出しの
参照先を失う)。

**形はログの中にある**: `args["entities"]` に翻訳済みの図形を入れるので、再生は外部ファイルもSVGパーサも
通らない。SVG本文を埋めて再生時に読み直さないのは、svgelementsの版が変わると同じログから別の形が
出て「保存したものを開くと同じ形になる」が黙って壊れるため。`args["source_name"]` は元のファイル名で
パスではない。

**取り込んだ図形の数値は名前つきの値に登録しない**(素の数値のまま持つ)。制御点の座標は人が打ち直す
寸法ではなく、`register_value` に通すとロゴ1枚で数千個の `d1` が生まれる。`parameters.resolve` は
素の数値をそのまま返すので通る(`sketch.Entity` のdocstring)。

失敗: パス無し=FileNotFoundError / 壊れている・図形が無い・多すぎる=ValueError / 貼る先のスケッチが
実在しない=KeyError。いずれも `doc.sketches` にもログにも何も残さない。
"""
from __future__ import annotations

from typing import List, Optional

from ..document import Document
from ..oplog import Operation, register_verb
from ..entry import entry
from ..paths import resolve_user_path
from ..sketch import Entity, EntityId, SketchId, get_sketch, new_entity_id, remove_entities, require_valid_params
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

    # 先に全部検査する。半分入れてから弾くと、以後 `regions()` が毎回失敗するスケッチが残る。
    for shape in shapes:
        require_valid_params(shape["kind"], shape["params"])

    entities = [
        {
            "entity_id": new_entity_id(),
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

    取り込んだ図形は名前つきの値を持たないので、`remove_entities` が手放すものは無い(そのまま通る)。
    """
    args = operation.args
    remove_entities(document, [entity["entity_id"] for entity in args["entities"]], sketch_id=args["sketch_id"])


register_verb(VERB_IMPORT_SVG, do=_do_import_svg, undo=_undo_import_svg)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_IMPORT_SVG)
def _enter_import_svg(
    document: Document, sketch_id: str, path: PathLike, scale: Optional[float] = None
) -> dict:
    """`scale` は解決後の mm へさらに掛ける倍率(省略時 1.0)。"""
    entity_ids = import_svg(
        document,
        str(sketch_id),
        str(resolve_user_path(path)),
        scale=1.0 if scale is None else float(scale),
    )
    args = document.log.entries[-1].args
    return {
        "sketch_id": str(sketch_id),
        "entity_ids": list(entity_ids),
        "source_name": args["source_name"],
        "entity_count": len(args["entities"]),
    }

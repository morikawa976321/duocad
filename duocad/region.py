"""領域 —— **描くものではなく計算されるもの**。

スケッチの図形群から閉領域を求めるのがこのモジュールで、幾何そのもの
(`BRepAlgoAPI_Splitter`)は `duocad.kernel_sketch` が持つ(OCPを直接importできるのはカーネル層だけ)。

`regions(doc, sketch_id)` の `index` は**1始まり**の連番で、**列挙順は同じスケッチに対して
安定**であること(選択集合が添字で領域を指すため)。

**注意**: パラメータを編集すると領域は分裂・合体・消失しうる。押し出していた領域が**消えた**
場合は再生時に `ValueError` で止まる(`parameters.set_parameter` が担う)。面積順が入れ替わった
だけの場合は添字が残ったまま**指す領域が別物にすり替わり**、検出できず新しい方へ適用される
(正当な編集でも面積順は入れ替わるので、一律に失敗へするとパラメータ追随を潰す)。
解くには `RegionRef.provenance` を実際に埋める必要があるが未着手。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from . import kernel, kernel_sketch
from . import parameters
from .document import Document
from .sketch import SketchId, entity_kind_ops, get_sketch


@dataclass(frozen=True)
class Region:
    """計算された閉領域。`area` は mm^2、`centroid` は面積重心のワールド座標 [mm]。"""

    index: int
    area: float
    centroid: tuple


@dataclass
class RegionRef:
    """領域への参照(`{ sketch, index, provenance }`)。

    `provenance` は**空スロット**(値は None でよく、スロットの存在自体が契約)。
    操作ログの `args` には `to_dict()` の形(素のdict)で入れる(`OpLog.to_json()` のため)。
    """

    sketch: SketchId
    index: int
    provenance: Optional[Any] = None

    def to_dict(self) -> dict:
        """JSON化できる素のdictへ(操作ログの `selection` / `args` に入れる形)。"""
        return {
            "sketch": self.sketch,
            "index": int(self.index),
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RegionRef":
        """`to_dict()` の形から復元する(再生時にログの selection を読み直すため)。"""
        return cls(
            sketch=data["sketch"],
            index=int(data["index"]),
            provenance=data.get("provenance"),
        )


def region_ref(sketch_id: SketchId, index: int) -> RegionRef:
    """`regions()` が返した領域の添字から参照を作る。"""
    return RegionRef(sketch=sketch_id, index=int(index))


def regions(doc: Document, sketch_id: SketchId) -> List[Region]:
    """スケッチの閉領域を計算して返す(1始まりの連番・順序は安定)。

    図形が1つも無ければ空リスト。実装は `BRepAlgoAPI_Splitter` で平面状の面をスケッチの辺で
    分割する(docs/DESIGN_DECISIONS.md「領域は計算されるもの」)。
    """
    return [
        Region(index=item["index"], area=item["area"], centroid=tuple(item["centroid"]))
        for item in _regions_detailed(doc, sketch_id)
    ]


def expanded_entities(doc: Document, sketch_id: SketchId) -> List[dict]:
    """パラメータを解決し、反復を展開した**画面に描くべき図形の全量**(素の図形リスト)。

    アプリ層が描くものと領域の計算が食うものを同じにするため公開している
    (別々に組むと、反復した図形が画面に出ないのに領域だけ増える)。

    反復に参照されている図形は単独では出さない(格子の (0,0) / 円形の i=0 が元の位置)。
    kernel は名前を知らないので、ここですべて素の数値にする。

    **反復の元が文書に無ければ、その元は展開しない**。verb `delete` は依存グラフを持たず
    元の図形も消せるので、`by_id[source_id]` が KeyError になると状態を配るたびに落ちる
    (`DocumentView.sketches_state` が毎回通る)。undoで元が戻れば反復も戻る。
    反復そのものを消したり `sources` を書き換えたりはしない。
    """
    sketch = get_sketch(doc, sketch_id)
    patterned = {
        source
        for pattern in sketch.patterns
        for source in pattern.sources
    }
    entities: List[dict] = []

    for entity in sketch.entities:
        if entity.id in patterned:
            continue
        resolved = parameters.resolve_all(doc, entity.params)
        entities.append({"kind": entity.kind, "params": resolved})

    by_id = {entity.id: entity for entity in sketch.entities}
    for pattern in sketch.patterns:
        if pattern.kind == "circular":
            cx = parameters.resolve(doc, pattern.params["cx"])
            cy = parameters.resolve(doc, pattern.params["cy"])
            count = int(parameters.resolve(doc, pattern.params["count"]))
            total_angle = parameters.resolve(doc, pattern.params["total_angle"])
            rotate_copies = bool(pattern.options.get("rotate_copies", True))
            # count==1 は total_angle/(count-1) の0除算を避ける(場合分けは `pattern_circular` のdocstring参照)。
            if count == 1:
                step = 0.0
            elif total_angle == 360:
                step = 360.0 / count
            else:
                step = total_angle / (count - 1)
            for source_id in pattern.sources:
                entity = by_id.get(source_id)
                if entity is None:  # 元が消えている(docstring)
                    continue
                base = parameters.resolve_all(doc, entity.params)
                for i in range(count):
                    angle = i * step
                    entities.append({
                        "kind": entity.kind,
                        "params": entity_kind_ops(entity.kind).rotate(
                            base, cx, cy, angle, rotate_copies
                        ),
                    })
            continue

        dx = parameters.resolve(doc, pattern.params["dx"])
        dy = parameters.resolve(doc, pattern.params["dy"])
        nx = int(parameters.resolve(doc, pattern.params["nx"]))
        ny = int(parameters.resolve(doc, pattern.params["ny"]))
        for source_id in pattern.sources:
            entity = by_id.get(source_id)
            if entity is None:  # 元が消えている(docstring)
                continue
            base = parameters.resolve_all(doc, entity.params)
            for j in range(ny):
                for i in range(nx):
                    entities.append(
                        {
                            "kind": entity.kind,
                            "params": entity_kind_ops(entity.kind).translate(base, i * dx, j * dy),
                        }
                    )
    return entities


def plane_frame(doc: Document, sketch_id: SketchId) -> dict:
    """スケッチ平面の3Dの枠(`kernel_sketch.sketch_plane_frame`)。

    同じ計算をアプリ層に持たせない(基準3面の軸の取り方と、面に貼ったときの法線反転の扱いが
    ずれる)。

    **面に貼ったスケッチでも `body.shape` を見ない**。姿勢はスケッチ作成時に
    `sketch.resolve_plane` が `plane["frame"]` へ焼き付けており、ここは読むだけ
    (`kernel_sketch.face_frame` のdocstring)。
    """
    sketch = get_sketch(doc, sketch_id)
    return kernel_sketch.sketch_plane_frame(sketch.plane)


def regions_with_geometry(doc: Document, sketch_id: SketchId, deviation: float) -> List[dict]:
    """領域を**画面へ描ける三角形つき**で返す。

    戻り値: [{"index", "area", "centroid", "positions", "indices"}, ...]

    アプリ層で図形から領域を組み直すと、コアが選択集合で指している添字と食い違う
    (順序は面積とu,vで決まる。`kernel_sketch.sketch_regions`)。**描くものも添字もコアが配る**。
    """
    out: List[dict] = []
    for item in _regions_detailed(doc, sketch_id):
        mesh = kernel.tessellate(item["face"], deviation)
        first = mesh["faces"][0] if mesh["faces"] else None
        out.append(
            {
                "index": item["index"],
                "area": item["area"],
                "centroid": item["centroid"],
                "positions": first["positions"] if first else [],
                "indices": first["indices"] if first else [],
            }
        )
    return out


def _regions_detailed(doc: Document, sketch_id: SketchId) -> List[dict]:
    """`kernel_sketch.sketch_regions` の結果をそのまま返す(面つき。`verbs/extrude.py` が使う)。

    **verb層と共有する内部API**。`regions()`(公開API)は面を落として `Region` だけを返す
    (押し出しには `TopoDS_Face` が要るが、選択集合や操作ログへ出てよいのは添字と面積だけ)。
    """
    sketch = get_sketch(doc, sketch_id)
    if not sketch.entities:
        return []
    entities = expanded_entities(doc, sketch_id)
    if not entities:
        return []
    # 面に貼ったスケッチでも `TopoDS_Face` は渡さない。使うのは無限平面としての姿勢だけで、
    # それは `plane["frame"]` が持つ(`plane_frame` のdocstring)。
    return kernel_sketch.sketch_regions(sketch.plane, entities)

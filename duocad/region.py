"""領域 —— **描くものではなく計算されるもの**。

スケッチの図形群から閉領域を求めるのがこのモジュールで、幾何そのもの
(`BRepAlgoAPI_Splitter`)は `duocad.kernel` が持つ(OCPを直接importできるのは kernel.py だけ)。

`regions(doc, sketch_id)` の `index` は**1始まり**の連番で、**列挙順は同じスケッチに対して
安定**であること(2回評価して同じ順序。選択集合が添字で領域を指す以上、順序が揺れると選択が
別物を指す)。

**注意**: パラメータを編集すると領域は分裂・合体・消失しうる。押し出していた領域が消える
壊れ方をするので、黙って通さないこと(`parameters.set_parameter` が担う)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from . import kernel
from . import parameters
from .document import Document
from .sketch import SketchId, entity_kind_ops, get_sketch


@dataclass(frozen=True)
class Region:
    """計算された閉領域。`area` は mm^2。"""

    index: int
    area: float


@dataclass
class RegionRef:
    """領域への参照(`{ sketch, index, provenance }`)。

    `provenance` は**空スロット**(値は None でよく、スロットの存在自体が契約)。領域は図形の
    配置から計算されるので、線を1本動かしただけで添字の指す領域が入れ替わりうる。

    操作ログの `args` には `to_dict()` の形(素のdict)で入れる —— `Operation` は
    `OpLog.to_json()` でJSONにできなければならない。
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

    図形が1つも無ければ空リスト。実装は `BRepAlgoAPI_Splitter` で平面状の面を
    スケッチの辺で分割する(実装計画書§3.3。実測で成立を確認済み)。
    """
    return [
        Region(index=item["index"], area=item["area"])
        for item in _regions_detailed(doc, sketch_id)
    ]


def _expanded_entities(doc: Document, sketch_id: SketchId) -> List[dict]:
    """パラメータを解決し、反復を展開した素の図形リストを返す。

    反復に参照されている図形は単独では出さない(格子の (0,0) / 円形の i=0 が元の位置)。
    kernel は名前を知らないので、ここですべて素の数値にする。

    **反復の元が文書に無ければ、その元は展開しない**(2026-09-03)。verb `delete` は
    図形どうしの依存グラフを持たないので、反復の元になっている図形も普通に消せる ——
    ここで `by_id[source_id]` と引くと KeyError になり、**状態を配るたびに落ちる**
    (`DocumentView.sketches_state` が毎回この関数を通る)ので、アプリごと使えなくなる。
    消えた元は展開されなくなるだけで、undoで元が戻れば反復も元どおり展開される。
    **反復そのものを消したり `sources` を書き換えたりはしない** —— それは undo で
    戻すべき状態を1つ増やすことになり、依存の作り込みを始めることになる。
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
        resolved = {
            key: parameters.resolve(doc, ref)
            for key, ref in entity.params.items()
        }
        entities.append({"kind": entity.kind, "params": resolved})

    by_id = {entity.id: entity for entity in sketch.entities}
    for pattern in sketch.patterns:
        if pattern.kind == "circular":
            cx = parameters.resolve(doc, pattern.params["cx"])
            cy = parameters.resolve(doc, pattern.params["cy"])
            count = int(parameters.resolve(doc, pattern.params["count"]))
            total_angle = parameters.resolve(doc, pattern.params["total_angle"])
            rotate_copies = bool(pattern.options.get("rotate_copies", True))
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
                base = {
                    key: parameters.resolve(doc, ref)
                    for key, ref in entity.params.items()
                }
                for i in range(count):
                    angle = i * step
                    # `rotate` は種別横断で `[{"kind":.., "params":..}, ...]` を返す
                    # (長方形の rotate_copies=True は4本の線分になり種別が変わるため。
                    # `duocad.sketch.EntityKindOps.rotate` のdocstring)。
                    entities.extend(
                        entity_kind_ops(entity.kind).rotate(base, cx, cy, angle, rotate_copies)
                    )
            continue

        dx = parameters.resolve(doc, pattern.params["dx"])
        dy = parameters.resolve(doc, pattern.params["dy"])
        nx = int(parameters.resolve(doc, pattern.params["nx"]))
        ny = int(parameters.resolve(doc, pattern.params["ny"]))
        for source_id in pattern.sources:
            entity = by_id.get(source_id)
            if entity is None:  # 元が消えている(docstring)
                continue
            base = {
                key: parameters.resolve(doc, ref)
                for key, ref in entity.params.items()
            }
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
    """スケッチ平面の3Dの枠(`kernel.sketch_plane_frame`)。

    アプリ層が「画面のクリック → 平面上の (u, v)」と「図形 → 3Dの線」を行き来するために
    要る。**同じ計算をアプリ層に持たせない**(基準3面の軸の取り方と、面に貼ったときの
    法線反転の扱いが必ずずれる)。

    **面に貼ったスケッチでも `body.shape` を見ない**。姿勢はスケッチ作成時に
    `sketch.resolve_plane` が `plane["frame"]` へ焼き付けており、ここはそれを読むだけである
    (`kernel.face_frame` のdocstring)。ライブの面から引き直していた頃は、スケッチと無関係な
    フィレット1つで添字の指す先が円錐面へずれ、スケッチが丸ごと描けなくなっていた。
    """
    sketch = get_sketch(doc, sketch_id)
    return kernel.sketch_plane_frame(sketch.plane)


def expanded_entities(doc: Document, sketch_id: SketchId) -> List[dict]:
    """反復を展開しパラメータを解決した、**画面に描くべき図形の全量**。

    公開しているのは、アプリ層が描くものと領域の計算が食うものを同じにするため
    (別々に組み立てると、反復した図形が画面に出ないのに領域だけ増える、が起きる)。
    """
    return _expanded_entities(doc, sketch_id)


def regions_with_geometry(doc: Document, sketch_id: SketchId, deviation: float) -> List[dict]:
    """領域を**画面へ描ける三角形つき**で返す。

    戻り値: [{"index", "area", "centroid", "positions", "indices"}, ...]

    領域はスケッチ平面上の面なので、実体(`TopoDS_Face`)を持っているのはコア側だけ。
    アプリ層で図形から領域を組み直すと、コアが選択集合で指している添字と食い違う
    (領域の順序は面積とu,vで決まる。`kernel.sketch_regions`)。**描くものも添字も
    コアが配る**。
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
    """`kernel.sketch_regions` の結果をそのまま返す(面つき。`verbs/extrude.py` が使う)。

    **verb層と共有する内部API**であり、`regions()`(公開API)は面を落として `Region` だけを
    返す。押し出しには領域の `TopoDS_Face` そのものが要る一方、選択集合や操作ログへ
    出てよいのは添字と面積だけ、という非対称をこの2段構えで表している。
    """
    sketch = get_sketch(doc, sketch_id)
    if not sketch.entities:
        return []
    entities = _expanded_entities(doc, sketch_id)
    if not entities:
        return []
    # 面に貼ったスケッチでも実際の面(`TopoDS_Face`)は渡さない —— `sketch_regions` が面から
    # 取っていたのは**無限平面としての姿勢だけ**で、トリムされた境界は使っていない。姿勢は
    # `plane["frame"]` が持っている(`plane_frame` のdocstring)。
    return kernel.sketch_regions(sketch.plane, entities)

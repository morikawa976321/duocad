"""領域(実装計画書 §3.3「スケッチと領域」・§3.5 選択集合)。M2で追加。

**領域は描くものではなく計算されるもの**。スケッチの図形群から閉領域を求めるのがこのモジュールで、
幾何そのもの(`BRepAlgoAPI_Splitter`)は `duocad.kernel` が持つ(OCPを直接importできるのは
kernel.py だけ。tests/test_boundaries.py が検査する)。

契約:
    regions(doc, sketch_id) -> list[Region]
        スケッチの閉領域。`index` は**1始まり**で連番(面の添字と同じ流儀。kernel._face_map)。
        **列挙順は同じスケッチに対して安定**であること(2回評価して同じ順序。
        選択集合が添字で領域を指す以上、順序が揺れると選択が別物を指す)。
    region_ref(sketch_id, index) -> RegionRef
        選択集合・操作ログへ入れる参照(§3.5)。

**注意(実装計画書§7・§3.3)**: パラメータを編集すると領域は分裂・合体・消失しうる。
押し出していた領域が消える壊れ方をするので、黙って通さないこと(M3の対象)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import kernel
from . import parameters
from .document import Document
from .sketch import SketchId, get_sketch


@dataclass(frozen=True)
class Region:
    """計算された閉領域。実装計画書§3.3の `Region : { index, area }` そのもの。

    `area` は mm^2(Document.units が常に "mm" であることに対応)。
    """

    index: int
    area: float


@dataclass
class RegionRef:
    """領域への参照(実装計画書§3.5 `RegionRef : { sketch, index, provenance: null }`)。

    `provenance` は **day1で用意する空スロット**(§6予防2)。値は None でよく、
    **スロットの存在自体が契約**。由来スロットの適用先は3つ — 面/辺の添字・スケッチを
    貼った面・**領域の同一性** — で、これがその3つ目。領域は図形の配置から計算されるので、
    線を1本動かしただけで添字の指す領域が入れ替わりうる(§7「領域はパラメータ編集で変わる」)。

    操作ログには `to_dict()` の形(素のdict)で入れる — `Operation.selection` は
    `OpLog.to_json()` でJSONにできなければならない(§3.6)。
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


def _translate_params(
    kind: str, params: Dict[str, float], dx: float, dy: float
) -> Dict[str, float]:
    """図形の2D params を (dx, dy) だけ平行移動したコピーを返す。"""
    moved = dict(params)
    if kind == "line":
        moved["x0"] = params["x0"] + dx
        moved["x1"] = params["x1"] + dx
        moved["y0"] = params["y0"] + dy
        moved["y1"] = params["y1"] + dy
    elif kind == "circle":
        moved["cx"] = params["cx"] + dx
        moved["cy"] = params["cy"] + dy
    elif kind == "arc":
        moved["cx"] = params["cx"] + dx
        moved["cy"] = params["cy"] + dy
    elif kind == "rectangle":
        moved["x"] = params["x"] + dx
        moved["y"] = params["y"] + dy
    else:
        raise ValueError(f"unknown entity kind: {kind!r}")
    return moved


def _expanded_entities(doc: Document, sketch_id: SketchId) -> List[dict]:
    """パラメータを解決し、矩形反復を nx×ny に展開した素の図形リストを返す。

    反復に参照されている図形は単独では出さない(格子の (0,0) が元の位置)。
    kernel は名前を知らないので、ここですべて素の数値にする。
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

    for pattern in sketch.patterns:
        dx = parameters.resolve(doc, pattern.params["dx"])
        dy = parameters.resolve(doc, pattern.params["dy"])
        nx = int(parameters.resolve(doc, pattern.params["nx"]))
        ny = int(parameters.resolve(doc, pattern.params["ny"]))
        by_id = {entity.id: entity for entity in sketch.entities}
        for source_id in pattern.sources:
            entity = by_id[source_id]
            base = {
                key: parameters.resolve(doc, ref)
                for key, ref in entity.params.items()
            }
            for j in range(ny):
                for i in range(nx):
                    entities.append(
                        {
                            "kind": entity.kind,
                            "params": _translate_params(
                                entity.kind, base, i * dx, j * dy
                            ),
                        }
                    )
    return entities


def _regions_detailed(doc: Document, sketch_id: SketchId) -> List[dict]:
    """`kernel.sketch_regions` の結果をそのまま返す(面つき。`verbs/extrude.py` が使う)。

    **verb層と共有する内部API**であり、`regions()`(公開API)は面を落として `Region` だけを
    返す。押し出しには領域の `TopoDS_Face` そのものが要る一方、選択集合や操作ログへ
    出てよいのは添字と面積だけ、という非対称をこの2段構えで表している。
    """
    sketch = get_sketch(doc, sketch_id)
    if not sketch.entities:
        return []
    face = None
    if sketch.plane.get("kind") == "face":
        body_id = sketch.plane["body"]
        body = next((candidate for candidate in doc.bodies if candidate.id == body_id), None)
        if body is None:
            raise KeyError(f"no such body: {body_id!r}")
        if body.shape is None:
            raise ValueError(f"body {body_id!r} has no shape")
        face = kernel.face_of(body.shape, int(sketch.plane["index"]))
    entities = _expanded_entities(doc, sketch_id)
    if not entities:
        return []
    return kernel.sketch_regions(sketch.plane, entities, face=face)

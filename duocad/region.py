"""領域(実装計画書 §3.3「スケッチと領域」・§3.5 選択集合)。M2で追加。

**領域は描くものではなく計算されるもの**。スケッチの図形群から閉領域を求めるのがこのモジュールで、
幾何そのもの(`BRepAlgoAPI_Splitter`)は `duocad.kernel` が持つ(OCPを直接importできるのは
kernel.py だけ。tests/test_boundaries.py が検査する)。

**M2のスタブ段階(2026-08-11)**: `Region` / `RegionRef` のデータモデルは実装済み、
`regions()` は `NotImplementedError`。

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
from typing import Any, List, Optional

from .document import Document
from .sketch import SketchId


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
    raise NotImplementedError("M2で実装する: regions")

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

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

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
        for item in _detected(doc, sketch_id)
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


def regions_with_geometry(
    doc: Document, sketch_id: SketchId, deviation: float, on_error: Optional[Callable[[int, Exception], None]] = None
) -> List[dict]:
    """領域を**画面へ描ける三角形つき**で返す。

    戻り値: [{"index", "area", "centroid", "positions", "indices"}, ...]

    アプリ層で図形から領域を組み直すと、コアが選択集合で指している添字と食い違う
    (順序は面積とu,vで決まる。`kernel_sketch.sketch_regions`)。**描くものも添字もコアが配る**。

    **三角形にできない領域があっても、ほかの領域を落とさない**: `on_error(領域の添字, 例外)` を渡すと、その領域は
    三角形が空のまま一覧に残し(添字と面積は有効)、理由を `on_error` へ渡す。渡さなければ例外をそのまま上げる。
    """
    out: List[dict] = []
    for item in _regions_detailed(doc, sketch_id):
        try:
            mesh = kernel.tessellate(item["face"], deviation)
        except Exception as error:  # noqa: BLE001  OCCT 由来の失敗は型で絞れない。領域ごとに隔離する
            if on_error is None:
                raise
            on_error(item["index"], error)
            mesh = {"faces": []}
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


#: 領域検出(`kernel_sketch.sketch_regions`)の結果を、入力そのもの(姿勢と展開した図形)の署名で引く表。
#: 検出は `BRepAlgoAPI_Splitter` で、長方形 150 個(線 600 本)のスケッチで 1 回 39 ms(初回 200 ms)かかり、
#: 選択の範囲検査(`selection.select_region` は参照 1 件ごと)・配信・押し出しが同じスケッチに何度も呼ぶ。
#: 署名(`json.dumps`)は同じスケッチで 0.2 ms。入力が同じなら結果も同じ(決定的)なので、文書をまたいで共有してよい。
#: 中の `face` は不変の形(差し替えはあっても書き換えは無い。`duocad.speculative` の docstring)。
_DETECTED: "OrderedDict[str, List[dict]]" = OrderedDict()
#: 表に残す件数。超えたら古いものから捨てる(編集中は図形を足すたびに署名が変わるので、寿命の管理が要る)。
_DETECTED_LIMIT = 32
#: アプリ内AI・形状最適化のスレッドからも読まれうるので、表の出し入れだけを直列にする。
_DETECTED_LOCK = threading.Lock()


def _regions_detailed(doc: Document, sketch_id: SketchId) -> List[dict]:
    """`kernel_sketch.sketch_regions` の結果を返す(面つき。`verbs/extrude.py` が使う)。同じ入力なら検出し直さない(`_DETECTED`)。

    **verb層と共有する内部API**。`regions()`(公開API)は面を落として `Region` だけを返す
    (押し出しには `TopoDS_Face` が要るが、選択集合や操作ログへ出てよいのは添字と面積だけ)。
    返すリスト・辞書・重心・面は呼ぶたびに新しい(面は写し。理由は `kernel_sketch.copy_face`)。
    """
    return [
        dict(item, centroid=list(item["centroid"]), face=kernel_sketch.copy_face(item["face"]))
        for item in _detected(doc, sketch_id)
    ]


def _detected(doc: Document, sketch_id: SketchId) -> List[dict]:
    """表(`_DETECTED`)が持つ検出結果そのもの。**面を外へ出さない**呼び手(添字・面積・重心だけを読む)専用。"""
    sketch = get_sketch(doc, sketch_id)
    if not sketch.entities:
        return []
    entities = expanded_entities(doc, sketch_id)
    if not entities:
        return []
    signature = json.dumps([sketch.plane, entities], sort_keys=True, default=str)
    with _DETECTED_LOCK:
        cached = _DETECTED.get(signature)
        if cached is not None:
            _DETECTED.move_to_end(signature)
    if cached is None:
        # 面に貼ったスケッチでも `TopoDS_Face` は渡さない。使うのは無限平面としての姿勢だけで、
        # それは `plane["frame"]` が持つ(`plane_frame` のdocstring)。
        cached = kernel_sketch.sketch_regions(sketch.plane, entities)
        with _DETECTED_LOCK:
            _DETECTED[signature] = cached
            while len(_DETECTED) > _DETECTED_LIMIT:
                _DETECTED.popitem(last=False)
    return cached

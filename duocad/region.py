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
import math
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


def _rotate_point(
    x: float, y: float, cx: float, cy: float, angle_deg: float
) -> tuple[float, float]:
    """点 (x, y) を中心 (cx, cy) まわりに angle_deg 度だけ反時計回りに回す。"""
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    dx = x - cx
    dy = y - cy
    return (cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a)


def _rotate_params(
    kind: str,
    params: Dict[str, float],
    cx: float,
    cy: float,
    angle_deg: float,
    rotate_copies: bool,
) -> Dict[str, float]:
    """図形の2D params を中心まわりに回したコピーを返す(種別は変えない)。

    `rotate_copies=False` のときは位置だけ回し、向きは元のまま
    (図形の中心を回して、その差分だけ平行移動する)。
    長方形の `rotate_copies=True` は軸平行で表せないので、呼び出し側で
    `_rectangle_as_rotated_lines` へ落とすこと。
    """
    if kind == "circle":
        nx, ny = _rotate_point(params["cx"], params["cy"], cx, cy, angle_deg)
        return {"cx": nx, "cy": ny, "radius": params["radius"]}
    if kind == "arc":
        nx, ny = _rotate_point(params["cx"], params["cy"], cx, cy, angle_deg)
        start_deg = params["start_deg"]
        end_deg = params["end_deg"]
        if rotate_copies:
            start_deg = start_deg + angle_deg
            end_deg = end_deg + angle_deg
        return {
            "cx": nx,
            "cy": ny,
            "radius": params["radius"],
            "start_deg": start_deg,
            "end_deg": end_deg,
        }
    if kind == "line":
        if rotate_copies:
            x0, y0 = _rotate_point(params["x0"], params["y0"], cx, cy, angle_deg)
            x1, y1 = _rotate_point(params["x1"], params["y1"], cx, cy, angle_deg)
            return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
        mx = (params["x0"] + params["x1"]) / 2.0
        my = (params["y0"] + params["y1"]) / 2.0
        nmx, nmy = _rotate_point(mx, my, cx, cy, angle_deg)
        return _translate_params(kind, params, nmx - mx, nmy - my)
    if kind == "rectangle":
        ecx = params["x"] + params["width"] / 2.0
        ecy = params["y"] + params["height"] / 2.0
        ncx, ncy = _rotate_point(ecx, ecy, cx, cy, angle_deg)
        return _translate_params(kind, params, ncx - ecx, ncy - ecy)
    raise ValueError(f"unknown entity kind: {kind!r}")


def _rectangle_as_rotated_lines(
    params: Dict[str, float], cx: float, cy: float, angle_deg: float
) -> List[dict]:
    """軸平行の長方形を、4隅を回した4本の線分へ落とす。"""
    x = params["x"]
    y = params["y"]
    width = params["width"]
    height = params["height"]
    corners = [
        (x, y),
        (x + width, y),
        (x + width, y + height),
        (x, y + height),
    ]
    rotated = [_rotate_point(px, py, cx, cy, angle_deg) for px, py in corners]
    lines: List[dict] = []
    for index in range(4):
        x0, y0 = rotated[index]
        x1, y1 = rotated[(index + 1) % 4]
        lines.append(
            {
                "kind": "line",
                "params": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
            }
        )
    return lines


def _expanded_entities(doc: Document, sketch_id: SketchId) -> List[dict]:
    """パラメータを解決し、反復を展開した素の図形リストを返す。

    反復に参照されている図形は単独では出さない(格子の (0,0) / 円形の i=0 が元の位置)。
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
                entity = by_id[source_id]
                base = {
                    key: parameters.resolve(doc, ref)
                    for key, ref in entity.params.items()
                }
                for i in range(count):
                    angle = i * step
                    if entity.kind == "rectangle" and rotate_copies:
                        entities.extend(
                            _rectangle_as_rotated_lines(base, cx, cy, angle)
                        )
                    else:
                        entities.append(
                            {
                                "kind": entity.kind,
                                "params": _rotate_params(
                                    entity.kind,
                                    base,
                                    cx,
                                    cy,
                                    angle,
                                    rotate_copies,
                                ),
                            }
                        )
            continue

        dx = parameters.resolve(doc, pattern.params["dx"])
        dy = parameters.resolve(doc, pattern.params["dy"])
        nx = int(parameters.resolve(doc, pattern.params["nx"]))
        ny = int(parameters.resolve(doc, pattern.params["ny"]))
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


def plane_face(doc: Document, sketch_id: SketchId) -> Any:
    """スケッチを貼った面(`TopoDS_Face`)。基準平面に貼ったスケッチなら None。

    面に貼ったスケッチは**平面の枠を実際の面から取る**必要がある(基準3面と違って
    位置も軸も文書の中身次第)。領域の計算とアプリ層への配信の両方が要るので、
    `_regions_detailed` の中から切り出した。
    """
    sketch = get_sketch(doc, sketch_id)
    if sketch.plane.get("kind") != "face":
        return None
    body_id = sketch.plane["body"]
    body = next((candidate for candidate in doc.bodies if candidate.id == body_id), None)
    if body is None:
        raise KeyError(f"no such body: {body_id!r}")
    if body.shape is None:
        raise ValueError(f"body {body_id!r} has no shape")
    return kernel.face_of(body.shape, int(sketch.plane["index"]))


def plane_frame(doc: Document, sketch_id: SketchId) -> dict:
    """スケッチ平面の3Dの枠(`kernel.sketch_plane_frame`)。C(2026-08-13)で追加。

    アプリ層が「画面のクリック → 平面上の (u, v)」と「図形 → 3Dの線」を行き来するために
    要る。**同じ計算をアプリ層に持たせない**(基準3面の軸の取り方と、面に貼ったときの
    法線反転の扱いが必ずずれる)。
    """
    sketch = get_sketch(doc, sketch_id)
    return kernel.sketch_plane_frame(sketch.plane, face=plane_face(doc, sketch_id))


def expanded_entities(doc: Document, sketch_id: SketchId) -> List[dict]:
    """反復を展開しパラメータを解決した、**画面に描くべき図形の全量**。

    公開しているのは、アプリ層が描くものと領域の計算が食うものを同じにするため
    (別々に組み立てると、反復した図形が画面に出ないのに領域だけ増える、が起きる)。
    """
    return _expanded_entities(doc, sketch_id)


def regions_with_geometry(doc: Document, sketch_id: SketchId, deviation: float) -> List[dict]:
    """領域を**画面へ描ける三角形つき**で返す(C・2026-08-13)。

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
    face = plane_face(doc, sketch_id)
    entities = _expanded_entities(doc, sketch_id)
    if not entities:
        return []
    return kernel.sketch_regions(sketch.plane, entities, face=face)

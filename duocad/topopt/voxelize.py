"""B-repソリッド(OCCT)から、トポロジー最適化の設計空間になる規則格子を起こす。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_voxelize.py   # 受入検査

これは `duocad.topopt` の **stage1**。担当は「形 → 格子」だけで、境界条件を面に紐づける
処理(stage2「面アンカー」)は入っていない。ただし**そのための材料は捨てない** —
`TriangleSoup` は三角形ごとに OCCT の面添字(1始まり)を持ち、`Grid` は原点と要素サイズを
持つので、stage2 は「格子の要素 ↔ ワールド座標 ↔ 面」を本モジュールだけで往復できる。

## 規約(ここを間違えると後段が全部ずれる)

- **セル中心規約**: `mask[i, j, k]` は要素 (i, j, k) の**中心**の値であり、頂点の値ではない。
  中心のワールド座標は `origin + spacing * (i + 0.5, j + 0.5, k + 0.5)`。
  `mesh.py` の `rho[ex]` と同じ規約で、`run_marching_cubes` が半セルのオフセットを
  引いているのはこの規約に戻すため(engine `marching-cubes-cell-center-half-offset`)。
  **新しく格子と実座標を対応づけるコードを書くときは、必ず `Grid.center_of` /
  `Grid.centers_1d` を通すこと。** 添字に `spacing` を直接掛けると半セルずれる。
- **要素は立方体・ワールド軸に整列**する。SIMPは全要素が同形状であることに依存していて
  要素剛性行列 `Ke` を1つ作って使い回すので、非立方体の要素は入れられない。
- 配列の軸順は `(nx, ny, nz)` で、`simp.py` の `rho` (nelx, nely, nelz) とそのまま合う。

## 占有判定のやり方(なぜこの形か)

`kernel.tessellate` が返す三角形群に対して、各列 (i, j) から **+z 方向のレイ**を飛ばし、
交点の z を集めて内部区間を塗る。新規依存はゼロで、numpy だけで書ける。

- **偶奇(parity)ではなく巻き数(winding)で数える。** レイが2枚の三角形の共有辺を
  ちょうど通ると、同じ z に交点が2つ出る。偶奇だとその上の全域で内外が反転して
  列がまるごと壊れるが、巻き数なら「入る/出る」の符号が相殺して壊れない。
  符号は三角形の xy 投影の符号付き面積 = 外向き法線の z 成分の符号から取る。
- **レイの (x, y) をセル中心から `RAY_JITTER` 倍(要素サイズ比)だけ決定的にずらす。**
  格子は bbox の中心から起こすので、軸に整列した箱では面や辺が列の中心と厳密に一致する
  (縮退が偶然ではなく系統的に起きる)。ずらし幅は要素サイズの 1e-4 程度で、
  受入検査が「曖昧」として除外する境界近傍の帯(要素サイズの数%)よりはるかに小さい。
  **乱数ではないので決定性は保たれる。**
- `BRepClass3d_SolidClassifier` は**本番経路では使わない**。1点ずつしか判定できず
  100³=100万点をPythonループで回すのは非現実的。受入検査の神託としてだけ使う。

## 黙って間違った格子を返さないための検査(`inspect_soup`)

1. 三角形が取れなかった面がある(`untriangulated`) → 穴の空いた表面になる
2. 有向辺の対が揃わない → 非水密(開いたシェル)、または向きが不整合
3. 符号付き体積が正でない → 法線が内向き(裏返った形状)

いずれも `ValueError` にする。ここを通さないと、開いたシェルは「レイが1回しか交差しない
列は全部内部」という**もっともらしいゴミ**を返す。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import numpy as np

from .. import kernel

# レイを列の中心からずらす幅(要素サイズに対する比)。軸整列した形状で縮退が系統的に
# 起きるのを避けるためのもので、乱数ではない。2軸で違う無理数比にして、両軸が同時に
# 縮退する形状(例: 対角線上の辺)も外す。
RAY_JITTER_X = 1.0e-4
RAY_JITTER_Y = 0.6180339887498949e-4

# **1つの固定値では守れない。** ずらし幅を固定にすると、その値にちょうど合った位置に
# 頂点が来る格子を渡されたときに縮退する(円錐の頂点へ既定のずらしを
# 合わせた格子を作れば再現する)。縮退(巻き数が0に戻らない列)を検知したら、**決定的な列**を
# 順に試し直す。倍率を素数にしてあるのは、ある倍率で当たった位置が別の倍率で当たり直さない
# ようにするため。乱数は使わない(決定性を保つ)。
RAY_JITTER_SEQUENCE: tuple[tuple[float, float], ...] = tuple(
    (RAY_JITTER_X * k, RAY_JITTER_Y * m)
    for k, m in ((1, 1), (3, 5), (7, 11), (13, 17), (19, 23), (29, 31))
)

# 既定の三角形分割の細かさ。**要素サイズに比例させる**(格子を細かくすると三角形も
# 細かくなる)。0.02 は「格子の誤差が支配的で、三角形分割の弦誤差はその20分の1」という
# 配分。受入検査が曖昧として除外する帯(要素サイズの5%)より細かいことが要る。
DEFAULT_DEVIATION_RATIO = 0.02
DEFAULT_ANGULAR_DEFLECTION = 0.2

# バウンディングボックスの周囲に取る余白(要素数)。
DEFAULT_PAD = 1

# **セル中心規約の唯一の定義**。要素 (i,j,k) の値は中心 origin + spacing*(i+CELL_CENTER_OFFSET)
# のもの。`mesh.py` の `rho[ex]` と同じで、半セルずれの原因になるのでここ以外に書かない
# (変異検査で、同じ 0.5 が3箇所に散っていて1箇所を壊しても検査が緑のままだったことがある)。
CELL_CENTER_OFFSET = 0.5


# ---------------------------------------------------------------------------
# 格子
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Grid:
    """立方体要素・ワールド軸整列の規則格子。

    `origin` は**要素 (0,0,0) の低い側の角**のワールド座標(中心ではない)。
    要素 (i,j,k) の中心は `origin + spacing * (i+0.5, j+0.5, k+0.5)`。
    """

    origin: tuple[float, float, float]
    spacing: float
    shape: tuple[int, int, int]

    def __post_init__(self) -> None:
        if not self.spacing > 0:
            raise ValueError(f"spacing must be positive, got {self.spacing!r}")
        if len(self.shape) != 3 or any(int(n) < 1 for n in self.shape):
            raise ValueError(f"shape must be 3 positive ints, got {self.shape!r}")

    @property
    def element_count(self) -> int:
        nx, ny, nz = self.shape
        return int(nx) * int(ny) * int(nz)

    @property
    def element_volume(self) -> float:
        return float(self.spacing) ** 3

    def centers_1d(self, axis: int) -> np.ndarray:
        """軸 `axis` のセル中心座標(長さ = その軸の要素数)。"""
        n = self.shape[axis]
        return self.origin[axis] + self.spacing * (
            np.arange(n, dtype=float) + CELL_CENTER_OFFSET
        )

    def center_of(self, i: int, j: int, k: int) -> np.ndarray:
        """要素 (i,j,k) の中心のワールド座標。"""
        return self.centers_of(np.array([[i, j, k]]))[0]

    def centers_of(self, indices: np.ndarray) -> np.ndarray:
        """(N,3) の要素添字 → (N,3) の中心座標。stage2 が要素と面を突き合わせるための口。"""
        idx = np.asarray(indices, dtype=float)
        if idx.ndim != 2 or idx.shape[1] != 3:
            raise ValueError(f"indices must be (N,3), got {idx.shape}")
        return np.asarray(self.origin, dtype=float) + self.spacing * (
            idx + CELL_CENTER_OFFSET
        )

    def index_of(self, points: np.ndarray) -> np.ndarray:
        """(N,3) のワールド座標 → その点を含む要素の添字 (N,3)。格子の外は負値/範囲外を返す。"""
        pts = np.asarray(points, dtype=float)
        if pts.ndim != 2 or pts.shape[1] != 3:
            raise ValueError(f"points must be (N,3), got {pts.shape}")
        return np.floor((pts - np.asarray(self.origin, dtype=float)) / self.spacing).astype(int)

    def world_box(self) -> tuple[np.ndarray, np.ndarray]:
        """格子が覆うワールド空間の箱 (min, max)。"""
        lo = np.asarray(self.origin, dtype=float)
        hi = lo + self.spacing * np.asarray(self.shape, dtype=float)
        return lo, hi

    def as_dict(self) -> dict:
        return {
            "origin": [float(v) for v in self.origin],
            "spacing": float(self.spacing),
            "shape": [int(v) for v in self.shape],
        }


def grid_for_bbox(
    bbox_min: Iterable[float],
    bbox_max: Iterable[float],
    elements_long_axis: int,
    pad: int = DEFAULT_PAD,
) -> Grid:
    """バウンディングボックスから立方体格子を起こす。

    要素サイズは `最長辺 / elements_long_axis`。各軸の要素数は
    `ceil(辺長 / spacing) + 2*pad` で、形状は格子の**中央**に置く(原点は bbox の中心から
    格子の半分を引く)。中央に置くのは決定的にするため — 低い側の角に合わせると、
    軸ごとに余白の配分が変わって同じ形でも軸ごとに違う位置に載る。
    """
    lo = np.asarray(list(bbox_min), dtype=float)
    hi = np.asarray(list(bbox_max), dtype=float)
    if lo.shape != (3,) or hi.shape != (3,):
        raise ValueError("bbox_min / bbox_max must have 3 components")
    if not np.all(np.isfinite(lo)) or not np.all(np.isfinite(hi)):
        raise ValueError(f"bbox is not finite: {lo} .. {hi}")
    extent = hi - lo
    if np.any(extent < 0):
        raise ValueError(f"bbox_max is below bbox_min: {lo} .. {hi}")
    longest = float(np.max(extent))
    if not longest > 0:
        raise ValueError("bbox has zero size in every axis (degenerate shape)")
    n_long = int(elements_long_axis)
    if n_long < 1:
        raise ValueError(f"elements_long_axis must be >= 1, got {elements_long_axis!r}")
    if int(pad) < 0:
        raise ValueError(f"pad must be >= 0, got {pad!r}")

    spacing = longest / n_long
    # 浮動小数の丸めで最長軸が n_long+1 になるのを避けるため、ceil の前に微小量を引く。
    counts = np.ceil(extent / spacing - 1e-9).astype(int)
    counts = np.maximum(counts, 1) + 2 * int(pad)
    center = 0.5 * (lo + hi)
    origin = center - 0.5 * spacing * counts.astype(float)
    return Grid(
        origin=(float(origin[0]), float(origin[1]), float(origin[2])),
        spacing=float(spacing),
        shape=(int(counts[0]), int(counts[1]), int(counts[2])),
    )


# ---------------------------------------------------------------------------
# 三角形群(面添字つき)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TriangleSoup:
    """外向きに揃った三角形群。**面添字を捨てない**(stage2「面アンカー」がこれを使う)。

    `positions`  : (V,3) 頂点座標[mm]
    `triangles`  : (T,3) 頂点添字
    `face_index` : (T,)  その三角形が属する OCCT の面添字(1始まり。`kernel.tessellate` と同じ)
    `face_kind`  : {面添字: "plane"/"cylinder"/...}
    `face_props` : {面添字: {"area": float, "centroid": (3,), "normal": (3,), "kind": str}}
                   **B-rep から直に取った真値**(三角形分割の近似ではない)。`kernel.tessellate`
                   が面ごとに返すものをそのまま畳んである。stage2「面アンカー」の受入検査が
                   「節点集合の重心・面積が解像度を上げると真の面へ近づく」を測る基準になる —
                   三角形から作った値を基準にすると、近似を近似と比べることになって
                   何も検査していないのと同じになる。
    """

    positions: np.ndarray
    triangles: np.ndarray
    face_index: np.ndarray
    face_kind: dict[int, str] = field(default_factory=dict)
    deviation: float = 0.0
    face_props: dict[int, dict] = field(default_factory=dict)

    @property
    def triangle_count(self) -> int:
        return int(self.triangles.shape[0])

    @property
    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        return self.positions.min(axis=0), self.positions.max(axis=0)

    def triangle_vertices(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        p = self.positions
        t = self.triangles
        return p[t[:, 0]], p[t[:, 1]], p[t[:, 2]]

    def triangles_of_face(self, face_index: int) -> np.ndarray:
        """面添字 → その面の三角形の添字。stage2 が面から要素を引くための口。"""
        return np.flatnonzero(self.face_index == int(face_index))

    def signed_volume(self, triangles: Optional[np.ndarray] = None) -> float:
        """発散定理による符号付き体積。外向きに揃っていれば正。

        **基準点(bboxの中心)を引いてから三重積を取る。** 絶対座標のまま足すと、原点から
        遠いソリッドで桁落ちする(原点から 1e8 mm の 40x20x10 の箱で
        8000 が 7957.33 になり、薄い形なら符号が反転して「法線が内向き」と誤って拒否する)。
        体積は平行移動に対して不変なので、基準点を引くのは数値の都合だけの操作。

        `triangles` に三角形の添字を渡すと、その部分集合だけの符号付き体積を返す
        (連結成分ごとに見るため)。このとき**基準点はその部分集合自身の点から取る**。
        soup 全体の bbox 中心を使うと、離れた位置にある小さな殻でその殻の座標が基準点から
        大きく離れ、桁落ちが戻ってくる。
        """
        p = self.positions
        t = self.triangles if triangles is None else self.triangles[triangles]
        if t.shape[0] == 0:
            # 三角形が1枚も無い部分集合は体積ゼロ。基準点を空配列から取ろうとすると落ちるので
            # 先に返す(いまの呼び出し元は必ず1枚以上あるが、stage2 はこの関数を直に呼ぶ)。
            return 0.0
        a_raw = p[t[:, 0]]
        b_raw = p[t[:, 1]]
        c_raw = p[t[:, 2]]
        lo = np.minimum(np.minimum(a_raw.min(axis=0), b_raw.min(axis=0)), c_raw.min(axis=0))
        hi = np.maximum(np.maximum(a_raw.max(axis=0), b_raw.max(axis=0)), c_raw.max(axis=0))
        origin = 0.5 * (lo + hi)
        return float(
            np.einsum(
                "ij,ij->i", a_raw - origin, np.cross(b_raw - origin, c_raw - origin)
            ).sum()
            / 6.0
        )

    def area(self) -> float:
        a, b, c = self.triangle_vertices()
        return float(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())


def triangles_from_shape(
    shape: Any,
    deviation: float,
    angular_deflection: float = DEFAULT_ANGULAR_DEFLECTION,
) -> TriangleSoup:
    """`kernel.tessellate` の戻り値を `TriangleSoup` に畳む(面添字は保持する)。"""
    payload = kernel.tessellate(
        shape, deviation=deviation, angular_deflection=angular_deflection
    )
    untriangulated = list(payload.get("untriangulated") or [])
    if untriangulated:
        raise ValueError(
            "三角形分割できなかった面がある(表面に穴が開くので格子を起こせない): "
            f"面添字={untriangulated}, deviation={deviation}"
        )

    pos_chunks: list[np.ndarray] = []
    tri_chunks: list[np.ndarray] = []
    face_chunks: list[np.ndarray] = []
    face_kind: dict[int, str] = {}
    face_props: dict[int, dict] = {}
    offset = 0
    for face in payload["faces"]:
        positions = np.asarray(face["positions"], dtype=float).reshape(-1, 3)
        indices = np.asarray(face["indices"], dtype=np.int64).reshape(-1, 3)
        face_kind[int(face["index"])] = str(face.get("kind", ""))
        face_props[int(face["index"])] = {
            "kind": str(face.get("kind", "")),
            "area": float(face.get("area", 0.0)),
            "centroid": np.asarray(face.get("centroid", (0.0, 0.0, 0.0)), dtype=float),
            "normal": np.asarray(face.get("normal", (0.0, 0.0, 0.0)), dtype=float),
        }
        if indices.size == 0:
            continue
        pos_chunks.append(positions)
        tri_chunks.append(indices + offset)
        face_chunks.append(np.full(indices.shape[0], int(face["index"]), dtype=np.int64))
        offset += positions.shape[0]

    if not tri_chunks:
        raise ValueError("三角形が1枚も取れなかった")

    return TriangleSoup(
        positions=np.concatenate(pos_chunks, axis=0),
        triangles=np.concatenate(tri_chunks, axis=0),
        face_index=np.concatenate(face_chunks, axis=0),
        face_kind=face_kind,
        deviation=float(deviation),
        face_props=face_props,
    )


# ---------------------------------------------------------------------------
# 入力の健全性検査
# ---------------------------------------------------------------------------
def weld_vertices(positions: np.ndarray, tolerance: float) -> np.ndarray:
    """座標が `tolerance` 以内の頂点を同一視するラベルを返す(格子への丸めによる融着)。

    `kernel.tessellate` は**面ごとに**頂点配列を返すので、隣り合う面の共有辺は
    別々の頂点添字になる。辺の対を数えるにはまず座標で融着する必要がある。
    """
    if not tolerance > 0:
        raise ValueError(f"tolerance must be positive, got {tolerance!r}")
    keys = np.round(positions / tolerance).astype(np.int64)
    _unique, labels = np.unique(keys, axis=0, return_inverse=True)
    return labels.astype(np.int64).ravel()


def inspect_soup(soup: TriangleSoup, weld_tolerance: Optional[float] = None) -> dict:
    """三角形群が「閉じていて外向きに揃った表面」かを調べる。壊れていたら `ValueError`。

    検査するのは3つ:

    1. **有向辺の対** — 閉じた向きの揃った表面では、辺 (u,v) はちょうど1回現れ、
       その逆向き (v,u) もちょうど1回現れる。片方しか無ければ穴(非水密)、
       同じ向きが2回現れれば向きの不整合。
    2. **符号付き体積が正** — 法線が内向き(裏返った形状)を弾く。
    3. **退化三角形が全部ではない**。
    """
    positions = soup.positions
    lo, hi = soup.bbox
    diagonal = float(np.linalg.norm(hi - lo))
    if not diagonal > 0:
        raise ValueError("形状の大きさがゼロ(退化した入力)")
    tol = float(weld_tolerance) if weld_tolerance else max(diagonal * 1e-7, 1e-9)

    labels = weld_vertices(positions, tol)
    tri = labels[soup.triangles]

    # 退化三角形(融着後に頂点が重なるもの)は面を持たないので辺の対から外す。
    degenerate = (tri[:, 0] == tri[:, 1]) | (tri[:, 1] == tri[:, 2]) | (tri[:, 2] == tri[:, 0])
    live = tri[~degenerate]
    if live.shape[0] == 0:
        raise ValueError("有効な三角形が1枚も無い(すべて退化している)")

    edges = np.concatenate(
        [live[:, [0, 1]], live[:, [1, 2]], live[:, [2, 0]]], axis=0
    )
    # 有向辺を1つの整数キーにする(頂点数は融着後のラベル数)
    n_labels = int(labels.max()) + 1
    forward = edges[:, 0].astype(np.int64) * n_labels + edges[:, 1].astype(np.int64)
    backward = edges[:, 1].astype(np.int64) * n_labels + edges[:, 0].astype(np.int64)
    keys, counts = np.unique(forward, return_counts=True)
    # 各有向辺が何回現れたか
    fwd_count = counts[np.searchsorted(keys, forward)]
    bwd_pos = np.searchsorted(keys, backward)
    bwd_pos = np.clip(bwd_pos, 0, keys.size - 1)
    bwd_count = np.where(keys[bwd_pos] == backward, counts[bwd_pos], 0)

    boundary = int(np.count_nonzero(bwd_count == 0))          # 相手のいない辺 = 穴
    duplicated = int(np.count_nonzero(fwd_count > 1))          # 同じ向きが複数 = 向きの不整合
    mismatched = int(np.count_nonzero(fwd_count != bwd_count))  # 対の数が合わない

    volume = soup.signed_volume()
    components = _shell_orientation_report(soup, labels, ~degenerate)
    report = {
        "triangles": soup.triangle_count,
        "degenerate_triangles": int(np.count_nonzero(degenerate)),
        "welded_vertices": n_labels,
        "weld_tolerance": tol,
        "boundary_edges": boundary,
        "duplicated_directed_edges": duplicated,
        "mismatched_edges": mismatched,
        "signed_volume": volume,
        "area": soup.area(),
        "components": len(components),
        "component_volumes": [round(comp["volume"], 6) for comp in components],
        "component_depths": [comp["depth"] for comp in components],
    }

    if boundary or duplicated or mismatched:
        raise ValueError(
            "表面が閉じていないか、面の向きが揃っていない"
            f"(相手のいない辺 {boundary} 本 / 同じ向きの重複辺 {duplicated} 本 / "
            f"対の数が合わない辺 {mismatched} 本)。"
            "非水密なシェルや向きの壊れた形状からは正しい内外判定ができないので、"
            "もっともらしい格子を返さずここで落とす。"
            f" 詳細: {report}"
        )
    if not volume > 0:
        raise ValueError(
            f"符号付き体積が正でない({volume:.6g} mm^3)。法線が内向き = 裏返った形状。"
            f" 詳細: {report}"
        )
    for comp in components:
        expected = "正" if comp["depth"] % 2 == 0 else "負(空洞)"
        actual_positive = comp["volume"] > 0
        if actual_positive != (comp["depth"] % 2 == 0):
            raise ValueError(
                f"殻の向きが揃っていない: {len(components)}個ある連結成分のうち "
                f"{comp['index']}番目(三角形{comp['triangles']}枚)は他の殻 {comp['depth']} 枚の"
                f"内側にあるので符号付き体積は{expected}でなければならないが、"
                f"{comp['volume']:.6g} mm^3 だった。"
                "**総体積だけを見ると正のまま通ってしまう壊れ方**(内部空洞の殻だけを裏返すと"
                "空洞が材料として埋まる)なので、殻ごとに見ている。"
                f" 詳細: {report}"
            )
    return report


def _shell_orientation_report(
    soup: TriangleSoup, labels: np.ndarray, live_mask: np.ndarray
) -> list[dict]:
    """連結成分(殻)ごとに、符号付き体積と「他の殻いくつの内側にあるか」を返す。

    内部空洞の殻は外向き法線が**空洞のほうを向く**ので符号付き体積が負になる。
    殻ごとに見ないと、**空洞の殻だけを裏返した入力**が総体積が正のまま通ってしまい、
    空洞が材料として埋まる(40³の箱に20³の空洞で実際に再現した)。
    入れ子の深さが偶数の殻は正、奇数(=空洞)の殻は負でなければならない。
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    tri = labels[soup.triangles]
    live_idx = np.flatnonzero(live_mask)
    live = tri[live_idx]
    n_labels = int(labels.max()) + 1
    rows = np.concatenate([live[:, 0], live[:, 1], live[:, 2]])
    cols = np.concatenate([live[:, 1], live[:, 2], live[:, 0]])
    graph = coo_matrix(
        (np.ones(rows.size, dtype=np.int8), (rows, cols)), shape=(n_labels, n_labels)
    ).tocsr()
    n_comp, comp_of_label = connected_components(graph, directed=False)

    comp_of_tri = comp_of_label[live[:, 0]]
    lo, hi = soup.bbox
    eps = float(np.linalg.norm(hi - lo)) * 1e-6

    out: list[dict] = []
    reps = []
    tri_sets = []
    for k in range(n_comp):
        members = live_idx[comp_of_tri == k]
        if members.size == 0:
            continue  # 三角形を持たない孤立ラベル(退化三角形だけのラベル)
        tri_sets.append(members)
        reps.append(soup.positions[soup.triangles[members[0], 0]])
        out.append(
            {
                "index": len(out),
                "triangles": int(members.size),
                "volume": soup.signed_volume(members),
                "depth": 0,
            }
        )

    for i, comp in enumerate(out):
        depth = 0
        for j, members in enumerate(tri_sets):
            if i == j:
                continue
            if _point_is_inside(soup, members, reps[i], eps):
                depth += 1
        comp["depth"] = depth
    return out


def _point_is_inside(
    soup: TriangleSoup, triangles: np.ndarray, point: np.ndarray, eps: float
) -> bool:
    """1点が三角形の部分集合が囲む領域の内側かを、+z のレイの巻き数で判定する。

    `occupancy` と同じ考え方だが、格子ではなく1点だけを見る(殻の入れ子の深さを数えるため)。
    レイの (x,y) は `eps` だけずらす(頂点や辺をちょうど貫く縮退を外す)。
    """
    idx = np.asarray(triangles)
    a = soup.positions[soup.triangles[idx, 0]]
    b = soup.positions[soup.triangles[idx, 1]]
    c = soup.positions[soup.triangles[idx, 2]]
    px = float(point[0]) + eps
    py = float(point[1]) + eps * 0.6180339887498949
    pz = float(point[2])
    area2 = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (
        c[:, 0] - a[:, 0]
    )
    usable = np.abs(area2) > 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        inv = np.where(usable, 1.0 / np.where(usable, area2, 1.0), 0.0)
        l0 = ((c[:, 0] - b[:, 0]) * (py - b[:, 1]) - (c[:, 1] - b[:, 1]) * (px - b[:, 0])) * inv
        l1 = ((a[:, 0] - c[:, 0]) * (py - c[:, 1]) - (a[:, 1] - c[:, 1]) * (px - c[:, 0])) * inv
        l2 = ((b[:, 0] - a[:, 0]) * (py - a[:, 1]) - (b[:, 1] - a[:, 1]) * (px - a[:, 0])) * inv
    inside = usable & (l0 >= 0.0) & (l1 >= 0.0) & (l2 >= 0.0)
    z = l0 * a[:, 2] + l1 * b[:, 2] + l2 * c[:, 2]
    hit = inside & (z > pz)
    winding = int(np.sign(area2[hit]).sum())
    return winding != 0


# ---------------------------------------------------------------------------
# 占有判定(+zレイの巻き数)
# ---------------------------------------------------------------------------
def occupancy(
    soup: TriangleSoup,
    grid: Grid,
    *,
    jitter: Optional[tuple[float, float]] = None,
    strict: bool = True,
) -> tuple[np.ndarray, dict]:
    """三角形群と格子から占有マスク (nx,ny,nz) の bool 配列を返す。

    `jitter=None`(既定)のときは `RAY_JITTER_SEQUENCE` を順に試し、縮退(巻き数が0に
    戻らない列)が出たら**次のずらし幅でやり直す**。全部だめだったときだけ例外にする。
    明示的にタプルを渡すとその1回だけを試す(受入検査がずらしの効きを見るための口)。
    `stats["jitter"]` に実際に成功したずらし幅、`stats["jitter_attempts"]` に試した回数が入る。


    各列 (i,j) の**セル中心**を通る +z のレイを飛ばし、三角形との交点を集める。
    交点には「入る(+1)/出る(-1)」の符号を付け、z 順の累積(巻き数)が 0 でない区間に
    ある**セル中心**を占有とする。詳細と、偶奇ではなく巻き数を使う理由はモジュール
    docstring を参照。

    戻り値の2つめは診断。`unbalanced_columns` は最後まで巻き数が 0 に戻らなかった列の数で、
    閉じた表面なら 0 でなければならない(`strict=True` なら 0 でないとき例外)。
    """
    if jitter is not None:
        return _occupancy_once(soup, grid, jitter, strict)

    last_error: Optional[ValueError] = None
    for attempt, candidate in enumerate(RAY_JITTER_SEQUENCE, start=1):
        try:
            mask, stats = _occupancy_once(soup, grid, candidate, strict=True)
        except ValueError as exc:
            last_error = exc
            continue
        stats["jitter"] = candidate
        stats["jitter_attempts"] = attempt
        return mask, stats
    if not strict:
        mask, stats = _occupancy_once(soup, grid, RAY_JITTER_SEQUENCE[0], strict=False)
        stats["jitter"] = RAY_JITTER_SEQUENCE[0]
        stats["jitter_attempts"] = len(RAY_JITTER_SEQUENCE)
        return mask, stats
    raise ValueError(
        f"レイのずらしを {len(RAY_JITTER_SEQUENCE)} 通り試したがすべて縮退した。"
        f"最後の理由: {last_error}"
    )


def _occupancy_once(
    soup: TriangleSoup,
    grid: Grid,
    jitter: tuple[float, float],
    strict: bool,
) -> tuple[np.ndarray, dict]:
    """`occupancy` の1回ぶん(ずらし幅を固定して1回だけ走査する)。"""
    nx, ny, nz = grid.shape
    h = float(grid.spacing)

    # セル中心は必ず Grid 経由で取る(規約の定義を1箇所に閉じ込めるため)。
    ray_x = grid.centers_1d(0) + jitter[0] * h
    ray_y = grid.centers_1d(1) + jitter[1] * h
    center_z = grid.centers_1d(2)

    a, b, c = soup.triangle_vertices()
    # xy 投影の符号付き面積 x2 = 外向き法線の z 成分(の定数倍)。
    area2 = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (
        c[:, 0] - a[:, 0]
    )
    area_eps = 1e-12 * h * h
    flat = np.abs(area2) <= area_eps  # xy に潰れた三角形(垂直な壁・退化)。交点を持たない

    tri_min_x = np.minimum(np.minimum(a[:, 0], b[:, 0]), c[:, 0])
    tri_max_x = np.maximum(np.maximum(a[:, 0], b[:, 0]), c[:, 0])
    tri_min_y = np.minimum(np.minimum(a[:, 1], b[:, 1]), c[:, 1])
    tri_max_y = np.maximum(np.maximum(a[:, 1], b[:, 1]), c[:, 1])

    # 三角形の xy 投影が覆う列の範囲(searchsorted で境界を出す)
    i_lo = np.searchsorted(ray_x, tri_min_x, side="left")
    i_hi = np.searchsorted(ray_x, tri_max_x, side="right")
    j_lo = np.searchsorted(ray_y, tri_min_y, side="left")
    j_hi = np.searchsorted(ray_y, tri_max_y, side="right")
    # どの列の中心も覆っていない三角形(格子より細かい三角形では大半がこれ)は素通り。
    covers = (i_hi > i_lo) & (j_hi > j_lo)
    usable = (~flat) & covers

    col_chunks: list[np.ndarray] = []
    z_chunks: list[np.ndarray] = []
    sign_chunks: list[np.ndarray] = []

    for t in np.flatnonzero(usable):
        x0, y0, z0 = a[t]
        x1, y1, z1 = b[t]
        x2, y2, z2 = c[t]
        xs = ray_x[i_lo[t] : i_hi[t]]
        ys = ray_y[j_lo[t] : j_hi[t]]
        px = xs[:, None]
        py = ys[None, :]

        inv = 1.0 / area2[t]
        l0 = ((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)) * inv
        l1 = ((x0 - x2) * (py - y2) - (y0 - y2) * (px - x2)) * inv
        l2 = ((x1 - x0) * (py - y0) - (y1 - y0) * (px - x0)) * inv
        inside = (l0 >= 0.0) & (l1 >= 0.0) & (l2 >= 0.0)
        if not inside.any():
            continue
        ii, jj = np.nonzero(inside)
        z = l0[ii, jj] * z0 + l1[ii, jj] * z1 + l2[ii, jj] * z2
        col_chunks.append((ii + i_lo[t]) * ny + (jj + j_lo[t]))
        z_chunks.append(z)
        # 法線の z 成分が正(上向き) = レイが出る側 → -1。負(下向き) = 入る側 → +1。
        sign_chunks.append(
            np.full(z.size, -1 if area2[t] > 0 else 1, dtype=np.int32)
        )

    mask = np.zeros((nx, ny, nz), dtype=bool)
    stats = {
        "triangles": soup.triangle_count,
        "jitter": jitter,
        "jitter_attempts": 1,
        "flat_projection": int(np.count_nonzero(flat)),
        "covers_no_column": int(np.count_nonzero(~covers)),
        "crossings": 0,
        "unbalanced_columns": 0,
    }
    if not col_chunks:
        if strict:
            raise ValueError(
                "三角形とレイの交点が1つも無い。格子が形状をまったく覆っていない"
                f"(格子の範囲 {np.round(grid.world_box()[0], 3).tolist()}"
                f" .. {np.round(grid.world_box()[1], 3).tolist()} /"
                f" 形状の範囲 {np.round(soup.bbox[0], 3).tolist()}"
                f" .. {np.round(soup.bbox[1], 3).tolist()})"
            )
        return mask, stats

    col = np.concatenate(col_chunks)
    z = np.concatenate(z_chunks)
    sign = np.concatenate(sign_chunks).astype(np.int64)
    stats["crossings"] = int(col.size)

    order = np.lexsort((z, col))
    col = col[order]
    z = z[order]
    sign = sign[order]

    # 列ごとの累積(巻き数)。全体の累積から、その列が始まる直前の値を引く。
    # 閉じた表面では各列の符号の和が 0 になるので、この引き算は実は恒等的に何もしない
    # (変異検査で確認済み)。それでも「列ごとの巻き数」という定義どおりに
    # 書いておく — 閉じているという不変条件が別の場所で守られていることに依存させない。
    cumulative = np.cumsum(sign)
    is_first = np.empty(col.size, dtype=bool)
    is_first[0] = True
    is_first[1:] = col[1:] != col[:-1]
    starts = np.flatnonzero(is_first)
    base_values = np.zeros(starts.size, dtype=np.int64)
    base_values[1:] = cumulative[starts[1:] - 1]
    group = np.cumsum(is_first) - 1
    winding = cumulative - base_values[group]

    is_last = np.empty(col.size, dtype=bool)
    is_last[-1] = True
    is_last[:-1] = col[1:] != col[:-1]
    unbalanced = int(np.count_nonzero(winding[is_last] != 0))
    stats["unbalanced_columns"] = unbalanced
    if unbalanced and strict:
        raise ValueError(
            f"レイの巻き数が 0 に戻らない列が {unbalanced} 本ある(全 {nx * ny} 列)。"
            "閉じた表面ならこれは 0 でなければならない。考えられる原因は "
            "(1) 表面が閉じていない/向きが壊れている、"
            "(2) レイが頂点をちょうど貫いた(円錐の頂点のように多数の三角形が集まる点。"
            f"`jitter` を既定の {RAY_JITTER_X}/{RAY_JITTER_Y} から変えると外れる)。"
            "間違った格子を返さずここで落とす。"
        )

    interior = (~is_last) & (winding != 0)
    if np.any(interior):
        z_lo = z[interior]
        z_hi = np.roll(z, -1)[interior]
        k_lo = np.searchsorted(center_z, z_lo, side="left")
        k_hi = np.searchsorted(center_z, z_hi, side="left")
        k_lo = np.clip(k_lo, 0, nz)
        k_hi = np.clip(k_hi, 0, nz)
        keep = k_hi > k_lo
        if np.any(keep):
            diff = np.zeros((nx * ny, nz + 1), dtype=np.int32)
            cols = col[interior][keep]
            np.add.at(diff, (cols, k_lo[keep]), 1)
            np.add.at(diff, (cols, k_hi[keep]), -1)
            filled = np.cumsum(diff, axis=1)[:, :nz] > 0
            mask = filled.reshape(nx, ny, nz)

    return mask, stats


# ---------------------------------------------------------------------------
# 表面までの距離(受入検査の「境界近傍を除く」と、stage2 の最近傍面のための共通部品)
# ---------------------------------------------------------------------------
def closest_triangle(
    soup: TriangleSoup, points: np.ndarray, chunk: Optional[int] = None
) -> tuple[np.ndarray, np.ndarray]:
    """各点について、三角形群までの**厳密な**最短距離とその三角形の添字を返す。

    面添字が要るときは `soup.face_index[tri]` で引ける(stage2「面アンカー」の素材)。

    **候補点の最小値で求める(領域分けの優先順位では求めない)。** 三角形上の最近傍点は
    「3頂点 / 3辺への射影(端で打ち切る) / 面への射影(重心座標が三角形の内側のときだけ)」の
    どれかなので、7候補の距離を全部出して最小を取れば必ず正しい。教科書(Ericson)の
    領域分けは分岐が排他であることを前提にしていて、**退化した三角形(2頂点が重なる等)では
    複数の領域が同時に真になり、後から上書きしたほうが勝って誤った距離を返す**
    (`a=b=(0,0,0), c=(5,4,4), p=(10,4,-8)` で 13.416407865 を返したが、
    線分への正解は 12.638010059)。**退化三角形はOCCTの三角形分割に実際に現れる**ので
    (円錐の頂点まわりなど)、入力側で弾くのではなくここで正しく扱う。

    点をチャンクに切ってメモリを抑える。頂点だけの近似(KD木)は三角形1枚ぶん距離を
    過大評価しうるので使わない。
    """
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f"points must be (N,3), got {pts.shape}")
    a, b, c = soup.triangle_vertices()
    ab = b - a
    ac = c - a
    bc = c - b
    if chunk is None:
        # (点数 x 三角形数) の中間配列を数MBに抑える。三角形が多いほど点を細かく切る。
        chunk = int(max(1, 200_000 // max(a.shape[0], 1)))

    # 辺の長さの2乗(0 なら退化した辺 = 端点そのものが最近傍)
    len_ab = np.einsum("ij,ij->i", ab, ab)
    len_ac = np.einsum("ij,ij->i", ac, ac)
    len_bc = np.einsum("ij,ij->i", bc, bc)
    normal = np.cross(ab, ac)
    len_n = np.einsum("ij,ij->i", normal, normal)

    def _seg_dist2(p_rel: np.ndarray, edge: np.ndarray, length2: np.ndarray) -> np.ndarray:
        """線分(始点 → 始点+edge)への距離の2乗。`p_rel` は始点からの相対位置 (m,T,3)。"""
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.einsum("ij,mij->mi", edge, p_rel) / np.where(length2 > 0, length2, 1.0)
        t = np.where(length2 > 0, t, 0.0)
        t = np.clip(t, 0.0, 1.0)
        diff = p_rel - edge[None, :, :] * t[..., None]
        return np.einsum("mij,mij->mi", diff, diff)

    dist = np.empty(pts.shape[0], dtype=float)
    which = np.empty(pts.shape[0], dtype=np.int64)
    for s in range(0, pts.shape[0], chunk):
        p = pts[s : s + chunk][:, None, :]  # (m,1,3)
        ap = p - a[None, :, :]
        bp = p - b[None, :, :]

        # 3辺への射影(端で打ち切る)。頂点はこの打ち切りに含まれる。
        best = _seg_dist2(ap, ab, len_ab)
        np.minimum(best, _seg_dist2(ap, ac, len_ac), out=best)
        np.minimum(best, _seg_dist2(bp, bc, len_bc), out=best)

        # 面への射影。重心座標が三角形の内側にあるときだけ候補にする。
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = np.where(len_n > 0, 1.0 / np.where(len_n > 0, len_n, 1.0), 0.0)
            cross_ap_ac = np.cross(ap, ac[None, :, :])
            cross_ab_ap = np.cross(ab[None, :, :], ap)
            v = np.einsum("mij,ij->mi", cross_ap_ac, normal) * inv
            w = np.einsum("mij,ij->mi", cross_ab_ap, normal) * inv
        signed = np.einsum("mij,ij->mi", ap, normal) * np.sqrt(np.where(len_n > 0, inv, 0.0))
        face_ok = (len_n > 0) & (v >= 0.0) & (w >= 0.0) & (v + w <= 1.0)
        face_dist2 = np.where(face_ok, signed * signed, np.inf)
        np.minimum(best, face_dist2, out=best)

        pick = np.argmin(best, axis=1)
        dist[s : s + chunk] = np.sqrt(np.maximum(best[np.arange(pick.size), pick], 0.0))
        which[s : s + chunk] = pick
    return dist, which


def distance_to_surface(soup: TriangleSoup, points: np.ndarray, chunk: int = 256) -> np.ndarray:
    """表面までの最短距離(符号なし)。"""
    return closest_triangle(soup, points, chunk=chunk)[0]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
@dataclass
class VoxelModel:
    """設計空間の格子と、そこに載る保持/障害物領域。

    `design` / `keep` / `obstacle` はすべて同じ格子・同じ形の bool 配列 (nx,ny,nz)。
    `keep` と `obstacle` は**設計空間の内側に切り詰めてある**(外へはみ出した分は捨てる)。
    """

    grid: Grid
    design: np.ndarray
    keep: np.ndarray
    obstacle: np.ndarray
    soup: TriangleSoup
    stats: dict = field(default_factory=dict)

    @property
    def design_volume(self) -> float:
        return float(np.count_nonzero(self.design)) * self.grid.element_volume

    @property
    def free(self) -> np.ndarray:
        """最適化が動かしてよい要素(設計空間のうち、保持でも障害物でもないもの)。"""
        return self.design & ~self.keep & ~self.obstacle

    def initial_density(self, volfrac: float) -> np.ndarray:
        """SIMP の初期密度場。保持=1、障害物=0、それ以外=volfrac、設計空間の外=0。

        stage2 以降が `simp.py` へ渡すための便宜。ここでは形を作るだけで最適化はしない。
        """
        rho = np.zeros(self.grid.shape, dtype=float)
        rho[self.free] = float(volfrac)
        rho[self.keep] = 1.0
        return rho


def voxelize(
    shape: Any,
    elements_long_axis: int = 40,
    *,
    keep: Any = None,
    obstacle: Any = None,
    pad: int = DEFAULT_PAD,
    grid: Optional[Grid] = None,
    deviation: Optional[float] = None,
    deviation_ratio: float = DEFAULT_DEVIATION_RATIO,
    angular_deflection: float = DEFAULT_ANGULAR_DEFLECTION,
) -> VoxelModel:
    """設計空間のソリッドから格子を起こし、保持領域・障害物領域を同じ格子に載せる。

    `shape` / `keep` / `obstacle` は OCCT の `TopoDS_Shape`(build123d のオブジェクトを
    渡した場合は `.wrapped` を使う)。`keep` と `obstacle` は設計空間の外へはみ出した分を
    捨て、両者が重なっていれば `ValueError` にする(どちらを優先するかは人が決めることで、
    黙って片方を勝たせてはいけない)。

    `grid` を渡すとその格子をそのまま使う(stage2 が同じ格子で別のソリッドを載せるための口)。
    """
    started = time.perf_counter()
    solid = _unwrap(shape)

    # 格子は **B-rep の bbox** から起こす。三角形分割の bbox から起こし直してはいけない ——
    # 弦は真の面より内側に来るので、仕様の「B-repの周囲に1要素分の余白」が満たせなくなる
    # (要素サイズ 0.89738 に対し余白 0.89605 になっていた)。
    if grid is None:
        lo, hi = _shape_bbox(solid)
        grid = grid_for_bbox(lo, hi, elements_long_axis, pad=pad)
    dev = float(deviation) if deviation else max(grid.spacing * deviation_ratio, 1e-6)

    soup = triangles_from_shape(solid, deviation=dev, angular_deflection=angular_deflection)
    report = inspect_soup(soup)

    design, occ_stats = occupancy(soup, grid)
    stats = {
        "seconds": 0.0,
        "deviation": dev,
        "grid": grid.as_dict(),
        "surface": report,
        "occupancy": occ_stats,
        "design_elements": int(np.count_nonzero(design)),
        "keep_elements": 0,
        "obstacle_elements": 0,
        "keep_dropped_outside": 0,
        "obstacle_dropped_outside": 0,
    }

    keep_mask = np.zeros_like(design)
    obstacle_mask = np.zeros_like(design)
    if keep is not None:
        raw, sub = _voxelize_region(
            keep, grid, deviation=dev, angular=angular_deflection, label="保持"
        )
        keep_mask = raw & design
        stats["keep_dropped_outside"] = int(np.count_nonzero(raw & ~design))
        stats["keep_surface"] = sub
    if obstacle is not None:
        raw, sub = _voxelize_region(
            obstacle, grid, deviation=dev, angular=angular_deflection, label="障害物"
        )
        obstacle_mask = raw & design
        stats["obstacle_dropped_outside"] = int(np.count_nonzero(raw & ~design))
        stats["obstacle_surface"] = sub

    overlap = int(np.count_nonzero(keep_mask & obstacle_mask))
    if overlap:
        raise ValueError(
            f"保持領域と障害物領域が {overlap} 要素で重なっている。"
            "「必ず材料を残す」と「材料を入れない」は同時に満たせないので、"
            "どちらを優先するかは呼び出し側が決めること(黙って片方を勝たせない)。"
        )

    stats["keep_elements"] = int(np.count_nonzero(keep_mask))
    stats["obstacle_elements"] = int(np.count_nonzero(obstacle_mask))
    stats["seconds"] = time.perf_counter() - started
    return VoxelModel(
        grid=grid,
        design=design,
        keep=keep_mask,
        obstacle=obstacle_mask,
        soup=soup,
        stats=stats,
    )


class OutsideDesignSpaceError(ValueError):
    """保持/障害物領域が塊(設計空間)からはみ出した。

    `ValueError` の派生なので、既存の `except ValueError` はそのまま効く。はみ出した要素数は
    `outside`(キーは `"keep"` / `"obstacle"`、値は要素数)で引ける。
    """

    def __init__(self, message: str, outside: dict) -> None:
        super().__init__(message)
        self.outside = dict(outside)


def model_from_design_mask(
    grid: Grid,
    design: Any,
    *,
    anchor_shape: Any,
    keep: Any = None,
    obstacle: Any = None,
    on_outside: str = "raise",
    deviation: Optional[float] = None,
    deviation_ratio: float = DEFAULT_DEVIATION_RATIO,
    angular_deflection: float = DEFAULT_ANGULAR_DEFLECTION,
) -> VoxelModel:
    """塊(設計空間)を bool マスクで、面アンカーの対象ソリッドを別に受け取って組み立てる。

    `voxelize()` は1つのソリッドに「削る前の塊」と「拘束・荷重に指定できる面」を兼ねさせる。
    こちらはその2つを切り離した入口で、塊は `design`(格子と同じ形の bool 配列)から、面は
    `anchor_shape` から来る。**塊をどのロジックで決めるかはここでは決めない**(呼び出し側の話)。

    `anchor_shape` は OCCT の `TopoDS_Shape` / build123d のオブジェクト / 既に作った
    `TriangleSoup` のいずれか。**面添字は渡したものの面の並びで決まる**ので、面を選ぶのに
    使った `TriangleSoup` があるならそれをそのまま渡すこと(作り直すと面添字が食い違いうる)。

    `keep` / `obstacle` は同じ格子に載せる。**塊からはみ出したら既定で
    `OutsideDesignSpaceError`**。`on_outside="clip"` を明示したときだけ `voxelize()` と同じに
    切り詰める。塊を自動で決めるようになるとはみ出しは普通に起こるので、既定は例外にしてある。
    """
    started = time.perf_counter()
    mode = str(on_outside)
    if mode not in ("raise", "clip"):
        raise ValueError(f"on_outside must be 'raise' or 'clip', got {on_outside!r}")

    mask = np.asarray(design)
    if mask.shape != tuple(grid.shape):
        raise ValueError(
            f"design のかたちが格子と違う: design {tuple(mask.shape)} / 格子 {tuple(grid.shape)}"
        )
    # 呼び出し側の配列と共有しない(後で書き換えられると `VoxelModel` の中身が黙って変わる)。
    design_mask = np.array(mask, dtype=bool)
    if not design_mask.any():
        raise ValueError(
            f"塊(設計空間)が空。格子 {tuple(grid.shape)} の全要素が False では最適化する場所が無い。"
        )

    dev = float(deviation) if deviation else max(grid.spacing * deviation_ratio, 1e-6)
    if isinstance(anchor_shape, TriangleSoup):
        soup = anchor_shape
    else:
        soup = triangles_from_shape(
            _unwrap(anchor_shape), deviation=dev, angular_deflection=angular_deflection
        )
    report = inspect_soup(soup)

    stats = {
        "seconds": 0.0,
        "deviation": dev,
        "grid": grid.as_dict(),
        "design_source": "mask",
        "on_outside": mode,
        "surface": report,
        "design_elements": int(np.count_nonzero(design_mask)),
        "keep_elements": 0,
        "obstacle_elements": 0,
        "keep_dropped_outside": 0,
        "obstacle_dropped_outside": 0,
    }

    raw_of: dict[str, np.ndarray] = {}
    for name, region, label in (("keep", keep, "保持"), ("obstacle", obstacle, "障害物")):
        if region is None:
            continue
        raw, sub = _voxelize_region(
            region, grid, deviation=dev, angular=angular_deflection, label=label
        )
        raw_of[name] = raw
        stats[f"{name}_surface"] = sub
        stats[f"{name}_dropped_outside"] = int(np.count_nonzero(raw & ~design_mask))

    outside = {n: int(stats[f"{n}_dropped_outside"]) for n in raw_of}
    if mode == "raise" and any(outside.values()):
        labels = {"keep": "保持", "obstacle": "障害物"}
        parts = [
            f"{labels[n]}領域が塊(設計空間)から {c} 要素はみ出している"
            f"(領域 {int(np.count_nonzero(raw_of[n]))} 要素中)"
            for n, c in outside.items()
            if c
        ]
        raise OutsideDesignSpaceError(
            "。".join(parts)
            + "。塊が条件を含むだけ大きく取れていないか、座標系・単位が違う。"
            "切り詰めてよいと分かっているなら on_outside='clip' を渡すこと(黙っては切らない)。",
            outside,
        )

    keep_mask = raw_of.get("keep", np.zeros_like(design_mask)) & design_mask
    obstacle_mask = raw_of.get("obstacle", np.zeros_like(design_mask)) & design_mask

    overlap = int(np.count_nonzero(keep_mask & obstacle_mask))
    if overlap:
        raise ValueError(
            f"保持領域と障害物領域が {overlap} 要素で重なっている。"
            "「必ず材料を残す」と「材料を入れない」は同時に満たせないので、"
            "どちらを優先するかは呼び出し側が決めること(黙って片方を勝たせない)。"
        )

    stats["keep_elements"] = int(np.count_nonzero(keep_mask))
    stats["obstacle_elements"] = int(np.count_nonzero(obstacle_mask))
    stats["seconds"] = time.perf_counter() - started
    return VoxelModel(
        grid=grid,
        design=design_mask,
        keep=keep_mask,
        obstacle=obstacle_mask,
        soup=soup,
        stats=stats,
    )


def _voxelize_region(region: Any, grid: Grid, deviation: float, angular: float, label: str):
    """保持/障害物の領域を設計空間と同じ格子に載せる。

    **格子とまったく重ならない領域は黙って空にせず拒否する。** 「材料を必ず残せ」と
    言われた領域が1要素も掴めないまま最適化が走ると、支えの無い構造がもっともらしく
    出てくる。座標系や単位の取り違えがいちばんありそうな原因なので、範囲を出して落とす。
    """
    solid = _unwrap(region)
    soup = triangles_from_shape(solid, deviation=deviation, angular_deflection=angular)
    report = inspect_soup(soup)
    grid_lo, grid_hi = grid.world_box()
    region_lo, region_hi = soup.bbox
    if np.any(region_hi < grid_lo) or np.any(region_lo > grid_hi):
        raise ValueError(
            f"{label}領域が設計空間の格子とまったく重なっていない。"
            f"{label}の範囲 {np.round(region_lo, 3).tolist()} .. {np.round(region_hi, 3).tolist()} /"
            f" 格子の範囲 {np.round(grid_lo, 3).tolist()} .. {np.round(grid_hi, 3).tolist()}。"
            "座標系か単位の取り違えの可能性が高い。"
        )
    mask, _stats = occupancy(soup, grid)
    if not mask.any():
        raise ValueError(
            f"{label}領域が1要素も掴めなかった(重なってはいるが、この要素サイズ"
            f" {grid.spacing:.6g} mm では領域の中心を1つも通らない)。"
            "解像度を上げるか領域を広げること。"
        )
    return mask, report


def _unwrap(shape: Any) -> Any:
    """build123d のオブジェクトなら `.wrapped` を取り出す。"""
    if shape is None:
        raise ValueError("shape is None")
    inner = getattr(shape, "wrapped", None)
    return inner if inner is not None else shape


def _shape_bbox(shape: Any) -> tuple[np.ndarray, np.ndarray]:
    """OCCT の bbox(`BRepBndLib::AddOptimal`)。**面のトリムを尊重する**。

    格子を起こす当たり(`voxelize`)と、格子外ガード(`design_space.derive`)の両方が使う。

    **`Add` にしてはいけない。** あちらはトリムを見ず下地のサーフェス全体で囲むので、
    トーラス面の部品が実寸の10倍以上に化け、**はみ出していない保持をはみ出したと言う**
    (実測: `models/hanger_bar_attachment.json` の 35mm 角の部品が 482.8mm と報告された)。
    円柱・平面だけの部品では両者がほとんど一致するので、**曲面の種類によって壊れ方が変わる**。

    **`useTriangulation=False` で呼ぶ。** `True` にすると、形状に**既に付いている三角形分割**
    があればそれを使うので、同じ形状オブジェクトに2回ボクセル化をかけると
    「1回目=素の幾何のbbox → 2回目以降=前回の三角形分割のbbox」と値が変わり、
    そこから決まる要素サイズ・三角形分割の細かさ・格子まで変わってしまう
    (実測: 実物STEPで三角形数が呼ぶたびに 62264 → 62168 → 62138 と動いた。
    `Cylinder(10,30)` でも1回目312枚・2回目以降308枚)。**決定性はここで壊れる**。

    **遅い**(トリム曲線を実際に評価する。実測: 1ボディ 18〜60ms)。OCCT は結果を持たない
    ので呼ぶたびに同じだけかかる —— 導出1回につき数回で済む場所から呼ぶこと。
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    # Bnd_Box は形状の公差ぶんの「隙間」を足して返す。その 1e-7 mm が要素サイズに乗ると、
    # 辺長が要素サイズの整数倍の箱で体積が厳密一致しなくなるので隙間を落とす。
    box.SetGap(0.0)
    if box.IsVoid():
        raise ValueError("bounding box is empty (null shape?)")
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return np.array([xmin, ymin, zmin]), np.array([xmax, ymax, zmax])


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="STEP/合成ソリッドをボクセル化して要約を出す")
    parser.add_argument("--step", type=str, default=None, help="STEPファイル(省略時は合成の箱)")
    parser.add_argument("--n", type=int, default=40, help="最長軸方向の要素数")
    args = parser.parse_args()

    if args.step:
        from build123d import import_step

        shape = import_step(args.step)
    else:
        from build123d import Box

        shape = Box(40, 20, 10)

    model = voxelize(shape, args.n)
    g = model.grid
    print(f"grid   : shape={g.shape} spacing={g.spacing:.6f} origin={g.origin}")
    print(f"design : {model.stats['design_elements']} elements "
          f"({model.design_volume:.3f} mm^3)")
    print(f"surface: {model.stats['surface']['triangles']} triangles, "
          f"volume={model.stats['surface']['signed_volume']:.3f} mm^3")
    print(f"seconds: {model.stats['seconds']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

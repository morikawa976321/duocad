"""B-repソリッド(OCCT)から、トポロジー最適化の設計空間になる規則格子を起こす。

    ..\\.venv\\Scripts\\python.exe check_voxelize.py     # 受入検査

これは `probe_topopt` の **stage1**。担当は「形 → 格子」だけで、境界条件を面に紐づける
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

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

# duocad 本体はvenvに入っている前提だが、入っていない環境でも読めるようにリポジトリ直下を足す。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from duocad import kernel  # noqa: E402  (sys.path を整えてから)

# レイを列の中心からずらす幅(要素サイズに対する比)。軸整列した形状で縮退が系統的に
# 起きるのを避けるためのもので、乱数ではない。2軸で違う無理数比にして、両軸が同時に
# 縮退する形状(例: 対角線上の辺)も外す。
RAY_JITTER_X = 1.0e-4
RAY_JITTER_Y = 0.6180339887498949e-4

# 既定の三角形分割の細かさ。**要素サイズに比例させる**(格子を細かくすると三角形も
# 細かくなる)。0.02 は「格子の誤差が支配的で、三角形分割の弦誤差はその20分の1」という
# 配分。受入検査が曖昧として除外する帯(要素サイズの5%)より細かいことが要る。
DEFAULT_DEVIATION_RATIO = 0.02
DEFAULT_ANGULAR_DEFLECTION = 0.2

# バウンディングボックスの周囲に取る余白(要素数)。
DEFAULT_PAD = 1

# **セル中心規約の唯一の定義**。要素 (i,j,k) の値は中心 origin + spacing*(i+CELL_CENTER_OFFSET)
# のもの。`mesh.py` の `rho[ex]` と同じで、半セルずれの原因になるのでここ以外に書かない
# (2026-08-31の変異検査で、同じ 0.5 が3箇所に散っていて1箇所を壊しても検査が緑のままだった)。
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
    """

    positions: np.ndarray
    triangles: np.ndarray
    face_index: np.ndarray
    face_kind: dict[int, str] = field(default_factory=dict)
    deviation: float = 0.0

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

    def signed_volume(self) -> float:
        """発散定理による符号付き体積。外向きに揃っていれば正。"""
        a, b, c = self.triangle_vertices()
        return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)

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
    offset = 0
    for face in payload["faces"]:
        positions = np.asarray(face["positions"], dtype=float).reshape(-1, 3)
        indices = np.asarray(face["indices"], dtype=np.int64).reshape(-1, 3)
        face_kind[int(face["index"])] = str(face.get("kind", ""))
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
    return report


# ---------------------------------------------------------------------------
# 占有判定(+zレイの巻き数)
# ---------------------------------------------------------------------------
def occupancy(
    soup: TriangleSoup,
    grid: Grid,
    *,
    jitter: tuple[float, float] = (RAY_JITTER_X, RAY_JITTER_Y),
    strict: bool = True,
) -> tuple[np.ndarray, dict]:
    """三角形群と格子から占有マスク (nx,ny,nz) の bool 配列を返す。

    各列 (i,j) の**セル中心**を通る +z のレイを飛ばし、三角形との交点を集める。
    交点には「入る(+1)/出る(-1)」の符号を付け、z 順の累積(巻き数)が 0 でない区間に
    ある**セル中心**を占有とする。詳細と、偶奇ではなく巻き数を使う理由はモジュール
    docstring を参照。

    戻り値の2つめは診断。`unbalanced_columns` は最後まで巻き数が 0 に戻らなかった列の数で、
    閉じた表面なら 0 でなければならない(`strict=True` なら 0 でないとき例外)。
    """
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
        "flat_projection": int(np.count_nonzero(flat)),
        "covers_no_column": int(np.count_nonzero(~covers)),
        "crossings": 0,
        "unbalanced_columns": 0,
    }
    if not col_chunks:
        if strict:
            raise ValueError("三角形とレイの交点が1つも無い(格子が形状を覆っていない)")
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
    # (2026-08-31の変異検査で確認済み)。それでも「列ごとの巻き数」という定義どおりに
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
    厳密な点-三角形距離(頂点・辺・内部の6分岐)を numpy でベクトル化し、点をチャンクに
    切ってメモリを抑える。頂点だけの近似(KD木)は三角形1枚ぶん距離を過大評価しうるので使わない。
    """
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f"points must be (N,3), got {pts.shape}")
    a, b, c = soup.triangle_vertices()
    ab = b - a
    ac = c - a
    bc = c - b
    if chunk is None:
        # (点数 x 三角形数 x 3) の中間配列を数MBに抑える。三角形が多いほど点を細かく切る。
        chunk = int(max(1, 200_000 // max(a.shape[0], 1)))

    dist = np.empty(pts.shape[0], dtype=float)
    which = np.empty(pts.shape[0], dtype=np.int64)
    for s in range(0, pts.shape[0], chunk):
        p = pts[s : s + chunk][:, None, :]  # (m,1,3)
        ap = p - a[None, :, :]
        bp = p - b[None, :, :]
        cp = p - c[None, :, :]

        d1 = np.einsum("tj,mtj->mt", ab, ap)
        d2 = np.einsum("tj,mtj->mt", ac, ap)
        d3 = np.einsum("tj,mtj->mt", ab, bp)
        d4 = np.einsum("tj,mtj->mt", ac, bp)
        d5 = np.einsum("tj,mtj->mt", ab, cp)
        d6 = np.einsum("tj,mtj->mt", ac, cp)
        va = d3 * d6 - d5 * d4
        vb = d5 * d2 - d1 * d6
        vc = d1 * d4 - d3 * d2

        denom = va + vb + vc
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = np.where(np.abs(denom) > 0, 1.0 / denom, 0.0)
            v_face = vb * inv
            w_face = vc * inv
            t_ab = np.where((d1 - d3) != 0, d1 / np.where((d1 - d3) != 0, d1 - d3, 1.0), 0.0)
            t_ac = np.where((d2 - d6) != 0, d2 / np.where((d2 - d6) != 0, d2 - d6, 1.0), 0.0)
            den_bc = (d4 - d3) + (d5 - d6)
            t_bc = np.where(den_bc != 0, (d4 - d3) / np.where(den_bc != 0, den_bc, 1.0), 0.0)

        closest = a[None, :, :] + ab[None, :, :] * v_face[..., None] + ac[
            None, :, :
        ] * w_face[..., None]

        # 領域ごとの上書き(Ericson "Real-Time Collision Detection" の分岐をそのまま)
        region_a = (d1 <= 0) & (d2 <= 0)
        region_b = (d3 >= 0) & (d4 <= d3)
        region_c = (d6 >= 0) & (d5 <= d6)
        region_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        region_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        region_bc = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)

        closest = np.where(
            region_bc[..., None], b[None, :, :] + bc[None, :, :] * t_bc[..., None], closest
        )
        closest = np.where(
            region_ac[..., None], a[None, :, :] + ac[None, :, :] * t_ac[..., None], closest
        )
        closest = np.where(
            region_ab[..., None], a[None, :, :] + ab[None, :, :] * t_ab[..., None], closest
        )
        closest = np.where(region_c[..., None], c[None, :, :], closest)
        closest = np.where(region_b[..., None], b[None, :, :], closest)
        closest = np.where(region_a[..., None], a[None, :, :], closest)

        d2min = np.einsum("mtj,mtj->mt", p - closest, p - closest)
        best = np.argmin(d2min, axis=1)
        dist[s : s + chunk] = np.sqrt(d2min[np.arange(best.size), best])
        which[s : s + chunk] = best
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

    # 格子を決めるための粗い bbox は、細かい三角形分割を待たずに OCCT から取る。
    if grid is None:
        lo, hi = _shape_bbox(solid)
        probe = grid_for_bbox(lo, hi, elements_long_axis, pad=pad)
    else:
        probe = grid
    dev = float(deviation) if deviation else max(probe.spacing * deviation_ratio, 1e-6)

    soup = triangles_from_shape(solid, deviation=dev, angular_deflection=angular_deflection)
    report = inspect_soup(soup)

    if grid is None:
        # 三角形の実際の bbox で格子を作り直す(弦は真の面より内側に来るので、粗い bbox の
        # ままだと余白が 1 要素からわずかにずれる)。
        lo, hi = soup.bbox
        grid = grid_for_bbox(lo, hi, elements_long_axis, pad=pad)

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
        raw, sub = _voxelize_region(keep, grid, deviation=dev, angular=angular_deflection)
        keep_mask = raw & design
        stats["keep_dropped_outside"] = int(np.count_nonzero(raw & ~design))
        stats["keep_surface"] = sub
    if obstacle is not None:
        raw, sub = _voxelize_region(obstacle, grid, deviation=dev, angular=angular_deflection)
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


def _voxelize_region(region: Any, grid: Grid, deviation: float, angular: float):
    solid = _unwrap(region)
    soup = triangles_from_shape(solid, deviation=deviation, angular_deflection=angular)
    report = inspect_soup(soup)
    mask, _stats = occupancy(soup, grid)
    return mask, report


def _unwrap(shape: Any) -> Any:
    """build123d のオブジェクトなら `.wrapped` を取り出す。"""
    if shape is None:
        raise ValueError("shape is None")
    inner = getattr(shape, "wrapped", None)
    return inner if inner is not None else shape


def _shape_bbox(shape: Any) -> tuple[np.ndarray, np.ndarray]:
    """OCCT の bbox。要素サイズ(=三角形分割の細かさ)を決めるための当たりを付けるのに使う。

    **`useTriangulation=False` で呼ぶ。** `True` にすると、形状に**既に付いている三角形分割**
    があればそれを使うので、同じ形状オブジェクトに2回ボクセル化をかけると
    「1回目=素の幾何のbbox → 2回目以降=前回の三角形分割のbbox」と値が変わり、
    そこから決まる要素サイズ・三角形分割の細かさ・格子まで変わってしまう
    (2026-08-31実測: 実物STEPで三角形数が呼ぶたびに 62264 → 62168 → 62138 と動いた。
    `Cylinder(10,30)` でも1回目312枚・2回目以降308枚)。**決定性はここで壊れる**。
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
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

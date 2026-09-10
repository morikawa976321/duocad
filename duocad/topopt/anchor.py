"""CAD の**面**と、格子の**節点**の対応表(面アンカー)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_anchor.py     # 受入検査
    .\\.venv\\Scripts\\python.exe -m duocad.topopt.anchor           # 合成ブラケットを端から端まで1回通す

これは `duocad.topopt` の **stage2**。stage1(`voxelize.py`)が作った格子の上に、
「ボディXの面#42」→「これらの格子節点」の翻訳を載せる。これが無いと固定面・荷重面を
指定できず、統合時の `add_fixture` / `add_load` verb も書けない。

## なぜボクセルの側から境界を取り直してはいけないか

ボクセルの表面は階段状で、真のCAD面から最大で半要素ずれる。「占有ボクセルの最も左の層」の
ような取り方をすると、斜面や曲面で荷重の向きと面積が歪み、階段の角に**偽の応力集中**が出る。
出てくる形は「少し悪い」のではなく「自信を持って間違っている」。

正解は**両方の表現が手元にある瞬間(=ボクセル化時)に対応を固定し、以後はその表を引く**こと。
本モジュールは `TriangleSoup`(面添字つきの三角形群)と `Grid` だけを見て表を作る。
ボクセルの占有マスクは**一切見ない**(見た瞬間に階段が混入する)。

## 契約(後続がここに依存する。壊すと黙って間違った荷重が入る)

- **節点は要素の角**である。要素格子が `(nx, ny, nz)` なら節点格子は `(nx+1, ny+1, nz+1)`。
  節点 (i,j,k) のワールド座標は `origin + spacing * (i, j, k)`(要素**中心**の
  `+0.5` は付かない。付けると半セルずれる。要素中心が要るときは `Grid.centers_of`)。
- **節点番号は `simp.py` の DOF 写像と同じ**: `n = i*(ny+1)*(nz+1) + j*(nz+1) + k`、
  自由度は `3n, 3n+1, 3n+2`(`simp._build_edof` と一致。ここがずれると荷重が
  別の場所に入り、それでも最適化は「もっともらしく」収束する)。
- **1つの節点は高々1つの面に属する**(後述の稜線規則)。面ごとの節点集合は排他である。
- **節点は「担当面積」を持つ**。荷重はこの面積比で配る。重み無しの等分は禁止 ——
  解像度を2倍にすると同じ面に4倍の節点が乗るので、等分すると総荷重か分布が黙って変わる。

## 表の作り方

1. 面 f の三角形を、辺長が `sample_ratio * spacing`(既定 0.25)以下になるまで細分し、
   各小三角形の**重心**を標本点にする。標本点は親三角形の面積を等分して持つ
   (小三角形の面積は厳密に等しいので、面積は分割で保存される)。
   細分が要るのは、平面の面が三角形2枚で返ってくるため —— 三角形の重心だけを見ると
   40x20 の面の荷重が2点に集中する。
2. 各標本点から半径 `radius = radius_ratio * spacing`(既定 1.0)の球に入る節点を拾い、
   **テント重み** `w = 1 - d/radius` を付ける。標本ごとに重みの和で正規化するので、
   面積は配っても保存される(単位の分割 = partition of unity)。
   - 既定を範囲(0.5〜1.0)の**上端**にしてあるのは、`radius >= sqrt(3)/2 * spacing`
     でないと「どの節点も球に入らない標本点」が出るため(要素の中心から最も近い節点までが
     `sqrt(3)/2 * spacing`)。そこへ落ちると最近傍1点へ全部載り、**面の置かれ方によって
     荷重が特定の節点へ集中する**(まさに避けたい失敗)。それでも念のため最近傍への
     退避は残してある(`stats["fallback_samples"]` に件数が出る)。
   - `radius_ratio` は引数で変えられる。小さくすると節点は面に貼り付くが上記の集中が出る。
3. 面 f が節点 n へ配った面積を `raw` として全部覚えておく。1つの節点が複数の面の
   球に入りうる(稜線付近)ので、**最後に持ち主を決める**。

## 稜線の規則(明文化。黙って重複させない)

**節点の持ち主は、その節点へ最も多くの面積を配った面**とする。同点のときは**面添字の
小さい面**が勝つ(決定的にするため)。したがって:

- 面ごとの節点集合は**排他**(どの節点も高々1つの面に属する)。
- 面 f が他面に取られた節点へ配った面積は**捨てる**(再正規化しない)。捨てた割合は
  `FaceAnchor.leak_ratio` に出る。再正規化しないのは、**担当面積の合計が「その面が
  実際に受け持っている面積」であってほしい**ため。捨てた分は隣の面が受け持っている。
- 荷重は「その面の節点の担当面積の比」で配るので、捨てた分があっても**指定した総荷重は
  厳密に保存される**(比の正規化で吸収される)。

重みを割って両方に配る案(節点を共有する)も考えたが採らなかった —— 固定面と荷重面が
稜線を共有していると、同じ節点が「拘束されていて、かつ荷重を受ける」ことになり、
その荷重は**黙って消える**(拘束DOFの荷重は解に効かない)。排他なら、少なくとも
どちらか一方には必ず効く。

## 荷重直下を非設計領域にする

最適化は荷重直下の材料を溶かしにいく(そこに材料が無くても計算上は荷重が入るため)。
実務上の定石どおり、アンカー節点に接する要素を保持領域(ρ=1)へ固定する口を付けてある
(`element_mask`。既定で有効)。機構は stage1 の保持領域と同じで、`simp.optimize` の
`solid_mask` へそのまま渡せる。
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from . import voxelize as vx

_HERE = Path(__file__).resolve().parent

# 標本点の目標間隔(要素サイズ比)。小さいほど分布が滑らかになり、標本点の数は2乗で増える。
DEFAULT_SAMPLE_RATIO = 0.25
# 節点を拾う球の半径(要素サイズ比)。既定を 1.0 にしてある理由はモジュール docstring。
DEFAULT_RADIUS_RATIO = 1.0
# 1三角形あたりの細分の上限(k x k 個の小三角形になる)。暴走を止めるためだけの値。
MAX_SUBDIVISION = 512
# 1回のベクトル演算で扱う標本点の上限(中間配列を数MBに抑える)。
SAMPLE_BATCH = 40_000


def _candidate_offsets(radius_ratio: float) -> np.ndarray:
    """半径 `radius_ratio * spacing` の球に入りうる節点の、格子添字のずれ。

    標本点 p が入る要素の低い側の角を `base = floor((p-origin)/h)` とすると、p の
    セル内の位置 `frac` は [0,1) にある。節点 `base+d` までの1軸ぶんの距離は
    `|d - frac| * h` なので、球に入りうるのは `d` が `[1-ceil(ρ), ceil(ρ)]` のときだけ
    (それ以外は frac をどう取っても距離が半径を超える)。既定の ρ=1 では
    **その要素の8隅だけ**になり、重みは三線形形状関数と同じ形になる。
    """
    reach = int(np.ceil(float(radius_ratio) - 1e-12))
    span = np.arange(1 - reach, reach + 1, dtype=np.int64)
    a, b, c = np.meshgrid(span, span, span, indexing="ij")
    return np.stack([a.ravel(), b.ravel(), c.ravel()], axis=1)


# ---------------------------------------------------------------------------
# 節点格子(要素格子の角)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NodeGrid:
    """要素格子 `Grid` の角にある節点の格子。番号は `simp.py` の DOF 写像と同じ。"""

    grid: vx.Grid

    @property
    def shape(self) -> tuple[int, int, int]:
        nx, ny, nz = self.grid.shape
        return (nx + 1, ny + 1, nz + 1)

    @property
    def count(self) -> int:
        nx, ny, nz = self.shape
        return nx * ny * nz

    def positions_of(self, indices: np.ndarray) -> np.ndarray:
        """(N,3) の節点添字 → (N,3) のワールド座標。**要素中心の +0.5 は付かない。**"""
        idx = np.asarray(indices, dtype=float)
        if idx.ndim != 2 or idx.shape[1] != 3:
            raise ValueError(f"indices must be (N,3), got {idx.shape}")
        return np.asarray(self.grid.origin, dtype=float) + self.grid.spacing * idx

    def ids_of(self, indices: np.ndarray) -> np.ndarray:
        """(N,3) の節点添字 → `simp.py` と同じ通し番号 (N,)。"""
        idx = np.asarray(indices, dtype=np.int64)
        if idx.ndim != 2 or idx.shape[1] != 3:
            raise ValueError(f"indices must be (N,3), got {idx.shape}")
        _, ny1, nz1 = self.shape
        return idx[:, 0] * (ny1 * nz1) + idx[:, 1] * nz1 + idx[:, 2]

    def indices_of_ids(self, ids: np.ndarray) -> np.ndarray:
        """通し番号 → (N,3) の節点添字(`ids_of` の逆)。"""
        flat = np.asarray(ids, dtype=np.int64)
        _, ny1, nz1 = self.shape
        i = flat // (ny1 * nz1)
        rest = flat - i * (ny1 * nz1)
        j = rest // nz1
        k = rest - j * nz1
        return np.stack([i, j, k], axis=1)

    def dofs_of(self, ids: np.ndarray, axes: Sequence[int] = (0, 1, 2)) -> np.ndarray:
        """通し番号 → 自由度番号。`axes` で成分を絞れる(すべり支持など)。"""
        flat = np.asarray(ids, dtype=np.int64)
        parts = [3 * flat + int(a) for a in axes]
        if not parts:
            return np.zeros(0, dtype=np.int64)
        return np.unique(np.concatenate(parts))

    @property
    def ndof(self) -> int:
        return 3 * self.count


# ---------------------------------------------------------------------------
# 1面ぶんのアンカー
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FaceAnchor:
    """面 `face_index` に対応づいた節点と、その担当面積。

    `nodes` / `node_ids` / `areas` / `normals` は**持ち主が確定した後**の集合(排他)。
    `raw_*` は持ち主を決める前に「この面が配った先」で、稜線の規則を検算するために残してある。
    `cad_*` は B-rep から直に取った真値(三角形分割の近似ではない)。
    """

    face_index: int
    kind: str
    nodes: np.ndarray          # (M,3) 節点添字
    node_ids: np.ndarray       # (M,)  通し番号
    areas: np.ndarray          # (M,)  担当面積 [mm^2]
    normals: np.ndarray        # (M,3) 面積重み平均の単位法線
    raw_node_ids: np.ndarray   # (R,)  持ち主を決める前に配った先
    raw_areas: np.ndarray      # (R,)
    triangle_area: float       # この面の三角形の総面積(細分前と厳密に同じ)
    cad_area: float
    cad_centroid: np.ndarray
    cad_normal: np.ndarray

    @property
    def node_count(self) -> int:
        return int(self.nodes.shape[0])

    @property
    def total_area(self) -> float:
        """担当面積の合計。稜線で隣の面に取られた分は入らない。"""
        return float(self.areas.sum())

    @property
    def leak_ratio(self) -> float:
        """この面が配った面積のうち、隣の面に持っていかれた割合。"""
        raw = float(self.raw_areas.sum())
        return 0.0 if raw <= 0.0 else float(1.0 - self.areas.sum() / raw)

    def centroid(self, node_grid: NodeGrid) -> np.ndarray:
        """担当面積で重み付けした節点集合の重心。解像度を上げると真の面の重心へ収束する。"""
        if self.areas.sum() <= 0.0:
            raise ValueError(f"face {self.face_index}: 担当面積が 0 で重心を出せない")
        pts = node_grid.positions_of(self.nodes)
        return np.asarray((pts * self.areas[:, None]).sum(axis=0) / self.areas.sum())

    def mean_normal(self) -> np.ndarray:
        """担当面積で重み付けした平均法線(正規化前の長さは向きの揃い具合を表す)。"""
        if self.areas.sum() <= 0.0:
            raise ValueError(f"face {self.face_index}: 担当面積が 0 で法線を出せない")
        vec = (self.normals * self.areas[:, None]).sum(axis=0) / self.areas.sum()
        return np.asarray(vec)

    def weights(self) -> np.ndarray:
        """担当面積を合計1に正規化した重み。**荷重はこれで配る。**"""
        total = self.areas.sum()
        if not total > 0.0:
            raise ValueError(f"face {self.face_index}: 担当面積が 0 で荷重を配れない")
        return self.areas / total


# ---------------------------------------------------------------------------
# 対応表
# ---------------------------------------------------------------------------
@dataclass
class AnchorTable:
    """面添字 → 節点集合の対応表。**これが stage2 の成果物**で、後続はこの表を引く。"""

    grid: vx.Grid
    node_grid: NodeGrid
    anchors: dict[int, FaceAnchor]
    radius: float
    stats: dict = field(default_factory=dict)

    # -- 引き方 -------------------------------------------------------------
    def face_indices(self) -> list[int]:
        return sorted(self.anchors)

    def empty_faces(self) -> list[int]:
        """節点が1つも付かなかった面(要素サイズより小さい面)。指定する前に見るための口。"""
        return [f for f in self.face_indices() if self.anchors[f].node_count == 0]

    def empty_face_diagnosis(self, face_index: int) -> dict:
        """`face_index` が空になった理由の当たりと、逃げ道2つの数字。**空でない面は例外**
        (呼び出し側の勘違いを早く拾うため —— 空の面だけを渡す前提の口)。

        - `absorbed_by_faces`: 稜線の規則(最も多く面積を配った面が持ち主)でこの面の
          `raw_node_ids`(持ち主を決める前に配った先)を実際に持っていった面。**この面を
          指し直せば拘束・荷重が乗る**という具体的な逃げ道1。標本点が1つも節点の球に
          入らなかった(=raw_node_ids も空の)極端に小さい面では特定できず、空リストになる。
        - `representative_length_mm` = `sqrt(cad_area)`。**近似**(正方形とみなした代表寸法。
          曲面・極端な縦横比の面では粗い)。逃げ道2「要素サイズをこれ以下にすれば節点が付く
          見込み」として `suggested_max_spacing_mm` にも同じ値を出す —— 保証ではなく目安
          (実際に付くかは対象の要素サイズで `empty_faces()` を取り直して確認すること)。
        """
        anchor = self[int(face_index)]
        if anchor.node_count != 0:
            raise ValueError(
                f"面 {face_index} は空でない({anchor.node_count} 節点)。"
                "empty_face_diagnosis は empty_faces() に入っている面だけを受け付ける"
            )
        raw_ids = np.unique(anchor.raw_node_ids)
        absorbed_by: set[int] = set()
        if raw_ids.size > 0:
            for f, other in self.anchors.items():
                if f == anchor.face_index or other.node_count == 0:
                    continue
                if np.intersect1d(raw_ids, other.node_ids, assume_unique=False).size > 0:
                    absorbed_by.add(int(f))
        area = float(anchor.cad_area)
        representative_length = math.sqrt(area) if area > 0.0 else float("nan")
        return {
            "face_index": int(face_index),
            "kind": anchor.kind,
            "cad_area_mm2": area,
            "element_size_mm": float(self.grid.spacing),
            "representative_length_mm": representative_length,
            "suggested_max_spacing_mm": representative_length,
            "absorbed_by_faces": sorted(absorbed_by),
        }

    def condition_face_diagnoses(self, fixed_faces: Any, load_faces: Any) -> list[dict]:
        """拘束面・荷重面のうち `empty_faces()` に入っているものだけの診断(側の別つき)。

        `run_pipeline` の入口で使う。**1枚も空でなければ空リスト** —— 通常経路の
        戻り値には影響しない(新しい通知の仕組みではなく、既存の `empty_faces()` を
        機械可読な形に整えるだけの薄い層)。面は `int` 1つでも並びでも受ける。
        """
        empty = set(self.empty_faces())
        out: list[dict] = []
        for side, faces in (("fixed", fixed_faces), ("load", load_faces)):
            for face in _face_list(faces):
                if int(face) in empty:
                    d = self.empty_face_diagnosis(face)
                    d["side"] = side
                    out.append(d)
        return out

    def __getitem__(self, face_index: int) -> FaceAnchor:
        try:
            return self.anchors[int(face_index)]
        except KeyError:
            raise KeyError(
                f"面 {face_index} のアンカーが無い。ある面: {self.face_indices()}"
            ) from None

    def _selected(self, faces: Iterable[int] | int) -> list[FaceAnchor]:
        if isinstance(faces, (int, np.integer)):
            faces = [int(faces)]
        picked = [self[int(f)] for f in faces]
        if not picked:
            raise ValueError("面が1つも指定されていない")
        return picked

    def node_ids(self, faces: Iterable[int] | int) -> np.ndarray:
        """指定した面の節点の通し番号(昇順・重複なし)。面ごとの集合は排他なので単純に連結できる。

        **節点が1つも付かなかった面が1つでも混ざっていたら落とす。** 要素サイズより小さい面
        (フィレット・面取り・小さな段差)は、隣の大きい面に節点を全部持っていかれて
        空になる(実測: 231面のSTEPを要素サイズ1.54mmで割ると22面が空)。黙って空の
        集合を返すと、**拘束の無い系や荷重0の系がそのまま最適化に入る**。
        """
        picked = self._selected(faces)
        ids = np.concatenate([a.node_ids for a in picked]) if picked else np.zeros(0, int)
        out = np.unique(ids)
        if out.size != ids.size:
            raise AssertionError(
                "面ごとの節点集合が排他でない(稜線の規則が壊れている)"
            )
        self._require_nodes(picked)
        return out

    def _require_nodes(self, picked: list[FaceAnchor]) -> None:
        """空の面が**1つでも**混ざっていたら落とす(`node_ids` と荷重の口で共有する)。

        **合計で見てはならない。** 大きい面と小さい面を一緒に指すと合計は正になり、
        小さい面だけが黙って落ちる —— 2面を固定したつもりで1面しか効いていない系が、
        例外も通知も無いまま最適化へ入る。
        """
        empty = [a for a in picked if a.node_count == 0]
        if not empty:
            return
        names = ", ".join(
            f"面{a.face_index}({a.kind}, CAD面積 {a.cad_area:.4g} mm^2)" for a in empty
        )
        raise ValueError(
            f"指定した面に格子節点が1つも付いていない: {names}。"
            f"要素サイズ {self.grid.spacing:.6g} mm に対して面が小さすぎて、"
            "稜線の規則で隣の面が節点を全部取った可能性が高い。"
            "解像度を上げるか、隣接する大きい面を指定すること"
        )

    def nodes(self, faces: Iterable[int] | int) -> np.ndarray:
        """指定した面の節点添字 (M,3)。"""
        return self.node_grid.indices_of_ids(self.node_ids(faces))

    def positions(self, faces: Iterable[int] | int) -> np.ndarray:
        """指定した面の節点のワールド座標 (M,3)。"""
        return self.node_grid.positions_of(self.nodes(faces))

    # -- 境界条件 -----------------------------------------------------------
    def fixed_dofs(
        self, faces: Iterable[int] | int, axes: Sequence[int] = (0, 1, 2)
    ) -> np.ndarray:
        """固定面 → 拘束する自由度番号。`axes` を絞ればすべり支持になる。"""
        return self.node_grid.dofs_of(self.node_ids(faces), axes=axes)

    def load_vector(
        self,
        faces: Iterable[int] | int,
        total_force: Sequence[float],
        *,
        out: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """荷重面 → 荷重ベクトル (ndof,)。**合計は `total_force` に厳密に一致する。**

        面が複数のときは、面の担当面積の合計比で面へ配ってから面内で配る
        (= 全節点の担当面積の比で配るのと同じ)。解像度を変えても総和は変わらない。
        """
        picked = self._selected(faces)
        force = np.asarray(total_force, dtype=float).reshape(3)
        ids = np.concatenate([a.node_ids for a in picked])
        areas = np.concatenate([a.areas for a in picked])
        self._require_nodes(picked)
        total = areas.sum()
        if not total > 0.0:
            raise ValueError(
                f"担当面積の合計が 0 で荷重を配れない(節点は {ids.size} 個ある)"
            )
        weights = areas / total
        f = np.zeros(self.node_grid.ndof) if out is None else out
        for axis in range(3):
            if force[axis] == 0.0:
                continue
            np.add.at(f, 3 * ids + axis, weights * force[axis])
        return f

    def pressure_vector(
        self,
        faces: Iterable[int] | int,
        pressure: float,
        *,
        out: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """圧力 [N/mm^2] を面法線の**逆**向き(面を押す向き)に載せた荷重ベクトル。

        節点力 = `-pressure * (cad_area/担当面積の合計) * 担当面積 * 節点法線`。
        向きの分布は担当面積の重みのまま(=節点への配り方は変えない)で、
        **総量だけを「面の真の面積」に合わせ直す**。

        **なぜ担当面積をそのまま使わないか。** 稜線の規則で節点を隣の面に取られたぶん、
        担当面積の合計は真の面積より数%小さい(実測: 24x24 の面で 10.7%、要素サイズ1.875mm)。
        `load_vector` は「指定した合力」を担当面積の**比**で配るので、この差は正規化で
        吸収されて消える。しかし圧力は `p x 面積` が合力そのものなので、**吸収されずに
        そのまま合力の不足になる**(粗い格子ほど大きく、解像度を上げると黙って合力が増える)。
        面の真の面積は B-rep から取れている(`FaceAnchor.cad_area`)ので、そこから出す。

        担当面積の**値**も所有の規則も変えていない。変えるのはこの関数が作る合力だけ。
        平面の面なら合力の大きさは厳密に `pressure * cad_area`(受入検査[1]が相対1e-12で見る)。
        曲面では `p * ∫n dA` の離散近似になる(それが圧力の意味であって、
        大きさが `p * cad_area` になるわけではない)。
        """
        picked = self._selected(faces)
        self._require_nodes(picked)
        f = np.zeros(self.node_grid.ndof) if out is None else out
        for anchor in picked:
            assigned = float(anchor.areas.sum())
            if not assigned > 0.0:
                raise ValueError(
                    f"面 {anchor.face_index}: 担当面積の合計が 0 で圧力を配れない"
                )
            true_area = float(anchor.cad_area)
            if not true_area > 0.0:
                # `face_props` を持たない TriangleSoup(検査が手で組んだもの等)への保険。
                # 三角形の総面積も真の面積の近似なので、無いよりはよい。
                true_area = float(anchor.triangle_area)
            if not true_area > 0.0:
                raise ValueError(
                    f"面 {anchor.face_index}: 真の面積が取れないので圧力の合力を決められない"
                )
            scale = -float(pressure) * (true_area / assigned)
            vec = scale * anchor.areas[:, None] * anchor.normals
            for axis in range(3):
                np.add.at(f, 3 * anchor.node_ids + axis, vec[:, axis])
        return f

    def element_mask(
        self, faces: Iterable[int] | int, layers: int = 1
    ) -> np.ndarray:
        """アンカー節点に接する要素の bool マスク (nx,ny,nz)。非設計領域に固定するのに使う。

        `layers=1` は「その節点を角に持つ要素」。2以上は各軸へその分だけ膨らませる。
        荷重・拘束の近傍を ρ=1 に固定するのが実務上の定石で、これをやらないと最適化が
        荷重直下の材料を溶かして「荷重が宙に浮いた」形が出る。
        """
        if int(layers) < 1:
            raise ValueError(f"layers must be >= 1, got {layers!r}")
        nx, ny, nz = self.grid.shape
        mask = np.zeros((nx, ny, nz), dtype=bool)
        idx = self.nodes(faces)
        for da in (-1, 0):
            for db in (-1, 0):
                for dc in (-1, 0):
                    e = idx + np.array([da, db, dc])
                    ok = np.all((e >= 0) & (e < np.array([nx, ny, nz])), axis=1)
                    e = e[ok]
                    mask[e[:, 0], e[:, 1], e[:, 2]] = True
        for _ in range(int(layers) - 1):
            grown = mask.copy()
            for axis in range(3):
                lo = [slice(None)] * 3
                hi = [slice(None)] * 3
                lo[axis] = slice(1, None)
                hi[axis] = slice(None, -1)
                grown[tuple(lo)] |= mask[tuple(hi)]
                grown[tuple(hi)] |= mask[tuple(lo)]
            mask = grown
        return mask


# ---------------------------------------------------------------------------
# 標本点(三角形の細分)
# ---------------------------------------------------------------------------
def _unit_triangle_centroids(k: int) -> np.ndarray:
    """単位三角形を k x k 個の等面積の小三角形へ割ったときの重心の重心座標 (k^2, 2)。"""
    ii, jj = np.meshgrid(np.arange(k), np.arange(k), indexing="ij")
    # 上向きの小三角形: 頂点 (j,i) (j+1,i) (j,i+1) / k  →  i + j <= k-1
    up = (ii + jj) <= k - 1
    out = [np.stack([(3 * jj[up] + 1) / (3.0 * k), (3 * ii[up] + 1) / (3.0 * k)], axis=1)]
    # 下向きの小三角形: 頂点 (j+1,i) (j,i+1) (j+1,i+1) / k  →  i + j <= k-2
    down = (ii + jj) <= k - 2
    if np.any(down):
        out.append(
            np.stack([(3 * jj[down] + 2) / (3.0 * k), (3 * ii[down] + 2) / (3.0 * k)], axis=1)
        )
    return np.concatenate(out, axis=0)


def _iter_samples(
    a: np.ndarray,
    b: np.ndarray,
    c: np.ndarray,
    target: float,
    max_subdivision: int = MAX_SUBDIVISION,
    batch: int = SAMPLE_BATCH,
):
    """三角形群を細分して、(点 (P,3), 面積 (P,), 親三角形の添字 (P,)) をまとめて返す。

    小三角形の面積はすべて等しい(親の面積 / k^2)ので、**面積は細分で厳密に保存される**。
    """
    ab = b - a
    ac = c - a
    area = 0.5 * np.linalg.norm(np.cross(ab, ac), axis=1)
    edge = np.maximum(
        np.maximum(np.linalg.norm(ab, axis=1), np.linalg.norm(ac, axis=1)),
        np.linalg.norm(c - b, axis=1),
    )
    k = np.ceil(np.where(target > 0, edge / target, 1.0) - 1e-9).astype(np.int64)
    k = np.clip(k, 1, int(max_subdivision))
    for kv in np.unique(k):
        members = np.flatnonzero(k == kv)
        uv = _unit_triangle_centroids(int(kv))
        per = uv.shape[0]
        step = max(1, int(batch) // max(per, 1))
        for s in range(0, members.size, step):
            grp = members[s : s + step]
            pts = (
                a[grp][:, None, :]
                + uv[None, :, 0, None] * ab[grp][:, None, :]
                + uv[None, :, 1, None] * ac[grp][:, None, :]
            )
            areas = np.repeat(area[grp] / (kv * kv), per)
            parents = np.repeat(grp, per)
            yield pts.reshape(-1, 3), areas, parents


# ---------------------------------------------------------------------------
# 表の構築
# ---------------------------------------------------------------------------
def build_anchors(
    soup: vx.TriangleSoup,
    grid: vx.Grid,
    *,
    faces: Optional[Iterable[int]] = None,
    radius_ratio: float = DEFAULT_RADIUS_RATIO,
    sample_ratio: float = DEFAULT_SAMPLE_RATIO,
) -> AnchorTable:
    """面添字 → 節点の対応表を作る。

    `faces` を渡すとその面だけを対象にする(既定は全面)。`radius_ratio` は節点を拾う球の
    半径(要素サイズ比)、`sample_ratio` は三角形を細分するときの目標辺長(要素サイズ比)。
    """
    started = time.perf_counter()
    if not radius_ratio > 0:
        raise ValueError(f"radius_ratio must be positive, got {radius_ratio!r}")
    if not sample_ratio > 0:
        raise ValueError(f"sample_ratio must be positive, got {sample_ratio!r}")

    node_grid = NodeGrid(grid)
    h = float(grid.spacing)
    radius = float(radius_ratio) * h
    offsets = _candidate_offsets(radius_ratio)
    origin = np.asarray(grid.origin, dtype=float)
    n_shape = np.asarray(node_grid.shape, dtype=np.int64)
    ny1, nz1 = int(n_shape[1]), int(n_shape[2])

    a_all, b_all, c_all = soup.triangle_vertices()
    tri_normal = np.cross(b_all - a_all, c_all - a_all)
    tri_len = np.linalg.norm(tri_normal, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        tri_unit = np.where(
            tri_len[:, None] > 0, tri_normal / np.where(tri_len[:, None] > 0, tri_len[:, None], 1.0), 0.0
        )

    if faces is None:
        wanted = sorted({int(v) for v in np.unique(soup.face_index)})
    else:
        wanted = sorted({int(v) for v in faces})
        missing = [f for f in wanted if not np.any(soup.face_index == f)]
        if missing:
            raise ValueError(
                f"三角形を1枚も持たない面が指定された: {missing}。"
                f"ある面: {sorted({int(v) for v in np.unique(soup.face_index)})}"
            )

    # 節点ごとの累積用の作業領域(面ごとに使い回し、触った所だけ戻す)。
    n_nodes = node_grid.count
    acc_area = np.zeros(n_nodes, dtype=float)
    acc_normal = np.zeros((n_nodes, 3), dtype=float)

    raw: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    tri_area_of_face: dict[int, float] = {}
    fallback_samples = 0
    sample_total = 0

    for f in wanted:
        tris = soup.triangles_of_face(f)
        a, b, c = a_all[tris], b_all[tris], c_all[tris]
        normals = tri_unit[tris]
        tri_area_of_face[f] = float(
            0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum()
        )
        touched: list[np.ndarray] = []
        for pts, areas, parents in _iter_samples(a, b, c, target=sample_ratio * h):
            sample_total += pts.shape[0]
            base = np.floor((pts - origin) / h).astype(np.int64)
            cand = base[:, None, :] + offsets[None, :, :]           # (P,C,3)
            valid = np.all((cand >= 0) & (cand < n_shape), axis=2)  # (P,C)
            cand_clipped = np.clip(cand, 0, n_shape - 1)
            node_pos = origin + h * cand_clipped
            dist = np.linalg.norm(node_pos - pts[:, None, :], axis=2)
            w = np.where(valid, np.maximum(0.0, 1.0 - dist / radius), 0.0)
            wsum = w.sum(axis=1)
            empty = wsum <= 0.0
            if np.any(empty):
                # 球に節点が1つも入らなかった標本点。最近傍1点へ退避する
                # (radius >= sqrt(3)/2*spacing なら起こらない)。
                fallback_samples += int(np.count_nonzero(empty))
                far = np.where(valid[empty], dist[empty], np.inf)
                nearest = np.argmin(far, axis=1)
                w[empty] = 0.0
                w[np.flatnonzero(empty), nearest] = 1.0
                wsum = w.sum(axis=1)
            w = w / wsum[:, None]
            ids = (
                cand_clipped[:, :, 0] * (ny1 * nz1)
                + cand_clipped[:, :, 1] * nz1
                + cand_clipped[:, :, 2]
            )
            contrib = w * areas[:, None]
            flat_ids = ids.ravel()
            flat_contrib = contrib.ravel()
            keep = flat_contrib > 0.0
            flat_ids = flat_ids[keep]
            flat_contrib = flat_contrib[keep]
            np.add.at(acc_area, flat_ids, flat_contrib)
            nrm = np.repeat(normals[parents], offsets.shape[0], axis=0)[keep]
            np.add.at(acc_normal, flat_ids, nrm * flat_contrib[:, None])
            touched.append(flat_ids)

        if touched:
            hit = np.unique(np.concatenate(touched))
        else:
            hit = np.zeros(0, dtype=np.int64)
        raw[f] = (hit, acc_area[hit].copy(), acc_normal[hit].copy())
        acc_area[hit] = 0.0
        acc_normal[hit] = 0.0

    # --- 稜線の規則: 最も多く面積を配った面が持ち主。同点は面添字の小さい面。 ---
    owner = np.full(n_nodes, -1, dtype=np.int64)
    owner_area = np.zeros(n_nodes, dtype=float)
    for f in wanted:  # 昇順に見るので、同点のときは先に置いた(小さい)面が残る
        ids, areas, _ = raw[f]
        better = areas > owner_area[ids]
        owner[ids[better]] = f
        owner_area[ids[better]] = areas[better]

    anchors: dict[int, FaceAnchor] = {}
    contested = 0
    for f in wanted:
        ids, areas, nsum = raw[f]
        mine = owner[ids] == f
        my_ids = ids[mine]
        my_areas = areas[mine]
        my_nsum = nsum[mine]
        length = np.linalg.norm(my_nsum, axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            my_normal = np.where(
                length[:, None] > 0, my_nsum / np.where(length[:, None] > 0, length[:, None], 1.0), 0.0
            )
        props = soup.face_props.get(f, {})
        anchors[f] = FaceAnchor(
            face_index=f,
            kind=str(props.get("kind", soup.face_kind.get(f, ""))),
            nodes=node_grid.indices_of_ids(my_ids),
            node_ids=my_ids,
            areas=my_areas,
            normals=my_normal,
            raw_node_ids=ids,
            raw_areas=areas,
            triangle_area=tri_area_of_face[f],
            cad_area=float(props.get("area", 0.0)),
            cad_centroid=np.asarray(props.get("centroid", (0.0, 0.0, 0.0)), dtype=float),
            cad_normal=np.asarray(props.get("normal", (0.0, 0.0, 0.0)), dtype=float),
        )
        contested += int(np.count_nonzero(~mine))

    stats = {
        "seconds": time.perf_counter() - started,
        "faces": len(anchors),
        "radius": radius,
        "radius_ratio": float(radius_ratio),
        "sample_ratio": float(sample_ratio),
        "samples": int(sample_total),
        "fallback_samples": int(fallback_samples),
        "contested_assignments": int(contested),
        "anchor_nodes": int(sum(a.node_count for a in anchors.values())),
        "empty_faces": [f for f in sorted(anchors) if anchors[f].node_count == 0],
        "node_shape": list(node_grid.shape),
    }
    return AnchorTable(
        grid=grid, node_grid=node_grid, anchors=anchors, radius=radius, stats=stats
    )


def anchors_for(
    model: vx.VoxelModel,
    *,
    faces: Optional[Iterable[int]] = None,
    radius_ratio: float = DEFAULT_RADIUS_RATIO,
    sample_ratio: float = DEFAULT_SAMPLE_RATIO,
) -> AnchorTable:
    """`voxelize()` の戻り値からそのまま対応表を作る(格子と三角形群は既に揃っている)。"""
    return build_anchors(
        model.soup,
        model.grid,
        faces=faces,
        radius_ratio=radius_ratio,
        sample_ratio=sample_ratio,
    )


def faces_matching(
    soup: vx.TriangleSoup,
    *,
    kind: Optional[str] = None,
    normal: Optional[Sequence[float]] = None,
    normal_tol: float = 1e-6,
    near_point: Optional[Sequence[float]] = None,
    near_tol: float = 1e-6,
) -> list[int]:
    """面を条件で拾う小さな道具(検査とデモが「あの面」を名指しするために使う)。

    実運用では人が画面でクリックした面添字が来るので、この関数は**検査の便宜**でしかない。
    """
    out = []
    for index, props in sorted(soup.face_props.items()):
        if not np.any(soup.face_index == index):
            continue
        if kind is not None and str(props.get("kind", "")) != kind:
            continue
        if normal is not None:
            want = np.asarray(normal, dtype=float)
            want = want / np.linalg.norm(want)
            got = np.asarray(props["normal"], dtype=float)
            if np.linalg.norm(got) <= 0 or np.dot(got / np.linalg.norm(got), want) < 1.0 - normal_tol:
                continue
        if near_point is not None:
            if np.linalg.norm(np.asarray(props["centroid"], dtype=float) - np.asarray(near_point, dtype=float)) > near_tol:
                continue
        out.append(int(index))
    return out


# ---------------------------------------------------------------------------
# 合成ブラケット(検査とビューアが共有する題材)
# ---------------------------------------------------------------------------
def demo_bracket():
    """設計空間になる合成ソリッド。**私物STEPに依存しない**(受入検査の制約)。

    x方向に伸びた箱の、先端の上に小さな耳(lug)を生やした形。
    - 固定面: 箱の x が最小の面(面積 24x24)
    - 荷重面: 耳の上面(面積 8x8)。箱の面とは別の面添字になるので、
      「小さい面を名指しして荷重を載せる」という stage2 の主眼がそのまま試せる。
    """
    from build123d import Box, Pos

    body = Box(60, 24, 24)
    lug = Pos(26, 0, 15) * Box(8, 8, 6)
    return body + lug


def demo_faces(soup: vx.TriangleSoup) -> tuple[int, int]:
    """`demo_bracket()` の (固定面, 荷重面) の面添字を、位置から決定的に選ぶ。"""
    fixed = faces_matching(soup, kind="plane", normal=(-1, 0, 0), near_point=(-30, 0, 0), near_tol=1e-6)
    load = faces_matching(soup, kind="plane", normal=(0, 0, 1), near_point=(26, 0, 18), near_tol=1e-6)
    if len(fixed) != 1 or len(load) != 1:
        raise ValueError(
            f"デモの面を一意に選べなかった(固定={fixed} 荷重={load})。demo_bracket を変えたら見直すこと"
        )
    return fixed[0], load[0]


def format_condition_face_warning(diagnoses: list[dict]) -> str:
    """`AnchorTable.condition_face_diagnoses` の結果を利用者向けの1メッセージにする。

    `run_pipeline` / `design_space.run_topopt` が空リスト(=空の条件面なし)ならこの関数を
    呼ばない。呼ぶのは1件以上あるときだけを想定している(空リストを渡すと空文字列)。
    """
    parts = []
    for d in diagnoses:
        side_ja = "拘束面" if d["side"] == "fixed" else "荷重面"
        rep = d["representative_length_mm"]
        rep_txt = f"約{rep:.3g}mm" if math.isfinite(rep) else "不明"
        if d["absorbed_by_faces"]:
            escape1 = f"別の面(面{d['absorbed_by_faces']})を選ぶ"
        else:
            escape1 = "別の(もっと大きい)面を選ぶ"
        parts.append(
            f"{side_ja}(面{d['face_index']})に格子節点が1つも付いていない: "
            f"CAD面積 {d['cad_area_mm2']:.4g} mm^2(代表寸法 {rep_txt})が"
            f"要素サイズ {d['element_size_mm']:.4g} mm に対して小さすぎる。"
            f"このままでは{side_ja}の条件が最適化に伝わらない。"
            f"逃げ道: {escape1}か、要素サイズを{rep_txt}以下にする"
            "(節点が付く保証はない目安 — 実際に付くかはその要素サイズで確認すること)。"
        )
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# 端から端まで(ソリッド → 格子 → 面アンカー → SIMP → marching cubes → STL)
# ---------------------------------------------------------------------------
def _face_list(value: Any) -> list:
    """`int` 1つでも面の並びでも受けて、面添字のリストにする。

    **既存の呼び出し(デモ・検査)が `int` を渡し続けられるようにするため**の入口。
    ここで均さないと「面が1枚の場合」の分岐が呼び出し側に散らばる。
    """
    if isinstance(value, (int, np.integer)):
        return [int(value)]
    return [int(item) for item in value]


def _assemble_load(
    table: "AnchorTable",
    load_faces: Sequence[int],
    force: Sequence[float],
    forces: Optional[Sequence[Sequence[float]]],
) -> tuple:
    """荷重面 → **無次元の荷重ベクトル**と、その実単位の総量。

    `forces` を渡すと面ごとに違う力ベクトル[N]を足し込み、`force` だけなら全ての荷重面が
    1本の向きを共有する(面積比で配るのは `load_vector` の役目で、そこは変えていない)。

    **無次元化の分母は「面ごとの大きさの総和」であって合力ではない。** 向きの違う荷重が
    打ち消し合うと合力(ベクトル和)は0になりうるが、総和は0にならない。`stress` 側は
    `load_magnitude_total` が解いたベクトルから同じ量を測っており、**2つの定義が食い違うと
    応力が黙って定数倍ずれる**。荷重が1本のとき(または全部同じ向きのとき)は
    `load_vector` が全節点へ平行なベクトルを配るので、総和と合力は厳密に一致する ——
    それが「1荷重の数値は1ビットも動かない」の根拠である。
    """
    if forces is None:
        vector = table.load_vector(load_faces, force)
        return vector, float(np.linalg.norm(np.asarray(force, dtype=float).reshape(3)))

    vector = np.zeros(table.node_grid.ndof)
    total = 0.0
    for face, one in zip(load_faces, forces):
        magnitude = float(np.linalg.norm(np.asarray(one, dtype=float).reshape(3)))
        if not magnitude > 0.0:
            raise ValueError(f"面 {int(face)} の荷重が零ベクトルで、向きが決まらない")
        table.load_vector([int(face)], one, out=vector)
        total += magnitude
    if not total > 0.0:
        raise ValueError("荷重の総量が0で、無次元化できない")
    vector /= total
    return vector, total


def reaction_payload(fixed_faces: Sequence[int], reactions: Sequence[Any]) -> list[dict]:
    """`simp.Reaction` の並び → **素の値だけの dict の並び**(そのまま JSON へ載る)。

    `optimize_run.run` の戻り値は素の値だけという契約なので、`simp` のデータクラスを
    上の層へ通さない。並びは `fixed_faces` と同じ。
    """
    return [
        {
            "name": reaction.name,
            "face": int(face),
            "force": [float(value) for value in reaction.force],
            "magnitude": float(reaction.magnitude),
            "fraction": float(reaction.fraction),
        }
        for face, reaction in zip(fixed_faces, reactions)
    ]


def run_pipeline(
    shape: Any = None,
    *,
    elements_long_axis: int = 40,
    volfrac: Optional[float] = None,
    material_mm3: Optional[float] = None,
    rmin: Optional[float] = None,
    rmin_mm: Optional[float] = None,
    max_iter: int = 40,
    force: Sequence[float] = (0.0, 0.0, -1.0),
    radius_ratio: float = DEFAULT_RADIUS_RATIO,
    keep_anchor_material: bool = True,
    keep_layers: int = 1,
    fixed_faces: Any = None,
    load_faces: Any = None,
    forces: Optional[Sequence[Sequence[float]]] = None,
    design_mask: Any = None,
    design_grid: Optional[vx.Grid] = None,
    keep: Any = None,
    obstacle: Any = None,
    stl_path: Optional[str | Path] = None,
    progress: Any = None,
    solver: Optional[str] = None,
    threads: Optional[int] = None,
    update: Optional[str] = None,
    reaction_min: Optional[float] = None,
    keep_model: bool = False,
) -> dict:
    """ソリッドを端から端まで1回通す。戻り値は各段の実測と成果物。

    `shape` を省略すると `demo_bracket()` を使い、面も `demo_faces()` が決める。
    **自分の形を渡すときは `fixed_faces` / `load_faces` も渡すこと** ——
    面添字は形ごとに違うので、デモの選び方を他の形へ当てると別の面を掴む。
    どちらも `int` 1つでも面の並びでも受ける。

    荷重の向きは `force`(荷重面すべてで共有する1本)か `forces`(`load_faces` と**同じ並び**の
    面ごとの力ベクトル[N])のどちらか一方。`forces` を渡すと荷重ベクトルは面ごとに
    足し込まれ、**その総量(大きさの総和)で割って無次元化してから** `simp` へ渡る ——
    `simp` が荷重の総量1.0で回り続ける契約は変えない。実単位[N]は
    `stress.UnitScale`(`load_n`)だけが持つ。

    `keep_anchor_material=True`(既定)のとき、固定面・荷重面のアンカー節点に接する要素を
    保持領域(ρ=1)に固定する。**これを切ると最適化は荷重直下の材料を溶かす。**

    `design_mask` と `design_grid` を両方渡すと、**塊(設計空間)はそのマスクになり、`shape` は
    面アンカーの対象になる**(`vx.model_from_design_mask` を通る)。この経路では材料は入力
    ソリッドの外へ出られる。`shape` には面を選ぶのに使った `TriangleSoup` をそのまま渡してよい。
    塊をどのロジックで決めるかはここでは決めない —— 呼び出し側が渡す。

    `keep` / `obstacle`(保持ソリッド / 除外ソリッド)はどちらの経路でも同じ格子に載る。
    `design_mask` の経路では**塊からはみ出したら例外**(`vx.OutsideDesignSpaceError`)。

    体積目標は `volfrac`(分率)か `material_mm3`(材料の総体積[mm^3])のどちらか一方。
    **要素体積はここが格子から決めて `simp.optimize` へ渡す** —— 呼び出し側が
    `mm^3 → 要素数` の換算を自分で書くと、格子の作り方が変わったときに黙ってずれる。
    どちらの経路でも戻り値に mm^3 の実測が入る。

    密度フィルタ半径は `rmin`(要素単位)か `rmin_mm`(mm)のどちらか一方(排他。
    `simp.optimize` と同じ形)。どちらも渡さなければ `simp.DEFAULT_RMIN`
    (**値は動く。床の `simp.MIN_RMIN`(=1.5要素)とは別物**)。
    **要素サイズはここが格子から決めて `simp.optimize` へ渡す**(体積目標と同じ理由)。
    要素サイズから決まる下限を `rmin_mm` が下回ると `simp.FilterRadiusError`。

    `solver` は `simp.optimize` の解法の**上書き**(既定は None = `simp` 側の既定)。
    **性能の新旧比較のためだけにある口**で、受入検査や比較実測が旧い解法(Jacobi 前処理)
    を同じセッションで撮り直すのに使う。None のときは `simp.optimize` へ渡さないので、
    既定の挙動は1ビットも変わらない。

    `threads` も同じ形の上書き(既定 None = `simp.PARALLEL_THREADS`)。**スレッド数を
    振ったスケーリング測定に使う**口で、None のときは渡さない。

    `keep_model=True` は `simp.optimize` へそのまま渡る。`result.model`(戻り値 dict の
    `"result"` キー)に実際に解いた `Model` が入るので、`stress.py` の後処理が拘束・荷重を
    手で組み直さずに済む。既定 `False` では入らない(数値は1ビットも変わらない)。

    `update` は `simp.optimize` の更新則の**上書き**(既定 None = `simp` 側の既定 `"oc"`)。
    None のときは `simp.optimize` へ渡さないので、既定の挙動は1ビットも変わらない。

    `reaction_min` を渡すと**各拘束面が受け持つ反力に下限**(荷重の合力に対する割合)を
    課す。制約が体積と合わせて複数本になるので、**`update` を省略すると `"mma"` へ倒す**
    (OC は乗数1本の二分探索で、制約を増やせない)。満たせなければ
    `simp.ReactionTargetError`。**渡さなければ経路は1ビットも変わらない。**

    戻り値の `"reactions"` は**拘束面1枚ごとの反力**(`simp.Reaction` を素の値に開いたもの。
    `fraction` は荷重の合力に対する割合)。**荷重を運んでいない取付点はここが 0 になる** ——
    構造最適化は荷重を運ばない材料を置かないので、そのままだと保持ソリッドが浮き島になって
    `mesh.remove_islands` に消える。**面の並びは `fixed_faces` と同じ。**

    `fixed_faces` / `load_faces` が要素サイズより小さくて格子節点が1つも付かないと
    (`AnchorTable.empty_faces()`)、**例外にはせず**戻り値の `"empty_condition_faces"`
    (`AnchorTable.condition_face_diagnoses` の結果。空でなければ1件以上)に診断を積んで
    早期returnする(当面の措置。本当の解決は保留 — docs/TOPOPT.md「面が小さすぎて空になる」参照)。
    このとき `"result"` / `"solid"` / `"void"` / `"surface"` / `"stl"` は `None`
    (`simp.optimize` を呼んでいない)。通常どおり最後まで走った戻り値では
    `"empty_condition_faces"` は常に `[]`。
    """
    from . import mesh
    from . import simp

    simp.check_volume_target(volfrac, material_mm3)
    simp.check_rmin_target(rmin, rmin_mm)
    if (fixed_faces is None) != (load_faces is None):
        raise ValueError("fixed_faces と load_faces は両方渡すか、両方省略すること")
    if shape is not None and fixed_faces is None:
        raise ValueError(
            "自分の形を渡すときは fixed_faces / load_faces も渡すこと"
            "(面添字は形ごとに違うので、デモの選び方は使えない)"
        )
    if (design_mask is None) != (design_grid is None):
        raise ValueError("design_mask と design_grid は両方渡すか、両方省略すること")
    if design_mask is not None and shape is None:
        raise ValueError(
            "design_mask を渡すときは面アンカーの対象(shape)も渡すこと"
            "(塊の面ではなく、拘束・荷重を指定できる面がどこから来るかを明示する)"
        )

    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    if design_mask is not None:
        model = vx.model_from_design_mask(
            design_grid, design_mask, anchor_shape=shape, keep=keep, obstacle=obstacle
        )
    else:
        if shape is None:
            shape = demo_bracket()
        model = vx.voxelize(shape, elements_long_axis, keep=keep, obstacle=obstacle)
    timings["voxelize"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    soup = model.soup
    if fixed_faces is None:
        fixed_faces, load_faces = demo_faces(soup)
    fixed_faces = _face_list(fixed_faces)
    load_faces = _face_list(load_faces)
    if forces is not None and len(forces) != len(load_faces):
        raise ValueError(
            f"forces は荷重面と同じ数だけ渡すこと(荷重面 {len(load_faces)} 枚 / "
            f"force {len(forces)} 本)。並びも同じ順であること"
        )
    table = build_anchors(soup, model.grid, radius_ratio=radius_ratio)
    timings["anchor"] = time.perf_counter() - t0

    # **実行経路の入口。** 拘束面・荷重面が要素サイズより小さくて節点が1つも
    # 付かないと(`table.empty_faces()`)、この先 `table.fixed_dofs` / `table.load_vector` の
    # `_require_nodes` が例外を投げる(その例外自体は消さない・弱めない — 空の面を名指しして
    # 拘束・荷重を掛けたら黙って0にせず落ちる、という既存の安全策)。**そこへ落ちる前に、
    # ここで一度立ち止まってメッセージを返す。** 本人指示で「落とさない・メッセージを出すだけ」
    # なので、`simp.optimize` は呼ばずに戻り値へ診断を積んで早期returnする。
    empty_condition_faces = table.condition_face_diagnoses(fixed_faces, load_faces)
    if empty_condition_faces:
        warning = format_condition_face_warning(empty_condition_faces)
        print(f"[anchor.run_pipeline] 警告: {warning}", file=sys.stderr)
        return {
            "model": model,
            "table": table,
            "result": None,
            "solid": None,
            "void": None,
            "surface": None,
            "stl": None,
            "fixed_faces": fixed_faces,
            "load_faces": load_faces,
            "timings": timings,
            "empty_condition_faces": empty_condition_faces,
            "warning": warning,
            # 早期returnの枝は `simp.optimize` を呼んでいないので、反力も更新則も無い
            # (キーの有無で分岐させないための空)。
            "reactions": [],
            "reaction_min": None,
            "update": None,
        }

    nx, ny, nz = model.grid.shape
    fixed_dofs = table.fixed_dofs(fixed_faces)
    # **反力は拘束面1枚を1グループにする。** 面ごとの節点集合は排他(`AnchorTable.node_ids`)
    # なので、そのまま `ReactionGroup` の「重ならない」契約を満たす。
    reaction_groups = [
        simp.ReactionGroup(name=f"fixed_face_{int(face)}", dofs=table.fixed_dofs([int(face)]))
        for face in fixed_faces
    ]
    load, load_total = _assemble_load(table, load_faces, force, forces)
    solid = model.keep.copy()
    if keep_anchor_material:
        near = table.element_mask(list(fixed_faces) + list(load_faces), layers=keep_layers)
        solid |= near & model.design
    void = ~model.design | model.obstacle

    resolved_update = update
    if reaction_min is not None and resolved_update is None:
        # 反力の下限は制約が複数本になるので OC では扱えない。**この経路だけ MMA へ倒す**
        # (`reaction_min` を渡さない呼び出しは既定の更新則のまま)。
        resolved_update = "mma"

    t0 = time.perf_counter()
    result = simp.optimize(
        nx,
        ny,
        nz,
        volfrac=volfrac,
        material_mm3=material_mm3,
        element_volume_mm3=model.grid.element_volume,
        rmin=rmin,
        rmin_mm=rmin_mm,
        max_iter=max_iter,
        fixed_dofs=fixed_dofs,
        load=load,
        void_mask=void,
        solid_mask=solid,
        reactions=reaction_groups,
        callback=progress,
        keep_model=keep_model,
        **({} if solver is None else {"solver": solver}),
        **({} if threads is None else {"threads": threads}),
        **({} if resolved_update is None else {"update": resolved_update}),
        **({} if reaction_min is None else {"reaction_min": float(reaction_min)}),
    )
    timings["optimize"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    # `solid` / `void` はここで `simp.optimize` へ渡したのと同じマスク(保持ソリッド+
    # アンカー材料 / 設計空間の外∪除外)。密度場側のガウシアン平滑化(段0)がこれらを
    # にじませるので、平滑化の後に同じマスクで塗り直す(`mesh.build_surface` の
    # docstring・`docs/TOPOPT.md`「密度場側のガウシアン平滑化」)。
    surface = mesh.build_surface(result.rho, keep_mask=solid, zero_mask=void)
    timings["mesh"] = time.perf_counter() - t0

    out = Path(stl_path) if stl_path is not None else _HERE / "output" / "bracket.stl"
    out.parent.mkdir(parents=True, exist_ok=True)
    mesh.write_stl(out, surface.verts_smooth, surface.faces)

    design_elements = int(np.count_nonzero(model.design))
    movable = ~void & ~solid
    free_elements = int(np.count_nonzero(movable))
    # **目標は `optimize` が実際に使った値を引く。** ここで再計算すると、絶対量で渡された
    # ときに換算をもう一度書くことになり、片方だけ直したときに黙ってずれる。
    target_volume = result.target_volume
    element_volume = model.grid.element_volume
    history = result.history
    return {
        "model": model,
        "table": table,
        "result": result,
        "solid": solid,
        "void": void,
        "surface": surface,
        "stl": str(out),
        "fixed_faces": fixed_faces,
        "load_faces": load_faces,
        "load_total": load_total,
        "timings": timings,
        "empty_condition_faces": [],
        "design_elements": design_elements,
        "free_elements": free_elements,
        "target_volfrac": result.volfrac,
        "target_volume": target_volume,
        "rmin": result.rmin,
        "rmin_mm": result.rmin_mm,
        "achieved_volume": float(result.rho.sum()),
        "element_volume_mm3": element_volume,
        "target_material_mm3": target_volume * element_volume,
        "achieved_material_mm3": float(result.rho.sum()) * element_volume,
        "solid_elements": int(np.count_nonzero(solid)),
        "void_elements": int(np.count_nonzero(void)),
        "achieved_volfrac": (
            float(result.rho[model.design].mean()) if design_elements else 0.0
        ),
        "reactions": reaction_payload(fixed_faces, result.reactions),
        "reaction_min": result.reaction_min,
        "update": result.update,
        "compliance_first": history[0].compliance if history else float("nan"),
        "compliance_last": history[-1].compliance if history else float("nan"),
        "iterations": len(history),
    }


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="面アンカーの要約と、端から端まで1回")
    parser.add_argument("--n", type=int, default=40, help="最長軸方向の要素数")
    parser.add_argument("--table-only", action="store_true", help="対応表だけ作って終わる")
    parser.add_argument("--max-iter", type=int, default=40)
    args = parser.parse_args()

    if args.table_only:
        model = vx.voxelize(demo_bracket(), args.n)
        table = build_anchors(model.soup, model.grid)
        print(f"grid   : {model.grid.shape} spacing={model.grid.spacing:.6f}")
        print(f"nodes  : {table.node_grid.shape}")
        print(f"table  : {table.stats['seconds']:.3f}秒 / {table.stats['faces']}面 / "
              f"標本点 {table.stats['samples']}")
        for f in table.face_indices():
            a = table[f]
            print(
                f"  面{f:3d} {a.kind:14s} 節点{a.node_count:5d} "
                f"担当面積 {a.total_area:10.3f} / CAD {a.cad_area:10.3f} "
                f"(こぼれ {a.leak_ratio:.1%})"
            )
        return 0

    out = run_pipeline(elements_long_axis=args.n, max_iter=args.max_iter)
    t = out["timings"]
    print(f"面      : 固定={out['fixed_faces']} 荷重={out['load_faces']}")
    print(f"格子    : {out['model'].grid.shape} 設計要素={out['design_elements']}")
    print(f"体積    : {out['achieved_volume']:.3f} 要素 (目標 {out['target_volume']:.3f}) / "
          f"設計空間内の平均密度 {out['achieved_volfrac']:.4f}")
    print(f"C       : {out['compliance_first']:.4f} → {out['compliance_last']:.4f}")
    print(f"秒      : ボクセル化 {t['voxelize']:.2f} / 対応表 {t['anchor']:.2f} / "
          f"最適化 {t['optimize']:.2f} / メッシュ化 {t['mesh']:.2f}")
    print(f"STL     : {out['stl']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

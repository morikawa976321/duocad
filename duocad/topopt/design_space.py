"""保持・除外・拘束・荷重の4条件から、材料を置ける設計空間(塊)を1回で作る。

derive: 保持ボディと条件面が属するボディの外接箱の頂点から凸包を作り、凸包から距離が全体寸法に比例する余白 `m` 以内の領域
(凸包と半径 `m` の球のミンコフスキー和)を格子に載せ、除外ソリッドの占有を引き、条件を1つも含まない連結成分を落とす。
材料量・安全率・参照密度・最適化結果は入力に取らない。1回の最適化実行を通して `DesignSpace` は同じものを使い、最適化側は書き換えない。
保持ソリッドが格子外へ出ていないことは voxelize が見ないのでここで確認する。設計理由・実測・制限は docs/TOPOPT.md。"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull, QhullError

from . import anchor as an
from . import trace
from . import voxelize as vx

#: 診断に書く導出方式の名前。記録を読む側(`topopt_checks/`)はこの文字列で新旧を見分ける。
DERIVATION = "convex-hull-offset"

# 余白は**条件の点群の外接箱の最長辺に対する比率**で持つ(絶対mmでは部品の大きさに追従しない)。0.20 は本人判断
# (2026-09-16。docs/TOPOPT.md「固定凸包による設計空間(2026-09-16)」)。
DEFAULT_MARGIN_RATIO = 0.20
DEFAULT_ELEMENTS_LONG_AXIS = 40

#: `ConditionBody.roles` に入れてよい値。
ROLES = ("keep", "fixed", "load")


class DesignSpaceTooSmallError(vx.OutsideDesignSpaceError):
    """導出した塊が条件(保持ソリッド)を含みきれていない、または塊が空になった。`voxelize.OutsideDesignSpaceError` の派生。"""


class DesignSpaceGeometryError(ValueError):
    """条件の点群から凸包が起こせない(階数不足・非有限・箱が潰れている・Qhull の失敗)。

    **外接直方体へ戻る代替経路も、Qhull の `QJ`(ランダム摂動)も使わない**(入力の矛盾を隠して別の塊を返すことになる)。
    """


class DesignSpaceConflictError(ValueError):
    """保持ボディの占有セルが除外ソリッドの占有と重なっている(入力矛盾)。

    最終ブーリアンは除外優先なので、格子の上で除外を保持で埋め戻すと最終CADと違う塊を最適化することになる。
    `bodies` はボディ名 → 重なったセル数。
    """

    def __init__(self, message: str, *, bodies: dict) -> None:
        super().__init__(message)
        self.bodies = {str(k): int(v) for k, v in bodies.items()}


class DesignSpaceDisconnectedError(ValueError):
    """塊の中で、全条件(保持ボディ・拘束面・荷重面)の必要領域が単一成分に収まらない。

    **`DesignSpaceTooSmallError` とは別の型**(あちらの「余白を上げろ」は分断には効かない。直し方は除外ソリッドを削るか条件を置き直すこと)。
    `components` は塊の連結成分の数、`unreached` は取り残された条件の名前、`seeds` は条件ごとの
    `{"elements": 塊の中の要素数, "labels": 所属する成分}`。名前は `fixed_face_<面添字>` / `load_face_<面添字>` / `keep_<ボディ名>`。
    """

    def __init__(
        self,
        message: str,
        *,
        components: int = 0,
        unreached: Sequence[str] = (),
        seeds: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.components = int(components)
        self.unreached = [str(v) for v in unreached]
        self.seeds = dict(seeds or {})


# **連結性は 6近傍(面で接する)で定義する。** 角1点・稜1本だけで触れる2要素が共有するのは節点1つで、回転を止められない
# ピン接合(機構)にしかならず、力の道にならない。26近傍だと宙に浮いた形を通す。
# **6近傍でつながっていることは、部材の幅や剛性を保証しない。**
NEIGHBOURHOOD = 6
_RANK_OF_NEIGHBOURHOOD = {6: 1, 18: 2, 26: 3}


def element_components(
    design: np.ndarray, *, neighbourhood: int = NEIGHBOURHOOD
) -> tuple[np.ndarray, int]:
    """塊の連結成分。`(labels, n)` を返す(`labels == 0` は塊の外)。**塊のガードは常に 6近傍**(`NEIGHBOURHOOD`)。
    18/26 を受けるのは定義を変えると答えが変わることを検査で見せるためだけの口。"""
    mask = np.asarray(design, dtype=bool)
    if mask.ndim != 3:
        raise ValueError(f"design は3次元の配列であること(いま shape={mask.shape})")
    try:
        rank = _RANK_OF_NEIGHBOURHOOD[int(neighbourhood)]
    except KeyError:
        raise ValueError(
            f"neighbourhood は {sorted(_RANK_OF_NEIGHBOURHOOD)} のいずれか(いま {neighbourhood!r})"
        ) from None
    labels, count = ndimage.label(mask, structure=ndimage.generate_binary_structure(3, rank))
    return labels, int(count)


def connect_seeds(design: np.ndarray, seeds: dict) -> tuple[np.ndarray, dict]:
    """**全ての条件(種)が同じ連結成分に届くことを個別に確かめ、条件を含まない成分を落とす。**

    `seeds` は名前 → 塊と同じかたちの bool マスク。戻り値は `(条件を含む成分だけを残した塊, 診断)`。
    塊の中に要素を持たない種、全ての種が共有する成分が無い、1つの種が複数成分にまたがる、のいずれも `DesignSpaceDisconnectedError`
    (`unreached` は届かない・分断された条件)。**最大成分を選んで成功にしない。条件の一部だけを残して成功にしない。**
    どの種にも触れない成分だけを落とす。
    """
    mask = np.asarray(design, dtype=bool)
    if not seeds:
        raise ValueError("条件(種)が1つも無い")
    labels, count = element_components(mask)
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    per: dict = {}
    for name, seed in seeds.items():
        seed = np.asarray(seed, dtype=bool)
        if seed.shape != mask.shape:
            raise ValueError(f"条件 {name} のかたち {seed.shape} が塊 {mask.shape} と違う")
        inside = seed & mask
        per[str(name)] = {
            "elements": int(np.count_nonzero(inside)),
            "elements_outside": int(np.count_nonzero(seed & ~mask)),
            "labels": sorted(int(v) for v in np.unique(labels[inside]) if v != 0),
        }
    where = (
        f"塊の連結成分は {count} 個(要素数 {[int(sizes[i]) for i in range(1, count + 1)]}、"
        f"{NEIGHBOURHOOD}近傍で判定)。条件ごとの所属成分: "
        + ", ".join(f"{n}={p['labels'] or '無し'}" for n, p in per.items())
        + "。"
    )
    missing = [name for name, p in per.items() if p["elements"] == 0]
    if missing:
        raise DesignSpaceDisconnectedError(
            f"塊の中に要素を1つも持たない条件がある: {', '.join(missing)}。{where}"
            "除外ソリッドが条件面のまわりの要素を全部削っている(条件面が除外の中にある入力矛盾か、"
            "条件面の載るボディがこの要素サイズで薄すぎて要素を持てない解像度不足)。"
            "除外ソリッドと条件面の位置を確認するか、要素サイズを小さくすること。",
            components=count,
            unreached=missing,
            seeds=per,
        )
    split = [name for name, p in per.items() if len(p["labels"]) > 1]
    if split:
        raise DesignSpaceDisconnectedError(
            f"条件の必要領域が複数の連結成分に分かれている: {', '.join(split)}。{where}"
            "除外ソリッドによる分断や、要素サイズによる接続部分の取りこぼしを確認すること。",
            components=count,
            unreached=split,
            seeds=per,
        )
    label_sets = [set(p["labels"]) for p in per.values()]
    common = set.intersection(*label_sets)
    if not common:
        votes = np.bincount(
            [lab for p in per.values() for lab in p["labels"]], minlength=count + 1
        )
        majority = int(np.argmax(votes))
        unreached = [name for name, p in per.items() if majority not in p["labels"]]
        raise DesignSpaceDisconnectedError(
            f"全ての条件を含む連結成分が無い。取り残された条件: {', '.join(unreached)}。{where}"
            "除外ソリッドが条件の間を塞いでいないか、条件面の位置を確認すること"
            "(このまま最適化に通すと、多くの場合エラーも警告も出ないまま"
            "条件ごとに別々に浮いた形が出る)。",
            components=count,
            unreached=unreached,
            seeds=per,
        )
    kept = sorted(common)
    dropped = [lab for lab in range(1, count + 1) if lab not in kept]
    final = mask & np.isin(labels, kept)
    report = {
        "neighbourhood": NEIGHBOURHOOD,
        "components": count,
        "component_sizes": [int(sizes[i]) for i in range(1, count + 1)],
        "seeds": per,
        "common_labels": sorted(common),
        "kept_labels": kept,
        "dropped_components": len(dropped),
        "dropped_elements": int(sum(int(sizes[lab]) for lab in dropped)),
        "design_elements": int(np.count_nonzero(final)),
    }
    return final, report


def connect_conditions(
    design: np.ndarray,
    table: an.AnchorTable,
    keep_seeds: dict,
    fixed_faces: Sequence[int] | int,
    load_faces: Sequence[int] | int,
) -> tuple[np.ndarray, dict]:
    """拘束面・荷重面のアンカー要素(1層)と保持ボディの占有を種にして `connect_seeds` を呼ぶ。

    **節点が1つも付かなかった条件面があるときは連結性を見ず塊をそのまま返す**(別の穴で、`optimize_run._empty_condition_faces` が診断を出す。
    ここで落とすと別の例外に化けて原因が分かりにくい)。
    """
    fixed = _as_faces(fixed_faces)
    load = _as_faces(load_faces)
    empty = [f for f in fixed + load if f in table.empty_faces()]
    if empty:
        return np.asarray(design, dtype=bool), {
            "skipped": "empty_anchor_faces",
            "empty_faces": empty,
        }
    seeds: dict = {}
    for kind, faces in (("fixed", fixed), ("load", load)):
        for face in faces:
            seeds[f"{kind}_face_{int(face)}"] = table.element_mask([int(face)])
    for name, mask in keep_seeds.items():
        seeds[str(name)] = mask
    return connect_seeds(design, seeds)


def _as_faces(value: Sequence[int] | int) -> tuple:
    """`int` 1つでも並びでも受けて、面添字のタプルにする。"""
    if isinstance(value, (int, np.integer)):
        return (int(value),)
    return tuple(int(item) for item in value)


# ---------------------------------------------------------------------------
# 入力
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConditionBody:
    """凸包の点群へ寄与する1ボディ(保持ボディ、または拘束面・荷重面が属するボディ)。

    `roles` は `ROLES` の部分集合。`bbox_min` / `bbox_max` は B-rep の外接箱[mm]。**三角形分割から取ってはいけない**
    (弦は曲面のぶん内側に来る上、表示用の三角形分割の有無で値が動く。`voxelize._shape_bbox` / `kernel.bounding_box` の契約)。
    `shape` は保持ボディでは必須、条件面だけのボディでは省ける。
    """

    name: str
    roles: tuple
    bbox_min: tuple
    bbox_max: tuple
    shape: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not str(self.name):
            raise ValueError("ConditionBody.name が空")
        roles = tuple(str(r) for r in (self.roles if not isinstance(self.roles, str) else (self.roles,)))
        bad = [r for r in roles if r not in ROLES]
        if bad or not roles:
            raise ValueError(f"roles は {ROLES} の空でない部分集合であること(いま {self.roles!r})")
        lo = tuple(float(v) for v in self.bbox_min)
        hi = tuple(float(v) for v in self.bbox_max)
        if len(lo) != 3 or len(hi) != 3:
            raise ValueError(f"外接箱は3成分であること(いま {self.bbox_min!r} .. {self.bbox_max!r})")
        if not all(math.isfinite(v) for v in lo + hi):
            raise ValueError(f"ボディ {self.name} の外接箱に非有限値がある: {lo} .. {hi}")
        if any(h < l for l, h in zip(lo, hi)):
            raise ValueError(f"ボディ {self.name} の外接箱が裏返っている: {lo} .. {hi}")
        if "keep" in roles and self.shape is None:
            raise ValueError(f"保持ボディ {self.name} には shape が要る(格子への占有を数える)")
        object.__setattr__(self, "name", str(self.name))
        object.__setattr__(self, "roles", tuple(sorted(set(roles), key=ROLES.index)))
        object.__setattr__(self, "bbox_min", lo)
        object.__setattr__(self, "bbox_max", hi)

    @property
    def corners(self) -> np.ndarray:
        """外接箱の8頂点 (8,3)。"""
        lo = np.asarray(self.bbox_min, dtype=float)
        hi = np.asarray(self.bbox_max, dtype=float)
        bits = np.array([[i, j, k] for i in (0, 1) for j in (0, 1) for k in (0, 1)], dtype=float)
        return lo + bits * (hi - lo)


def condition_body(name: str, shape: Any, *, roles: Sequence[str]) -> ConditionBody:
    """形状から外接箱を測って `ConditionBody` を作る(検査・CLI・合成題材の入口。製品は `optimize_run.build_conditions` が文書側のキャッシュ付き箱を使う)。"""
    lo, hi = vx._shape_bbox(vx.unwrap(shape))
    return ConditionBody(
        name=str(name),
        roles=tuple(roles),
        bbox_min=tuple(float(v) for v in lo),
        bbox_max=tuple(float(v) for v in hi),
        shape=shape,
    )


@dataclass(frozen=True)
class Conditions:
    """利用者が入れる4条件と、凸包の材料になるボディの外接箱。**設計空間ソリッドは無い。**

    `fixed_faces` / `load_faces` は `anchor_shape`(省略時は `keep`)の三角形群での面添字で、何枚でもよい(`int` 1つも受ける)。
    面添字は渡した形の面の並びで決まるので、面を選ぶのに使った形をそのまま渡すこと。
    `bodies` は保持ボディと条件面が属するボディを**重複なく**並べたもの。除外だけ・無関係・以前の最適化結果のボディを入れると塊が広がる。
    `keep` は `bodies` の保持ボディと同じ集合を指すこと(層をまたぐ契約: 格子の外へ出ていないかは `keep` の外接箱で、
    格子への占有と条件ごとの接続は `bodies` のボディごとに見る)。荷重の大きさはここに無い(`optimize_run.build_conditions` の `forces`)。
    """

    keep: Any
    fixed_faces: Any
    load_faces: Any
    obstacle: Any = None
    anchor_shape: Any = None
    bodies: tuple = field(kw_only=True)

    def __post_init__(self) -> None:
        # `frozen=True` なので `object.__setattr__`。
        object.__setattr__(self, "fixed_faces", _as_faces(self.fixed_faces))
        object.__setattr__(self, "load_faces", _as_faces(self.load_faces))
        if not self.fixed_faces:
            raise ValueError("拘束面が1枚も無い")
        if not self.load_faces:
            raise ValueError("荷重面が1枚も無い")
        bodies = tuple(self.bodies)
        if not bodies:
            raise ValueError(
                "bodies が空。保持ボディと条件面が属するボディの外接箱(ConditionBody)を渡すこと"
            )
        for body in bodies:
            if not isinstance(body, ConditionBody):
                raise TypeError(f"bodies の要素は ConditionBody であること(いま {type(body).__name__})")
        names = [body.name for body in bodies]
        if len(set(names)) != len(names):
            raise ValueError(f"bodies の名前が重複している: {names}")
        if not any("keep" in body.roles for body in bodies):
            raise ValueError("bodies に保持ボディ(roles に 'keep')が1つも無い")
        object.__setattr__(self, "bodies", bodies)

    @property
    def anchor(self) -> Any:
        return self.keep if self.anchor_shape is None else self.anchor_shape


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
@dataclass
class DesignSpace:
    """導出した塊と、その導出過程の実測。**1回の最適化実行を通して1つ**を使い回す。

    `mask` は `grid` と同じかたちの bool 配列(塊)で**読み取り専用**。`model` は保持ソリッドを同じ格子に載せた `VoxelModel` で、
    **`model.obstacle` は常に全 False**(除外は塊から引き切っている)。`table` は導出が作った面アンカーの表。
    `hull_planes` は凸包の支持平面 `(n, 4)`(`ax+by+cz+d <= 0` が内側、法線は単位長)、`hull_triangles` は凸包の表面の三角形 `(f, 3, 3)`、
    `corners` は凸包を起こした点群。
    """

    grid: vx.Grid
    mask: np.ndarray
    model: vx.VoxelModel
    soup: vx.TriangleSoup
    conditions: Conditions
    bbox_min: np.ndarray
    bbox_max: np.ndarray
    margin: float
    margin_ratio: float
    hull_planes: np.ndarray
    hull_triangles: np.ndarray
    corners: np.ndarray
    stats: dict = field(default_factory=dict)
    table: Any = None
    #: 除外ソリッドの格子上の占有(凸包の外や塊から外れた分も含む。塊の穴が除外由来かを見分けるのに使う)。除外が無い・格子と重ならないときは全 False。読み取り専用。
    obstacle: Optional[np.ndarray] = None

    @property
    def element_count(self) -> int:
        return int(np.count_nonzero(self.mask))

    @property
    def fixed_faces(self) -> tuple:
        return self.conditions.fixed_faces

    @property
    def load_faces(self) -> tuple:
        return self.conditions.load_faces

    @property
    def keep(self) -> np.ndarray:
        return self.model.keep

    @property
    def mask_hash(self) -> str:
        """塊の内容ハッシュ(候補間で塊が同一であることの検査用)。"""
        return mask_digest(self.mask)


def mask_digest(mask: np.ndarray) -> str:
    array = np.ascontiguousarray(np.asarray(mask, dtype=bool))
    digest = hashlib.sha256(json.dumps(list(array.shape)).encode())
    digest.update(np.packbits(array.ravel()).tobytes())
    return digest.hexdigest()


def condition_corners(bodies: Sequence[ConditionBody]) -> np.ndarray:
    """全ボディの外接箱の頂点(重複除去済み) (m,3)。"""
    if not bodies:
        raise ValueError("ボディが1つも無い")
    corners = np.concatenate([body.corners for body in bodies], axis=0)
    return np.unique(corners, axis=0)


def convex_hull(corners: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict]:
    """点群の凸包。`(支持平面 (n, 4), 表面の三角形 (f, 3, 3), 診断)` を返す。

    支持平面は `ax + by + cz + d <= 0` が内側、`(a,b,c)` は単位法線(scipy の `ConvexHull.equations` の契約)。
    **点群が平面・直線・1点に潰れていれば `DesignSpaceGeometryError`**(`QJ` で通さない)。
    """
    points = np.asarray(corners, dtype=float).reshape(-1, 3)
    if points.shape[0] == 0 or not np.all(np.isfinite(points)):
        raise DesignSpaceGeometryError(f"凸包の点群が空か非有限値を含む(点数 {points.shape[0]})")
    rank = int(np.linalg.matrix_rank(points - points.mean(axis=0), tol=1e-9 * max(1.0, float(np.ptp(points)))))
    if rank < 3:
        raise DesignSpaceGeometryError(
            f"条件の点群が{['1点', '直線', '平面'][rank]}に潰れていて凸包が起こせない"
            f"(階数 {rank}、点数 {points.shape[0]})。条件ボディに有限の厚みが要る"
        )
    try:
        hull = ConvexHull(points)
    except QhullError as exc:
        raise DesignSpaceGeometryError(f"凸包の計算に失敗した: {exc}") from exc
    equations = np.array(hull.equations, dtype=float)
    triangles = np.array(points[hull.simplices], dtype=float)
    info = {
        "vertex_count": int(len(hull.vertices)),
        "facet_count": int(equations.shape[0]),
        "volume_mm3": float(hull.volume),
        "area_mm2": float(hull.area),
    }
    return equations, triangles, info


def offset_hull_volume(corners: np.ndarray, margin: float) -> float:
    """凸包と半径 `margin` の球のミンコフスキー和の体積[mm³](シュタイナーの公式)。

    `V = V_凸包 + A·m + m²·Σ(稜長 × 外角)/2 + 4π/3·m³`(外角は隣り合う三角形の外向き法線のなす角)。`derive` の塊の
    格子に載せる前の連続体積で、除外と「条件を含まない連結成分」を引く前の値(除外の分は `obstacle_free_fraction`)。
    見積もり全体の誤差は docs/TOPOPT.md「要素サイズの下限」。
    """
    _planes, _triangles, info = convex_hull(corners)  # 退化(平面・直線・1点)はここで断る
    points = np.asarray(corners, dtype=float).reshape(-1, 3)
    hull = ConvexHull(points)
    normals = hull.equations[:, :3]
    edge_term = 0.0
    for i, neighbours in enumerate(hull.neighbors):
        for slot, j in enumerate(neighbours):
            if j <= i:
                continue
            # 三角形 i の頂点 slot の向かいの辺 = 隣 j との共有辺。
            a, b = [v for s, v in enumerate(hull.simplices[i]) if s != slot]
            length = float(np.linalg.norm(points[a] - points[b]))
            cosine = float(np.clip(np.dot(normals[i], normals[j]), -1.0, 1.0))
            edge_term += length * math.acos(cosine)
    m = float(margin)
    return float(hull.volume + hull.area * m + 0.5 * edge_term * m * m + 4.0 / 3.0 * math.pi * m ** 3)


def obstacle_free_fraction(corners: np.ndarray, obstacle: Any, *, margin_ratio: float = DEFAULT_MARGIN_RATIO,
                           elements_long_axis: int = 60) -> float:
    """余白付き凸包のセルのうち、除外ソリッドに食われずに残る割合(`derive` と同じ判定を `elements_long_axis` 分割の粗い格子で数えた値)。
    粗いので揺れる(hanger で分割数 40〜240 の実測 0.810〜0.828)。除外が無ければ 1。"""
    if obstacle is None:
        return 1.0
    pts = np.asarray(corners, dtype=float).reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    margin = float(margin_ratio) * float(np.max(hi - lo))
    box_lo, box_hi = frame_box(lo, hi, margin_ratio=margin_ratio)
    planes, triangles, _info = convex_hull(pts)
    grid = grid_for_frame(box_lo, box_hi, elements_long_axis=elements_long_axis)
    candidate = box_mask(grid, box_lo, box_hi) & offset_hull_mask(grid, planes, margin)
    candidate &= hull_distance_mask(grid, planes, triangles, margin)
    total = int(np.count_nonzero(candidate))
    if total == 0:
        return 1.0
    soup = vx.triangles_from_shape(
        vx.unwrap(obstacle), deviation=max(grid.spacing * vx.DEFAULT_DEVIATION_RATIO, 1e-6),
        angular_deflection=vx.DEFAULT_ANGULAR_DEFLECTION,
    )
    grid_lo, grid_hi = grid.world_box()
    obs_lo, obs_hi = soup.bbox
    if np.any(obs_hi < grid_lo) or np.any(obs_lo > grid_hi):
        return 1.0
    occupied, _stats = vx.occupancy(soup, grid, allow_empty=True)
    return float(np.count_nonzero(candidate & ~occupied)) / total


def offset_hull_mask(grid: vx.Grid, planes: np.ndarray, margin: float) -> np.ndarray:
    """セル中心が、各支持平面を `margin` だけ外へ押し出した半空間の全てに入るセル。

    **塊の定義ではない。** 距離 `margin` 以内の領域(`hull_distance_mask`)を必ず含む多面体で、距離の判定を全セルで払わないための外側の当たり。
    """
    equations = np.asarray(planes, dtype=float)
    if equations.ndim != 2 or equations.shape[1] != 4:
        raise ValueError(f"planes は (n,4) であること(いま {equations.shape})")
    x = grid.centers_1d(0)[:, None, None]
    y = grid.centers_1d(1)[None, :, None]
    z = grid.centers_1d(2)[None, None, :]
    inside = np.ones(tuple(grid.shape), dtype=bool)
    for a, b, c, d in equations:
        inside &= (a * x + b * y + c * z + d) <= float(margin)
    return inside


def distance_to_hull(points: np.ndarray, planes: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """点群 `(N,3)` から凸包までのユークリッド距離(内側は 0)。外側の点からの最短距離は表面の三角形への距離の最小に一致する。"""
    pts = np.asarray(points, dtype=float).reshape(-1, 3)
    eq = np.asarray(planes, dtype=float)
    signed = pts @ eq[:, :3].T + eq[:, 3]
    inside = np.all(signed <= 0.0, axis=1)
    out = np.zeros(pts.shape[0], dtype=float)
    outside = np.flatnonzero(~inside)
    if outside.size:
        # 距離は `voxelize.distance_to_triangles`(7候補の最小)で取る。Ericson の領域分けは退化三角形で NaN や過大な距離を返し、
        # NaN は `np.minimum` を通って**そのセルを黙って塊から落とす**。
        tri = np.asarray(triangles, dtype=float).reshape(-1, 3, 3)
        out[outside] = vx.distance_to_triangles(tri[:, 0], tri[:, 1], tri[:, 2], pts[outside])[0]
    return out


def hull_distance_mask(
    grid: vx.Grid, planes: np.ndarray, triangles: np.ndarray, margin: float
) -> np.ndarray:
    """セル中心が凸包から距離 `margin` 以内にあるセル(凸包と半径 `margin` の球のミンコフスキー和。判定は `距離 <= margin`)。
    距離は `offset_hull_mask` の多面体に入るセルだけで測る(外は必ず距離 > margin)。"""
    bound = offset_hull_mask(grid, planes, margin)
    index = np.argwhere(bound)
    if index.size == 0:
        return bound
    distance = distance_to_hull(grid.centers_of(index), planes, triangles)
    mask = np.zeros(tuple(grid.shape), dtype=bool)
    keep = distance <= float(margin)
    mask[index[keep, 0], index[keep, 1], index[keep, 2]] = True
    return mask


def box_mask(grid: vx.Grid, lo: Sequence[float], hi: Sequence[float]) -> np.ndarray:
    """軸整列の箱の占有マスク。**要素中心が箱の中にあるか**だけで決める(格子の外枠に使う)。"""
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    axes = [grid.centers_1d(a) for a in range(3)]
    inside = [(axes[a] >= lo[a]) & (axes[a] <= hi[a]) for a in range(3)]
    return inside[0][:, None, None] & inside[1][None, :, None] & inside[2][None, None, :]


def frame_box(cloud_lo, cloud_hi, *, margin_ratio: float = DEFAULT_MARGIN_RATIO) -> tuple:
    """条件の点群の外接箱 → 格子の外枠 `(lo, hi)`。余白は `margin_ratio × 点群の最長辺`。"""
    lo = np.asarray(cloud_lo, dtype=float)
    hi = np.asarray(cloud_hi, dtype=float)
    longest = float(np.max(hi - lo))
    if not longest > 0:
        raise DesignSpaceGeometryError("条件の点群が1点に潰れている(外接箱が起こせない)")
    margin = float(margin_ratio) * longest
    return lo - margin, hi + margin


def grid_for_frame(box_lo, box_hi, *, elements_long_axis: int = DEFAULT_ELEMENTS_LONG_AXIS,
                   spacing: Optional[float] = None) -> vx.Grid:
    """外枠 → 格子。`spacing` を渡すと最長辺の分割数を丸めて決める(実 spacing は指定値と一致しない)。

    **格子を起こす規則はここだけにある**(`derive` も解像度の梯子(`optimize_run`)もこれを呼ぶ。2箇所に書くと画面の要素サイズ・要素数と実際の格子がずれる。層をまたぐ契約)。
    """
    if spacing is None:
        n_long = int(elements_long_axis)
    else:
        n_long = max(1, int(round(float(np.max(np.asarray(box_hi, dtype=float) - np.asarray(box_lo, dtype=float)))
                                  / float(spacing))))
    return vx.grid_for_bbox(box_lo, box_hi, n_long)


@trace.stage("derive")
def derive(
    conditions: Conditions,
    *,
    margin_ratio: float = DEFAULT_MARGIN_RATIO,
    elements_long_axis: int = DEFAULT_ELEMENTS_LONG_AXIS,
    spacing: Optional[float] = None,
    deviation: Optional[float] = None,
    deviation_ratio: float = vx.DEFAULT_DEVIATION_RATIO,
    angular_deflection: float = vx.DEFAULT_ANGULAR_DEFLECTION,
) -> DesignSpace:
    """4条件から塊(設計空間)を1回で導出する。**材料量は受け取らない。**

    手順: 外接箱の頂点 → 凸包 → 余白 `m = margin_ratio × 点群の外接箱の最長辺` → 外枠(点群の外接箱を各軸に `m` 広げた箱。`grid_for_bbox`)
    かつ凸包から距離 `m` 以内のセル → 除外の占有を引く → 保持ボディを1つずつ格子に載せて衝突・取りこぼしを名指しで数える
    → 条件ごとの接続を確かめ条件を含まない成分を落とす → 確定マスクで `VoxelModel` を1回組む。
    `spacing` を渡すと `elements_long_axis` の代わりに**要素サイズを固定**する(丸めで実 spacing は厳密には一致しない)。
    """
    started = time.perf_counter()
    ratio = float(margin_ratio)
    if not math.isfinite(ratio) or ratio < 0.0:
        raise ValueError(f"margin_ratio は 0 以上の有限の値であること(いま {margin_ratio!r})")
    if int(elements_long_axis) < 1:
        raise ValueError(f"elements_long_axis must be >= 1, got {elements_long_axis!r}")
    if spacing is not None and not float(spacing) > 0:
        raise ValueError(f"spacing must be positive, got {spacing!r}")

    corners = condition_corners(conditions.bodies)
    cloud_lo = corners.min(axis=0)
    cloud_hi = corners.max(axis=0)
    longest = float(np.max(cloud_hi - cloud_lo))
    box_lo, box_hi = frame_box(cloud_lo, cloud_hi, margin_ratio=ratio)
    margin = ratio * longest
    planes, triangles, hull_info = convex_hull(corners)
    grid = grid_for_frame(box_lo, box_hi, elements_long_axis=elements_long_axis, spacing=spacing)
    # 実際に使った最長軸の分割数。
    n_long = int(round(float(np.max(box_hi - box_lo)) / grid.spacing))
    # **保持/除外を格子に載せるときの三角形分割は要素サイズから決める**(下の `model_from_design_mask` が既定の deviation で呼ぶので、揃えないと通した保持がそこで通らない)。
    dev = float(deviation) if deviation else max(grid.spacing * deviation_ratio, 1e-6)

    frame = box_mask(grid, box_lo, box_hi)
    # 塊にするのは距離のほう(平面のオフセットは外側の当たり)。
    polytope = frame & offset_hull_mask(grid, planes, margin)
    candidate = polytope & hull_distance_mask(grid, planes, triangles, margin)
    if not candidate.any():
        raise DesignSpaceGeometryError("余白付き凸包に入るセル中心が1つも無い(格子が粗すぎる)")

    obs: Optional[np.ndarray] = None
    obstacle_stats: dict = {}
    design = candidate
    if conditions.obstacle is not None:
        obs_soup = vx.triangles_from_shape(
            vx.unwrap(conditions.obstacle), deviation=dev, angular_deflection=angular_deflection
        )
        grid_lo, grid_hi = grid.world_box()
        obs_lo, obs_hi = obs_soup.bbox
        if np.any(obs_hi < grid_lo) or np.any(obs_lo > grid_hi):
            obstacle_stats = {"skipped": "outside_grid"}
        else:
            # 列の中心を1本も通らない除外(薄い壁など)は0セル。`optimize_run._region_representation` がボディごとに報告する。
            obs, obstacle_stats = vx.occupancy(obs_soup, grid, allow_empty=True)
            design = candidate & ~obs
    obstacle_elements_removed = int(np.count_nonzero(candidate & ~design))
    if not design.any():
        raise DesignSpaceTooSmallError(
            "除外ソリッドを引いたら塊が空になった(除外が余白付き凸包を覆い尽くしている)。",
            {"obstacle": -1},
        )

    # **格子の外は voxelize が見ていない。** 渡された外接箱と `keep` の形が食い違っていると黙って消えるので先に落とす。
    grid_lo, grid_hi = grid.world_box()
    k_lo, k_hi = vx._shape_bbox(vx.unwrap(conditions.keep))
    if np.any(k_lo < grid_lo - 1e-9) or np.any(k_hi > grid_hi + 1e-9):
        raise DesignSpaceTooSmallError(
            f"保持ソリッド {np.round(k_lo, 3).tolist()} .. {np.round(k_hi, 3).tolist()} が"
            f"格子 {np.round(grid_lo, 3).tolist()} .. {np.round(grid_hi, 3).tolist()} の外へ出ている。"
            "bodies の外接箱が keep の形と合っていない(格子の外にはセル中心が無いので、"
            "このままだとはみ出しが件数にも例外にも現れない)。",
            {"keep": -1},
        )

    bodies_report, keep_seeds, keep_cells, conflicts, unrepresented = _keep_bodies_on_grid(
        conditions.bodies, grid, design, obs, dev, angular_deflection
    )
    if conflicts:
        raise DesignSpaceConflictError(
            "保持ボディの占有セルが除外ソリッドの占有と重なっている(入力矛盾: CAD の保持と除外が"
            "交わっている。最終ブーリアンは除外優先なので、格子で除外を保持で埋め戻さない): "
            + ", ".join(f"{name} {n} セル" for name, n in conflicts.items())
            + "。除外ソリッドを保持から離すか、保持を除外に食わせない形にすること。",
            bodies=conflicts,
        )

    soup = vx.triangles_from_shape(
        vx.unwrap(conditions.anchor), deviation=dev, angular_deflection=angular_deflection
    )
    # アンカーの表は**全面・既定の半径**で作る。`faces=` で絞ると稜線の規則の競争相手が減り、絞らない表とは別の節点集合になる。
    table = an.build_anchors(soup, grid)
    connected, connectivity = connect_conditions(
        design, table, keep_seeds, conditions.fixed_faces, conditions.load_faces
    )
    trace.snapshot(
        "derive-masks",
        frame=frame,
        polytope=polytope,
        hull=candidate,
        obstacle=(np.zeros(tuple(grid.shape), dtype=bool) if obs is None else obs),
        minus_obstacle=design,
        connected=connected,
    )

    try:
        # 保持はボディごとの占有の和を渡す(まとめた形で数え直すと、全保持が要素サイズより小さいときにレイが1本も当たらず
        # `unrepresented_bodies` の報告の前に例外になる)。
        model = vx.model_from_design_mask(
            grid,
            connected,
            anchor_shape=soup,
            keep=keep_cells,
            deviation=dev,
            angular_deflection=angular_deflection,
        )
    except vx.OutsideDesignSpaceError as exc:
        raise DesignSpaceConflictError(
            f"保持ボディの占有が塊からはみ出した: {exc} "
            "ボディごとの占有では除外との衝突が無かったので、保持ボディが条件の凸包の外に"
            "セルを持っている(bodies の外接箱と形が食い違っている)。",
            bodies={"keep": int(getattr(exc, "outside", {}).get("keep", 0))},
        ) from exc

    final = np.array(connected, dtype=bool)
    for array in (final, model.design, model.keep, model.obstacle):
        array.setflags(write=False)
    obstacle_cells = np.zeros(tuple(grid.shape), dtype=bool) if obs is None else np.array(obs, dtype=bool)
    obstacle_cells.setflags(write=False)

    stats = {
        "derivation": DERIVATION,
        "seconds": 0.0,
        "anchor_seconds": float(table.stats["seconds"]),
        "margin_ratio": ratio,
        "margin": float(margin),
        "deviation": dev,
        "elements_long_axis": int(n_long),
        "spacing_requested": (None if spacing is None else float(spacing)),
        "spacing": float(grid.spacing),
        "bodies": bodies_report,
        "corners": corners.tolist(),
        "condition_bbox": [cloud_lo.tolist(), cloud_hi.tolist()],
        "condition_longest_edge": longest,
        "hull": dict(hull_info, planes=planes.tolist(), triangles=triangles.tolist()),
        "inflation": "minkowski-ball",
        "design_bbox": [box_lo.tolist(), box_hi.tolist()],
        "grid": grid.as_dict(),
        "cells": {
            "grid": int(grid.element_count),
            "frame": int(np.count_nonzero(frame)),
            "offset_polytope": int(np.count_nonzero(polytope)),
            "hull": int(np.count_nonzero(candidate)),
            "minus_obstacle": int(np.count_nonzero(design)),
            "connected": int(np.count_nonzero(final)),
        },
        "box_elements": int(np.count_nonzero(frame)),
        "design_elements": int(np.count_nonzero(final)),
        "obstacle_elements_removed": obstacle_elements_removed,
        "obstacle_occupancy": obstacle_stats,
        "keep_elements": int(model.stats["keep_elements"]),
        "unrepresented_bodies": unrepresented,
        "connectivity": connectivity,
        "mask_hash": mask_digest(final),
    }
    stats["seconds"] = time.perf_counter() - started
    return DesignSpace(
        grid=grid,
        mask=final,
        model=model,
        soup=soup,
        conditions=conditions,
        bbox_min=cloud_lo,
        bbox_max=cloud_hi,
        margin=float(margin),
        margin_ratio=ratio,
        hull_planes=planes,
        hull_triangles=triangles,
        corners=corners,
        stats=stats,
        table=table,
        obstacle=obstacle_cells,
    )


def _keep_bodies_on_grid(
    bodies: Sequence[ConditionBody],
    grid: vx.Grid,
    design: np.ndarray,
    obstacle: Optional[np.ndarray],
    deviation: float,
    angular_deflection: float,
) -> tuple[list, dict, np.ndarray, dict, list]:
    """保持ボディを**1つずつ**格子に載せ、占有・除外との衝突・取りこぼしを名前付きで数える。

    まとめると大きい保持が1つあれば合計は正になり、小さいほうが1要素も掴めていないことが隠れる。
    戻り値は `(ボディごとの報告, 接続の種 {keep_<名前>: マスク}, 全保持ボディの占有の和, 衝突 {名前: セル数}, 取りこぼした名前)`。
    取りこぼし(0セル)は解像度不足で入力矛盾ではないので落とさず報告だけする(`optimize_run` が `unrepresented_regions` として返す)。
    **閉じていない・向きの壊れた表面は取りこぼしに数えず落とす**(解像度を上げても直らない)。
    """
    report: list = []
    seeds: dict = {}
    union = np.zeros(tuple(grid.shape), dtype=bool)
    conflicts: dict = {}
    unrepresented: list = []
    for body in bodies:
        if "keep" not in body.roles:
            report.append(
                {"name": body.name, "roles": list(body.roles), "bbox": [list(body.bbox_min), list(body.bbox_max)]}
            )
            continue
        soup = vx.triangles_from_shape(
            vx.unwrap(body.shape), deviation=deviation, angular_deflection=angular_deflection
        )
        vx.inspect_soup(soup)
        raw, occupancy_stats = vx.occupancy(soup, grid, allow_empty=True)
        cells = int(np.count_nonzero(raw))
        in_obstacle = 0 if obstacle is None else int(np.count_nonzero(raw & obstacle))
        in_space = int(np.count_nonzero(raw & design))
        report.append(
            {
                "name": body.name,
                "roles": list(body.roles),
                "bbox": [list(body.bbox_min), list(body.bbox_max)],
                "keep_elements": cells,
                "keep_elements_in_obstacle": in_obstacle,
                "keep_elements_in_space": in_space,
                "reason": (
                    "格子の列(セル中心を通る +z のレイ)が1本もこの形状を貫かない"
                    if occupancy_stats.get("empty") else None
                ),
            }
        )
        union |= raw
        if in_obstacle:
            conflicts[body.name] = in_obstacle
        if cells == 0:
            unrepresented.append(body.name)
        else:
            seeds[f"keep_{body.name}"] = raw & design
    return report, seeds, union, conflicts, unrepresented

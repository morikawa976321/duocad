"""保持・除外・拘束・荷重の4条件から、材料を置ける設計空間(塊)を1回で作る。

derive: 保持ボディと条件面が属するボディの外接箱の頂点から凸包を作り、凸包から距離が
全体寸法に比例する余白 `m` 以内の領域(凸包と半径 `m` の球のミンコフスキー和)を格子に載せ、
除外ソリッドの占有を引き、条件(保持ボディ・拘束面・荷重面)を1つも含まない連結成分を落とす。

材料量・安全率・参照密度・最適化結果は導出の入力に取らない。1回の最適化実行を通して
格子・設計マスク・保持マスク・面アンカーの表は同じもの(`DesignSpace`)を使い、
最適化側はそれを書き換えない(`mask` は読み取り専用にしてある)。

voxelize のはみ出し判定は格子内だけを見るため、保持ソリッドが格子外へ
出ていないことをここで確認してから model_from_design_mask を呼ぶ。
設計理由・実測・制限は docs/TOPOPT.md を参照。"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull, QhullError

from . import anchor as an
from . import trace
from . import voxelize as vx

_HERE = Path(__file__).resolve().parent

#: 診断に書く導出方式の名前。記録を読む側(`topopt_checks/`)はこの文字列で新旧を見分ける。
DERIVATION = "convex-hull-offset"

# 余白は**条件の点群の外接箱の最長辺に対する比率**で持つ(絶対mmでは部品の大きさに追従しない)。
# 0.20 は固定凸包への移行時の本人判断(2026-09-16。案1の 0.25 から下げた。
# docs/TOPOPT.md「固定凸包による設計空間(2026-09-16)」)。
DEFAULT_MARGIN_RATIO = 0.20
DEFAULT_ELEMENTS_LONG_AXIS = 40

#: `ConditionBody.roles` に入れてよい値。
ROLES = ("keep", "fixed", "load")


class DesignSpaceTooSmallError(vx.OutsideDesignSpaceError):
    """導出した塊が条件(保持ソリッド)を含みきれていない、または塊が空になった。

    `voxelize.OutsideDesignSpaceError` の派生なので、既存の `except ValueError` /
    `except vx.OutsideDesignSpaceError` はそのまま効く。
    """


class DesignSpaceGeometryError(ValueError):
    """条件の点群から凸包が起こせない(階数不足・非有限・箱が潰れている・Qhull の失敗)。

    **黙って外接直方体へ戻る代替経路は無い。** ランダム摂動(Qhull の `QJ`)も使わない ——
    どちらも入力の矛盾を隠して別の塊を返すことになる。
    """


class DesignSpaceConflictError(ValueError):
    """保持ボディの占有セルが除外ソリッドの占有と重なっている(入力矛盾)。

    セル中心が両方のソリッドの内側にあるので、CAD の保持ソリッドと除外ソリッドが実際に
    交わっている。最終ブーリアンは除外優先なので、格子の上で除外を保持で埋め戻すと
    最終CADと違う塊を最適化することになる —— だからここで落とす。
    `bodies` はボディ名 → 重なったセル数。
    """

    def __init__(self, message: str, *, bodies: dict) -> None:
        super().__init__(message)
        self.bodies = {str(k): int(v) for k, v in bodies.items()}


class DesignSpaceDisconnectedError(ValueError):
    """塊の中で、全条件(保持ボディ・拘束面・荷重面)を含む連結成分が無い。

    **`DesignSpaceTooSmallError` とは別の型にしてある。** あちらは「余白を上げろ」と言う文面で、
    分断には効かない直し方を指す —— 分断の直し方は除外ソリッドを削るか条件を置き直すことで、
    余白を上げても塞がれた道は開かない。`ValueError` の派生なので既存の `except ValueError` は
    そのまま効く。

    `components` は塊の連結成分の数、`unreached` は取り残された条件の名前、`seeds` は
    条件ごとの `{"elements": 塊の中の要素数, "labels": 所属する成分}`。条件面の名前は
    `anchor.run_pipeline` が反力グループに付けるものと同じ `fixed_face_<面添字>` /
    `load_face_<面添字>`、保持ボディは `keep_<ボディ名>`。
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


# ---------------------------------------------------------------------------
# 連結性
# ---------------------------------------------------------------------------
# **連結性は 6近傍(面で接する)で定義する。** 26近傍にすると、角1点・稜1本だけで触れている
# 2つの島を「つながっている」と数える。構造格子の六面体要素では、角で触れた2要素が共有するのは
# 節点1つだけで、それは回転を止められないピン接合(機構)にしかならない —— 力の道としては
# 成立しないので、26近傍で見るとこの検査は「切れていない」と言い張りながら宙に浮いた形を通す。
# **6近傍でつながっていることは、部材の幅や剛性を保証しない**(材料が通る道があるというだけ)。
NEIGHBOURHOOD = 6
_RANK_OF_NEIGHBOURHOOD = {6: 1, 18: 2, 26: 3}


def element_components(
    design: np.ndarray, *, neighbourhood: int = NEIGHBOURHOOD
) -> tuple[np.ndarray, int]:
    """塊の連結成分。`(labels, n)` を返す(`labels == 0` は塊の外)。

    `neighbourhood` は連結性の定義で、**塊のガードは常に 6近傍**(`NEIGHBOURHOOD`)。
    18/26 を受けるのは「定義を変えると答えが変わる」ことを検査で見せるためだけの口で、
    導出の経路からは 6 以外を渡さない。
    """
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

    - 塊の中に要素を1つも持たない種があれば `DesignSpaceDisconnectedError`(その名前が
      `unreached`)。除外ソリッドが条件面の要素を全部削った、または保持ボディの占有が
      塊の外にあるケース。
    - 全ての種が共有する成分が1つも無ければ同じ例外(`unreached` は、最も多くの条件が
      属する成分に入っていない条件)。**最大成分を選んで成功にしない。**
    - どの種にも触れない成分だけを落とす。種に触れる成分は(一部の要素しか触れていなくても)残す。
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
    kept = sorted(set().union(*label_sets))
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

    **節点が1つも付かなかった条件面があるときは連結性を見ず、塊もそのまま返す** —— それは
    「要素サイズより小さい面にアンカーが付かない」という別の穴で、`AnchorTable.empty_faces()` を
    見る既存の口(`optimize_run` の `empty_condition_faces` / `run_topopt` の早期return)が
    診断を出す。ここで落とすとその手前で別の例外に化けて原因が分かりにくくなる。
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
            seeds[f"{kind}_face_{int(face)}"] = table.element_mask([int(face)], layers=1)
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

    `roles` は `ROLES` の部分集合(1つのボディが保持で、かつ条件面を持つこともある)。
    `bbox_min` / `bbox_max` は B-rep の外接箱[mm]。**三角形分割から取ってはいけない** ——
    弦は真の面より内側に来るので曲面のぶん小さくなり、しかも同じ形状で表示用の三角形分割が
    先にあるかどうかで値が動く(`voxelize._shape_bbox` / `kernel.bounding_box` の契約)。
    `shape` は保持ボディでは必須(格子への占有を個別に数える)。条件面だけのボディでは省ける。
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
    """形状から外接箱を測って `ConditionBody` を作る(検査・CLI・合成題材の入口)。

    製品(`optimize_run.build_conditions`)は文書側のキャッシュ付き `kernel.bounding_box` で
    同じ B-rep の箱を取る。どちらも `useTriangulation=False` の `BRepBndLib.AddOptimal`。
    """
    lo, hi = vx._shape_bbox(vx._unwrap(shape))
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

    `fixed_faces` / `load_faces` は `anchor_shape`(省略時は `keep`)の三角形群での面添字で、
    **どちらも何枚でもよい**(`int` 1つも受ける)。面添字は渡した形の面の並びで決まるので、
    面を選ぶのに使った形をそのまま渡すこと。

    `bodies` は保持ボディと条件面が属するボディを**重複なく**並べたもの(`ConditionBody`)。
    除外だけのボディ・無関係なボディ・以前の最適化結果を入れてはいけない(塊が広がる)。
    `keep` はそれら保持ボディをまとめた形で、`bodies` の保持ボディと同じ集合を指すこと
    (層をまたぐ契約: 塊が保持を含みきれているかは `keep` で、条件ごとの接続は `bodies` で見る)。

    `load_n` は荷重の**実際の大きさ**[N]。荷重が複数のときは**その大きさの総和**であって
    合力(ベクトル和)ではない —— 向きの違う荷重が打ち消し合うと合力は0になりうるが、
    総和なら0にならない(`optimize_run.build_conditions` / `stress.load_magnitude_total`)。
    最適化そのもの(`simp.optimize` / `run_topopt` の `force` / `forces`)は無次元
    (荷重の総量が1.0)で回り続ける —— `load_n` はここでは使われず、
    `stress.UnitScale.for_model(..., load_n=conditions.load_n)` が応力を実単位[MPa]へ
    換算するときにだけ参照する(`safety_loop.py`)。
    """

    keep: Any
    fixed_faces: Any
    load_faces: Any
    obstacle: Any = None
    anchor_shape: Any = None
    load_n: float = 1.0
    bodies: tuple = field(kw_only=True)

    def __post_init__(self) -> None:
        # `int` 1つも面の並びも受けてタプルへ均す(`frozen=True` なので `object.__setattr__`)。
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

    `mask` は `grid` と同じかたちの bool 配列(塊)で**読み取り専用**。`model` は保持ソリッドを
    同じ格子に載せた `VoxelModel` で、**`model.obstacle` は常に全 False**(除外は塊から
    引き切っているため)。`table` は導出が作った面アンカーの表(条件ごとの接続の確認に使った
    もの)。`hull_planes` は凸包の支持平面 `(n, 4)`(各行 `[a,b,c,d]`、`ax+by+cz+d <= 0` が内側、
    法線は単位長)、`hull_triangles` は凸包の表面の三角形 `(f, 3, 3)`(距離の計算に使ったもの)、
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
        """塊の内容ハッシュ。候補間で塊が同一であることを検査が突き合わせる口。"""
        return mask_digest(self.mask)


def mask_digest(mask: np.ndarray) -> str:
    array = np.ascontiguousarray(np.asarray(mask, dtype=bool))
    digest = hashlib.sha256(json.dumps(list(array.shape)).encode())
    digest.update(np.packbits(array.ravel()).tobytes())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 幾何(点群 → 凸包 → 余白付きの支持平面 → 格子のマスク)
# ---------------------------------------------------------------------------
def condition_corners(bodies: Sequence[ConditionBody]) -> np.ndarray:
    """全ボディの外接箱の頂点(重複除去済み) (m,3)。"""
    if not bodies:
        raise ValueError("ボディが1つも無い")
    corners = np.concatenate([body.corners for body in bodies], axis=0)
    return np.unique(corners, axis=0)


def convex_hull(corners: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict]:
    """点群の凸包。`(支持平面 (n, 4), 表面の三角形 (f, 3, 3), 診断)` を返す。

    支持平面の各行は `[a, b, c, d]` で、`ax + by + cz + d <= 0` が凸包の内側、`(a,b,c)` は
    単位法線(scipy の `ConvexHull.equations` の契約そのまま)。三角形は `ConvexHull.simplices`
    の頂点座標で、凸包からの距離(`hull_distance_mask`)はこれに対して測る。
    **点群が平面・直線・1点に潰れていれば `DesignSpaceGeometryError`** —— Qhull の `QJ`
    (ランダム摂動)で通さない。
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


def offset_hull_mask(grid: vx.Grid, planes: np.ndarray, margin: float) -> np.ndarray:
    """セル中心が、各支持平面を `margin` だけ外へ押し出した半空間の全てに入るセル。

    **塊の定義ではない。** これは凸包から距離 `margin` 以内の領域(`hull_distance_mask`)を
    必ず含む多面体で、角・稜のまわりに余分を持つ。距離の判定を全セルで払わないための
    外側の当たりとして使い、診断にはこの多面体のセル数も残す。判定は `n·x + d <= margin`。
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


def _closest_points_on_triangle(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """点群 `p (N,3)` それぞれに最も近い三角形 `abc` 上の点 (N,3)(Ericson の領域分け)。"""
    ab = b - a
    ac = c - a
    ap = p - a
    d1 = ap @ ab
    d2 = ap @ ac
    bp = p - b
    d3 = bp @ ab
    d4 = bp @ ac
    cp = p - c
    d5 = cp @ ab
    d6 = cp @ ac
    vc = d1 * d4 - d3 * d2
    vb = d5 * d2 - d1 * d6
    va = d3 * d6 - d5 * d4
    q = np.empty_like(p)
    done = np.zeros(p.shape[0], dtype=bool)

    def take(mask: np.ndarray, value: np.ndarray) -> None:
        nonlocal done
        mask = mask & ~done
        q[mask] = value if value.ndim == 1 else value
        done |= mask

    take((d1 <= 0) & (d2 <= 0), a)  # 頂点 A の領域
    take((d3 >= 0) & (d4 <= d3), b)  # 頂点 B
    take((d6 >= 0) & (d5 <= d6), c)  # 頂点 C
    m = (vc <= 0) & (d1 >= 0) & (d3 <= 0) & ~done  # 辺 AB
    if m.any():
        v = d1[m] / (d1[m] - d3[m])
        q[m] = a + v[:, None] * ab
        done |= m
    m = (vb <= 0) & (d2 >= 0) & (d6 <= 0) & ~done  # 辺 AC
    if m.any():
        w = d2[m] / (d2[m] - d6[m])
        q[m] = a + w[:, None] * ac
        done |= m
    m = (va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0) & ~done  # 辺 BC
    if m.any():
        w = (d4[m] - d3[m]) / ((d4[m] - d3[m]) + (d5[m] - d6[m]))
        q[m] = b + w[:, None] * (c - b)
        done |= m
    m = ~done  # 面の内側
    if m.any():
        denom = 1.0 / (va[m] + vb[m] + vc[m])
        v = vb[m] * denom
        w = vc[m] * denom
        q[m] = a + ab * v[:, None] + ac * w[:, None]
    return q


def distance_to_hull(points: np.ndarray, planes: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """点群 `(N,3)` から凸包までのユークリッド距離(内側は 0)。

    凸包は凸なので、外側の点から凸包への最短距離は**表面の三角形への距離の最小**に一致する
    (最近点は必ず境界上にある)。内側は支持平面の全てで `n·x + d <= 0` になる点。
    """
    pts = np.asarray(points, dtype=float).reshape(-1, 3)
    eq = np.asarray(planes, dtype=float)
    signed = pts @ eq[:, :3].T + eq[:, 3]
    inside = np.all(signed <= 0.0, axis=1)
    out = np.zeros(pts.shape[0], dtype=float)
    outside = np.flatnonzero(~inside)
    if outside.size:
        q = pts[outside]
        best = np.full(outside.size, np.inf)
        for a, b, c in np.asarray(triangles, dtype=float):
            closest = _closest_points_on_triangle(q, a, b, c)
            np.minimum(best, np.linalg.norm(q - closest, axis=1), out=best)
        out[outside] = best
    return out


def hull_distance_mask(
    grid: vx.Grid, planes: np.ndarray, triangles: np.ndarray, margin: float
) -> np.ndarray:
    """セル中心が凸包から距離 `margin` 以内にあるセル(凸包と半径 `margin` の球のミンコフスキー和)。

    面のまわりは支持平面を `margin` 押し出したものと同じで、稜・頂点のまわりは丸くなる
    (平面のオフセットより小さい)。判定は `距離 <= margin`(等号を含む)。距離は
    `offset_hull_mask` の多面体に入るセルだけで測る(外は必ず距離 > margin)。
    """
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


# ---------------------------------------------------------------------------
# 導出(本体)
# ---------------------------------------------------------------------------
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

    手順: ボディの外接箱の頂点 → 凸包 → 余白 `m = margin_ratio × 点群の外接箱の最長辺` →
    格子の外枠 = 点群の外接箱を各軸に `m` 広げた箱(`grid_for_bbox`)→ 外枠かつ凸包から距離 `m`
    以内のセル(凸包と球のミンコフスキー和)→ 除外ソリッドの占有を引く → 保持ボディを1つずつ格子に
    載せて除外との衝突・取りこぼしを名指しで数える → 条件ごとの接続を確かめ、条件を含まない
    成分を落とす → 確定マスクで `VoxelModel` を1回組む。

    `spacing` を渡すと `elements_long_axis` の代わりに**要素サイズを固定**する(外枠の大きさから
    要素数を逆算する。丸めのぶん実 spacing は指定値と厳密には一致しない)。
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
    if not longest > 0:
        raise DesignSpaceGeometryError("条件の点群が1点に潰れている(外接箱が起こせない)")
    margin = ratio * longest
    planes, triangles, hull_info = convex_hull(corners)
    box_lo = cloud_lo - margin
    box_hi = cloud_hi + margin

    if spacing is None:
        n_long = int(elements_long_axis)
    else:
        n_long = max(1, int(round(float(np.max(box_hi - box_lo)) / float(spacing))))
    grid = vx.grid_for_bbox(box_lo, box_hi, n_long)
    # **保持/除外を格子に載せるときの三角形分割は要素サイズから決める。** `anchor.run_pipeline` は
    # `model_from_design_mask` を deviation 無しで呼ぶ(= 要素サイズの `DEFAULT_DEVIATION_RATIO` 倍)
    # ので、ここを揃えておかないとここで通した保持が run_pipeline では通らない。
    dev = float(deviation) if deviation else max(grid.spacing * deviation_ratio, 1e-6)

    frame = box_mask(grid, box_lo, box_hi)
    # 平面のオフセット(多面体)は距離 m 以内の領域を含む外側の当たり。塊にするのは距離のほう。
    polytope = frame & offset_hull_mask(grid, planes, margin)
    candidate = polytope & hull_distance_mask(grid, planes, triangles, margin)
    if not candidate.any():
        raise DesignSpaceGeometryError("余白付き凸包に入るセル中心が1つも無い(格子が粗すぎる)")

    obs: Optional[np.ndarray] = None
    obstacle_stats: dict = {}
    design = candidate
    if conditions.obstacle is not None:
        obs_soup = vx.triangles_from_shape(
            vx._unwrap(conditions.obstacle), deviation=dev, angular_deflection=angular_deflection
        )
        grid_lo, grid_hi = grid.world_box()
        obs_lo, obs_hi = obs_soup.bbox
        if np.any(obs_hi < grid_lo) or np.any(obs_lo > grid_hi):
            # 格子とまったく重ならない除外は引くものが無い(`occupancy` はレイが1本も当たらない
            # 形を縮退として落とすので、ここで先に見分ける)。格子に重なる除外の縮退は落とす。
            obstacle_stats = {"skipped": "outside_grid"}
        else:
            obs, obstacle_stats = vx.occupancy(obs_soup, grid)
            design = candidate & ~obs
    obstacle_elements_removed = int(np.count_nonzero(candidate & ~design))
    if not design.any():
        raise DesignSpaceTooSmallError(
            "除外ソリッドを引いたら塊が空になった(除外が余白付き凸包を覆い尽くしている)。",
            {"obstacle": -1},
        )

    # **格子の外は voxelize が見ていない。** 外枠は保持ボディの箱から起こしているので通常は
    # 出ないが、渡された外接箱と `keep` の形が食い違っていると黙って消える —— 先に落とす。
    grid_lo, grid_hi = grid.world_box()
    k_lo, k_hi = vx._shape_bbox(vx._unwrap(conditions.keep))
    if np.any(k_lo < grid_lo - 1e-9) or np.any(k_hi > grid_hi + 1e-9):
        raise DesignSpaceTooSmallError(
            f"保持ソリッド {np.round(k_lo, 3).tolist()} .. {np.round(k_hi, 3).tolist()} が"
            f"格子 {np.round(grid_lo, 3).tolist()} .. {np.round(grid_hi, 3).tolist()} の外へ出ている。"
            "bodies の外接箱が keep の形と合っていない(格子の外にはセル中心が無いので、"
            "このままだとはみ出しが件数にも例外にも現れない)。",
            {"keep": -1},
        )

    bodies_report, keep_seeds, conflicts, unrepresented = _keep_bodies_on_grid(
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
        vx._unwrap(conditions.anchor), deviation=dev, angular_deflection=angular_deflection
    )
    # アンカーの表は `run_pipeline` と同じ作り方(全面・既定の半径)で作る。`faces=` で2面に
    # 絞ると稜線の規則(節点は最も面積を配った面が持つ)の競争相手が減り、**向こうとは別の
    # 節点集合**になる —— 同じものを見ているつもりで違うものを見ることになる。
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
        model = vx.model_from_design_mask(
            grid,
            connected,
            anchor_shape=soup,
            keep=conditions.keep,
            deviation=dev,
            angular_deflection=angular_deflection,
        )
    except vx.OutsideDesignSpaceError as exc:
        raise DesignSpaceConflictError(
            f"まとめた保持ソリッド(keep)が塊からはみ出した: {exc} "
            "ボディごとの占有では衝突が無かったので、keep と bodies が別の形を指しているか、"
            "保持ボディ同士が重なっていて巻き数が相殺している。",
            bodies={"keep": int(getattr(exc, "outside", {}).get("keep", 0))},
        ) from exc

    final = np.array(connected, dtype=bool)
    for array in (final, model.design, model.keep, model.obstacle):
        array.setflags(write=False)

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
    )


def _keep_bodies_on_grid(
    bodies: Sequence[ConditionBody],
    grid: vx.Grid,
    design: np.ndarray,
    obstacle: Optional[np.ndarray],
    deviation: float,
    angular_deflection: float,
) -> tuple[list, dict, dict, list]:
    """保持ボディを**1つずつ**格子に載せ、占有・除外との衝突・取りこぼしを名前付きで数える。

    まとめた形で数えると、大きい保持が1つあれば合計は必ず正になり、小さいほうが1要素も
    掴めていないことが隠れる(`optimize_run._region_representation` と同じ理屈)。
    戻り値は `(ボディごとの報告, 接続の種 {keep_<名前>: マスク}, 衝突 {名前: セル数}, 取りこぼした名前)`。
    取りこぼし(0セル)は解像度不足であって入力矛盾ではないので、ここでは落とさず報告だけする
    —— `optimize_run` が `unrepresented_regions` として利用者へ返す。
    """
    report: list = []
    seeds: dict = {}
    conflicts: dict = {}
    unrepresented: list = []
    for body in bodies:
        if "keep" not in body.roles:
            report.append(
                {"name": body.name, "roles": list(body.roles), "bbox": [list(body.bbox_min), list(body.bbox_max)]}
            )
            continue
        soup = vx.triangles_from_shape(
            vx._unwrap(body.shape), deviation=deviation, angular_deflection=angular_deflection
        )
        reason = None
        try:
            raw, _ = vx.occupancy(soup, grid)
        except ValueError as exc:
            # 格子の列(セル中心を通る +z のレイ)が1本もその形状を貫かない = この要素サイズでは
            # 表現できていない。異常ではなく取りこぼしとして数える。
            raw = np.zeros(tuple(grid.shape), dtype=bool)
            reason = str(exc)
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
                "reason": reason,
            }
        )
        if in_obstacle:
            conflicts[body.name] = in_obstacle
        if cells == 0:
            unrepresented.append(body.name)
        else:
            seeds[f"keep_{body.name}"] = raw & design
    return report, seeds, conflicts, unrepresented


# ---------------------------------------------------------------------------
# 端から端まで(塊 → 面アンカー → SIMP → marching cubes → STL)
# ---------------------------------------------------------------------------
@trace.stage("candidate")
def run_topopt(
    space: DesignSpace,
    *,
    volfrac: Optional[float] = None,
    material_mm3: Optional[float] = None,
    rmin: Optional[float] = None,
    rmin_mm: Optional[float] = None,
    max_iter: int = 60,
    force: Sequence[float] = (0.0, 0.0, -1.0),
    forces: Optional[Sequence[Sequence[float]]] = None,
    stl_path: Optional[str | Path] = None,
    progress: Any = None,
    solver: Optional[str] = None,
    threads: Optional[int] = None,
    update: Optional[str] = None,
    reaction_min: Optional[float] = None,
    keep_model: bool = False,
    dynamic_active: bool = False,
) -> dict:
    """導出した塊を端から端まで回す(塊 → 面アンカー → SIMP → marching cubes → STL)。

    `stl_path` は**必ず渡すか、既定のこの関数の出力先に任せる**。`anchor.run_pipeline` の既定は
    `output/bracket.stl` で、それは `check_anchor.py` の成果物である。

    体積目標は `volfrac`(分率)か `material_mm3`(材料の総体積[mm^3])のどちらか一方
    (両方渡すと例外)。**塊を自動で導出する経路では絶対量を使うこと** —— 余白比率や姿勢で
    塊の大きさが変わると、同じ分率でも材料の量が変わってしまう。どちらも渡さなければ
    `simp.DEFAULT_VOLFRAC`。

    密度フィルタ半径は `rmin`(要素単位)か `rmin_mm`(mm)のどちらか一方(排他)。
    どちらも渡さなければ `simp.DEFAULT_RMIN`(**値は動く。床の `simp.MIN_RMIN` とは別物**)。
    `anchor.run_pipeline` へそのまま渡す(要素サイズはそちらが格子から決める)。

    `keep_model=True` / `dynamic_active=True` / `update` / `reaction_min` / `force` / `forces` /
    `solver` / `threads` は `anchor.run_pipeline` の同名の引数へそのまま渡る(意味の正はそちら)。
    **どれも既定のままなら経路は1ビットも変わらない。**

    戻り値の `"reactions"` は拘束面1枚ごとの反力(`anchor.run_pipeline` の契約そのまま)。

    拘束面・荷重面が要素サイズより小さくて格子節点が1つも付かないと
    (`anchor.AnchorTable.empty_faces()`)、**例外にはせず**戻り値の
    `"empty_condition_faces"` に診断が積まれて早期returnする(`anchor.run_pipeline` の契約
    そのまま)。同じ診断を `space.stats["empty_condition_faces_warning"]` にも書く
    (`DesignSpace` は呼び出し元がそのまま持っているので、戻り値を経由しなくても後から
    見える。docs/TOPOPT.md「面が小さすぎて空になる」参照)。**塊そのものは書き換えない。**
    """
    out = Path(stl_path) if stl_path is not None else _HERE / "output" / "design_space_case1.stl"
    result = an.run_pipeline(
        shape=space.soup,
        design_mask=space.mask,
        design_grid=space.grid,
        keep=space.conditions.keep,
        # 除外は塊から引き切ってある(モジュール docstring の設計判断)。
        obstacle=None,
        fixed_faces=space.fixed_faces,
        load_faces=space.load_faces,
        volfrac=volfrac,
        material_mm3=material_mm3,
        rmin=rmin,
        rmin_mm=rmin_mm,
        max_iter=max_iter,
        force=force,
        forces=forces,
        stl_path=out,
        progress=progress,
        solver=solver,
        threads=threads,
        update=update,
        reaction_min=reaction_min,
        keep_model=keep_model,
        dynamic_active=dynamic_active,
    )
    # `run_pipeline` は保持ソリッドを**自分でもう一度**格子へ載せる。三角形分割の細かさが
    # `derive` と食い違うと、ここで通した塊が向こうでは通らない(逆もある)。揃えてあることを
    # 数字で確かめる —— 揃っていなければ黙って別のものを最適化していることになる。
    if not np.array_equal(np.asarray(result["model"].keep), np.asarray(space.model.keep)):
        raise ValueError(
            "derive() と run_pipeline() で保持領域の要素が食い違った"
            f"({int(space.model.keep.sum())} 要素 → {int(result['model'].keep.sum())} 要素)。"
            "derive() に deviation を明示的に渡していないか確認すること"
            "(run_pipeline は要素サイズから決め直す)。"
        )
    if result.get("empty_condition_faces"):
        space.stats["empty_condition_faces_warning"] = result["empty_condition_faces"]
    return result


# ---------------------------------------------------------------------------
# 合成の4条件(受入検査と実測が共有する題材。**私物STEPに依存しない**)
# ---------------------------------------------------------------------------
def demo_conditions(angle_deg: float = 0.0) -> Conditions:
    """4条件だけの合成題材。**設計空間ソリッドは作らない。**

    - 保持ソリッド: 固定側パッド(x=-30の板)と荷重側パッド(x=+26,z=15の小箱)の2つ。
      利用者が「ここは必ず材料を残す」と指定した部分に相当する。両者は離れており、
      **その間をどう繋ぐかが最適化の答え**になる。
    - 除外ソリッド: 2つのパッドの間を y 方向に貫く円柱(配線・ボルトの通り道に相当)。
    - 拘束面: 固定側パッドの x 最小面。荷重面: 荷重側パッドの上面。
      **どちらも保持ソリッドの面**(利用者が画面でクリックできるのは自分が置いた形の面だけ)。

    `angle_deg` は世界の z 軸まわりの回転。**外接箱は軸整列なので、回すと塊が膨らむ**
    —— その姿勢依存を測るための口。
    """
    from build123d import Box, Cylinder, Pos, Rot

    rot = Rot(0, 0, float(angle_deg))
    fixed_pad = rot * (Pos(-30, 0, 0) * Box(6, 24, 24))
    load_pad = rot * (Pos(26, 0, 15) * Box(8, 8, 6))
    keep = rot * ((Pos(-30, 0, 0) * Box(6, 24, 24)) + (Pos(26, 0, 15) * Box(8, 8, 6)))
    obstacle = rot * (Pos(0, 0, 0) * Rot(90, 0, 0) * Cylinder(radius=5, height=40))

    soup = vx.triangles_from_shape(vx._unwrap(keep), deviation=0.2)
    fixed = _rotated_face(soup, angle_deg, normal=(-1, 0, 0), point=(-33, 0, 0))
    load = _rotated_face(soup, angle_deg, normal=(0, 0, 1), point=(26, 0, 18))
    bodies = (
        condition_body("fixed_pad", fixed_pad, roles=("keep", "fixed")),
        condition_body("load_pad", load_pad, roles=("keep", "load")),
    )
    return Conditions(
        keep=keep, fixed_faces=fixed, load_faces=load, obstacle=obstacle, bodies=bodies
    )


def severed_conditions(angle_deg: float = 0.0, *, thickness: float = 10.0) -> Conditions:
    """`demo_conditions` の除外ソリッドを、**拘束と荷重の間を丸ごと塞ぐ壁**に差し替えた配置。

    壁は2つのパッドの間(x=0)に立て、y/z 方向は塊よりずっと大きく取る —— 塊の断面を端まで
    塞がないと、**縁を回り込む道が残って「切れている」配置にならない**(既定の余白比率 0.20 で
    塊は y -25..25 / z -25..31 程度なので、±100 あれば余白比率を振っても足りる)。

    `thickness` は壁の厚み[mm]。**要素サイズより十分厚くすること** —— 要素中心が壁に入らないと
    塊が1要素も削れず、塞いだつもりの配置がつながったまま通る。
    """
    from build123d import Box, Pos, Rot

    base = demo_conditions(angle_deg)
    wall = Rot(0, 0, float(angle_deg)) * (Pos(0, 0, 0) * Box(float(thickness), 200, 200))
    return Conditions(
        keep=base.keep,
        fixed_faces=base.fixed_faces,
        load_faces=base.load_faces,
        obstacle=wall,
        anchor_shape=base.anchor_shape,
        bodies=base.bodies,
    )


def _rotated_face(
    soup: vx.TriangleSoup, angle_deg: float, *, normal: Sequence[float], point: Sequence[float]
) -> int:
    """回す前の法線・重心で指定した面を、回したあとの三角形群から一意に選ぶ。

    面添字が回転で変わらないことに賭けない(OCCT の面の並びは形の作り方に依存するので、
    「回しても同じ添字のはず」は検査が守っていない仮定になる)。
    """
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    rot = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    n = rot @ np.asarray(normal, dtype=float)
    p = rot @ np.asarray(point, dtype=float)
    hits = an.faces_matching(
        soup, kind="plane", normal=n, near_point=p, normal_tol=1e-6, near_tol=1e-4
    )
    if len(hits) != 1:
        raise ValueError(
            f"デモの面を一意に選べなかった(法線={np.round(n, 4).tolist()}"
            f" 重心={np.round(p, 4).tolist()} → {hits})。demo_conditions を変えたら見直すこと"
        )
    return hits[0]


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="4条件から塊(設計空間)を導出する")
    parser.add_argument("--margin-ratio", type=float, default=DEFAULT_MARGIN_RATIO)
    parser.add_argument("--elements-long-axis", type=int, default=28)
    args = parser.parse_args()

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    space = derive(
        demo_conditions(0.0),
        margin_ratio=args.margin_ratio,
        elements_long_axis=args.elements_long_axis,
    )
    print(json.dumps(space.stats, ensure_ascii=False, default=float, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

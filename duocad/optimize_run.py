"""文書の条件を数値計算へ渡し、平滑化後の保持・除外ブーリアンを適用してCADを追加する。

optimize.py は条件の保存・配信、ここは条件の点検・実行・進捗・結果の追加を担当する。元のボディは変更しない。

面は (body_id, 1始まりの面添字) で指定する。複数ボディを compound にまとめると
面添字がずれるため、_verify_face_mapping が面積・法線・重心を照合する。
設計理由と実測は docs/TOPOPT.md の「設計と画面」「本体への配線」を参照。"""
from __future__ import annotations

import math
import time
import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover - 型注釈のためだけ
    from .document import Document

#: 結果ボディの名前の頭。同じ文書で2回走らせると `optimized-2` になる。
RESULT_NAME = "optimized"

#: 設計空間の要素数(塊。空要素は `simp.Model(active_mask=...)` で外れる)の上限 = 要素サイズの下限を決める。
#: 130万は本人判断(2026-09-26: 「1.6mm が通る130万」)。
#:
#: 格子の箱の数でなく塊の数で見る: hanger は箱の約42%しか塊にならず、箱の数の上限ではモデルの形でメモリが約2.4倍振れる。
#: 実行前(`resolution_limits`)は幾何の見積もり(`_design_element_estimate`)、実行時は `derive` 後の
#: 実数(`space.element_count`)で判定する。誤差は docs/TOPOPT.md「要素サイズの下限」。
#:
#: **これより粗ければ通る、とは限らない。** `models/hanger_bar_attachment.json` は薄い部品(5.00mm)があり、
#: 粗いと事前検出(`_region_representation`)で止まる。通るかは要素サイズに対して単調でない
#: (2026-09-09 実測: 4.2mm 止まる / 4.4 通る / 4.6 止まる / 4.8 通る / 5.333 止まる。格子の
#: 原点と刻みが要素サイズごとに引き直され、薄い部品にセル中心が入るかが偶然で決まる)。
MAX_DESIGN_ELEMENTS = 1_300_000

#: 要素サイズの上限の固定値[mm](2026-10-09 本人決定、#130)。上限は min(これ, 条件面からの要求)。条件面だけで決めると
#: 面の大きな題材で格子が粗くなり、完成形の検査(`topopt.final_check`)が拘束面を割り当て直せず安全率を測れない
#: (実測: path 題材は条件面 200mm² で上限 14mm になり測れず、5mm・2.5mm では測れた)。4mm で必ず測れる保証ではない。
MAX_ELEMENT_SIZE_MM = 4.0

#: 最小幅の段の下限[mm]と上限(格子の最短の辺に対する割合)。**根拠の無い暫定値**(2026-10-09 本人決定)。
#: 選べる m は「m × 要素サイズ ≥ この下限」を満たす最小の m(ただし m ≥ 2)から、
#: 「m ≤ 格子の最短の辺のセル数 × この割合」の最大の m まで(`min_width_cell_range`)。
MIN_WIDTH_FLOOR_MM = 2.0
MIN_WIDTH_CEILING_RATIO = 0.25

#: 保持/除外ソリッドが「この解像度では表現できていない」と見なす要素数の境目。
#: **0 は必ず止める**(その条件が黙って消える)。0 より多く これ未満は警告して続ける。暫定値。
MIN_REGION_ELEMENTS = 8

#: 解像度の梯子の段数(既定=いちばん粗い、を含む)。粗い側の端から細かい方向へだけ動くスライダの目盛りで、
#: 画面はこの並びの添字を持つ(`resolution_limits()["steps"]`)。各段は要素サイズ・要素数だけを持ち時間を
#: 付けないので、「時間から解像度を逆算する」経路が画面に生えない。
RESOLUTION_STEPS = 6

#: 目標安全率の既定。**利用者が入れる値**で、ここにあるのは画面が最初に出す初期値でしかない。
DEFAULT_TARGET_SAFETY_FACTOR = 2.0

#: 探索に渡す目標安全率 = 利用者の目標 × この係数(m に依らず1つ)。探索の格子(k=1)は、細い部材の
#: 曲げ・ねじりを要素中心の応力から見落として安全率を過大に出す。2.4 は最小幅 m≥2 セルの直線部材で
#: その過大評価が取る上限(m=2 の曲げ 2.36)の切り上げ。m=1 には上限が無いが、`_resolve_min_width` の
#: 既定(2026-09-22決定)により製品の実行は m=1 に到達しない。根拠と実測・限界は docs/TOPOPT_TARGET_FACTOR.md。
SEARCH_TARGET_FACTOR = 2.4

#: デバッグ表示(`?debug=space` / `?voxel=1`)で1回に配るボクセルの上限。表示は `InstancedMesh`
#: (立方体1個 = 12三角形)なので、5〜12万要素の塊をそのまま出すと 60〜144万三角形になる。
#: 超えたら軸方向に等間隔で間引き(`_debug_voxel_payload` の `stride`)、payload の `stride`/`total`/`shown`
#: に出す。間引きは表示だけで、計算には触れない。
DEBUG_VOXEL_LIMIT = 150_000

#: デバッグ配信の種類。`run()` の `debug_kinds` に渡せる値。
DEBUG_KINDS = ("space", "anchors", "rho")

#: `?voxel=1` のボクセル表示で「材料がある」と見なす密度の境目(`np.flatnonzero(flat > 0.5)`)。
#: `shoot-topopt.mjs` と揃える(不一致だと同じ密度場が別の形に見える)。
DEBUG_RHO_THRESHOLD = 0.5


class OptimizeInputError(ValueError):
    """条件がそろっていない・この版では扱えない(`ValueError` = MCPの `invalid_argument`)。
    利用者に見せる文がそのまま入る。
    """


class FaceMappingError(RuntimeError):
    """条件面と `TriangleSoup` の面添字の対応が取れなかった(静かな事故の門)。

    `ValueError` にしないのは握り潰されないため。そのまま走らせると荷重が別の面に入ったまま最適化が成功する。
    """


class OptimizeCancelled(RuntimeError):
    """`should_cancel()` が真、または探索が `termination='cancelled'` で戻ったので途中で降りた
    (`docs/contracts/ui-optimize-integration.md`「最適化の配線」)。

    `RuntimeError` なのは `acceptance.inspect` 内の例外処理に拾われないため(`ValueError` だと入力不備と分類され探索が続く)。
    """


@dataclass
class OptimizeOptions:
    """実行の摘み。既定値は「条件を満たす、最も粗くて安い設定」(本人決定)。

    `element_size_mm` を省くと `resolution_limits()` の上限側を採る。密度フィルタ半径(`rmin_mm`)・SIMP反復数
    (`max_iter`)・材料量の直接指定(`material_min_mm3`/`material_max_mm3`)は無い(二値探索は連続密度も
    SIMP反復も持たない)。届いたら黙って無視せず `OptimizeInputError`(`session.run_optimization` / `resolution_limits` の入口)。
    """

    #: 目標安全率。許容応力は固定(`allowable_stress()`)なので、利用者が動かすのはこちら。
    target_safety_factor: float = DEFAULT_TARGET_SAFETY_FACTOR
    #: 探索に渡す目標 = `target_safety_factor` × これ(`SEARCH_TARGET_FACTOR`)。UI・WebSocket の入力には出さない。
    #: 1.0 はテストと測定が「係数なし」に固定する口。1.0 未満・非有限は断る。
    search_target_factor: float = SEARCH_TARGET_FACTOR
    element_size_mm: Optional[float] = None
    #: 最小幅[mm]。省くと既定 = 選べるいちばん細い段(`min_width_cell_range` の下限の m)。
    min_width_mm: Optional[float] = None
    #: 形状探索の評価予算(`binary_search.find_best_feasible` の `max_evaluations`。段階3・4通しの1本)。
    max_evaluations: Optional[int] = None


# --------------------------------------------------------------------------------------
# 固定している材料の許容応力
# --------------------------------------------------------------------------------------
def allowable_stress() -> dict:
    """許容応力[MPa]とその導出。値を持つのは `duocad.topopt.material` の1箇所だけで、ここは読んで並べ直すだけ。
    """
    from .topopt import material

    return {
        "material": "PETG",
        "yield_mpa": float(material.PETG_YIELD_MPA),
        "print_derate": float(material.PRINT_DERATE),
        "allowable_mpa": float(material.DEFAULT_ALLOWABLE_MPA),
    }


# --------------------------------------------------------------------------------------
# 条件の点検(走らせる前に、何が足りないかを名指しする)
# --------------------------------------------------------------------------------------
def conditions_report(doc: "Document") -> dict:
    """4条件が走らせられる状態かを返す。画面はこれで実行ボタンの可否を決める。

        {"ready": bool, "problems": [str, ...],
         "preserve_bodies": [BodyId,...], "exclude_bodies": [BodyId,...],
         "fixtures": [FaceCondition,...], "loads": [LoadCondition,...],
         "load_total_n": float}   # 荷重の大きさの総和[N](合力ではない)

    `state_payload()` の `stale`(いま画面で指せるか)とは別の点検で、こちらは最適化に渡せるか
    (非表示のボディは指せないが渡せる)。
    """
    from . import optimize

    setup = doc.optimize
    problems: List[str] = []

    if not setup.preserve_bodies:
        problems.append(
            "保持ソリッドが1つも無い。塊(設計空間)は保持ソリッドと条件面から起こすので、"
            "残したい部分を set_preserve_bodies で指定すること"
        )
    if not setup.fixtures:
        problems.append("拘束面が1枚も無い")
    if not setup.loads:
        problems.append("荷重面が1枚も無い")
    for condition in setup.loads:
        if not (float(condition.get("magnitude") or 0.0) > 0.0):
            problems.append(
                f"荷重面 {condition['body']} の面 #{int(condition['index'])} に"
                "大きさ[N]が入っていない(set_loads の magnitude)"
            )

    for role in optimize.BODY_ROLES:
        for body_id in getattr(setup, role):
            problems.extend(_body_problem(doc, role, body_id))
    for role in optimize.FACE_ROLES:
        for condition in getattr(setup, role):
            problems.extend(_face_problem(doc, role, condition))

    return {
        "ready": not problems,
        "problems": problems,
        "preserve_bodies": list(setup.preserve_bodies),
        "exclude_bodies": list(setup.exclude_bodies),
        "fixtures": [dict(item) for item in setup.fixtures],
        "loads": [dict(item) for item in setup.loads],
        "load_total_n": sum(
            float(item.get("magnitude") or 0.0) for item in setup.loads
        ),
    }


def _body_problem(doc: "Document", role: str, body_id: str) -> List[str]:
    from . import meshbody, optimize

    label = optimize.ROLE_LABEL[role]
    try:
        body = doc.get_body(body_id)
    except KeyError:
        return [f"{label}のボディ {body_id} が文書に無い"]
    if body.mesh is not None:
        # 過去のログを再生した文書には入りうるので、入口(`verbs/optimize_setup.py`)に加えてここでも断る。
        try:
            meshbody.reject(body, f"{label}に指定する")
        except Exception as error:  # noqa: BLE001 - 文面だけを取り出す
            return [str(error)]
    if body.shape is None:
        return [f"{label}のボディ {body.name} に形が無い"]
    return []


def _face_problem(doc: "Document", role: str, condition: dict) -> List[str]:
    from . import optimize

    label = optimize.ROLE_LABEL[role]
    problems = _body_problem(doc, role, condition["body"])
    if problems:
        return problems
    if condition["body"] not in doc.optimize.preserve_bodies:
        # 条件面は保持ソリッドの面に限る(2026-09-18 本人指示)。入口(`verbs/optimize_setup.py`)に加え、
        # 過去のログを再生した文書にも効かせるためここでも断る。
        name = doc.get_body(condition["body"]).name
        return [
            f"{label}が保持ソリッドでないボディ {name} の面に載っている。"
            "そのボディを保持ソリッドに指定するか、条件を置き直すこと"
        ]
    reason = optimize.fingerprint_mismatch(doc, condition)
    if reason is not None:
        return [f"{label}が指す面が今の形と合わない({reason})。条件を置き直すこと"]
    return []


# --------------------------------------------------------------------------------------
# 4条件 → probe の Conditions
# --------------------------------------------------------------------------------------
@dataclass
class ConditionBundle:
    """`Conditions` と、そこへ至る対応表。**検査が突き合わせるのはこれ**。"""

    conditions: Any  # duocad.topopt.design_space.Conditions
    anchor_body_ids: List[str]
    face_offsets: Dict[str, int]
    fixed_faces: tuple
    load_faces: tuple
    keep_body_ids: List[str]
    exclude_body_ids: List[str]
    #: 荷重面と同じ並びの力ベクトル[N]。`load_faces[i]` に `forces[i]`。
    forces: tuple


def build_conditions(doc: "Document") -> ConditionBundle:
    """4条件を `duocad.topopt.design_space.Conditions` へ翻訳する。

    - 保持ソリッド → `keep`(複数なら `kernel.make_compound`)、除外ソリッド → `obstacle`(空なら `None`)
    - 拘束面 / 荷重面 → `anchor_shape` の面添字(`fixed_faces` / `load_faces`)
    - 保持ボディ ∪ 条件面のボディ → `bodies`(役割と B-rep の外接箱。塊の凸包の元。除外だけのボディは入れない)
    - 荷重 → `forces`(面ごとの実単位[N]の力ベクトル。探索はこれをそのまま受け、面アンカーが節点へ配る)

    「荷重が全部0」の門は大きさの総和で見る(打ち消し合う組では合力が0になるが、総和は0にならない)。
    """
    from . import optimize
    from .topopt import design_space as ds

    report = conditions_report(doc)
    if not report["ready"]:
        raise OptimizeInputError("。".join(report["problems"]))

    setup = doc.optimize
    keep_ids = list(setup.preserve_bodies)
    exclude_ids = list(setup.exclude_bodies)
    fixtures = [dict(item) for item in setup.fixtures]
    loads = [dict(item) for item in setup.loads]

    # 面アンカーの対象は条件面を持つボディだけ。文書の並び順で固定する(集合の反復順では面添字が変わりうる)。
    anchor_ids: List[str] = []
    wanted = {item["body"] for item in fixtures} | {item["body"] for item in loads}
    for body in doc.bodies:
        if body.id in wanted:
            anchor_ids.append(body.id)

    anchor_shape, offsets = _compound_with_offsets(doc, anchor_ids)
    _verify_face_mapping(doc, anchor_shape, offsets, fixtures + loads)

    keep_shape, _ = _compound_with_offsets(doc, keep_ids)
    obstacle_shape = None
    if exclude_ids:
        obstacle_shape, _ = _compound_with_offsets(doc, exclude_ids)

    fixed_faces = tuple(
        offsets[item["body"]] + int(item["index"]) for item in fixtures
    )
    load_faces = tuple(offsets[item["body"]] + int(item["index"]) for item in loads)
    forces = tuple(tuple(optimize.load_force(item)) for item in loads)
    load_total = sum(math.sqrt(sum(c * c for c in vector)) for vector in forces)
    if not load_total > 0.0:
        raise OptimizeInputError("荷重の大きさが全部0で、実単位との対応が決まらない")

    bodies = tuple(
        ds.ConditionBody(
            name=body_id,
            roles=roles,
            bbox_min=box["min"],
            bbox_max=box["max"],
            shape=doc.get_body(body_id).shape,
        )
        for body_id, roles, box in _condition_boxes(doc)
    )
    conditions = ds.Conditions(
        keep=keep_shape,
        fixed_faces=fixed_faces,
        load_faces=load_faces,
        obstacle=obstacle_shape,
        anchor_shape=anchor_shape,
        bodies=bodies,
    )
    return ConditionBundle(
        conditions=conditions,
        anchor_body_ids=anchor_ids,
        face_offsets=offsets,
        fixed_faces=conditions.fixed_faces,
        load_faces=conditions.load_faces,
        keep_body_ids=keep_ids,
        exclude_body_ids=exclude_ids,
        forces=forces,
    )


def _compound_with_offsets(doc: "Document", body_ids: List[str]) -> tuple:
    """ボディの並び → (まとめた形, body_id → 面添字のオフセット)。

    1つだけならまとめない。複数のときだけ `kernel.make_compound` で1つにし、面添字は `face_count` の累積でずらす。
    """
    from . import kernel

    if not body_ids:
        raise OptimizeInputError("形をまとめる対象が1つも無い")
    offsets: Dict[str, int] = {}
    total = 0
    shapes = []
    for body_id in body_ids:
        body = doc.get_body(body_id)
        if body.shape is None:
            raise OptimizeInputError(f"ボディ {body.name} に形が無い")
        offsets[body_id] = total
        total += kernel.face_count(body.shape)
        shapes.append(body.shape)
    if len(shapes) == 1:
        return shapes[0], offsets
    return kernel.make_compound(shapes), offsets


def _verify_face_mapping(
    doc: "Document", anchor_shape: Any, offsets: Dict[str, int], conditions: List[dict]
) -> None:
    """まとめた形の面添字が本当に同じ面を指しているかを測り直す。合わなければ `FaceMappingError`
    (外すと荷重が別の面に入ったまま最適化が収束する)。
    """
    from . import query

    for condition in conditions:
        body = doc.get_body(condition["body"])
        local = int(condition["index"])
        mapped = offsets[condition["body"]] + local
        want = query.face_fingerprint(body.shape, local)
        try:
            got = query.face_fingerprint(anchor_shape, mapped)
        except ValueError as error:
            raise FaceMappingError(
                f"ボディ {body.name} の面 #{local} を、まとめた形の面 #{mapped} へ対応させたが"
                f"その添字が範囲外だった({error})"
            ) from error
        if not _same_face(want, got):
            raise FaceMappingError(
                f"ボディ {body.name} の面 #{local} と、まとめた形の面 #{mapped} が別の面である"
                f"(面積 {want['area']:.6g} / {got['area']:.6g}、"
                f"重心 {_fmt(want['center'])} / {_fmt(got['center'])})。"
                "面添字の対応が壊れているので最適化は走らせない"
            )


def _same_face(left: dict, right: dict) -> bool:
    """面積・法線・重心が一致するか(同じ面を2回測ったのかを見る)。"""
    if not _close(left["area"], right["area"]):
        return False
    return _close_seq(left["normal"], right["normal"]) and _close_seq(
        left["center"], right["center"]
    )


def _close(left: float, right: float) -> bool:
    # 本来は完全一致する。桁落ちだけを吸う幅(緩めると「別の面で面積が近い」を通してしまう)。
    return math.isclose(float(left), float(right), rel_tol=1e-7, abs_tol=1e-7)


def _close_seq(left, right) -> bool:
    return len(left) == len(right) and all(_close(a, b) for a, b in zip(left, right))


def _fmt(values) -> str:
    return "[" + ", ".join(f"{float(v):.4g}" for v in values) + "]"


def _not_done(
    common: dict,
    started: float,
    keep_room_warning,
    *,
    state: str,
    warning: str,
    unrepresented_regions: list,
    boundary_mapping,
    evaluations: int,
    termination,
    empty_condition_faces=None,
) -> dict:
    """結果を作れずに戻るときの応答(`state="done"` 以外の4経路)。

    値の無い欄は None に固定し、`keep_room_warning` はここで繋ぐ(経路ごとに繋ぐと繋ぎ忘れる。
    `tests/test_optimize_backend.py` の "rides on every early return" が固定する)。
    """
    return dict(
        common,
        state=state,
        body_id=None,
        name=None,
        warning=_join_warnings(warning, keep_room_warning),
        empty_condition_faces=[] if empty_condition_faces is None else empty_condition_faces,
        unrepresented_regions=unrepresented_regions,
        boundary_mapping=boundary_mapping,
        safety_factor=None,
        material_mm3=None,
        evaluations=evaluations,
        termination=termination,
        triangle_count=None,
        vertex_count=None,
        final_safety_factor=None,
        final_check=None,
        seconds=time.perf_counter() - started,
    )


# --------------------------------------------------------------------------------------
# 解像度(要素サイズ)をどう決めるか
# --------------------------------------------------------------------------------------
def resolution_limits(doc: "Document", element_size_mm: Any = None) -> dict:
    """要素サイズの**上限**(これより粗いと間違う)と**下限**(これより細かくはできない)。

    `element_size_mm` を渡すと、その値を選んだときの段(`steps` の1件と同じ形)を `selected` に、
    範囲外・不正なら理由を `selected_error` に入れ、渡した値を `requested_element_size_mm` に返す(画面の自由入力)。

        {"max_element_size_mm": float,        # min(MAX_ELEMENT_SIZE_MM, 条件面からの要求)
         "min_element_size_mm": float,        # 設計空間の要素数の上限(見積もり)から
         "default_element_size_mm": float,    # 既定 = 上限側(いちばん粗くて安い)
         "from_condition_faces_mm": float,    # min(sqrt(条件面のCAD面積))
         "max_element_size_cap_mm": float,    # MAX_ELEMENT_SIZE_MM
         "condition_faces": [{...}, ...],
         "bbox_min"/"bbox_max": [x,y,z],      # 条件の外接直方体(余白なし)
         "max_design_elements": int}

    上限は固定値 `MAX_ELEMENT_SIZE_MM` と条件面からの要求(拘束面・荷重面に格子節点が付く大きさ)の細かい方。
    条件面からの要求は `anchor.AnchorTable.empty_face_diagnosis` の `suggested_max_spacing_mm` と同じ式
    (`sqrt(CAD面積)`)で、近似であって保証ではない(`docs/contracts/topopt-design-space.md`「条件面が空のとき」。
    曲面や極端な縦横比では過大にも過小にもなり、厳密には「その要素サイズで `empty_faces()` を取り直す」しかない)。
    密度フィルタ半径は上限に効かない。

    下限は設計空間の要素数の上限(`MAX_DESIGN_ELEMENTS`)を幾何の見積もりで割り戻した値。

    保持・除外ソリッドからは上限を厳密に計算できない(細い通路を取りこぼす)ので見ず、ボクセル化後に数えて通知する
    (`run()` の事後検出)。所要時間は見積もらない(走らせる前は実測が無い。実測に基づく見積もりは `run()` の進捗イベント
    (`_ProgressPump`)側)。
    """
    from . import optimize

    setup = doc.optimize
    faces: List[dict] = []
    for role in optimize.FACE_ROLES:
        for condition in getattr(setup, role):
            problems = _face_problem(doc, role, condition)
            if problems:
                raise OptimizeInputError("。".join(problems))
            area = float(condition["area"])
            faces.append(
                {
                    "role": role,
                    "body": condition["body"],
                    "index": int(condition["index"]),
                    "cad_area_mm2": area,
                    "suggested_max_spacing_mm": _face_max_spacing_mm(area),
                }
            )
    if not faces:
        raise OptimizeInputError("拘束面・荷重面が1枚も無いので解像度を決められない")

    from_faces = min(item["suggested_max_spacing_mm"] for item in faces)
    upper = min(from_faces, MAX_ELEMENT_SIZE_MM)

    lo, hi = _condition_bbox(doc)
    volume = _design_volume(doc)
    if not upper > 0.0:
        raise OptimizeInputError("条件面の面積が0で要素サイズの上限を決められない")
    # 上限は実際の格子の要素サイズに揃える(`derive` は最長辺を要求値で割った数を丸めるので、要求値のままだと
    # 実 spacing が僅かに大きくなる。実測: 要求 4.0mm → 実 4.038mm)。
    upper = _snap_to_grid(lo, hi, upper)
    # 下限も実格子へ揃える(画面が `min_element_size_mm` を送り返しても `_resolve_element_size` が断らないため)。
    lower = _snap_to_grid(lo, hi, _min_element_size(lo, hi, volume))

    limits = {
        "max_element_size_mm": upper,
        "min_element_size_mm": lower,
        "default_element_size_mm": upper,
        "steps": _resolution_steps(lo, hi, upper, lower, volume),
        "from_condition_faces_mm": from_faces,
        "max_element_size_cap_mm": MAX_ELEMENT_SIZE_MM,
        "condition_faces": faces,
        "bbox_min": [float(v) for v in lo],
        "bbox_max": [float(v) for v in hi],
        "max_design_elements": MAX_DESIGN_ELEMENTS,
        # 上限が下限を下回ると、設計空間の要素数の上限に引っかかって「間違わない解像度」が作れない。画面はここを見て断る。
        "feasible": lower <= upper,
    }
    if element_size_mm is None:
        return limits
    limits["requested_element_size_mm"] = element_size_mm
    try:
        size = _snap_to_grid(lo, hi, _resolve_element_size(limits, element_size_mm))
    except (OptimizeInputError, TypeError, ValueError) as error:
        limits["selected_error"] = str(error)
    else:
        limits["selected"] = _resolution_step(lo, hi, size, volume, index=-1)
    return limits


def _resolution_steps(lo, hi, upper: float, lower: float, volume: float) -> List[dict]:
    """要素サイズの梯子。添字0が上限側(いちばん粗い=既定)で、進むほど細かい。

    粗い側へ動く目盛りは作らない: 粗い側の端は「正しさ」の境界(割ると条件面から節点が静かに消える)、細かい側の端は
    「都合」の境界(遅い・落ちる)で性質が違うので片側だけ開ける(タスク `20260904-1705`、本人了承)。

    段は上限から下限までの等比(要素数は要素サイズの3乗に反比例し、等差だと最後の1段で要素数が桁で跳ねる)。
    時間は付けない。見せる要素数は設計空間の見積もり(`_design_element_estimate`)で実数ではない。

    上限が下限以下のときは空を返す(`feasible` が偽になる)。
    """
    if not (upper > 0.0 and lower > 0.0) or lower > upper:
        return []
    count = max(int(RESOLUTION_STEPS), 1)
    ratio = (lower / upper) ** (1.0 / (count - 1)) if count > 1 else 1.0
    steps: List[dict] = []
    seen: set = set()
    for index in range(count):
        # 最後の段は等比の丸めを使わず下限そのものに合わせる(端が届かないと「いちばん細かい」が選べない)。
        # 各段も実格子の要素サイズへ揃える。
        size = lower if index == count - 1 else _snap_to_grid(lo, hi, upper * (ratio ** index))
        step = _resolution_step(lo, hi, size, volume, index=len(steps))
        if step["design_elements_estimate"] in seen:
            continue
        seen.add(step["design_elements_estimate"])
        steps.append(step)
    return steps


def _resolution_step(lo, hi, size: float, volume: float, *, index: int) -> dict:
    """要素サイズ `size`(実格子へ揃えた値)の段1件。梯子の段と自由入力の `selected` が同じ形を使う。"""
    shape = _grid_shape(lo, hi, size)
    return {
        "index": int(index),
        "element_size_mm": float(size),
        "design_elements_estimate": int(_design_element_estimate(volume, size)),
        "min_width_floor_mm": float(min_width_cell_range(size, min(shape))[0] * size),
        "min_width_ceiling_mm": float(min_width_cell_range(size, min(shape))[1] * size),
        "min_width_steps_mm": min_width_steps_mm(size, min(shape)),
    }


def _face_max_spacing_mm(cad_area_mm2: float) -> float:
    """条件面からの要求要素サイズ(この面に格子節点が付く見込みの上限)。

    `anchor.AnchorTable.empty_face_diagnosis` の `suggested_max_spacing_mm` と同じ式(`sqrt(CAD面積)`)。
    近似であって保証ではない(`docs/contracts/topopt-design-space.md`「条件面が空のとき」)。
    一致は `topopt_checks/check_optimize_wiring.py` [8] が突き合わせる。
    """
    area = float(cad_area_mm2)
    return math.sqrt(area) if area > 0.0 else float("nan")


def _condition_boxes(doc: "Document") -> List[tuple]:
    """凸包の点群になるボディ(保持ソリッド ∪ 条件面が属するボディ)を文書の並びで返す。
    要素は `(body_id, roles, kernel.bounding_box の戻り)`。

    `design_space.derive` はこの**ボディ全体**の外接箱の頂点から凸包を起こす。除外ソリッドは入れない。
    **`doc.bbox_cache` を必ず渡す。** `resolution_limits` は目盛りのたびに呼ばれ、`bounding_box` は1形状数十ms
    かかる。渡さないと定常状態が 0.5ms から 145ms になる(2026-09-07実測)。
    """
    from . import kernel

    setup = doc.optimize
    roles_of: Dict[str, List[str]] = {}
    for body_id in setup.preserve_bodies:
        roles_of.setdefault(body_id, []).append("keep")
    for role, name in (("fixtures", "fixed"), ("loads", "load")):
        for condition in getattr(setup, role):
            roles_of.setdefault(condition["body"], []).append(name)
    out: List[tuple] = []
    for body in doc.bodies:
        if body.id not in roles_of:
            continue
        box = kernel.bounding_box(body.shape, cache=doc.bbox_cache)
        out.append((body.id, tuple(dict.fromkeys(roles_of[body.id])), box))
    return out


def _condition_bbox(doc: "Document") -> tuple:
    """条件の外接直方体(`_condition_boxes` の箱の和)。塊の格子の外枠はこれに余白を足したもの。"""
    boxes = [box for _body_id, _roles, box in _condition_boxes(doc)]
    if not boxes:
        raise OptimizeInputError("条件が1つも無いので外接直方体が取れない")
    lo = [min(box["min"][axis] for box in boxes) for axis in range(3)]
    hi = [max(box["max"][axis] for box in boxes) for axis in range(3)]
    return lo, hi


def _box_longest_edge(lo, hi) -> float:
    """`derive` が格子を起こす外枠(点群の外接箱 + 余白)の最長辺[mm]。"""
    from .topopt import design_space as ds

    box_lo, box_hi = ds.frame_box(lo, hi)
    return float(max(box_hi[a] - box_lo[a] for a in range(3)))


def _snap_to_grid(lo, hi, spacing: float) -> float:
    """要求した要素サイズ以下で、`derive` の丸め(`round(最長辺 / spacing)`)がそのまま再現する値。

    `derive` は `n = round(最長辺 / spacing)`、実 spacing = `最長辺 / n` なので、要求値より
    大きくなりうる。1つ多い分割数に倒せば実 spacing は要求値以下になり、しかも
    `round(最長辺 / 実spacing)` がその分割数に戻る(層をまたぐ契約: `design_space.derive`)。
    """
    box_long = _box_longest_edge(lo, hi)
    if not box_long > 0.0 or not spacing > 0.0:
        return float(spacing)
    n = max(1, int(round(box_long / float(spacing))))
    if box_long / n > float(spacing) * (1.0 + 1e-12):
        n += 1
    return box_long / n


def _grid_shape(lo, hi, spacing: float) -> tuple:
    """`derive(spacing=...)` が起こす格子のセル数(x, y, z)。最小幅の上限と梯子が使う。"""
    from .topopt import design_space as ds

    return tuple(int(n) for n in ds.grid_for_frame(*ds.frame_box(lo, hi), spacing=float(spacing)).shape)


def _design_volume(doc: "Document") -> float:
    """設計空間の体積の見積もり[mm³] = 領域(条件の点群の凸包 + 余白 m の球)の連続体積 × 除外に食われない割合。

    `derive` と同じ点群(`_condition_boxes` の箱の頂点)・同じ余白(点群の最長辺 × `DEFAULT_MARGIN_RATIO`)。
    """
    import numpy as np

    from .topopt import design_space as ds

    boxes = [box for _body_id, _roles, box in _condition_boxes(doc)]
    if not boxes:
        raise OptimizeInputError("条件が1つも無いので設計空間の大きさが取れない")
    bits = np.array([[i, j, k] for i in (0, 1) for j in (0, 1) for k in (0, 1)], dtype=float)
    corners = np.unique(
        np.concatenate(
            [np.asarray(box["min"], dtype=float) + bits * (np.asarray(box["max"], dtype=float) - np.asarray(box["min"], dtype=float)) for box in boxes]
        ),
        axis=0,
    )
    longest = float(np.max(corners.max(axis=0) - corners.min(axis=0)))
    volume = ds.offset_hull_volume(corners, ds.DEFAULT_MARGIN_RATIO * longest)
    exclude_ids = list(doc.optimize.exclude_bodies)
    if not exclude_ids:
        return volume
    obstacle, _offsets = _compound_with_offsets(doc, exclude_ids)
    return volume * ds.obstacle_free_fraction(corners, obstacle)


def _design_element_estimate(volume: float, spacing: float) -> int:
    """設計空間の要素数の見積もり = 領域の連続体積 ÷ 要素の体積。

    **実数ではない。** 除外の分は粗い格子で数えた割合(`obstacle_free_fraction`)で、揺れる。
    「条件を含まない連結成分」は引かないので実数より多く出る向きの誤差は残る(誤差の実測は docs/TOPOPT.md)。
    """
    return int(round(float(volume) / float(spacing) ** 3))


def _min_element_size(lo, hi, volume: float) -> float:
    """設計空間の要素数の見積もりが `MAX_DESIGN_ELEMENTS` を超えない、いちばん小さい要素サイズ。

    格子の要素サイズは `最長辺 / n`(n は整数)しか取れないので、閉じた式 `(体積/上限)^(1/3)` を
    **要求より粗い側へ**格子に揃える(`n` を切り捨てる)。細かい側へ丸めると見積もりが上限を超える。
    """
    if not max(float(hi[a]) - float(lo[a]) for a in range(3)) > 0.0:
        raise OptimizeInputError("条件の外接直方体が1点に潰れている")
    if not volume > 0.0:
        raise OptimizeInputError("設計空間の体積が0で要素サイズの下限を決められない")
    box_long = _box_longest_edge(lo, hi)
    wanted = (float(volume) / MAX_DESIGN_ELEMENTS) ** (1.0 / 3.0)
    n = max(1, int(math.floor(box_long / wanted * (1.0 + 1e-12))))
    return box_long / n


# --------------------------------------------------------------------------------------
# デバッグ表示の配信(`?debug=space` / `?debug=anchors` / `?voxel=1`)。頼まれたときだけ `run()` がここを通って
# `debug(event)` を呼び、頼まれなければ配列を作らない。材料はデモ題材ではなく走っている最適化そのものから取る。
# 座標はワールドmm。要素 (i,j,k) の中心は `origin + spacing*(i+0.5, j+0.5, k+0.5)`、角の節点は
# `origin + spacing*(i,j,k)`(セル中心の規約 `duocad/topopt/mesh.py:169`)。画面側で足し引きしない(両側で足すと半要素ずれる)。
# --------------------------------------------------------------------------------------
def _debug_stride_for(mask, limit: int) -> int:
    """`mask[::s,::s,::s]` の立っている数が `limit` 以下になる最小の `s`。"""
    import numpy as np

    if int(np.count_nonzero(mask)) <= limit:
        return 1
    longest = max(int(n) for n in mask.shape)
    for stride in range(2, longest + 1):
        if int(np.count_nonzero(mask[::stride, ::stride, ::stride])) <= limit:
            return stride
    return longest


def _debug_voxel_payload(kind: str, stage: str, grid, mask, *, limit: int) -> dict:
    """立っている要素の通し添字(C順 `i*(ny*nz) + j*nz + k`)を配る。
    座標でなく添字なのは配る量を1/3にするため。画面は `grid.shape` から `(i,j,k)` へ戻す。
    """
    import numpy as np

    mask = np.asarray(mask, dtype=bool)
    total = int(np.count_nonzero(mask))
    stride = _debug_stride_for(mask, int(limit))
    idx = np.argwhere(mask)
    if stride > 1 and idx.size:
        idx = idx[(idx % stride == 0).all(axis=1)]
    _, ny, nz = (int(n) for n in mask.shape)
    flat = idx[:, 0] * (ny * nz) + idx[:, 1] * nz + idx[:, 2]
    return {
        "kind": kind,
        "stage": stage,
        "grid": grid.as_dict(),
        "elements": [int(v) for v in flat.tolist()],
        "total": total,
        "shown": int(flat.shape[0]),
        "stride": int(stride),
    }


def _debug_anchor_payload(stage: str, space, table=None) -> Optional[dict]:
    """拘束面・荷重面に実際に割り当たった格子節点のワールド座標。
    出所は `table`(製品経路では探索が使う確定表)、省くと `DesignSpace.table`。表が無い塊では `None`。
    """
    import numpy as np

    if table is None:
        table = getattr(space, "table", None)
    if table is None:
        return None
    node_grid = table.node_grid
    out: dict = {"kind": "anchors", "stage": stage, "grid": space.grid.as_dict()}
    # 側ごとに1つの点群にまとめる(画面は拘束側・荷重側の2色で、面の別は色に出ない)。
    for key, faces in (("fixed", space.fixed_faces), ("load", space.load_faces)):
        chunks = []
        area = 0.0
        for face_index in faces:
            anchor = table.anchors.get(int(face_index))
            if anchor is None:
                continue
            chunks.append(np.asarray(node_grid.positions_of(anchor.nodes), dtype=float))
            area += float(anchor.total_area)
        points = (
            np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 3), dtype=float)
        )
        out[key] = {
            "faces": [int(f) for f in faces],
            "count": int(points.shape[0]),
            "points": [float(v) for v in points.ravel().tolist()],
            "area_mm2": area,
        }
    return out


class _DebugSink:
    """`run()` からデバッグ配信を出す口。頼まれた種類だけ payload を作る(数十万要素の `argwhere` を払わないため)。
    """

    def __init__(
        self, sink: Optional[Callable[[dict], None]], kinds: Optional[Any]
    ) -> None:
        self._sink = sink
        self._kinds = frozenset(str(k) for k in (kinds or ())) if sink is not None else frozenset()

    def wants(self, kind: str) -> bool:
        return self._sink is not None and kind in self._kinds

    def space(self, stage: str, space) -> None:
        if not self.wants("space"):
            return
        self._sink(
            _debug_voxel_payload("space", stage, space.grid, space.mask, limit=DEBUG_VOXEL_LIMIT)
        )

    def anchors(self, stage: str, space, table=None) -> None:
        if not self.wants("anchors"):
            return
        payload = _debug_anchor_payload(stage, space, table)
        if payload is not None:
            self._sink(payload)

    def rho(self, stage: str, grid, rho) -> None:
        if not self.wants("rho"):
            return
        import numpy as np

        payload = _debug_voxel_payload(
            "rho", stage, grid, np.asarray(rho) > DEBUG_RHO_THRESHOLD, limit=DEBUG_VOXEL_LIMIT
        )
        payload["threshold"] = float(DEBUG_RHO_THRESHOLD)
        self._sink(payload)


# 端から端まで
# --------------------------------------------------------------------------------------
def run(
    doc: "Document",
    options: Optional[OptimizeOptions] = None,
    *,
    progress: Optional[Callable[[dict], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    debug: Optional[Callable[[dict], None]] = None,
    debug_kinds: Optional[Any] = None,
    steps: Optional[Any] = None,
    finish_request: Optional[Callable[[], Optional[int]]] = None,
) -> dict:
    """実製品の最適化を、成功・失敗・中断を含めて実行ごとに記録する。

    `steps`(`optimize_steps.OptimizationSteps`)を渡すと探索の合格形をそこへ足し、探索の後に仕上げの手順を置く
    (実行の後で別のステップを `finish_step` で仕上げられる)。`finish_request()` が `None` 以外(ステップの番号。
    負なら最新)を返すと、探索を止めてそのステップを仕上げる(終了理由 `stopped_by_user`)。中断(`should_cancel`)が先。
    """
    from .topopt import trace

    with trace.recording(doc, options or OptimizeOptions()) as recorder:
        def report(event):
            recorder.snapshot('progress', event)
            if progress is not None:
                progress(dict(event, diagnostic_path=str(recorder.path)))

        result = _run(doc, options, progress=report, should_cancel=should_cancel,
                      debug=debug, debug_kinds=debug_kinds, steps=steps, finish_request=finish_request)
        result['diagnostic_path'] = str(recorder.path)
        recorder.snapshot('result', result)
        recorder.document(doc, 'document_after')
        recorder.finish(result['state'])
        return result


def finish_step(
    doc: "Document",
    steps: Any,
    index: int,
    *,
    progress: Optional[Callable[[dict], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    debug: Optional[Callable[[dict], None]] = None,
    debug_kinds: Optional[Any] = None,
) -> dict:
    """終わった実行の `index` 番目のステップ(探索の合格形)を、探索をやり直さずに仕上げて `doc` に足す。

    条件・格子・完成形検査の設定は、その実行のもの(`steps.finisher` が持つ)。戻り値は `run` と同じ形。
    """
    from .topopt import trace

    if steps is None or steps.finisher is None:
        raise OptimizeInputError("仕上げられるステップが無い(探索が合格した形を出す前に終わった実行か、まだ走っていない)")
    count = len(steps)
    if not 0 <= int(index) < count:
        raise OptimizeInputError(f"ステップ {index} は無い(0〜{count - 1})")
    with trace.recording(doc, OptimizeOptions()) as recorder:
        def report(event):
            recorder.snapshot('progress', event)
            if progress is not None:
                progress(dict(event, diagnostic_path=str(recorder.path)))

        result = steps.finisher(doc, int(index), report, should_cancel, debug, debug_kinds)
        result['diagnostic_path'] = str(recorder.path)
        recorder.snapshot('result', result)
        recorder.document(doc, 'document_after')
        recorder.finish(result['state'])
        return result


def _run(
    doc: "Document",
    options: Optional[OptimizeOptions] = None,
    *,
    progress: Optional[Callable[[dict], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    debug: Optional[Callable[[dict], None]] = None,
    debug_kinds: Optional[Any] = None,
    steps: Optional[Any] = None,
    finish_request: Optional[Callable[[], Optional[int]]] = None,
) -> dict:
    """入力条件 → 固定設計空間の導出(1回) → 全材料の評価とエネルギー順の削減 → 1×1 の空隙を埋めて6条件を取り直す
    (`thin_voids`)→ 最終確定マスクのメッシュ化 → 保持・除外のメッシュ集合演算 → メッシュボディとして文書へ
    (単一の製品経路。`docs/contracts/topopt-binary-search.md`)。

    元のボディは変えず、足すのは1つだけ(その `body_id` を返す)。探索候補の評価中はメッシュ化・STL出力を呼ばない
    (`docs/TOPOPT.md`「二値探索の方式と契約」)。後段へ渡るのは `binary_search.find_best_feasible` の最終確定マスクに
    1×1 の空隙を埋めたものだけ(埋めたときは6条件を取り直し、`safety_factor` はその値。探索時の値は `search_safety_factor`)。

    戻り値(`state` で分岐すること。`"done"` 以外では `body_id` は `None`):

        {"state": "done" | "empty_condition_faces" | "unrepresented_regions"
                  | "boundary_mapping_failed"
                  | "no_feasible_found" | "input_contradiction" | "resolution_insufficient"
                  | "fill_infeasible"                        # 空隙を埋めた形が6条件に不合格
                  | "final_unsafe",                          # 完成形の表面の応力(面積の上位値)で見た安全率が本来の目標を割る
         "search_safety_factor": float|None, "filled_cells": int|None,
         "body_id": BodyId | None, "name": str | None,
         "warning": str | None,
         "empty_condition_faces": [診断, ...],     # 空の条件面ごとの診断(`AnchorTable.condition_face_diagnoses`)
         "unrepresented_regions": [診断, ...],     # 事後検出(下記)
         "boundary_mapping": {...} | None,        # 確定表の検証(`anchor.condition_mapping`)
         "resolution": resolution_limits() の戻り, "element_size_mm": float,
         "min_width": {"requested_mm": float, "spacing_mm": float, "cells": int, "guaranteed_mm": float,
                       "source": "default" | "requested"},
         "min_width_keep_without_room": {"cells": int, "around_exclude": int, "around_outside": int},
         "grid": {"origin","spacing","shape"}, "design_elements": int,
         "safety_factor": float|None, "target_safety_factor": float,
         "search_target_safety_factor": float, "search_target_factor": float,
         "allowable": allowable_stress() の戻り,
         "material_mm3": float|None, "evaluations": int, "termination": str|None,
         "anchor_check": {"limit_mm","fixed_mm","load_mm"} | None,
         "triangle_count": int|None, "vertex_count": int|None,
         "final_safety_factor": float|None,      # 完成形(平滑化・集合演算の後)を細かい格子で解いた、部材域の表面の応力(面積の上位値)での安全率
         "final_check": topopt.final_check.inspect の戻り | None,   # 強度と、つなぎ目の幅の診断(3値。適合は出さない)
         "full_material_limit_load": {"factor","load_total_n","limit_load_total_n","safety_factor",
                                      "search_target_safety_factor"} | None,   # 全材料が安全率だけで落ちたとき、探索を始められる荷重
         "backtrack": {"reason","steps_back","checks","first_final_safety_factor","first_material_mm3"} | None,
                                                 # 探索の最終形が完成形検査で割り、合格形をさかのぼったときの記録
         "seconds": float}

    `target_safety_factor` は利用者の実際の目標、`search_target_safety_factor` は探索(と6条件の `safety`)に渡した目標
    (= 実際の目標 × `search_target_factor`)。`safety_factor` は探索の格子での値で、探索時の目標に対する合否であって
    実際の目標に対する実測ではない。`final_safety_factor` は完成形を間隔 0.67 mm 以下の格子で解き直し、部材域の表面の
    面積 4 mm² の上位値(要素数の上限で粗くしか測れないときは割り増す)で割った値で、係数を掛けずに実際の目標と比べる。
    割ったときは探索の合格形をさかのぼり、完成形検査に通る形を警告付きで足す(`topopt.complete.backtrack`、`backtrack` に記録)。
    どれも通らなければ `state="final_unsafe"` で結果を足さない(#129、2026-10-11 本人決定)。
    測れなかったとき(`final_safety_factor` が None)は警告を付けて足す。

    `min_width_keep_without_room` は、最小幅の規則の対象外にした保持ソリッドのうち、設計空間のどこにも m 立方が入らず
    保証が効かないセルの数(`binary_search.keep_cells_without_room`)。0 でなければ `warning` に出る。

    後半の `state`(`no_feasible_found` / `input_contradiction` / `resolution_insufficient` / `boundary_mapping_failed`)は
    `binary_search.SearchResult.status` をそのまま渡したもの。`boundary_mapping_failed` は探索の前、確定表の検証
    (`_map_boundary_conditions`)で不成立になったときにもここから返す。

    戻り値は素の値だけでできている(`json.dumps` が通り、画面がWebSocketへそのまま載せられる)。
    `DesignSpace` や `AnchorTable` のような実体は外へ出さない。

    デバッグ表示は `debug(event: dict)` + `debug_kinds`(`DEBUG_KINDS` の部分集合)。頼まれたときだけ、塊(`space`)を
    導出直後に1回(`stage="derive"`)、面アンカーの節点(`anchors`。探索・FEMが使う確定表の節点)を境界条件の確定直後に
    1回(`stage="mapping"`)、最終確定マスク(`rho`。0/1の二値)を探索の後に1回(`stage="final"`)配る。
    戻り値には入れない(結果メッセージが数MBに膨らむため)。

    進捗は `progress(event: dict)`(`_ProgressPump`)。中断は `should_cancel()` が真になった時点で `OptimizeCancelled` を
    投げて降りる(`find_best_feasible` の `is_cancelled` にも同じ callable を渡す)。探索は評価と評価のあいだでしか
    中断を見ず、FEM評価1回の途中では降りない(hanger 2.4mm では1回 約100〜137秒)。

    失敗は例外で上がる。握り潰さないこと —— `OptimizeInputError` / `FaceMappingError`(RuntimeError 派生)/
    `OptimizeCancelled` と、下の層の `design_space.DesignSpaceDisconnectedError` / `DesignSpaceTooSmallError` /
    `DesignSpaceConflictError` / `DesignSpaceGeometryError` は、どれも「利用者が次に何をすればよいか」を文面に持っている。
    """
    from .topopt import design_space as ds

    started = time.perf_counter()
    opts = options or OptimizeOptions()
    search_target = _search_target_safety_factor(opts)
    factor = float(opts.search_target_factor)
    bundle = build_conditions(doc)
    limits = resolution_limits(doc)
    # 梯子の外の値を渡されても、`derive` が実際に作る格子の要素サイズ(要求値以下)に揃える。
    spacing = _snap_to_grid(
        limits["bbox_min"], limits["bbox_max"], _resolve_element_size(limits, opts.element_size_mm)
    )
    min_width = _resolve_min_width(
        spacing, opts.min_width_mm, max_cells=min(_grid_shape(limits["bbox_min"], limits["bbox_max"], spacing))
    )

    from .topopt import trace
    trace.snapshot('resolved-conditions', bundle=bundle, limits=limits, spacing=spacing,
                   min_width=min_width, allowable=allowable_stress(),
                   target_safety_factor=float(opts.target_safety_factor),
                   search_target_safety_factor=search_target, search_target_factor=factor)

    pump = _ProgressPump(progress, should_cancel)
    dbg = _DebugSink(debug, debug_kinds)
    pump.emit(phase="prepare", message=f"要素サイズ {spacing:.4g} mm で塊を導出する")

    # 塊はここで1回だけ導出し、探索の全候補がこの `space` を使う。
    space = ds.derive(bundle.conditions, spacing=spacing)
    pump.check()
    if steps is not None:
        steps.grid = space.grid
    _require_design_elements_within_limit(space.element_count, float(space.grid.spacing))
    pump.emit(
        phase="derive",
        message=f"塊 {space.element_count} 要素(格子 {space.grid.shape})",
        design_elements=space.element_count,
    )
    # 塊は1回だけ配る(`?debug=space`)。最後に届くのは密度場 `rho` だけ。
    dbg.space("derive", space)

    from .topopt import binary_search

    keep_room = binary_search.keep_cells_without_room(space.mask, space.keep, min_width["cells"], space.obstacle)
    trace.snapshot('min-width-keep-without-room', **keep_room)
    keep_room_warning = _keep_without_room_warning(min_width, keep_room)

    common = {
        "resolution": limits,
        "element_size_mm": float(space.grid.spacing),
        "min_width": min_width,
        "min_width_keep_without_room": keep_room,
        "grid": space.grid.as_dict(),
        "design_elements": int(space.element_count),
        "allowable": allowable_stress(),
        "target_safety_factor": float(opts.target_safety_factor),
        "search_target_safety_factor": search_target,
        "search_target_factor": factor,
        # 条件面が空で早期returnしたときは測れない(節点が0個)ので None のまま。
        "anchor_check": None,
        # 全材料が安全率だけで落ちたときだけ値を持つ(`_full_material_limit_load`)。
        "full_material_limit_load": None,
        "backtrack": None,
    }

    # 条件面が本番の解像度で空になっていないか(`design_space.connect_conditions` は空のとき判定を降りるだけで
    # 上へ伝えない)。空なら診断を積み、探索を呼ばず早期return。
    empty_faces = _empty_condition_faces(space)
    if empty_faces:
        from .topopt import anchor as an

        return _not_done(
            common,
            started,
            keep_room_warning,
            state="empty_condition_faces",
            warning=an.format_condition_face_warning(empty_faces),
            empty_condition_faces=empty_faces,
            unrepresented_regions=[],
            boundary_mapping=None,
            evaluations=0,
            termination=None,
        )

    # 事後検出。保持・除外ソリッドがこの解像度で1つでも表現できていなければ最適化を回さずに戻る。
    unrepresented = _region_representation(doc, bundle, space)
    if any(item["elements"] == 0 for item in unrepresented):
        return _not_done(
            common,
            started,
            keep_room_warning,
            state="unrepresented_regions",
            warning=format_region_warning(unrepresented),
            unrepresented_regions=unrepresented,
            boundary_mapping=None,
            evaluations=0,
            termination=None,
        )

    # 境界条件の確定。導出出力に接する節点だけへ条件面を対応付け直した確定表を作って検証する。
    # 以後は全てこの表を使い、不成立なら探索もFEMも始めない。
    from .topopt import anchor as an

    table, mapping = _map_boundary_conditions(space, bundle)
    pump.check()
    if mapping["status"] != "pass":
        return _not_done(
            common,
            started,
            keep_room_warning,
            state="boundary_mapping_failed",
            warning=an.format_mapping_warning(mapping),
            unrepresented_regions=unrepresented,
            boundary_mapping=mapping,
            evaluations=0,
            termination="boundary_mapping_failed",
        )

    # 荷重・拘束が選んだ面に載ったかを確定表の節点で測り直す(`_verify_face_mapping` は面添字の対応、こちらは節点の位置。2枚目の網)。
    anchor_check = _check_anchor_centroids(doc, space, table=table)
    common["anchor_check"] = anchor_check
    dbg.anchors("mapping", space, table=table)

    conditions = _build_search_conditions(doc, space, bundle, opts, table=table, mapping=mapping)
    search_kwargs: Dict[str, Any] = {}
    if opts.max_evaluations is not None:
        search_kwargs["max_evaluations"] = int(opts.max_evaluations)
    search_kwargs["min_width_cells"] = min_width["cells"]
    budget_text = str(opts.max_evaluations) if opts.max_evaluations is not None else "既定"
    target_text = f"目標安全率 {opts.target_safety_factor:.3g}"
    if factor != 1.0:
        target_text += (f"(探索の格子では {search_target:.3g} = {opts.target_safety_factor:.3g} × {factor:.3g} "
                        "を満たす形を探す)")
    pump.emit(
        phase="search",
        message=f"形状の探索を始める(全材料の評価 → エネルギー順の削減。{target_text}・評価予算 {budget_text})",
    )
    pump.check()

    def on_evaluation(event: dict) -> None:
        latest = steps.latest() if steps is not None and event.get("outcome") in ("initial", "accepted") else None
        pump.send(
            phase="search",
            message=_evaluation_message(
                event, design_elements=int(space.element_count), factor=factor,
                elapsed_seconds=time.perf_counter() - started),
            **event,
            **({} if latest is None else {"step": latest}),
        )

    def stop_requested() -> bool:
        return bool(should_cancel is not None and should_cancel()) or (
            finish_request is not None and finish_request() is not None)

    search_result = binary_search.find_best_feasible(
        space.mask, conditions, is_cancelled=stop_requested, on_evaluation=on_evaluation,
        on_accept=None if steps is None else steps.append, **search_kwargs
    )
    trace.snapshot(
        'search-result',
        status=search_result.status,
        termination=search_result.termination,
        evaluations=search_result.evaluations,
        material_mm3=(
            None if search_result.best_feasible is None
            else search_result.best_feasible.material_mm3
        ),
    )

    # 仕上げの要求は探索がどう終わっても読む(予算・枯渇で終わる直前に届いた要求を黙って捨てない)。
    requested = None if finish_request is None else finish_request()
    if search_result.termination == 'cancelled':
        # 中断時は製品文書へ勝手に結果を追加しない(best_feasible が見つかっていても。トレースには残した)。
        if (should_cancel is not None and should_cancel()) or requested is None:
            raise OptimizeCancelled("形状探索を中断した")
        if search_result.status != 'feasible' or not search_result.accepted:
            raise OptimizeCancelled("合格した形がまだ無いので、仕上げずに探索を止めた")

    if search_result.status != 'feasible':
        # `SearchResult.status` が 'feasible' 以外ならそのまま渡す(段階4の契約)。
        limit = _full_material_limit_load(
            search_result, sum(float(item.get("magnitude") or 0.0) for item in doc.optimize.loads))
        return dict(_not_done(
            common,
            started,
            keep_room_warning,
            state=search_result.status,
            warning=_search_failure_warning(search_result, min_width, factor, limit),
            unrepresented_regions=unrepresented,
            boundary_mapping=mapping,
            evaluations=int(search_result.evaluations),
            termination=search_result.termination,
        ), full_material_limit_load=limit)

    best = search_result.best_feasible
    from .topopt import complete as completion

    def place(verts) -> List[float]:
        positions = _mesh_to_world(verts, space.grid)
        _check_mesh_placement(positions, space.grid)
        return positions

    target = float(opts.target_safety_factor)
    context = completion.Context(
        space_mask=space.mask, grid=space.grid, conditions=conditions, min_width=min_width,
        target_safety_factor=target, search_target_safety_factor=float(search_target),
        keep_shapes=[doc.get_body(bid).shape for bid in bundle.keep_body_ids],
        exclude_shapes=[doc.get_body(bid).shape for bid in bundle.exclude_body_ids],
        anchor_soup=space.soup, emit=lambda phase, message: pump.emit(phase=phase, message=message),
        check=pump.check, place=place, search_status=search_result.status,
        search_termination=search_result.termination, passthrough=(OptimizeCancelled,))
    accepted = list(search_result.accepted) or [binary_search.AcceptedShape.of(best)]
    shared = dict(context=context, accepted=accepted, best=best, common=common,
                  keep_room_warning=keep_room_warning, unrepresented=unrepresented, mapping=mapping, target=target,
                  evaluations=int(search_result.evaluations))
    if steps is not None:
        steps.keep(len(accepted))

        def finisher(target_doc, index, report, cancel, debug=None, debug_kinds=None) -> dict:
            again = _ProgressPump(report, cancel)
            ctx = dataclasses.replace(context, emit=lambda phase, message: again.emit(phase=phase, message=message),
                                      check=again.check)
            return _finish_shape(target_doc, index, dict(shared, context=ctx), pump=again,
                                 dbg=_DebugSink(debug, debug_kinds), started=time.perf_counter(),
                                 termination="finished_after_run", chosen_by_user=True)

        steps.finisher = finisher
    last = len(accepted) - 1
    if requested is None or not 0 <= requested < last:
        index = last
    else:
        index = int(requested)
    stopped = search_result.termination == 'cancelled'
    return _finish_shape(doc, index, shared, pump=pump, dbg=dbg, started=started,
                         termination="stopped_by_user" if stopped else search_result.termination,
                         chosen_by_user=requested is not None)


def _finish_shape(doc: "Document", index: int, shared: dict, *, pump: "_ProgressPump", dbg: "_DebugSink",
                  started: float, termination: str, chosen_by_user: bool) -> dict:
    """探索の合格形の列 `shared['accepted']` の `index` 番目を完成化し、完成形検査に割れば列をさかのぼり、通った形を
    メッシュボディとして `doc` に足す(`run` の探索の後と、実行の後に選んだステップを仕上げる `finish_step` が使う)。"""
    from .topopt import complete as completion
    from .topopt import final_check

    context, accepted, best = shared["context"], shared["accepted"], shared["best"]
    common, keep_room_warning = shared["common"], shared["keep_room_warning"]
    unrepresented, mapping, target = shared["unrepresented"], shared["mapping"], shared["target"]
    space_grid = context.grid
    last = len(accepted) - 1
    if index == last:
        done = completion.complete(best.mask, float(best.safety_factor), float(best.material_mm3), context)
    else:
        shape = accepted[index]
        done = completion.complete(shape.mask(), float(shape.safety_factor), float(shape.material_mm3), context)
    backtrack_warning = ""
    backtrack_report = None
    if done.verdict in ("fail", "fill_infeasible"):
        # 完成形が本来の目標を割るか、空隙を埋めた形が6条件に落ちたら、探索の合格形をさかのぼって通る形を足す
        # (2026-10-11・12 本人決定)。通らなければ足さない(#129)
        back = completion.backtrack(accepted[:index + 1], done, context)
        steps_back = None if back.chosen_index is None else index - back.chosen_index
        backtrack_report = dict(reason=back.reason, steps_back=steps_back, checks=back.checks,
                                first_final_safety_factor=done.final_safety_factor,
                                first_material_mm3=done.material_mm3)
        if back.chosen is None:
            unsafe = done.verdict == "fail"
            return dict(_not_done(
                common,
                started,
                keep_room_warning,
                state="final_unsafe" if unsafe else "fill_infeasible",
                warning=_join_warnings(completion.shortfall_message(done, target) if unsafe else done.fill_failure,
                                       completion.exhausted_note(back)),
                unrepresented_regions=unrepresented,
                boundary_mapping=mapping,
                evaluations=shared["evaluations"],
                termination=termination,
            ), final_safety_factor=done.final_safety_factor, final_check=done.checked, backtrack=backtrack_report,
                finished_step=index, steps=last + 1)
        backtrack_warning = completion.backtrack_message(done, back.chosen, steps_back, target)
        done = back.chosen
    # 最終確定マスク(0/1のbool)。`_DebugSink.rho` の閾値0.5はboolにも成り立つ。
    dbg.rho("final", space_grid, done.mask)
    checked = done.checked
    final_sf = done.final_safety_factor
    final_warning = final_check.neck_warning(checked["neck"])
    if final_sf is None:
        final_warning = _join_warnings(final_warning, "完成形の安全率を測れなかった(拘束・荷重の節点が完成形に載らないか、解けなかった)")
    final_vertices, final_faces, surface = done.final_vertices, done.final_faces, done.surface
    positions = final_vertices.ravel().tolist()
    indices = final_faces.ravel().tolist()
    from .topopt import trace
    trace.final_mesh(surface.verts_raw, surface.verts_smooth, surface.faces, space_grid,
                     final_vertices=final_vertices, final_faces=final_faces)

    # 名前の番号は画面と同じく 1 から(`index` は 0 から)。
    name = _unique_result_name(doc, f"{RESULT_NAME}-step{index + 1}" if chosen_by_user else RESULT_NAME)
    pump.emit(phase="insert", message="保持・除外を反映した結果をメッシュボディとして足す")
    from .verbs import add_mesh_body

    # 結果は編集可能ソリッドとして持たない(2026-09-23 本人判断)。画面・STL は `finalize` が合格させた三角形をそのまま使う。
    body_id = add_mesh_body(doc, positions, indices, name=name)

    return dict(
        common,
        state="done",
        body_id=body_id,
        name=name,
        warning=_join_warnings(
            format_region_warning(unrepresented) if unrepresented else "", keep_room_warning, done.fill_warning,
            backtrack_warning, final_warning) or None,
        empty_condition_faces=[],
        unrepresented_regions=unrepresented,
        boundary_mapping=mapping,
        safety_factor=done.safety_factor,
        search_safety_factor=done.search_safety_factor,
        final_safety_factor=final_sf,
        final_check=checked,
        backtrack=backtrack_report,
        filled_cells=int(done.fill["cells"]),
        material_mm3=done.material_mm3,
        evaluations=shared["evaluations"],
        termination=termination,
        finished_step=index,
        steps=last + 1,
        triangle_count=len(indices) // 3,
        vertex_count=len(positions) // 3,
        seconds=time.perf_counter() - started,
    )


def _check_anchor_centroids(doc: "Document", space, *, table=None) -> dict:
    """条件面の節点集合が本当にその面のところにあるかを測る(実行時の2枚目の網)。

    面添字の取り違えは例外を出さず、`_verify_face_mapping` を通り抜けた場合の最後の砦になる。
    `anchor.FaceAnchor.centroid`(節点の担当面積で重み付けした重心)と、その面の面積重心(`query.face_centroid`)の距離を
    `anchor.position_limit`(`anchor.condition_mapping` の作用位置の門と同じ値)と比べる。`table` を省くと `space.table`。

    条件に焼き込んである `center` を使ってはいけない: `Face.center()` の既定(`CenterOf.GEOMETRY`)は曲面で面パッチから離れる
    (hanger の荷重面(トーラス)では節点重心から 12.807mm、面積重心なら 0.055mm)。許容は要素サイズ比例なので、
    使うと要素を細かくしたとき必ず落ちる。
    """
    import numpy as np

    from . import query
    from .topopt import anchor as an

    if table is None:
        table = getattr(space, "table", None)
    if table is None:
        return {}
    setup = doc.optimize
    limit = an.position_limit(table)
    out: dict = {"limit_mm": limit, "table": table.stats.get("mapping", "geometry")}
    # 面ごとに測る。合計や平均では、1枚だけ取り違えても他が合っていれば許容に収まってしまう。
    for key, faces, conditions in (
        ("fixed", space.fixed_faces, setup.fixtures),
        ("load", space.load_faces, setup.loads),
    ):
        distances = []
        for face_index, condition in zip(faces, conditions):
            anchor = table[int(face_index)]
            centroid = np.asarray(anchor.centroid(table.node_grid), dtype=float)
            body = doc.get_body(condition["body"])
            center = np.asarray(
                query.face_centroid(body.shape, int(condition["index"])), dtype=float
            )
            distance = float(np.linalg.norm(centroid - center))
            distances.append(distance)
            if not distance <= limit:
                raise FaceMappingError(
                    f"{key} の面アンカーが選んだ面から離れている"
                    f"(面添字 {int(face_index)} / 節点の重心 {np.round(centroid, 3).tolist()} /"
                    f" 面の重心 {np.round(center, 3).tolist()} / 距離 {distance:.3f}mm >"
                    f" 許容 {limit:.3f}mm)。面添字の対応が壊れている可能性がある"
                )
        out[f"{key}_mm"] = max(distances) if distances else 0.0
        out[f"{key}_mm_each"] = distances
    return out


def _empty_condition_faces(space) -> List[dict]:
    """本番解像度で拘束面・荷重面のアンカーが空になっていないか。`DesignSpace.table` が無ければ空リスト。"""
    table = getattr(space, "table", None)
    if table is None:
        return list(space.stats.get("empty_condition_faces_warning") or [])
    return list(table.condition_face_diagnoses(space.fixed_faces, space.load_faces))


def _require_design_elements_within_limit(count: int, spacing: float) -> None:
    """`derive` 後の設計空間の要素数(実数)が上限を超えていたら、探索の前に止める。

    実行前の見積もり(`resolution_limits`)は実数と食い違いうるので、拒否の最終判断はここ。
    上限に収まる要素サイズは要素数が要素サイズの3乗に反比例するとして逆算して示す(概算。その値でもう一度超えることはありうる)。
    """
    if count <= MAX_DESIGN_ELEMENTS:
        return
    suggested = spacing * (count / MAX_DESIGN_ELEMENTS) ** (1.0 / 3.0)
    raise OptimizeInputError(
        f"設計空間の要素数 {count:,} が上限 {MAX_DESIGN_ELEMENTS:,} を超えている"
        f"(要素サイズ {spacing:.4g} mm)。要素サイズを {math.ceil(suggested * 1000) / 1000:.4g} mm 以上にすること"
    )


def _resolve_element_size(limits: dict, requested: Optional[float]) -> float:
    upper = float(limits["max_element_size_mm"])
    lower = float(limits["min_element_size_mm"])
    if not limits["feasible"]:
        raise OptimizeInputError(
            f"要素サイズの上限 {upper:.4g} mm が下限 {lower:.4g} mm を下回っている"
            f"(設計空間の要素数の上限 {MAX_DESIGN_ELEMENTS} に収まらない)。上限は条件面からの要求と固定値 "
            f"{MAX_ELEMENT_SIZE_MM:g} mm の細かい方。条件面が小さいなら大きい面に取り直し、部品が大きいなら設計空間を小さくすること"
        )
    if requested is None:
        return upper
    value = float(requested)
    if not math.isfinite(value) or value <= 0.0:
        raise OptimizeInputError(f"element_size_mm は有限の正の値であること(いま {requested!r})")
    # 上限は実格子へ切り下げた値(4mm → 3.98mm など)なので、切り下げる前の値までは受ける(`_snap_to_grid` は単調)。
    unsnapped = min(float(limits.get("from_condition_faces_mm", upper)), float(limits.get("max_element_size_cap_mm", upper)))
    if value > max(upper, unsnapped) * (1.0 + 1e-12):
        raise OptimizeInputError(
            f"要素サイズ {value:.4g} mm は上限 {upper:.4g} mm より粗い"
            f"(条件面に格子節点が付かなくなるか、固定値 {MAX_ELEMENT_SIZE_MM:g} mm を超えて完成形の安全率が測れなくなる)"
        )
    if value < lower:
        raise OptimizeInputError(
            f"要素サイズ {value:.4g} mm は下限 {lower:.4g} mm より細かい"
            f"(設計空間の要素数の見積もりが {MAX_DESIGN_ELEMENTS} を超える)"
        )
    return value


def min_width_cell_range(spacing: float, max_cells: Optional[int]) -> tuple:
    """選べる m の範囲 `(下限, 上限)`。`max_cells`(格子の最短の辺のセル数)が `None` なら上限は `None`。

    下限は `m × spacing ≥ MIN_WIDTH_FLOOR_MM` を満たす最小の m で、m=1(最小幅の規則なし)にはしない。
    上限は `floor(max_cells × MIN_WIDTH_CEILING_RATIO)`。上限が下限を下回れば、この解像度で選べる段は無い。
    """
    spacing = float(spacing)
    lower = max(2, int(math.ceil(MIN_WIDTH_FLOOR_MM / spacing - 1e-9)))
    upper = None if max_cells is None else int(math.floor(int(max_cells) * MIN_WIDTH_CEILING_RATIO + 1e-9))
    return lower, upper


def min_width_steps_mm(spacing: float, max_cells: int) -> List[float]:
    """この解像度で選べる最小幅の段(`min_width_cell_range` の m の `m × spacing`)。

    各値は `_resolve_min_width` に渡すとちょうどその m になる形へ揃える(浮動小数の丸めで `m × spacing / spacing` が
    m を超える(0.1×3/0.1 → 4)ときは、値を1ulpずつ下げる)。
    """
    spacing = float(spacing)
    lower, upper = min_width_cell_range(spacing, max_cells)
    return [_cells_value(spacing, cells) for cells in range(lower, upper + 1)]


def _cells_value(spacing: float, cells: int) -> float:
    value = cells * spacing
    while math.ceil(value / spacing) > cells:
        value = math.nextafter(value, 0.0)
    return float(value)


def _resolve_min_width(spacing: float, requested: Optional[float], max_cells: Optional[int] = None) -> dict:
    """最小幅[mm]の入口検査(`_resolve_element_size` と同じ形)。

    `None` → 既定 = 選べるいちばん細い段。有限の正の値でなければ `OptimizeInputError`。`m = ceil(値 / spacing)` が
    下限の m を下回る値も同じ例外。`max_cells` を渡すと上限の段を超える値も断る。
    通れば m を、実際に保証される幅 `m × spacing` と、既定を採用したか(`source`)と一緒に返す。
    `requested_mm` は既定のときも実際に使った値(`None` だと呼び出し側が「未指定=制約なし」と読みうる。
    `web/src/session.ts` の `min_width` 型コメント)。
    """
    spacing = float(spacing)
    lower, upper = min_width_cell_range(spacing, max_cells)
    if upper is not None and upper < lower:
        raise OptimizeInputError(
            f"この解像度では選べる最小幅が無い(下限 {lower} セル = {lower * spacing:.4g} mm({MIN_WIDTH_FLOOR_MM:g} mm 以上)が、"
            f"上限 {upper} セル(格子の最短の辺 {max_cells} セルの {MIN_WIDTH_CEILING_RATIO:g})を上回る)。要素サイズを下げること"
        )
    if requested is None:
        value, source = _cells_value(spacing, lower), "default"
    else:
        value, source = float(requested), "requested"
    if not math.isfinite(value) or value <= 0.0:
        raise OptimizeInputError(f"min_width_mm は有限の正の値であること(いま {requested!r})")
    cells = int(math.ceil(value / spacing))
    if cells < lower:
        raise OptimizeInputError(
            f"最小幅 {value:.4g} mm は下限 {lower * spacing:.4g} mm({lower} セル。{MIN_WIDTH_FLOOR_MM:g} mm 以上で"
            f"いちばん細い段)より細い。選べる段から選ぶこと"
        )
    if upper is not None:
        ceiling = upper * spacing
        # 上限そのものの値は `value / spacing` の丸めで upper を超えうるので、mm で比べて m を丸める
        if value > ceiling:
            raise OptimizeInputError(
                f"最小幅 {value:.4g} mm は上限 {ceiling:.4g} mm({upper} セル = 格子の最短の辺 {max_cells} セルの "
                f"{MIN_WIDTH_CEILING_RATIO:g})より太い。選べる段から選ぶこと"
            )
        cells = min(cells, upper)
    return {"requested_mm": value, "spacing_mm": spacing, "cells": cells, "guaranteed_mm": cells * spacing,
            "source": source}


def _map_boundary_conditions(space, bundle: ConditionBundle) -> tuple:
    """導出出力(`space.mask`)に接する節点だけへ条件面を対応付けた確定表と、その検証。

    導出が接続の確認に使った幾何の表(`space.table`。材料を見ない)とは別物で、表の `stats["mapping"]`
    ("geometry" / "material")と記録の段階名 `boundary-mapping` で見分ける。確定後は再配分しない: 候補の削除で
    確定節点が材料から離れたら、その候補が不合格になる(`acceptance.condition_check`)。
    """
    import numpy as np

    from .topopt import acceptance, trace
    from .topopt import anchor as an

    allowed = acceptance.active_nodes(space.mask)
    table = an.build_anchors(space.soup, space.grid, allowed_nodes=allowed)
    mapping = an.condition_mapping(
        table, space.fixed_faces, space.load_faces, bundle.forces, material=space.mask
    )
    geometry = getattr(space, "table", None)
    if geometry is not None:
        live = allowed.ravel()
        difference: dict = {}
        for row in mapping["faces"]:
            face = int(row["face"])
            before = geometry[face]
            after = table[face]
            lost = int((~live[before.node_ids]).sum()) if before.node_count else 0
            difference[row["condition"]] = {
                "geometry_nodes": int(before.node_count),
                "geometry_uncovered_nodes": lost,
                "mapped_nodes": int(after.node_count),
                "only_in_geometry": int(np.setdiff1d(before.node_ids, after.node_ids).size),
                "only_in_mapped": int(np.setdiff1d(after.node_ids, before.node_ids).size),
                "geometry_assigned_area_mm2": float(before.total_area),
                "mapped_assigned_area_mm2": float(after.total_area),
            }
        mapping["geometry_table_difference"] = difference
    trace.snapshot(
        "boundary-mapping",
        mapping=mapping,
        table_stats=dict(table.stats),
        design_mask_hash=space.mask_hash,
    )
    return table, mapping


def _build_search_conditions(doc: "Document", space, bundle: ConditionBundle,
                             opts: OptimizeOptions, *, table=None, mapping=None) -> Any:
    """`DesignSpace` と `ConditionBundle` から `binary_search.SearchConditions` を組み立てる。

    `table` を省くとここで確定表を作る(検査・CLIがこの関数だけを呼んでも製品と同じ入口検査を通る)。
    `preserves`/`excludes` は `(name, shape)` の対(`acceptance.inspect_document` と同じ形)。
    `bbox_cache=doc.bbox_cache` は `_condition_boxes` と同じ理由。探索の目標は実際の目標 × `opts.search_target_factor`
    で、係数を掛けるのはここ1か所(測定は `search_target_factor=1.0` で固定)。安全率の代表応力は部材域の固定物理体積の
    上位値で、許す超過体積 V = 探索格子の要素 `stress.MEMBER_VOLUME_CELLS` 個分。
    """
    from .topopt import binary_search, stress

    if table is None:
        table, mapping = _map_boundary_conditions(space, bundle)
    preserves = tuple((bid, doc.get_body(bid).shape) for bid in bundle.keep_body_ids)
    excludes = tuple((bid, doc.get_body(bid).shape) for bid in bundle.exclude_body_ids)
    return binary_search.SearchConditions(
        grid=space.grid,
        table=table,
        fixed_faces=space.fixed_faces,
        load_faces=space.load_faces,
        forces=bundle.forces,
        preserves=preserves,
        excludes=excludes,
        target_safety_factor=_search_target_safety_factor(opts),
        bbox_cache=doc.bbox_cache,
        mapping=mapping,
        keep_cells=space.keep,
        member_volume_mm3=stress.MEMBER_VOLUME_CELLS * float(space.grid.element_volume),
    )


def _search_target_safety_factor(opts: OptimizeOptions) -> float:
    """探索(と6条件の `safety`)に渡す目標安全率 = 実際の目標 × `search_target_factor`。係数の不正は入口で断る。"""
    factor = float(opts.search_target_factor)
    if not (math.isfinite(factor) and factor >= 1.0):
        raise OptimizeInputError(
            f"search_target_factor は 1.0 以上の有限の数でなければならない(渡された値: {opts.search_target_factor!r})")
    target, searched = float(opts.target_safety_factor), float(opts.target_safety_factor) * factor
    if math.isfinite(target) and not math.isfinite(searched):
        raise OptimizeInputError(f"目標安全率 {target:.4g} × search_target_factor {factor:.4g} が有限の数にならない")
    return searched


def _join_warnings(*parts: str) -> str:
    return "。".join(part.rstrip("。") for part in parts if part)


def _keep_without_room_warning(min_width: dict, keep_room: dict) -> str:
    """最小幅が保証されない保持セルがあるときの一言。周りが除外ソリッドか設計空間の外かで原因を言い分ける。"""
    cells = int(keep_room["cells"])
    if not cells:
        return ""
    around_exclude, around_outside = int(keep_room["around_exclude"]), int(keep_room["around_outside"])
    head = (f"最小幅 {float(min_width['guaranteed_mm']):.4g} mm({int(min_width['cells'])}セル)が保証されない"
            f"保持ソリッドのセルが {cells} 個ある(最小幅の規則の対象外として扱った。書き出す形には保持ソリッドの実形状が戻る)")
    if around_outside == 0:
        cause = f"その周りの空きセル {around_exclude} 個はすべて除外ソリッドで、除外ソリッドに挟まれて太れない"
    elif around_exclude == 0:
        cause = f"その周りの空きセル {around_outside} 個はすべて設計空間の外(端・凸包の外)で、設計空間の端にあって太れない"
    else:
        cause = (f"その周りの空きセルは除外ソリッド {around_exclude} 個・設計空間の外(端・凸包の外) {around_outside} 個で、"
                 "除外ソリッドと設計空間の端に挟まれて太れない")
    return f"{head}。{cause}。最小幅を下げるか、除外ソリッドとの間隔を広げること"


def _search_failure_warning(search_result: Any, min_width: Optional[dict] = None, factor: float = 1.0,
                            limit: Optional[dict] = None) -> str:
    """`SearchResult.status` が `'feasible'` 以外のときの一言。最小幅で設計空間を絞ったなら原因の候補を末尾に添える。
    `factor` は探索の目標に掛けた係数で、安全率の不足を言うときに実際の目標との関係を添える。"""
    message = _search_failure_message(search_result, factor, limit)
    note = _min_width_failure_note(search_result, min_width)
    return f"{message}。{note}" if note else message


def _min_width_failure_note(search_result: Any, min_width: Optional[dict]) -> str:
    """最小幅で設計空間が絞られたときだけ返す(入口検査も全材料の評価も絞った後の結果であることを添える)。"""
    if not min_width:
        return ""
    if search_result.status == 'resolution_insufficient':
        return ""
    info = (search_result.diagnostics or {}).get('min_width') or {}
    trimmed = int(info.get('trimmed_cells', 0))
    if not trimmed:
        return ""
    head = f"最小幅 {float(min_width['guaranteed_mm']):.4g} mm({int(min_width['cells'])}セル)の制約が有効"
    return (f"{head}: この検査は、設計空間から立方が入らない {trimmed}/{info['design_cells']} セルを除いた後の"
            "空間に対して行った。最小幅を下げると通ることがある")


def _search_failure_message(search_result: Any, factor: float = 1.0, limit: Optional[dict] = None) -> str:
    """`SearchResult.status` が `'feasible'` 以外のときの一言(段階4の契約によりその値自体はそのままUI/ログへ渡す)。
    評価予算を増やす案内は付けない: `feasible` 以外はどれも全材料かその手前で決まり、予算では直らない。
    """
    diagnostics = search_result.diagnostics or {}
    if search_result.status == 'input_contradiction':
        voids = diagnostics.get('closed_voids')
        if voids:
            cells = ", ".join(f"{row['representative_cell']}({row['cells']}セル)" for row in voids['largest'])
            return (
                f"設計空間に閉じた空洞が {voids['count']} 個ある(格子の添字とセル数: {cells})。"
                "形状最適化は中空構造を許容しない。除外ソリッドが設計空間(塊)に完全に包まれていないか確認し、"
                "除外ソリッドを塊の外へつなげるか除外を外すこと"
            )
        return (
            "保持ソリッドが設計空間全体を使っても収まらない(条件が矛盾している)。"
            "保持ソリッド・除外ソリッド・条件面を見直すこと"
        )
    if search_result.status == 'resolution_insufficient':
        empty = diagnostics.get('empty_faces', [])
        return f"この解像度では拘束面・荷重面 {empty} のアンカーが空になる。解像度を上げること"
    if search_result.status == 'boundary_mapping_failed':
        # 落ちた理由は対応付けの検証の報告から作る(節点の被覆だけを語ると、対応不能標本・作用位置のずれで落ちたときに面の名前が出ない)。
        from .topopt import anchor as an

        for report in (diagnostics.get('entry_mapping'), diagnostics.get('mapping')):
            message = an.format_mapping_warning(report) if isinstance(report, dict) else ''
            if message:
                return message + "。材料を減らす探索では直らない(候補は設計空間の部分集合)"
        rows = diagnostics.get('uncovered_condition_nodes', [])
        names = ", ".join(f"{row['condition']}({row['uncovered_nodes']}/{row['nodes']}節点)" for row in rows)
        origin = "材料に対応付けた確定表" if diagnostics.get('mapped') else "材料を見ない幾何の表"
        return (
            f"拘束面・荷重面の節点が設計空間全体の材料に接していない: {names}(表: {origin})。"
            "材料を減らす探索では直らない(候補は設計空間の部分集合)ので、"
            "除外ソリッド・条件面の位置と対応付けを見直すこと"
        )
    return _full_material_failure_message(search_result, factor, limit)


def _full_material_failure_message(search_result: Any, factor: float = 1.0, limit: Optional[dict] = None) -> str:
    """全材料が合格しなかった(`no_feasible_found`)ときの一言。落ちた検査と数値を `diagnostics['full_material']['attempts']`
    から出す(形として落ちた成分を優先し、無ければ計算失敗の成分)。`factor` が 1 でなければ、検査の目標が探索時の目標であることを言う。"""
    full = (search_result.diagnostics or {}).get('full_material') or {}
    attempts = full.get('attempts') or []
    attempt = next((row for row in attempts if row.get('status') == 'fail'), attempts[0] if attempts else {})
    checks = attempt.get('checks') or {}
    reasons = []
    for name, check in checks.items():
        if name == 'no_component':
            components = (full.get('islands') or {}).get('components')
            reasons.append(f"全条件を単独で満たす連結成分が無い(成分 {components} 個、欠けた条件 {check.get('missing')})")
        elif name == 'min_width':
            reasons.append("島を除いた形が最小幅の規則(立方の被覆と芯の連結)を満たさない")
        elif name == 'watertight':
            parts = []
            if check.get('irregular_vertices'):
                parts.append(f"非多様体の頂点 {check['irregular_vertices']} 個"
                             f"(格子の頂点の添字の例 {check.get('sample_vertex_indices', [])[:3]})")
            if check.get('void_components', 1) != 1:
                parts.append(f"空の成分 {check['void_components']} 個")
            reasons.append("水密でない(" + "、".join(parts or ["詳細なし"]) + ")")
        elif name == 'single_solid':
            reasons.append(f"材料が1つの塊でない(成分 {check.get('components')} 個)")
        elif name in ('preserve', 'exclude'):
            bodies = [str(row.get('body')) for row in check.get('bodies', []) if row.get('status') != 'pass']
            label = "保持ソリッドとの重なりが足りない" if name == 'preserve' else "除外ソリッドに材料が掛かっている"
            reasons.append(f"{label}({', '.join(bodies)})")
        elif name == 'conditions':
            reasons.append("拘束面・荷重面の条件が成り立たない")
        elif name == 'safety':
            if check.get('reason') == 'prerequisites_failed':
                continue
            if check.get('status') == 'unknown':
                reasons.append(f"応力計算が失敗した({check.get('error') or check.get('reason')})")
            elif check.get('safety_factor') is None:
                reasons.append(str(check.get('message') or check.get('reason')))
            else:
                searched = float(check['target_safety_factor'])
                if factor == 1.0:
                    shortfall = f"目標 {searched:.4g} に届かない"
                else:
                    shortfall = (f"探索の格子で必要な安全率 {searched:.4g}"
                                 f"(= 目標 {searched / factor:.4g} × 係数 {factor:.3g})に届かない")
                reasons.append(f"安全率 {float(check['safety_factor']):.4g} が{shortfall}")
    if search_result.termination == 'evaluation_error':
        head = "設計空間の全材料の応力計算が失敗した"
    else:
        head = "設計空間の全材料でも6条件に合格しない"
    detail = f": {'、'.join(reasons)}" if reasons else ""
    message = f"{head}{detail}。形状最適化は全材料から材料を減らすだけなので、ここから合格形状は探さない"
    if limit is not None:
        return (f"{message}。この形で探索を始められる荷重は、今の {limit['factor']:.2g} 倍"
                f"(荷重の大きさの合計 {limit['load_total_n']:.4g} N → {limit['limit_load_total_n']:.3g} N)まで"
                "(応力は荷重に比例する)。荷重・目標安全率を下げるか、高応力の所の形(保持・設計空間)を見直すこと")
    return f"{message}。条件(荷重・目標安全率・保持・除外・条件面・解像度)を見直すこと"


def _full_material_limit_load(search_result: Any, load_total_n: float) -> Optional[dict]:
    """全材料が安全率だけで落ちたとき、全材料が探索の目標を満たす荷重の上限。他の検査も落ちていれば None。

    線形弾性なので応力は荷重に比例し、安全率は荷重に反比例する: 上限 = 今の荷重 × 全材料の安全率 ÷ 探索の目標。
    探索は全材料から始まり途中の形も全部合格でなければならないので、これを超える荷重ではこの探索は合格形を作れない。
    """
    if search_result.termination != 'full_material_infeasible' or not load_total_n > 0.0:
        return None
    attempts = ((search_result.diagnostics or {}).get('full_material') or {}).get('attempts') or []
    attempt = next((row for row in attempts if row.get('status') == 'fail'), None)
    checks = (attempt or {}).get('checks') or {}
    safety = checks.get('safety') or {}
    if safety.get('status') != 'fail' or any(check.get('status') != 'pass' for name, check in checks.items()
                                             if name != 'safety'):
        return None
    sf, target = safety.get('safety_factor'), safety.get('target_safety_factor')
    if sf is None or not target or not math.isfinite(float(sf)):
        return None
    factor = float(sf) / float(target)
    return dict(factor=factor, load_total_n=float(load_total_n), limit_load_total_n=float(load_total_n) * factor,
                safety_factor=float(sf), search_target_safety_factor=float(target))


def _mesh_to_world(verts, grid) -> List[float]:
    """要素添字空間の頂点 → ワールド座標[mm]の平らな並び(モジュール docstring)。"""
    import numpy as np

    array = np.asarray(verts, dtype=float)
    world = np.asarray(grid.origin, dtype=float) + array * float(grid.spacing)
    return world.ravel().tolist()


def _check_mesh_placement(positions: List[float], grid) -> None:
    """結果メッシュが格子の箱の中にあることを確かめる。座標変換を取り違えても例外は出ず、原点付近の縮んだ形が黙って文書に入る。"""
    import numpy as np

    points = np.asarray(positions, dtype=float).reshape(-1, 3)
    lo, hi = grid.world_box()
    slack = float(grid.spacing)  # marching cubes の縁とパッドのぶん
    if np.any(points < lo - slack) or np.any(points > hi + slack):
        raise FaceMappingError(
            "結果メッシュが格子の箱の外にある"
            f"(メッシュ {np.round(points.min(axis=0), 3).tolist()}"
            f" .. {np.round(points.max(axis=0), 3).tolist()} /"
            f" 格子 {np.round(lo, 3).tolist()} .. {np.round(hi, 3).tolist()})。"
            "要素添字空間からワールド座標への変換が壊れている"
        )


def _unique_result_name(doc: "Document", base: str = RESULT_NAME) -> str:
    """`optimized` / `optimized-2` / ... (作るときに一意にする)。"""
    taken = {body.name for body in doc.bodies}
    if base not in taken:
        return base
    index = 2
    while f"{base}-{index}" in taken:
        index += 1
    return f"{base}-{index}"


# --------------------------------------------------------------------------------------
# 事後検出(保持・除外がこの解像度で表現できているか)
# --------------------------------------------------------------------------------------
def _region_representation(doc: "Document", bundle: ConditionBundle, space) -> List[dict]:
    """保持・除外ソリッドを1つずつ塊の格子へ載せて要素数を数える。

    事前計算では取りこぼす(必要な要素サイズは中心軸変換に近い話で、外接箱や体積では細い通路が消えたことに気づけない)ので、
    ボクセル化のあとに数える。まとめた形でなくボディごとに数える: 大きい保持ソリッドが1つあれば合計は必ず正になり、
    小さいほうが1要素も掴めていないことが隠れる(`anchor._require_nodes` と同じ理屈)。
    戻り値は問題のあるボディだけ(無ければ空リスト)。
    """
    import numpy as np

    from . import optimize
    from .topopt import voxelize as vx

    deviation = float(space.stats.get("deviation") or (space.grid.spacing * vx.DEFAULT_DEVIATION_RATIO))
    design = np.asarray(space.mask, dtype=bool)
    out: List[dict] = []
    for role, body_ids in (
        ("preserve_bodies", bundle.keep_body_ids),
        ("exclude_bodies", bundle.exclude_body_ids),
    ):
        for body_id in body_ids:
            body = doc.get_body(body_id)
            soup = vx.triangles_from_shape(body.shape, deviation=deviation)
            # 格子の列(セル中心を通る +z のレイ)が1本も貫かないのは異常でなく「この解像度では表現できていない」そのもの
            # (実測: 要素サイズ 4mm の格子に 1mm 角の保持ソリッドを置くと必ずこうなる)。0要素として扱う。
            mask, occupancy_stats = vx.occupancy(soup, space.grid, allow_empty=True)
            reason = (
                "格子の列(セル中心を通る +z のレイ)が1本もこの形状を貫かない"
                if occupancy_stats.get("empty") else None
            )
            elements = int(np.count_nonzero(mask))
            inside = int(np.count_nonzero(mask & design))
            # 除外は塊から引かれているので `inside` は 0 が正しい。表現できているかは格子の側(`elements`)で見る。
            counted = elements if role == "exclude_bodies" else inside
            if counted >= MIN_REGION_ELEMENTS:
                continue
            out.append(
                {
                    "role": role,
                    "role_label": optimize.ROLE_LABEL[role],
                    "body": body_id,
                    "name": body.name,
                    "elements": counted,
                    "grid_elements": elements,
                    "element_size_mm": float(space.grid.spacing),
                    "threshold": MIN_REGION_ELEMENTS,
                    "reason": reason,
                }
            )
    return out


def format_region_warning(diagnoses: List[dict]) -> str:
    """事後検出の診断 → 日本語1文(`anchor.format_condition_face_warning` と同じ役回り)。"""
    if not diagnoses:
        return ""
    parts = []
    for item in diagnoses:
        if item["elements"] == 0:
            parts.append(
                f"{item['role_label']}「{item['name']}」が要素サイズ"
                f" {item['element_size_mm']:.4g} mm では1要素も掴めていない"
            )
        else:
            parts.append(
                f"{item['role_label']}「{item['name']}」が {item['elements']} 要素しかない"
                f"(目安 {item['threshold']} 要素)"
            )
    return (
        "、".join(parts)
        + "。この解像度ではその形状を表現できていないので、要素サイズを下げる"
        "(解像度を上げる)か、その条件を外すこと"
    )


# --------------------------------------------------------------------------------------
# 進捗と中断
# --------------------------------------------------------------------------------------
def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}秒"
    hours, minutes = divmod(total // 60, 60)
    return f"{hours}時間{minutes}分" if hours else f"{minutes}分"


def _format_margin(percent: float) -> str:
    size = abs(percent)
    return f"{percent:+.0f}" if size >= 10 else f"{percent:+.1f}" if size >= 1 else f"{percent:+.2f}"


_FAILED_CHECK_LABELS = {
    "safety": "安全率を計算できず", "geometry": "幾何", "preserve": "保持", "exclude": "除外",
    "watertight": "水密", "single_solid": "単一成分", "conditions": "条件面", "min_width": "最小幅",
}


def _evaluation_message(event: dict, *, design_elements: int, factor: float, elapsed_seconds: float) -> str:
    """`binary_search` の `on_evaluation` の1件を、パネルの1行にする。
    安全率は探索の格子での値なので、比べる相手も探索時の目標(`event['target_safety_factor']`)。
    """
    target = float(event["target_safety_factor"])
    safety = float(event["safety_factor"])
    label = "目標" if factor == 1.0 else "探索の目標"
    ratio = safety / target
    gap = f"{ratio:.1f} 倍" if ratio >= 2.0 else f"{_format_margin(100.0 * (ratio - 1.0))}%"
    best = (f"安全率 {safety:.4g}({label} {target:.3g} の {gap})・"
            f"材料 {100.0 * event['cells'] / design_elements:.1f}%({event['cells']:,} 要素)・"
            f"経過 {_format_elapsed(elapsed_seconds)}")
    if event["outcome"] == "initial":
        return f"評価 {event['evaluation']}: 全材料は合格。{best}"
    if event["outcome"] == "accepted":
        return f"評価 {event['evaluation']}: 採用。{best}"
    candidate = event.get("candidate_safety_factor")
    if "safety" in event["failed"] and candidate is not None:
        reason = f"安全率 {candidate:.5g}"
    else:
        reason = "・".join(_FAILED_CHECK_LABELS.get(name, name) for name in event["failed"]) or "計算失敗"
    return f"評価 {event['evaluation']}: 不合格({reason})。合格形状は{best}"


class _ProgressPump:
    """進捗を1つの形に均して配り、そのついでに中断を見る(長い処理の中で呼ばれる点が進捗しかない)。

    探索の中の途中経過は `send`(中断を見ない)で配る。探索は自分の `is_cancelled` で候補を作る前ごとに中断を見て、
    合格チェックポイントを持つ `SearchResult` を返す。`on_evaluation` から `emit` を呼ぶと `OptimizeCancelled` が上がり、
    探索の戻り値(`search-result` の記録)が失われる。
    """

    def __init__(
        self,
        sink: Optional[Callable[[dict], None]],
        should_cancel: Optional[Callable[[], bool]],
    ) -> None:
        self._sink = sink
        self._should_cancel = should_cancel

    def check(self) -> None:
        if self._should_cancel is not None and self._should_cancel():
            raise OptimizeCancelled("形状最適化を中断した")

    def emit(self, **event: Any) -> None:
        self.check()
        self.send(**event)

    def send(self, **event: Any) -> None:
        if self._sink is None:
            return
        payload = {
            "phase": event.pop("phase"),
            "message": event.pop("message", ""),
        }
        payload.update(event)
        self._sink(payload)

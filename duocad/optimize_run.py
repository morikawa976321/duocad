"""文書の条件を数値計算へ渡し、平滑化後の保持・除外ブーリアンを適用してCADを追加する。

optimize.py は条件の保存・配信、ここは条件の点検・実行・進捗・結果の追加を担当する。
元のボディは変更しない。

面は (body_id, 1始まりの面添字) で指定する。複数ボディを compound にまとめると
面添字がずれるため、_verify_face_mapping が面積・法線・重心を照合する。

メッシュ頂点は要素添字座標なので、_mesh_to_world で
grid.origin + verts * grid.spacing によりワールド座標[mm]へ変換する。
幾何操作は kernel / query 越しに行い、topopt は関数内で遅延 import する。
設計理由と実測は docs/TOPOPT.md の「設計と画面」「本体への配線」を参照。"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover - 型注釈のためだけ
    from .document import Document

#: 結果ボディの名前の頭。同じ文書で2回走らせると `optimized-2` になる
#: (改名のverbがどの種別にも無いため。`done/20260904-1703` の契約③)。
RESULT_NAME = "optimized"

#: **総要素数の上限**(= 要素サイズの下限を決める)。
#:
#: **実物が通るための政策上の上限であって、メモリの安全域ではない。**
#: `models/hanger_bar_attachment.json` は薄い部品(5.00mm)があり、粗いと事前検出
#: (`_region_representation`)で止まる。**通るかどうかは要素サイズに対して単調でない**
#: (2026-09-09 実測: 4.2mm 止まる / 4.4 通る / 4.6 止まる / 4.8 通る / 5.333 止まる。格子の
#: 原点と刻みが要素サイズごとに引き直され、薄い部品にセル中心が入るかが偶然で決まる)——
#: 「N mm 以下なら通る」とは書けない。
#: **ピークメモリを決めるのは箱の要素数ではなく、組み立てに載る要素数(= 塊。空要素は
#: `simp.Model(active_mask=...)` で外れる)である。** この機械(34GB)で hanger を回した
#: ときのピーク増(2026-09-09実測。**3点は経路が違うので併記する**):
#:
#:   5.368mm  113,400箱 /  34,910塊 → +1,287MB  design_space.run_topopt の単発(40反復)
#:   4.4mm    196,080箱 / 137,683塊 → +3,767MB  この `run`(安全率探索9評価ぶんの最大)
#:   2.4mm  1,173,276箱 / 172,310塊 → +4,970MB  design_space.run_topopt の単発(40反復)
#:
#: **塊1要素あたり約 28kB** で、箱の数はほとんど効かない(2.4mm は箱が6倍あっても
#: 4.4mm の1.3倍しか使っていない)。したがってこの定数から必要メモリは出せない。
MAX_ELEMENTS = 2_000_000

#: 保持/除外ソリッドが「この解像度では表現できていない」と見なす要素数の境目。
#: **0 は必ず止める**(その条件が黙って消える)。0 より多く これ未満は警告して続ける。
#: 暫定値 —— 厳密な下限は中心軸変換に近い話で、事前計算では出せない(タスク本文)。
MIN_REGION_ELEMENTS = 8

#: 解像度の梯子の段数(既定=いちばん粗い、を含む)。**粗い側の端から細かい方向へだけ**
#: 動くスライダの目盛りで、画面はこの並びの添字を持つ(`resolution_limits()["steps"]`)。
#: **時間を入力にしないための形**でもある —— 各段は要素サイズ・要素数だけを持ち時間を
#: 付けない(段階5で所要時間の事前見積もりを廃止した)ので、「時間から解像度を逆算する」
#: 経路が画面に生えない(タスク `20260904-1705` の設計)。
RESOLUTION_STEPS = 6

#: 目標安全率の既定。**利用者が入れる値**で、ここにあるのは画面が最初に出す初期値でしかない。
DEFAULT_TARGET_SAFETY_FACTOR = 2.0

#: 探索に渡す目標安全率 = 利用者の目標 × この係数(m に依らず1つ)。探索の格子(k=1)は、細い部材の
#: 曲げ・ねじりを要素中心の応力から見落として安全率を過大に出す。2.4 は最小幅 m≥2 セルの直線部材で
#: その過大評価が取る上限(m=2 の曲げ 2.36)の切り上げ。m=1 には上限が無いが、`_resolve_min_width` の
#: 既定(2026-09-22決定)により製品の実行は m=1 に到達しない。根拠と実測・限界は docs/TOPOPT_TARGET_FACTOR.md。
SEARCH_TARGET_FACTOR = 2.4

#: **デバッグ表示(`?debug=space` / `?voxel=1`)で1回に配るボクセルの上限。**
#:
#: 表示は `InstancedMesh`(立方体1個 = 12三角形)なので、実物の部品で 5〜12万要素の塊を
#: そのまま出すと 60〜144万三角形になる。上限を超えたら**軸方向に等間隔で間引く**
#: (`_debug_voxel_payload` の `stride`) —— 間引いたことは payload の `stride` /
#: `total` / `shown` に出るので、画面は「全部見えている」と誤解しない。
#: **間引きは表示だけの話で、計算には1ビットも触れない。**
DEBUG_VOXEL_LIMIT = 150_000

#: デバッグ配信の種類。`run()` の `debug_kinds` に渡せる値。
DEBUG_KINDS = ("space", "anchors", "rho")

#: `?voxel=1` のボクセル表示で「材料がある」と見なす密度の境目。
#: `np.flatnonzero(flat > 0.5)`。`shoot-topopt.mjs` と揃える —— 不一致だと同じ密度場が
#: 別の形に見える。
DEBUG_RHO_THRESHOLD = 0.5


class OptimizeInputError(ValueError):
    """条件がそろっていない・この版では扱えない(`ValueError` = MCPの `invalid_argument`)。

    **利用者に見せる文がそのまま入る** —— 何が足りないかと、次に何をすればよいかを含める。
    """


class FaceMappingError(RuntimeError):
    """条件面と `TriangleSoup` の面添字の対応が取れなかった(**静かな事故の門**)。

    `ValueError` にしていないのは、**握り潰されてはならない**からである。これが出るのは
    「まとめた形の面の並びが、ボディごとの並びの連結ではなかった」ときで、そのまま走らせると
    荷重が別の面に入ったまま最適化が成功してしまう。
    """


class OptimizeCancelled(RuntimeError):
    """`should_cancel()` が真を返した、または探索が `termination='cancelled'` で戻ったので
    途中で降りた(計画書§4.6「中断時は...製品文書へ勝手に結果を追加しない」)。

    `RuntimeError` にしてあるのは、`acceptance.inspect` 内の例外処理に**拾われないため**
    である(`ValueError` にすると入力の不備と分類されて探索が続く)。
    """


@dataclass
class OptimizeOptions:
    """実行の摘み。**既定値は「条件を満たす、最も粗くて安い設定」**(本人決定)。

    `element_size_mm` を省くと `resolution_limits()` の上限側(いちばん粗い)を採る。
    時間は制約にしない。**密度フィルタ半径(`rmin_mm`)・SIMP反復数(`max_iter`)・材料量の
    直接指定(`material_min_mm3`/`material_max_mm3`)は無い**——二値探索は
    連続密度もSIMP反復も持たないので、渡しても意味の翻訳先が無い。届いたら黙って無視せず
    `OptimizeInputError` にする(`session.run_optimization` / `resolution_limits` の入口)。
    """

    #: 目標安全率。許容応力は固定(`allowable_stress()`)なので、利用者が動かすのはこちら。
    target_safety_factor: float = DEFAULT_TARGET_SAFETY_FACTOR
    #: 探索に渡す目標 = `target_safety_factor` × これ(`SEARCH_TARGET_FACTOR`)。利用者の入力ではなく、
    #: UI・WebSocket の入力には出さない。1.0 はテストと測定が「係数なし」に固定する口。1.0 未満・非有限は断る。
    search_target_factor: float = SEARCH_TARGET_FACTOR
    #: 要素サイズ[mm]。省くと上限側(既定)。
    element_size_mm: Optional[float] = None
    #: 最小幅[mm]。省くと既定 `max(2 × 実spacing, 1.0mm)` を使う(2026-09-22本人決定)。
    #: 下限は要素サイズの2倍(`_resolve_min_width`)。
    min_width_mm: Optional[float] = None
    #: 形状探索の評価予算(`binary_search.find_best_feasible` の
    #: `max_evaluations`。段階3・段階4を通した1本の予算)。省くとそちらの既定(1000)。
    max_evaluations: Optional[int] = None


# --------------------------------------------------------------------------------------
# 固定している材料の許容応力
# --------------------------------------------------------------------------------------
def allowable_stress() -> dict:
    """許容応力[MPa]と**その導出**。duocad 側でも材料は選ばせない(暫定)。

    値を持っているのは `duocad.topopt.material` の1箇所だけで、ここはそれを読んで
    導出を並べ直すだけである(2箇所に書くと片方だけ直る)。
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
    """4条件が**そろって走らせられる状態か**を返す。画面はこれで実行ボタンの可否を決める。

        {"ready": bool, "problems": [str, ...],
         "preserve_bodies": [BodyId,...], "exclude_bodies": [BodyId,...],
         "fixtures": [FaceCondition,...], "loads": [LoadCondition,...],
         "load_total_n": float}   # 荷重の大きさの総和[N](合力ではない)

    `loads` の要素は **LoadCondition**(`optimize.load_condition`)なので、大きさ[N]と向きは
    面ごとに入っている。

    **`state_payload()` の `stale` とは別の点検**である。あちらは「いま画面で指せるか」で、
    こちらは「いま最適化に渡せるか」——非表示のボディは指せないが、形はあるので渡せる。
    """
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

    for role in ("preserve_bodies", "exclude_bodies"):
        for body_id in getattr(setup, role):
            problems.extend(_body_problem(doc, role, body_id))
    for role in ("fixtures", "loads"):
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
        # `verbs/optimize_setup.py` の入口でも断っているが、**過去のログを再生した文書**には
        # 入りうるので、ここでも同じ門を通す(断り文の実体は `meshbody.reject` の1箇所)。
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
        # **条件面は保持ソリッドの面に限る**(2026-09-18 本人指示)。`verbs/optimize_setup.py` の
        # 入口でも断っているが、**過去のログを再生した文書**には入りうるので、ここでも通す
        # (メッシュボディの門と同じ形)。
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
    #: 荷重面と**同じ並び**の力ベクトル[N](向き × 大きさ)。`load_faces[i]` に `forces[i]`。
    forces: tuple
    #: 荷重の総量 = `sum(|forces[i]|)`。**合力(ベクトル和)ではない**(理由は下記)。
    load_n: float


def build_conditions(doc: "Document") -> ConditionBundle:
    """4条件を `duocad.topopt.design_space.Conditions` へ翻訳する。

    - 保持ソリッド → `keep`(1つならその形、複数なら `kernel.make_compound`)
    - 除外ソリッド → `obstacle`(空なら `None`)
    - 拘束面 / 荷重面 → `anchor_shape` の面添字(`fixed_faces` / `load_faces`)
    - 保持ボディ ∪ 条件面のボディ → `bodies`(ボディごとの役割と B-rep の外接箱。
      塊の凸包はここから起きる。除外だけのボディは入れない)
    - 荷重 → `forces`(面ごとの**実単位[N]の力ベクトル**)と `load_n`(その大きさの総和)

    **`load_n` は合力(ベクトル和の大きさ)ではなく、面ごとの大きさの総和である。** 荷重が
    1つ、または全部同じ向きなら両者は一致するが、向きの違う荷重が混ざると一致しない ——
    打ち消し合う組(面Aへ下向き50N・面Bへ上向き50N)では合力が0になり、**応力を実単位へ
    戻す割り算が壊れる**。総和なら打ち消しに影響されない。`stress.load_magnitude_total`
    が解いた側で同じ量を測っており、**2つの定義が食い違うと応力が黙って定数倍ずれる**。

    探索(`binary_search`/`acceptance`)は無次元(荷重の総量1.0)で回り続ける契約
    (`done/20260904-0143`)。実単位[N]は `load_n` だけが持ち、
    `stress.UnitScale.for_model` がそれを読む。
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

    # 面アンカーの対象は「条件面を持っているボディ」だけ。**文書の並び順**で固定する
    # (集合の反復順に頼ると、同じ文書でも面添字が変わりうる)。
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
    load_n = sum(math.sqrt(sum(c * c for c in vector)) for vector in forces)
    if not load_n > 0.0:
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
        load_n=float(load_n),
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
        load_n=float(load_n),
    )


def _compound_with_offsets(doc: "Document", body_ids: List[str]) -> tuple:
    """ボディの並び → (まとめた形, body_id → 面添字のオフセット)。

    **1つだけならまとめない**(その形をそのまま渡せば面添字は完全に同じ)。複数のときだけ
    `kernel.make_compound` で1つにし、面添字は `face_count` の累積でずらす。
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
    """**まとめた形の面添字が、本当に同じ面を指しているか**を測り直して確かめる。

    合わなければ `FaceMappingError`。**この検査を外すと、荷重が別の面に入ったまま
    最適化がもっともらしく収束する**(モジュール docstring)。
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
    """同じ `TopoDS_Face` を2回測ったのかを見る。**重心も見る**(モジュール docstring)。"""
    if not _close(left["area"], right["area"]):
        return False
    return _close_seq(left["normal"], right["normal"]) and _close_seq(
        left["center"], right["center"]
    )


def _close(left: float, right: float) -> bool:
    # 同じ面を同じ関数で2回測っているので、本来は完全一致する。桁落ちだけを吸う幅にしてある
    # (緩めると「別の面だが面積が近い」を通してしまう)。
    return math.isclose(float(left), float(right), rel_tol=1e-7, abs_tol=1e-7)


def _close_seq(left, right) -> bool:
    return len(left) == len(right) and all(_close(a, b) for a, b in zip(left, right))


def _fmt(values) -> str:
    return "[" + ", ".join(f"{float(v):.4g}" for v in values) + "]"


# --------------------------------------------------------------------------------------
# 解像度(要素サイズ)をどう決めるか
# --------------------------------------------------------------------------------------
def resolution_limits(doc: "Document") -> dict:
    """要素サイズの**上限**(これより粗いと間違う)と**下限**(これより細かくはできない)。

        {"max_element_size_mm": float,        # 条件面からの要求
         "min_element_size_mm": float,        # 総要素数の上限から
         "default_element_size_mm": float,    # 既定 = 上限側(いちばん粗くて安い)
         "from_condition_faces_mm": float,    # min(sqrt(条件面のCAD面積))
         "condition_faces": [{...}, ...],
         "bbox_min"/"bbox_max": [x,y,z],      # 条件の外接直方体(余白なし)
         "max_elements": int}

    **上限は条件面からの要求で決まる**(拘束面・荷重面に格子節点が付く大きさ)。
    **密度フィルタ半径(rmin)は上限に効かない**(解像度の上限からrmin/MIN_RMINを
    除去」——二値探索は密度フィルタを持たないので、最小部材幅からの要求という概念自体が無い)。
    **利用者に聞かない**。`anchor.AnchorTable.empty_face_diagnosis` の
    `suggested_max_spacing_mm` と**同じ式**(`sqrt(CAD面積)`)を使う。**近似であって保証ではない**
    (`done/20260904-1556` の契約②)—— 正方形とみなした代表寸法なので、曲面や極端な縦横比の面
    では過大にも過小にもなる。厳密に確かめる手段は「その要素サイズで実際に `empty_faces()` を
    取り直す」しかない。

    **下限は暫定の固定値**(`MAX_ELEMENTS`)。根拠はその定数のところにある。

    保持・除外ソリッドからは**上限を厳密に計算できない**(細い通路を取りこぼす)ので、
    ここでは見ない —— ボクセル化したあとに数えて通知する(`run()` の事後検出)。

    **所要時間は見積もらない**(所要時間は旧SIMP係数を使わない。初回は不明と
    表示し、実測FEM評価時間と予算から幅付きで見積もる」)。走らせる前は1回もFEMを解いていない
    ので実測が無く、旧SIMP係数を使った桁の見当も出さない —— 実測に基づく見積もりは
    `run()` の進捗イベント(`_ProgressPump`)側で、実際に評価が進んでから出す。
    """
    setup = doc.optimize
    faces: List[dict] = []
    for role in ("fixtures", "loads"):
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
    upper = from_faces

    lo, hi = _condition_bbox(doc)
    if not upper > 0.0:
        raise OptimizeInputError("条件面の面積が0で要素サイズの上限を決められない")
    # **上限は実際の格子の要素サイズに揃える。** `derive` は最長辺を要求値で割った数を丸めるので、
    # 要求値そのままだと実 spacing が僅かに大きくなる(実測: 要求 4.0mm → 実 4.038mm)。
    upper = _snap_to_grid(lo, hi, upper)
    # 下限も実格子の要素サイズへ揃える(梯子の最後の段 = `min_element_size_mm` を画面が送り返しても
    # `_resolve_element_size` が「下限より細かい」と断らないため)。分割数は変わらないので要素数も変わらない。
    lower = _snap_to_grid(lo, hi, _min_element_size(lo, hi))

    return {
        "max_element_size_mm": upper,
        "min_element_size_mm": lower,
        "default_element_size_mm": upper,
        # **粗い側の端から細かい方向へだけ**動く目盛り(添字0が既定=上限側)。
        "steps": _resolution_steps(lo, hi, upper, lower),
        "from_condition_faces_mm": from_faces,
        "condition_faces": faces,
        "bbox_min": [float(v) for v in lo],
        "bbox_max": [float(v) for v in hi],
        "max_elements": MAX_ELEMENTS,
        # **上限側が下限を下回る**(部品に対して条件面が小さすぎる)ときは、要素数の上限に
        # 引っかかって「間違わない解像度」が作れない。画面はここを見て断る。
        "feasible": lower <= upper,
    }


def _resolution_steps(lo, hi, upper: float, lower: float) -> List[dict]:
    """要素サイズの梯子。**添字0が上限側(いちばん粗くて安い=既定)**で、進むほど細かい。

    **粗い側へ動く目盛りを作らない。** 粗い側の端は「正しさ」の境界(割ると条件面から
    節点が消える。しかも silent)であり、細かい側の端は「都合」の境界
    (遅い・落ちる、それだけ)である —— 性質が違うので、片側だけ開ける
    (タスク `20260904-1705` の設計。本人了承)。

    段は上限から下限までの**等比**で刻む(要素数は要素サイズの3乗に反比例するので、
    等差にすると最後の1段だけで要素数が桁で跳ねる)。**予想時間は付けない**
    (所要時間は旧SIMP係数を使わない。初回は不明と表示する——走らせる前は
    実測が無いので、要素数だけを見せて時間は語らない)。

    上限が下限以下(部品に対して条件面が小さすぎる)のときは**空**を返す ——
    「間違わない解像度」が作れないので、選ばせる目盛りが無い(`feasible` が偽になる)。
    """
    if not (upper > 0.0 and lower > 0.0) or lower > upper:
        return []
    count = max(int(RESOLUTION_STEPS), 1)
    ratio = (lower / upper) ** (1.0 / (count - 1)) if count > 1 else 1.0
    steps: List[dict] = []
    seen: set = set()
    for index in range(count):
        # 最後の段は等比の丸めを使わず下限そのものに合わせる(端が届かないと、
        # 「いちばん細かい」が選べない目盛りになる)。
        # 各段も実格子の要素サイズへ揃える(画面に出す値と `derive` が作る格子を一致させる)。
        size = lower if index == count - 1 else _snap_to_grid(lo, hi, upper * (ratio ** index))
        shape = _grid_shape(lo, hi, size)
        elements = shape[0] * shape[1] * shape[2]
        if elements in seen:
            continue
        seen.add(elements)
        steps.append(
            {
                "index": len(steps),
                "element_size_mm": float(size),
                "element_count": int(elements),
                "min_width_floor_mm": float(2.0 * size),
                "min_width_ceiling_mm": float(min(shape) * size),
                "min_width_steps_mm": min_width_steps_mm(size, min(shape)),
            }
        )
    return steps


def _face_max_spacing_mm(cad_area_mm2: float) -> float:
    """**条件面からの要求要素サイズ**(この面に格子節点が付く見込みの上限)。

    `anchor.AnchorTable.empty_face_diagnosis` の `suggested_max_spacing_mm` と**同じ式**
    (正方形とみなした代表寸法 `sqrt(CAD面積)`)。probe 側は空になった面の逃げ道として、
    ここは事前の解像度の決め方として同じ数字を使う。**近似であって保証ではない**
    (`done/20260904-1556` の契約②)—— 曲面・極端な縦横比では過大にも過小にもなる。
    値が一致していることは `topopt_checks/check_optimize_wiring.py` [8] が突き合わせる。
    """
    area = float(cad_area_mm2)
    return math.sqrt(area) if area > 0.0 else float("nan")


def _condition_boxes(doc: "Document") -> List[tuple]:
    """凸包の点群になるボディ(保持ソリッド ∪ 条件面が属するボディ)を文書の並びで返す。

    要素は `(body_id, roles, kernel.bounding_box の戻り)`。`design_space.derive` はこの
    **ボディ全体**の外接箱の頂点から凸包を起こす(条件面だけの箱ではない)。
    **除外ソリッドは入れない**(除外は塊から引くものであって、塊を広げる理由にはならない)。

    **`doc.bbox_cache` を必ず渡す。** `resolution_limits` は目盛りを引くたびに呼ばれ、
    `bounding_box` は1形状あたり数十msかかる(`kernel.BoundingBoxCache`)。渡さないと
    定常状態が 0.5ms から 145ms になる(2026-09-07実測)。
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
    """`derive(spacing=...)` が起こす格子のセル数(x, y, z)。`_element_count` と最小幅の上限が共有する。"""
    from .topopt import design_space as ds

    return tuple(int(n) for n in ds.grid_for_frame(*ds.frame_box(lo, hi), spacing=float(spacing)).shape)


def _element_count(lo, hi, spacing: float) -> int:
    """`derive(spacing=...)` が実際に起こす格子の要素数。

    格子そのものを `design_space.grid_for_frame`(`derive` と同じ規則)で1つ起こして数える ——
    ボクセル化も導出も走らせないので安い。要求した要素サイズではなく**丸めた後の実 spacing** の
    格子になるので、梯子に出す要素数が実格子と一致する。
    """
    shape = _grid_shape(lo, hi, spacing)
    return int(shape[0] * shape[1] * shape[2])


def _min_element_size(lo, hi) -> float:
    """総要素数が `MAX_ELEMENTS` を超えない、いちばん小さい要素サイズ。

    `_element_count` は単調(要素サイズを下げると増える)なので、解析的な当たりから
    上へ詰める(切り上げとパッドがあるので閉じた式にはならない)。
    """
    from .topopt import design_space as ds

    if not max(float(hi[a]) - float(lo[a]) for a in range(3)) > 0.0:
        raise OptimizeInputError("条件の外接直方体が1点に潰れている")
    box_lo, box_hi = ds.frame_box(lo, hi)
    volume = 1.0
    for axis in range(3):
        volume *= float(box_hi[axis] - box_lo[axis])
    guess = (volume / MAX_ELEMENTS) ** (1.0 / 3.0) if volume > 0 else _box_longest_edge(lo, hi)
    spacing = max(guess, _box_longest_edge(lo, hi) * 1e-6)
    for _ in range(200):
        if _element_count(lo, hi, spacing) <= MAX_ELEMENTS:
            return spacing
        spacing *= 1.05
    return spacing


# --------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------
# デバッグ表示の配信(`?debug=space` / `?debug=anchors` / `?voxel=1`)
#
# **製品の既定の画面には1ビットも出ない。** 画面がURLパラメータを見て `debug_kinds` を
# 頼んだときだけ、`run()` がここを通って `debug(event)` を呼ぶ。頼まれなければ配列を
# 1本も作らない(費用ゼロ)。
#
# **デモ題材に固定されていない**(このタスクの完了条件2) —— 材料はすべて
# **いま走っている最適化そのもの**(利用者の文書の条件から導いた `DesignSpace` と、
# 実際に解いた密度場)から取る。固定の合成題材カタログを参照する経路はここに無い。
#
# 座標は**ワールドmm**である。要素 (i,j,k) の中心は
# `origin + spacing*(i+0.5, j+0.5, k+0.5)`、その角の節点 (i,j,k) は `origin + spacing*(i,j,k)`
# ——「セル中心の規約」(`duocad/topopt/mesh.py:169`)そのままで、画面側では足し引きしない
# (両側で足すと半要素ずれる)。
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
    """立っている要素の**通し添字**(C順 `i*(ny*nz) + j*nz + k`)を配る。

    座標そのものではなく添字を配るのは、150,000要素で座標だと float が45万個になるため
    (添字なら15万個)。画面は `grid.shape` から `(i,j,k)` へ戻して中心を計算する。
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
    """拘束面・荷重面に**実際に割り当たった格子節点**のワールド座標。

    出所は `table`(製品経路では探索が使う確定表。`_check_anchor_centroids` が距離を測るのと
    同じ表)。省くと導出が持ち帰った `DesignSpace.table`。表が無い塊(検査が直接組んだもの)では
    `None` を返す。
    """
    import numpy as np

    if table is None:
        table = getattr(space, "table", None)
    if table is None:
        return None
    node_grid = table.node_grid
    out: dict = {"kind": "anchors", "stage": stage, "grid": space.grid.as_dict()}
    # **側ごとに1つの点群へまとめる**(面ごとに分けない)。画面が描くのは「拘束側の節点」と
    # 「荷重側の節点」の2色で、面の別は色に出ない —— 分けて配ると受け取る側に
    # 面ごとのループが増えるだけで、見えるものは同じになる。
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
    """`run()` からデバッグ配信を出す口。**頼まれた種類だけ**を作る。

    `kinds` に無い種類は payload を組み立てるところまで行かない —— 配列を作る費用
    (数十万要素の `argwhere`)を、頼まれてもいないのに払わないため。
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
) -> dict:
    """実製品の最適化を、成功・失敗・中断を含めて実行ごとに記録する。"""
    from .topopt import trace

    with trace.recording(doc, options or OptimizeOptions()) as recorder:
        def report(event):
            recorder.snapshot('progress', event)
            if progress is not None:
                progress(dict(event, diagnostic_path=str(recorder.path)))

        result = _run(doc, options, progress=report, should_cancel=should_cancel,
                      debug=debug, debug_kinds=debug_kinds)
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
) -> dict:
    """入力条件 → 固定設計空間の導出(1回) → 全材料の評価とエネルギー順の削減 → 最終確定マスクの
    メッシュ化 → 保持・除外ブーリアン → CADとして文書へ(計画書§3の単一経路)。

    **元のボディは1つも消さない・変えない。** 足すのは1つだけで、その `body_id` を返す。
    **探索候補の評価中はメッシュ化・STL出力を呼ばない**(計画書§3) —— 後段
    (`duocad.topopt.mesh.build_surface_from_mask` 以降)へ渡るのは
    `binary_search.find_best_feasible` が返す最終確定マスク(`best_feasible`)だけである。

    戻り値(`state` で分岐すること。`"done"` 以外では `body_id` は `None`):

        {"state": "done" | "empty_condition_faces" | "unrepresented_regions"
                  | "boundary_mapping_failed"
                  | "no_feasible_found" | "input_contradiction" | "resolution_insufficient",
         "body_id": BodyId | None, "name": str | None,
         "warning": str | None,
         "empty_condition_faces": [診断, ...],     # `20260904-1556` と同じ形
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
         "seconds": float}

    `target_safety_factor` は利用者が指定した実際の目標、`search_target_safety_factor` は探索(と6条件の
    `safety`)に渡した目標(= 実際の目標 × `search_target_factor`)。**`safety_factor` は探索の格子での値**で、
    探索時の目標に対する合否であって、実際の目標に対する安全率の実測ではない。

    `min_width_keep_without_room` は、最小幅の規則の対象外にした保持ソリッドのうち、設計空間のどこにも
    m 立方が入らず保証が効かないセルの数(`binary_search.keep_cells_without_room`)。0 でなければ `warning` に出る。
    既定でも m≥2 の制約が掛かる(`_resolve_min_width`)ので、影響が無ければ 0 になるだけで、
    「最小幅が無効だから 0」という状態はもう無い。

    後半の `state`(`no_feasible_found` / `input_contradiction` / `resolution_insufficient` /
    `boundary_mapping_failed`)は `binary_search.SearchResult.status` を**そのまま**渡したもの
    (段階4の契約「製品配線側はこの値をそのままUI/ログへ渡せばよい」)。`boundary_mapping_failed`
    は探索の前、確定表の検証(`_map_boundary_conditions`)で不成立になったときにもここから返す
    (探索の入口検査と同じ語)。

    **戻り値は素の値だけでできている**(`json.dumps` がそのまま通る)。画面(`20260904-1705`)は
    これをWebSocketへそのまま載せられる —— `DesignSpace` や `AnchorTable` のような
    実体をここから外へ出さない(出すと配れる形が壊れ、キャッシュの持ち主も増える)。

    **デバッグ表示**は `debug(event: dict)` + `debug_kinds`(`DEBUG_KINDS` の部分集合)。
    **頼まれたときだけ**塊(`space`)を導出直後に1回(`stage="derive"`)、面アンカーの節点
    (`anchors`)を境界条件の確定直後に1回(`stage="mapping"`。探索・FEMが実際に使う確定表の
    節点であって、導出が接続の確認に使った幾何の表ではない)、最終確定マスク(`rho`。値は0/1の
    二値)を探索の後に1回(`stage="final"`)配る —— 画面の `?debug=space` / `?debug=anchors` /
    `?voxel=1` の材料である。塊は探索を通して1つなので、最初と最後で違う塊を出すことはない。
    頼まれなければ配列を1本も作らないので、既定の実行の費用は1ビットも増えない。
    **戻り値には入れない**(素の値だけという契約を保つのと、結果メッセージが数MBに
    膨らむのを避けるため)。中身は `_DebugSink` の各メソッド、座標はワールドmm。

    **進捗**は `progress(event: dict)`(下記 `_ProgressPump`)。**中断**は
    `should_cancel()` が真を返した時点で `OptimizeCancelled` を投げて降りる。
    `find_best_feasible` 自体の `is_cancelled` 引数にも同じ callable を渡す。探索は評価と評価の
    あいだ(段階3は候補ブロックを1つ調べる前ごと、段階4は削る候補を作る前ごと)で中断を見るが、
    **FEM評価1回の途中では降りない**(hanger 2.4mm では1回 約100〜137秒)。

    失敗は例外で上がる。**握り潰さないこと** —— `OptimizeInputError`(条件が足りない・
    この版で扱えない)/ `FaceMappingError`(面添字の対応が壊れている。**RuntimeError 派生**)/
    `OptimizeCancelled`(中断)と、下の層からの `design_space.DesignSpaceDisconnectedError`
    (全条件を含む連結成分が無い)/ `DesignSpaceTooSmallError` / `DesignSpaceConflictError`
    (保持と除外が重なっている)/ `DesignSpaceGeometryError`(凸包が起こせない)はどれも
    「利用者が次に何をすればよいか」を文面に持っている。
    """
    from .topopt import design_space as ds

    started = time.perf_counter()
    opts = options or OptimizeOptions()
    search_target = _search_target_safety_factor(opts)
    factor = float(opts.search_target_factor)
    bundle = build_conditions(doc)
    limits = resolution_limits(doc)
    # 利用者が梯子の外の値を渡しても、`derive` が実際に作る格子の要素サイズ(要求値以下)に揃える。
    # 梯子の値は既にその形なので変わらない。
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

    # **塊はここで1回だけ導出し、探索の全候補がこの `space` を使う。** 材料量の探索範囲は
    # 呼び出し側が決める契約(`20260904-1437`)なので、塊の容量と保持ソリッドが占める体積を
    # ここで測る。導出は材料量を受け取らないので「量を決めるために量が要る」堂々巡りは無い。
    space = ds.derive(bundle.conditions, spacing=spacing)
    pump.check()
    pump.emit(
        phase="derive",
        message=f"塊 {space.element_count} 要素(格子 {space.grid.shape})",
        design_elements=space.element_count,
    )
    # **塊はここで1回だけ配る**(`?debug=space`)。探索の全候補が同じ塊を使うので、最後に別の塊を
    # 配り直すことはない —— 最後に届くのは密度場 `rho` だけ。アンカーは確定表ができてから配る。
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
    }

    # **条件面が本番の解像度で空になっていないか。** 導出が作った面アンカーの表から自分で見る
    # (`design_space.connect_conditions` は空のときに判定を降りるだけで、上へは伝えない)。
    # 運び方は `20260904-1556` と同じ —— 診断を戻り値へ積み、1文の警告を付け、
    # 探索(`binary_search.find_best_feasible`)は呼ばずに早期return。
    empty_faces = _empty_condition_faces(space)
    if empty_faces:
        from .topopt import anchor as an

        return dict(
            common,
            state="empty_condition_faces",
            body_id=None,
            name=None,
            warning=_join_warnings(an.format_condition_face_warning(empty_faces), keep_room_warning),
            empty_condition_faces=empty_faces,
            unrepresented_regions=[],
            boundary_mapping=None,
            safety_factor=None,
            material_mm3=None,
            evaluations=0,
            termination=None,
            triangle_count=None,
            vertex_count=None,
            seconds=time.perf_counter() - started,
        )

    # **事後検出。** 保持・除外ソリッドが、この解像度で1つでも表現できていないなら
    # 最適化を回さずに戻る(同じ運び方)。
    unrepresented = _region_representation(doc, bundle, space)
    if any(item["elements"] == 0 for item in unrepresented):
        return dict(
            common,
            state="unrepresented_regions",
            body_id=None,
            name=None,
            warning=_join_warnings(format_region_warning(unrepresented), keep_room_warning),
            empty_condition_faces=[],
            unrepresented_regions=unrepresented,
            boundary_mapping=None,
            safety_factor=None,
            material_mm3=None,
            evaluations=0,
            termination=None,
            triangle_count=None,
            vertex_count=None,
            seconds=time.perf_counter() - started,
        )

    # **境界条件の確定。** 導出出力に接する節点だけへ条件面を対応付け直した確定表を作り、
    # 面ごとの対応・荷重保存・作用位置を検証する。以後の検査・島処理・FEM・デバッグ表示は
    # 全てこの表を使う。不成立なら候補生成もFEMも始めない(探索の入口検査と同じ語で返す)。
    from .topopt import anchor as an

    table, mapping = _map_boundary_conditions(space, bundle)
    pump.check()
    if mapping["status"] != "pass":
        return dict(
            common,
            state="boundary_mapping_failed",
            body_id=None,
            name=None,
            warning=_join_warnings(an.format_mapping_warning(mapping), keep_room_warning),
            empty_condition_faces=[],
            unrepresented_regions=unrepresented,
            boundary_mapping=mapping,
            safety_factor=None,
            material_mm3=None,
            evaluations=0,
            termination="boundary_mapping_failed",
            triangle_count=None,
            vertex_count=None,
            seconds=time.perf_counter() - started,
        )

    # **荷重・拘束が本当に選んだ面に載ったか**を、確定表の節点で測り直す。
    # `_verify_face_mapping` は「まとめた形の面添字」の対応を見るが、こちらは
    # 「その面添字で作られた節点集合が、その面のところにあるか」を見る(2枚目の網)。
    anchor_check = _check_anchor_centroids(doc, space, table=table)
    common["anchor_check"] = anchor_check
    # **アンカーはここで1回だけ配る**(`?debug=anchors`)。探索が使う確定表の節点。
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
    # 探索に入る前の中断はここで拾う(探索の中では評価と評価のあいだでしか見ない)。
    pump.check()

    def on_evaluation(event: dict) -> None:
        pump.send(
            phase="search",
            message=_evaluation_message(
                event, design_elements=int(space.element_count), factor=factor,
                elapsed_seconds=time.perf_counter() - started),
            **event,
        )

    search_result = binary_search.find_best_feasible(
        space.mask, conditions, is_cancelled=should_cancel, on_evaluation=on_evaluation, **search_kwargs
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

    if search_result.termination == 'cancelled':
        # 計画書§4.6「中断時は合格チェックポイントを記録するが、製品文書へ勝手に結果を
        # 追加しない」—— best_feasible が見つかっていても保存しない(トレースには残した)。
        raise OptimizeCancelled("形状探索を中断した")

    if search_result.status != 'feasible':
        # 段階4の契約「SearchResult.status が 'feasible' 以外なら、この値をそのまま
        # UI/ログへ渡せばよい」——ここではそれに日本語の一言を添えるだけ。
        return dict(
            common,
            state=search_result.status,
            body_id=None,
            name=None,
            warning=_join_warnings(
                _search_failure_warning(search_result, min_width, factor), keep_room_warning),
            empty_condition_faces=[],
            unrepresented_regions=unrepresented,
            boundary_mapping=mapping,
            safety_factor=None,
            material_mm3=None,
            evaluations=int(search_result.evaluations),
            termination=search_result.termination,
            triangle_count=None,
            vertex_count=None,
            seconds=time.perf_counter() - started,
        )

    best = search_result.best_feasible
    # 最終確定マスク(値は0/1のbool)。`_DebugSink.rho` の閾値0.5はboolに対しても
    # 形式的に成り立つ(True > 0.5 / False > 0.5)ので、二値専用の配信経路を新設していない。
    dbg.rho("final", space.grid, best.mask)

    from .topopt import mesh as topo_mesh

    pump.emit(phase="mesh", message="確定マスクをメッシュ化する")
    # **候補評価中には一度も呼ばれない入口。** 島除去・保持塗り直しをもう一度掛けない
    # (計画書§3、`mesh.build_surface_from_mask` のdocstring)。
    # **`spacing` は既定(要素添字空間の1.0)のまま渡す。** 実寸への変換は下の
    # `_mesh_to_world`(`grid.origin + verts * grid.spacing`)が一括して行う契約
    # (モジュール docstring)——ここでも実 spacing を渡すと二重に掛かってメッシュが
    # 格子の外へ大きく外れる。
    surface = topo_mesh.build_surface_from_mask(best.mask)
    positions = _mesh_to_world(surface.verts_smooth, space.grid)
    _check_mesh_placement(positions, space.grid)
    indices = surface.faces.ravel().tolist()
    trace.snapshot('selected-result', status=search_result.status, termination=search_result.termination,
                   material_mm3=best.material_mm3, safety_factor=best.safety_factor)
    import numpy as np
    from .topopt.finalize import finalize, solid_mesh

    shape = finalize(
        np.asarray(positions).reshape(-1, 3), surface.faces,
        [doc.get_body(bid).shape for bid in bundle.keep_body_ids],
        [doc.get_body(bid).shape for bid in bundle.exclude_body_ids],
        progress=lambda message: pump.emit(phase="finalize", message=message),
    )
    final_vertices, final_faces = solid_mesh(shape)
    positions = final_vertices.ravel().tolist()
    indices = final_faces.ravel().tolist()
    trace.final_mesh(surface.verts_raw, surface.verts_smooth, surface.faces, space.grid,
                     final_vertices=final_vertices, final_faces=final_faces)

    name = _unique_result_name(doc)
    pump.emit(phase="insert", message="保持・除外を反映した結果をメッシュボディとして足す")
    from .verbs import add_mesh_body

    # 結果は編集可能ソリッドとして持たない(2026-09-23 本人判断)。文書に入るのは `solid_mesh` が
    # 合格させた三角形で、画面・STL はそれをそのまま使う(再テセレーションも B-rep の保存も無い)。
    # ブーリアン後の B-rep 自体は記録の `final-solid` スナップショットにだけ残る(finalize._save_shape)。
    # 112k 面の B-rep を `add_solid_body` で入れると、操作ログに 128 MB のテキストが載り、開くたびに
    # BRepCheck が走り、画面は面ごとの Mesh を 22 万個作っていた。
    body_id = add_mesh_body(doc, positions, indices, name=name)

    return dict(
        common,
        state="done",
        body_id=body_id,
        name=name,
        warning=_join_warnings(
            format_region_warning(unrepresented) if unrepresented else "", keep_room_warning) or None,
        empty_condition_faces=[],
        unrepresented_regions=unrepresented,
        boundary_mapping=mapping,
        safety_factor=float(best.safety_factor),
        material_mm3=float(best.material_mm3),
        evaluations=int(search_result.evaluations),
        termination=search_result.termination,
        triangle_count=len(indices) // 3,
        vertex_count=len(positions) // 3,
        seconds=time.perf_counter() - started,
    )


def _check_anchor_centroids(doc: "Document", space, *, table=None) -> dict:
    """条件面の**節点集合が本当にその面のところにあるか**を測る(実行時の2枚目の網)。

    面添字の取り違えは例外を出さない。`_verify_face_mapping` は B-rep の側で対応を確かめるが、
    それを通り抜けた場合(まとめ方を将来変えたとき等)、ここが最後に気づける場所である。
    測るのは `anchor.FaceAnchor.centroid`(節点の担当面積で重み付けした重心)と、
    **その面の面積重心**(`query.face_centroid`)の距離。許容は `anchor.position_limit`(対応付けの
    探索半径)で、`anchor.condition_mapping` の作用位置の門と同じ値。あちらはまとめた形の三角形群が
    持つ面の重心と比べ、こちらは文書のボディの面から取り直した重心と比べる —— 面添字の対応が
    壊れていれば、こちらだけが気づく。`table` を渡すとその表(探索用の確定表)を
    測り、省くと導出の幾何の表(`space.table`)を測る。

    **条件に焼き込んである `center` を使ってはいけない。** あれは `Face.center()` の既定
    (`CenterOf.GEOMETRY`)で、曲面では面パッチから離れる —— hanger の荷重面(トーラス)では
    節点重心から 12.807mm 離れ、許容は要素サイズ比例なので**要素を細かくするといつか必ず落ちる**
    (面積重心なら 0.055mm)。ここは離散化の誤差を測る門なので、比べる相手は
    「その面の上のどこか」でなければならない。
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
    # **面ごとに測る。合計や平均で見てはならない** —— 1枚だけ取り違えても、他が合っていれば
    # 平均は許容に収まる。面が増えるほど取り違えは起きやすくなるので、ここは緩めない。
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
    """本番解像度で拘束面・荷重面のアンカーが空になっていないか(`20260904-1556` と同じ形)。

    `DesignSpace.table` は導出が作った**本番の格子の**面アンカーの表である。
    無ければ空リストを返す(`derive()` が `table` を必ず設定する現行経路では通常起きない)。
    """
    table = getattr(space, "table", None)
    if table is None:
        return list(space.stats.get("empty_condition_faces_warning") or [])
    return list(table.condition_face_diagnoses(space.fixed_faces, space.load_faces))


def _resolve_element_size(limits: dict, requested: Optional[float]) -> float:
    upper = float(limits["max_element_size_mm"])
    lower = float(limits["min_element_size_mm"])
    if not limits["feasible"]:
        raise OptimizeInputError(
            f"要素サイズの上限 {upper:.4g} mm が下限 {lower:.4g} mm を下回っている"
            f"(総要素数の上限 {MAX_ELEMENTS} に収まらない)。条件面をもっと大きい面に取り直すこと"
        )
    if requested is None:
        return upper
    value = float(requested)
    if not math.isfinite(value) or value <= 0.0:
        raise OptimizeInputError(f"element_size_mm は有限の正の値であること(いま {requested!r})")
    if value > upper:
        raise OptimizeInputError(
            f"要素サイズ {value:.4g} mm は上限 {upper:.4g} mm より粗い"
            "(条件面に格子節点が付かなくなる)"
        )
    if value < lower:
        raise OptimizeInputError(
            f"要素サイズ {value:.4g} mm は下限 {lower:.4g} mm より細かい"
            f"(総要素数が {MAX_ELEMENTS} を超える)"
        )
    return value


def min_width_steps_mm(spacing: float, max_cells: int) -> List[float]:
    """この解像度で保証できる最小幅の段(m = 2..max_cells の `m × spacing`)。

    各値は `_resolve_min_width` に渡すとちょうどその m になる形へ揃える: 浮動小数の丸めで
    `m × spacing / spacing` が m を超える(0.1×3/0.1 → 4)ときは、値を1ulpずつ下げる。
    """
    spacing = float(spacing)
    steps: List[float] = []
    for cells in range(2, int(max_cells) + 1):
        value = cells * spacing
        while math.ceil(value / spacing) > cells:
            value = math.nextafter(value, 0.0)
        steps.append(float(value))
    return steps


def _resolve_min_width(spacing: float, requested: Optional[float], max_cells: Optional[int] = None) -> dict:
    """最小幅[mm]の入口検査(`_resolve_element_size` と同じ形)。

    `None` → 既定 `max(2 × spacing, 1.0mm)`(2026-09-22本人決定)。下限(`2 × spacing`。8等分した評価格子で
    4セル分が見える最小の幅、案B)そのものなので、1.0mm が効くのは `spacing < 0.5mm` のときだけ。
    有限の正の値でなければ `OptimizeInputError`。`2 × spacing` を下回る明示の値も同じ例外。
    `max_cells`(格子の最短の辺のセル数)を渡すと、`max_cells × spacing` を超える値も断る
    (m 立方が格子に1つも入らず、空マスク以外は必ず違反になる不可能な値。巨大な値は導出の後に時間とメモリを使い切る)。
    通れば `m = ceil(値 / spacing)` を、実際に保証される幅 `m × spacing` と、既定を採用したか(`source`)と
    一緒に返す。**`requested_mm` は既定のときも実際に使った値**(`None` のままだと呼び出し側が
    「未指定=制約なし」と読みうる。`web/src/session.ts` の `min_width` 型コメント)。
    """
    spacing = float(spacing)
    if requested is None:
        value, source = max(2.0 * spacing, 1.0), "default"
    else:
        value, source = float(requested), "requested"
    if not math.isfinite(value) or value <= 0.0:
        raise OptimizeInputError(f"min_width_mm は有限の正の値であること(いま {requested!r})")
    floor = 2.0 * spacing
    if value < floor:
        raise OptimizeInputError(
            f"最小幅 {value:.4g} mm は下限 {floor:.4g} mm(要素サイズ {spacing:.4g} mm の2倍)より細い。"
            "最小幅を上げるか、要素サイズを下げること"
        )
    cells = int(math.ceil(value / spacing))
    if max_cells is not None:
        ceiling = max_cells * spacing
        # 上限そのものの値は `value / spacing` の丸めで max_cells を超えうるので、mm で比べて m を丸める
        # (画面が出す上限と同じ比較にするため)。
        if value > ceiling:
            if source == "default":
                # 利用者は最小幅を入れていないので「最小幅を下げよ」ではなく「要素サイズを下げよ」と言う。
                # 既定値の内訳(2×spacing か 1.0mm か)は spacing に依るので、ここでは断定しない。
                raise OptimizeInputError(
                    f"最小幅の指定が無いので既定 {value:.4g} mm(max(要素サイズ {spacing:.4g} mm の2倍, "
                    f"1.0 mm))を使ったが、設計空間の最短の辺({max_cells}セル × 要素サイズ = {ceiling:.4g} mm)"
                    "がそれより細い。要素サイズを下げること"
                )
            raise OptimizeInputError(
                f"最小幅 {value:.4g} mm は上限 {ceiling:.4g} mm(格子の最短の辺 {max_cells} セル × 要素サイズ "
                f"{spacing:.4g} mm)より太い。この幅の立方は格子に入らないので、どの形も満たせない。"
                "最小幅を下げるか、要素サイズを上げること"
            )
        cells = min(cells, int(max_cells))
    return {"requested_mm": value, "spacing_mm": spacing, "cells": cells, "guaranteed_mm": cells * spacing,
            "source": source}


def _map_boundary_conditions(space, bundle: ConditionBundle) -> tuple:
    """導出出力(`space.mask`)に接する節点だけへ条件面を対応付けた**確定表**と、その検証。

    導出が条件ごとの接続の確認に使った幾何の表(`space.table`。材料を見ない)とは別物で、
    表の `stats["mapping"]`("geometry" / "material")と記録の段階名 `boundary-mapping` で
    見分ける。検証(`anchor.condition_mapping`)は面ごとの節点数・対応不能標本・被覆・合力・
    重心・モーメントを持ち、元の幾何の表との差(節点数・被覆されない節点数・節点集合の差)も残す。
    **確定後は再配分しない** —— 候補の削除で確定節点が材料から離れたら、その候補が不合格になる
    (`acceptance.condition_check`)。
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

    `table` は探索用の確定表(`_map_boundary_conditions`)。省くとここで作る —— 検査・CLIが
    この関数だけを呼んでも、製品と同じ確定表・同じ入口検査を通る(製品入口だけの防御にしない)。
    `preserves`/`excludes` は `(name, shape)` の対にする —— `acceptance.inspect_document`
    (過去記録を読む読み取りアダプター)が同じ変換をしている(計画書§5)ので、それと
    同じ形にしておく。`bbox_cache=doc.bbox_cache` を渡すのは `_condition_boxes` と同じ理由
    (境界箱の再計算を候補評価のたびに払わないため、`kernel.BoundingBoxCache`)。
    探索の目標は実際の目標 × `opts.search_target_factor`。**係数を掛けるのはここ1か所**で、検査・測定が
    この関数を通すときも掛かる(測定は `search_target_factor=1.0` で「渡した目標そのまま」に固定する)。
    """
    from .topopt import binary_search

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


def _search_failure_warning(search_result: Any, min_width: Optional[dict] = None, factor: float = 1.0) -> str:
    """`SearchResult.status` が `'feasible'` 以外のときの一言。最小幅で設計空間を絞ったなら、原因の候補を末尾に添える。
    `factor` は探索の目標に掛けた係数で、安全率の不足を言うときに実際の目標との関係を添える。"""
    message = _search_failure_message(search_result, factor)
    note = _min_width_failure_note(search_result, min_width)
    return f"{message}。{note}" if note else message


def _min_width_failure_note(search_result: Any, min_width: Optional[dict]) -> str:
    """最小幅で設計空間が絞られたときだけ返す: 入口検査も全材料の評価も、絞った後の設計空間に
    対する結果であることを添える。合格形状から始まる段階4は `feasible` で返るので、ここへは来ない。"""
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


def _search_failure_message(search_result: Any, factor: float = 1.0) -> str:
    """`SearchResult.status` が `'feasible'` 以外のときの一言(段階4の契約により値自体は

    そのままUI/ログへ渡す。ここでは日本語の説明を1つ添えるだけ)。評価予算を増やす案内は付けない ——
    `feasible` 以外はどれも全材料かその手前で決まり、予算では直らない。
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
        # 対応付けの検証がある(製品経路は必ずある)なら、落ちた理由をその報告から作る ——
        # 節点の被覆だけを語ると、対応不能標本・作用位置のずれで落ちたときに面の名前が出ない。
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
    return _full_material_failure_message(search_result, factor)


def _full_material_failure_message(search_result: Any, factor: float = 1.0) -> str:
    """設計空間の全材料が合格しなかった(`no_feasible_found`)ときの一言。落ちた検査と数値を
    `diagnostics['full_material']['attempts']` から出す(形として落ちた成分を優先し、無ければ計算失敗の成分)。
    検査が持つ目標は探索時の目標(= 実際の目標 × `factor`)なので、`factor` が 1 でなければそのことを言う。"""
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
        head = "設計空間の全材料の応力計算が、解法を変えた再試行の後も失敗した"
    else:
        head = "設計空間の全材料でも6条件に合格しない"
    detail = f": {'、'.join(reasons)}" if reasons else ""
    return (f"{head}{detail}。形状最適化は全材料から材料を減らすだけなので、ここから合格形状は探さない。"
            "条件(荷重・目標安全率・保持・除外・条件面・解像度)を見直すこと")


def _mesh_to_world(verts, grid) -> List[float]:
    """要素添字空間の頂点 → ワールド座標[mm]の平らな並び(モジュール docstring)。"""
    import numpy as np

    array = np.asarray(verts, dtype=float)
    world = np.asarray(grid.origin, dtype=float) + array * float(grid.spacing)
    return world.ravel().tolist()


def _check_mesh_placement(positions: List[float], grid) -> None:
    """結果メッシュが**格子の箱の中にある**ことを確かめる(座標変換の取り違えの門)。

    ずれても例外は出ず、原点付近の縮んだ形が黙って文書に入るだけなので、ここで見る。
    """
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


def _unique_result_name(doc: "Document") -> str:
    """`optimized` / `optimized-2` / ... 。**名前は作るときにしか決められない**
    (どの種別のボディにも改名のverbが無い。`done/20260904-1703` の契約③)。"""
    taken = {body.name for body in doc.bodies}
    if RESULT_NAME not in taken:
        return RESULT_NAME
    index = 2
    while f"{RESULT_NAME}-{index}" in taken:
        index += 1
    return f"{RESULT_NAME}-{index}"


# --------------------------------------------------------------------------------------
# 事後検出(保持・除外がこの解像度で表現できているか)
# --------------------------------------------------------------------------------------
def _region_representation(doc: "Document", bundle: ConditionBundle, space) -> List[dict]:
    """保持・除外ソリッドを**1つずつ**塊の格子へ載せて、要素数を数える。

    **事前計算では取りこぼす** —— 「この形状を表現するのに要素サイズいくつ以下が要るか」は
    中心軸変換に近い話で、バウンディングボックスや体積では細い通路が消えたことに気づけない
    (タスク本文)。だからボクセル化のあとに数える。

    まとめた形(compound)ではなく**ボディごとに**数えるのが要点である —— 大きい保持ソリッドが
    1つあれば合計は必ず正になり、小さいほうが1要素も掴めていないことが隠れる
    (`anchor._require_nodes` が「合計で見てはならない」としているのと同じ理屈)。

    戻り値は問題のあるボディだけ(問題が無ければ空リスト)。
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
            reason = None
            try:
                mask, _stats = vx.occupancy(soup, space.grid)
            except ValueError as error:
                # **格子の列(セル中心を通る +z のレイ)が1本もその形状を貫かない**と
                # `occupancy` は縮退として落ちる。この配線ではそれは異常ではなく
                # 「この解像度ではこの形状を表現できていない」そのもの(実測: 要素サイズ
                # 4mm の格子に 1mm 角の保持ソリッドを置くと必ずこれになる)。0要素として扱う。
                mask = np.zeros(space.grid.shape, dtype=bool)
                reason = str(error)
            elements = int(np.count_nonzero(mask))
            inside = int(np.count_nonzero(mask & design))
            # **除外は塊から引かれている**ので `inside` は 0 が正しい。表現できているかを
            # 見るのは格子の側(`elements`)で、塊の側ではない。
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

    安全率は探索の格子での値なので、比べる相手も探索時の目標(`event['target_safety_factor']`)である。
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
    """進捗を1つの形に均して配り、そのついでに中断を見る。

    **中断を見る場所を進捗と同じにしてある**のは、長い処理の中で呼ばれる点がそこしか
    無いからである(タスク本文「`progress` コールバックから止められる程度」)。

    **探索の中の途中経過は `send`(中断を見ない)で配る。** 探索は自分の `is_cancelled` で候補を作る前ごとに
    中断を見て、合格チェックポイントを持った `SearchResult` を返す。`find_best_feasible` の `on_evaluation` から
    `emit` を呼ぶと、そこで `OptimizeCancelled` が上がって探索の戻り値(`search-result` の記録)が失われる。
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

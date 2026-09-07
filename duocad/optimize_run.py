"""形状最適化を**端から端まで走らせる**配線 —— 4条件 → 塊の導出 → 最適化 → メッシュボディ。

`duocad/optimize.py` が「条件を表現し、保存し、配る」ところまでを持つのに対し、ここは
**その条件を `duocad.topopt` の `Conditions` へ翻訳して回し、結果を新しいメッシュボディとして
文書へ足す**ところを持つ。verbは1本も足していない —— 結果を文書へ入れる口は既存の
`add_mesh_body` だけである(`done/20260904-1703` の契約①)。

**元のボディは1つも消さない・変えない。** 最適化は文書に対する編集ではなく、条件から新しい
形を1つ作る操作である。保持ソリッドも除外ソリッドも元の部品もそのまま
残り、結果は `optimized` という名前のメッシュボディとして**足されるだけ**である。

## 面の対応(この配線でいちばん壊れやすいところ)

duocad は面を **(body_id, 1始まりの面添字)** で持ち、`duocad.topopt` は
**`TriangleSoup` の面添字**(= `kernel.tessellate` が返す添字)で持つ。この2つは
**同じ `kernel._face_map` の数え方**なので、**同じ形状オブジェクトを渡すかぎり一致する**。
食い違うのは、条件面が複数のボディに散っていて `kernel.make_compound` で1つにまとめたときで、
このとき面添字は「そのボディまでの `face_count` の累積」だけずれる。

**ずれても例外は出ない。** 荷重が別の面に入り、最適化はもっともらしく収束して、
出てきた形が間違っているだけになる(`topopt_checks/check_anchor.py` の検査[0]が守っているのと
同じ失敗)。だから `_verify_face_mapping` が**毎回**、まとめた形の側で測り直した指紋
(面積・法線・**重心**)を、ボディ単体で測った指紋と突き合わせる。**重心を外してはいけない** ——
同じ形のボディを2つ並べた文書では面積と法線だけでは区別が付かず、まさに取り違えたい場合を
取りこぼす。

## 単位と座標系(もう1つの静かな事故)

`duocad.topopt.mesh.build_surface` が返す頂点は **`spacing=(1,1,1)` の要素添字空間**で、
要素 `ex` の中心が `ex+0.5` に載る規約である(probe の描画がそうなっていた)。
**ワールド座標[mm]は `grid.origin + verts * grid.spacing`** で、この変換を忘れると
原点付近に縮んだメッシュが黙って入る。`_mesh_to_world` の1箇所に閉じてあり、
結果のbboxが格子の箱に収まっていることを毎回確認する(`_check_mesh_placement`)。

**OCP / build123d を直接importしない**(幾何は `kernel` / `query` 越し)。
`duocad.topopt` は**関数の中で**importする —— `duocad/__init__.py` から連鎖する層に
scipy / skimage を引き込むと、pytest 全体が毎回その費用を払う
(`done/20260904-1600` の契約②)。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover - 型注釈のためだけ
    from .document import Document

#: 結果のメッシュボディの名前の頭。同じ文書で2回走らせると `optimized-2` になる
#: (改名のverbがどの種別にも無いため。`done/20260904-1703` の契約③)。
RESULT_NAME = "optimized"

#: **総要素数の上限**(= 要素サイズの下限を決める暫定の固定値)。
#:
#: 本来はメモリで決まる。`simp.Model` は `_iK` / `_jK` に**要素数 × 576** の int32 を
#: 2本、常駐で持つ(`duocad/topopt/simp.py` の `_iK`)ので、12万要素で約 0.55GB、
#: これに COO の値配列と CSC 化の一時が乗る。**`20260904-1415`(matrix-free / CPU並列)が
#: 入るとこの常駐が丸ごと消えて下限が桁で動く**ので、いま精密に作っても作り直しになる。
#: だから根拠つきの固定値にしてある(本番の基準 `elements_long_axis=40` は題材で
#: 1.4万〜5.6万要素なので、ここは2〜8倍の余裕である)。
MAX_ELEMENTS = 120_000

#: 保持/除外ソリッドが「この解像度では表現できていない」と見なす要素数の境目。
#: **0 は必ず止める**(その条件が黙って消える)。0 より多く これ未満は警告して続ける。
#: 暫定値 —— 厳密な下限は中心軸変換に近い話で、事前計算では出せない(タスク本文)。
MIN_REGION_ELEMENTS = 8

#: 予想時間の係数。**この機械(14スレッド)の実測**で、docs/TOPOPT.md「4題材と `perf.py`」の
#: 8行から「秒 / (格子要素数 × 設計反復)」を取ると 1.05e-4 〜 2.82e-4 に散る(格子要素数の
#: 1.05乗〜2.03乗で伸びるので線形では収まらない)。**桁の見当であって予測ではない** ——
#: 熱状態だけで 1.7倍振れる(engine `benchmark-seconds-swing-with-thermal-state`)。
SECONDS_PER_ELEMENT_ITERATION = 1.8e-4

#: 案5(既定の導出)の粗い段の固定費[秒]。probe の実測 1.2〜1.6秒の中央。
#: **合計から落とさないこと**(候補材料量ごとに1回ずつ掛かる)。
COARSE_DERIVE_SECONDS = 1.4

#: 二分探索が典型的に使う評価回数(上端・下端 + 4回)。上限は
#: `safety_loop.DEFAULT_MAX_EVALUATIONS`。
TYPICAL_EVALUATIONS = 6

#: 解像度の梯子の段数(既定=いちばん粗い、を含む)。**粗い側の端から細かい方向へだけ**
#: 動くスライダの目盛りで、画面はこの並びの添字を持つ(`resolution_limits()["steps"]`)。
#: **時間を入力にしないための形**でもある —— 段ごとに予想時間が付いて回るので、
#: 「時間から解像度を逆算する」経路が画面に生えない(タスク `20260904-1705` の設計)。
RESOLUTION_STEPS = 6

#: 目標安全率の既定。**利用者が入れる値**で、ここにあるのは画面が最初に出す初期値でしかない。
DEFAULT_TARGET_SAFETY_FACTOR = 2.0

#: 材料量の探索範囲の作り方(`safety_loop.solve_for_safety_factor` は範囲を自分で決めない)。
#: 下端は保持ソリッドが占める体積より必ず大きく、上端は塊の容量の端に置かない
#: (端ちょうどは OC の二分探索が退化する。`20260904-0142` の罠)。
MATERIAL_MIN_RATIO = 1.10  # 保持ソリッドが占める体積(**格子の上で数えた量**)に対する下端の倍率
#: 上端 = **案1の塊の容量 × これ**。案1(外接直方体−除外)は材料量を受け取らない導出なので
#: 「量を決めるために量が要る」堂々巡りが起きず、しかも案5の塊を必ず含む(案5は
#: `(膨らませた材料 | 条件の要素) & 案1の塊` である)。0.55 なのは、案5の塊が案1の
#: **0.62〜0.84倍**という実測(docs/TOPOPT.md「案1との比較」)の下側 0.62 に対しても
#: 容量の 0.89 に収まるからである —— 上端が塊の容量の端に触ると OC の二分探索が退化する
#: (`20260904-0142` の罠)。
MATERIAL_MAX_RATIO = 0.55

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
#: **旧実験場 `probe_topopt/server.py` と同じ 0.5**(`np.flatnonzero(flat > 0.5)`。実験場は
#: 既に削除済み)—— 揃えないと同じ密度場が別の形に見える。
#: `docs/topopt-smooth-compare/` の16枚と見え方を突き合わせる土俵でもある。
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
    """`should_cancel()` が真を返したので途中で降りた。

    `RuntimeError` にしてあるのは、`safety_loop._evaluate` の
    `except (VolumeTargetError, DesignSpaceDisconnectedError, ...)` に**拾われないため**
    である(`ValueError` にすると「材料が足りない」と分類されて探索が続く)。
    """


@dataclass
class OptimizeOptions:
    """実行の摘み。**既定値は「条件と最小部材幅を満たす、最も粗くて安い設定」**(本人決定)。

    `element_size_mm` を省くと `resolution_limits()` の上限側(いちばん粗い)を採る。
    時間は制約にしない —— 予想時間は `resolution_limits()` の帰結として出るだけである。
    """

    #: 密度フィルタ半径[mm](最小部材幅の入力)。省くと `simp.DEFAULT_RMIN`[要素]。
    rmin_mm: Optional[float] = None
    #: 目標安全率。許容応力は固定(`allowable_stress()`)なので、利用者が動かすのはこちら。
    target_safety_factor: float = DEFAULT_TARGET_SAFETY_FACTOR
    #: 要素サイズ[mm]。省くと上限側(既定)。
    element_size_mm: Optional[float] = None
    #: SIMP の設計反復数。省くと `safety_loop.DEFAULT_MAX_ITER`。
    max_iter: Optional[int] = None
    #: 二分探索の評価回数の上限。省くと `safety_loop.DEFAULT_MAX_EVALUATIONS`。
    max_evaluations: Optional[int] = None
    #: 材料量の探索範囲[mm^3]。省くと塊の容量と保持ソリッドの体積から決める(上記の比率)。
    material_min_mm3: Optional[float] = None
    material_max_mm3: Optional[float] = None
    #: 結果のメッシュボディの名前。省くと `optimized`(既にあれば `optimized-2`, ...)。
    name: Optional[str] = None


# --------------------------------------------------------------------------------------
# 固定している材料の許容応力
# --------------------------------------------------------------------------------------
def allowable_stress() -> dict:
    """許容応力[MPa]と**その導出**。duocad 側でも材料は選ばせない(暫定)。

    値を持っているのは `duocad.topopt.safety_loop` の1箇所だけで、ここはそれを読んで
    導出を並べ直すだけである(2箇所に書くと片方だけ直る)。
    """
    from .topopt import safety_loop

    return {
        "material": "PETG",
        "yield_mpa": float(safety_loop.PETG_YIELD_MPA),
        "print_derate": float(safety_loop.PRINT_DERATE),
        "allowable_mpa": float(safety_loop.DEFAULT_ALLOWABLE_MPA),
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
    - 荷重 → `forces`(面ごとの**実単位[N]の力ベクトル**)と `load_n`(その大きさの総和)

    **`load_n` は合力(ベクトル和の大きさ)ではなく、面ごとの大きさの総和である。** 荷重が
    1つ、または全部同じ向きなら両者は一致するが、向きの違う荷重が混ざると一致しない ——
    打ち消し合う組(面Aへ下向き50N・面Bへ上向き50N)では合力が0になり、**応力を実単位へ
    戻す割り算が壊れる**。総和なら打ち消しに影響されない。`stress.load_magnitude_total`
    が解いた側で同じ量を測っており、**2つの定義が食い違うと応力が黙って定数倍ずれる**。

    `simp` は無次元(荷重の総量1.0)で回り続ける契約(`done/20260904-0143`)。実単位[N]は
    `load_n` だけが持ち、`stress.UnitScale.for_model` がそれを読む。
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

    conditions = ds.Conditions(
        keep=keep_shape,
        fixed_faces=fixed_faces,
        load_faces=load_faces,
        obstacle=obstacle_shape,
        anchor_shape=anchor_shape,
        load_n=float(load_n),
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
def resolution_limits(doc: "Document", *, rmin_mm: Optional[float] = None) -> dict:
    """要素サイズの**上限**(これより粗いと間違う)と**下限**(これより細かくはできない)。

        {"max_element_size_mm": float,        # min(最小部材幅からの要求, 条件面からの要求)
         "min_element_size_mm": float,        # 総要素数の上限から
         "default_element_size_mm": float,    # 既定 = 上限側(いちばん粗くて安い)
         "from_rmin_mm": float | None,        # rmin_mm / simp.MIN_RMIN
         "from_condition_faces_mm": float,    # min(sqrt(条件面のCAD面積))
         "condition_faces": [{...}, ...],
         "bbox_min"/"bbox_max": [x,y,z],      # 条件の外接直方体(余白なし)
         "max_elements": int,
         "estimate": {...}}                   # 既定の設定での予想時間

    **上限は2つの要求の min** である:

    - **最小部材幅から** —— `rmin_mm` が `simp.MIN_RMIN`(1.5)要素を下回れないので
      `要素サイズ <= rmin_mm / 1.5`(`done/20260904-1557` の契約)。`rmin_mm` を省くと
      この要求は無い(フィルタ半径は要素単位の既定になる)。
    - **条件面から** —— 拘束面・荷重面に格子節点が付く大きさ。**利用者に聞かない**。
      `anchor.AnchorTable.empty_face_diagnosis` の `suggested_max_spacing_mm` と
      **同じ式**(`sqrt(CAD面積)`)を使う。**近似であって保証ではない** ——
      正方形とみなした代表寸法なので、曲面や極端な縦横比の面では過大にも過小にもなる
      (`done/20260904-1556` の契約②)。厳密に確かめる手段は「その要素サイズで実際に
      `empty_faces()` を取り直す」しかない。

    **下限は暫定の固定値**(`MAX_ELEMENTS`)。根拠はその定数のところにある。

    保持・除外ソリッドからは**上限を厳密に計算できない**(細い通路を取りこぼす)ので、
    ここでは見ない —— ボクセル化したあとに数えて通知する(`run()` の事後検出)。
    """
    from .topopt import simp

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
    from_rmin: Optional[float] = None
    if rmin_mm is not None:
        value = float(rmin_mm)
        if not math.isfinite(value) or value <= 0.0:
            raise OptimizeInputError(f"rmin_mm は有限の正の値であること(いま {rmin_mm!r})")
        from_rmin = value / float(simp.MIN_RMIN)

    upper = from_faces if from_rmin is None else min(from_faces, from_rmin)

    lo, hi = _condition_bbox(doc)
    lower = _min_element_size(lo, hi)
    if not upper > 0.0:
        raise OptimizeInputError("条件面の面積が0で要素サイズの上限を決められない")

    return {
        "max_element_size_mm": upper,
        "min_element_size_mm": lower,
        "default_element_size_mm": upper,
        # **粗い側の端から細かい方向へだけ**動く目盛り(添字0が既定=上限側)。
        "steps": _resolution_steps(lo, hi, upper, lower),
        "from_rmin_mm": from_rmin,
        "from_condition_faces_mm": from_faces,
        "condition_faces": faces,
        "bbox_min": [float(v) for v in lo],
        "bbox_max": [float(v) for v in hi],
        "max_elements": MAX_ELEMENTS,
        # **上限側が下限を下回る**(部品に対して条件面が小さすぎる)ときは、要素数の上限に
        # 引っかかって「間違わない解像度」が作れない。画面はここを見て断る。
        "feasible": lower <= upper,
        "estimate": estimate_seconds(_element_count(lo, hi, upper)),
    }


def _resolution_steps(lo, hi, upper: float, lower: float) -> List[dict]:
    """要素サイズの梯子。**添字0が上限側(いちばん粗くて安い=既定)**で、進むほど細かい。

    **粗い側へ動く目盛りを作らない。** 粗い側の端は「正しさ」の境界(割ると条件が消え、
    最小部材幅が表現できない。しかも silent)であり、細かい側の端は「都合」の境界
    (遅い・落ちる、それだけ)である —— 性質が違うので、片側だけ開ける
    (タスク `20260904-1705` の設計。本人了承)。

    段は上限から下限までの**等比**で刻む(要素数は要素サイズの3乗に反比例するので、
    等差にすると最後の1段だけで要素数が桁で跳ねる)。**予想時間を段ごとに付ける**ので、
    画面は往復せずに「細かくすると何秒になるか」を出せる。

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
        size = lower if index == count - 1 else upper * (ratio ** index)
        elements = _element_count(lo, hi, size)
        if elements in seen:
            continue
        seen.add(elements)
        estimate = estimate_seconds(elements)
        steps.append(
            {
                "index": len(steps),
                "element_size_mm": float(size),
                "element_count": int(elements),
                "seconds_typical": float(estimate["seconds_typical"]),
                "seconds_worst": float(estimate["seconds_worst"]),
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


def _condition_bbox(doc: "Document") -> tuple:
    """条件の外接直方体(保持ソリッド ∪ 条件面)。`design_space.condition_points` と同じ集合。

    **除外ソリッドは入れない**(除外は塊から引くものであって、塊を広げる理由にはならない)。
    面だけの bbox を OCCT から直に取るので、三角形分割には依存しない。

    **`doc.bbox_cache` を必ず渡す。** `resolution_limits` は目盛りを引くたびに呼ばれ、
    `bounding_box` は1形状あたり数十msかかる(`kernel.BoundingBoxCache`)。渡さないと
    定常状態が 0.5ms から 145ms になる(2026-09-07実測)。条件面は毎回 `face_of` で
    取り直すが、同じ面は `IsSame` で同一と見なされるので当たる。
    """
    from . import kernel

    setup = doc.optimize
    cache = doc.bbox_cache
    boxes = []
    for body_id in setup.preserve_bodies:
        boxes.append(kernel.bounding_box(doc.get_body(body_id).shape, cache=cache))
    for role in ("fixtures", "loads"):
        for condition in getattr(setup, role):
            body = doc.get_body(condition["body"])
            face = kernel.face_of(body.shape, int(condition["index"]))
            boxes.append(kernel.bounding_box(face, cache=cache))
    if not boxes:
        raise OptimizeInputError("条件が1つも無いので外接直方体が取れない")
    lo = [min(box["min"][axis] for box in boxes) for axis in range(3)]
    hi = [max(box["max"][axis] for box in boxes) for axis in range(3)]
    return lo, hi


def _element_count(lo, hi, spacing: float) -> int:
    """`derive` が起こす格子の要素数(余白 0.25 と `grid_for_bbox` のパッド1を織り込む)。

    **`design_space.DEFAULT_MARGIN_RATIO` と `voxelize.DEFAULT_PAD` をここで再計算している**
    ——1回の導出も走らせずに数を出すためで、実際の格子は `derive` が作る。
    """
    from .topopt import design_space as ds
    from .topopt import voxelize as vx

    extent = [float(hi[a]) - float(lo[a]) for a in range(3)]
    longest = max(extent)
    margin = ds.DEFAULT_MARGIN_RATIO * longest
    total = 1
    for axis in range(3):
        length = extent[axis] + 2.0 * margin
        total *= max(int(math.ceil(length / spacing - 1e-9)), 1) + 2 * vx.DEFAULT_PAD
    return total


def _min_element_size(lo, hi) -> float:
    """総要素数が `MAX_ELEMENTS` を超えない、いちばん小さい要素サイズ。

    `_element_count` は単調(要素サイズを下げると増える)なので、解析的な当たりから
    上へ詰める(切り上げとパッドがあるので閉じた式にはならない)。
    """
    extent = [float(hi[a]) - float(lo[a]) for a in range(3)]
    longest = max(extent)
    if not longest > 0.0:
        raise OptimizeInputError("条件の外接直方体が1点に潰れている")
    from .topopt import design_space as ds

    margin = ds.DEFAULT_MARGIN_RATIO * longest
    volume = 1.0
    for axis in range(3):
        volume *= extent[axis] + 2.0 * margin
    guess = (volume / MAX_ELEMENTS) ** (1.0 / 3.0) if volume > 0 else longest
    spacing = max(guess, longest * 1e-6)
    for _ in range(200):
        if _element_count(lo, hi, spacing) <= MAX_ELEMENTS:
            return spacing
        spacing *= 1.05
    return spacing


def estimate_seconds(
    element_count: int,
    *,
    max_iter: Optional[int] = None,
    evaluations: Optional[int] = None,
) -> dict:
    """その設定でどれくらい掛かるかの**見当**(桁の見当であって予測ではない)。

    1回の評価 = 粗い段の固定費 + 設計反復 × 格子要素数 × 係数。安全率の外側ループは
    候補材料量ごとに1回ずつ回すので、それを掛ける。**粗い段を合計から落とさないこと**
    (`20260904-1416` の契約 —— 案5は粗い段が固定費で乗る)。

    `SECONDS_PER_ELEMENT_ITERATION` の散らばり(1.05e-4〜2.82e-4)と熱状態の 1.7倍を
    考えると、**返るのは 2〜3倍の幅で読む値**である。
    """
    from .topopt import safety_loop

    iters = int(safety_loop.DEFAULT_MAX_ITER if max_iter is None else max_iter)
    evals = int(TYPICAL_EVALUATIONS if evaluations is None else evaluations)
    one = COARSE_DERIVE_SECONDS + iters * int(element_count) * SECONDS_PER_ELEMENT_ITERATION
    return {
        "element_count": int(element_count),
        "max_iter": iters,
        "evaluations": evals,
        "seconds_per_evaluation": one,
        "seconds_typical": one * evals,
        "seconds_worst": one * int(safety_loop.DEFAULT_MAX_EVALUATIONS),
        # **予測ではない**。この2つを画面に出すときは幅であることを添えること。
        "accuracy": "order-of-magnitude (x2〜x3)",
    }


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
# 実際に解いた密度場)から取る。`design_space.CASES` を参照する経路はここに無い。
#
# 座標は**ワールドmm**である(probe は格子添字空間だった)。要素 (i,j,k) の中心は
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


def _debug_anchor_payload(stage: str, space) -> Optional[dict]:
    """拘束面・荷重面に**実際に割り当たった格子節点**のワールド座標。

    出所は導出が持ち帰った `DesignSpace.table`(`_check_anchor_centroids` が距離を測るのと
    同じ表)。表が無い塊(検査が直接組んだもの)では `None` を返す。
    """
    import numpy as np

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

    def anchors(self, stage: str, space) -> None:
        if not self.wants("anchors"):
            return
        payload = _debug_anchor_payload(stage, space)
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
    """4条件 → 塊の導出 → 連結性ガード → 最適化 → メッシュ化 → **メッシュボディとして文書へ**。

    **元のボディは1つも消さない・変えない。** 足すのは1つだけで、その `body_id` を返す。

    戻り値(`state` で分岐すること。`"done"` 以外では `body_id` は `None`):

        {"state": "done" | "empty_condition_faces" | "unrepresented_regions",
         "body_id": BodyId | None, "name": str | None,
         "warning": str | None,
         "empty_condition_faces": [診断, ...],     # `20260904-1556` と同じ形
         "unrepresented_regions": [診断, ...],     # 事後検出(下記)
         "resolution": resolution_limits() の戻り, "element_size_mm": float,
         "grid": {"origin","spacing","shape"}, "design_elements": int,
         "safety_factor": float|None, "target_safety_factor": float,
         "allowable": allowable_stress() の戻り,
         "material_mm3": float|None, "achieved_material_mm3": float|None,
         "converged": bool|None, "evaluations": int, "history": [...],
         "anchor_check": {"limit_mm","fixed_mm","load_mm"} | None,
         "triangle_count": int|None, "vertex_count": int|None,
         "seconds": float}

    **戻り値は素の値だけでできている**(`json.dumps` がそのまま通る)。画面(`20260904-1705`)は
    これをWebSocketへそのまま載せられる —— `DesignSpace` や `AnchorTable` のような
    実体をここから外へ出さない(出すと配れる形が壊れ、キャッシュの持ち主も増える)。

    **デバッグ表示**は `debug(event: dict)` + `debug_kinds`(`DEBUG_KINDS` の部分集合)。
    **頼まれたときだけ**塊(`space`)・面アンカーの節点(`anchors`)・平滑化前の密度場
    (`rho`)を配る —— 画面の `?debug=space` / `?debug=anchors` / `?voxel=1` の材料である。
    頼まれなければ配列を1本も作らないので、既定の実行の費用は1ビットも増えない。
    **戻り値には入れない**(素の値だけという契約を保つのと、結果メッセージが数MBに
    膨らむのを避けるため)。中身は `_DebugSink` の各メソッド、座標はワールドmm。

    **進捗**は `progress(event: dict)`(下記 `_ProgressPump`)。**中断**は
    `should_cancel()` が真を返した時点で `OptimizeCancelled` を投げて降りる ——
    見るのは SIMP の設計反復ごとと、二分探索の候補ごとである(粗い段の中は見ない。
    1.2〜1.6秒なので、その粒度で止まる)。

    失敗は例外で上がる。**握り潰さないこと** —— `OptimizeInputError`(条件が足りない・
    この版で扱えない)/ `FaceMappingError`(面添字の対応が壊れている。**RuntimeError 派生**)/
    `OptimizeCancelled`(中断)と、下の層からの `design_space.DesignSpaceDisconnectedError`
    (拘束と荷重がつながっていない)/ `DesignSpaceTooSmallError` /
    `design_space.CoarseRunConditionFacesEmptyError` / `safety_loop.SafetyTargetError`
    (目標安全率に届かない)/ `safety_loop.ConditionFacesEmptyError` はどれも
    「利用者が次に何をすればよいか」を文面に持っている。
    """
    from .topopt import design_space as ds
    from .topopt import safety_loop

    started = time.perf_counter()
    opts = options or OptimizeOptions()
    bundle = build_conditions(doc)
    limits = resolution_limits(doc, rmin_mm=opts.rmin_mm)
    spacing = _resolve_element_size(limits, opts.element_size_mm)

    pump = _ProgressPump(progress, should_cancel)
    dbg = _DebugSink(debug, debug_kinds)
    pump.emit(phase="prepare", message=f"要素サイズ {spacing:.4g} mm で塊を導出する")

    derivation_kwargs: dict = {"spacing": spacing}

    # **測るための導出を1回だけ先に回す。** 材料量の探索範囲は呼び出し側が決める契約
    # (`20260904-1437`)なので、塊の容量と保持ソリッドが占める体積を実際に測る必要がある。
    # ここだけ **`derivation="bbox"`(案1)を名指しする** —— 既定の案5は
    # `coarse_material_mm3` を要求する(= 量を決めるために量が要る)ので測る道具に使えない。
    # 案1は材料量を受け取らず、しかも**案5の塊を必ず含む**
    # (案5は `(膨らませた材料 | 条件の要素) & 案1の塊`)ので、容量の上界としてちょうどよい。
    # 格子は案5の本番段と**同一**である(どちらも同じ `derive(conditions, spacing=...)` から
    # 起きる)ので、ここで作った面アンカーの表と塊で、条件面と保持/除外の点検までできる。
    # **案1を消すときはここも一緒に直すこと。**
    space = ds.derive_default(
        bundle.conditions, derivation="bbox", **derivation_kwargs
    )
    pump.check()
    pump.emit(
        phase="derive",
        message=f"塊 {space.element_count} 要素(格子 {space.grid.shape})",
        design_elements=space.element_count,
    )
    # **早い段でも1度配る。** ここは実行ボタンから数秒で通る点で、`?debug=space` /
    # `?debug=anchors` を付けた人はここで塊とアンカーを見られる(最適化の数十秒〜数分を
    # 待たずに「塊が変」と分かる)。**これは案1(bbox)の塊であって本番の案5ではない**ので
    # `stage="derive"` を付けて区別する —— 案5の実物は下で `stage="final"` として配り直す。
    dbg.space("derive", space)
    dbg.anchors("derive", space)

    common = {
        "resolution": limits,
        "element_size_mm": float(space.grid.spacing),
        "grid": space.grid.as_dict(),
        "design_elements": int(space.element_count),
        "allowable": allowable_stress(),
        "target_safety_factor": float(opts.target_safety_factor),
        # 条件面が空で早期returnしたときは測れない(節点が0個)ので None のまま。
        "anchor_check": None,
    }

    # **条件面が本番の解像度で空になっていないか。** `derive_default` は粗い段の空だけを
    # 例外(`CoarseRunConditionFacesEmptyError`)で落とすので、本番解像度のほうは
    # 導出が持ち帰った面アンカーの表から自分で見る(`assert_faces_connected` は
    # 空のときに判定を降りるだけで、上へは伝えない)。運び方は `20260904-1556` と同じ ——
    # 診断を戻り値へ積み、1文の警告を付け、`simp` は呼ばずに早期return。
    empty_faces = _empty_condition_faces(space)
    if empty_faces:
        from .topopt import anchor as an

        return dict(
            common,
            state="empty_condition_faces",
            body_id=None,
            name=None,
            warning=an.format_condition_face_warning(empty_faces),
            empty_condition_faces=empty_faces,
            unrepresented_regions=[],
            safety_factor=None,
            material_mm3=None,
            achieved_material_mm3=None,
            converged=None,
            evaluations=0,
            history=[],
            triangle_count=None,
            vertex_count=None,
            seconds=time.perf_counter() - started,
        )

    # **荷重・拘束が本当に選んだ面に載ったか**を、実際にできたアンカーで測り直す。
    # `_verify_face_mapping` は「まとめた形の面添字」の対応を見るが、こちらは
    # 「その面添字で作られた節点集合が、その面のところにあるか」を見る(2枚目の網)。
    anchor_check = _check_anchor_centroids(doc, space)
    common["anchor_check"] = anchor_check

    # **事後検出。** 保持・除外ソリッドが、この解像度で1つでも表現できていないなら
    # 最適化を回さずに戻る(同じ運び方)。
    unrepresented = _region_representation(doc, bundle, space)
    if any(item["elements"] == 0 for item in unrepresented):
        return dict(
            common,
            state="unrepresented_regions",
            body_id=None,
            name=None,
            warning=format_region_warning(unrepresented),
            empty_condition_faces=[],
            unrepresented_regions=unrepresented,
            safety_factor=None,
            material_mm3=None,
            achieved_material_mm3=None,
            converged=None,
            evaluations=0,
            history=[],
            triangle_count=None,
            vertex_count=None,
            seconds=time.perf_counter() - started,
        )

    element_volume = space.grid.element_volume
    capacity_mm3 = space.element_count * element_volume
    keep_mm3 = float(space.model.keep.sum()) * element_volume
    material_min, material_max = _material_bounds(opts, keep_mm3, capacity_mm3)

    pump.emit(
        phase="search",
        message=(
            f"材料量 {material_min:.4g}〜{material_max:.4g} mm^3 で"
            f"安全率 {opts.target_safety_factor:.3g} を満たす最小を探す"
        ),
    )

    result = safety_loop.solve_for_safety_factor(
        bundle.conditions,
        target_safety_factor=float(opts.target_safety_factor),
        material_min_mm3=material_min,
        material_max_mm3=material_max,
        max_iter=int(
            safety_loop.DEFAULT_MAX_ITER if opts.max_iter is None else opts.max_iter
        ),
        max_evaluations=int(
            safety_loop.DEFAULT_MAX_EVALUATIONS
            if opts.max_evaluations is None
            else opts.max_evaluations
        ),
        forces=bundle.forces,
        derivation_kwargs=derivation_kwargs,
        progress=pump.on_search_step,
        run_progress=pump.on_iteration,
    )
    pump.check()

    # **本番の塊(案5)と、平滑化する前の密度場。** ここが `?debug=space` / `?voxel=1` の正で、
    # 上の `stage="derive"` は「実行ボタンからすぐ見える速報」でしかない。
    dbg.space("final", result.space)
    dbg.anchors("final", result.space)
    if result.run is not None and result.run.get("result") is not None:
        dbg.rho("final", result.space.grid, result.run["result"].rho)

    surface = result.run["surface"]
    grid = result.space.grid
    positions = _mesh_to_world(surface.verts_smooth, grid)
    _check_mesh_placement(positions, grid)
    indices = surface.faces.ravel().tolist()

    name = opts.name or _unique_result_name(doc)
    pump.emit(phase="insert", message=f"三角形 {len(indices) // 3} 枚のボディを足す")
    from .verbs import add_mesh_body

    body_id = add_mesh_body(doc, positions, indices, name=name)

    return dict(
        common,
        state="done",
        body_id=body_id,
        name=name,
        element_size_mm=float(grid.spacing),
        grid=grid.as_dict(),
        design_elements=int(result.space.element_count),
        warning=format_region_warning(unrepresented) if unrepresented else None,
        empty_condition_faces=[],
        unrepresented_regions=unrepresented,
        safety_factor=float(result.safety_factor),
        material_mm3=float(result.material_mm3),
        achieved_material_mm3=float(result.achieved_material_mm3),
        converged=bool(result.converged),
        evaluations=int(result.evaluations),
        history=[_step_payload(step) for step in result.history],
        triangle_count=len(indices) // 3,
        vertex_count=len(positions) // 3,
        seconds=time.perf_counter() - started,
    )


#: アンカーの節点重心が条件面の重心からどこまで離れてよいか(要素サイズの倍数)。
#: **粗い網である** —— 狙いは「別のボディの面を掴んだ」級の取り違え(このタスクの題材では
#: 50mm 以上離れる)を実行時にも捕まえることで、隣接面との取り違えは
#: `_verify_face_mapping` が決定的に落とす。
ANCHOR_CENTROID_LIMIT_ELEMENTS = 3.0


def _check_anchor_centroids(doc: "Document", space) -> dict:
    """条件面の**節点集合が本当にその面のところにあるか**を測る(実行時の2枚目の網)。

    面添字の取り違えは例外を出さない。`_verify_face_mapping` は B-rep の側で対応を確かめるが、
    それを通り抜けた場合(まとめ方を将来変えたとき等)、ここが最後に気づける場所である。
    測るのは `anchor.FaceAnchor.centroid`(節点の担当面積で重み付けした重心)と、条件に
    焼き込んである面の重心の距離。
    """
    import numpy as np

    table = getattr(space, "table", None)
    if table is None:
        return {}
    setup = doc.optimize
    limit = ANCHOR_CENTROID_LIMIT_ELEMENTS * float(space.grid.spacing)
    out: dict = {"limit_mm": limit}
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
            center = np.asarray(condition["center"], dtype=float)
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

    `DesignSpace.table` は導出が作った**本番の格子の**面アンカーの表である
    (案5が同じ表を2度作らないために持たせてある)。無ければ空リストを返す ——
    その場合は `run_topopt` の入口層が同じ診断を出す。
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
            f"(総要素数の上限 {MAX_ELEMENTS} に収まらない)。"
            "条件面をもっと大きい面にするか、最小部材幅(rmin)を大きくすること"
        )
    if requested is None:
        return upper
    value = float(requested)
    if not math.isfinite(value) or value <= 0.0:
        raise OptimizeInputError(f"element_size_mm は有限の正の値であること(いま {requested!r})")
    if value > upper:
        raise OptimizeInputError(
            f"要素サイズ {value:.4g} mm は上限 {upper:.4g} mm より粗い"
            "(条件面に格子節点が付かないか、密度フィルタが効かなくなる)"
        )
    if value < lower:
        raise OptimizeInputError(
            f"要素サイズ {value:.4g} mm は下限 {lower:.4g} mm より細かい"
            f"(総要素数が {MAX_ELEMENTS} を超える)"
        )
    return value


def _material_bounds(
    opts: OptimizeOptions, keep_mm3: float, capacity_mm3: float
) -> tuple:
    """材料量の探索範囲[mm^3]。`solve_for_safety_factor` は範囲を自分で決めない契約である。

    下端は**保持ソリッドが占める体積より必ず大きく**(そこは動かせないので、それ以下を
    目標にすると `simp` の OC が退化する)、上端は**塊の容量の端に置かない**
    (`20260904-0142` の罠)。
    """
    low = keep_mm3 * MATERIAL_MIN_RATIO
    high = capacity_mm3 * MATERIAL_MAX_RATIO
    if opts.material_min_mm3 is not None:
        low = float(opts.material_min_mm3)
    if opts.material_max_mm3 is not None:
        high = float(opts.material_max_mm3)
    if not (0.0 < low < high):
        raise OptimizeInputError(
            f"材料量の探索範囲が作れない(下端 {low:.6g} / 上端 {high:.6g} mm^3。"
            f"保持ソリッド {keep_mm3:.6g} / 塊の容量 {capacity_mm3:.6g})。"
            "保持ソリッドが塊のほとんどを占めていると、動かせる材料が残らない"
        )
    return low, high


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


def _step_payload(step) -> dict:
    return {
        "material_mm3": float(step.material_mm3),
        "outcome": str(step.outcome),
        "safety_factor": None if step.safety_factor is None else float(step.safety_factor),
        "elements": None if step.elements is None else int(step.elements),
        "error": None if step.error is None else str(step.error),
    }


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
class _ProgressPump:
    """進捗を1つの形に均して配り、そのついでに中断を見る。

    **中断を見る場所を進捗と同じにしてある**のは、長い処理の中で呼ばれる点がそこしか
    無いからである(タスク本文「`progress` コールバックから止められる程度」)。
    """

    def __init__(
        self,
        sink: Optional[Callable[[dict], None]],
        should_cancel: Optional[Callable[[], bool]],
    ) -> None:
        self._sink = sink
        self._should_cancel = should_cancel
        self._evaluation = 0

    def check(self) -> None:
        if self._should_cancel is not None and self._should_cancel():
            raise OptimizeCancelled("形状最適化を中断した")

    def emit(self, **event: Any) -> None:
        self.check()
        if self._sink is None:
            return
        payload = {
            "phase": event.pop("phase"),
            "message": event.pop("message", ""),
            "evaluation": self._evaluation,
        }
        payload.update(event)
        self._sink(payload)

    def on_iteration(self, iteration, _rho) -> None:
        """`simp.optimize` の設計反復ごと(`callback(Iteration, rho)`)。"""
        self.emit(
            phase="optimize",
            message=f"設計反復 {iteration.it}",
            iteration=int(iteration.it),
            compliance=float(iteration.compliance),
            volfrac=float(iteration.volfrac),
            change=float(iteration.change),
        )

    def on_search_step(self, step) -> None:
        """`safety_loop` の候補材料量ごと(`progress(SafetyLoopStep)`)。"""
        self._evaluation += 1
        self.emit(
            phase="search",
            message=(
                f"材料 {step.material_mm3:.4g} mm^3 → "
                + (
                    f"安全率 {step.safety_factor:.3f}"
                    if step.safety_factor is not None
                    else str(step.outcome)
                )
            ),
            material_mm3=float(step.material_mm3),
            outcome=str(step.outcome),
            safety_factor=None if step.safety_factor is None else float(step.safety_factor),
        )

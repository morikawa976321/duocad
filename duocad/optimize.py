"""形状最適化(第3のモード)の条件 —— **データ・面の指紋・配りものだけ**を持つ。

verbの本体は `duocad/verbs/optimize_setup.py`、状態を配る口は
`DocumentView.state_meta()` である。ここに置くのは3つ:

1. **`OptimizeSetup`** … 1文書が1つ持つ条件の入れ物(`Document.optimize`)。
   保持/除外ソリッド・拘束面・荷重面と、荷重の大きさ・向き。
2. **面の指紋** … 面の参照は添字だけでは足りない(`duocad/selection.py` のとおり
   **添字は現時点のものであって、形が変われば別の実体を指す**)。重心・法線・面積を
   一緒に控えておき、配るときに照合する。**壊れないようにするのではなく、壊れたと
   気づけるようにする**のが狙いで、合わない条件は配らずに `stale` として名指しする。
   **照合に使うのは面積と法線だけで、重心は記録するだけ**(理由は `fingerprint_mismatch`)。
3. **配りもの** … `state_meta()` に載せる形(下記 `state_payload`)と、その**増分維持**。

**配信のたびに全ボディを走査しない**(engine `duocad-state-meta-recomputes-all-volumes`)。
照合が要るのは**条件が指しているボディだけ**で、しかも**その形状が差し替わったときだけ**
である。条件を1つも置いていない文書ではOCCTを1回も呼ばない。

**OCP / build123d を直接importしない**(幾何は `query` 越し)。さらに `query` の import は
**関数の中**に置いてある —— `document.py` がこのモジュールを読むので、モジュール直下で
`query` を引くと、文書のデータモデルを触るだけでOCPまで芋づるに読み込まれる。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - 型注釈のためだけ(実行時は読まない)
    from .document import Document

#: 面の指紋の許容差(相対・絶対)。
#:
#: **再生は決定的**(同じ操作列を同じOCCTへ流す)なので、開き直した文書の面積・法線は
#: 記録した値と一致する。ここにあるのは丸め誤差ぶんの隙間でしかなく、**緩めると
#: 「添字が別の面へ移った」を見逃す**(それを検出するために指紋を置いている)。
FINGERPRINT_RTOL = 1e-6
FINGERPRINT_ATOL = 1e-9

#: 荷重の向きの表し方。
#:
#: - `"normal"`  … その面の**外向き法線**(面ごとに向きが変わる)
#: - `"-normal"` … その逆向き(面を押し込む向き)
#: - `"vector"`  … 世界座標の単位ベクトル(`OptimizeSetup.load_vector`)
#:
#: **世界軸は `"vector"` で表す**(`-Z` は `[0, 0, -1]`)。軸の名前をもう一つの語彙として
#: 足すと、同じ向きの綴りが2系統になり、片方だけ直したときに黙って食い違う。
DIRECTION_MODES = ("normal", "-normal", "vector")

#: 面を食う役割の名前(排他の相手を引くのにも使う)。
FACE_ROLES = ("fixtures", "loads")

#: ボディを食う役割の名前。
BODY_ROLES = ("preserve_bodies", "exclude_bodies")

#: 役割 -> 先勝ちの相手(**排他は対称**なので両方向を書く)。
OPPOSITE_ROLE = {
    "preserve_bodies": "exclude_bodies",
    "exclude_bodies": "preserve_bodies",
    "fixtures": "loads",
    "loads": "fixtures",
}

#: 役割の日本語名(エラー文はAIにも人にも読まれるので、名前を1箇所で持つ)。
ROLE_LABEL = {
    "preserve_bodies": "保持ソリッド",
    "exclude_bodies": "除外ソリッド",
    "fixtures": "拘束面",
    "loads": "荷重面",
}


class RoleConflict(ValueError):
    """**先勝ち**の排他に当たった(`ValueError` の派生 = MCPの `invalid_argument`)。

    「すでに保持に割り当てられているボディを除外に割り当てようとした」ような失敗。
    **どれが衝突したかを名指しで持つ**(`conflicts`)ので、画面はそれをトーストに出せる。
    集合代入なので、移したいときは「元の役割のverbで外す → 新しい役割のverbで付ける」の
    2手になる —— これは意図した仕様であって、ここで自動的に奪い取らない。
    """

    def __init__(self, message: str, role: str, conflicts: Sequence[dict]) -> None:
        super().__init__(message)
        self.role = role
        self.conflicts = [dict(item) for item in conflicts]


@dataclass
class OptimizeSetup:
    """形状最適化の条件。**操作ログが正で、これはその再生結果**(`Document.optimize`)。

    保存されるのは操作ログのJSONだけなので、ここを直に書き換えてはならない ——
    書き換えると保存・再オープンで復元されない状態が生まれる。書き込む口は
    `duocad/verbs/optimize_setup.py` の4つのverbだけである。

    `fixtures` / `loads` の要素は **FaceCondition**(下記 `face_condition`)で、
    `{"body", "index", "area", "normal", "center"}` を持つ。
    """

    #: 保持ソリッド / 除外ソリッド(`body_id` の並び。`body-{uuid}` は安定なのでそのまま)
    preserve_bodies: List[str] = field(default_factory=list)
    exclude_bodies: List[str] = field(default_factory=list)
    #: 拘束面 / 荷重面(FaceCondition の並び)
    fixtures: List[dict] = field(default_factory=list)
    loads: List[dict] = field(default_factory=list)
    #: 荷重の大きさ[N]。**v1は1文書に1種類**なので、`loads` の全面がこれを共有する。
    load_magnitude: float = 0.0
    #: 荷重の向き(`DIRECTION_MODES`)と、`"vector"` のときの単位ベクトル。
    load_direction: str = "vector"
    load_vector: Optional[List[float]] = None
    #: **配りものの増分維持のための版**。中身を変えたら必ず `touch()` で1つ上げる
    #: (上げ忘れると、画面が古い割り当てを描き続ける)。
    revision: int = 0

    def touch(self) -> None:
        self.revision += 1


# --------------------------------------------------------------------------------------
# 面の指紋
# --------------------------------------------------------------------------------------
def face_fingerprint(doc: "Document", body_id: str, index: int) -> dict:
    """いまの形から面1枚の指紋(面積・法線・重心)を測る。

    **重心も測って記録する**が、照合には使わない(`fingerprint_mismatch`)。

    存在しないBodyは KeyError、範囲外の添字は ValueError(`query.face_fingerprint`)。
    """
    from . import query  # 遅延import(モジュール docstring)

    body = doc.get_body(body_id)
    if body.mesh is not None:
        # **名指しの面参照なので断る**(`predicates._target_bodies` と同じ線)。
        # 断り文は `meshbody.reject` に一元化してある。
        from . import meshbody  # 遅延import(モジュール docstring)

        meshbody.reject(body, "面の指紋を測る")
    if body.shape is None:
        raise ValueError(f"body {body_id!r} has no shape (面を測れない)")
    return query.face_fingerprint(body.shape, int(index))


def face_condition(doc: "Document", ref: dict) -> dict:
    """面の ShapeRef から **FaceCondition** を作る(指紋を**その場で**測って焼き込む)。

    `{"body": BodyId, "index": int, "area": float, "normal": [x,y,z], "center": [x,y,z]}`。

    **測るのは付けた瞬間の1回だけ**である。以後は照合に使うだけなので、配信のたびに
    測り直すことはしない(モジュール docstring)。
    """
    body_id = ref["body"]
    index = int(ref["index"])
    measured = face_fingerprint(doc, body_id, index)
    return {
        "body": body_id,
        "index": index,
        "area": measured["area"],
        "normal": list(measured["normal"]),
        "center": list(measured["center"]),
    }


def face_key(item: dict) -> tuple:
    """FaceCondition / ShapeRef を「同じ面か」で比べるための鍵。"""
    return (item["body"], int(item["index"]))


def fingerprint_mismatch(doc: "Document", condition: dict) -> Optional[str]:
    """条件が指す面が**今も同じ面か**を照合する。合っていれば `None`、違えば理由の文字列。

    理由を文字列で返すのは、画面がトーストにそのまま出すためである(「合わない」だけでは
    次の一手が書いていない)。**ここで文書を直さない** —— 条件は操作ログの上に残り、
    形を戻せば(undo)また合うようになる。消してしまうと戻せない。

    **見るのは面積と法線だけで、重心は見ない**。重心は世界座標なので、
    面の添字を変えない `move_body`(平行移動)でも不一致になり、位置を詰めるたびに条件が
    付け直しになっていた。面積と法線は平行移動で不変で、かつ照合には添字の一致も要るため、
    本来の狙い(添字が別の面を指すようになった)の検出力はほとんど落ちない。
    重心は `center` として記録され続ける —— 不一致になったときに何が起きたかを見るため。
    """
    body_id = condition["body"]
    index = int(condition["index"])
    try:
        measured = face_fingerprint(doc, body_id, index)
    except KeyError:
        return f"ボディ {body_id} が文書に無い"
    except ValueError as error:
        return f"面 #{index} を測れない({error})"

    if not _close(measured["area"], condition["area"]):
        return (
            f"面 #{index} の面積が変わった"
            f"(記録 {condition['area']:.6g} / いま {measured['area']:.6g})"
        )
    if not _close_point(measured["normal"], condition["normal"]):
        return (
            f"面 #{index} の法線が変わった"
            f"(記録 {_fmt(condition['normal'])} / いま {_fmt(measured['normal'])})"
        )
    return None


def _close(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) <= FINGERPRINT_ATOL + FINGERPRINT_RTOL * max(
        abs(float(left)), abs(float(right))
    )


def _close_point(left: Sequence[float], right: Sequence[float]) -> bool:
    return len(left) == len(right) and all(_close(a, b) for a, b in zip(left, right))


def _fmt(values: Sequence[float]) -> str:
    return "[" + ", ".join(f"{float(value):.4g}" for value in values) + "]"


# --------------------------------------------------------------------------------------
# 荷重の向き・大きさ
# --------------------------------------------------------------------------------------
def normalize_direction(value: Any) -> tuple:
    """`set_loads` の `direction` 引数を `(モード, 単位ベクトル|None)` へ均す。

    受けるのは3つの形: `"normal"` / `"-normal"` / 長さ3の数値の並び。

    **ベクトルは単位化して控える。** 大きさ[N]は `magnitude` が持つと決めた以上、向きの
    長さにも意味があると、`magnitude=100` `direction=[0,0,-2]` が 100N なのか 200N なのかを
    読む側が決められない。長さ0・非有限は ValueError。
    """
    if isinstance(value, str):
        mode = value.strip()
        if mode not in ("normal", "-normal"):
            raise ValueError(
                f"unknown load direction: {value!r} "
                "(面法線なら 'normal' / '-normal'、それ以外は [x, y, z] のベクトルで渡す)"
            )
        return mode, None

    if isinstance(value, bool) or not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(
            f"load direction must be 'normal', '-normal', or [x, y, z], got {value!r}"
        )
    try:
        components = [float(item) for item in value]
    except (TypeError, ValueError):
        raise ValueError(f"load direction has a non-numeric component: {value!r}") from None
    if not all(math.isfinite(item) for item in components):
        raise ValueError(f"load direction must be finite, got {value!r}")
    length = math.sqrt(sum(item * item for item in components))
    if length <= 0.0:
        raise ValueError("load direction must not be the zero vector")
    return "vector", [item / length for item in components]


def normalize_magnitude(value: Any) -> float:
    """荷重の大きさ[N]。**有限かつ正**(0は「荷重が無い」= 面を1枚も選ばないことで表す)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"load magnitude must be a number [N], got {value!r}")
    magnitude = float(value)
    if not math.isfinite(magnitude) or magnitude <= 0.0:
        raise ValueError(f"load magnitude must be finite and > 0 [N], got {value!r}")
    return magnitude


# --------------------------------------------------------------------------------------
# 配りもの(`state_meta()` の "optimize")
# --------------------------------------------------------------------------------------
def body_ref(body_id: str) -> dict:
    """配信用のボディ参照。**`selection.select_body` と同じ形**にしておく。

    画面は受け取った参照をそのまま `modify_selection` へ渡してプリセレクトする ——
    形が違うと、画面側にもう1つ変換が生まれる(engine
    `duocad-target-set-taken-only-from-selection` が消したのはその形の重複である)。
    """
    return {"body": body_id, "kind": "body", "index": None, "provenance": None}


def face_ref(condition: dict) -> dict:
    """配信用の面参照(`selection.select_face` と同じ形。理由は `body_ref` と同じ)。"""
    return {
        "body": condition["body"],
        "kind": "face",
        "index": int(condition["index"]),
        "provenance": None,
    }


class OptimizeStateCache:
    """`state_payload()` の増分維持(`DocumentView` が1つ持つ)。

    作り直すのは次の3つのどれかが起きたときだけ:

    1. 条件そのものが変わった(`OptimizeSetup.revision` が動いた)
    2. 条件が**指しているボディ**の形状が差し替わった(`is` で判定。他のボディは見ない)
    3. まだ1度も作っていない

    2'. 条件が指しているボディの**可視性**が変わった(見えないものは指せないので、
        配る参照から外れる)

    定常状態では**OCCTを1回も呼ばない**。形状オブジェクトは参照ごと控える ——
    `id()` の整数だけだと、解放後に同じアドレスが再利用されて偽ヒットしうる
    (`DocumentView._body_volume` と同じ代償・同じ理由)。
    """

    def __init__(self) -> None:
        self._revision: Optional[int] = None
        self._referenced: dict = {}
        self._payload: Optional[dict] = None

    def clear(self) -> None:
        """文書を差し替えたら捨てる(`DocumentView.reset`)。"""
        self._revision = None
        self._referenced = {}
        self._payload = None

    def fresh_for(self, revision: int, referenced: dict) -> bool:
        """`(形状, 可視性)` の表が前回と同じか。**形状は `is`、可視性は `==`**。"""
        if self._payload is None or self._revision != revision:
            return False
        if set(self._referenced) != set(referenced):
            return False
        return all(
            self._referenced[key][0] is referenced[key][0]
            and self._referenced[key][1] == referenced[key][1]
            for key in referenced
        )

    def store(self, revision: int, referenced: dict, payload: dict) -> dict:
        self._revision = revision
        self._referenced = dict(referenced)
        self._payload = payload
        return payload

    @property
    def payload(self) -> Optional[dict]:
        return self._payload


#: `stale` の理由コード。**文言ではなくこれで判定させる**(画面がどれをトーストに出すかを
#: 決めるのに要る)。`"hidden"` は人が部品を隠しただけで日常的に出るので、出さない側である。
#:
#: - `"fingerprint"` … 後から形を変えて、添字が別の面を指すようになった(**本来の狙い**)
#: - `"missing"`     … 対象のボディが文書に無い(undoで作成ごと消えている)
#: - `"hidden"`      … 対象のボディが非表示。表示に戻せばそのまま使われる
STALE_CODES = ("fingerprint", "missing", "hidden")


def state_payload(
    doc: "Document",
    cache: Optional[OptimizeStateCache] = None,
    *,
    previewing: bool = False,
) -> dict:
    """`state_meta()["optimize"]` に載せる形。**画面側(後続タスク)が読む契約はこれ。**

        {
          "preserve_bodies": [BodyRef, ...],   # {"body","kind":"body","index":null,"provenance":null}
          "exclude_bodies":  [BodyRef, ...],
          "fixtures":        [FaceRef, ...],   # {"body","kind":"face","index":int,"provenance":null}
          "loads":           [FaceRef, ...],
          "load": {"magnitude": float,                     # [N]
                   "direction": "normal"|"-normal"|"vector",
                   "vector": [x,y,z] | null} | null,   # 荷重面を1枚も置いていなければ null
          "stale": [{"role": "preserve_bodies"|"exclude_bodies"|"fixtures"|"loads",
                     "body": BodyId, "index": int|null,
                     "code": STALE_CODES のどれか, "reason": str}, ...],
          "revision": int,
        }

    **4つの配列はそのまま `modify_selection` へ渡せる** —— verbを起動したときに、既に
    その役割が付いている対象をプリセレクトするのが画面側の使い方だからである。
    したがって**いま指せないものは4つの配列に載らない**(不可視・消えたボディを
    `modify_selection` へ渡すと例外になる。選択機構の「見えないものは指せない」)。
    載らなかったものは全部 `stale` に理由つきで出るので、**黙って消える経路は無い**。

    **`load` は文書が持つ how であって「いま荷重がかかっているか」ではない。** 荷重面の
    ボディを隠すと `loads` は空になるが `load` は残る —— 隠しただけで大きさと向きが画面から
    消えると、表示に戻したときに打ち直しになるからである。かかる面は `loads` を見ること。

    **`stale` は「割り当てが消えた」ではない。** 文書からは何も消えていない(操作ログが正)
    ので、形を戻せば・表示に戻せば次の配信でまた載る。画面がトーストに出すかどうかは
    `code` で決めること(`STALE_CODES`)——`"hidden"` は人が部品を隠すたびに出る。

    `previewing` の間は作り直さない —— プレビューは文書を投機的に書き換えて必ず元へ戻すので、
    その最中の形で照合すると偽の `stale` が出る(`DocumentView._prune_body_caches` と同じ規律)。

    **戻り値は書き換えないこと**(キャッシュしている実体をそのまま返す)。
    """
    setup = doc.optimize
    referenced = _referenced_state(doc, setup)

    if cache is not None:
        if previewing and cache.payload is not None:
            return cache.payload
        if cache.fresh_for(setup.revision, referenced):
            return cache.payload  # type: ignore[return-value]

    live: dict = {role: [] for role in BODY_ROLES + FACE_ROLES}
    stale: List[dict] = []

    for role in BODY_ROLES:
        for body_id in getattr(setup, role):
            trouble = _body_trouble(doc, body_id)
            if trouble is None:
                live[role].append(body_ref(body_id))
            else:
                code, reason = trouble
                stale.append(
                    {
                        "role": role,
                        "body": body_id,
                        "index": None,
                        "code": code,
                        "reason": reason,
                    }
                )

    for role in FACE_ROLES:
        for condition in getattr(setup, role):
            trouble = _body_trouble(doc, condition["body"])
            if trouble is None:
                reason = fingerprint_mismatch(doc, condition)
                trouble = None if reason is None else ("fingerprint", reason)
            if trouble is None:
                live[role].append(face_ref(condition))
            else:
                code, reason = trouble
                stale.append(
                    {
                        "role": role,
                        "body": condition["body"],
                        "index": int(condition["index"]),
                        "code": code,
                        "reason": reason,
                    }
                )

    payload = {
        "preserve_bodies": live["preserve_bodies"],
        "exclude_bodies": live["exclude_bodies"],
        "fixtures": live["fixtures"],
        "loads": live["loads"],
        "load": (
            {
                "magnitude": setup.load_magnitude,
                "direction": setup.load_direction,
                "vector": None if setup.load_vector is None else list(setup.load_vector),
            }
            # **`live["loads"]` ではなく文書の側を見る** —— 荷重面のボディを隠しただけで
            # 大きさと向きが画面から消えると、表示に戻したときに打ち直しになる。
            # 「実際に荷重がかかる面」は `loads` が持つ(こちらは how だけ)。
            if setup.loads
            else None
        ),
        "stale": stale,
        "revision": setup.revision,
    }
    if cache is not None:
        return cache.store(setup.revision, referenced, payload)
    return payload


def _body_trouble(doc: "Document", body_id: str) -> Optional[tuple]:
    """そのボディが**いま指せるか**。指せれば `None`、指せなければ `(code, 理由)`。

    可視性の判定をここに置いてあるのは、**指紋を測る前に落とすため**でもある
    (隠しただけの部品でOCCTを回す理由が無い)。
    """
    try:
        body = doc.get_body(body_id)
    except KeyError:
        return ("missing", f"ボディ {body_id} が文書に無い")
    if not body.visible:
        return ("hidden", f"ボディ {body.name} が非表示(表示に戻せばまた使われる)")
    return None


def _referenced_state(doc: "Document", setup: OptimizeSetup) -> dict:
    """条件が指しているボディの `(形状, 可視性)` だけを集める(**全ボディを走査しない**)。

    文書から消えたボディは `(None, False)` を持たせる —— 「消えた」も「隠された」も
    配りものを変える状態なので、キャッシュの鍵に入っていないと判定が更新されない
    (可視性は形状の同一性を動かさないので、形だけを鍵にすると取りこぼす)。
    """
    referenced: dict = {}
    for role in BODY_ROLES:
        for body_id in getattr(setup, role):
            referenced.setdefault(body_id, _body_state(doc, body_id))
    for role in FACE_ROLES:
        for condition in getattr(setup, role):
            referenced.setdefault(
                condition["body"], _body_state(doc, condition["body"])
            )
    return referenced


def _body_state(doc: "Document", body_id: str) -> tuple:
    try:
        body = doc.get_body(body_id)
    except KeyError:
        return (None, False)
    return (body.shape, body.visible)

"""形状最適化(第3のモード)の条件 —— **データ・面の指紋・配りものだけ**を持つ。

verbの本体は `duocad/verbs/optimize_setup.py`、状態を配る口は `DocumentView.state_meta()`。
ここに置くのは3つ:

1. **`OptimizeSetup`** … 1文書が1つ持つ条件の入れ物(`Document.optimize`)。保持/除外ソリッド・
   拘束面・荷重面。荷重の大きさ[N]と向きは面ごとに持つ。
2. **面の指紋** … 面の参照は添字だけでは足りない(`duocad/selection.py`: 添字は現時点のもので、
   形が変われば別の実体を指す)。面積・法線・重心を控えて配るときに照合し、合わない条件は
   配らずに `stale` として名指しする。**照合に使うのは面積と法線だけで、重心は記録するだけ**
   (理由は `fingerprint_mismatch`)。
3. **配りもの** … `state_meta()` に載せる形(`state_payload`)と、その増分維持。

**配信のたびに全ボディを走査しない**。照合が要るのは条件が指しているボディの形状が差し替わった
ときだけで、条件を1つも置いていない文書ではOCCTを1回も呼ばない。

**OCP / build123d を直接importしない**(幾何は `query` 越し)。`query` の import は**関数の中**に
置く —— `document.py` がこのモジュールを読むので、モジュール直下で引くと文書のデータモデルを
触るだけでOCPまで読み込まれる。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - 型注釈のためだけ(実行時は読まない)
    from .document import Document

#: 面の指紋の許容差(相対・絶対)。再生は決定的なので丸め誤差ぶんの隙間でしかなく、
#: 緩めると「添字が別の面へ移った」を見逃す。
FINGERPRINT_RTOL = 1e-6
FINGERPRINT_ATOL = 1e-9

#: 面を食う役割の名前。
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

#: 役割の日本語名(エラー文用)。
ROLE_LABEL = {
    "preserve_bodies": "保持ソリッド",
    "exclude_bodies": "除外ソリッド",
    "fixtures": "拘束面",
    "loads": "荷重面",
}


class RoleConflict(ValueError):
    """**先勝ち**の排他に当たった(`ValueError` の派生 = MCPの `invalid_argument`)。

    衝突したものを `conflicts` で名指しで持つ(画面がトーストに出す)。役割を移すには
    「元の役割のverbで外す → 新しい役割のverbで付ける」の2手で、**自動的に奪い取らない**(意図した仕様)。
    """

    def __init__(self, message: str, conflicts: Sequence[dict]) -> None:
        super().__init__(message)
        self.conflicts = [dict(item) for item in conflicts]


@dataclass
class OptimizeSetup:
    """形状最適化の条件。**操作ログが正で、これはその再生結果**(`Document.optimize`)。

    直に書き換えてはならない(保存・再オープンで復元されない状態が生まれる)。書き込む口は
    `duocad/verbs/optimize_setup.py` の4つのverbだけ。

    `fixtures` の要素は **FaceCondition**(`face_condition`: `{"body", "index", "area", "normal", "center"}`)、
    `loads` の要素はそこへ `{"magnitude", "direction", "vector"}` を足した **LoadCondition**
    (`load_condition`)で、大きさと向きは面ごとに違ってよい。
    """

    #: 保持ソリッド / 除外ソリッド(`body_id` の並び)
    preserve_bodies: List[str] = field(default_factory=list)
    exclude_bodies: List[str] = field(default_factory=list)
    #: 拘束面(FaceCondition の並び)
    fixtures: List[dict] = field(default_factory=list)
    #: 荷重面(LoadCondition の並び。大きさ[N]・向きを面ごとに持つ)
    loads: List[dict] = field(default_factory=list)
    #: 配りものの増分維持のための版。中身を変えたら必ず `touch()` で上げる(忘れると画面が古い割り当てを描き続ける)。
    revision: int = 0

    def touch(self) -> None:
        self.revision += 1


# --------------------------------------------------------------------------------------
# 面の指紋
# --------------------------------------------------------------------------------------
def face_fingerprint(doc: "Document", body_id: str, index: int) -> dict:
    """いまの形から面1枚の指紋(面積・法線・重心)を測る。重心は照合に使わない(`fingerprint_mismatch`)。

    存在しないBodyは KeyError、範囲外の添字は ValueError(`query.face_fingerprint`)。
    """
    from . import query  # 遅延import(モジュール docstring)

    body = doc.get_body(body_id)
    if body.mesh is not None:
        # 名指しの面参照なので断る(`predicates._target_bodies` と同じ線)。
        from . import meshbody  # 遅延import(モジュール docstring)

        meshbody.reject(body, "面の指紋を測る")
    if body.shape is None:
        raise ValueError(f"body {body_id!r} has no shape (面を測れない)")
    return query.face_fingerprint(body.shape, int(index))


def face_condition(doc: "Document", ref: dict) -> dict:
    """面の ShapeRef から **FaceCondition** を作る(指紋を**その場で**測って焼き込む)。

    `{"body": BodyId, "index": int, "area": float, "normal": [x,y,z], "center": [x,y,z]}`。

    測るのは付けた瞬間の1回だけ。配信のたびに測り直さない(モジュール docstring)。
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


def load_condition(doc: "Document", ref: dict, magnitude: Any, direction: Any) -> dict:
    """面の ShapeRef + 大きさ + 向き から **LoadCondition** を作る。

    `FaceCondition` に `{"magnitude": float, "direction": str, "vector": [x,y,z]|None}` を足したもの。
    荷重の合成(`optimize_run.build_conditions`)は面ごとの力ベクトルを足し合わせる。

    `direction` が `"normal"` / `"-normal"` のとき `vector` は `None`(向きは指紋の法線から起こす)。
    """
    condition = face_condition(doc, ref)
    mode, vector = normalize_direction(direction)
    condition["magnitude"] = normalize_magnitude(magnitude)
    condition["direction"] = mode
    condition["vector"] = None if vector is None else list(vector)
    return condition


def load_force(condition: dict) -> List[float]:
    """LoadCondition → **世界座標の力ベクトル[N]**(向き × 大きさ)。`"normal"` / `"-normal"` は指紋の外向き法線から起こす。"""
    mode = condition["direction"]
    if mode == "vector":
        vector = condition.get("vector")
        if not vector:
            raise ValueError("荷重の向きベクトルが入っていない")
        unit = [float(v) for v in vector]
    elif mode == "normal":
        unit = [float(v) for v in condition["normal"]]
    elif mode == "-normal":
        unit = [-float(v) for v in condition["normal"]]
    else:
        raise ValueError(f"未知の荷重の向き {mode!r}")
    # 面法線は単位ベクトルとは限らない(`query.face_fingerprint` の値をそのまま焼いてある)。
    # 単位化しないと大きさ[N]が法線の長さぶんずれる。
    length = math.sqrt(sum(component * component for component in unit))
    if not length > 0.0:
        raise ValueError("荷重の向きが零ベクトルになっている")
    magnitude = float(condition["magnitude"])
    return [component / length * magnitude for component in unit]


def face_key(item: dict) -> tuple:
    """FaceCondition / ShapeRef を「同じ面か」で比べるための鍵。"""
    return (item["body"], int(item["index"]))


def fingerprint_mismatch(doc: "Document", condition: dict) -> Optional[str]:
    """条件が指す面が**今も同じ面か**を照合する。合っていれば `None`、違えば理由の文字列。

    理由は画面がトーストにそのまま出す。**ここで文書を直さない**(条件は操作ログ上に残り、undoで合うようになる)。

    **見るのは面積と法線だけで、重心は見ない**。重心は世界座標なので、添字を変えない `move_body`
    でも不一致になり、位置を詰めるたびに条件が付け直しになる。面積と法線は平行移動で不変で、
    添字の一致も要るため、検出力はほとんど落ちない。
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

    受けるのは `"normal"` / `"-normal"` / 長さ3の数値の並び。世界軸に名前を足さない(`-Z` は
    `[0, 0, -1]` で渡す)。ベクトルは単位化して控える(大きさ[N]は `magnitude` だけが持つ)。
    長さ0・非有限は ValueError。
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
    """配信用のボディ参照。画面が `modify_selection` へそのまま渡すので、`selection.select_body` と同じ形にする。"""
    return {"body": body_id, "kind": "body", "index": None, "provenance": None}


def face_ref(condition: dict) -> dict:
    """配信用の面参照(`selection.select_face` と同じ形)。"""
    return {
        "body": condition["body"],
        "kind": "face",
        "index": int(condition["index"]),
        "provenance": None,
    }


class OptimizeStateCache:
    """`state_payload()` の増分維持(`DocumentView` が1つ持つ)。

    作り直すのは、条件が変わった(`OptimizeSetup.revision`)・条件が指しているボディの形状が
    差し替わった(`is` で判定)・そのボディの可視性か名前が変わった(理由文が名前を含む)・まだ作っていない、のどれかのときだけ。
    定常状態ではOCCTを呼ばない。形状は `id()` ではなく参照ごと控える(解放後のアドレス再利用で偽ヒットしうる)。
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
        """`(形状, (可視性, 名前))` の表が前回と同じか(形状は `is`、残りは `==`)。"""
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


def state_payload(
    doc: "Document",
    cache: Optional[OptimizeStateCache] = None,
    *,
    previewing: bool = False,
) -> dict:
    """`state_meta()["optimize"]` に載せる形。**画面側が読む契約はこれ。**

        {
          "preserve_bodies": [BodyRef, ...],   # {"body","kind":"body","index":null,"provenance":null}
          "exclude_bodies":  [BodyRef, ...],
          "fixtures":        [FaceRef, ...],   # {"body","kind":"face","index":int,"provenance":null}
          "loads":           [FaceRef, ...],
          "load_specs": [{"body": BodyId, "index": int,     # 面ごとの大きさ[N]と向き
                          "magnitude": float,
                          "direction": "normal"|"-normal"|"vector",
                          "vector": [x,y,z] | null}, ...],  # 荷重面が0枚なら空
          "stale": [{"role": "preserve_bodies"|"exclude_bodies"|"fixtures"|"loads",
                     "body": BodyId, "index": int|null,
                     "code": "fingerprint"|"missing"|"hidden", "reason": str}, ...],
          "revision": int,
        }

    **4つの配列はそのまま `modify_selection` へ渡せる**(画面がプリセレクトに使う)ので、**いま指せない
    ものは載らない**(不可視・消えたボディを渡すと例外になる)。載らなかったものは全部 `stale` に
    理由つきで出るので、黙って消える経路は無い。

    **`load_specs` は文書が持つ how であって「いま荷重がかかっているか」ではない。** 荷重面のボディを
    隠すと `loads` は空になるが `load_specs` は残る(表示に戻したとき打ち直しにならないよう)。かかる面は
    `loads` を見る。両者は長さが違いうるので、画面は `(body, index)` で突き合わせること。

    **`stale` は「割り当てが消えた」ではない。** 文書からは何も消えておらず、形を戻せば・表示に戻せば
    次の配信でまた載る。**画面がトーストに出すかどうかは文言ではなく `code` で決めること**:

    - `"fingerprint"` … 後から形を変えて、添字が別の面を指すようになった(**本来の狙い**)
    - `"missing"`     … 対象のボディが文書に無い(undoで作成ごと消えている)
    - `"hidden"`      … 対象のボディが非表示。表示に戻せばそのまま使われる。人が部品を隠す
      たびに出るので、**出さない側である**

    `previewing` の間は作り直さない(プレビューは文書を投機的に書き換えるので、偽の `stale` が出る。
    `DocumentView._prune_body_caches` と同じ規律)。

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
        # `live["loads"]` ではなく文書の側を見る(上の docstring の `load_specs`)。
        "load_specs": [
            {
                "body": condition["body"],
                "index": int(condition["index"]),
                "magnitude": float(condition["magnitude"]),
                "direction": condition["direction"],
                "vector": None if condition.get("vector") is None else list(condition["vector"]),
            }
            for condition in setup.loads
        ],
        "stale": stale,
        "revision": setup.revision,
    }
    if cache is not None:
        return cache.store(setup.revision, referenced, payload)
    return payload


def _body_trouble(doc: "Document", body_id: str) -> Optional[tuple]:
    """そのボディが**いま指せるか**。指せれば `None`、指せなければ `(code, 理由)`。指紋を測る前に落とす。"""
    try:
        body = doc.get_body(body_id)
    except KeyError:
        return ("missing", f"ボディ {body_id} が文書に無い")
    if not body.visible:
        return ("hidden", f"ボディ {body.name} が非表示(表示に戻せばまた使われる)")
    return None


def _referenced_state(doc: "Document", setup: OptimizeSetup) -> dict:
    """条件が指しているボディの `(形状, (可視性, 名前))` だけを集める。消えたボディは `(None, (False, None))`。

    可視性と名前は形状の同一性を動かさないので、形だけを鍵にすると取りこぼす(名前は非表示の理由文に入る)。
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
        return (None, (False, None))
    return (body.shape, (body.visible, body.name))

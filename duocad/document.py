"""文書とボディのデータモデル。

- `Document.units` は "mm" 固定で変更できない(セッターを持たないプロパティ)。
- `Body.kind` は "solid"(編集対象)か "reference"(読み込んだSTEP等・読み取り専用)。

存在しないBodyIdを渡された `remove_body` / `set_active_body` / `is_editable` は KeyError、
ID重複の `add_body` は ValueError で落とす(黙って無視すると、取り消しや再生の空振りが
静かに文書を壊すため)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

BodyId = str

_VALID_KINDS = {"solid", "reference"}

#: 存在しないIDのエラーに並べる有効IDの上限。超えたぶんは件数だけ言って列挙ツールへ送る
#: (エラー文がスケッチ1枚ぶんの図形IDで埋まると、AIの文脈をそれだけで食う)。
_MAX_LISTED_IDS = 20


def unknown_id_message(
    kind: str, missing: Any, known: Any, *, where: str = ""
) -> str:
    """存在しないIDのエラー文。**有効なIDか、その出し方を必ず添える**。

    `no such entity: 'c1'` のように「違う」とだけ言うエラーは行き止まりで、次の一手が
    書いていない。AIはIDが分からないとき「尋ねる」ではなく**「推測する」に倒れる**ので
    (実測: ローカルLLMが「ID形式を推測して試します」と述べて存在しない図形IDを捏造した)、
    弾くだけでは同じ推測を繰り返される。**弾いたら、その場で正解の集合を見せる。**

    並び順は与えられた順のままにする —— 図形IDは作った順に並んでおり、ソートするとその
    手がかりが消える(IDの文字列自体には意味が無い)。
    """
    ids = list(known)
    scope = f" in {where}" if where else ""
    if not ids:
        listed = "none exist yet"
    elif len(ids) <= _MAX_LISTED_IDS:
        listed = repr(ids)
    else:
        listed = (
            f"{ids[:_MAX_LISTED_IDS]!r} and {len(ids) - _MAX_LISTED_IDS} more"
            " (describe_document lists them all)"
        )
    return f"no such {kind}: {missing!r}{scope} (existing: {listed})"


def new_body_id() -> BodyId:
    """新しいBodyId(**再生時は記録済みのIDをそのまま使う**のでここは通らない)。

    **BodyId の持ち主はこのモジュール**。verb側に置くと `io_step` が verbs/ を引き層が逆になる。
    """
    return f"body-{uuid4().hex[:12]}"


@dataclass
class Body:
    """1つのボディ。`kind="reference"`(読み込んだSTEP等)は編集操作の対象にならない。

    `visible` は**選択機構の一部**(見えないものは指せない)で、選択と同じく
    **操作ログに載らず保存もされない** —— 切り替えは `duocad.selection.set_visible`。
    """

    id: BodyId
    name: str
    kind: str
    shape: Any = None
    visible: bool = True

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)!r}, got {self.kind!r}")


class Document:
    """文書。units="mm"固定、bodiesはリスト、logは操作ログ(唯一の正)。"""

    def __init__(self) -> None:
        self._units = "mm"
        self.bodies: list[Body] = []
        # 型は duocad.sketch.Sketch だが、document.py -> sketch.py の依存を作らないため
        # 注釈は付けない(sketch.py が document.py を import する一方向を保つ)。
        self.sketches: list = []
        # 名前つきの寸法値。図形は値そのものではなくこの名前への参照を持つ。
        # 書き換えは `duocad.parameters.set_parameter` を通す(直接代入すると操作ログに
        # 載らず、形状の再生と食い違う)。
        self.parameters: dict = {}
        self._active_body_id: Optional[BodyId] = None
        # 遅延import: document.py -> oplog.py の一方向依存に留め、循環importを避ける。
        from .oplog import OpLog

        self.log = OpLog()
        # 遅延import の理由は OpLog と同じ(selection.py が document.py を import する一方向)。
        # **保存されない** — 保存の経路は `self.log.to_json()` だけで、選択はログに載らない。
        from .selection import Selection

        self.selection = Selection()

    @property
    def units(self) -> str:
        """常に "mm"。セッターを持たないため代入は AttributeError になる。"""
        return self._units

    def add_body(self, body: Body) -> BodyId:
        """bodiesへ追加し、追加したボディを常にactiveにして body.id を返す。

        同じIDのボディが既にある場合は ValueError(黙って上書き・重複させない)。
        """
        if self._find(body.id) is not None:
            raise ValueError(f"body id already exists: {body.id!r}")
        self.bodies.append(body)
        self._active_body_id = body.id
        return body.id

    def remove_body(self, body_id: BodyId) -> None:
        """指定IDのボディをbodiesから取り除く。activeだった場合はactiveをNoneに戻す。

        存在しないIDは KeyError(取り消しや再生が空振りしたことを黙って見逃さない)。
        """
        body = self._find(body_id)
        if body is None:
            raise KeyError(f"no such body: {body_id!r}")
        self.bodies.remove(body)
        if self._active_body_id == body_id:
            self._active_body_id = None

    def active_body(self) -> Optional[Body]:
        if self._active_body_id is None:
            return None
        return self._find(self._active_body_id)

    def set_active_body(self, body_id: BodyId) -> None:
        if self._find(body_id) is None:
            raise KeyError(f"no such body: {body_id!r}")
        self._active_body_id = body_id

    def is_editable(self, body_id: BodyId) -> bool:
        body = self._find(body_id)
        if body is None:
            raise KeyError(f"no such body: {body_id!r}")
        return body.kind == "solid"

    def get_body(self, body_id: BodyId) -> Body:
        """IDでボディを引く。**無ければ KeyError**(黙ってNoneを返さない)。

        呼び出し側がそれぞれ `next(...)` を書き直すと「引けなかったときにどうなるか」が
        呼び出し元ごとに変わる(`StopIteration` と `KeyError` が混ざる)ので、ここへ一本化する。
        """
        body = self._find(body_id)
        if body is None:
            raise KeyError(
                unknown_id_message(
                    "body",
                    body_id,
                    [b.id for b in self.bodies if b.kind != "reference"],
                )
            )
        return body

    def _find(self, body_id: BodyId) -> Optional[Body]:
        """IDでボディを引く(無ければNone)。存在検査に使う内部用。"""
        for body in self.bodies:
            if body.id == body_id:
                return body
        return None

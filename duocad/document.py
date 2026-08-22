"""文書とボディのデータモデル(実装計画書 §3.1 / §3.2)。

契約:
- Document.units は "mm" 固定で変更できない(セッターを持たないプロパティ)。
- Document.bodies は常にリストで持つ(v1のUIは1ボディしか見せないが、データモデルは複数対応。
  参照STEPを読み込んだ時点で既に複数ボディになりうるため、day1でリストにしておく)。
- Body.kind は "solid"(編集対象) か "reference"(読み込んだSTEP等・読み取り専用)のいずれか。

存在しないBodyIdを渡された `remove_body` / `set_active_body` /
`is_editable` は KeyError、ID重複の `add_body` は ValueError で落とす
(黙って無視すると、取り消しや再生の空振りが静かに文書を壊すため。§6予防6)。
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
    """存在しないIDのエラー文。**有効なIDか、その出し方を必ず添える**(2026-08-22)。

    `no such entity: 'c1'` のように「違う」とだけ言うエラーは行き止まりで、次の一手が
    書いていない。AIはIDが分からないとき「尋ねる」ではなく**「推測する」に倒れる**ので
    (実測: ローカルLLMが「ID形式を推測して試します」と述べて存在しない図形IDを捏造した)、
    弾くだけでは同じ推測を繰り返される。**弾いたら、その場で正解の集合を見せる。**

    形は `mcp_server._find_body` が以前から使っていた `(editable bodies: [...])` に合わせて
    あり、ここはそれを他のID系エラー(スケッチ・図形・パラメータ・ボディ)へ広げたもの。
    並び順は与えられた順のままにする —— 図形IDは作った順に並んでおり、
    ソートするとその手がかりが消える(IDの文字列自体には意味が無い)。
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
    """新しいBodyId。**再生時は記録済みのIDをそのまま使う**のでここは通らない。

    **BodyId の持ち主はこのモジュール**であり、verb側に置くと `io_step` が verbs/ を引く
    ことになり層が逆になる。
    """
    return f"body-{uuid4().hex[:12]}"


@dataclass
class Body:
    """1つのボディ。

    kind="solid"   : 編集操作(verb)の対象になる
    kind="reference": import_step等で読み込んだ読み取り専用ボディ。編集操作の対象にならない
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
    """文書。units="mm"固定、bodiesはリスト、logは操作ログ(唯一の正)。

    契約:
        add_body(body)         -> BodyId   追加し、追加したボディを常にactiveにする
        remove_body(body_id)   -> None     bodiesから取り除く。activeだったら解除する
        active_body()          -> Body | None
        set_active_body(id)    -> None
        is_editable(body_id)   -> bool     kind=="solid" のときだけ True
    """

    def __init__(self) -> None:
        self._units = "mm"
        self.bodies: list[Body] = []
        # M2追加。実装計画書§3.1の文書は bodies と並んで sketches を持つ。
        # 型は duocad.sketch.Sketch だが、document.py -> sketch.py の依存を作らないため
        # 注釈は付けない(sketch.py が document.py を import する一方向を保つ)。
        self.sketches: list = []
        # M3追加。実装計画書§3.1の `parameters : { name: number }`(名前つきの寸法値)。
        # 図形は値そのものではなくこの名前への参照を持つ(§6予防5)。
        # 書き換えは `duocad.parameters.set_parameter` を通す(直接代入すると
        # 操作ログに載らず、形状の再生と食い違う)。
        self.parameters: dict = {}
        self._active_body_id: Optional[BodyId] = None
        # 遅延import: document.py -> oplog.py の一方向依存に留め、循環importを避ける。
        from .oplog import OpLog

        self.log = OpLog()
        # A1追加。実装計画書§2の図で、文書・操作ログと**並んで**コアに置かれているもの。
        # アプリ層に持たせると交互操作(AIが提案 → 人が直す → AIが適用)が原理的に閉じない。
        # 遅延import の理由は OpLog と同じ(selection.py が document.py を import する一方向を保つ)。
        # **保存されない** — 保存の経路は `self.log.to_json()` だけで、選択はログに載らない
        # (選択の変更は文書を変更しないので操作ログに積まない。duocad/selection.py 参照)。
        from .selection import Selection

        self.selection = Selection()

    @property
    def units(self) -> str:
        """常に "mm"。セッターを持たないため代入は AttributeError になる。"""
        return self._units

    def add_body(self, body: Body) -> BodyId:
        """bodiesへ追加し、追加したボディを常にactiveにして body.id を返す。

        v1のUIは単一フォーカスなので、最新の操作対象を自動でactiveにする。
        同じIDのボディが既にある場合は ValueError(黙って上書き・重複させない。§6予防6)。
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
            # 取り除いたボディを指したまま参照が残るのを防ぐ
            self._active_body_id = None

    def active_body(self) -> Optional[Body]:
        """現在アクティブなボディを返す(無ければNone)。"""
        if self._active_body_id is None:
            return None
        return self._find(self._active_body_id)

    def set_active_body(self, body_id: BodyId) -> None:
        """アクティブボディを切り替える。存在しないIDならKeyErrorを送出する。"""
        if self._find(body_id) is None:
            raise KeyError(f"no such body: {body_id!r}")
        self._active_body_id = body_id

    def is_editable(self, body_id: BodyId) -> bool:
        """kind=="solid" のボディのみ True。参照ボディ(kind="reference")は編集操作の対象にならない。"""
        body = self._find(body_id)
        if body is None:
            raise KeyError(f"no such body: {body_id!r}")
        return body.kind == "solid"

    def get_body(self, body_id: BodyId) -> Body:
        """IDでボディを引く。**無ければ KeyError**(黙ってNoneを返さない)。

        呼び出し側(`server.py` / `mcp_server.py` / `verbs/visibility.py` / 各verb)がそれぞれ
        `next(candidate for ... if candidate.id == body_id)` を実装し直すと、「引けなかった
        ときにどうなるか」が呼び出し元ごとに違ってしまう —— `StopIteration` を投げるものと
        `KeyError` を投げるものが混ざるのが本当の害なので、ここに一本化して揃える。
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

"""文書とボディのデータモデル(実装計画書 §3.1 / §3.2)。

M0の契約:
- Document.units は "mm" 固定で変更できない(セッターを持たないプロパティ)。
- Document.bodies は常にリストで持つ(v1のUIは1ボディしか見せないが、データモデルは複数対応。
  参照STEPを読み込んだ時点で既に複数ボディになりうるため、day1でリストにしておく)。
- Body.kind は "solid"(編集対象) か "reference"(読み込んだSTEP等・読み取り専用)のいずれか。

このモジュールのメソッド本体(振る舞い)はスタブ(NotImplementedError)。
データの入れ物としての Body / Document の生成そのものは実装済みで、M0コア実装タスクの対象外。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

BodyId = str

_VALID_KINDS = {"solid", "reference"}


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

    契約(次タスクが実装する振る舞い):
        add_body(body)         -> BodyId   追加し、追加したボディを常にactiveにする
        remove_body(body_id)   -> None     bodiesから取り除く。activeだったら解除する
        active_body()          -> Body | None
        set_active_body(id)    -> None
        is_editable(body_id)   -> bool     kind=="solid" のときだけ True
    """

    def __init__(self) -> None:
        self._units = "mm"
        self.bodies: list[Body] = []
        self._active_body_id: Optional[BodyId] = None
        # 遅延import: document.py -> oplog.py の一方向依存に留め、循環importを避ける。
        from .oplog import OpLog

        self.log = OpLog()

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

    def _find(self, body_id: BodyId) -> Optional[Body]:
        """IDでボディを引く(無ければNone)。"""
        for body in self.bodies:
            if body.id == body_id:
                return body
        return None

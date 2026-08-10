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
        """bodiesへ追加し、追加したボディを常にactiveにして body.id を返す。"""
        raise NotImplementedError

    def remove_body(self, body_id: BodyId) -> None:
        """指定IDのボディをbodiesから取り除く。activeだった場合はactiveをNoneに戻す。"""
        raise NotImplementedError

    def active_body(self) -> Optional[Body]:
        """現在アクティブなボディを返す(無ければNone)。"""
        raise NotImplementedError

    def set_active_body(self, body_id: BodyId) -> None:
        """アクティブボディを切り替える。存在しないIDならKeyErrorを送出する。"""
        raise NotImplementedError

    def is_editable(self, body_id: BodyId) -> bool:
        """kind=="solid" のボディのみ True。参照ボディ(kind="reference")は編集操作の対象にならない。"""
        raise NotImplementedError

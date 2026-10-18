"""文書とボディのデータモデル。

- `Document.units` は "mm" 固定で変更できない(セッターを持たないプロパティ)。
- `Body.kind` は "solid"(B-rep・編集対象)と "mesh"(三角形メッシュ・**表示とSTL書き出しだけ**)の2種。
- **形の持ち方はkindごとに1本だけ**: "solid" は `shape`(TopoDS_Shape)、"mesh" は `mesh`(`Mesh`)。
  両方を持つBodyは作れない(`__post_init__` が弾く)。混ぜると `body.shape is None` で分岐している
  既存の経路が判定できなくなる。

存在しないBodyIdを渡された `remove_body` / `remove_added_body` / `set_active_body` / `is_editable` は KeyError、
ID重複の `add_body` は ValueError(黙って無視すると取り消しや再生の空振りが文書を静かに壊す)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

BodyId = str

#: B-rep(編集できる)
SOLID_KIND = "solid"
#: 三角形メッシュ(**表示とSTL書き出しだけ**。編集verbは拒否する)
MESH_KIND = "mesh"

_VALID_KINDS = {SOLID_KIND, MESH_KIND}

#: 存在しないIDのエラーに並べる有効IDの上限。超えたぶんは件数だけ言って列挙ツールへ送る。
_MAX_LISTED_IDS = 20


def unknown_id_message(
    kind: str, missing: Any, known: Any, *, where: str = ""
) -> str:
    """存在しないIDのエラー文。**有効なIDか、その出し方を必ず添える**。

    AIはIDが分からないとき「尋ねる」ではなく「推測する」に倒れる(実測: ローカルLLMが
    「ID形式を推測して試します」と述べて存在しない図形IDを捏造した)ので、弾いたらその場で
    正解の集合を見せる。並び順は与えられた順のまま(図形IDは作った順で、ソートすると手がかりが消える)。
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
    """新しいBodyId(再生時は記録済みのIDを使うのでここは通らない)。持ち主がこのモジュールなのは、verb側だと `io_step` が verbs/ を引き層が逆になるため。"""
    return f"body-{uuid4().hex[:12]}"


@dataclass(frozen=True)
class Mesh:
    """三角形メッシュ(頂点＋三角形)。**不変**。

    `positions` は x,y,z を平らに並べた頂点座標[mm]、`indices` は頂点添字を3つずつ並べた三角形
    (**0始まり**。`kernel.tessellate` の並べ方と同じ)。タプルなので `Body` を控えて戻すだけの
    巻き戻し(`duocad.speculative`)が効く —— **書き換える経路を作らないこと**。
    検査を通った三角形群であることを型で示す(生のリストだと範囲外の添字が画面まで届く)。
    """

    positions: tuple
    indices: tuple

    def __post_init__(self) -> None:
        if len(self.positions) % 3 != 0:
            raise ValueError(
                f"positions must be a flat x,y,z list (length divisible by 3), got {len(self.positions)}"
            )
        if len(self.indices) % 3 != 0:
            raise ValueError(
                f"indices must be a flat triangle list (length divisible by 3), got {len(self.indices)}"
            )
        if not self.indices:
            raise ValueError("mesh has no triangle: 三角形が0枚のメッシュボディは作らない")
        limit = len(self.positions) // 3
        for index in self.indices:
            if not (0 <= index < limit):
                raise ValueError(
                    f"triangle index out of range: {index} (vertices: {limit})"
                )

    @property
    def vertex_count(self) -> int:
        return len(self.positions) // 3

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3


def make_mesh(positions, indices) -> Mesh:
    """素の並びから `Mesh` を作る。`positions` は float、`indices` は int へ落とす(numpyスカラーの表現の違いがログへ漏れないように)。"""
    return Mesh(
        positions=tuple(float(value) for value in positions),
        indices=tuple(int(value) for value in indices),
    )


@dataclass
class Body:
    """1つのボディ。`visible` は選択機構の一部で、操作ログに載らず保存もされない(切り替えは `duocad.selection.set_visible`)。"""

    id: BodyId
    name: str
    kind: str
    shape: Any = None
    visible: bool = True
    #: `kind="mesh"` のときだけ入る三角形メッシュ。B-repボディでは常に None。
    mesh: Optional[Mesh] = None

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)!r}, got {self.kind!r}")
        if self.kind == MESH_KIND:
            if not isinstance(self.mesh, Mesh):
                raise ValueError(f"kind={MESH_KIND!r} needs a Mesh in `mesh`, got {self.mesh!r}")
            if self.shape is not None:
                raise ValueError(
                    f"kind={MESH_KIND!r} must not carry a B-rep shape "
                    "(形の持ち方はkindごとに1本だけ)"
                )
        elif self.mesh is not None:
            raise ValueError(
                f"kind={self.kind!r} must not carry a mesh (`mesh` は kind={MESH_KIND!r} 専用)"
            )


class Document:
    """文書。units="mm"固定、bodiesはリスト、logは操作ログ(唯一の正)。"""

    def __init__(self) -> None:
        self._units = "mm"
        # STEPは1回の読み込みで複数ソリッドを生むので、「文書=1ボディ」を前提にしない。
        self.bodies: list[Body] = []
        # 型は duocad.sketch.Sketch(sketch.py が document.py を import する一方向を保つため注釈なし)。
        self.sketches: list = []
        # 名前つきの寸法値。書き換えは `duocad.parameters.set_parameter` を通す(直接代入は操作ログに載らない)。
        self.parameters: dict = {}
        self._active_body_id: Optional[BodyId] = None
        #: ボディ -> それを `add_body` する直前の active(`remove_added_body` が戻す先)。保存されない。
        self._active_before_add: dict[BodyId, Optional[BodyId]] = {}
        # 遅延import: 循環importを避ける(oplog / selection / optimize は document.py を読む側)。
        from .oplog import OpLog

        self.log = OpLog()
        # 保存されない(保存の経路は `self.log.to_json()` だけ)。
        from .selection import Selection

        self.selection = Selection()
        # 形状最適化の条件。選択と違って保存される(書き込む口は `duocad/verbs/optimize_setup.py` の4 verb。実体は操作ログ)。
        from .optimize import OptimizeSetup

        self.optimize = OptimizeSetup()
        #: bbox のメモ。この文書と寿命を共にする(`Session._replace_document` が新しい `Document` を作る)。保存されない。
        from .kernel import BoundingBoxCache

        self.bbox_cache = BoundingBoxCache()

    @property
    def units(self) -> str:
        """常に "mm"。セッターを持たないため代入は AttributeError になる。"""
        return self._units

    def add_body(self, body: Body, index: Optional[int] = None) -> BodyId:
        """bodiesへ追加し、追加したボディを常にactiveにして body.id を返す。

        同じIDのボディが既にある場合は ValueError(黙って上書き・重複させない)。

        `index` を渡すとその位置へ差し込む(既定は末尾)。使うのは削除のundoだけ(パーツ一覧の順序を元へ戻す)。
        その場合は active も呼び出し側が戻す(`_common.reattach_bodies`)ので、足す直前の active は控えない。
        """
        if self._find(body.id) is not None:
            raise ValueError(f"body id already exists: {body.id!r}")
        if index is None:
            self.bodies.append(body)
            self._active_before_add[body.id] = self._active_body_id
        else:
            self.bodies.insert(int(index), body)
        self._active_body_id = body.id
        return body.id

    def remove_body(self, body_id: BodyId) -> None:
        """指定IDのボディをbodiesから取り除く。activeだった場合はactiveをNoneに戻す。

        存在しないIDは KeyError。ボディを足した操作の undo は `remove_added_body`。
        """
        body = self.get_body(body_id)
        self.bodies.remove(body)
        if self._active_body_id == body_id:
            self._active_body_id = None

    def remove_added_body(self, body_id: BodyId) -> None:
        """`add_body` した操作の undo: ボディを取り除き、それが active だったなら足す直前の active へ戻す。

        `remove_body` だけだと active が None になり、undo した後の join / cut の行き先が、同じログを
        再生した文書(足す前の active のまま)と食い違う。直前の active が既にいなければ None のまま。
        1回の操作が複数のボディを足したときは、**足した順の逆に**呼ぶ(最後に足したものから戻す)。
        """
        was_active = self._active_body_id == body_id
        before = self._active_before_add.get(body_id)
        self.remove_body(body_id)
        if was_active and before is not None and self._find(before) is not None:
            self._active_body_id = before

    def active_body(self) -> Optional[Body]:
        if self._active_body_id is None:
            return None
        return self._find(self._active_body_id)

    def set_active_body(self, body_id: BodyId) -> None:
        self.get_body(body_id)
        self._active_body_id = body_id

    def is_editable(self, body_id: BodyId) -> bool:
        """編集verbを掛けてよいボディか。**"mesh" は False**(拒否の入口は
        `duocad.verbs._common.editable_body`。理由は `duocad/meshbody.py` のdocstring)。"""
        return self.get_body(body_id).kind == SOLID_KIND

    def get_body(self, body_id: BodyId) -> Body:
        """IDでボディを引く。**無ければ KeyError**(呼び出し元ごとに `StopIteration` と混ざらないよう一本化)。"""
        body = self._find(body_id)
        if body is None:
            raise KeyError(
                unknown_id_message("body", body_id, [b.id for b in self.bodies])
            )
        return body

    def _find(self, body_id: BodyId) -> Optional[Body]:
        """IDでボディを引く(無ければNone)。"""
        for body in self.bodies:
            if body.id == body_id:
                return body
        return None

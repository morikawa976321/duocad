"""文書とボディのデータモデル。

- `Document.units` は "mm" 固定で変更できない(セッターを持たないプロパティ)。
- `Body.kind` は "solid"(B-rep・編集対象)と "mesh"(三角形メッシュ・**表示とSTL書き出しだけ**)の2種。
- **形の持ち方はkindごとに1本だけ**: "solid" は `shape`(TopoDS_Shape)、"mesh" は `mesh`(`Mesh`)。
  両方を同時に持つBodyは作れない(`__post_init__` が弾く)。**混ぜられるようにすると、
  `body.shape is None` で分岐している既存の経路が「どちらの意味か」を判定できなくなる。**

存在しないBodyIdを渡された `remove_body` / `set_active_body` / `is_editable` は KeyError、
ID重複の `add_body` は ValueError で落とす(黙って無視すると、取り消しや再生の空振りが
静かに文書を壊すため)。
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


@dataclass(frozen=True)
class Mesh:
    """三角形メッシュ(頂点＋三角形)。**不変**。

    `positions` は x,y,z を平らに並べた頂点座標[mm]、`indices` は頂点添字を3つずつ並べた
    三角形(**0始まり**。`kernel.tessellate` が面ごとに返すのと同じ並べ方)。
    どちらもタプルなので、`Body` を控えて戻すだけの巻き戻し(`duocad.speculative`)が
    そのまま効く —— **書き換える経路を作らないこと**(編集verbを拒否する設計と対)。

    `dict`/`list` ではなくこの型にしてあるのは、**検査を通った三角形群であることを型で示す**
    ため。生のリストを持ち回すと、範囲外の添字や3の倍数でない長さが画面まで届く。
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
    """素の並び(list / tuple / 数値)から `Mesh` を作る。**値の型もここで確定させる**。

    `positions` は float、`indices` は int へ落とす —— JSONから戻ってきた値・numpyの
    スカラーがそのまま入ると、保存のたびに `1.0` と `np.float64(1.0)` のような
    表現の違いがログへ漏れる。
    """
    return Mesh(
        positions=tuple(float(value) for value in positions),
        indices=tuple(int(value) for value in indices),
    )


@dataclass
class Body:
    """1つのボディ。

    `visible` は**選択機構の一部**(見えないものは指せない)で、選択と同じく
    **操作ログに載らず保存もされない** —— 切り替えは `duocad.selection.set_visible`。
    """

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
        # 常にリスト(Optionalな単一ボディにしない)。STEPは1回の読み込みで複数ソリッドを
        # 普通に生むので、「文書=1ボディ」という前提を持ち込まないこと。
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
        # 形状最適化(第3のモード)の条件。**選択と違って保存される** —— 書き込む口は
        # `duocad/verbs/optimize_setup.py` の4 verb だけで、実体は操作ログに載る
        # (ここにあるのはその再生結果)。遅延import の理由は上の2つと同じ。
        from .optimize import OptimizeSetup

        self.optimize = OptimizeSetup()
        #: bbox のメモ。**この文書と寿命を共にする** —— 文書を差し替えると
        #: `Session._replace_document` が新しい `Document` を作るので一緒に捨てられる
        #: (`view.DocumentView.reset()` と同じ規律)。**保存されない**。
        from .kernel import BoundingBoxCache

        self.bbox_cache = BoundingBoxCache()

    @property
    def units(self) -> str:
        """常に "mm"。セッターを持たないため代入は AttributeError になる。"""
        return self._units

    def add_body(self, body: Body, index: Optional[int] = None) -> BodyId:
        """bodiesへ追加し、追加したボディを常にactiveにして body.id を返す。

        同じIDのボディが既にある場合は ValueError(黙って上書き・重複させない)。

        `index` を渡すとその位置へ差し込む(既定は末尾)。**使うのは削除のundoだけ**で、
        消したボディを元の並びへ戻すために要る —— 末尾へ足すだけだと、undoしたのに
        ブラウザのパーツ一覧の順序だけが変わり、「取り消したのに元に戻っていない」
        という説明のつかない差が残る(形は同じなので誰も落とせない)。
        """
        if self._find(body.id) is not None:
            raise ValueError(f"body id already exists: {body.id!r}")
        if index is None:
            self.bodies.append(body)
        else:
            self.bodies.insert(int(index), body)
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
        """編集verbを掛けてよいボディか。**"mesh" は False**(拒否の入口は
        `duocad.verbs._common.editable_body`。理由は `duocad/meshbody.py` のdocstring)。"""
        body = self._find(body_id)
        if body is None:
            raise KeyError(f"no such body: {body_id!r}")
        return body.kind == SOLID_KIND

    def get_body(self, body_id: BodyId) -> Body:
        """IDでボディを引く。**無ければ KeyError**(黙ってNoneを返さない)。

        呼び出し側がそれぞれ `next(...)` を書き直すと「引けなかったときにどうなるか」が
        呼び出し元ごとに変わる(`StopIteration` と `KeyError` が混ざる)ので、ここへ一本化する。
        """
        body = self._find(body_id)
        if body is None:
            raise KeyError(
                unknown_id_message("body", body_id, [b.id for b in self.bodies])
            )
        return body

    def _find(self, body_id: BodyId) -> Optional[Body]:
        """IDでボディを引く(無ければNone)。存在検査に使う内部用。"""
        for body in self.bodies:
            if body.id == body_id:
                return body
        return None

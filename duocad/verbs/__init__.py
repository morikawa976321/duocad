"""verb層 — 1 verb 1モジュール(実装計画書§2「verbの粒度」)。

このパッケージを import すると、含まれる全verbが `oplog` のレジストリへ登録される
(登録はverbを定義したモジュール側が `register_verb()` で行う。`oplog.py` に verb名の
if 連鎖を置かないための設計)。`duocad/__init__.py` がこのパッケージを import するので、
`duocad` を使えば必ずレジストリが埋まっている — 保存したログを `from_json` して
`replay` するだけの経路でverbが未登録にならないようにするため。

**OCP / build123d を直接importしない**(tests/test_boundaries.py が再帰的に検査する)。
幾何は `duocad.kernel` 越しに呼ぶ。verbが増えたらここに1行足す。
"""
from .extrude import extrude  # noqa: F401
from .extrude_face import extrude_face  # noqa: F401
from .make_editable_copy import make_editable_copy  # noqa: F401

__all__ = ["extrude", "extrude_face", "make_editable_copy"]

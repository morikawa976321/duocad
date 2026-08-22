"""verb層 — 1 verb 1モジュール。

このパッケージを import すると、含まれる全verbが `oplog` のレジストリへ登録される
(登録はverb側の `register_verb()`)。verbが増えたらここに1行足す。

幾何は `duocad.kernel` 越しに呼ぶ — **OCP / build123d を直接importしない**。
"""
from .chamfer import chamfer  # noqa: F401
from .extrude import extrude  # noqa: F401
from .extrude_face import extrude_face  # noqa: F401
from .fillet import fillet  # noqa: F401
from .import_editable import import_step_editable  # noqa: F401
from .make_editable_copy import make_editable_copy  # noqa: F401
from .pattern import pattern_circular, pattern_rectangular  # noqa: F401
from .visibility import set_visible  # noqa: F401

__all__ = [
    "chamfer",
    "extrude",
    "extrude_face",
    "fillet",
    "import_step_editable",
    "make_editable_copy",
    "pattern_circular",
    "pattern_rectangular",
    "set_visible",
]

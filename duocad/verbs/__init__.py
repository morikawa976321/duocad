"""verb層 — 1 verb 1モジュール。

このパッケージを import すると、含まれる全verbが `oplog` のレジストリへ登録される
(登録はverb側の `register_verb()`)。verbが増えたらここに1行足す。

幾何は `duocad.kernel` 越しに呼ぶ — **OCP / build123d を直接importしない**。
"""
from .chamfer import chamfer  # noqa: F401
from .delete import delete  # noqa: F401
from .extrude import extrude  # noqa: F401
from .extrude_face import extrude_face  # noqa: F401
from .fillet import fillet  # noqa: F401
from .import_editable import import_step  # noqa: F401
from .import_svg import import_svg  # noqa: F401
from .move_body import move_body  # noqa: F401
from .move_sketch import move_sketch_entities  # noqa: F401
from .optimize_setup import (  # noqa: F401
    set_exclude_bodies,
    set_fixtures,
    set_loads,
    set_preserve_bodies,
)
from .pattern import pattern_circular, pattern_rectangular  # noqa: F401

__all__ = [
    "chamfer",
    "delete",
    "extrude",
    "extrude_face",
    "fillet",
    "import_step",
    "import_svg",
    "move_body",
    "move_sketch_entities",
    "set_exclude_bodies",
    "set_fixtures",
    "set_loads",
    "set_preserve_bodies",
    "pattern_circular",
    "pattern_rectangular",
]

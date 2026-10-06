"""verb層 — 1 verb 1モジュール。

このパッケージを import すると、含まれる全verbが `oplog` のレジストリへ登録される
(登録はverb側の `register_verb()`)。verbが増えたらここに1行足す。

幾何は `duocad.kernel` 越しに呼ぶ — **OCP / build123d を直接importしない**。
"""
from .add_mesh import add_mesh_body  # noqa: F401
from .add_solid import add_solid_body  # noqa: F401
from .boolean_body import boolean_body  # noqa: F401
from .chamfer import chamfer  # noqa: F401
from .delete import delete, delete_items  # noqa: F401
from .envelope_regions import envelope_regions  # noqa: F401
from .extrude import extrude, extrude_faces  # noqa: F401
from . import extrude_face as _extrude_face_replay  # noqa: F401  再生専用verbの登録
from .fillet import fillet  # noqa: F401
from .import_editable import import_step  # noqa: F401
from .import_svg import import_svg  # noqa: F401
from .mirror_body import mirror_body  # noqa: F401
from .mirror_sketch import mirror_sketch_entities  # noqa: F401
from .move_body import move_body  # noqa: F401
from .move_sketch import move_sketch_entities  # noqa: F401
from .optimize_setup import (  # noqa: F401
    clear_loads,
    set_exclude_bodies,
    set_fixtures,
    set_loads,
    set_preserve_bodies,
)
from .pattern import pattern_circular, pattern_rectangular  # noqa: F401
from .rename import rename  # noqa: F401

__all__ = [
    "add_mesh_body",
    "add_solid_body",
    "boolean_body",
    "chamfer",
    "delete",
    "delete_items",
    "envelope_regions",
    "extrude",
    "extrude_faces",
    "fillet",
    "import_step",
    "import_svg",
    "clear_loads",
    "mirror_body",
    "mirror_sketch_entities",
    "move_body",
    "move_sketch_entities",
    "set_exclude_bodies",
    "set_fixtures",
    "set_loads",
    "set_preserve_bodies",
    "pattern_circular",
    "pattern_rectangular",
    "rename",
]

"""古い版の保存ファイルを今の版の操作へ書き換える(`OpLog.from_payload` が読む前に通す)。

版ごとの差は図形の `params` のキーと値だけで、操作の並び・ID・`d<n>` の振り順は変えない。

- **version 2 → 3**: 円の `radius` を `diameter`(値は2倍)へ。キーの位置は変えない(`register_all` は
  キーの順に `d<n>` を振るので、位置を動かすと以後の名前がずれる)。
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List


def upgrade_operations(version: int, operations: List[Any]) -> List[Any]:
    """`version` の操作の並びを今の版へ写して返す(元の並びは書き換えない)。"""
    upgraded = copy.deepcopy(operations)
    if version <= 2:
        for operation in upgraded:
            if isinstance(operation, dict) and operation.get("verb") == "set_parameter":
                # 名前つきの値がどの図形のどのキーかは再生しないと分からない。円の半径を指していたら値を2倍にする必要があるが
                # 確かめられないので、黙って読まずに断る(`set_parameter` は画面にもMCPにも出していない)。
                raise ValueError(
                    "この保存ファイル(version 2)は set_parameter を含むので円の半径→直径の変換ができない"
                )
            if isinstance(operation, dict):
                _circle_radius_to_diameter(operation.get("args"))
    return upgraded


def _circle_radius_to_diameter(node: Any) -> None:
    """`{"kind": "circle", "params"|"refs": {...}}` を全部探して `radius` を `diameter` へ書き換える。

    図形を記録する操作(`add_circle`・`import_svg`・`envelope_regions`・`delete` の退避)はどれもこの形で持つ。
    `delete_items` の退避だけはスケッチごとに名前つきの値を `values` に別に持つので、そこも2倍にする。
    """
    if isinstance(node, list):
        for item in node:
            _circle_radius_to_diameter(item)
        return
    if not isinstance(node, dict):
        return
    values = node.get("values") if isinstance(node.get("values"), dict) else None
    if node.get("kind") == "circle":
        for key in ("params", "refs"):
            params = node.get(key)
            if isinstance(params, dict) and "radius" in params:
                node[key] = _renamed(params, values)
        # `delete` の退避は名前つきの値を持っていたキーの名前を `named` に並べる。
        if isinstance(node.get("named"), list):
            node["named"] = ["diameter" if key == "radius" else key for key in node["named"]]
    for key, child in node.items():
        if key == "entities" and values is not None and isinstance(child, list):
            for entity in child:
                if isinstance(entity, dict) and entity.get("kind") == "circle" and isinstance(entity.get("refs"), dict):
                    if "radius" in entity["refs"]:
                        entity["refs"] = _renamed(entity["refs"], values)
            continue
        _circle_radius_to_diameter(child)


def _renamed(params: Dict[str, Any], values: Any) -> Dict[str, Any]:
    """`radius` を同じ位置の `diameter` にした写し。値が名前つきの値への参照なら `values` 側を2倍にする。"""
    renamed: Dict[str, Any] = {}
    for key, value in params.items():
        if key != "radius":
            renamed[key] = value
        elif isinstance(value, str):
            if isinstance(values, dict) and value in values:
                values[value] = 2.0 * float(values[value])
            renamed["diameter"] = value
        else:
            renamed["diameter"] = 2.0 * float(value)
    return renamed

"""realworld.py の出力(標準出力のまま)を、断った理由の分類つきの表(Markdown)にする。

    .venv/Scripts/python.exe -m tools.face_edit_probe.realworld <file.step> ... > out.txt
    .venv/Scripts/python.exe -m tools.face_edit_probe.realworld_table out.txt
"""
from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict

KINDS = {
    "delete_blend": "丸めの連なりの削除",
    "delete_hole": "穴の削除",
    "move_free": "平面の移動(接線の隣なし)",
    "move_tangent": "平面の移動(接線の隣あり)",
}
COLUMNS = ["通った", "警告で断る", "丸め追従の対象外", "作れない", "OCCT の例外", "関門で断る", "60秒超", "落ちる"]


def classify(reason: str) -> str:
    if reason == "hang":
        return "60秒超"
    if reason.startswith("crash"):
        return "落ちる"
    if "RemoveFeatures warned" in reason:
        return "警告で断る"
    if reason.startswith(("Standard_", "StdFail_", "BRep", "Geom")) and not reason.startswith("BRepCheck"):
        return "OCCT の例外"
    if "MakeOffset" in reason or "MakerVolume" in reason or "re-blending failed" in reason:
        return "作れない"
    if any(key in reason for key in ("blend", "tangent neighbour", "sphere", "supports", "corner")):
        return "丸め追従の対象外"
    return "関門で断る"


def parse(text: str):
    """(種類, 方式) → {分類: 件数}、部品ごとの通過数。"""
    table = defaultdict(lambda: defaultdict(int))
    line_re = re.compile(r"^\s+(\w+)\s+(\w+)\s+(\d+)/\s*(\d+)\s+median=.*?ms\s+(\{.*\})\s*$")
    for line in text.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        kind, method, ok, total, reasons = m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), ast.literal_eval(m.group(5))
        cell = table[(kind, method)]
        cell["通った"] += ok
        cell["件数"] += total
        for reason, count in reasons.items():
            cell[classify(reason)] += count
    return table


def markdown(table) -> str:
    out = ["| 試行 | 方式 | 件数 | " + " | ".join(COLUMNS) + " |", "|---|---|---:|" + "---:|" * len(COLUMNS)]
    for kind in KINDS:
        for (k, method), cell in sorted(table.items()):
            if k != kind:
                continue
            out.append(f"| {KINDS[kind]} | {method} | {cell['件数']} | " + " | ".join(str(cell.get(c, 0)) for c in COLUMNS) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    print(markdown(parse(open(sys.argv[1], encoding="utf-8").read())))

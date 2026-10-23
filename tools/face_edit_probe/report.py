"""run.py の `--json` の結果を docs/FACE_EDIT.md の判定表(Markdown)にする。

    .venv/Scripts/python.exe -m tools.face_edit_probe.run --json legacy.json
    .venv/Scripts/python.exe -m tools.face_edit_probe.run --strict --json strict.json
    .venv/Scripts/python.exe -m tools.face_edit_probe.report legacy.json strict.json
"""
from __future__ import annotations

import json
import sys

MARK = {"match": "一致", "wrong": "**誤**", "refused": "断", "crash": "**落**", "recorded": "記録"}
DELETE_COLUMNS = ["A", "C", "SA"]
MOVE_COLUMNS = ["B0", "B1", "C", "DB", "SB", "SD"]


def _cell(row):
    if row is None:
        return ""
    mark = MARK[row["verdict"]]
    if row["verdict"] == "refused" and "fail" in row.get("tags", []):
        mark = "断(正)"
    return mark


def table(rows, prefix, methods):
    cases = []
    for r in rows:
        if r["case"].startswith(prefix) and r["case"] not in cases:
            cases.append(r["case"])
    by = {(r["case"], r["method"]): r for r in rows}
    out = ["| ケース | 内容 | " + " | ".join(methods) + " |", "|---|---|" + "---|" * len(methods)]
    for name in cases:
        first = next(r for r in rows if r["case"] == name)
        out.append(f"| `{name}` | {first.get('note', '')} | " + " | ".join(_cell(by.get((name, m))) for m in methods) + " |")
    return "\n".join(out)


def main(paths):
    rows = [row for path in paths for row in json.loads(open(path, encoding="utf-8").read())]
    print(table(rows, "d_", DELETE_COLUMNS))
    print()
    print(table(rows, "m_", MOVE_COLUMNS))


if __name__ == "__main__":
    main(sys.argv[1:])

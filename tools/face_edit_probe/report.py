"""run.py の `--json` の結果を docs/FACE_EDIT.md の表(Markdown)にする。

    .venv/Scripts/python.exe -m tools.face_edit_probe.run --json out.json
    .venv/Scripts/python.exe -m tools.face_edit_probe.report out.json
"""
from __future__ import annotations

import json
import sys

MARK = {"match": "一致", "wrong": "**誤**", "refused": "断", "crash": "**落**", "recorded": "記録"}


def _cell(row, expected_failure):
    mark = MARK[row["verdict"]]
    if row["verdict"] == "refused" and expected_failure:
        mark = "断(正)"
    if row["verdict"] == "refused" and not expected_failure and row.get("note_unknown"):
        mark = "断"
    return f"{mark} {row['ms']:.0f}" if row.get("ms") == row.get("ms") and "ms" in row else mark


def table(rows, methods):
    cases = []
    for r in rows:
        if r["case"] not in cases:
            cases.append(r["case"])
    by = {(r["case"], r["method"]): r for r in rows}
    out = ["| ケース | 内容 | " + " | ".join(methods) + " |", "|---|---|" + "---|" * len(methods)]
    for name in cases:
        first = next(r for r in rows if r["case"] == name)
        expected_failure = "fail" in first.get("tags", [])
        cells = [_cell(by[(name, m)], expected_failure) if (name, m) in by else "" for m in methods]
        out.append(f"| `{name}` | {first.get('note', '')} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def main(path):
    rows = json.loads(open(path, encoding="utf-8").read())
    delete = [r for r in rows if r["case"].startswith("d_")]
    move = [r for r in rows if r["case"].startswith("m_")]
    print("削除(数字は ms)\n")
    print(table(delete, ["A", "C"]))
    print("\n移動(数字は ms)\n")
    print(table(move, ["B0", "B1", "C", "DB", "DC"]))


if __name__ == "__main__":
    main(sys.argv[1])

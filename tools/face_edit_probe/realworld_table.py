"""realworld.py の結果を、断った理由の分類つきの表(Markdown)にする。

    FACE_EDIT_ROWS=rows.jsonl .venv/Scripts/python.exe -m tools.face_edit_probe.realworld <file.step> ...
    .venv/Scripts/python.exe -m tools.face_edit_probe.realworld_table rows.jsonl
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict

KINDS = {
    "delete_blend": "丸めの連なりの削除",
    "delete_hole": "穴の削除",
    "move_free": "平面の移動(接線の隣なし)",
    "move_tangent": "平面の移動(接線の隣あり)",
}
COLUMNS = ["通った", "警告で断る", "丸め追従の対象外", "作れない", "OCCT の例外", "関門で断る", "60秒超", "落ちる"]


def classify(reason) -> str:
    if reason is None:
        return "通った"
    if reason == "hang":
        return "60秒超"
    if reason.startswith("crash"):
        return "落ちる"
    if "RemoveFeatures warned" in reason:
        return "警告で断る"
    if reason.startswith(("Standard_", "StdFail_")):
        return "OCCT の例外"
    if "MakeOffset" in reason or "MakerVolume" in reason or "re-blending failed" in reason:
        return "作れない"
    # 入力が丸め追従の認識規則に当てはまらない(計算する前に断る)ときだけ「対象外」。丸め直した後の検査落ちは関門で断ったもの
    if any(key in reason for key in ("tangent neighbour", "rests on another blend", "blend of kind", "blend has", "sphere next to a blend",
                                     "supports of a blend share")):
        return "丸め追従の対象外"
    return "関門で断る"


def load(path):
    return [json.loads(line) for line in open(path, encoding="utf-8")]


def markdown(models) -> str:
    cells = defaultdict(lambda: defaultdict(int))
    times = defaultdict(list)
    for model in models:
        for row in model["rows"]:
            key = (row["kind"], row["method"])
            cells[key]["件数"] += 1
            cells[key][classify(row["reason"])] += 1
            if row["ok"] and row["ms"] == row["ms"]:
                times[key].append(row["ms"])
    out = ["| 試行 | 方式 | 件数 | " + " | ".join(COLUMNS) + " | 通ったものの時間 中央値 / 最大 |", "|---|---|---:|" + "---:|" * len(COLUMNS) + "---|"]
    for kind, label in KINDS.items():
        for (k, method), cell in sorted(cells.items()):
            if k != kind:
                continue
            ms = times[(k, method)]
            span = f"{statistics.median(ms):.0f} / {max(ms):.0f} ms" if ms else "—"
            out.append(f"| {label} | {method} | {cell['件数']} | " + " | ".join(str(cell.get(c, 0)) for c in COLUMNS) + f" | {span} |")
    per_model = ["| 部品 | 試行 | 通った |", "|---|---:|---:|"]
    for model in models:
        rows = model["rows"]
        per_model.append(f"| {model['model']} | {len(rows)} | {sum(1 for r in rows if r['ok'])} |")
    return "\n".join(out) + "\n\n" + "\n".join(per_model)


if __name__ == "__main__":
    print(markdown(load(sys.argv[1])))

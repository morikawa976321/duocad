"""実物の試行(`realworld.py` の行の記録 FACE_EDIT_ROWS)で、断った理由の内訳を見る。

    python -m tools.face_edit_probe.realworld_why <rows.jsonl> <file.step>

1. 平面の移動: ソリッドごとに断った理由を数える(動かした面によらず、同じ理由で全部断るかを見る)。
2. 穴の削除: 断った穴を、壁だけでなく穴を作る面の全部(止まり穴の底・座ぐりの段とその先の壁)で消し直す。
"""
import collections
import json
import sys
from pathlib import Path


def _rows(rows_file, name):
    for line in open(rows_file, encoding="utf-8"):
        record = json.loads(line)
        if record["model"] == name:
            return {r["done"]: r for r in record["rows"]}
    raise SystemExit(f"{name} is not in {rows_file}")


def _on_outer_wire(face, edges):
    from OCP.BRepTools import BRepTools

    from . import geometry as g

    return any(e.IsSame(h) for e in g.edges_of(BRepTools.OuterWire_s(face)) for h in edges)


def whole_hole(solid, wall):
    """穴の壁から、外周の輪でつながる平面(止まり穴の底・座ぐりの段)と、その平面の内側へ続く壁をたどる。"""
    from . import geometry as g

    chosen, grew = [wall], True
    while grew:
        grew = False
        walls = [c for c in chosen if g.surface_type(c) != "plane"]
        floors = [c for c in chosen if g.surface_type(c) == "plane"]
        for n in g.neighbours(solid, chosen):
            if any(n.IsSame(c) for c in chosen):
                continue
            kind = g.surface_type(n)
            if kind == "plane" and _on_outer_wire(n, [e for w in walls for e in g.edges_of(w)]):
                chosen.append(n)
                grew = True
            elif kind in ("cylinder", "cone") and any(e.IsSame(x) for f in floors for x in g.edges_of(f) for e in g.edges_of(n)):
                chosen.append(n)
                grew = True
    return chosen


def main(rows_file, path):
    from . import strict
    from .realworld import _trials

    rows = _rows(rows_file, Path(path).name)
    by_solid = collections.defaultdict(collections.Counter)
    for r in rows.values():
        if r["kind"] == "move_free":
            by_solid[r["solid"]]["ok" if r["ok"] else (r["reason"] or "")[:70]] += 1
    print("平面の移動(ソリッドごと)")
    for solid, reasons in sorted(by_solid.items()):
        print(f"  {solid:3d} {dict(reasons)}")

    tally = collections.Counter()
    for i, (solid_index, solid, kind, faces, _) in enumerate(_trials(path)):
        if kind != "delete_hole" or i not in rows or rows[i]["ok"]:
            continue
        chosen = whole_hole(solid, faces[0])
        try:
            strict.strict_delete(solid, chosen)
            after = "ok"
        except Exception as error:  # noqa: BLE001
            after = f"{type(error).__name__}: {str(error)[:60]}"
        tally[(len(chosen), after)] += 1
    print("断った穴を、穴を作る面の全部で消し直す(選んだ面の数, 結果)")
    for (count, after), n in tally.most_common():
        print(f"  {n:4d}  {count}面  {after}")


if __name__ == "__main__":
    main(*sys.argv[1:3])

"""規模の実測: 穴 N 個の板で、穴を1個/全部消すときの A と C の時間(docs/FACE_EDIT.md「規模」)。

    .venv/Scripts/python.exe -m tools.face_edit_probe.scale
"""
from __future__ import annotations

import time

from . import custom, methods
from . import geometry as g
from .shapes import _compound, box, curved, cut, cylinder, pick


def plate_with_holes(n_side):
    plate = box(20 * n_side, 20 * n_side, 10)
    holes = [cylinder(3, 12, (10 + 20 * i, 10 + 20 * j, -1)) for i in range(n_side) for j in range(n_side)]
    return cut(plate, _compound(holes))


def timed(fn):
    started = time.perf_counter()
    result = fn()
    return 1000 * (time.perf_counter() - started), result


def main():
    print("holes  remove  A_ms     A_par_ms  C_ms    faces_after(A/C)")
    for n_side in (1, 3, 5, 7, 10):
        shape = plate_with_holes(n_side)
        holes = pick(shape, curved)
        for label, chosen in (("one", holes[:1]), ("all", holes)):
            ta, (ra, _) = timed(lambda: methods.defeature(shape, chosen))
            tp, _ = timed(lambda: methods.defeature(shape, chosen, parallel=True))
            tc, rc = timed(lambda: custom.rebuild_delete(shape, chosen))
            print(f"{n_side*n_side:5d}  {label:6s}  {ta:8.1f} {tp:8.1f}  {tc:7.1f}  {len(g.faces_of(ra))}/{len(g.faces_of(rc))}")


if __name__ == "__main__":
    main()

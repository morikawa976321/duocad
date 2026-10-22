"""PC ケース(実際の27部品)の気になる組を掘る。real_docs.py の結果(JSON)を入力にする。

    .\\.venv\\Scripts\\python.exe -m tools.interference_probe.pc_detail <STEP> <real_docs の JSON>

1. 重なりの「厚さ」: 重なり部分の立体の 2V/A(薄い板なら厚さ、塊なら大きさの目安)と、境界箱の最小辺。
   体積だけでは、面の数値誤差による薄片と実際の食い込みが分けられないかを見る。
2. 隙間がちょうど 0.2 に設計された組の距離を有効数字いっぱいで出す(境目の判定)。
3. DistShapeShape の SetMultiThread(True) の効き。
4. General Fuse の警告の中身と、並列化の効き。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.Message import Message_Gravity

from . import methods as M
from .real_docs import load_step


def area(shape):
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return props.Mass()


def report_text(algo, gravity) -> str:
    try:
        report = algo.GetReport()
        alerts = report.GetAlerts(gravity)
        names = []
        for alert in alerts:
            names.append(alert.GetMessageKey())
        return ",".join(sorted(set(names))) or "-"
    except Exception as exc:
        return f"? {exc!r:.60}"


def main(argv=None):
    step, result = Path(sys.argv[1]), Path(sys.argv[2])
    solids, _ = load_step(step)
    shapes = [s.shape for s in solids]
    index = {s.name: k for k, s in enumerate(solids)}
    rows = json.loads(result.read_text(encoding="utf-8"))[0]["pairs"]

    print("== 1. 重なりの厚さ ==")
    for r in sorted(rows, key=lambda r: -(r.get("volume") or 0)):
        if not r.get("volume"):
            continue
        a, b = shapes[index[r["a"]]], shapes[index[r["b"]]]
        c = M.common(a, b)
        A = area(c["shape"])
        lo, hi = M.bbox(c["shape"], optimal=True)
        ext = sorted(hi - lo)
        print(f"{r['a']:>8s}-{r['b']:<8s} V={c['volume']:.6g} A={A:.6g} 2V/A={2 * c['volume'] / A if A else 0:.3g} "
              f"bbox_min={ext[0]:.3g} mid={ext[1]:.3g} max={ext[2]:.3g} solids={c['solids']} warn={c['warnings']}")

    print("== 2. 隙間 0.2 前後の組 ==")
    for r in rows:
        if r["class"] == "near":
            print(f"{r['a']:>8s}-{r['b']:<8s} d={r['dist']!r}")

    print("== 3. DistShapeShape の並列化 ==")
    slow = sorted(rows, key=lambda r: -r["dist_s"])[:4]
    for r in slow:
        a, b = shapes[index[r["a"]]], shapes[index[r["b"]]]
        s1 = M.dist(a, b, parallel=False)
        s2 = M.dist(a, b, parallel=True)
        print(f"{r['a']:>8s}-{r['b']:<8s} serial {s1['seconds']:.2f}s d={s1.get('distance'):.6g} | "
              f"multi {s2['seconds']:.2f}s d={s2.get('distance'):.6g}")

    print("== 4. General Fuse ==")
    from OCP.BOPAlgo import BOPAlgo_Builder
    from OCP.TopTools import TopTools_ListOfShape
    for parallel in (False, True):
        t0 = time.perf_counter()
        gf = BOPAlgo_Builder()
        args = TopTools_ListOfShape()
        for s in shapes:
            args.Append(s)
        gf.SetArguments(args)
        gf.SetRunParallel(parallel)
        gf.Perform()
        print(f"parallel={parallel} {time.perf_counter() - t0:.2f}s errors={gf.HasErrors()} "
              f"warnings={report_text(gf, Message_Gravity.Message_Warning)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

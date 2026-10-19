r"""PC ケースの部品の形状許容差(BRep の頂点・辺・面の tolerance)と、薄片の厚さの関係、
および BOPAlgo_BOP の DS(干渉の記録)で接触を判定できるかを見る。

    .\.venv\Scripts\python.exe -m tools.interference_probe.tolerance_check <STEP>
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from OCP.BOPAlgo import BOPAlgo_BOP, BOPAlgo_Operation, BOPAlgo_PaveFiller
from OCP.TopTools import TopTools_ListOfShape
from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance

from . import methods as M
from .real_docs import load_step


def bop_touch(a, b):
    """交差の段(PaveFiller)を自分で持って干渉を数え、同じ交差の結果で COMMON を作る。

    `BOPAlgo_BOP.PDS()` や `PPaveFiller().DS()` が返すポインタを Python から触ると、ラッパが持ち主の
    つもりで解放して落ちる(実測、終了コード 127)。PaveFiller を自分で作り `DS()`(参照)を読む。
    """
    t0 = time.perf_counter()
    pf = BOPAlgo_PaveFiller()
    args = TopTools_ListOfShape()
    args.Append(a)
    args.Append(b)
    pf.SetArguments(args)
    pf.Perform()
    ds = pf.DS()
    n = 0
    for pair in ds.Interferences():
        i, j = pair.Indices()
        if ds.Rank(i) != ds.Rank(j):
            n += 1
    op = BOPAlgo_BOP()
    op.AddArgument(a)
    op.AddTool(b)
    op.SetOperation(BOPAlgo_Operation.BOPAlgo_COMMON)
    op.PerformWithFiller(pf)
    shape = op.Shape()
    v = M.volume(shape) if shape is not None and not shape.IsNull() else 0.0
    return n, v, time.perf_counter() - t0


def main():
    solids, _ = load_step(Path(sys.argv[1]))
    S = {s.name: s.shape for s in solids}
    st = ShapeAnalysis_ShapeTolerance()
    for name in ["solid17", "solid23", "solid21", "solid26", "solid14", "solid16", "solid2", "solid10", "solid1"]:
        print(name, "maxtol=%.3g avg=%.3g" % (st.Tolerance(S[name], 1), st.Tolerance(S[name], 0)), flush=True)
    for a, b in [("solid4", "solid5"), ("solid7", "solid15"), ("solid7", "solid21"), ("solid2", "solid10"),
                 ("solid19", "solid24"), ("solid11", "solid19"), ("solid0", "solid17")]:
        print(a, b, "interferences=%d V=%.6g %.3fs" % bop_touch(S[a], S[b]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

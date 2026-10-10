"""既定(いちばん粗い)解像度で design_space.derive がレイキャストの ValueError を踏むかを、製品経路で確かめる。

optimize_run.run を本物で呼び、ds.derive の直後で止める(探索・FEM は回さない)。各題材で
- 既定(element_size_mm を省く)
- 選べる範囲(下限〜上限)を STEP mm 刻みで要求した各解像度(格子に切り下げた値で重複を除く)
- --beyond: 上限 4mm を外して、以前の既定が届いていた粗い側(4.5〜20mm)
を試し、通ったか・どの例外で落ちたかを JSON 行で出す。実測は docs/TOPOPT.md「制限」。

    .venv/Scripts/python.exe topopt_checks/probe_resolution_raycast.py wiring|wiring_tiny|<models/ の json 名> [--step 0.05] [--beyond]
"""
import argparse
import json
import time
import traceback
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

from duocad import optimize_run
from duocad.topopt import design_space as ds

REPO = Path(__file__).resolve().parents[1]


class Stop(Exception):
    pass


def open_doc(name):
    if name in ("wiring", "wiring_tiny"):
        import check_optimize_wiring as w
        return w.build_case(extra_tiny_keep=(name == "wiring_tiny"))[0].document
    from _documents import open_session
    return open_session(REPO / "models" / f"{name}.json").document


def probe(doc, request):
    real = ds.derive
    seen = {}

    def wrapped(conditions, **kwargs):
        seen["spacing"] = float(kwargs.get("spacing"))
        t = time.perf_counter()
        space = real(conditions, **kwargs)
        seen["derive_seconds"] = round(time.perf_counter() - t, 2)
        seen["elements"] = int(space.element_count)
        raise Stop

    started = time.perf_counter()
    try:
        with patch.object(ds, "derive", wrapped):
            result = optimize_run.run(doc, optimize_run.OptimizeOptions(element_size_mm=request, max_evaluations=1))
        row = dict(outcome="returned", state=result.get("state"), warning=(result.get("warning") or "")[:300])
    except Stop:
        row = dict(outcome="derived")
    except Exception as error:  # 落ち方を記録する
        row = dict(outcome="error", error=f"{type(error).__name__}: {error}"[:400],
                   where=traceback.extract_tb(error.__traceback__)[-1].name)
    return dict(request=request, seconds=round(time.perf_counter() - started, 2), **seen, **row)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("case")
    ap.add_argument("--step", type=float, default=0.05)
    ap.add_argument("--beyond", action="store_true")
    a = ap.parse_args()
    doc = open_doc(a.case)
    limits = optimize_run.resolution_limits(doc)
    lo, hi = float(limits["min_element_size_mm"]), float(limits["max_element_size_mm"])
    print(json.dumps(dict(case=a.case, limits={k: v for k, v in limits.items() if not isinstance(v, (list, tuple))}),
                     ensure_ascii=False, default=str), flush=True)
    print(json.dumps(dict(case=a.case, kind="default", **probe(doc, None)), ensure_ascii=False), flush=True)
    if a.beyond:
        requests = [round(4.0 + 0.5 * i, 3) for i in range(1, 33)]
        ctx = patch.object(optimize_run, "MAX_ELEMENT_SIZE_MM", 1e9)
    else:
        # 下限ちょうどの要求は丸めで下限を割って断られるので、1刻み上から始める
        n = int((hi - lo) / a.step)
        requests = [lo + a.step * i for i in range(1, n + 1)] + [hi]
        ctx = patch.object(optimize_run, "MAX_ELEMENT_SIZE_MM", optimize_run.MAX_ELEMENT_SIZE_MM)
    tried = set()
    with ctx:
        for r in requests:
            row = probe(doc, r)
            key = row.get("spacing")
            if key is not None and key in tried:
                continue
            tried.add(key)
            print(json.dumps(dict(case=a.case, kind="beyond" if a.beyond else "range", **row), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

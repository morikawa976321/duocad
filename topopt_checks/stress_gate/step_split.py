"""歩幅の実験(Fable の G): 合否は製品の Q_V(V=1セル)のまま、歩幅 r の安全率だけを V=wide_mm3 の上位値で取る(同じ解から)。

    python step_split.py <wide_mm3> <run_product の引数...>

出力は output/stress_gate_20261008/step_split<wide>/product/(製品の記録を上書きしない)。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402
from duocad.topopt import binary_search, stress  # noqa: E402

wide = float(sys.argv[1])
pa.OUT = pa.OUT / f"step_split{wide:g}"
latest = dict(ratio=1.0)
state = dict(best_ratio=1.0)
real_mvs = stress.member_volume_stress
real_ratio = binary_search.step_ratio
real_find = binary_search.find_best_feasible


def mvs(values, region, *, volume_mm3, cell_mm3):
    value, population = real_mvs(values, region, volume_mm3=volume_mm3, cell_mm3=cell_mm3)
    broad, _ = real_mvs(values, region, volume_mm3=wide, cell_mm3=cell_mm3)
    latest["ratio"] = value / broad if broad > 0 else 1.0
    return value, population


def on_event(event):
    if event["outcome"] in ("initial", "accepted"):
        state["best_ratio"] = latest["ratio"]


def ratio(safety_factor, target, **kwargs):
    return real_ratio(safety_factor * max(1.0, state["best_ratio"]), target, **kwargs)


def find(*args, **kwargs):
    outer = kwargs.get("on_evaluation")

    def both(event):
        on_event(event)
        if outer is not None:
            outer(event)

    kwargs["on_evaluation"] = both
    return real_find(*args, **kwargs)


stress.member_volume_stress = mvs
binary_search.step_ratio = ratio
binary_search.find_best_feasible = find
import run_product  # noqa: E402

sys.argv = ["run_product.py"] + sys.argv[2:]
sys.exit(run_product.main())

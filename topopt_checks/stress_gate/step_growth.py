"""歩幅の実験: 採用されたら次の手は「直前に採用された削除数 × growth」以上を試す(安全率の余裕から決まる歩幅が小さいときだけ効く)。

    python step_growth.py <growth> <run_product の引数...>

出力は output/stress_gate_20261008/step_growth<growth>/product/ (製品の記録を上書きしない)。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402
from duocad.topopt import binary_search  # noqa: E402

growth = float(sys.argv[1])
pa.OUT = pa.OUT / f"step_growth{growth:g}"
state = dict(prev=None, k=None)
real_ratio = binary_search.step_ratio
real_find = binary_search.find_best_feasible


def on_event(event):
    if event["outcome"] == "accepted" and state["prev"] is not None:
        state["k"] = growth * max(1, state["prev"] - event["cells"])
    state["prev"] = event["cells"]


def ratio(safety_factor, target, *, exponent=binary_search.DEFAULT_STEP_EXPONENT,
          max_ratio=binary_search.DEFAULT_MAX_STEP_RATIO):
    gap = real_ratio(safety_factor, target, exponent=exponent, max_ratio=max_ratio)
    if state["k"] is None or not state["prev"]:
        return gap
    return min(max_ratio, max(gap, state["k"] / state["prev"]))


def find(*args, **kwargs):
    outer = kwargs.get("on_evaluation")

    def both(event):
        on_event(event)
        if outer is not None:
            outer(event)

    kwargs["on_evaluation"] = both
    return real_find(*args, **kwargs)


binary_search.step_ratio = ratio
binary_search.find_best_feasible = find
import run_product  # noqa: E402

sys.argv = ["run_product.py"] + sys.argv[2:]
sys.exit(run_product.main())

"""止め方の案を、止めずに回した探索の記録(`run_product.py --trace` の _trace.json)に当てはめる(docs/TOPOPT_STOPPING.md)。

    python replay_stops.py <trace.json> ...

各規則は「評価 e の直後に止めるか」を返す。止めたときの合格形の材料を、記録の最後(予算いっぱい・または順位が尽きた)の材料と比べる。
"""
import json
import sys
from pathlib import Path


def load(path):
    t = json.loads(Path(path).read_text(encoding="utf-8"))
    cell = t["row"]["cell_mm3"]
    ev = t["events"]
    steps = t["steps"]
    assert len(steps) == len(ev) - 1, (len(steps), len(ev))
    rows = []
    for i, e in enumerate(ev):
        s = steps[i - 1] if i > 0 else None
        rows.append(dict(e=e["evaluation"], outcome=e["outcome"], failed=e["failed"], sf=e["safety_factor"],
                         cand_sf=e["candidate_safety_factor"], mm3=e["cells"] * cell, t=e["seconds"],
                         picked=None if s is None else s["peel"]["picked"],
                         requested=None if s is None else s["requested_cells"],
                         target=e["target_safety_factor"]))
    return t, rows


def single_safety_fail(r):
    return r["outcome"] == "rejected" and r["picked"] == 1 and "safety" in r["failed"]


def patience(n):
    def rule(rows, i, st):
        st.setdefault("streak", 0)
        r = rows[i]
        if r["outcome"] == "accepted":
            st["streak"] = 0
        elif single_safety_fail(r):
            st["streak"] += 1
        return st["streak"] >= n
    rule.name = f"patience {n}"
    return rule


def band_entry(tol):
    def rule(rows, i, st):
        return rows[i]["sf"] <= rows[i]["target"] * (1 + tol)
    rule.name = f"band entry {tol:g}"
    return rule


def band_first_fail(tol):
    def rule(rows, i, st):
        r = rows[i]
        return single_safety_fail(r) and r["sf"] <= r["target"] * (1 + tol)
    rule.name = f"band+1fail {tol:g}"
    return rule


def gain_window(w, p):
    def rule(rows, i, st):
        if i < w:
            return False
        return rows[i - w]["mm3"] - rows[i]["mm3"] < p * rows[i]["mm3"]
    rule.name = f"gain W{w} p{p * 100:g}%"
    return rule


def single_phase(n):
    def rule(rows, i, st):
        st.setdefault("run", 0)
        st["run"] = st["run"] + 1 if rows[i]["requested"] == 1 else 0
        return st["run"] >= n
    rule.name = f"k=1 run {n}"
    return rule


def either(a, b):
    def rule(rows, i, st):
        sa = st.setdefault("a", {})
        sb = st.setdefault("b", {})
        ra, rb = a(rows, i, sa), b(rows, i, sb)
        return ra or rb
    rule.name = f"{a.name} | {b.name}"
    return rule


def apply(rule, rows):
    st = {}
    for i in range(len(rows)):
        if rule(rows, i, st):
            return i
    return len(rows) - 1


RULES = ([patience(n) for n in (4, 8, 16, 32)]
         + [band_entry(t) for t in (0.01, 0.05)] + [band_first_fail(0.01)]
         + [gain_window(w, p) for w in (20, 50, 100) for p in (0.001, 0.0025, 0.005, 0.01)]
         + [single_phase(n) for n in (20, 50, 100)])


def report(path, rules=RULES):
    t, rows = load(path)
    end = rows[-1]
    least = min(r["mm3"] for r in rows)
    print(f"\n## {Path(path).name}  stop={t['stop_reason']} term={t['termination']} evals={end['e']} "
          f"end={end['mm3']:.0f} mm3 least={least:.0f} t={end['t']:.0f}s")
    print(f"{'rule':34s} {'eval':>5s} {'mm3':>9s} {'loss%':>7s} {'sec':>7s}")
    for rule in rules:
        i = apply(rule, rows)
        r = rows[i]
        print(f"{rule.name:34s} {r['e']:5d} {r['mm3']:9.0f} {100 * (r['mm3'] / least - 1):7.2f} {r['t']:7.0f}")
    return rows


if __name__ == "__main__":
    for p in sys.argv[1:]:
        report(p)

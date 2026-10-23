"""山形・径・ピッチごとの「ガタ無しで回る隙間の範囲」(推奨の範囲)を出す。ねじ verb の隙間の推奨の範囲の元になる表。

    .venv/Scripts/python.exe -X utf8 -m tools.thread_sim.window [--workers 3]

結果は output/thread_sim/windows_fine.json と表(標準出力)。前提(THREAD_SIM.md 6.8〜6.10):
- 印刷は本人のふだんの 0.12mm Fine(printer.FINE)に、試し刷り TP1 に合わせた係数(printer.TP1_FIT)。
- ボルトは呼び寸法、隙間はナットだけ(split=0)。半円は法線方向、台形は半径方向(試し刷りと同じ付け方)。
- 範囲の下端 = 誤差 E_MAX でも余り ≥ 0 の最小の隙間。上端 = 誤差 0 でも軸方向の遊び ≤ PLAY_MAX かつ掛かり ≥ E_REQ の最大の隙間。
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "output" / "thread_sim"
E_MAX = 0.05  # 刷るたびの寸法誤差の幅(TP1 と TP2 から逆算した 0.015〜0.05 の上側)
PLAY_MAX = 0.15  # 押し引きで分かるガタの境目の軸方向の遊び(TP4 の B2・E2 から 0.15 寄り)
E_REQ = 0.1  # 掛かりの下限
FAMILIES = {  # 名前: (山形を作る式, 隙間の付け方)
    "半円": (lambda D, P: ("coil", D, P, round(0.4 * P, 4)), "normal"),
    "30°台形": (lambda D, P: ("trap", D, P, round(0.4 * P, 4), 30), "radial"),
    "45°台形": (lambda D, P: ("trap", D, P, round(0.4 * P, 4), 45), "radial"),
}


def _profile(spec):
    from . import profiles as pf
    if spec[0] == "coil":
        return pf.coil(spec[1], spec[2], spec[3])
    return pf.trapezoid(spec[1], spec[2], spec[3], spec[4], spec[4])


def _state(args):
    fam, D, P, c, e = args
    from . import evaluate as ev
    from .printer import FINE, TP1_FIT
    mk, mode = FAMILIES[fam]
    pr = FINE.with_(**TP1_FIT)
    pr = pr.with_(contour_bias=pr.contour_bias + e / 2, hole_bias=pr.hole_bias - e / 2)
    try:
        r = ev.evaluate(ev.Design(fam, _profile(mk(D, P)), c, mode=mode, L_nut=float(D), R_out=0.9 * D, split=0.0), pr,
                        with_strength=False)
        return fam, D, P, c, e, r.margin, r.engagement, r.play
    except ValueError:
        return fam, D, P, c, e, None, None, None


def windows(states):
    by = {}
    for fam, D, P, c, e, m, g, p in states:
        by.setdefault((fam, D, P), {}).setdefault(c, {})[e] = (m, g, p)
    out = []
    for (fam, D, P), row in sorted(by.items()):
        lo = next((c for c in sorted(row) if row[c][E_MAX][0] is not None and row[c][E_MAX][0] >= 0), None)
        hi = None
        for c in sorted(row):
            m, g, p = row[c][0.0]
            if m is not None and g >= E_REQ and p <= PLAY_MAX:
                hi = c
        ok = lo is not None and hi is not None and hi >= lo
        out.append(dict(family=fam, D=D, P=P, lo=lo if ok else None, hi=hi if ok else None,
                        center=round(0.5 * (lo + hi), 3) if ok else None))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--families", default=",".join(FAMILIES))
    a = ap.parse_args(argv)
    fams = a.families.split(",")
    cs = [round(0.01 * k, 2) for k in range(1, 61)]
    jobs = [(f, D, round(pr * D / 0.05) * 0.05, c, e) for f in fams for D in (3, 4, 5, 6, 8, 10) for pr in (0.25, 0.3, 0.4)
            for c in cs for e in (0.0, E_MAX)]
    with ProcessPoolExecutor(a.workers) as pool:
        states = list(pool.map(_state, jobs, chunksize=8))
    res = windows(states)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "windows_fine.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("山形 D P 範囲(真ん中)")
    for r in res:
        rng = f"[{r['lo']:.2f}, {r['hi']:.2f}]({r['center']:.3f})" if r["lo"] is not None else "範囲なし"
        print(f"{r['family']} D{r['D']:g} P{r['P']:g} {rng}")


if __name__ == "__main__":
    main()

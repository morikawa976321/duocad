"""山形・径・ピッチごとの隙間の範囲(ねじ verb の隙間の上下限・推奨の範囲・既定値の元になる表)。

    .venv/Scripts/python.exe -X utf8 -m tools.thread_sim.window [--workers 3]

結果は output/thread_sim/windows_fine.json と表(標準出力)。前提(THREAD_SIM.md 6.8〜6.10):
- 印刷は本人のふだんの 0.12mm Fine(printer.FINE)に、試し刷り TP1 に合わせた係数(printer.TP1_FIT)。
- ボルトは呼び寸法、隙間はナットだけ(split=0)。半円は法線方向、台形は半径方向(試し刷りと同じ付け方)。
- 推奨の下端 = 誤差 E_MAX でも余り ≥ 0 の最小の隙間。推奨の上端 = 誤差 0 でも軸方向の遊び ≤ PLAY_MAX かつ掛かり ≥ E_REQ の最大の隙間。
- 上限(受け付けない境目) = 誤差 0 で掛かり ≥ E_REQ が残り、山形が作れる最大の隙間。下限は 0。
余りと遊びは隙間とともに増え、掛かりは減る(単調)ので、境目は二分法で探す。
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
PITCH_RATIOS = (0.25, 0.3, 0.4)
DIAMETERS = (3, 4, 5, 6, 8, 10)


def _profile(spec):
    from . import profiles as pf
    if spec[0] == "coil":
        return pf.coil(spec[1], spec[2], spec[3])
    return pf.trapezoid(spec[1], spec[2], spec[3], spec[4], spec[4])


def _state(fam, D, P, c, e):
    """(余り, 掛かり, 遊び)。山形が作れないときは None。"""
    from . import evaluate as ev
    from .printer import FINE, TP1_FIT
    mk, mode = FAMILIES[fam]
    pr = FINE.with_(**TP1_FIT)
    pr = pr.with_(contour_bias=pr.contour_bias + e / 2, hole_bias=pr.hole_bias - e / 2)
    try:
        r = ev.evaluate(ev.Design(fam, _profile(mk(D, P)), c, mode=mode, L_nut=float(D), R_out=0.9 * D, split=0.0), pr,
                        with_strength=False)
        return r.margin, r.engagement, r.play
    except ValueError:
        return None


def _bisect(ok, lo, hi, tol=0.004):
    """ok(lo) が真、ok(hi) が偽のとき、真の最大の点(ok が c について 真→偽 と一度だけ変わる前提)。"""
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


def case(args):
    fam, D, P = args
    depth = 0.4 * P
    top = 2.5 * depth + 0.3  # これより大きい隙間では、どの山形も掛かりが残らない
    holds = lambda c: (s := _state(fam, D, P, c, 0.0)) is not None and s[1] >= E_REQ
    fits = lambda c: (s := _state(fam, D, P, c, E_MAX)) is not None and s[0] >= 0
    quiet = lambda c: (s := _state(fam, D, P, c, 0.0)) is not None and s[1] >= E_REQ and s[2] <= PLAY_MAX
    hard_hi = _bisect(holds, 0.0, top) if holds(0.0) else None
    rec_lo = (_bisect(lambda c: not fits(c), 0.0, top) + 0.004) if not fits(0.0) else 0.0
    rec_hi = _bisect(quiet, rec_lo, top) if quiet(rec_lo) else None
    return dict(family=fam, D=D, P=P, depth=round(depth, 3), hard_lo=0.0,
                hard_hi=round(hard_hi, 3) if hard_hi is not None else None,
                rec_lo=round(rec_lo, 3), rec_hi=round(rec_hi, 3) if rec_hi is not None else None)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args(argv)
    jobs = [(f, D, round(pr * D / 0.05) * 0.05) for f in FAMILIES for D in DIAMETERS for pr in PITCH_RATIOS]
    with ProcessPoolExecutor(a.workers) as pool:
        res = list(pool.map(case, jobs))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "windows_fine.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("山形 D P | 上限(掛かり 0.1) | 推奨の範囲")
    for r in res:
        rec = f"{r['rec_lo']:.3f}〜{r['rec_hi']:.3f}" if r["rec_hi"] is not None and r["rec_hi"] >= r["rec_lo"] else "範囲なし"
        print(f"{r['family']} D{r['D']:g} P{r['P']:g} | {r['hard_hi']} | {rec}", flush=True)


if __name__ == "__main__":
    main()

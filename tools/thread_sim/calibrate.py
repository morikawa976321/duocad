"""試し刷りの結果(index.html がコピーさせる1行)を読み、事前の予測と比べる。

    .venv/Scripts/python.exe -X utf8 -m tools.thread_sim.calibrate "ねじ試し刷り結果 TP1 A1=手で軽く回る ..."

- 合否: 「手で軽く回る」(山飛びなし)だけを、初期設定で軽く使える合格とする。「全長回るが固い」は合格に数えず別に数える
  (剛体の模型と実物の違い = 段の鼻の潰れなど、を読む材料。astra の指摘)。「試していない」は不合格と推定しない。
- ゲージ(G・H): すっと入る最小の穴から、山形によらない寸法誤差 e(雌雄の和・半径)の区間 [1つ手前の穴の隙間, その穴の隙間] を出す。
  ねじの予測には補助としてだけ使う(ゲージとねじで同じ誤差とは限らない)。
- 予測: 誤差 e のもとでナットがはまるのは、事前に計算した包絡の余り margin(誤差なし)≥ e のとき
  (外形の太り・穴の痩せは余りをちょうど e だけ減らす。THREAD_SIM.md 1章)。
- 系列ごとの残差 = 観測の境目 − 予測の境目。系列によらず同じなら、山形の間の差の予測は当たっていて、ずれは山形によらない量。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .testprint import GAUGES, OUT, SERIES

PASS = "手で軽く回る"
TIGHT = "全長回るが固い"
FAIL = ("入らない", "途中で止まる")


def parse(text: str) -> dict:
    out = {}
    for tok in text.split():
        m = re.match(r"^([A-Z]\d?)=(.+)$", tok)
        if m:
            main, *flags = m.group(2).split("+")
            out[m.group(1)] = (main, flags)
    return out


def analyze(text: str, folder: Path = OUT) -> dict:
    res = parse(text)
    pred = json.loads((folder / "predictions.json").read_text(encoding="utf-8"))
    report = dict(test_id=(re.search(r"結果 (TP\d+)", text) or [None, None])[1], gauges={}, series={})
    for gid, d, holes in GAUGES:
        ans = res.get(gid, ("未回答", []))[0]
        if ans.startswith("穴"):
            k = int(ans[1:]) - 1
            report["gauges"][gid] = dict(D=d, e_range=((holes[k - 1] - d) / 2 if k > 0 else None, (holes[k] - d) / 2))
        else:
            report["gauges"][gid] = dict(D=d, answer=ans)
    for sid, name, D, _, mode, _, cs in SERIES:
        p = pred.get(sid, {})
        margins = {round(n["c"], 4): n["margin"] for n in p.get("nuts", [])}
        g = report["gauges"].get("G" if D >= 6 else "H", {})
        e_lo, e_hi = g.get("e_range") or (None, None)
        rows = []
        for i, c in enumerate(sorted(cs, reverse=True), 1):
            main, flags = res.get(f"{sid}{i}", ("未回答", []))
            ok = main == PASS and "山を飛ぶ・抜ける" not in flags
            mg = margins.get(round(c, 4))
            if mg is None or e_hi is None:
                predicted = None
            elif mg >= e_hi:
                predicted = "はまる"
            elif e_lo is not None and mg < e_lo:
                predicted = "はまらない"
            else:
                predicted = "境目"
            rows.append(dict(c=c, result=main, flags=flags, pass_=ok, tight=main == TIGHT, margin0=mg, predicted=predicted))
        passed = [r["c"] for r in rows if r["pass_"]]
        failed = [r["c"] for r in rows if r["result"] in FAIL or r["tight"]]
        c_hi = min(passed) if passed else None
        c_lo = max([c for c in failed if c_hi is None or c < c_hi], default=None)
        c_geo = p.get("c_geo")
        exp_hi = c_geo + e_hi if (c_geo is not None and e_hi is not None) else None
        report["series"][sid] = dict(name=name, mode=mode, rows=rows, boundary=(c_lo, c_hi), c_geo=c_geo, expected=exp_hi,
                                     residual=(c_hi - exp_hi) if (c_hi is not None and exp_hi is not None) else None)
    return report


def _nut_state(args):
    """印刷の模擬の係数 kw のもとで、系列 sid のナット(隙間 c)の誤差なしの余りと掛かり代。ボルトは呼び寸法(split=0)。"""
    sid, c, kw = args
    from dataclasses import replace
    from . import evaluate as ev
    from .printer import DEFAULT
    _, name, D, mk, mode, _, _ = next(s for s in SERIES if s[0] == sid)
    des = ev.Design(name, mk(), c, mode=mode, L_nut=float(D), R_out=0.9 * D, split=0.0)
    e = ev.evaluate(replace(des, clearance=c), DEFAULT.with_(**kw), with_strength=False)
    return e.margin, e.engagement


def fit(text: str, grid: dict, workers: int = 3) -> list:
    """係数の組ごとに、観測(はまった/はまらない)と一致する誤差 e の範囲を径ごとに探す。
    はまった = 「手で軽く回る」「全長回るが固い」(嵌合の境目を見るので山飛びは問わない)、はまらない = 「入らない」「途中で止まる」。
    予測は「余り ≥ e ならはまる」。返り値は係数の組ごとの (一致数, 全数, 径ごとの e の一致区間)。"""
    import itertools
    from concurrent.futures import ProcessPoolExecutor
    res = parse(text)
    obs = []
    for sid, name, D, _, mode, _, cs in SERIES:
        for i, c in enumerate(sorted(cs, reverse=True), 1):
            main = res.get(f"{sid}{i}", ("未回答", []))[0]
            if main in (PASS, TIGHT):
                obs.append((sid, D, c, True))
            elif main in FAIL:
                obs.append((sid, D, c, False))
    keys = list(grid)
    combos = [dict(zip(keys, v)) for v in itertools.product(*(grid[k] for k in keys))]
    jobs = [(sid, c, kw) for kw in combos for sid, _, c, _ in obs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        states = list(pool.map(_nut_state, jobs, chunksize=4))
    out = []
    for n, kw in enumerate(combos):
        st = states[n * len(obs):(n + 1) * len(obs)]
        e_grid = [k * 0.005 for k in range(0, 61)]
        best_total, per_D = 0, {}
        for D in sorted({o[1] for o in obs}):
            idx = [j for j, o in enumerate(obs) if o[1] == D]
            score = [sum((st[j][0] >= e) == obs[j][3] for j in idx) for e in e_grid]
            m = max(score)
            ok = [e for e, s in zip(e_grid, score) if s == m]
            per_D[D] = (m, len(idx), (min(ok), max(ok)))
            best_total += m
        out.append(dict(kw=kw, agree=best_total, total=len(obs), per_D=per_D,
                        detail=[(o[0], o[2], o[3], round(s[0], 3), round(s[1], 3)) for o, s in zip(obs, st)]))
    return sorted(out, key=lambda r: -r["agree"])


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--fit":
        text = " ".join(sys.argv[2:])
        grid = dict(sag_amp=[0.0, 0.25, 0.5, 1.0, 2.0], bead_round=[0.0, 0.5, 1.0], sag_phi_ok=[0.3, 0.5, 0.7])
        for r in fit(text, grid):
            per = "  ".join(f"D{D:g}: {m}/{t} e {lo:.3f}〜{hi:.3f}" for D, (m, t, (lo, hi)) in r["per_D"].items())
            print(f"{r['agree']}/{r['total']}  {r['kw']}  {per}")
        return
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    rep = analyze(text)
    print("試験版", rep["test_id"])
    for gid, g in rep["gauges"].items():
        print(f"ゲージ {gid} D{g['D']:g}: 寸法誤差 e(雌雄の和・半径)の区間 {g.get('e_range', g.get('answer'))}")
    for sid, s in rep["series"].items():
        rs = " ".join(f"{r['c']:g}:{r['result']}{'+' + '+'.join(r['flags']) if r['flags'] else ''}(予測 {r['predicted']})" for r in s["rows"])
        print(f"{sid} {s['name']}: 観測の境目 {s['boundary']} / 予測 c_geo {s['c_geo']} + e → {s['expected']} / 残差 {s['residual']}\n   {rs}")


if __name__ == "__main__":
    main()

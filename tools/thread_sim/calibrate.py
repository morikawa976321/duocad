"""試し刷りの結果(index.html がコピーさせる1行)を読み、事前の予測と比べる。

    .venv/Scripts/python.exe -X utf8 -m tools.thread_sim.calibrate "ねじ試し刷り結果 A1=手で軽く回る ..."

- ゲージ(G・H): すっと入る最小の穴から、山形によらない寸法誤差 e(雌雄の和・半径)= (穴径 − ピン径)/2 の上限を出す
  (1つ手前の穴は入らなかったので、e はその間にある)。
- ねじの各系列: 回った(「全長回るが固い」か「手で軽く回る」)最小の隙間 c_obs と、回らなかった最大の隙間の間に境目がある。
  事前の予測 c_geo(predictions.json)+ e と比べ、残差 = 観測 − 予測 を出す。残差が系列によらず同じなら、
  山形の間の差の予測は当たっていて、ずれは山形によらない量(段の鼻の潰れなど)。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .testprint import GAUGES, OUT, SERIES

PASS = ("全長回るが固い", "手で軽く回る")
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
    report = dict(gauges={}, series={})
    for gid, d, holes in GAUGES:
        ans = res.get(gid, ("未回答", []))[0]
        if ans.startswith("穴"):
            k = int(ans[1:]) - 1
            hi = (holes[k] - d) / 2
            lo = (holes[k - 1] - d) / 2 if k > 0 else None
            report["gauges"][gid] = dict(D=d, e_range=(lo, hi))
        else:
            report["gauges"][gid] = dict(D=d, answer=ans)
    for sid, name, D, _, mode, _, cs in SERIES:
        rows = []
        for i, c in enumerate(sorted(cs, reverse=True), 1):
            main, flags = res.get(f"{sid}{i}", ("未回答", []))
            rows.append(dict(c=c, result=main, flags=flags))
        passed = [r["c"] for r in rows if r["result"] in PASS]
        failed = [r["c"] for r in rows if r["result"] in FAIL]
        c_obs_hi = min(passed) if passed else None  # 回った最小の隙間
        c_obs_lo = max([c for c in failed if c_obs_hi is None or c < c_obs_hi], default=None)  # それより小さくて回らなかった最大
        g = report["gauges"].get("G" if D >= 6 else "H", {})
        e_hi = (g.get("e_range") or (None, None))[1]
        p = pred.get(sid, {})
        expect = p.get("c_geo") + e_hi if (p.get("c_geo") is not None and e_hi is not None) else None
        report["series"][sid] = dict(name=name, mode=mode, rows=rows, c_obs=(c_obs_lo, c_obs_hi), c_geo=p.get("c_geo"),
                                     expected_upper=expect,
                                     residual_upper=(c_obs_hi - expect) if (c_obs_hi is not None and expect is not None) else None)
    return report


def main():
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    rep = analyze(text)
    for gid, g in rep["gauges"].items():
        print(f"ゲージ {gid} D{g['D']:g}: 寸法誤差 e(雌雄の和・半径)の範囲 {g.get('e_range', g.get('answer'))}")
    for sid, s in rep["series"].items():
        rs = " ".join(f"{r['c']:g}:{r['result']}{'+' + '+'.join(r['flags']) if r['flags'] else ''}" for r in s["rows"])
        print(f"{sid} {s['name']}: 境目 {s['c_obs']} / 予測 c_geo {s['c_geo']} + e → {s['expected_upper']} / 残差 {s['residual_upper']}  [{rs}]")


if __name__ == "__main__":
    main()

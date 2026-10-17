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


def main():
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

"""study の JSONL を表にする(Markdown)。

    .venv/Scripts/python.exe -X utf8 -m tools.thread_sim.report study1
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "output" / "thread_sim"
MODE_JA = dict(core="芯の層間", core_slice="芯(上限)", strip_bolt="雄の山", strip_nut="雌の山", tcd="FEA(芯の層間ほか)", tighten="締め付け")


def load(study: str):
    rows = []
    for line in (OUT / f"{study}.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def fmin(row):
    """引張で保持できる荷重(締め付けの判定は除く)。"""
    F = row.get("F")
    if not F:
        return None, None
    F = {k: v for k, v in F.items() if k in ("tcd", "strip_bolt", "strip_nut", "core")}
    k = min(F, key=F.get)
    return F[k], k


def cell_F(r, e):
    for row in r.get("rows", []):
        if abs(row["e"] - e) < 1e-9:
            if "error" in row and not row.get("F"):
                return "—"
            v, k = fmin(row)
            if v is None:
                return f"掛からず({row.get('engagement', 0):+.2f})"
            tight = row["F"].get("tighten")
            return f"{v:.0f} {MODE_JA.get(k, k)} / 締{tight:.0f} (掛{row['engagement']:.2f})" if tight else f"{v:.0f} {MODE_JA.get(k, k)} (掛{row['engagement']:.2f})"
    return ""


def table_study1(rows, out=sys.stdout):
    groups = defaultdict(list)
    for r in rows:
        key = (r["D"], r.get("pitch_ratio", "ISO"))
        groups[key].append(r)
    for key in sorted(groups, key=lambda k: (k[0], str(k[1]))):
        D, pr = key
        out.write(f"\n### D{D} " + ("ISO 並目" if pr == "ISO" else f"P={pr}·D(深さ 0.4P)") + "\n\n")
        out.write("| 設計 | 必要な隙間 c_geo | 張り出し欠け≥50% 雄/雌 | 垂れ最大 雄/雌 | 山の保持 | F @誤差0 | F @誤差0.1 | F @誤差0.2 |\n")
        out.write("|---|---|---|---|---|---|---|---|\n")
        for r in sorted(groups[key], key=lambda r: r["name"]):
            if "error" in r and "c_geo" not in r:
                out.write(f"| {r['name']} | エラー | | | | | | |\n")
                continue
            ob, on = r.get("over_bolt", {}), r.get("over_nut", {})
            out.write(f"| {r['name']} | {r['c_geo']:.3f} | {ob.get('lack_ge_50', float('nan')):.2f}/{on.get('lack_ge_50', float('nan')):.2f} | "
                      f"{ob.get('sag_max', float('nan')):.3f}/{on.get('sag_max', float('nan')):.3f} | {r.get('depth_kept', float('nan')):.2f} | "
                      f"{cell_F(r, 0.0)} | {cell_F(r, 0.1)} | {cell_F(r, 0.2)} |\n")


def table_study2(rows, out=sys.stdout):
    by = defaultdict(dict)
    for r in rows:
        base, var = r["name"].split(" | ")
        by[base][var] = r
    variants = []
    for d in by.values():
        for v in d:
            if v not in variants:
                variants.append(v)
    out.write("\n必要な隙間 c_geo(mm)\n\n| 設計 | " + " | ".join(variants) + " |\n|---|" + "---|" * len(variants) + "\n")
    for base, d in by.items():
        out.write(f"| {base} | " + " | ".join(f"{d[v]['c_geo']:.3f}" if v in d and "c_geo" in d[v] else "" for v in variants) + " |\n")
    out.write("\n破断荷重 F(N、誤差 0.1)\n\n| 設計 | " + " | ".join(variants) + " |\n|---|" + "---|" * len(variants) + "\n")
    for base, d in by.items():
        cells = []
        for v in variants:
            r = d.get(v)
            if not r or not r.get("rows"):
                cells.append("")
                continue
            val, k = fmin(r["rows"][0])
            cells.append(f"{val:.0f} {MODE_JA.get(k, k)}" if val else "掛からず")
        out.write(f"| {base} | " + " | ".join(cells) + " |\n")


def _wrow(r, k):
    """run_window の rows[k](0=中心、1=緩い端、2=固い端)を「保持(下限〜上限) 締め付け 乗り越え」に。"""
    rows = r.get("rows") or []
    if len(rows) <= k:
        return "—"
    x = rows[k]
    if not x.get("F"):
        return "—" if "error" in x else "掛からず"
    v, mode = fmin(x)
    F = x["F"]
    return (f"{v:.0f}〜{F.get('core_slice', float('nan')):.0f} {MODE_JA.get(mode, mode)} / 締{F.get('tighten', float('nan')):.0f}"
            f" / 越{x.get('jump_ratio', float('nan')):.2f}")


def table_window(rows, out=sys.stdout, key=lambda r: (r["D"], r.get("pitch_ratio", 0))):
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    for gk in sorted(groups, key=lambda k: tuple(str(x) for x in k) if not isinstance(k, tuple) else k):
        out.write(f"\n### {gk}\n\n")
        out.write("| 設計 | c_geo | 幅 W | 欠け≥50% 雄/雌 | 垂れ最大 雄/雌 | 山の保持 | 窓の中心: 保持 N(下限〜上限) / 締め付け / 乗り越え | 緩い端 |\n")
        out.write("|---|---|---|---|---|---|---|---|\n")
        for r in sorted(groups[gk], key=lambda r: -r.get("W", -9)):
            if "c_geo" not in r:
                out.write(f"| {r['name']} | エラー {str(r.get('error', ''))[:40]} | | | | | | |\n")
                continue
            ob, on = r.get("over_bolt", {}), r.get("over_nut", {})
            out.write(f"| {r['name']} | {r['c_geo']:.3f} | {r.get('W', float('nan')):+.3f} | "
                      f"{ob.get('lack_ge_50', float('nan')):.2f}/{on.get('lack_ge_50', float('nan')):.2f} | "
                      f"{ob.get('sag_max', float('nan')):.3f}/{on.get('sag_max', float('nan')):.3f} | {r.get('depth_kept', float('nan')):.2f} | "
                      f"{_wrow(r, 0)} | {_wrow(r, 1)} |\n")


def _lower(r, k=0):
    rows = r.get("rows") or []
    if len(rows) <= k or not rows[k].get("F"):
        return None
    return fmin(rows[k])[0]


def table_pareto(rows, out=sys.stdout, w_min=0.2):
    """径ごとに、(誤差を吸収できる幅 W, 窓の中心での保持の下限) の両方でほかに負けない設計(パレート)と、
    W ≥ w_min の中で保持の下限が最大の設計。"""
    by = defaultdict(list)
    for r in rows:
        if r.get("W") is not None and _lower(r) is not None:
            by[r["D"]].append(r)
    for D in sorted(by):
        rs = sorted(by[D], key=lambda r: -r["W"])
        front, best_f = [], -1.0
        for r in rs:  # W の大きい順に、それまでの最大より強いものだけ残す
            f = _lower(r)
            if f > best_f:
                front.append(r)
                best_f = f
        ok = [r for r in rs if r["W"] >= w_min]
        pick = max(ok, key=_lower) if ok else None
        out.write(f"\n### D{D}(パレート {len(front)} 件 / 全 {len(rs)} 件)\n\n")
        out.write("| 設計 | P | 深さ | β | c_geo | 幅 W | 保持の下限〜上限 N | 締め付け N | 緩い端の保持 N | 乗り越え |\n|---|---|---|---|---|---|---|---|---|---|\n")
        for r in front:
            x = r["rows"][0]
            mark = " **(W≥%.2f で最強)**" % w_min if r is pick else ""
            out.write(f"| {r['name']}{mark} | {r.get('P', '')} | {r.get('depth', ''):.3g} | {r.get('b_lo', '')} | {r['c_geo']:.3f} | {r['W']:.3f} | "
                      f"{_lower(r):.0f}〜{x['F'].get('core_slice', float('nan')):.0f} | {x['F'].get('tighten', float('nan')):.0f} | "
                      f"{(_lower(r, 1) or float('nan')):.0f} | {x.get('jump_ratio', float('nan')):.2f} |\n")
        if pick is not None and pick not in front:
            out.write(f"\nW≥{w_min} で最強: {pick['name']}(W {pick['W']:.3f}、保持の下限 {_lower(pick):.0f} N)\n")


if __name__ == "__main__":
    st = sys.argv[1]
    rows = load(st)
    if st == "study1":
        table_study1(rows)
    elif st == "study4":
        table_window(rows)
    elif st == "study3":
        table_pareto(rows)
    else:
        table_study2(rows)

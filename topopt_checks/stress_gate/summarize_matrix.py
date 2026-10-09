r"""13条件の比較(*_mx.json)を、条件ごとに現行と統合ゲートで並べる。

    .venv\Scripts\python.exe topopt_checks\stress_gate\summarize_matrix.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: E402,F401  (コンソールを UTF-8 にする)

OUT = Path(__file__).resolve().parents[2] / "output" / "stress_gate_20261008" / "searches"
rows: dict = {}
for p in OUT.glob("*_mx.json"):
    d = json.loads(p.read_text(encoding="utf-8"))
    key = (d["model"], d["load_n"], d["h"], d["w"])
    k1 = d.get("k1") or {}
    k2 = d.get("k2") or {}
    m1 = k1.get("member_k1") or {}
    m2 = k2.get("member") or {}
    rows.setdefault(key, {})[d["gate"]] = dict(
        mat=k1.get("material_mm3"), ev=d["evaluations"], s=d["wall_s"], st=d["status"],
        sf=(40.0 / k1["p995"]) if k1 else None, bnd=k1.get("zone_ball_boundary_d"),
        m1=m1.get("QV_w3"), m2=m2.get("QV_w3"), mx2=m2.get("max"))


def f(v, n=0):
    return "-" if v is None else f"{v:.{n}f}"


gates = ["current", "unified_zball", "unified"]
print("| 題材 | 荷重N | h | w | " + " | ".join(f"{g} mm³" for g in gates) + " | 統合zball差 | 部材のみ差 | 評価回数 | 部材域Q k1 (現/zb/部) | 部材域Q k2 (現/zb/部) | 境界球平均 (現/zb/部) |")
print("|" + "---|" * 13)
for key in sorted(rows):
    r = rows[key]
    c = r.get("current", {})
    cells = []
    for g in gates:
        x = r.get(g, {})
        cells.append(f(x.get("mat")) if x.get("st") == "feasible" else (x.get("st") or "-"))
    def diff(g):
        x = r.get(g, {})
        return f((x["mat"] / c["mat"] - 1) * 100, 1) + "%" if c.get("mat") and x.get("mat") else "-"
    ev = "/".join(str(r.get(g, {}).get("ev", "-")) for g in gates)
    m1 = "/".join(f(r.get(g, {}).get("m1"), 2) for g in gates)
    m2 = "/".join(f(r.get(g, {}).get("m2"), 2) for g in gates)
    bd = "/".join(f(r.get(g, {}).get("bnd"), 2) for g in gates)
    print(f"| {key[0]} | {key[1]:g} | {key[2]:g} | {key[3]:g} | " + " | ".join(cells) + f" | {diff('unified_zball')} | {diff('unified')} | {ev} | {m1} | {m2} | {bd} |")

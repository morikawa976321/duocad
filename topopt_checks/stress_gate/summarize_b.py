"""段階Bの探索結果(output/stress_gate_20261008/searches/*.json)を1表にする。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\summarize_b.py [名前の部分一致...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "output" / "stress_gate_20261008" / "searches"


def fmt(v, nd=2):
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def main() -> int:
    keys = sys.argv[1:]
    rows = []
    for p in sorted(OUT.glob("*.json")):
        if keys and not any(k in p.stem for k in keys):
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        k1 = d.get("k1") or {}
        mem = {k: (d.get(k) or {}).get("member") or {} for k in ("k2", "k3")}
        m1 = k1.get("member_k1") or {}
        rows.append([p.stem, d["status"], d["termination"], d["evaluations"], round(d["wall_s"]),
                     fmt(k1.get("material_mm3"), 0), fmt(40.0 / k1["p995"] if k1 else None),
                     fmt(k1.get("QV_w3")), fmt(k1.get("QV_w3_out2mm")), fmt(k1.get("hot_mm3"), 0),
                     fmt(d.get("baseline_exceed_mm3"), 0),
                     fmt(m1.get("QV_w3")), fmt(mem["k2"].get("QV_w3")), fmt(mem["k3"].get("QV_w3")),
                     fmt(mem["k3"].get("max")), fmt(mem["k3"].get("over20_mm3"), 1)])
    head = ["name", "status", "term", "eval", "s", "mat_mm3", "SF_p995", "QVw3", "QVw3_out2", "hot", "E_full",
            "mem_QV_k1", "mem_QV_k2", "mem_QV_k3", "mem_max_k3", "mem>20_k3"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        print("| " + " | ".join(str(x) for x in r) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

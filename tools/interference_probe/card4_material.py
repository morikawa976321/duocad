r"""カード4(接触と微小重なりを一覧に出すか)の判断材料: PC ケースで tiny_overlap と contact に入った組の中身。

    .\.venv\Scripts\python.exe -m tools.interference_probe.card4_material <PC ケースの STEP>

tiny_overlap は領域の有向境界箱の3辺(薄い・中・長い)、contact は接触の種類と面積を出す。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from .pipeline import classify
from .real_docs import load_step


def main():
    solids, _ = load_step(Path(sys.argv[1]))
    res = classify([s.shape for s in solids], 0.0, [s.name for s in solids])
    by = {}
    for r in res["pairs"]:
        by.setdefault(r["relation"], []).append(r)
    for r in sorted(by.get("overlap", []), key=lambda r: -r["volume"]):
        sides = "×".join(f"{x:.3g}" for x in r["region_detail"][0]["obb_sides"])
        print(f"overlap {r['a']}-{r['b']} V={r['volume']:.4g}mm3 2V/A={r['thickness']:.3g} 有向箱 {sides}")
    print("-- tiny_overlap --")
    for r in sorted(by.get("tiny_overlap", []), key=lambda r: -r["volume"]):
        regions = "; ".join("×".join(f"{x:.2g}" for x in d["obb_sides"]) for d in r["region_detail"][:3])
        more = f" ほか{r['regions'] - 3}領域" if r["regions"] > 3 else ""
        print(f"tiny {r['a']}-{r['b']} V={r['volume']:.3g}mm3 2V/A={r['thickness']:.2g} 領域{r['regions']} 有向箱 {regions}{more}")
    print("-- contact --")
    contacts = by.get("contact", [])
    kinds = Counter(r["kind"] for r in contacts)
    areas = sorted(r["area"] for r in contacts if r["kind"] == "face")
    print(f"contact 種類 {dict(kinds)} 面の接触の面積 最小 {areas[0]:.3g} 中央 {areas[len(areas) // 2]:.3g} 最大 {areas[-1]:.3g} mm2")
    tiny_pairs = {(r["a"], r["b"]) for r in by.get("tiny_overlap", [])}
    print(f"微小重なりの組 {len(tiny_pairs)} / 接触の組 {len(contacts)} / 触れている部品 "
          f"{len({p for r in contacts + by.get('tiny_overlap', []) + by.get('overlap', []) for p in (r['a'], r['b'])})} / 全 {len(solids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

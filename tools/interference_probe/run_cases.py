"""cases.py の各組に方式候補を全部かけ、分類・体積・距離・秒を表にする。

    .\\.venv\\Scripts\\python.exe -m tools.interference_probe.run_cases [--deviation 0.01] [--json out.json]

`-m` で走らせる(cwd が sys.path の先頭に入り、ワークツリーの duocad を読む。docs/DEV_NOTES.md「ワークツリー」)。
"""
from __future__ import annotations

import argparse
import json
import math
import sys

from . import methods as M
from .cases import build_cases

#: 試作で置いた分類のしきい値(実測の結果ではなく、比べるための仮置き)。
CONTACT_TOL = 1e-4          # 最短距離がこれ以下なら「触れている」
VOLUME_TOL_REL = 1e-9       # 重なり体積が min(体積A, 体積B) のこの割合以下なら 0 とみなす


def classify_exact(dist, vol, va, vb):
    """dist(DistShapeShape)と common の体積から3分類。"""
    if vol > VOLUME_TOL_REL * min(va, vb):
        return "overlap"
    if dist <= CONTACT_TOL:
        return "contact"
    return "clear"


def classify_manifold(vol, gap, va, vb):
    if vol > VOLUME_TOL_REL * min(va, vb):
        return "overlap"
    if gap <= CONTACT_TOL:
        return "contact"
    return "clear"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--deviation", type=float, default=0.01, help="manifold 経路の三角形分割の線形偏差(mm)")
    ap.add_argument("--json")
    ap.add_argument("--only")
    args = ap.parse_args(argv)
    rows = []
    for case in build_cases():
        if args.only and args.only not in case.name:
            continue
        va, vb = M.volume(case.a), M.volume(case.b)
        row = dict(name=case.name, expect=case.expect, exp_volume=case.volume, exp_gap=case.gap)
        c = M.common(case.a, case.b)
        d = M.dist(case.a, case.b)
        row.update(common_volume=c["volume"], common_solids=c["solids"], common_faces=c["faces"],
                   common_warn=c["warnings"], common_err=c["errors"], common_s=c["seconds"],
                   dist=d.get("distance"), dist_inner=d.get("inner"), dist_s=d["seconds"])
        row["exact_class"] = classify_exact(d.get("distance", math.inf), c["volume"], va, vb)
        k = M.checker(case.a, case.b)
        row.update(checker_kinds=k["kinds"], checker_warn=k["warnings"], checker_s=k["seconds"])
        g = M.gf_pairs([case.a, case.b])
        row.update(gf_volume=g["pairs"].get((0, 1), 0.0), gf_warn=g["warnings"], gf_s=g["seconds"])
        try:
            ta = M.tessellated(case.a, args.deviation)
            tb = M.tessellated(case.b, args.deviation)
            ma, mb = M.to_manifold(*ta), M.to_manifold(*tb)
            mr = M.manifold_pair(ma, mb, search=1.0)
            row.update(mf_volume=mr["volume"], mf_gap=mr["min_gap"], mf_s=mr["seconds"],
                       mf_tris=len(ta[1]) + len(tb[1]))
            row["mf_class"] = classify_manifold(mr["volume"], mr["min_gap"], ma.volume(), mb.volume())
        except Exception as exc:
            row.update(mf_error=repr(exc)[:200], mf_class="error")
        p = M.proximity(case.a, case.b, tol=0.2, deflection=args.deviation)
        row.update(prox_pairs=p.get("face_pairs"), prox_value=p.get("proximity"), prox_s=p["seconds"],
                   prox_err=p.get("proximity_error"))
        rows.append(row)
        ok_e = "OK" if row["exact_class"] == case.expect else "NG"
        ok_m = "OK" if row.get("mf_class") == case.expect else "NG"
        print(f"{case.name:28s} expect={case.expect:8s} exact={row['exact_class']:8s}{ok_e} "
              f"mf={row.get('mf_class'):8s}{ok_m} | Vexp={case.volume!s:>10.10} Vcom={c['volume']:.6g} "
              f"Vgf={row['gf_volume']:.6g} Vmf={row.get('mf_volume', float('nan')):.6g} | "
              f"gexp={case.gap:.6g} d={row['dist'] if row['dist'] is not None else float('nan'):.6g} "
              f"inner={row['dist_inner']} gmf={row.get('mf_gap', float('nan')):.6g} | "
              f"chk={row['checker_kinds']} prox={row['prox_pairs']}/{row['prox_value']} | "
              f"t com={c['seconds']*1e3:.1f} dist={d['seconds']*1e3:.1f} gf={g['seconds']*1e3:.1f} "
              f"mf={row.get('mf_s', float('nan'))*1e3:.1f} ms", flush=True)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())

r"""B-rep とメッシュのボディが混ざった文書(形状最適化の結果が入った文書)で、メッシュ経路を測る。読むだけ。

    .\.venv\Scripts\python.exe -m tools.interference_probe.mixed <文書.json> [--clearance 0.2] [--to-brep]

- メッシュを含む組: B-rep は三角形分割(0.01mm)して manifold3d の交差体積と min_gap
- B-rep どうし: pipeline.classify(GF)と、同じ組の manifold 経路
- --to-brep: メッシュを三角形1枚1面の B-rep に直して(mesh_geometry.make_material)GF に入れる時間も測る
"""
from __future__ import annotations

import argparse
import sys
import time
from itertools import combinations
from pathlib import Path

import numpy as np

from . import methods as M
from .pipeline import GAP_TOL, THICKNESS_TOL, classify
from .real_docs import load


def mesh_arrays(body):
    from duocad.mesh_geometry import weld_exact
    v = np.asarray(body.mesh.positions, dtype=np.float64).reshape(-1, 3)
    f = np.asarray(body.mesh.indices, dtype=np.int64).reshape(-1, 3)
    return weld_exact(v[f])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("doc")
    ap.add_argument("--clearance", type=float, default=0.2)
    ap.add_argument("--deviation", type=float, default=0.01)
    ap.add_argument("--to-brep", action="store_true")
    ap.add_argument("--no-gf", action="store_true", help="--to-brep で変換だけ測り、GF に入れない")
    args = ap.parse_args(argv)
    doc, t_replay = load(Path(args.doc))
    bodies = list(doc.bodies)
    print(f"replay {t_replay:.1f}s bodies:", [(b.name, b.kind) for b in bodies], flush=True)

    t0 = time.perf_counter()
    mfs, boxes, vols = [], [], []
    for b in bodies:
        if b.kind == "mesh":
            v, f = mesh_arrays(b)
        else:
            v, f = M.tessellated(b.shape, args.deviation)
        m = M.to_manifold(v, f)
        mfs.append(m)
        boxes.append((v.min(axis=0), v.max(axis=0)))
        vols.append(m.volume())
        print(f"  {b.name:24s} {b.kind:5s} tris={len(f):7d} V={m.volume():.1f}", flush=True)
    print(f"to manifold {time.perf_counter() - t0:.2f}s", flush=True)

    t0 = time.perf_counter()
    for i, j in combinations(range(len(bodies)), 2):
        if M.bbox_gap(boxes[i], boxes[j]) > args.clearance + args.deviation * 2:
            continue
        # min_gap は [0, search] に丸める。search を 0 にすると全部 0(=接触)になる。
        r = M.manifold_pair(mfs[i], mfs[j], search=max(args.clearance * 2, 1.0))
        inter = mfs[i] ^ mfs[j]
        area = inter.surface_area()
        thick = 2 * r["volume"] / area if area else 0.0
        cls = ("overlap" if thick > THICKNESS_TOL else "contact" if r["min_gap"] <= 1e-6
               else "near" if r["min_gap"] < args.clearance - GAP_TOL else "clear")
        print(f"  {bodies[i].name}({bodies[i].kind}) - {bodies[j].name}({bodies[j].kind}): {cls} "
              f"V={r['volume']:.4g} thick={thick:.3g} gap={r['min_gap']:.4g} ({r['seconds'] * 1e3:.0f}ms)", flush=True)
    print(f"mesh pairs {time.perf_counter() - t0:.2f}s", flush=True)

    solids = [b for b in bodies if b.kind == "solid"]
    if len(solids) >= 2:
        res = classify([b.shape for b in solids], args.clearance, [b.name for b in solids])
        print("exact B-rep pairs:", {k: res[k] for k in ("cand", "gf_s", "dist_s", "total_s")},
              [(p["a"], p["b"], p["relation"], p.get("clearance")) for p in res["pairs"]
               if p["relation"] != "separated" or p.get("clearance") not in (None, "ok")], flush=True)

    if args.to_brep:
        from duocad.mesh_geometry import make_material, topology
        for b in bodies:
            if b.kind != "mesh":
                continue
            v, f = mesh_arrays(b)
            t0 = time.perf_counter()
            try:
                _, components = topology(v, f)
                solid, info = make_material(v, f, components, check_intersections=False)
                if solid is None:
                    raise RuntimeError(f"make_material が立体を返さない: {info}")
                t_conv = time.perf_counter() - t0
                print(f"  to-brep {b.name}: convert {t_conv:.1f}s faces={M.count(solid, M.TopAbs_FACE)} "
                      f"V={M.volume(solid):.1f}(メッシュ {vols[bodies.index(b)]:.1f})", flush=True)
                if args.no_gf:
                    continue
                others = [s.shape for s in solids]
                t1 = time.perf_counter()
                g = M.gf_relations([solid, *others], pairs=[(0, k + 1) for k in range(len(others))])
                print(f"  to-brep {b.name}: convert {t_conv:.1f}s, GF with {len(others)} solids "
                      f"{time.perf_counter() - t1:.1f}s errors={g['errors']} warn={g['warnings']} "
                      f"{[(solids[k].name, r['kind']) for (_, k1), r in g['relations'].items() for k in [k1 - 1]]}",
                      flush=True)
            except Exception as exc:
                print(f"  to-brep {b.name}: failed after {time.perf_counter() - t0:.1f}s {exc!r:.120}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

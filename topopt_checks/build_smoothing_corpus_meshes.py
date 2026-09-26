"""平滑化の検証材料: `output/smoothing_corpus/voxel/<条件名>.npz` の各マスクから、製品と同じメッシュ化の STL を作る。

    .\\.venv\\Scripts\\python.exe topopt_checks\\build_smoothing_corpus_meshes.py            # 全条件(再開可・並列)
    .\\.venv\\Scripts\\python.exe topopt_checks\\build_smoothing_corpus_meshes.py --one F0200_h2.4_w2
    .\\.venv\\Scripts\\python.exe topopt_checks\\build_smoothing_corpus_meshes.py --manifest  # mesh_manifest.json を作り直して数える
    .\\.venv\\Scripts\\python.exe topopt_checks\\build_smoothing_corpus_meshes.py --summary   # 自己交差の集計表

出力(`output/smoothing_corpus/`。gitignore 済み):
  marching_cubes/<条件名>.stl  平滑化前(`verts_raw`)と `faces`。ワールド座標[mm]のバイナリSTL
  smoothed/<条件名>.stl        平滑化後(`verts_smooth`)と同じ `faces`
  mesh_records/<条件名>.json   1条件の記録(**最後に書く**ので、あれば条件は確定済み)
  mesh_manifest.json           mesh_records/ を voxel/manifest.json の条件の順に並べたもの

`mesh.build_surface_from_mask` は既定値のまま呼ぶ(実 spacing を渡さない)。ワールド座標への変換は
`optimize_run._mesh_to_world` と同じ `origin + verts * spacing`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "output" / "smoothing_corpus"
MAX_CENTROIDS = 20
DEFAULT_WORKERS = 6
_ID = np.dtype([("normal", "<f4", 3), ("verts", "<f4", (3, 3)), ("attr", "<u2")])


def _names() -> list[str]:
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    return [row["name"] for row in manifest["rows"] if row["status"] == "ok"]


def read_stl(path: Path) -> np.ndarray:
    """バイナリSTLの三角形 (N,3,3) float32。"""
    raw = path.read_bytes()
    count = int(np.frombuffer(raw, "<u4", 1, 80)[0])
    if len(raw) != 84 + 50 * count:
        raise ValueError(f"{path.name}: バイナリSTLの長さが三角形数と合わない")
    return np.frombuffer(raw, _ID, count, 84)["verts"]


def _check_voxel_stl(name: str, mask: np.ndarray, origin: np.ndarray, spacing: float) -> dict:
    """ボクセルSTL と npz が同じマスクを表すこと: 体積が材料セル数×要素体積、外接箱が材料セルの外接箱。"""
    tri = read_stl(OUT / "voxel" / f"{name}.stl").astype(np.float64)
    volume = float(np.einsum("ij,ij->", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])) / 6.0)
    cells = int(mask.sum())
    expected = cells * spacing ** 3
    idx = np.argwhere(mask)
    lo, hi = origin + idx.min(axis=0) * spacing, origin + (idx.max(axis=0) + 1) * spacing
    box_error = float(max(np.abs(tri.reshape(-1, 3).min(axis=0) - lo).max(),
                          np.abs(tri.reshape(-1, 3).max(axis=0) - hi).max()))
    ok = abs(volume - expected) <= 1e-4 * expected and box_error <= 1e-3 * spacing
    return dict(mask_cells=cells, voxel_stl_volume_mm3=volume, expected_volume_mm3=expected,
                volume_rel_error=(volume - expected) / expected, bbox_max_error_mm=box_error, same_mask=bool(ok))


def _intersections(verts: np.ndarray, faces: np.ndarray) -> dict:
    from duocad.mesh_intersections import triangle_intersections

    result = triangle_intersections(verts, faces)
    pairs = result["pairs"]
    seen, centroids = set(), []
    for i, j, kind in pairs:
        for t in (i, j):
            if t not in seen and len(centroids) < MAX_CENTROIDS:
                centroids.append(dict(triangle=int(t), kind=kind,
                                      centroid_mm=verts[faces[t]].mean(axis=0).tolist()))
            seen.add(t)
    return dict(counts=result["counts"], pairs=len(pairs), candidates=result["candidates"],
                triangles_involved=len(seen), centroids=centroids)


def _topology(verts: np.ndarray, faces: np.ndarray) -> dict:
    from duocad.mesh_geometry import TOPOLOGY_DEFECTS, topology

    info, _ = topology(verts, faces)
    out = {key: int(info[key]) for key in TOPOLOGY_DEFECTS}
    out["boundary_components"] = int(info["boundary_components"])
    return out


def build_one(name: str) -> dict:
    from duocad.topopt import mesh

    t_start = time.perf_counter()
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    data = np.load(OUT / "voxel" / f"{name}.npz")
    mask, origin, spacing = data["mask"], np.asarray(data["origin"], float), float(data["spacing"])
    voxel = _check_voxel_stl(name, mask, origin, spacing)

    t0 = time.perf_counter()
    surface = mesh.build_surface_from_mask(mask)
    mesh_seconds = time.perf_counter() - t0
    faces = surface.faces
    raw_w = origin + np.asarray(surface.verts_raw, float) * spacing
    smooth_w = origin + np.asarray(surface.verts_smooth, float) * spacing
    mesh.write_stl(OUT / "marching_cubes" / f"{name}.stl", raw_w, faces)
    mesh.write_stl(OUT / "smoothed" / f"{name}.stl", smooth_w, faces)

    move = np.linalg.norm(np.asarray(surface.verts_smooth, float) - np.asarray(surface.verts_raw, float), axis=1)
    volumes = dict(raw_mm3=mesh.mesh_volume(raw_w, faces), smooth_mm3=mesh.mesh_volume(smooth_w, faces))
    t0 = time.perf_counter()
    topo = dict(raw=_topology(raw_w, faces), smooth=_topology(smooth_w, faces))
    topo_seconds = time.perf_counter() - t0
    t0 = time.perf_counter()
    inter = dict(raw=_intersections(raw_w, faces), smooth=_intersections(smooth_w, faces))
    inter_seconds = time.perf_counter() - t0
    record = dict(
        name=name, status="ok", started_at=started,
        finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        grid=dict(origin=origin.tolist(), spacing=spacing, shape=list(mask.shape)),
        voxel_check=voxel,
        marching_cubes_stl=f"marching_cubes/{name}.stl", smoothed_stl=f"smoothed/{name}.stl",
        triangles=int(len(faces)), vertices=int(len(surface.verts_raw)),
        volume_mm3=volumes,
        volume_smooth_over_raw=volumes["smooth_mm3"] / volumes["raw_mm3"],
        vertex_move=dict(max_cells=float(move.max()), p99_cells=float(np.percentile(move, 99)),
                         max_mm=float(move.max() * spacing), p99_mm=float(np.percentile(move, 99) * spacing)),
        topology=topo, intersections=inter,
        seconds=dict(build_surface=mesh_seconds, topology=topo_seconds, intersections=inter_seconds,
                     total=time.perf_counter() - t_start),
    )
    path = OUT / "mesh_records" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return record


def _done(name: str) -> bool:
    return (OUT / "mesh_records" / f"{name}.json").exists()


def _work(name: str) -> tuple[str, str | None]:
    try:
        rec = build_one(name)
        return name, f"ok {rec['seconds']['total']:.0f}s cross_raw={rec['intersections']['raw']['counts']['cross']} " \
                     f"cross_smooth={rec['intersections']['smooth']['counts']['cross']}"
    except Exception as error:  # 1条件で全体を止めない
        return name, f"ERROR {type(error).__name__}: {error}"


def build_manifest() -> dict:
    names = _names()
    missing = [n for n in names if not _done(n)]
    rows = [json.loads((OUT / "mesh_records" / f"{n}.json").read_text(encoding="utf-8"))
            for n in names if _done(n)]
    manifest = dict(generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    conditions_total=len(names), built=len(rows), missing=missing, rows=rows)
    tmp = OUT / "mesh_manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, OUT / "mesh_manifest.json")
    return manifest


def _parse(name: str) -> tuple[int, float, int]:
    f, h, w = name.split("_")
    return int(f[1:]), float(h[1:]), int(w[1:])


def summary() -> None:
    rows = json.loads((OUT / "mesh_manifest.json").read_text(encoding="utf-8"))["rows"]
    hit = []
    for r in rows:
        for phase in ("raw", "smooth"):
            c = r["intersections"][phase]
            if c["pairs"]:
                hit.append((r["name"], phase, c))
    print(f"自己交差あり: 平滑化前 {len({n for n, p, _ in hit if p == 'raw'})} 条件 / "
          f"平滑化後 {len({n for n, p, _ in hit if p == 'smooth'})} 条件 (全{len(rows)}条件)")
    for phase in ("raw", "smooth"):
        print(f"\n[{phase}] 条件別(種類別件数)")
        for n, p, c in hit:
            if p == phase:
                print(f"  {n}: {c['counts']}")
    for axis, label, pick in (("要素サイズ[mm]", "size", 1), ("最小幅[セル]", "width", 2), ("荷重[N]", "load", 0)):
        print(f"\n{label} 別 (条件数 / 平滑化前に交差 / 平滑化後に交差 / 平滑化後の交差ペア合計)")
        keys = sorted({_parse(r["name"])[pick] for r in rows})
        for k in keys:
            sel = [r for r in rows if _parse(r["name"])[pick] == k]
            print(f"  {axis}={k}: {len(sel)} / {sum(1 for r in sel if r['intersections']['raw']['pairs'])} / "
                  f"{sum(1 for r in sel if r['intersections']['smooth']['pairs'])} / "
                  f"{sum(r['intersections']['smooth']['pairs'] for r in sel)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--one")
    parser.add_argument("--manifest", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()
    if args.summary:
        summary()
        return 0
    if args.manifest:
        m = build_manifest()
        print(f"built {m['built']}/{m['conditions_total']} missing={m['missing']}")
        return 0 if not m["missing"] else 1
    names = [args.one] if args.one else [n for n in _names() if not _done(n)]
    print(f"{len(names)} 条件を作る", flush=True)
    failed = []
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for name, message in pool.map(_work, names):
            print(f"[{name}] {message}", flush=True)
            if message and message.startswith("ERROR"):
                failed.append(name)
    m = build_manifest()
    print(f"built {m['built']}/{m['conditions_total']} missing={m['missing']} failed={failed}")
    return 0 if not m["missing"] else 1


if __name__ == "__main__":
    sys.exit(main())

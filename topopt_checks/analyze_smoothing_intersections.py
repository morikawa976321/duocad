"""平滑化後の自己交差が起きるボクセルの局所形を、検証材料の全条件で集計する。用語・結果は docs/TOPOPT_ACCEPTANCE.md「平滑化(Taubin)後の自己交差」。

    .venv/Scripts/python.exe -X utf8 topopt_checks/analyze_smoothing_intersections.py            # 全条件(再開可・並列)
    ... --one F1000_h2.4_w2 / --force / --workers N
    ... --report        # 集計表(markdown)
    ... --synthetic     # 合成マスクでの再現・対照
    ... --locality      # 実データの管を切り出して交差が残るか
    ... --sensitivity   # 管の周りのセルを1個ずつ反転して結果が変わる数

入力は `output/smoothing_corpus/voxel/<条件名>.npz` の `mask`。`mesh.build_surface_from_mask` を既定値のまま呼び、要素添字空間
(セル `i` は `[i, i+1]`)で扱う。ワールド座標は `origin + 添字座標 * spacing`。
出力(`output/smoothing_corpus/`。gitignore 済み): intersection_records/<条件名>.json(1条件の原本)・intersection_sites.json(交差箇所1件1行)・
intersection_synthetic.json・intersection_locality.json・intersection_sensitivity.json。

二値マスクの marching cubes は境界面の中心に頂点を1つずつ置くので、境界面と頂点が1対1に対応する(`_face_vertex_tables` が検査する)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
from scipy import ndimage

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "output" / "smoothing_corpus"
RECORDS = OUT / "intersection_records"
SITES = OUT / "intersection_sites.json"
DEFAULT_WORKERS = 6

# 分析者が置いた値(実測ではない)。
CLUSTER_MERGE_CELLS = 3.0  # 平滑化前の位置がこれ以内の交差の塊は1箇所にまとめる
GAP_EDGES = [0.0, 0.1, 0.25, 0.5, 0.75]  # 壁の間隔の区切り[要素]。下から <0 / [0,0.1) / [0.1,0.25) / [0.25,0.5) / [0.5,0.75) / >=0.75
GAP_LABELS = ["<0", "0-0.1", "0.1-0.25", "0.25-0.5", "0.5-0.75", ">=0.75"]
SURROUND_CAP = 8  # 管の周りの材料の厚さを数える上限(セル)


def _bin(gaps: np.ndarray) -> np.ndarray:
    return np.searchsorted(np.asarray(GAP_EDGES), gaps, side="right")


def _names() -> list[str]:
    manifest = json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))
    return [row["name"] for row in manifest["rows"] if row["status"] == "ok"]


def _parse(name: str) -> tuple[int, float, int]:
    load, size, width = name.split("_")
    return int(load[1:]), float(size[1:]), int(width[1:])


def _neighbour(padded: np.ndarray, shape: tuple, axis: int, sign: int) -> np.ndarray:
    sl = tuple(slice(1 + (sign if ax == axis else 0), 1 + (sign if ax == axis else 0) + shape[ax]) for ax in range(3))
    return padded[sl]


def _face_vertex_tables(verts_raw: np.ndarray, shape: tuple) -> list[np.ndarray]:
    """軸 a ごとに、境界面(セル添字 n-1 と n の間、他の2軸のセル添字 i,j)→ 平滑化前の頂点添字。無ければ -1。"""
    frac = verts_raw - np.floor(verts_raw + 1e-6)
    is_int = np.abs(frac) < 1e-6
    if not (is_int.sum(axis=1) == 1).all():
        raise ValueError("marching cubes の頂点が面の中心に無い(二値マスクの前提が崩れている)")
    axis = is_int.argmax(axis=1)
    tables = []
    for a in range(3):
        other = [ax for ax in range(3) if ax != a]
        table = -np.ones((shape[a] + 1, shape[other[0]], shape[other[1]]), dtype=np.int64)
        sel = np.flatnonzero(axis == a)
        n = np.rint(verts_raw[sel, a]).astype(int)
        i = np.floor(verts_raw[sel, other[0]]).astype(int)
        j = np.floor(verts_raw[sel, other[1]]).astype(int)
        if ((table[n, i, j]) != -1).any():
            raise ValueError("1つの境界面に頂点が2つ以上ある")
        table[n, i, j] = sel
        tables.append(table)
    return tables


def _material_run(padded: np.ndarray, shape: tuple, cells: np.ndarray, axis: int, sign: int, cap: int) -> np.ndarray:
    count = np.zeros(len(cells), dtype=int)
    alive = np.ones(len(cells), dtype=bool)
    for step in range(1, cap + 1):
        pos = cells.copy()
        pos[:, axis] += sign * step
        inside = ((pos >= -1) & (pos <= np.asarray(shape))).all(axis=1)
        val = np.zeros(len(cells), dtype=bool)
        val[inside] = padded[tuple((pos[inside] + 1).T)]
        alive &= val
        count += alive
    return count


def _open_extension(padded: np.ndarray, shape: tuple, cell: np.ndarray, axis: int, sign: int) -> int:
    """管の開いた端の外へ、側面4方向のうち3方向以上が材料の空セルが何個続くか。"""
    side = [ax for ax in range(3) if ax != axis]
    count = 0
    pos = cell.copy()
    while True:
        pos[axis] += sign
        if not ((pos >= 0) & (pos < np.asarray(shape))).all() or padded[tuple(pos + 1)]:
            return count
        mat = 0
        for ax in side:
            for d in (-1, 1):
                q = pos.copy()
                q[ax] += d
                mat += bool(padded[tuple(q + 1)])
        if mat < 3:
            return count
        count += 1


def analyze_mask(mask: np.ndarray) -> dict:
    from duocad.mesh_intersections import triangle_intersections
    from duocad.topopt import mesh

    shape = tuple(int(v) for v in mask.shape)
    surface = mesh.build_surface_from_mask(mask)
    verts_raw, verts_smooth, faces = surface.verts_raw, surface.verts_smooth, surface.faces
    inter = triangle_intersections(verts_smooth, faces)
    tables = _face_vertex_tables(verts_raw, shape)
    padded = np.pad(mask, 1)
    empty = ~mask
    move = np.linalg.norm(verts_smooth - verts_raw, axis=1)

    pin = [empty & _neighbour(padded, shape, a, -1) & _neighbour(padded, shape, a, 1) for a in range(3)]

    def gap(a: int, cells: np.ndarray) -> np.ndarray:
        other = [ax for ax in range(3) if ax != a]
        lo = tables[a][cells[:, a], cells[:, other[0]], cells[:, other[1]]]
        hi = tables[a][cells[:, a] + 1, cells[:, other[0]], cells[:, other[1]]]
        if (lo < 0).any() or (hi < 0).any():
            raise ValueError("挟まれたセルの面に頂点が無い")
        return verts_smooth[hi, a] - verts_smooth[lo, a]

    runs = []
    cell_run = {}
    for a in range(3):
        b, c = [ax for ax in range(3) if ax != a]
        tube = pin[b] & pin[c] & ~pin[a]
        structure = np.zeros((3, 3, 3), dtype=bool)
        structure[1, 1, 1] = True
        structure[tuple(np.array([1, 1, 1]) - np.eye(3, dtype=int)[a])] = True
        structure[tuple(np.array([1, 1, 1]) + np.eye(3, dtype=int)[a])] = True
        labels, count = ndimage.label(tube, structure=structure)
        if count == 0:
            continue
        cells_all = np.argwhere(tube)
        lab_all = labels[tuple(cells_all.T)]
        order = np.argsort(lab_all, kind="stable")
        cells_all, lab_all = cells_all[order], lab_all[order]
        bounds = np.flatnonzero(np.diff(lab_all)) + 1
        for chunk in np.split(np.arange(len(cells_all)), bounds):
            cells = cells_all[chunk]
            cells = cells[np.argsort(cells[:, a])]
            length = len(cells)
            ends = []
            for sign, cell in ((-1, cells[0]), (1, cells[-1])):
                beyond = cell.copy()
                beyond[a] += sign
                ends.append("止" if padded[tuple(beyond + 1)] else "開")
            extension = []
            for sign, cell in ((-1, cells[0]), (1, cells[-1])):
                extension.append(_open_extension(padded, shape, cell, a, sign) if ends[len(extension)] == "開" else 0)
            gb, gc = gap(b, cells), gap(c, cells)
            per_cell = np.minimum(gb, gc)
            surround = min(int(_material_run(padded, shape, cells, ax, s, SURROUND_CAP).min()) for ax in (b, c) for s in (-1, 1))
            diag_empty = np.zeros(length, dtype=int)
            for sb in (-1, 1):
                for sc in (-1, 1):
                    pos = cells.copy()
                    pos[:, b] += sb
                    pos[:, c] += sc
                    diag_empty += ~padded[tuple((pos + 1).T)]
            interior = per_cell[1:-1] if length > 2 else np.array([])
            index = len(runs)
            runs.append(dict(
                axis="xyz"[a], length=length, end_lo=ends[0], end_hi=ends[1],
                first_cell=[int(v) for v in cells[0]], gap_min=float(per_cell.min()),
                gap_interior_min=float(interior.min()) if len(interior) else None,
                gap_end_min=float(min(per_cell[0], per_cell[-1])), surround_min=surround,
                ext_lo=extension[0], ext_hi=extension[1],
                diag_empty_max=int(diag_empty.max()), diag_empty_cells=int((diag_empty > 0).sum())))
            for cell in cells:
                cell_run[tuple(int(v) for v in cell)] = index

    npin = sum(p.astype(int) for p in pin)
    slit_cells = np.argwhere(npin == 1)
    cavity_cells = np.argwhere(npin == 3)
    slit_class, slit_gap = [], []
    for a in range(3):
        cells = slit_cells[pin[a][tuple(slit_cells.T)]]
        if not len(cells):
            continue
        other = [ax for ax in range(3) if ax != a]
        k = sum(_neighbour(padded, shape, ax, s)[tuple(cells.T)].astype(int) for ax in other for s in (-1, 1))
        slit_class.append(k)
        slit_gap.append(gap(a, cells))
    slit_class = np.concatenate(slit_class) if slit_class else np.zeros(0, dtype=int)
    slit_gap = np.concatenate(slit_gap) if slit_gap else np.zeros(0)
    slit_hist = {str(k): np.bincount(_bin(slit_gap[slit_class == k]), minlength=len(GAP_LABELS)).tolist() for k in (0, 1, 2)}
    slit_min = {str(k): (float(slit_gap[slit_class == k].min()) if (slit_class == k).any() else None) for k in (0, 1, 2)}

    sites = _cluster_sites(inter, faces, verts_raw, verts_smooth, tables, padded, shape, move, runs, cell_run, pin)
    return dict(
        shape=list(shape), material_cells=int(mask.sum()), intersections=inter["counts"], intersection_pairs=len(inter["pairs"]),
        sites=sites, runs=runs, slit_hist=slit_hist, slit_min_gap=slit_min, slit_cells=int(len(slit_cells)),
        cavity_cells=int(len(cavity_cells)), triangles=int(len(faces)))


def _empty_side_cell(verts_raw: np.ndarray, padded: np.ndarray, v: int) -> tuple:
    p = verts_raw[v]
    a = int(np.argmin(np.abs(p - np.rint(p))))
    n = int(np.rint(p[a]))
    cell = np.floor(p).astype(int)
    cell[a] = n
    pos = cell.copy()
    pos[a] = n - 1
    return tuple(int(x) for x in (pos if not padded[tuple(pos + 1)] else cell))


def _cluster_sites(inter, faces, verts_raw, verts_smooth, tables, padded, shape, move, runs, cell_run, pin) -> list[dict]:
    pairs = inter["pairs"]
    if not pairs:
        return []
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _kind in pairs:
        parent[find(i)] = find(j)
    groups: dict[int, list] = {}
    for i, j, kind in pairs:
        groups.setdefault(find(i), []).append((i, j, kind))
    comps = []
    for group in groups.values():
        tris = sorted({t for i, j, _ in group for t in (i, j)})
        verts = np.unique(faces[tris])
        comps.append(dict(pairs=group, tris=tris, verts=verts, centre=verts_raw[verts].mean(axis=0)))
    merged = True
    while merged:
        merged = False
        for x in range(len(comps)):
            for y in range(x + 1, len(comps)):
                if np.linalg.norm(comps[x]["centre"] - comps[y]["centre"]) < CLUSTER_MERGE_CELLS:
                    a, b = comps[x], comps.pop(y)
                    a["pairs"] += b["pairs"]
                    a["tris"] = sorted(set(a["tris"]) | set(b["tris"]))
                    a["verts"] = np.unique(np.concatenate([a["verts"], b["verts"]]))
                    a["centre"] = verts_raw[a["verts"]].mean(axis=0)
                    merged = True
                    break
            if merged:
                break
    sites = []
    for comp in sorted(comps, key=lambda c: tuple(c["centre"])):
        verts = comp["verts"]
        cells = {_empty_side_cell(verts_raw, padded, int(v)) for v in verts}
        hit_runs = sorted({cell_run[c] for c in cells if c in cell_run})
        thin = [c for c in cells if any(pin[a][c] for a in range(3))]
        counts: dict[str, int] = {}
        for _, _, kind in comp["pairs"]:
            counts[kind] = counts.get(kind, 0) + 1
        lo = np.floor(verts_raw[verts].min(axis=0)).astype(int)
        hi = np.floor(verts_raw[verts].max(axis=0)).astype(int)
        sites.append(dict(
            pairs=len(comp["pairs"]), counts=counts, triangles=len(comp["tris"]), vertices=int(len(verts)),
            centre_cell=[round(float(x), 3) for x in comp["centre"]], cell_bbox=[lo.tolist(), hi.tolist()],
            void_cells=len(cells), thin_void_cells=len(thin), run_ids=hit_runs,
            near_grid_edge=bool((lo <= 1).any() or (hi >= np.asarray(shape) - 2).any()),
            move_max_cells=round(float(move[verts].max()), 4), move_mean_cells=round(float(move[verts].mean()), 4)))
    return sites


def _mask_sha256(name: str) -> str:
    return hashlib.sha256(np.ascontiguousarray(np.load(OUT / "voxel" / f"{name}.npz")["mask"]).tobytes()).hexdigest()


def _stale(name: str) -> bool:
    """記録が無い、または入力のマスクが記録の後で変わっている。分析コードの変更は検出しない(--force で作り直す)。"""
    path = RECORDS / f"{name}.json"
    return not path.is_file() or json.loads(path.read_text(encoding="utf-8")).get("mask_sha256") != _mask_sha256(name)


def analyze_condition(name: str) -> dict:
    data = np.load(OUT / "voxel" / f"{name}.npz")
    mask, origin, spacing = data["mask"], data["origin"], float(data["spacing"])
    t0 = time.perf_counter()
    mask_sha256 = _mask_sha256(name)
    result = analyze_mask(mask)
    load, size, width = _parse(name)
    manifest_row = next(r for r in json.loads((OUT / "mesh_manifest.json").read_text(encoding="utf-8"))["rows"] if r["name"] == name)
    expected = manifest_row["intersections"]["smooth"]["counts"]
    for site in result["sites"]:
        site["centre_world_mm"] = [round(float(x), 3) for x in origin + np.asarray(site["centre_cell"]) * spacing]
        site["move_max_mm"] = round(site["move_max_cells"] * spacing, 4)
    return dict(
        name=name, mask_sha256=mask_sha256, load_n=load, element_size_mm=size, min_width_cells=width, spacing_mm=spacing, origin=[float(x) for x in origin],
        counts_match_mesh_manifest=result["intersections"] == expected, seconds=round(time.perf_counter() - t0, 1), **result)


def _load_records() -> list[dict]:
    records = [json.loads((RECORDS / f"{n}.json").read_text(encoding="utf-8")) for n in _names()]
    stale = [r["name"] for r in records if r.get("mask_sha256") != _mask_sha256(r["name"])]
    if stale:
        raise ValueError(f"入力のマスクが記録の後で変わっている(--force で作り直す): {stale}")
    for record in records:
        hit = {i for site in record["sites"] for i in site["run_ids"]}
        for i, run in enumerate(record["runs"]):
            run["hit"] = i in hit
            run["ends"] = run["end_lo"] + run["end_hi"]
    return records


def _md(header: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _end_class(run: dict) -> str:
    return {"開開": "開-開(貫通)", "止止": "止-止(閉じた管)"}.get(run["ends"], "止-開(止まり)")


def report() -> None:
    records = _load_records()
    print(f"条件 {len(records)} 件・交差箇所 {sum(len(r['sites']) for r in records)}・交差の組 {sum(r['intersection_pairs'] for r in records)}\n")
    print("### 軸ごとの集計(交差した箇所・組と、管の数・壁の間隔 0.1 要素未満に潰れた管の数)\n")
    for axis, label in (("element_size_mm", "要素サイズ[mm]"), ("min_width_cells", "最小幅[セル]"), ("load_n", "荷重[N]")):
        rows = []
        for value in sorted({r[axis] for r in records}):
            group = [r for r in records if r[axis] == value]
            runs = [run for r in group for run in r["runs"]]
            rows.append([value, len(group), sum(len(r["sites"]) for r in group), sum(r["intersection_pairs"] for r in group), len(runs),
                         sum(1 for run in runs if run["gap_min"] < GAP_EDGES[1]), sum(1 for run in runs if run["hit"])])
        print(_md([label, "条件数", "交差箇所", "交差の組", "管の列", "うち間隔<0.1", "うち交差"], rows), "\n")
    print("### 交差した箇所\n")
    rows = []
    for r in records:
        for site in r["sites"]:
            runs = [r["runs"][i] for i in site["run_ids"]]
            main = min(runs, key=lambda run: run["gap_min"]) if runs else None
            rows.append([r["name"], site["pairs"], site["counts"], main["axis"] if main else "-",
                         main["length"] if main else "-", main["ends"] if main else "-", f"{main['gap_min']:.3f}" if main else "-",
                         main["surround_min"] if main else "-", site["near_grid_edge"], site["centre_world_mm"]])
    print(_md(["条件", "組", "種類", "管の軸", "長さ", "端", "壁の間隔の最小", "周りの材料の厚さの最小", "設計空間の縁", "ワールド座標[mm]"], rows), "\n")
    print("### 管の列: 端の種類 × 長さ\n")
    table: dict[tuple, list] = {}
    for record in records:
        for run in record["runs"]:
            table.setdefault((_end_class(run), min(run["length"], 6)), []).append(run)
    rows = []
    for key in sorted(table):
        runs = table[key]
        gaps = sorted(run["gap_min"] for run in runs)
        rows.append([key[0], "6以上" if key[1] == 6 else key[1], len(runs), sum(run["hit"] for run in runs),
                     f"{gaps[0]:.3f}", f"{gaps[len(gaps) // 2]:.3f}", f"{gaps[-1]:.3f}"])
    print(_md(["端", "長さ[セル]", "列数", "交差した列", "間隔 最小", "間隔 中央値", "間隔 最大"], rows), "\n")
    print("### 管の列の壁の間隔の分布(区切りは分析者が置いた値。括弧内は交差した列の数)\n")
    rows = []
    for key in sorted(table):
        gaps = np.array([run["gap_min"] for run in table[key]])
        hit = np.array([run["hit"] for run in table[key]])
        hist = np.bincount(_bin(gaps), minlength=len(GAP_LABELS))
        hit_hist = np.bincount(_bin(gaps[hit]), minlength=len(GAP_LABELS))
        rows.append([key[0], "6以上" if key[1] == 6 else key[1]] + [f"{h}({x})" if x else str(h) for h, x in zip(hist, hit_hist)])
    print(_md(["端", "長さ"] + GAP_LABELS, rows), "\n")
    print("### 溝・空洞(セル単位。断面4方向のうち材料が k 個)の壁の間隔の分布\n")
    rows = []
    for k, name in (("0", "k=0 両側が開いた薄い隙間"), ("1", "k=1"), ("2", "k=2 隣り合う2方向が材料(隅の溝)")):
        hist = np.sum([r["slit_hist"][k] for r in records], axis=0)
        mins = [r["slit_min_gap"][k] for r in records if r["slit_min_gap"][k] is not None]
        rows.append([name, int(hist.sum())] + [int(h) for h in hist] + [f"{min(mins):.3f}" if mins else "-"])
    print(_md(["形", "セル数"] + GAP_LABELS + ["間隔 最小"], rows), "\n")
    other = sum(1 for r in records for s in r["sites"] if not s["run_ids"])
    print(f"空洞(3軸とも挟まれた1セル): {sum(r['cavity_cells'] for r in records)} セル。管の列に触れていない交差箇所: {other} 件。")


def local_crossings(mask: np.ndarray, centre, radius: float = 3.5) -> tuple[int, int]:
    """交差した2三角形の頂点の重心が centre(添字座標)から radius 要素以内の組数と、全体の組数。"""
    from duocad.mesh_intersections import triangle_intersections
    from duocad.topopt import mesh

    surface = mesh.build_surface_from_mask(mask)
    pairs = triangle_intersections(surface.verts_smooth, surface.faces)["pairs"]
    near = sum(1 for i, j, _ in pairs
               if np.linalg.norm(surface.verts_raw[np.unique(surface.faces[[i, j]])].mean(axis=0) - np.asarray(centre)) < radius)
    return near, len(pairs)


def _tube_gap(mask: np.ndarray, centre) -> tuple[float | None, dict | None]:
    best = None
    for run in analyze_mask(mask)["runs"]:
        axis = "xyz".index(run["axis"])
        lo = np.asarray(run["first_cell"], float)
        hi = lo.copy()
        hi[axis] += run["length"]
        if ((np.asarray(centre) >= lo - 1e-9) & (np.asarray(centre) <= hi + 1 + 1e-9)).all():
            best = run
    return (None if best is None else best["gap_min"]), best


PLATE = 25  # 合成マスクの板の一辺[セル]
MARGIN = 4


def plate_mask(thickness: int, hole: tuple, size: tuple = (PLATE, PLATE)) -> tuple[np.ndarray, np.ndarray]:
    """hole=((x0,x1),(y0,y1)) は板の座標の半開区間。z が板厚方向。戻り値は (マスク, 穴の中心の添字座標)。"""
    m = np.zeros((size[0] + 2 * MARGIN, size[1] + 2 * MARGIN, thickness + 2 * MARGIN), bool)
    m[MARGIN:MARGIN + size[0], MARGIN:MARGIN + size[1], MARGIN:MARGIN + thickness] = True
    (x0, x1), (y0, y1) = hole
    m[MARGIN + x0:MARGIN + x1, MARGIN + y0:MARGIN + y1, MARGIN:MARGIN + thickness] = False
    return m, np.array([MARGIN + (x0 + x1) / 2, MARGIN + (y0 + y1) / 2, MARGIN + thickness / 2])


def blind_mask(depth: int, size: int = 15) -> tuple[np.ndarray, np.ndarray]:
    thickness = depth + 3
    m, _ = plate_mask(thickness, ((0, 0), (0, 0)), (size, size))
    h = size // 2
    m[MARGIN + h, MARGIN + h, MARGIN + thickness - depth:MARGIN + thickness] = False
    return m, np.array([MARGIN + h + .5, MARGIN + h + .5, MARGIN + thickness - depth / 2])


def groove_mask(thickness: int, size: int = 15) -> tuple[np.ndarray, np.ndarray]:
    m, _ = plate_mask(thickness, ((0, 0), (0, 0)), (size, size))
    h = size // 2
    m[MARGIN:MARGIN + h + 1, MARGIN + h, MARGIN:MARGIN + thickness] = False
    return m, np.array([MARGIN + h / 2 + .5, MARGIN + h + .5, MARGIN + thickness / 2])


def grooved_mask(t: int, lo: int, hi: int, size: int = 21) -> tuple[np.ndarray, np.ndarray]:
    """板厚 t の 1x1 貫通穴の両面の外に、穴が +x 側へ開いた溝として lo / hi セル続く形(穴の側面のうち3方向が材料)。"""
    m = np.zeros((size, size, lo + t + hi + 2 * MARGIN), bool)
    m[:, :, MARGIN:MARGIN + lo + t + hi] = True
    h = size // 2
    m[h, h, MARGIN:MARGIN + lo + t + hi] = False
    m[h:, h, MARGIN:MARGIN + lo] = False
    m[h:, h, MARGIN + lo + t:MARGIN + lo + t + hi] = False
    return m, np.array([h + .5, h + .5, MARGIN + lo + t / 2])


def _thin_gap(mask: np.ndarray) -> float | None:
    """マスク内の細い空隙(管・溝)すべての壁の間隔の最小。細い空隙が無ければ None。"""
    result = analyze_mask(mask)
    gaps = [run["gap_min"] for run in result["runs"]] + [g for g in result["slit_min_gap"].values() if g is not None]
    return min(gaps) if gaps else None


def _bars(t: int) -> list:
    return [plate_mask(t, ((ox, ox + 1), (oy, oy + 1)), (wx, wy)) for wx in (3, 4, 5, 6, 7) for wy in (3, 4, 5, 6, 7)
            for ox in range(1, wx - 1) for oy in range(1, wy - 1)]


def synthetic() -> dict:
    out: dict = {}
    rows = []
    for t in range(1, 13):
        flat, centre = plate_mask(t, ((12, 13), (12, 13)))
        near, _ = local_crossings(flat, centre)
        gap, _ = _tube_gap(flat, centre)
        bars = _bars(t)
        bar_hits = sum(local_crossings(m, c)[0] > 0 for m, c in bars)
        rows.append(dict(thickness=t, flat_pairs=near, flat_gap_min=gap, bars=len(bars), bars_crossing=int(bar_hits)))
        print(f"貫通 1x1 板厚 {t}: 平板 交差の組 {near}・間隔 {gap:.3f} / 棒 {bar_hits}/{len(bars)} 本で交差", flush=True)
    out["through_1x1"] = rows
    controls = []
    for name, build in (("貫通 2x2", lambda d: plate_mask(d, ((12, 14), (12, 14)))),
                        ("貫通 1x2", lambda d: plate_mask(d, ((12, 13), (12, 14)))),
                        ("片側が開いた溝(貫通)", groove_mask),
                        ("止まり穴 1x1(深さ)", blind_mask)):
        for d in range(1, 9):
            m, c = build(d)
            near, _ = local_crossings(m, c)
            thin = _thin_gap(m)
            controls.append(dict(kind=name, size=d, pairs=near, thin_void_gap_min=thin))
            print(f"対照 {name} {d}: 交差の組 {near}・細い空隙の間隔の最小 {'なし' if thin is None else format(thin, '.3f')}", flush=True)
    out["controls"] = controls
    extension = []
    for t in range(2, 7):
        for lo in range(4):
            for hi in range(4):
                m, c = grooved_mask(t, lo, hi)
                extension.append(dict(thickness=t, groove_lo=lo, groove_hi=hi, pairs=local_crossings(m, c)[0]))
        print(f"開-開 板厚 {t} の穴の両面に溝(0〜3セル)を足す: 交差した組み合わせ "
              f"{[(e['groove_lo'], e['groove_hi'], e['pairs']) for e in extension if e['thickness'] == t and e['pairs']]}", flush=True)
    out["through_1x1_with_grooves"] = extension
    rng = np.random.default_rng(0)
    perturbed = []
    for t in range(2, 9):
        hits = 0
        for _ in range(100):
            m, c = plate_mask(t, ((12, 13), (12, 13)))
            xs, ys = np.arange(m.shape[0])[:, None], np.arange(m.shape[1])[None, :]
            far = np.maximum(abs(xs + .5 - c[0]), abs(ys + .5 - c[1])) >= 2.5
            low, high = (rng.random(far.shape) < 0.15) & far, (rng.random(far.shape) < 0.15) & far
            m[:, :, MARGIN - 1] = low
            m[:, :, MARGIN] &= ~low
            m[:, :, MARGIN + t] = high
            m[:, :, MARGIN + t - 1] &= ~high
            hits += local_crossings(m, c)[0] > 0
        perturbed.append(dict(thickness=t, hit=int(hits), of=100))
        print(f"貫通 1x1 板厚 {t}: 穴から 2 セル以上離れた板の両面を 15% ずつ盛る/削る 100 通りのうち交差 {hits}", flush=True)
    out["through_1x1_perturbed_faces"] = perturbed
    return out


def locality(flips: int = 40, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    rows = []
    for record in _load_records():
        runs = [run for run in record["runs"] if run["ends"] == "開開" and run["length"] >= 3]
        if not runs:
            continue
        mask = np.load(OUT / "voxel" / f"{record['name']}.npz")["mask"]
        for run in runs:
            axis = "xyz".index(run["axis"])
            lo = np.asarray(run["first_cell"])
            hi = lo.copy()
            hi[axis] += run["length"] - 1
            centre = (lo + hi + 1) / 2
            cropped = {}
            for radius in (2, 3, 4, 6):
                a, b = np.maximum(lo - radius, 0), np.minimum(hi + radius + 1, mask.shape)
                cropped[radius] = local_crossings(mask[tuple(slice(x, y) for x, y in zip(a, b))], centre - a)[0]
            a, b = np.maximum(lo - 5, 0), np.minimum(hi + 6, mask.shape)
            sub = mask[tuple(slice(x, y) for x, y in zip(a, b))]
            base = local_crossings(sub, centre - a)[0] > 0
            lateral = [k for k in range(3) if k != axis]
            index = np.argwhere(np.ones(sub.shape, bool))
            in_ring = (np.abs(index[:, lateral] - (lo - a)[lateral]).max(axis=1) <= 1) & \
                      (index[:, axis] >= (lo - a)[axis]) & (index[:, axis] <= (hi - a)[axis])
            movable = index[~in_ring]
            changed = 0
            for cell in movable[rng.choice(len(movable), flips, replace=False)]:
                trial = sub.copy()
                trial[tuple(cell)] = ~trial[tuple(cell)]
                changed += (local_crossings(trial, centre - a)[0] > 0) != base
            rows.append(dict(condition=record["name"], axis=run["axis"], length=run["length"], crossing=run["hit"],
                             cropped_pairs_by_radius=cropped, flips=flips, flips_changed=int(changed)))
            print(f"{record['name']} {run['axis']} L={run['length']} {'交差' if run['hit'] else '    '} "
                  f"切り出し(半径→組数) {cropped} / 1セル反転 {flips} 回のうち結果が変わった {changed}", flush=True)
    return dict(rows=rows)


def _sensitivity_job(args: tuple) -> dict:
    name, index, radius = args
    record = json.loads((RECORDS / f"{name}.json").read_text(encoding="utf-8"))
    run = record["runs"][index]
    mask = np.load(OUT / "voxel" / f"{name}.npz")["mask"]
    axis = "xyz".index(run["axis"])
    lo = np.asarray(run["first_cell"])
    hi = lo.copy()
    hi[axis] += run["length"] - 1
    a, b = np.maximum(lo - radius, 0), np.minimum(hi + radius + 1, mask.shape)
    sub = mask[tuple(slice(x, y) for x, y in zip(a, b))]
    centre = (lo + hi + 1) / 2 - a
    base = local_crossings(sub, centre)[0] > 0
    lateral = [k for k in range(3) if k != axis]
    changed = total = 0
    for cell in np.argwhere(np.ones(sub.shape, bool)):
        rel = cell + a - lo
        if abs(rel[lateral]).max() <= 1 and 0 <= rel[axis] <= run["length"] - 1:
            continue
        trial = sub.copy()
        trial[tuple(cell)] = ~trial[tuple(cell)]
        total += 1
        changed += (local_crossings(trial, centre)[0] > 0) != base
    return dict(condition=name, axis=run["axis"], length=run["length"], crossing_in_window=bool(base), cells=total, flips_that_change=changed)


def sensitivity(radius: int = 4, workers: int = DEFAULT_WORKERS) -> dict:
    tasks = [(r["name"], i, radius) for r in _load_records() for i, run in enumerate(r["runs"]) if run["ends"] == "開開" and run["length"] in (3, 4)]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(_sensitivity_job, tasks))
    for row in rows:
        print(f"{row['condition']} {row['axis']} L={row['length']} 窓内の交差 {'あり' if row['crossing_in_window'] else 'なし'} "
              f"反転 {row['cells']} 個のうち結果が変わった {row['flips_that_change']}", flush=True)
    return dict(radius=radius, rows=rows)


def _work(name: str) -> tuple[str, str | None]:
    try:
        record = analyze_condition(name)
        if not record["counts_match_mesh_manifest"]:
            return name, "平滑化後の交差件数が mesh_manifest.json と一致しない"
        RECORDS.mkdir(parents=True, exist_ok=True)
        tmp = RECORDS / f"{name}.json.tmp"
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(RECORDS / f"{name}.json")
        return name, None
    except Exception as exc:  # noqa: BLE001
        return name, f"{type(exc).__name__}: {exc}"


def build_sites() -> dict:
    rows, per = [], []
    for name in _names():
        record = json.loads((RECORDS / f"{name}.json").read_text(encoding="utf-8"))
        if not record["counts_match_mesh_manifest"]:
            raise ValueError(f"{name}: 平滑化後の交差件数が mesh_manifest.json と一致しない")
        per.append(record)
        for n, site in enumerate(record["sites"]):
            runs = [record["runs"][i] for i in site["run_ids"]]
            main = min(runs, key=lambda r: r["gap_min"]) if runs else None
            local = dict(
                kind="closed_tube" if main else ("thin_void_other" if site["thin_void_cells"] else "no_thin_void"),
                tube=None if not main else dict(
                    axis=main["axis"], cross_section_cells=[1, 1], section="closed", length_cells=main["length"],
                    ends=[main["end_lo"], main["end_hi"]], open_extension_cells=[main["ext_lo"], main["ext_hi"]], first_cell=main["first_cell"],
                    surround_material_min_cells=main["surround_min"], gap_min=round(main["gap_min"], 4),
                    gap_interior_min=None if main["gap_interior_min"] is None else round(main["gap_interior_min"], 4)),
                tubes_hit=len(runs), thin_void_cells=site["thin_void_cells"], void_cells=site["void_cells"])
            rows.append(dict(
                condition=record["name"], load_n=record["load_n"], element_size_mm=record["element_size_mm"],
                min_width_cells=record["min_width_cells"], site=n, pairs=site["pairs"], counts=site["counts"],
                triangles=site["triangles"], centre_cell=site["centre_cell"], centre_world_mm=site["centre_world_mm"],
                cell_bbox=site["cell_bbox"], local_shape=local, near_grid_edge=site["near_grid_edge"],
                keep_or_exclude_contact="not_tracked", move_max_cells=site["move_max_cells"], move_mean_cells=site["move_mean_cells"],
                move_max_mm=site["move_max_mm"]))
    return dict(
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"), conditions_total=len(per),
        conditions_with_sites=sum(1 for r in per if r["sites"]), sites_total=len(rows), pairs_total=sum(r["pairs"] for r in rows),
        parameters=dict(cluster_merge_cells=CLUSTER_MERGE_CELLS, gap_edges=GAP_EDGES, surround_cap=SURROUND_CAP),
        rows=rows)


def write_sites() -> dict:
    doc = build_sites()
    rows = doc.pop("rows")
    head = json.dumps(doc, ensure_ascii=False)[:-1]
    body = ",\n".join("    " + json.dumps(r, ensure_ascii=False) for r in rows)
    SITES.write_text(f'{head}, "rows": [\n{body}\n]}}\n', encoding="utf-8")
    return dict(doc, rows=rows)


def run_all(workers: int, force: bool) -> int:
    names = _names()
    todo = [n for n in names if force or _stale(n)]
    print(f"{len(names)} 条件のうち {len(todo)} 件を解析", flush=True)
    failed = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for name, error in pool.map(_work, todo):
            print(f"[{name}] {'OK' if error is None else 'FAIL ' + error}", flush=True)
            if error:
                failed.append(name)
    if failed:
        print("失敗:", failed)
        return 1
    doc = write_sites()
    print(f"intersection_sites.json: {doc['sites_total']} 箇所・{doc['pairs_total']} 組")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--one")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--locality", action="store_true")
    parser.add_argument("--sensitivity", action="store_true")
    args = parser.parse_args()
    if args.report:
        report()
        return 0
    if args.synthetic or args.locality or args.sensitivity:
        run = synthetic if args.synthetic else locality if args.locality else sensitivity
        (OUT / f"intersection_{run.__name__}.json").write_text(json.dumps(run(), ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    if args.one:
        record = analyze_condition(args.one)
        print(json.dumps({k: v for k, v in record.items() if k != "runs"}, ensure_ascii=False, indent=1))
        return 0
    return run_all(args.workers, args.force)


if __name__ == "__main__":
    sys.exit(main())

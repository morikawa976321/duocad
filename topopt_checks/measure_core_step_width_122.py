"""Issue #122: 芯が隣り合う m 立方の段(面・斜め)のつなぎ目を通る球の最大直径を、ボクセルの立方の和集合と、製品の表面生成
(marching cubes + Taubin 平滑化)を通した完成形の両方で測る。

.venv/Scripts/python.exe topopt_checks/measure_core_step_width_122.py

- voxel: 2つの m 立方(ずれ d)の和集合を1セル12分割で標本化し、EDT が t 以上の標本だけで立方Aの中心から立方Bの中心へつながる最大の t
  を二分探索して直径 2t(セル単位)。標本化で一律 −1/12 ずれる。式 min(m, √2(m−1)) と比べる。
- finished: 2本の太い柱(2m 角)を「太い板」と「試験棒(m 立方を 4m 段の階段で並べたもの)」でつないだ輪を表面生成し、完成メッシュを
  1セル6分割で標本化して、試験棒の高さだけを通って柱の中心どうしをつなぐ球の最大直径(セル単位)。
"""
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from duocad.topopt import mesh, voxelize  # noqa: E402


def _bottleneck(inside, sampling, a, b, region=None):
    edt = ndimage.distance_transform_edt(inside) * sampling
    if region is not None:
        edt = np.where(region, edt, 0.0)
    lo, hi = 0.0, float(min(edt[a], edt[b]))
    for _ in range(30):
        t = (lo + hi) / 2
        lab, _ = ndimage.label(edt >= t)
        if lab[a] and lab[a] == lab[b]:
            lo = t
        else:
            hi = t
    return 2 * lo


def voxel_pair(m, d, k=12):
    pad = 2
    shape = np.array([m + abs(x) for x in d]) + 2 * pad
    cells = np.zeros(shape, bool)
    a0 = np.array([pad + max(-x, 0) for x in d])
    b0 = a0 + np.array(d)
    for c in (a0, b0):
        cells[c[0]:c[0] + m, c[1]:c[1] + m, c[2]:c[2] + m] = True
    fine = cells.repeat(k, 0).repeat(k, 1).repeat(k, 2)
    idx = lambda c: tuple(int((x + m / 2) * k) for x in c)
    return _bottleneck(fine, 1.0 / k, idx(a0), idx(b0)) - 1.0 / k


def _ring(m, steps, n_steps):
    pts = [np.zeros(3, int)]
    for i in range(n_steps):
        pts.append(pts[-1] + np.asarray(steps[i % len(steps)]))
    pts = np.array(pts)
    pts -= pts.min(0)
    span = pts.max(0) + m
    height = int(span[2]) + m + 2 + 6 * m
    off = np.array([2 * m, 2 * m, m + 2])
    shape = (int(span[0]) + 6 * m, int(span[1]) + 6 * m, height + 4)
    cells = np.zeros(shape, bool)
    for p in pts + off:
        cells[p[0]:p[0] + m, p[1]:p[1] + m, p[2]:p[2] + m] = True
    a, b = pts[0] + off, pts[-1] + off
    top = height - 2 * m
    cells[a[0] - m:a[0] + m, a[1] - m:a[1] + m, 2:top + 2 * m] = True  # 柱(中心は a の角)
    cells[b[0]:b[0] + 2 * m, b[1]:b[1] + 2 * m, 2:top + 2 * m] = True  # 柱(中心は b + m)
    cells[a[0] - 2 * m:b[0] + 3 * m, min(a[1], b[1]) - 2 * m:max(a[1], b[1]) + 3 * m, top:top + 2 * m] = True  # 板
    centre_a = (a[0], a[1], a[2] + m / 2)
    centre_b = (b[0] + m, b[1] + m, b[2] + m / 2)
    bar_top = int(pts[:, 2].max()) + off[2] + m + 1
    return cells, centre_a, centre_b, bar_top


def finished_ring(m, steps, k=6):
    cells, ca, cb, bar_top = _ring(m, steps, 4 * m)
    surface = mesh.build_surface_from_mask(cells)
    v, f = np.asarray(surface.verts_smooth, float), np.asarray(surface.faces)
    grid = voxelize.Grid((0.0, 0.0, 0.0), 1.0 / k, tuple(int(n * k) for n in cells.shape))
    soup = voxelize.TriangleSoup(positions=v, triangles=f, face_index=np.zeros(len(f), dtype=np.int64))
    inside = voxelize.occupancy(soup, grid, allow_empty=True)[0]
    region = np.zeros_like(inside)
    region[:, :, :bar_top * k] = True
    idx = lambda c: tuple(int(x * k) for x in c)
    return _bottleneck(inside, 1.0 / k, idx(ca), idx(cb), region)


def main():
    print('voxel: m  step  直径(セル)  式 min(m, √2(m−1))')
    for m in range(2, 8):
        for d in [(1, 0, 0), (1, 1, 0), (1, 1, 1)]:
            o = sorted(m - abs(x) for x in d)
            print(f'  {m}  {d}  {voxel_pair(m, d):.3f}  {min(m, float(np.hypot(o[0], o[1]))):.3f}', flush=True)
    print('finished: m  段  直径(セル)')
    patterns = [((1, 0, 0),), ((1, 1, 0),), ((1, 1, 1),), ((1, 1, 0), (1, -1, 0)), ((1, 1, 1), (1, -1, -1)),
                ((1, 1, 0), (1, 0, 1), (1, -1, -1))]
    for m in (3, 4, 5, 6):
        for steps in patterns:
            print(f'  {m}  {steps}  {finished_ring(m, steps):.3f}', flush=True)


if __name__ == '__main__':
    main()

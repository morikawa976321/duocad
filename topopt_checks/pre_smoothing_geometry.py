"""ボクセル集合の境界と、CAD条件との体積的な重なりを検査する。"""
from itertools import product

import numpy as np
from scipy import ndimage
from OCP.BOPAlgo import BOPAlgo_BOP, BOPAlgo_Operation
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import kernel


def _regular_vertex(pattern):
    # 格子頂点を囲む微小球上で、境界との交線が単一の閉路なら局所的に2次元多様体。
    graph = {i: [] for i in range(6)}
    for octant in product(range(2), repeat=3):
        index = 4 * octant[0] + 2 * octant[1] + octant[2]
        for axis in range(3):
            if octant[axis]:
                continue
            other = index | (1 << (2 - axis))
            if bool(pattern & (1 << index)) == bool(pattern & (1 << other)):
                continue
            rays = [2 * a + octant[a] for a in range(3) if a != axis]
            a, b = rays
            graph[a].append(b)
            graph[b].append(a)
    active = {i for i, neighbours in graph.items() if neighbours}
    if not active:
        return True
    if any(len(graph[i]) != 2 for i in active):
        return False
    reached, pending = set(), [next(iter(active))]
    while pending:
        i = pending.pop()
        if i not in reached:
            reached.add(i)
            pending.extend(graph[i])
    return reached == active


REGULAR_VERTEX = np.array([_regular_vertex(p) for p in range(256)])


def boundary_check(material):
    material = np.asarray(material, dtype=bool)
    codes = np.zeros(tuple(n + 1 for n in material.shape), dtype=np.uint8)
    for octant in product(range(2), repeat=3):
        slices = tuple(slice(1 - o, n + 1 - o) for o, n in zip(octant, material.shape))
        codes[slices] |= material.astype(np.uint8) << (4 * octant[0] + 2 * octant[1] + octant[2])
    bad = ~REGULAR_VERTEX[codes]
    count = int(bad.sum())
    return dict(status='pass' if material.any() and count == 0 else 'fail',
                irregular_vertices=count, sample_vertex_indices=np.argwhere(bad)[:20].tolist(),
                boundary_vertices=int(((codes != 0) & (codes != 255)).sum()),
                rule='closed cubical boundary; one cycle in every boundary vertex link')


def connectivity_check(material):
    _, count = ndimage.label(material, structure=ndimage.generate_binary_structure(3, 1))
    return dict(status='pass' if count == 1 else 'fail', components=int(count), neighbourhood=6)


def active_nodes(material):
    nodes = np.zeros(tuple(n + 1 for n in material.shape), dtype=bool)
    for corner in product(range(2), repeat=3):
        nodes[tuple(slice(c, c + n) for c, n in zip(corner, material.shape))] |= material
    return nodes


def intersection_volume(material, grid, shape):
    """セル中心近似を使わず、互いに体積を共有しないセル列とCADの共通体積を合計する。"""
    shape = getattr(shape, 'wrapped', shape)
    if shape.IsNull() or not BRepCheck_Analyzer(shape).IsValid():
        raise ValueError('条件CADが空または不正')
    total = float(kernel.volume(shape))
    if not np.isfinite(total) or total <= 0:
        raise ValueError('条件CADに正の体積がない')
    bounds = kernel.bounding_box(shape)
    origin, h = np.asarray(grid.origin), float(grid.spacing)
    lo = np.maximum(0, np.floor((np.array(bounds['min']) - origin) / h).astype(int))
    hi = np.minimum(material.shape, np.ceil((np.array(bounds['max']) - origin) / h).astype(int))
    overlap = 0.0
    for j in range(lo[1], hi[1]):
        for k in range(lo[2], hi[2]):
            row = material[lo[0]:hi[0], j, k]
            if not row.any():
                continue
            changes = np.diff(np.pad(row.astype(int), 1))
            for start, end in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)):
                a = origin + h * np.array([lo[0] + start, j, k])
                b = a + h * np.array([end - start, 1, 1])
                box = BRepPrimAPI_MakeBox(gp_Pnt(*a), gp_Pnt(*b)).Shape()
                operation = BOPAlgo_BOP()
                operation.AddArgument(shape)
                operation.AddTool(box)
                operation.SetOperation(BOPAlgo_Operation.BOPAlgo_COMMON)
                operation.SetFuzzyValue(0.0)
                operation.Perform()
                if operation.HasErrors() or operation.HasWarnings():
                    raise RuntimeError('CAD共通体積演算が正常終了しなかった')
                if operation.Shape().IsNull() or not BRepCheck_Analyzer(operation.Shape()).IsValid():
                    raise RuntimeError('CAD共通体積演算の結果が不正')
                volume = float(kernel.volume(operation.Shape()))
                if not np.isfinite(volume) or volume < 0:
                    raise RuntimeError('CAD共通体積が不正')
                overlap += volume
    return total, overlap


def region_check(material, grid, bodies, *, preserve, volume_atol=1e-9, volume_rtol=1e-9):
    rows = []
    for name, shape in bodies:
        try:
            total, overlap = intersection_volume(material, grid, shape)
            tolerance = max(volume_atol, volume_rtol * total)
            if overlap > total + tolerance:
                raise RuntimeError('共通体積が条件ソリッドの体積を超えた')
            measured = overlap if preserve else max(0.0, total - overlap)
            rows.append(dict(body=name, status='pass' if measured > tolerance else 'fail',
                             body_volume_mm3=total, intersection_mm3=overlap,
                             empty_volume_mm3=max(0.0, total - overlap), tolerance_mm3=tolerance))
        except Exception as error:
            rows.append(dict(body=name, status='unknown', error=f'{type(error).__name__}: {error}'))
    statuses = [row['status'] for row in rows]
    status = 'fail' if 'fail' in statuses else 'unknown' if 'unknown' in statuses else 'pass'
    if preserve and not rows:
        status = 'fail'
    return dict(status=status, bodies=rows, rule='intersection > tolerance' if preserve else 'CAD minus material > tolerance')

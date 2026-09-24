"""二値探索(`duocad.topopt.binary_search`)と受入検査のテストが共有する題材と、実装とは独立な検算。"""
import dataclasses

import numpy as np
from build123d import Box, Pos
from scipy import ndimage

from duocad.topopt import acceptance, anchor, voxelize
from duocad.topopt import binary_search as bs


class Table:
    """`anchor.AnchorTable` と同じ面/節点インタフェースの軽量な代用。"""

    def __init__(self, grid, faces, empty=()):
        self.node_grid = anchor.NodeGrid(grid)
        self.faces = faces
        self._empty = set(empty)

    def node_ids(self, faces):
        face = faces[0]
        if face in self._empty:
            return np.zeros(0, dtype=int)
        return np.array(self.faces[face], dtype=int)

    def fixed_dofs(self, faces):
        ids = self.node_ids(faces)
        return (ids[:, None] * 3 + np.arange(3)).ravel()

    def load_vector(self, faces, force):
        out = np.zeros(self.node_grid.ndof)
        ids = self.node_ids(faces)
        out.reshape(-1, 3)[ids] = np.asarray(force) / len(ids)
        return out

    def empty_faces(self):
        return sorted(self._empty)


def bar_conditions(shape=(4, 2, 2), *, fixed=(1,), load=(2,), empty=(), forces=((0., 0., -1.),),
                    keep_box=None, target_safety_factor=.001, solver='direct'):
    """まっすぐな棒(4x2x2既定)。固定面=x最小平面(節点0..8)、荷重面=x最大平面。

    `keep_box` を省略すると、格子全体を覆う箱にする(保持条件を常に自明に満たし、
    島処理の判定を固定面・荷重面だけに絞れるようにする補助)。
    """
    grid = voxelize.Grid((0., 0., 0.), 1., shape)
    ny1, nz1 = shape[1] + 1, shape[2] + 1
    x_max_node = shape[0] * ny1 * nz1
    faces = {1: list(range(ny1 * nz1)), 2: list(range(x_max_node, x_max_node + ny1 * nz1))}
    table = Table(grid, faces, empty=empty)
    if keep_box is None:
        cx, cy, cz = (shape[0] + .0) / 2, shape[1] / 2, shape[2] / 2
        keep_box = Pos(cx, cy, cz) * Box(shape[0] + 2., shape[1] + 2., shape[2] + 2.)
    return bs.SearchConditions(
        grid=grid, table=table, fixed_faces=fixed, load_faces=load, forces=forces,
        preserves=[('keep', keep_box.wrapped)], excludes=[],
        target_safety_factor=target_safety_factor, solver=solver,
    )


def slim_bar_conditions(**kwargs):
    """10x4x4 の格子の中の 2x2 断面の棒(y・z < 2)の条件。固定面・荷重面は棒の両端の断面の節点だけ。"""
    conditions = bar_conditions(shape=(10, 4, 4), **kwargs)
    nodes = lambda x: [(x * 5 + y) * 5 + z for y in range(3) for z in range(3)]
    return dataclasses.replace(conditions, table=Table(conditions.grid, {1: nodes(0), 2: nodes(10)}))


def appendage_conditions(target_safety_factor=.001):
    """固定面(x=0)から荷重面(x=10)へ通る 10x2x2 の棒と、荷重経路に乗らない 4x2x2 の付属部分(y=2..3)。"""
    shape = (10, 4, 2)
    grid = voxelize.Grid((0., 0., 0.), 1., shape)
    ny1, nz1 = shape[1] + 1, shape[2] + 1

    def bar_end(x):
        return [x * ny1 * nz1 + y * nz1 + z for y in range(3) for z in range(nz1)]

    keep = Pos(5., 2., 1.) * Box(12., 6., 4.)
    conditions = bs.SearchConditions(
        grid=grid, table=Table(grid, {1: bar_end(0), 2: bar_end(shape[0])}), fixed_faces=(1,), load_faces=(2,),
        forces=((0., 0., -1.),), preserves=[('keep', keep.wrapped)], excludes=[],
        target_safety_factor=target_safety_factor, solver='direct')
    design = np.zeros(shape, bool)
    design[:, :2, :] = True
    appendage = np.zeros(shape, bool)
    appendage[3:7, 2:, :] = True
    return conditions, design | appendage, appendage


def fake_report(material, status='fail'):
    """FEM・CADを置き換える小さな報告(候補管理だけを検査するため)。"""
    material = np.asarray(material, dtype=bool)
    volume = float(material.sum())
    return dict(status=status, material_sha256=acceptance.material_sha256(material),
                volumes=dict(material_mm3=volume, before_mm3=volume, removed_islands_mm3=0.0),
                checks=dict(watertight=dict(status='pass'), single_solid=dict(status='pass'),
                            preserve=dict(status='pass'), exclude=dict(status='pass'),
                            conditions=dict(status='pass'), safety=dict(status=status, safety_factor=0.5)))


def assert_peel_invariants(mask, conditions):
    """peel の出力が満たすべき4つ(1成分・不正頂点0・閉じた空洞なし・全条件節点に支えあり)を、全体の判定で確かめる。"""
    assert acceptance.connectivity_check(mask)['components'] == 1
    assert acceptance.boundary_check(mask)['irregular_vertices'] == 0
    assert acceptance.void_components(mask) == 1
    assert acceptance.active_nodes(mask).ravel()[bs._anchor_node_ids(conditions)].all()


def random_connected_mask(rng, shape, density=.7):
    labels, count = ndimage.label(rng.random(shape) < density, structure=bs.NEIGHBOURHOOD_STRUCTURE)
    return labels == 1 + int(np.argmax(np.bincount(labels.ravel())[1:])) if count else np.zeros(shape, bool)


def opening_satisfied(mask: np.ndarray, m: int) -> bool:
    """`mask` の全材料セルが m 立方の材料だけのブロックのどれかに属するか(peel の局所判定とは
    独立な、全体を数え直す物差し)。"""
    if m <= 1:
        return True
    opened = ndimage.binary_opening(mask, structure=np.ones((m, m, m), dtype=bool))
    return not (mask & ~opened).any()


def rule_violations(mask: np.ndarray, m: int, exempt=None) -> np.ndarray:
    """規則を破るセル(実装とは独立に scipy で数える)。`exempt` は被覆を問わず、`mask` に無くても材料に数える。"""
    material = mask if exempt is None else mask | exempt
    bad = mask & ~ndimage.binary_opening(material, structure=np.ones((m, m, m), dtype=bool))
    return bad if exempt is None else bad & ~exempt


def _box_sums(values: np.ndarray, m: int) -> np.ndarray:
    """`values` の、各位置を最小の角とする m 立方の総和。配列に収まる位置だけ(形は各軸 n−m+1)。"""
    total = np.pad(values.astype(np.int32), ((1, 0),) * 3).cumsum(0).cumsum(1).cumsum(2)
    return (total[m:, m:, m:] - total[:-m, m:, m:] - total[m:, :-m, m:] - total[m:, m:, :-m]
            + total[:-m, :-m, m:] + total[:-m, m:, :-m] + total[m:, :-m, :-m] - total[:-m, :-m, :-m])


def core_groups_oracle(mask: np.ndarray, m: int, exempt=None) -> int:
    """S(m) の物差し(実装とは独立): 芯 = m 立方が全部材料のセル(累積和で数える)の6連結成分を、立方が `exempt` の
    6連結成分と重なるか6近傍で接するものどうしで結んだときの塊の数。要求は1以下(芯が空なら0)。"""
    material = mask if exempt is None else mask | exempt
    if any(n < m for n in mask.shape):
        return 0
    fits = _box_sums(material, m) == m ** 3
    core = np.zeros(mask.shape, bool)
    core[tuple(slice(0, n) for n in fits.shape)] = fits
    six = ndimage.generate_binary_structure(3, 1)
    labels, count = ndimage.label(core, structure=six)
    parent = list(range(count + 1))

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    if exempt is not None and exempt.any():
        e_labels, _ = ndimage.label(exempt, structure=six)
        e_root = {}
        for i in range(1, count + 1):
            cover = np.zeros(mask.shape, bool)
            for c in np.argwhere(labels == i):
                cover[tuple(slice(a, a + m) for a in c)] = True
            touching = cover | ndimage.binary_dilation(cover, structure=six)
            for j in np.unique(e_labels[touching & exempt]):
                if j in e_root:
                    parent[find(i)] = find(e_root[j])
                else:
                    e_root[j] = i
    return len({find(i) for i in range(1, count + 1)})


def assert_min_width_invariants(mask, m, exempt=None):
    """`peel` の不変条件(規則(`exempt` を除く)・侵食した芯の連結・非正則頂点0・1成分・閉じた空洞0)を、全体を数え直して確かめる。"""
    assert bs._mask_satisfies_min_width(mask, m, exempt)
    assert core_groups_oracle(mask, m, exempt) <= 1
    assert acceptance.boundary_check(mask)['irregular_vertices'] == 0
    assert acceptance.connectivity_check(mask)['components'] == 1
    assert acceptance.void_components(mask) == 1


def peel_box_until_stuck(m, kind):
    """12x20x16 の直方体(固定面 y=0・荷重面 y=最大の中央)を、固定のエネルギー場で `peel`(2割要求)が止まるまで
    繰り返し、各ステップの不変条件を全体で数え直す。戻り値は止まったときの材料割合。"""
    shape = (12, 20, 16)
    nodes = np.zeros(tuple(n + 1 for n in shape), bool)
    nodes[:, 0, :] = True
    nodes[4:8, shape[1], 5:10] = True
    anchors = np.flatnonzero(nodes.ravel())
    x, y, z = np.meshgrid(*(np.arange(n, dtype=float) for n in shape), indexing='ij')
    noise = np.random.default_rng(1).random(shape)
    energy = (((shape[1] - y) / shape[1]) ** 2 * ((z - 7.5) / 8) ** 2 + 1e-6 * noise) if kind == 'bending' else noise
    mask = np.ones(shape, bool)
    for _ in range(60):
        after, stats = bs.peel(mask, bs.ranked_cells(mask, energy), max(1, int(.2 * mask.sum())), anchors,
                               min_width_cells=m)
        assert stats.picked + stats.casualties + stats.islands == mask.sum() - after.sum()
        if after.sum() == mask.sum():
            break
        assert_min_width_invariants(after, m)
        assert acceptance.active_nodes(after).ravel()[anchors].all()
        mask = after
    return mask.sum() / mask.size

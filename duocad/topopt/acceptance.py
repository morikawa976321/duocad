"""島除去後・平滑化前の6条件(計画書§2 `docs/PLAN_BINARY_STRESS_SEARCH.md`)。

検査対象は候補に対して島除去を終えた、平滑化前の材料マスク(bool)。表面抽出・平滑化・
最終CAD生成は呼ばない。合否の意味・許容差の規則は `docs/TOPOPT_PRE_SMOOTHING_CHECKS.md`。

役割を4つに分けて持つ:
    幾何       — 格子内部の境界・連結性(numpy/scipyのみ、OCPには触らない)
    CAD        — 保持・除外条件との体積の重なり(OCPは `duocad.kernel` 越しにだけ触る)
    境界条件   — 荷重・拘束面の健全性(FEMを組む前の静的な点検)
    FEM        — 材料マスクを再構築して安全率(`fem.Model` の唯一の実装に乗る)
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from itertools import product
import hashlib
import math

import numpy as np
from scipy import ndimage

from duocad import kernel

from . import anchor, fem, stress, voxelize
from .anchor import active_nodes  # noqa: F401  6条件と探索用の確定表が同じ「材料に接する節点」の定義を使う
from .material import DEFAULT_ALLOWABLE_MPA, DEFAULT_YOUNGS_MODULUS_MPA


def _positive(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be finite and positive')


# ---------------------------------------------------------------------------
# 幾何: 格子内部の境界・連結性
# ---------------------------------------------------------------------------

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


def irregular_vertices(material):
    """格子頂点(節点格子の形)ごとに、境界が局所的に2次元多様体でないか。`boundary_check` の判定そのもの。"""
    material = np.asarray(material, dtype=bool)
    codes = np.zeros(tuple(n + 1 for n in material.shape), dtype=np.uint8)
    for octant in product(range(2), repeat=3):
        slices = tuple(slice(1 - o, n + 1 - o) for o, n in zip(octant, material.shape))
        codes[slices] |= material.astype(np.uint8) << (4 * octant[0] + 2 * octant[1] + octant[2])
    return ~REGULAR_VERTEX[codes], codes


def material_sha256(material) -> str:
    """材料マスクの内容ハッシュ。**候補の突き合わせ(`binary_search`)と `inspect` の報告が同じ式を使う**
    (層をまたぐ契約 —— 別々に書くと、同じ形が違うハッシュで記録される)。"""
    return hashlib.sha256(np.ascontiguousarray(np.asarray(material, dtype=bool)).tobytes()).hexdigest()


def boundary_check(material):
    material = np.asarray(material, dtype=bool)
    bad, codes = irregular_vertices(material)
    count = int(bad.sum())
    return dict(status='pass' if material.any() and count == 0 else 'fail',
                irregular_vertices=count, sample_vertex_indices=np.argwhere(bad)[:20].tolist(),
                boundary_vertices=int(((codes != 0) & (codes != 255)).sum()),
                rule='closed cubical boundary; one cycle in every boundary vertex link')


def connectivity_check(material):
    _, count = ndimage.label(material, structure=ndimage.generate_binary_structure(3, 1))
    return dict(status='pass' if count == 1 else 'fail', components=int(count), neighbourhood=6)


# ---------------------------------------------------------------------------
# CAD: 保持・除外条件との体積の重なり。OCPには触らず duocad.kernel を呼ぶだけ。
# ---------------------------------------------------------------------------

def _material_boxes_within_bbox(material, grid, shape, *, cache=None):
    """材料セルのうち `shape` のbboxと重なる範囲だけを、x方向に融着した直方体列
    (world座標の(min, max)ペア)へ変換する。互いに体積を共有しない列になる。"""
    bounds = kernel.bounding_box(shape, cache=cache)
    origin, h = np.asarray(grid.origin), float(grid.spacing)
    lo = np.maximum(0, np.floor((np.array(bounds['min']) - origin) / h).astype(int))
    hi = np.minimum(material.shape, np.ceil((np.array(bounds['max']) - origin) / h).astype(int))
    boxes = []
    for j in range(lo[1], hi[1]):
        for k in range(lo[2], hi[2]):
            row = material[lo[0]:hi[0], j, k]
            if not row.any():
                continue
            changes = np.diff(np.pad(row.astype(int), 1))
            for start, end in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)):
                a = origin + h * np.array([lo[0] + start, j, k])
                b = a + h * np.array([end - start, 1, 1])
                boxes.append((a.tolist(), b.tolist()))
    return boxes


def intersection_volume(material, grid, shape, *, cache=None):
    """セル中心近似を使わず、材料セルとCADの共通体積を合計する(kernel.boxes_common_volume)。

    `cache` に `duocad.kernel.BoundingBoxCache`(既定 `document.bbox_cache`)を渡すと、
    同じCAD形状のbbox測り直しを避ける。**測り方自体は変わらない**ので、`cache=None` と
    渡した場合とで体積は一致する(この一致は検査で固定してある)。
    """
    shape = getattr(shape, 'wrapped', shape)
    kernel.require_valid_solid(shape)
    total = float(kernel.volume(shape))
    if not np.isfinite(total) or total <= 0:
        raise ValueError('条件CADに正の体積がない')
    boxes = _material_boxes_within_bbox(material, grid, shape, cache=cache)
    overlap = kernel.boxes_common_volume(shape, boxes)
    return total, overlap


#: 前計算の切り分け単位(セル数/辺)。塊全体との共通が「空」「全部中」なら1回で終わり、境界にかかる塊
#: だけを切る。大きいほど塊の数は減るが、1回の切り分けが重くなる(実測は docs/TOPOPT_SPEED.md)。
TILE_CELLS = 8


class _CellOverlaps:
    """1つのCADソリッドと1つの格子について、セルごとの共通体積 [mm³] を求めて持つ。

    保持するのはソリッドの外接箱と重なる範囲だけ(外は0)。値は `NaN`(未計算)から始まり、材料マスクに
    現れたセルを含む `TILE_CELLS` 角の塊ごとに、その塊のセルをまとめて求める。材料は設計空間の内側にしか
    無いので、外接箱に入っていても設計空間に掛からない塊は一度も計算しない。
    """

    def __init__(self, grid, shape, *, cache=None):
        self.shape = getattr(shape, 'wrapped', shape)
        kernel.require_valid_solid(self.shape)
        self.total = float(kernel.volume(self.shape))
        if not np.isfinite(self.total) or self.total <= 0:
            raise ValueError('条件CADに正の体積がない')
        bounds = kernel.bounding_box(self.shape, cache=cache)
        self.origin, self.spacing = np.asarray(grid.origin, dtype=float), float(grid.spacing)
        self.lo = np.maximum(0, np.floor((np.array(bounds['min']) - self.origin) / self.spacing).astype(int))
        hi = np.minimum(grid.shape, np.ceil((np.array(bounds['max']) - self.origin) / self.spacing).astype(int))
        self.values = np.full(tuple(np.maximum(hi, self.lo) - self.lo), np.nan)

    def overlap_of(self, material):
        window = tuple(slice(int(a), int(a) + n) for a, n in zip(self.lo, self.values.shape))
        wanted = np.asarray(material, dtype=bool)[window]
        missing = wanted & np.isnan(self.values)
        if missing.any():
            padded = np.pad(missing, [(0, -n % TILE_CELLS) for n in missing.shape])
            tiles = padded.reshape(*(m for n in padded.shape for m in (n // TILE_CELLS, TILE_CELLS))).any(axis=(1, 3, 5))
            for tile in np.argwhere(tiles):
                self._resolve(tile * TILE_CELLS)
        return float(self.values[wanted].sum())

    def _resolve(self, start):
        stop = np.minimum(start + TILE_CELLS, self.values.shape)
        counts = stop - start
        corner = self.origin + self.spacing * (self.lo + start)
        kind, volumes = kernel.grid_cell_common_volumes(self.shape, corner.tolist(), self.spacing, counts.tolist())
        window = tuple(slice(int(a), int(b)) for a, b in zip(start, stop))
        if kind == 'full':
            self.values[window] = self.spacing ** 3
            return
        block = np.zeros(tuple(counts))
        for cell, volume in volumes.items():
            block[cell] = volume
        self.values[window] = block


class RegionOverlaps:
    """保持・除外CADと格子セルの重なり体積を、格子 × ソリッドの組ごとに求めて持つメモ。

    `region_check(..., overlaps=)` に渡すと、候補マスクごとの共通体積が「材料セルの重なり体積の
    足し算」になる(旧経路 `intersection_volume` は候補ごとに材料を直方体列へ融着してブーリアンを
    取っていた)。重なり体積は旧経路と同じく、セル中心近似ではなく**セルとソリッドの共通体積
    そのもの**(境界のセルは部分的な重なりを持つ)。

    **寿命は探索1回**(`binary_search.SearchConditions` が持つ。探索中は格子も条件も固定)。
    鍵は (ソリッドの同一性〈TShape・Location・Orientation〉, 格子の原点・間隔・形) なので、
    入力が変われば別の項目になり古い値は読まれない。文書を編集するとソリッドは別物になるので、
    探索をまたいで持ち越さない。**スレッド間で共有しない**(計算は遅延して書き込む)。
    メモリは外接箱と格子の共通部分のセル数 × 8 byte(ソリッドごと)。

    前計算を作れない・途中で失敗するソリッドは、そのソリッドだけ旧経路へ戻す(`fallbacks` に理由が残る)。
    前計算は速さのためだけにあり、旧経路と同じ答えを返せないなら使わない。
    """

    def __init__(self):
        self._entries = {}
        self.fallbacks = {}

    def __len__(self):
        return len(self._entries)

    @property
    def nbytes(self):
        """保持している値の大きさ(byte)。"""
        return sum(entry.values.nbytes for entry in self._entries.values() if entry is not None)

    def clear(self):
        self._entries.clear()
        self.fallbacks.clear()

    def overlap_volume(self, material, grid, shape, *, cache=None):
        """`intersection_volume` と同じ `(ソリッドの体積, 材料との共通体積)`。"""
        raw = getattr(shape, 'wrapped', shape)
        key = (kernel.shape_key(raw), tuple(float(v) for v in grid.origin), float(grid.spacing),
               tuple(int(n) for n in grid.shape))
        if key in self._entries:
            entry = self._entries[key]  # 旧経路へ戻した項目は None
        else:
            try:
                entry = self._entries[key] = _CellOverlaps(grid, raw, cache=cache)
            except Exception as error:
                entry = self._give_up(key, error)
        if entry is not None:
            try:
                return entry.total, entry.overlap_of(material)
            except Exception as error:
                self._give_up(key, error)
        return intersection_volume(material, grid, raw, cache=cache)

    def _give_up(self, key, error):
        self.fallbacks[key] = f'{type(error).__name__}: {error}'
        self._entries[key] = None
        return None


def region_check(material, grid, bodies, *, preserve, volume_atol=1e-9, volume_rtol=1e-9, cache=None,
                 overlaps=None):
    """`overlaps`(`RegionOverlaps`)を渡すと、共通体積を前計算したセルごとの値の足し算で取る。
    渡さなければ `intersection_volume`(候補ごとのブーリアン)。**戻り値は同じ形・同じ数値許容差**。"""
    rows = []
    for name, shape in bodies:
        try:
            if overlaps is None:
                total, overlap = intersection_volume(material, grid, shape, cache=cache)
            else:
                total, overlap = overlaps.overlap_volume(material, grid, shape, cache=cache)
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


# ---------------------------------------------------------------------------
# 境界条件: 荷重・拘束面の健全性(FEMを組む前の静的な点検)
# ---------------------------------------------------------------------------

def condition_check(material, table, fixed_faces, load_faces, forces):
    live = active_nodes(material).ravel()
    rows = []
    fixed_faces, load_faces = tuple(fixed_faces), tuple(load_faces)
    forces = np.asarray(forces, dtype=float)
    if not fixed_faces or not load_faces or forces.shape != (len(load_faces), 3):
        raise ValueError('拘束・荷重面と荷重ベクトルの対応が不正')
    if not np.isfinite(forces).all() or not np.any(forces):
        raise ValueError('荷重が非有限または全てゼロ')
    load = np.zeros(3 * live.size)
    fixed = []
    for kind, faces in (('fixed', fixed_faces), ('load', load_faces)):
        for index, face in enumerate(faces):
            ids = table.node_ids([face])
            lost = ids[~live[ids]]
            row = dict(condition=f'{kind}_face_{face}', nodes=int(ids.size),
                       lost_nodes=int(lost.size), status='pass' if ids.size and not lost.size else 'fail')
            if kind == 'fixed':
                if ids.size:
                    fixed.extend(table.fixed_dofs([face]).tolist())
            elif ids.size:
                vector = table.load_vector([face], forces[index])
                row['requested_force_n'] = forces[index].tolist()
                row['lost_force_l1_n'] = float(np.abs(vector.reshape(-1, 3)[~live]).sum())
                row['resultant_n'] = vector.reshape(-1, 3).sum(axis=0).tolist()
                if not np.allclose(row['resultant_n'], forces[index], atol=1e-10, rtol=1e-10):
                    row['status'] = 'fail'
                load += vector
            rows.append(row)
    fixed = np.unique(fixed).astype(np.int64)
    # 各荷重面を個別に点検してから加算する。相反する荷重の脱落を相殺で隠さない。
    supported = bool(rows) and all(row['status'] == 'pass' for row in rows)
    return dict(status='pass' if supported else 'fail', faces=rows), fixed, load


# ---------------------------------------------------------------------------
# FEM: 材料マスクを再構築して安全率
# ---------------------------------------------------------------------------

def safety_check(material, grid, fixed, load, *, target, allowable_mpa, youngs_modulus_mpa,
                 percentile, solver, fields=None):
    try:
        live = active_nodes(material).ravel()
        active_dofs = np.repeat(live, 3)
        physical_fixed = np.intersect1d(fixed, np.flatnonzero(active_dofs))
        node_indices = np.array(np.unravel_index(physical_fixed // 3, tuple(n + 1 for n in material.shape))).T
        positions = node_indices - node_indices.mean(axis=0)
        rigid = np.zeros((len(physical_fixed), 6))
        for i, (position, dof) in enumerate(zip(positions, physical_fixed)):
            axis = dof % 3
            rigid[i, axis] = 1
            for rotation in range(3):
                rigid[i, 3 + rotation] = np.cross(np.eye(3)[rotation], position)[axis]
        if np.linalg.matrix_rank(rigid) < 6:
            return dict(status='fail', reason='rigid_body_modes', message='剛体運動を拘束できない')
        model = fem.Model(*material.shape, fixed_dofs=fixed, load=load,
                          extra_fixed_dofs=np.flatnonzero(~active_dofs), active_mask=material)
        density = material.astype(float)
        displacement = stress.solve_displacement(model, density, solver=solver)
        residual = model.stiffness_apply(density, 3.0, displacement) - model.f
        relative = float(np.linalg.norm(residual[model.free]) / np.linalg.norm(model.f[model.free]))
        if not np.isfinite(displacement).all() or not math.isfinite(relative) or relative > fem.SOLVE_RESIDUAL_TOL:
            raise RuntimeError(f'残差検算に失敗: {relative}')
        # load は既に N。相殺後の節点力で再正規化せず、単位格子の長さだけ換算する。
        scale = stress.UnitScale(grid.spacing, 1.0, youngs_modulus_mpa, solved_load=1.0)
        report = stress.evaluate(model, density, scale=scale, allowable_mpa=allowable_mpa,
                                 u=displacement, percentile=percentile)
        sf = float(report.safety_factor)
        if not math.isfinite(sf):
            raise RuntimeError('安全率が非有限')
        if fields is not None:
            # 同じ変位から取る u_eᵀ k₀ u_e(単位剛性。空セルは節点を共有しても 0 にする)。
            _, element_energy = model.compliance(density, 3.0, displacement)
            fields['element_energy'] = np.where(material, element_energy, 0.0)
        return dict(status='pass' if sf >= target else 'fail', safety_factor=sf,
                    target_safety_factor=target, representative_mpa=report.representative_mpa,
                    allowable_mpa=allowable_mpa, youngs_modulus_mpa=youngs_modulus_mpa,
                    raw_max_mpa=report.raw_max_mpa, percentile=percentile,
                    max_displacement_mm=report.max_displacement_mm, relative_residual=relative,
                    residual_tolerance=fem.SOLVE_RESIDUAL_TOL, solver=solver,
                    evaluated_material_cells=int(material.sum()), density_rule='binary, active elements only')
    except Exception as error:
        return dict(status='unknown', reason='evaluation_error', error=f'{type(error).__name__}: {error}')


# ---------------------------------------------------------------------------
# 探索履歴の診断(6条件の合否とは別枠。形状検査だけでも履歴なしで動く)
# ---------------------------------------------------------------------------

def search_diagnostics(history=None, *, converged=None, max_evaluations=None, target=None):
    if history is None:
        return dict(status='unknown', termination='unknown', minimum_proven=False,
                    note='探索履歴なし。形状の合否と探索完了は別判定。')
    rows = [asdict(row) if is_dataclass(row) else dict(row) for row in history]
    valid, errors = [], []
    for i, row in enumerate(rows):
        volume, sf = row.get('material_mm3'), row.get('safety_factor')
        if (row.get('outcome') == 'ok' and isinstance(volume, (int, float)) and
                math.isfinite(volume) and volume > 0 and isinstance(sf, (int, float)) and math.isfinite(sf) and sf > 0):
            valid.append((volume, sf, i))
        else:
            errors.append(i)
    valid.sort()
    inversions = []
    for a, b in zip(valid, valid[1:]):
        if a[0] < b[0] and a[1] > b[1] + 1e-9 * max(a[1], b[1]):
            inversions.append(dict(smaller_evaluation=a[2], larger_evaluation=b[2]))
    termination = ('reported_converged' if converged is True else
                   'evaluation_limit' if max_evaluations is not None and len(rows) >= max_evaluations else 'unknown')
    bracket = None
    if target is not None:
        safe = [v for v, sf, _ in valid if sf >= target]
        if safe:
            upper = min(safe)
            unsafe = [v for v, sf, _ in valid if sf < target and v < upper]
            if unsafe:
                lower = max(unsafe)
                bracket = dict(lower_mm3=lower, upper_mm3=upper, relative_gap=(upper - lower) / upper)
            elif valid and upper == min(v for v, _, _ in valid) and converged is True:
                termination = 'lowest_evaluated_already_safe'
    return dict(status='warning' if errors or inversions else 'recorded', termination=termination,
                minimum_proven=False, history=rows, failed_or_unevaluated=errors,
                monotonicity_inversions=inversions, volume_basis='requested density integral mm3',
                observed_bracket=bracket,
                note='収束フラグは元の探索の申告。最終二値体積の最小性は保証しない。')


# ---------------------------------------------------------------------------
# 統合: 6条件を全部通して合否の型を返す
# ---------------------------------------------------------------------------

def inspect(material, before, grid, *, table, fixed_faces, load_faces, forces,
            preserves, excludes, target_safety_factor, allowable_mpa=DEFAULT_ALLOWABLE_MPA,
            youngs_modulus_mpa=DEFAULT_YOUNGS_MODULUS_MPA, percentile=stress.DEFAULT_PERCENTILE,
            solver='cg', history=None, converged=None, max_evaluations=None,
            volume_atol=1e-9, volume_rtol=1e-9, bbox_cache=None, region_overlaps=None, fields=None):
    """6条件の入口。**`material`/`before` は格子と同じ形のboolマスク(材料=True)を直接
    受ける**(計画書§5: 「新規の製品APIはboolマスクを受ける」)。過去記録の密度場からの
    二値化は行わない —— それは読み取りアダプター `inspect_document` に閉じてある。
    `before` は島除去前のboolマスク(除去で材料が増えていないことをここで確認する)。
    `region_overlaps`(`RegionOverlaps`)を渡すと、保持・除外の共通体積を前計算から引く(既定は候補ごとに
    ブーリアンを取る旧経路。報告の形と数値許容差は同じ)。
    `fields` に dict を渡すと、安全率を計算できたときだけ要素ごとの `u_eᵀ k₀ u_e`(格子と同じ形、
    材料外は 0)を `fields['element_energy']` に入れる。報告(JSON)には入れない(段階4の削る順位用)。
    """
    for name, value in [('spacing', grid.spacing), ('target', target_safety_factor),
                        ('allowable_mpa', allowable_mpa), ('youngs_modulus_mpa', youngs_modulus_mpa)]:
        _positive(value, name)
    if not np.isfinite(grid.origin).all() or not 0 < percentile <= 100:
        raise ValueError('原点・応力パーセンタイルが不正')
    if any(not math.isfinite(v) or v < 0 for v in (volume_atol, volume_rtol)):
        raise ValueError('体積許容差が不正')
    material, original = np.asarray(material), np.asarray(before)
    for field, label in ((material, 'material'), (original, 'before')):
        if field.shape != tuple(grid.shape) or field.dtype != bool:
            raise ValueError(f'{label} は格子と同じ形のboolマスクであること(受け取った形/型: {field.shape}/{field.dtype})')
    if tuple(table.node_grid.grid.shape) != tuple(grid.shape) or table.node_grid.grid != grid:
        raise ValueError('アンカーと検査格子が一致しない')
    if np.any(material & ~original):
        raise ValueError('島除去後に材料セルが増えている')
    checks = dict(watertight=boundary_check(material), single_solid=connectivity_check(material),
                  preserve=region_check(material, grid, preserves, preserve=True, volume_atol=volume_atol,
                                        volume_rtol=volume_rtol, cache=bbox_cache, overlaps=region_overlaps),
                  exclude=region_check(material, grid, excludes, preserve=False, volume_atol=volume_atol,
                                       volume_rtol=volume_rtol, cache=bbox_cache, overlaps=region_overlaps))
    checks['conditions'], fixed, load = condition_check(material, table, fixed_faces, load_faces, forces)
    if all(checks[key]['status'] == 'pass' for key in ('watertight', 'single_solid', 'conditions')):
        checks['safety'] = safety_check(material, grid, fixed, load, target=target_safety_factor,
                                       allowable_mpa=allowable_mpa, youngs_modulus_mpa=youngs_modulus_mpa,
                                       percentile=percentile, solver=solver, fields=fields)
    else:
        checks['safety'] = dict(status='unknown', reason='prerequisites_failed')
    statuses = [check['status'] for check in checks.values()]
    status = 'fail' if 'fail' in statuses else 'unknown' if 'unknown' in statuses else 'pass'
    return dict(schema_version=1, status=status, exit_code={'pass': 0, 'fail': 1, 'unknown': 2}[status],
                stage='post-island-removal, pre-smoothing',
                material_sha256=material_sha256(material),
                grid=grid.as_dict(), checks=checks,
                volumes=dict(material_mm3=float(material.sum() * grid.element_volume),
                             before_mm3=float(original.sum() * grid.element_volume),
                             removed_islands_mm3=float((original & ~material).sum() * grid.element_volume)),
                search=search_diagnostics(history, converged=converged, max_evaluations=max_evaluations,
                                          target=target_safety_factor))


def inspect_document(document, rho_clean, rho_before, grid, *, level=0.5, **kwargs):
    """過去記録(密度場)を読む**読み取りアダプター**。密度→bool変換をここに閉じ、
    boolマスクを直接受ける `inspect` を呼ぶ(計画書§5)。

    密度積算量(SIMP時代の指標。過去記録にしか存在しない)は、二値領域の体積とは別に
    `volumes.density_integral_*_mm3` として付記する(`docs/TOPOPT_PRE_SMOOTHING_CHECKS.md`
    「密度積算量と、この二値領域の体積は別々に報告する」)。
    """
    before = np.asarray(rho_before, dtype=float)
    clean = np.asarray(rho_clean, dtype=float)
    for field, field_name in ((before, 'rho_before'), (clean, 'rho_clean')):
        if field.shape != tuple(grid.shape) or not np.isfinite(field).all() or np.any((field < 0) | (field > 1)):
            raise ValueError(f'{field_name} の形・値が不正(格子と同じ形の有限な0〜1の密度であること)')
    if not 0 <= level < 1:
        raise ValueError('閾値levelが不正')
    material, original = clean > level, before > level
    from duocad.optimize_run import build_conditions
    kwargs.setdefault('bbox_cache', document.bbox_cache)
    bundle = build_conditions(document)
    soup = voxelize.triangles_from_shape(voxelize._unwrap(bundle.conditions.anchor),
                                         deviation=grid.spacing * voxelize.DEFAULT_DEVIATION_RATIO)
    # 「元の条件」は材料に対応付けた確定表(`docs/TOPOPT_PRE_SMOOTHING_CHECKS.md`)。過去記録には
    # 設計空間が無いので、島除去前の材料(候補が使えた材料の上限)に接する節点へ対応付ける。
    # 対応付けが不成立(対応不能標本など)なら、節点だけを見る条件5を合格にしない。
    table = anchor.build_anchors(soup, grid, allowed_nodes=anchor.active_nodes(original))
    mapping = anchor.condition_mapping(table, bundle.conditions.fixed_faces, bundle.conditions.load_faces,
                                       bundle.forces, material=original)
    setup = document.optimize
    report = inspect(material, original, grid, table=table,
                     fixed_faces=bundle.conditions.fixed_faces, load_faces=bundle.conditions.load_faces,
                     forces=bundle.forces,
                     preserves=[(name, document.get_body(name).shape) for name in setup.preserve_bodies],
                     excludes=[(name, document.get_body(name).shape) for name in setup.exclude_bodies], **kwargs)
    report['boundary_mapping'] = mapping
    if mapping['status'] != 'pass':
        report['checks']['conditions']['status'] = 'fail'
        report['checks']['conditions']['mapping_reasons'] = {
            row['condition']: row['reasons'] for row in mapping['faces'] if row['reasons']}
        statuses = [check['status'] for check in report['checks'].values()]
        report['status'] = 'fail' if 'fail' in statuses else 'unknown' if 'unknown' in statuses else 'pass'
        report['exit_code'] = {'pass': 0, 'fail': 1, 'unknown': 2}[report['status']]
    report['level'] = level
    report['threshold_rule'] = 'rho > level'
    report['volumes']['density_integral_before_mm3'] = float(before.sum() * grid.element_volume)
    report['volumes']['density_integral_after_mm3'] = float(clean.sum() * grid.element_volume)
    return report

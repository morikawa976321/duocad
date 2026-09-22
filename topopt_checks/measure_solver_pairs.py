"""実探索(段階3・4)の評価ごとに、同じ系を cg と直接解法(`spsolve`)の両方で解いた対を記録する。

    .\.venv\Scripts\python.exe topopt_checks\measure_solver_pairs.py --subject hanger --spacing 2.4 --target 4.8 --cells 2 \
        --out <jsonl> --save-accepted <dir> --head <ハッシュ>
    .\.venv\Scripts\python.exe topopt_checks\measure_solver_pairs.py --analyze <jsonl ...>
    .\.venv\Scripts\python.exe topopt_checks\measure_solver_pairs.py --self-check
    .\.venv\Scripts\python.exe topopt_checks\measure_solver_pairs.py --spacing 2.4 --shape-quantities <形.npz ...>

探索は cg のまま進める(軌道は製品と同じ)。実行時に `fem.PreparedSystem.solve`・`binary_search._evaluate_survivor`・
`_geometry_rejection_trial`・`_reduce_volume`・`find_initial_feasible` を包み、評価ごとに1行(JSON Lines)を**評価のたびに**書く。
`duocad/` は書き換えない。自由な自由度が `--pair-limit` 以下で空きメモリが `--pair-min-free-gb` 以上のときだけ、同じ `kf`・右辺を
`spsolve` でも解いて、秒・常駐量の増分・残差・変位の差を対にする(直接解法の解は測ったら捨てる)。
形から決定的に計算できる量(材料セル数・自由度・nnz・外接箱の充填率、対の評価では RCM の帯幅)も各 solve に書く。
`--time-limit-min` は探索の開始(段階3の前)からの壁時計の経過(対の測定の時間を含む)。`--max-evals` は段階4の評価の数
(幾何却下・FEM なしを含む)。`--save-accepted` の `accepted_000.npz` は段階3の全材料の形(段階4の出発形)、`accepted_001.npz` 以降が段階4の合格の通し番号。
`--analyze` は記録から docs 用の表A〜D を markdown で出す(判断はしない)。
"""
import argparse
import dataclasses
import gc
import hashlib
import inspect
import json
import math
import os
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401
import numpy as np
import psutil
import scipy.sparse.linalg as spla
from _hanger_setup import MODELS_DIR, PENAL, _setup, free_gb, model_for
from measure_inner_target_synthetic import SHAPE as SYNTHETIC_SHAPE
from measure_inner_target_synthetic import SUBJECTS as SYNTHETIC_SUBJECTS
from measure_inner_target_synthetic import build_conditions as synthetic_build_conditions
from scipy.sparse.csgraph import reverse_cuthill_mckee

from duocad.topopt import binary_search, fem, trace

LOW_MEMORY_GB = 2.0
RSS_SAMPLE_SECONDS = 0.02
MAX_EVALS_LIMIT = 900
DOF_BINS = ((0, 30_000, '〜3万'), (30_000, 50_000, '3〜5万'), (50_000, 70_000, '5〜7万'), (70_000, 90_000, '7〜9万'),
            (90_000, 110_000, '9〜11万'), (110_000, 130_000, '11〜13万'))
THRESHOLDS = (30_000, 50_000, 70_000, 90_000, 110_000, 130_000)


@dataclasses.dataclass
class Subject:
    label: str
    mask: np.ndarray
    conditions: binary_search.SearchConditions
    spacing: float
    origin: np.ndarray
    extra: dict


DEFAULT_HANGER_MODEL = 'hanger_bar_attachment.json'
HANGER_MODEL_STEM = 'hanger_bar_attachment'


def _model_tag(model_name):
    if model_name == DEFAULT_HANGER_MODEL:
        return ''
    remainder = model_name.removesuffix('.json')
    if remainder.startswith(HANGER_MODEL_STEM):
        remainder = remainder[len(HANGER_MODEL_STEM):]
    return remainder.lstrip('_')


def _hanger_label(spacing, model_name):
    if model_name == DEFAULT_HANGER_MODEL:
        return f'hanger-e{spacing:g}'
    tag = _model_tag(model_name)
    return f'hanger{tag}-e{spacing:g}'


def _build_hanger(args):
    space, conditions, min_width = _setup(args.spacing, model_name=args.model)
    conditions = dataclasses.replace(conditions, target_safety_factor=float(args.target), solver=args.solver)
    model_path = Path(MODELS_DIR) / args.model
    model_sha1 = hashlib.sha1(model_path.read_bytes()).hexdigest()
    return Subject(label=_hanger_label(args.spacing, args.model), mask=space.mask, conditions=conditions,
                   spacing=float(space.grid.spacing), origin=np.asarray(space.grid.origin, dtype=float),
                   extra=dict(spacing_requested=args.spacing, actual_spacing_mm=float(space.grid.spacing),
                              product_min_width_cells=int(min_width['cells']), model_file=args.model,
                              model_sha1=model_sha1))


def _build_synthetic(name):
    """合成の塊(`measure_inner_target_synthetic.SUBJECTS` の S1/S2/S3。80x20x20 = 全設計空間で約10.7万自由度)。
    探索は全材料(`k=1` の格子・確定表)から始める。目標と m は `--target`・`--cells` でこの呼び出し元が指定する
    (`build_conditions` は渡された値をそのまま `target_safety_factor` に入れるので二重に掛からない)。"""
    def build(args):
        spec = SYNTHETIC_SUBJECTS[name]
        conditions = dataclasses.replace(synthetic_build_conditions(name, 1, args.target), solver=args.solver)
        mask = np.ones(SYNTHETIC_SHAPE, dtype=bool)
        return Subject(label=f'synthetic-{name}', mask=mask, conditions=conditions, spacing=1.0,
                       origin=np.zeros(3), extra=dict(subject_name=spec['name'], force_n=list(spec['force'])))
    return build


SUBJECTS = {'hanger': _build_hanger, 'S1': _build_synthetic('S1'), 'S2': _build_synthetic('S2'),
            'S3': _build_synthetic('S3')}
#: `--spacing` は hanger の要素サイズの要求値だけに要る(合成題材は要素サイズ 1 の無次元格子)。
SUBJECTS_REQUIRE_SPACING = {'hanger'}


def build_subject(args):
    """`探索に渡す目標そのもの` を conditions へ入れる。`_setup` の係数 1.0 固定は呼ぶ側で差し替える(ここ)。"""
    subject = SUBJECTS[args.subject](args)
    passed = float(subject.conditions.target_safety_factor)
    assert passed == float(args.target), f'探索へ渡る目標 {passed} != --target {args.target}'
    return subject


class RssPeak:
    """with の間、常駐量(RSS)を 20ms 間隔で標本し、直前からの増分の最大を持つ。"""

    def __init__(self):
        self.process = psutil.Process()
        self.increase = 0.0

    def __enter__(self):
        self.before = self.peak = self.process.memory_info().rss
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _run(self):
        while not self.stop.wait(RSS_SAMPLE_SECONDS):
            self.peak = max(self.peak, self.process.memory_info().rss)

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        self.peak = max(self.peak, self.process.memory_info().rss)
        self.increase = max(self.peak - self.before, 0) / 1e9


def rcm_measures(kf):
    """RCM で並べ替えた下三角の行ごとの帯幅から、最大・profile(合計)・Σ帯幅²と、計算にかかった秒。"""
    started = time.perf_counter()
    permutation = reverse_cuthill_mckee(kf, symmetric_mode=True)
    inverse = np.empty_like(permutation)
    inverse[permutation] = np.arange(permutation.size)
    coo = kf.tocoo()
    row, col = inverse[coo.row], inverse[coo.col]
    lower = col <= row
    first = np.arange(kf.shape[0])
    np.minimum.at(first, row[lower], col[lower])
    width = (np.arange(kf.shape[0]) - first).astype(np.int64)
    return dict(seconds=time.perf_counter() - started, bandwidth_max=int(width.max()), profile=int(width.sum()),
                sum_sq_bandwidth=int((width * width).sum()))


def shape_measures(model, kf):
    """形だけから決まる量(秒を含まない)。"""
    dims = (model.nelx, model.nely, model.nelz)
    if model.active is None:
        cells, bbox = int(np.prod(dims)), int(np.prod(dims))
    else:
        cells = int(model.active.size)
        idx = np.array(np.unravel_index(model.active, dims))
        bbox = int(np.prod(idx.max(axis=1) - idx.min(axis=1) + 1))
    free = int(model.free.size)
    return dict(free_dofs=free, material_cells=cells, nnz=int(kf.nnz), nnz_per_dof=kf.nnz / free,
                bbox_cells=bbox, fill_ratio=cells / bbox)


def solve_direct_pair(kf, ff, u_cg_free, tolerance):
    error = None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with RssPeak() as meter:
            started = time.perf_counter()
            try:
                u_direct = np.asarray(spla.spsolve(kf, ff), dtype=float)
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            seconds = time.perf_counter() - started
    if error is not None:
        return dict(status='error', error=error, seconds=seconds, rss_increase_gb=meter.increase)
    finite = bool(np.all(np.isfinite(u_direct)))
    pair = dict(status='measured', seconds=seconds, rss_increase_gb=meter.increase, finite=finite,
                warnings=sorted({w.category.__name__ for w in caught}), tolerance=tolerance)
    if finite:
        pair['relative_residual'] = float(np.linalg.norm(kf @ u_direct - ff) / np.linalg.norm(ff))
        scale = float(np.abs(u_direct).max())
        pair['max_relative_difference'] = float(np.abs(u_cg_free - u_direct).max() / scale) if scale > 0 else None
    return pair


class CpuMeter:
    """前回の読み取りからの CPU 使用率。`other` は機械全体から自プロセスぶんを引いた値(並走の目安)。"""

    def __init__(self):
        self.process = psutil.Process()
        self.count = psutil.cpu_count()
        psutil.cpu_percent(None)
        self._mark()

    def _mark(self):
        times = self.process.cpu_times()
        self.last = (time.perf_counter(), times.user + times.system)

    def read(self):
        system = psutil.cpu_percent(None)
        wall, cpu = self.last
        times = self.process.cpu_times()
        now = time.perf_counter()
        own = (times.user + times.system - cpu) / max(now - wall, 1e-9) * 100.0 / self.count
        self._mark()
        return dict(cpu_percent_system=system, cpu_percent_self=own, cpu_percent_other=max(system - own, 0.0))


class State:
    def __init__(self, out_path, stats, args, save_dir, subject):
        self.out_path = out_path
        self.stats = stats
        self.args = args
        self.save_dir = save_dir
        self.subject = subject
        self.cpu = CpuMeter()
        self.stage = 3
        self.t_search = None
        self.t4 = None
        self.n = 0
        self.evals4 = 0
        self.accepted_index = 0
        self.parent_cells = None
        self.current = None
        self.other_solves = 0
        self.seen_targets = []
        self.stop = None
        self.cg_rtol = inspect.signature(fem.PreparedSystem.solve).parameters['cg_rtol'].default
        self.stage3_evaluations = None

    def write(self, record):
        with open(self.out_path, 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=float, separators=(', ', ': ')) + '\n')


def _solve_wrapper(original, state):
    args = state.args

    def solve(self, rhs, **kwargs):
        ev = state.current
        if ev is None:
            state.other_solves += 1
            return original(self, rhs, **kwargs)
        state.cg_rtol = kwargs.get('cg_rtol', state.cg_rtol)
        rhs2 = np.atleast_2d(np.asarray(rhs, dtype=float))
        entry = dict(solver=self._solver, nrhs=int(rhs2.shape[0]), **shape_measures(self.model, self.kf))
        ev['solves'].append(entry)
        stats = state.stats
        iterations, fallbacks = stats['cg_iterations'], stats['fallbacks']
        started = time.perf_counter()
        try:
            out = original(self, rhs, **kwargs)
        except Exception as error:
            entry.update(error=f'{type(error).__name__}: {error}', seconds=time.perf_counter() - started,
                         iterations=int(stats['cg_iterations'] - iterations))
            raise
        entry.update(seconds=time.perf_counter() - started, iterations=int(stats['cg_iterations'] - iterations),
                     fallbacks=int(stats['fallbacks'] - fallbacks))
        if self._solver != 'cg':
            return out
        ff = rhs2[0][self.model.free]
        if rhs2.shape[0] != 1 or not np.any(ff):
            entry['pair'] = dict(status='skipped', reason='multi_rhs_or_zero_rhs')
        elif entry['free_dofs'] > args.pair_limit:
            entry['pair'] = dict(status='skipped', reason='dofs_above_limit')
        elif free_gb() < args.pair_min_free_gb:
            entry['pair'] = dict(status='skipped', reason='low_memory', free_gb=free_gb())
        else:
            overhead_started = time.perf_counter()
            pair = solve_direct_pair(self.kf, ff, out[0, self.model.free], fem.SOLVE_RESIDUAL_TOL)
            if pair['status'] == 'measured':
                pair['rcm'] = rcm_measures(self.kf)
            gc.collect()
            pair['overhead_seconds'] = time.perf_counter() - overhead_started
            entry['pair'] = pair
        return out

    return solve


def _emit(state, record):
    state.n += 1
    record.update(n=state.n, stage=state.stage, t_s=round(time.perf_counter() - state.t_search, 2),
                  **state.cpu.read())
    state.write(record)
    _progress(record)


def _removed(operation):
    return int(operation.split(':')[1]) if operation.startswith('remove:') else None


def _save_accepted(state, survivor, record):
    if state.save_dir is None:
        return
    name = f'accepted_{state.accepted_index:03d}.npz'
    np.savez_compressed(state.save_dir / name, mask=survivor, origin=state.subject.origin,
                        spacing=state.subject.spacing)
    record['accepted_file'] = name
    state.accepted_index += 1


def _evaluate_wrapper(original, state):
    def evaluate(survivor, before, conditions, **kwargs):
        ev = state.current = dict(solves=[])
        try:
            report, trial = original(survivor, before, conditions, **kwargs)
        finally:
            state.current = None
        fields = kwargs.get('fields')
        safety = report['checks']['safety']
        accepted = report['status'] == 'pass' and (state.stage == 3 or (fields is not None and 'element_energy' in fields))
        operation = kwargs.get('operation', '')
        cells = int(survivor.sum())
        record = dict(event='eval', operation=operation, removed=_removed(operation), parent_cells=state.parent_cells,
                      cells=cells,
                      outcome='accepted' if accepted else 'rejected' if ev['solves'] else 'no_fem',
                      failed=sorted(name for name, check in report['checks'].items() if check.get('status') != 'pass'),
                      safety_factor=safety.get('safety_factor'), solves=ev['solves'])
        if accepted:
            _save_accepted(state, survivor, record)
            state.parent_cells = cells
        _count_stage4(state)
        _emit(state, record)
        return report, trial

    return evaluate


def _geometry_wrapper(original, state):
    def rejection_trial(history, candidate, parent, operation, conditions, rejection):
        trial = original(history, candidate, parent, operation, conditions, rejection)
        _count_stage4(state)
        _emit(state, dict(event='eval', operation=operation, removed=_removed(operation),
                          parent_cells=state.parent_cells, cells=int(candidate.sum()), outcome='geometry_rejected',
                          failed=['geometry'], safety_factor=None, solves=[]))
        return trial

    return rejection_trial


def _count_stage4(state):
    if state.stage == 4:
        state.evals4 += 1


def _watch_target(original, state):
    signature = inspect.signature(original)

    def watched(*a, **k):
        seen = float(signature.bind(*a, **k).arguments['conditions'].target_safety_factor)
        state.seen_targets.append(seen)
        assert seen == float(state.args.target), f'探索が受け取った目標 {seen} != --target {state.args.target}'
        return original(*a, **k)

    return watched


def _reduce_wrapper(original, state):
    inner = _watch_target(original, state)

    def reduce_volume(initial, *a, **k):
        state.stage = 4
        state.t4 = time.perf_counter()
        state.parent_cells = int(initial.best_feasible.mask.sum())
        state.stage3_evaluations = int(initial.evaluations)
        try:
            return inner(initial, *a, **k)
        finally:
            state.stage = 3

    return reduce_volume


def _progress(record):
    pairs = [s.get('pair') or {} for s in record['solves']]
    pair = next((p for p in pairs if p.get('status') == 'measured'), None)
    first = record['solves'][0] if record['solves'] else {}
    text = f"eval {record['n']:4d} s{record['stage']} {record['operation']:>10} {record['outcome']:>17} " \
           f"cells {record['cells']} dofs {first.get('free_dofs', '-')} {first.get('solver', '-')} {first.get('iterations', '-')}it " \
           f"{first.get('seconds', 0.0):.1f}s"
    if pair is not None:
        text += f" direct {pair['seconds']:.1f}s +{pair['rss_increase_gb']:.2f}GB"
    elif 'pair' in first:
        text += f" (対なし: {first['pair'].get('reason')})"
    print(f"{text} t={record['t_s']:.0f}s free={free_gb():.1f}GB", flush=True)


def _peak_working_set_gb():
    info = psutil.Process().memory_info()
    return getattr(info, 'peak_wset', info.rss) / 1e9


def _header(args, subject, free_at_start):
    conditions = subject.conditions
    return dict(event='header', head=args.head, subject=args.subject, label=subject.label, target_arg=float(args.target),
                conditions_target_safety_factor=float(conditions.target_safety_factor), min_width_cells=int(args.cells),
                grid_shape=list(subject.mask.shape), design_cells=int(subject.mask.sum()), threads=int(fem.PARALLEL_THREADS),
                cg_rtol=None, solver=conditions.solver, pair_limit=args.pair_limit, pair_min_free_gb=args.pair_min_free_gb,
                max_evals=args.max_evals, time_limit_min=args.time_limit_min, free_gb_at_start=free_at_start,
                residual_tolerance=fem.SOLVE_RESIDUAL_TOL, duocad_root=str(Path(fem.__file__).resolve().parents[2]),
                finished=False, stop_reason=None, **subject.extra)


def run(args, subject, out_path, save_dir, cap=None):
    """探索を1本回して out_path へ書く。`cap` は段階4の評価数の上限を差し替える(`--self-check` 用)。"""
    if subject.conditions.solver not in ('cg', 'auto'):
        raise SystemExit(f"conditions.solver が {subject.conditions.solver!r}(cg か auto を想定)")
    if subject.conditions.solver == 'auto' and args.pair_limit != 0:
        raise SystemExit('--solver auto では対を測らない(直接解法へ回った評価は cg で解いていない)。--pair-limit 0 にする')
    if Path(fem.__file__).resolve().parents[2] != _bootstrap._REPO_ROOT:
        raise SystemExit(f'読み込んだ duocad が {fem.__file__}(このスクリプトの隣のソースではない。editable install の罠)')
    header = _header(args, subject, free_gb())
    if cap is not None:
        header['max_evals'] = cap
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(header, ensure_ascii=False, default=float) + '\n', encoding='utf-8', newline='\n')
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
    max_evals = header['max_evals']
    with tempfile.TemporaryDirectory(prefix='topopt-trace-') as trace_root, \
            patch.object(trace, 'DEFAULT_ROOT', Path(trace_root)), fem.collect_solve_stats() as stats:
        state = State(out_path, stats, args, save_dir, subject)
        state.t_search = time.perf_counter()
        limit_s = args.time_limit_min * 60.0

        def is_cancelled():
            if state.stage != 4:
                return False
            if state.evals4 >= max_evals:
                state.stop = 'evaluation_cap'
            elif time.perf_counter() - state.t_search >= limit_s:
                state.stop = 'time_limit'
            elif free_gb() < LOW_MEMORY_GB:
                state.stop = 'low_memory'
            return state.stop is not None

        with patch.object(fem.PreparedSystem, 'solve', _solve_wrapper(fem.PreparedSystem.solve, state)), \
                patch.object(binary_search, '_evaluate_survivor', _evaluate_wrapper(binary_search._evaluate_survivor, state)), \
                patch.object(binary_search, '_geometry_rejection_trial',
                             _geometry_wrapper(binary_search._geometry_rejection_trial, state)), \
                patch.object(binary_search, '_reduce_volume', _reduce_wrapper(binary_search._reduce_volume, state)), \
                patch.object(binary_search, 'find_initial_feasible',
                             _watch_target(binary_search.find_initial_feasible, state)):
            started = time.perf_counter()
            result = binary_search.find_best_feasible(subject.mask, subject.conditions, min_width_cells=args.cells,
                                                      is_cancelled=is_cancelled)
            total = time.perf_counter() - started
    reduction = (result.diagnostics or {}).get('reduction') or {}
    header.update(
        finished=True, cg_rtol=state.cg_rtol, stop_reason=reduction.get('stop_reason'), termination=result.termination,
        runner_stop=state.stop, search_status=result.status, evaluations_total=int(result.evaluations),
        stage3_evaluations=state.stage3_evaluations, stage4_records=state.evals4, records_written=state.n,
        record_count_matches=(state.n == int(result.evaluations)),
        stage4_steps_in_diagnostics=len(reduction.get('steps') or []),
        search_targets_seen=state.seen_targets, total_seconds=total,
        stage3_seconds=(state.t4 - state.t_search) if state.t4 is not None else None,
        stage4_seconds=(time.perf_counter() - state.t4) if state.t4 is not None else None,
        other_solves=state.other_solves, peak_working_set_gb=_peak_working_set_gb(), free_gb_at_end=free_gb(),
        final_safety_factor=(float(result.best_feasible.safety_factor) if result.best_feasible else None),
        final_cells=(int(result.best_feasible.mask.sum()) if result.best_feasible else None),
        final_mask_hash=(result.best_feasible.mask_hash if result.best_feasible else None))
    rewrite_header(out_path, header)
    print(json.dumps({k: v for k, v in header.items() if k != 'event'}, ensure_ascii=False, default=float), flush=True)
    return result, state


def read_records(path):
    lines = [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]
    return lines[0], [line for line in lines[1:] if line.get('event') == 'eval']


def rewrite_header(path, header):
    path = Path(path)
    lines = path.read_text(encoding='utf-8').splitlines()
    lines[0] = json.dumps(header, ensure_ascii=False, default=float)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    os.replace(temporary, path)


def self_check(args):
    """包まない探索と包んだ探索を同じ評価数で回し、cg の反復数の列・採用形状が一致すること、対が測れて解が一致することを確かめる。"""
    cap = 30
    args.subject, args.spacing, args.target, args.cells = 'hanger', 4.8, 2.0, 2
    args.model = DEFAULT_HANGER_MODEL
    args.solver = 'cg'
    # 4.8mm の太い形(目標 4.8 の探索が止まる 10万自由度前後)は spsolve が 8 分待っても終わらなかった(2026-09-22)ので、小さい形だけ対にする。
    args.pair_limit = 50_000
    subject = build_subject(args)
    plain_events = []

    def on_evaluation(event):
        plain_events.append(event)

    def plain_cancelled():
        return sum(e['outcome'] != 'initial' for e in plain_events) >= cap

    with tempfile.TemporaryDirectory(prefix='topopt-trace-') as root, patch.object(trace, 'DEFAULT_ROOT', Path(root)), \
            fem.collect_solve_stats() as plain_stats:
        plain = binary_search.find_best_feasible(subject.mask, subject.conditions, min_width_cells=args.cells,
                                                 is_cancelled=plain_cancelled, on_evaluation=on_evaluation)
    with tempfile.TemporaryDirectory(prefix='solver-pairs-selfcheck-') as folder:
        out = Path(folder) / 'check.jsonl'
        wrapped, _ = run(args, subject, out, None, cap=cap)
        header, records = read_records(out)
    problems = []
    # 最後の solve は探索の終わりの再検査(評価の記録に入らない)なので、記録に入る評価の solve だけを並べる。
    plain_iterations = plain_stats['cg_iterations_each']
    wrapped_iterations = [s['iterations'] for r in records for s in r['solves']]
    if plain_iterations[:len(wrapped_iterations)] != wrapped_iterations:
        problems.append(f'cg の反復数の列が包む前と違う: {plain_iterations[:len(wrapped_iterations)]} / {wrapped_iterations}')
    if plain.best_feasible.mask_hash != wrapped.best_feasible.mask_hash:
        problems.append('採用形状のハッシュが包む前と違う')
    if plain.evaluations != wrapped.evaluations:
        problems.append(f'評価数が違う: {plain.evaluations} / {wrapped.evaluations}')
    plain_steps = [(e['outcome'] == 'accepted', e['candidate_cells'], e['candidate_safety_factor'])
                   for e in plain_events if e['outcome'] != 'initial']
    wrapped_steps = [(r['outcome'] == 'accepted', r['cells'], r['safety_factor']) for r in records if r['stage'] == 4]
    if len(plain_steps) != len(wrapped_steps) or not all(
            a[:2] == b[:2] and (a[2] == b[2] or (a[2] is not None and b[2] is not None and math.isclose(a[2], b[2], rel_tol=1e-12)))
            for a, b in zip(plain_steps, wrapped_steps)):
        problems.append(f'段階4の評価ごとの(合否・セル数・安全率)が包む前と違う: {plain_steps} / {wrapped_steps}')
    pairs = [s['pair'] for r in records for s in r['solves'] if (s.get('pair') or {}).get('status') == 'measured']
    if not pairs:
        problems.append('対が1つも測れていない')
    for pair in pairs:
        if not pair['finite'] or pair['relative_residual'] > pair['tolerance'] or not pair['max_relative_difference'] < 1e-6:
            problems.append(f'対の解が一致しない: {pair}')
    print(f"self-check: 評価 {len(records)}・対 {len(pairs)}・cg 反復 {wrapped_iterations}・"
          f"変位の最大相対差の最大 {max((p['max_relative_difference'] for p in pairs), default=None)}")
    if problems:
        print('self-check FAIL\n' + '\n'.join(problems))
        return 1
    print('self-check PASS')
    return 0


def _median(values):
    return float(np.median(values)) if len(values) else None


def _fmt(value, spec='.3f'):
    return '–' if value is None else format(value, spec)


def _rows(records):
    """段階4の評価から、cg で解いた solve を1行ずつ取り出す(評価の情報を付ける)。"""
    for record in records:
        if record['stage'] != 4:
            continue
        for solve in record['solves']:
            if solve['solver'] == 'cg' and 'seconds' in solve:
                pair = solve.get('pair') or {}
                yield dict(solve, record=record, measured=pair.get('status') == 'measured', pair=pair)


def _stage4_seconds(header, records):
    """探索の外の時間(対の測定)を引いた段階4の秒。閉じていない記録は最後の評価の時刻から推定する。"""
    overhead = sum((s.get('pair') or {}).get('overhead_seconds', 0.0) for r in records if r['stage'] == 4 for s in r['solves'])
    if header.get('stage4_seconds') is not None:
        return header['stage4_seconds'] - overhead, False
    stage3 = [r['t_s'] for r in records if r['stage'] == 3]
    stage4 = [r['t_s'] for r in records if r['stage'] == 4]
    if not stage4:
        return 0.0, True
    return stage4[-1] - (stage3[-1] if stage3 else 0.0) - overhead, True


def table_a(rows):
    labels = list(DOF_BINS)
    if any(r['free_dofs'] > DOF_BINS[-1][1] for r in rows):
        labels.append((DOF_BINS[-1][1], float('inf'), '13万超'))
    lines = ['| 自由度 | 評価数 | 対の数 | 秒の比 direct/cg 中央値 | 最小 | 最大 | direct 増分の最大(GB) | cg 反復の中央値 |',
             '|---|---|---|---|---|---|---|---|']
    for low, high, name in labels:
        group = [r for r in rows if low < r['free_dofs'] <= high]
        pairs = [r for r in group if r['measured']]
        ratios = [r['pair']['seconds'] / r['seconds'] for r in pairs if r['seconds'] > 0]
        lines.append(f"| {name} | {len(group)} | {len(pairs)} | {_fmt(_median(ratios))} | {_fmt(min(ratios) if ratios else None)} | "
                     f"{_fmt(max(ratios) if ratios else None)} | "
                     f"{_fmt(max((r['pair']['rss_increase_gb'] for r in pairs), default=None), '.2f')} | "
                     f"{_fmt(_median([r['iterations'] for r in group]), '.0f')} |")
    return '\n'.join(lines)


def _percent(part, whole):
    return '–' if whole <= 0 else f'{part / whole * 100:.1f}%'


def policy_row(rows, threshold, stage4_seconds):
    cg_total = sum(r['seconds'] for r in rows)
    below = [r for r in rows if r['free_dofs'] <= threshold]
    unpaired = [r for r in below if not r['measured']]
    paired = [r for r in below if r['measured']]
    total = sum(r['seconds'] for r in rows if r['free_dofs'] > threshold) + sum(r['seconds'] for r in unpaired) \
        + sum(r['pair']['seconds'] for r in paired)
    slower = [r['pair']['seconds'] - r['seconds'] for r in paired if r['pair']['seconds'] > r['seconds']]
    return (f'| {threshold / 1e4:g}万 | {len(below)} | {len(unpaired)} | {total:.0f} | '
            f'{_percent(total, cg_total)} | {_percent(total, stage4_seconds)} | {len(slower)} | '
            f"{_fmt(max(slower, default=None), '.1f')} | "
            f"{_fmt(max((r['pair']['rss_increase_gb'] for r in paired), default=None), '.2f')} |")


def table_b(rows, stage4_seconds):
    cg_total = sum(r['seconds'] for r in rows)
    lines = [f'cg だけのときの段階4の solve の秒の合計 {cg_total:.0f}・段階4全体 {stage4_seconds:.0f}(対の測定の時間を除く)\n',
             '| T | T 以下の評価数 | うち対が無く cg のまま | 直接解法にした solve 秒の合計 | cg だけの合計に対する割合 | '
             '段階4全体に対する割合 | T 以下で direct のほうが遅かった評価数 | 最大の損(秒) | direct 増分の最大(GB) |',
             '|---|---|---|---|---|---|---|---|---|']
    lines += [policy_row(rows, t, stage4_seconds) for t in THRESHOLDS]
    return '\n'.join(lines)


def table_c(rows):
    pairs = [r for r in rows if r['measured']]
    tolerance = next((r['pair'].get('tolerance') for r in pairs), fem.SOLVE_RESIDUAL_TOL)
    differences = [r['pair']['max_relative_difference'] for r in pairs if r['pair'].get('max_relative_difference') is not None]
    return '\n'.join([
        '| 対の数 | うち不合格の評価 | 残差検算を通らなかった | 非有限 | 警告が出た | direct の例外 | '
        'cg がフォールバック(対のうち) | cg がフォールバック(段階4の全 solve) | 変位の最大相対差の最大 |',
        '|---|---|---|---|---|---|---|---|---|',
        f"| {len(pairs)} | {sum(r['record']['outcome'] == 'rejected' for r in pairs)} | "
        f"{sum(1 for r in pairs if r['pair'].get('finite') and r['pair']['relative_residual'] > tolerance)} | "
        f"{sum(1 for r in pairs if not r['pair'].get('finite', True))} | "
        f"{sum(1 for r in pairs if r['pair'].get('warnings'))} | "
        f"{sum(1 for r in rows if r['pair'].get('status') == 'error')} | "
        f"{sum(1 for r in pairs if r.get('fallbacks'))} | {sum(1 for r in rows if r.get('fallbacks'))} | "
        f"{_fmt(max(differences, default=None), '.2e')} |"])


def table_d(rows):
    pairs = [r for r in rows if r['measured'] and r['pair'].get('finite')]
    lines = ['| 説明変数 | 点数 | 傾き | 決定係数 | 実測/予測の最大 | 最小 |', '|---|---|---|---|---|---|']
    columns = (('自由な自由度', lambda r: r['free_dofs']), ('nnz', lambda r: r['nnz']),
               ('RCM の Σ帯幅²', lambda r: r['pair']['rcm']['sum_sq_bandwidth']))
    for name, getter in columns:
        if len(pairs) < 3:
            lines.append(f'| {name} | {len(pairs)} | – | – | – | – |')
            continue
        x = np.log([max(getter(r), 1) for r in pairs])
        y = np.log([max(r['pair']['seconds'], 1e-6) for r in pairs])
        slope, intercept = np.polyfit(x, y, 1)
        fitted = slope * x + intercept
        ss_res = float(((y - fitted) ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum())
        ratio = np.exp(y - fitted)
        lines.append(f'| {name} | {len(pairs)} | {slope:.3f} | {1 - ss_res / ss_tot if ss_tot > 0 else float("nan"):.3f} | '
                     f'{ratio.max():.2f} | {ratio.min():.2f} |')
    return '\n'.join(lines)


def analyze_group(title, headers, records_list):
    records = [r for records in records_list for r in records]
    rows = list(_rows(records))
    seconds, estimated = zip(*[_stage4_seconds(h, r) for h, r in zip(headers, records_list)])
    print(f'### {title}\n')
    stage4 = [r for r in records if r['stage'] == 4]
    counts = {}
    for record in stage4:
        counts[record['outcome']] = counts.get(record['outcome'], 0) + 1
    stage3 = [s for r in records if r['stage'] == 3 for s in r['solves'] if s['solver'] == 'cg']
    print(f'- 段階4の評価 {len(stage4)}({counts})・cg の solve {len(rows)}・対 {sum(r["measured"] for r in rows)}・'
          f'段階3の cg solve {len(stage3)}(対 {sum((s.get("pair") or {}).get("status") == "measured" for s in stage3)}。表には入れない)'
          f"{'・段階4の秒は記録が閉じていないため最後の評価の時刻から推定' if any(estimated) else ''}\n")
    print('**表A 自由度の区間ごとの秒の比**\n')
    print(table_a(rows) + '\n')
    print('**表B 自由度が T 以下を直接解法にしたときの段階4の solve の秒**(対の実測を当てはめる。判断ではない)\n')
    print(table_b(rows, sum(seconds)) + '\n')
    print('**表C 直接解法の頑健さ**\n')
    print(table_c(rows) + '\n')
    print('**表D log(direct 秒)の直線当てはめ**(記述であって規則の提案ではない)\n')
    print(table_d(rows) + '\n')
    pairs = [r for r in rows if r['measured']]
    if pairs:
        print('**対の一覧**(段階4。評価番号は記録の `n`)\n')
        print(table_pairs(pairs) + '\n')


def table_pairs(pairs):
    lines = ['| n | 材料セル | 自由な自由度 | nnz/自由度 | 充填率 | RCM の Σ帯幅² | cg 秒 | direct 秒 | 比 | direct 増分(GB) |',
             '|---|---|---|---|---|---|---|---|---|---|']
    for r in pairs:
        pair = r['pair']
        lines.append(f"| {r['record']['n']} | {r['material_cells']:,} | {r['free_dofs']:,} | {r['nnz_per_dof']:.1f} | "
                     f"{r['fill_ratio']:.3f} | {pair['rcm']['sum_sq_bandwidth']:,} | {r['seconds']:.2f} | {pair['seconds']:.2f} | "
                     f"{pair['seconds'] / r['seconds']:.2f} | {pair['rss_increase_gb']:.2f} |")
    return '\n'.join(lines)


#: 規則1・3・4の自由度の閾値 T(固定の6値: 2・3・5・7・9・11万)。規則2(N)・3(r)・4(φ)・5(B)の第二引数は、
#: この母集団で実際に観測された値そのものを候補にする(隣り合う観測値の間はルーティングの分かれ目にならないので、これで総当たりになる)。
DOF_T_GRID = ((20_000, '2万'), (30_000, '3万'), (50_000, '5万'), (70_000, '7万'), (90_000, '9万'), (110_000, '11万'))
RULE_NAMES = {1: '規則1: 自由な自由度 ≤ T', 2: '規則2: kf.nnz ≤ N', 3: '規則3: 自由度 ≤ T かつ nnz/自由度 ≤ r',
              4: '規則4: 自由度 ≤ T かつ 外接箱の充填率 ≤ φ', 5: '規則5: RCM の Σ帯幅² ≤ B(RCM の秒を規則の費用に足す)'}


def _population(groups):
    """表E の母集団: 対の実測(`pair.status=='measured'`)がある全評価を、全 jsonl から集める(題材・格子のキー付き)。"""
    rows = []
    for key, members in groups.items():
        for _, _, records in members:
            rows += [dict(row, group=key) for row in _rows(records) if row['measured']]
    return rows


def _sorted_unique(values):
    return sorted(set(values))


def _no_cost(_rows):
    return 0.0


def _rcm_cost(rows):
    return sum(r['pair']['rcm']['seconds'] for r in rows)


def _rule_candidates(rule_id, population):
    """(条件の説明, ルーティング述語, 費用関数)の総当たりの列。費用関数は行の部分集合を受けて規則の追加費用(規則5のみ非0)を返す。"""
    if rule_id == 1:
        return [(f'自由度≤{name}({t:,})', (lambda t=t: lambda r: r['free_dofs'] <= t)(), _no_cost) for t, name in DOF_T_GRID]
    if rule_id == 2:
        return [(f'nnz≤{n:,}', (lambda n=n: lambda r: r['nnz'] <= n)(), _no_cost)
                for n in _sorted_unique(r['nnz'] for r in population)]
    if rule_id == 3:
        # 候補は観測値そのもの(丸めない)。丸めた値を閾値にすると、丸めが切り捨て方向に働いた行は
        # 自分自身の観測値から作った閾値 `<=` を満たせなくなり、総当たりが観測値を網羅しなくなる。
        r_values = _sorted_unique(r['nnz_per_dof'] for r in population)
        return [(f'自由度≤{name} かつ nnz/自由度≤{rv:.2f}', (lambda t=t, rv=rv: lambda r: r['free_dofs'] <= t and r['nnz_per_dof'] <= rv)(),
                 _no_cost) for t, name in DOF_T_GRID for rv in r_values]
    if rule_id == 4:
        phi_values = _sorted_unique(r['fill_ratio'] for r in population)  # 同上(丸めない)
        return [(f'自由度≤{name} かつ 充填率≤{p:.3f}', (lambda t=t, p=p: lambda r: r['free_dofs'] <= t and r['fill_ratio'] <= p)(),
                 _no_cost) for t, name in DOF_T_GRID for p in phi_values]
    if rule_id == 5:
        return [(f'Σ帯幅²≤{b:,}', (lambda b=b: lambda r: r['pair']['rcm']['sum_sq_bandwidth'] <= b)(), _rcm_cost)
                for b in _sorted_unique(r['pair']['rcm']['sum_sq_bandwidth'] for r in population)]
    raise ValueError(rule_id)


def _policy_metrics(rows, predicate, cost_fn):
    routed = [r for r in rows if predicate(r)]
    kept = [r for r in rows if not predicate(r)]
    cg_total = sum(r['seconds'] for r in rows)
    policy_total = sum(r['pair']['seconds'] for r in routed) + sum(r['seconds'] for r in kept) + cost_fn(rows)
    slower = [r for r in routed if r['pair']['seconds'] > r['seconds']]
    worst = max(slower, key=lambda r: r['pair']['seconds'] - r['seconds']) if slower else None
    missed = [r for r in kept if r['pair']['seconds'] < r['seconds']]
    return dict(routed=len(routed), cg_total=cg_total, policy_total=policy_total,
                ratio=(policy_total / cg_total if cg_total > 0 else None), slower_count=len(slower),
                max_loss=(worst['pair']['seconds'] - worst['seconds']) if worst else 0.0,
                max_loss_increase=(worst['pair']['rss_increase_gb'] if worst else None), missed_count=len(missed),
                missed_seconds=sum(r['seconds'] - r['pair']['seconds'] for r in missed),
                missed_increase_max=max((r['pair']['rss_increase_gb'] for r in missed), default=None))


def _select_best(rule_id, population):
    """`損の最大が1秒以下`の中で秒の合計が最小の組を選ぶ。1つも無ければ損の最大が最小の組で代える(その旨を表に書く)。"""
    candidates = _rule_candidates(rule_id, population)
    scored = [(desc, predicate, cost_fn, _policy_metrics(population, predicate, cost_fn)) for desc, predicate, cost_fn in candidates]
    qualifying = [i for i, (*_a, m) in enumerate(scored) if m['max_loss'] <= 1.0]
    if qualifying:
        best_idx = min(qualifying, key=lambda i: scored[i][3]['policy_total'])
        return scored, best_idx, True
    best_idx = min(range(len(scored)), key=lambda i: scored[i][3]['max_loss'])
    return scored, best_idx, False


def _rule_rows_markdown(scored, best_idx):
    lo, hi = max(0, best_idx - 1), min(len(scored), best_idx + 2)
    lines = ['| | 条件 | 直接解法へ回る評価数 | 秒の合計 | cg合計比 | 損えた評価数 | 損の最大(秒) | そのときの増分(GB) | '
             '取りこぼした評価数 | 取りこぼした秒 | 取りこぼし増分の最大(GB) |', '|---|---|---|---|---|---|---|---|---|---|---|']
    for i in range(lo, hi):
        desc, _predicate, _cost_fn, m = scored[i]
        marker = '選定' if i == best_idx else ('前' if i < best_idx else '後')
        ratio = f"{m['ratio'] * 100:.1f}%" if m['ratio'] is not None else '–'
        lines.append(f"| {marker} | {desc} | {m['routed']} | {m['policy_total']:.1f}s | {ratio} | {m['slower_count']} | "
                     f"{_fmt(m['max_loss'], '.2f')} | {_fmt(m['max_loss_increase'], '.2f')} | {m['missed_count']} | "
                     f"{_fmt(m['missed_seconds'], '.1f')} | {_fmt(m['missed_increase_max'], '.2f')} |")
    return '\n'.join(lines)


def _group_breakdown_markdown(population, predicate, cost_fn):
    by_group = {}
    for row in population:
        by_group.setdefault(row['group'], []).append(row)
    lines = ['| 題材・目標・m・格子 | 対の数 | 直接解法へ回る数 | cgだけの秒 | 規則下の秒 | cg合計比 |', '|---|---|---|---|---|---|']
    for (label, grid, target, cells), rows in by_group.items():
        m = _policy_metrics(rows, predicate, cost_fn)
        ratio = f"{m['ratio'] * 100:.1f}%" if m['ratio'] is not None else '–'
        lines.append(f"| {label}・目標{target:g}・m={cells}・{list(grid)} | {len(rows)} | {m['routed']} | "
                     f"{m['cg_total']:.1f}s | {m['policy_total']:.1f}s | {ratio} |")
    return '\n'.join(lines)


def table_e(groups):
    """表E: 規則1〜5を候補の総当たりに当てた成績表(採用の決定はしない)。"""
    population = _population(groups)
    print('### 表E cg と直接解法の使い分け条件の候補ごとの成績表(判断はしない。母集団は対の実測がある全評価)\n')
    print(f'母集団: {len(population)} 対(対の実測がある評価。無い測定は規則1〜4でも「対の実測がある」母集団には入らない)\n')
    if not population:
        print('対が1つも無いので規則を当てられない。\n')
        return
    for rule_id in (1, 2, 3, 4, 5):
        print(f'**{RULE_NAMES[rule_id]}**\n')
        scored, best_idx, qualifies = _select_best(rule_id, population)
        if not qualifies:
            print('損の最大が1秒以下の組は無かった(下は損の最大が最小の組とその前後)。\n')
        print(_rule_rows_markdown(scored, best_idx) + '\n')
        _desc, predicate, cost_fn, _m = scored[best_idx]
        print('選定した組の題材・格子ごとの内訳:\n')
        print(_group_breakdown_markdown(population, predicate, cost_fn) + '\n')


def analyze(paths):
    sys.stdout.reconfigure(encoding='utf-8')
    print('cg と直接解法の対(`--analyze`。判断はしない)\n')
    groups = {}
    for path in paths:
        header, records = read_records(path)
        key = (header['label'], tuple(header['grid_shape']), header['conditions_target_safety_factor'],
               header['min_width_cells'])
        groups.setdefault(key, []).append((path, header, records))
    for (label, _grid, target, cells), members in groups.items():
        for path, header, _ in members:
            stop = header.get('runner_stop') or header.get('stop_reason')
            print(f'## {Path(path).name}({label}・目標 {target:g}・m={cells}・格子 {header["grid_shape"]}・'
                  f'設計空間 {header["design_cells"]:,} セル・HEAD {header.get("head")}・停止 {stop}・'
                  f'段階4 {(header.get("stage4_seconds") or 0) / 60:.1f}分(対の測定を含む)・'
                  f'ピーク常駐 {header.get("peak_working_set_gb") or 0:.1f}GB・'
                  f'記録数と評価数の一致 {header.get("record_count_matches")})\n')
        analyze_group(f'{label}・目標 {target:g}・m={cells}', [h for _, h, _ in members], [r for _, _, r in members])
    if len(groups) > 1:
        members = [m for group in groups.values() for m in group]
        print('## 全部を合わせた表\n')
        analyze_group('全測定', [h for _, h, _ in members], [r for _, _, r in members])
    table_e(groups)
    return 0


def shape_quantities(args):
    """既存の形(mask を持つ npz)に、記録と同じ形の量を当てる。探索も直接解法も走らせない。"""
    subject = build_subject(args)
    print('| 形 | 材料セル | 自由な自由度 | nnz | nnz/自由度 | 充填率 | RCM 帯幅の最大 | profile | Σ帯幅² | RCM の秒 |\n'
          '|---|---|---|---|---|---|---|---|---|---|')
    for path in args.shape_quantities:
        mask = np.load(path)['mask'].astype(bool)
        if mask.shape != subject.mask.shape:
            raise SystemExit(f'格子が違う: モデル {subject.mask.shape} / {path} {mask.shape}')
        model = model_for(mask, subject.conditions)
        kf = model.prepare(mask.astype(float), PENAL, solver='direct').kf
        shape, rcm = shape_measures(model, kf), rcm_measures(kf)
        print(f"| {Path(path).stem} | {shape['material_cells']:,} | {shape['free_dofs']:,} | {shape['nnz']:,} | "
              f"{shape['nnz_per_dof']:.1f} | {shape['fill_ratio']:.3f} | {rcm['bandwidth_max']:,} | {rcm['profile']:,} | "
              f"{rcm['sum_sq_bandwidth']:,} | {rcm['seconds']:.2f} |", flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--subject', default='hanger', choices=sorted(SUBJECTS))
    parser.add_argument('--spacing', type=float, help='要素サイズの要求値[mm](hanger。製品と同じ丸めを通す)')
    parser.add_argument('--model', default=DEFAULT_HANGER_MODEL,
                        help='models/ 内のファイル名(hanger)')
    parser.add_argument('--target', type=float, default=4.8, help='探索に渡す目標そのもの(製品の既定は 2.0 × 係数 2.4 = 4.8)')
    parser.add_argument('--cells', type=int, default=2, help='最小幅のセル数 m')
    parser.add_argument('--max-evals', type=int, default=300)
    parser.add_argument('--time-limit-min', type=float, default=150.0)
    parser.add_argument('--min-free-gb', type=float, default=12.0, help='起動時の空きメモリの下限')
    parser.add_argument('--pair-limit', type=int, default=130_000, help='対を測る自由な自由度の上限')
    parser.add_argument('--pair-min-free-gb', type=float, default=8.0, help='対を測る直前の空きメモリの下限')
    parser.add_argument('--solver', default='cg', choices=('cg', 'auto'),
                        help='探索の解法。cg = 全評価を cg で解き、対を測る。auto = 製品の既定(fem.choose_solver が cg / direct を選ぶ)で回し、'
                             '評価ごとに使った解法と秒を記録する(対は測らない。--pair-limit 0 が要る)')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--save-accepted', type=Path, default=None)
    parser.add_argument('--head', default='unknown', help='測定に使ったコードの HEAD のハッシュ(記録用。実行したソースは検証しない)')
    parser.add_argument('--analyze', type=Path, nargs='+', metavar='JSONL')
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--shape-quantities', type=Path, nargs='+', metavar='NPZ',
                        help='既存の形に記録と同じ量(自由度・nnz・充填率・RCM の帯幅)を当てる。--spacing が要る')
    args = parser.parse_args()
    if args.analyze:
        return analyze(args.analyze)
    if args.shape_quantities:
        if args.spacing is None:
            parser.error('--shape-quantities には --spacing が要る')
        return shape_quantities(args)
    if args.self_check:
        return self_check(args)
    if args.out is None:
        parser.error('--out が要る(または --analyze・--self-check・--shape-quantities)')
    if args.subject in SUBJECTS_REQUIRE_SPACING and args.spacing is None:
        parser.error(f'--subject {args.subject} には --spacing が要る')
    if args.max_evals > MAX_EVALS_LIMIT:
        parser.error(f'--max-evals は {MAX_EVALS_LIMIT} 以下(探索の評価予算は find_best_feasible の既定 1000 で、段階3を含む)')
    if args.out.exists():
        raise SystemExit(f'{args.out} は既にある(探索は途中から再開できない。別の名前にするか消す)')
    free = free_gb()
    if free < args.min_free_gb:
        print(f'空きメモリ {free:.1f}GB が下限 {args.min_free_gb}GB 未満。測らずに終わる', flush=True)
        return 3
    run(args, build_subject(args), args.out, args.save_accepted)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

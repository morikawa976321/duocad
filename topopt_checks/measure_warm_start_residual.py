"""段階4の warm start の初期残差 r0 を、実探索(製品の既定 = cold・目標2.0・m=1)の軌道で評価ごとに測る。

    .\.venv\Scripts\python.exe topopt_checks\measure_warm_start_residual.py --spacing 2.4 --out <jsonl> --head <ハッシュ>
    .\.venv\Scripts\python.exe topopt_checks\measure_warm_start_residual.py --analyze <2.4mm.jsonl> <2.0mm.jsonl> [--kcurve <dir>]

`find_best_feasible` を cold で回し、実行時に `fem.PreparedSystem.solve`・`binary_search.peel`・`_evaluate_survivor`・
`_reduce_volume` を包んで、段階4の評価(採用・不合格・幾何却下・FEM を解かず)ごとに1行(JSON Lines)を**評価のたびに**書く。
`duocad/` は書き換えない。段階3の評価は数えない。軌道は包まない cold と同じ(副測定の解は捨てる)。
r0 = ‖K候補·u親 − f‖ / ‖f‖(自由な自由度)。親の変位 = 直前に採用された評価の解。段階4の最初の評価は親の変位が無いので r0 は None。
段階4の評価が `--max-evals` に達するか、段階4の開始から `--time-limit-min` 分を過ぎたら止める。
`--analyze` は記録から docs 用の表(r0 の分布・副測定の比・「r0 < θ のときだけ warm」の削減率)を markdown で出す(判断はしない)。
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401
import numpy as np
from measure_warm_start import free_gb
from measure_warm_start_kcurve import _setup

from duocad.topopt import binary_search, fem, trace

SIDE_CELL_LIMIT = 50_000
SIDE_R0_ALWAYS = 0.1
SIDE_SAMPLE_EVERY = 5
LOW_MEMORY_GB = 2.0


class State:
    def __init__(self, out_path, stats, max_evals, time_limit_s):
        self.out_path = out_path
        self.stats = stats
        self.max_evals = max_evals
        self.time_limit_s = time_limit_s
        self.stage4 = False
        self.t4 = None
        self.evals = 0
        self.parent_u = None
        self.parent_sf = None
        self.pending = None
        self.unevaluated_peels = 0
        self.current_eval = None
        self.other_count = 0
        self.side_done = 0
        self.other_solves = 0
        self.stop = None
        self.side_errors = []
        self.stage3_evaluations = None

    def write(self, record):
        with open(self.out_path, 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=float, separators=(', ', ': ')) + '\n')


def _solve_wrapper(original, state):
    def solve(self, rhs, *, u0=None, cg_rtol=1e-9):
        ev = state.current_eval
        if ev is None:
            state.other_solves += 1
            return original(self, rhs, u0=u0, cg_rtol=cg_rtol)
        stats = state.stats
        entry = dict(solver=self._solver)
        ev['solves'].append(entry)
        first = len(ev['solves']) == 1 and self._solver == 'cg' and u0 is None
        ff = np.atleast_2d(np.asarray(rhs, dtype=float))[0][self.model.free]
        parent = state.parent_u
        if first and parent is not None and np.any(ff):
            residual = ff - self.kf @ parent[self.model.free]
            entry['r0'] = float(np.linalg.norm(residual) / np.linalg.norm(ff))
        started, before, fallbacks = time.perf_counter(), stats['cg_iterations'], stats['fallbacks']
        try:
            out = original(self, rhs, u0=u0, cg_rtol=cg_rtol)
        except Exception as error:
            entry.update(error=f'{type(error).__name__}: {error}',
                         seconds=time.perf_counter() - started, iterations=int(stats['cg_iterations'] - before))
            raise
        entry.update(iterations=int(stats['cg_iterations'] - before), seconds=time.perf_counter() - started,
                     fallbacks=int(stats['fallbacks'] - fallbacks))
        r0 = entry.get('r0')
        if r0 is not None and ev['cells'] <= SIDE_CELL_LIMIT:
            if r0 < SIDE_R0_ALWAYS:
                reason = 'r0_below_0.1'
            else:
                reason = 'sampled' if state.other_count % SIDE_SAMPLE_EVERY == 0 else None
                state.other_count += 1
            if reason is not None:
                try:
                    started, before, fallbacks = time.perf_counter(), stats['cg_iterations'], stats['fallbacks']
                    warm = original(self, rhs, u0=parent[None, :], cg_rtol=cg_rtol)
                    seconds = time.perf_counter() - started
                    iterations = int(stats['cg_iterations'] - before)
                    scale = max(float(np.abs(out).max()), 1e-30)
                    entry['warm'] = dict(
                        reason=reason, iterations=iterations, seconds=seconds,
                        fallbacks=int(stats['fallbacks'] - fallbacks),
                        ratio=iterations / entry['iterations'] if entry['iterations'] else None,
                        max_relative_difference=float(np.abs(out - warm).max() / scale))
                    state.side_done += 1
                except Exception as error:
                    state.side_errors.append(f'eval {state.evals}: {type(error).__name__}: {error}')
        return out

    return solve


def _peel_wrapper(original, state):
    def peel(mask, order, count, anchor_ids, **kwargs):
        candidate, peeled = original(mask, order, count, anchor_ids, **kwargs)
        if state.stage4:
            caller = sys._getframe(1)
            local = caller.f_locals if caller.f_code.co_name == '_reduce_volume' else {}
            if state.pending is not None and not state.pending.get('used'):
                state.unevaluated_peels += 1
            state.pending = dict(requested=int(count), parent_cells=int(mask.sum()), picked=int(peeled.picked),
                                 removed=int(mask.sum() - candidate.sum()),
                                 multiplier=local.get('multiplier'), gap_ratio=local.get('gap_ratio'))
        return candidate, peeled

    return peel


def _base_record(state, operation):
    state.evals += 1
    pending = state.pending
    pending['used'] = True
    return dict(event='eval', n=state.evals, operation=operation, requested=pending['requested'],
                removed=pending['removed'], parent_cells=pending['parent_cells'], parent_sf=state.parent_sf,
                multiplier=pending['multiplier'], gap_ratio=pending['gap_ratio'],
                t_stage4_s=round(time.perf_counter() - state.t4, 1))


def _evaluate_wrapper(original, state):
    def evaluate(survivor, before, conditions, **kwargs):
        if not state.stage4:
            return original(survivor, before, conditions, **kwargs)
        record = _base_record(state, kwargs['operation'])
        ev = state.current_eval = dict(cells=int(survivor.sum()), solves=[])
        try:
            report, trial = original(survivor, before, conditions, **kwargs)
        finally:
            state.current_eval = None
        fields = kwargs.get('fields')
        safety = report['checks']['safety']
        accepted = report['status'] == 'pass' and fields is not None and 'element_energy' in fields
        solves = ev['solves']
        record.update(
            cells=ev['cells'],
            outcome='accepted' if accepted else 'rejected' if solves else 'no_fem',
            failed=sorted(name for name, check in report['checks'].items() if check.get('status') != 'pass'),
            safety_factor=safety.get('safety_factor'), solves=len(solves))
        if solves:
            first = solves[0]
            record.update(cold_iterations=first.get('iterations'), cold_seconds=first.get('seconds'),
                          solver=first['solver'], r0=first.get('r0'), cold_fallbacks=first.get('fallbacks'))
            if 'warm' in first:
                record['warm'] = first['warm']
            if 'error' in first or len(solves) > 1:
                record['solve_errors'] = [s.get('error') for s in solves]
        state.write(record)
        if accepted:
            state.parent_u = fields['displacement']
            state.parent_sf = safety['safety_factor']
        _progress(record)
        return report, trial

    return evaluate


def _geometry_wrapper(original, state):
    def rejection_trial(history, candidate, parent, operation, conditions, rejection):
        trial = original(history, candidate, parent, operation, conditions, rejection)
        if state.stage4:
            record = _base_record(state, operation)
            record.update(cells=int(candidate.sum()), outcome='geometry_rejected', failed=['geometry'],
                          geometry=dict(components=rejection['components'],
                                        surviving_components=rejection['surviving_components'],
                                        missing=len(rejection['missing'])))
            state.write(record)
            _progress(record)
        return trial

    return rejection_trial


def _reduce_wrapper(original, state):
    def reduce_volume(initial, design_space_mask, conditions, **kwargs):
        state.stage4 = True
        state.t4 = time.perf_counter()
        state.parent_sf = initial.best_feasible.safety_factor
        state.stage3_evaluations = int(initial.evaluations)
        try:
            return original(initial, design_space_mask, conditions, **kwargs)
        finally:
            state.stage4 = False

    return reduce_volume


def _progress(record):
    warm = record.get('warm')
    r0 = record.get('r0')
    print(f"eval {record['n']:4d} {record['operation']:>11} {record['outcome']:>17} 親{record['parent_cells']} "
          f"cold {record.get('cold_iterations')} r0 {'-' if r0 is None else format(r0, '.3g')} "
          f"warm {'-' if not warm else warm['iterations']} t={record['t_stage4_s']:.0f}s "
          f"free={free_gb():.1f}GB", flush=True)


def _peak_working_set_gb():
    import psutil

    info = psutil.Process().memory_info()
    return getattr(info, 'peak_wset', info.rss) / 1e9


def run(args):
    space, conditions, min_width = _setup(args.spacing)
    if conditions.solver != 'cg':
        raise SystemExit(f'conditions.solver が {conditions.solver!r}(cg を想定)')
    spacing = float(space.grid.spacing)
    header = dict(event='header', head=args.head, spacing_requested=args.spacing, actual_spacing_mm=spacing,
                  grid_shape=list(space.grid.shape), design_cells=int(space.mask.sum()),
                  min_width_cells=int(min_width['cells']), target_safety_factor=float(conditions.target_safety_factor),
                  solver=conditions.solver, warm_start=False, max_evals=args.max_evals,
                  time_limit_min=args.time_limit_min, side_cell_limit=SIDE_CELL_LIMIT, side_r0_always=SIDE_R0_ALWAYS,
                  side_sample_every=SIDE_SAMPLE_EVERY, free_gb_at_start=free_gb(), finished=False, stop_reason=None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(header, ensure_ascii=False, default=float) + '\n', encoding='utf-8', newline='\n')

    with tempfile.TemporaryDirectory(prefix='topopt-trace-') as trace_root, \
            patch.object(trace, 'DEFAULT_ROOT', Path(trace_root)), fem.collect_solve_stats() as stats:
        state = State(args.out, stats, args.max_evals, args.time_limit_min * 60.0)

        def is_cancelled():
            if not state.stage4:
                return False
            if state.evals >= state.max_evals:
                state.stop = 'evaluation_cap'
            elif time.perf_counter() - state.t4 >= state.time_limit_s:
                state.stop = 'time_limit'
            elif free_gb() < LOW_MEMORY_GB:
                state.stop = 'low_memory'
            return state.stop is not None

        with patch.object(fem.PreparedSystem, 'solve', _solve_wrapper(fem.PreparedSystem.solve, state)), \
                patch.object(binary_search, 'peel', _peel_wrapper(binary_search.peel, state)), \
                patch.object(binary_search, '_evaluate_survivor',
                             _evaluate_wrapper(binary_search._evaluate_survivor, state)), \
                patch.object(binary_search, '_geometry_rejection_trial',
                             _geometry_wrapper(binary_search._geometry_rejection_trial, state)), \
                patch.object(binary_search, '_reduce_volume',
                             _reduce_wrapper(binary_search._reduce_volume, state)):
            started = time.perf_counter()
            result = binary_search.find_best_feasible(space.mask, conditions, min_width_cells=min_width['cells'],
                                                      warm_start=False, is_cancelled=is_cancelled)
            total_seconds = time.perf_counter() - started

    reduction = (result.diagnostics or {}).get('reduction') or {}
    steps = reduction.get('steps') or []
    records = read_records(args.out)[1]
    mismatches = [i for i, (rec, step) in enumerate(zip(records, steps))
                  if (rec['requested'], rec['removed'], rec['outcome'] == 'accepted') !=
                  (step['requested_cells'], step['removed_cells'], step['outcome'] == 'accepted')]
    header.update(
        finished=True, stop_reason=reduction.get('stop_reason'), termination=result.termination,
        runner_stop=state.stop, search_status=result.status, evaluations_total=int(result.evaluations),
        stage3_evaluations=state.stage3_evaluations, stage4_evaluations=state.evals,
        stage4_steps_in_diagnostics=len(steps), record_step_mismatches=mismatches,
        record_count_matches=len(records) == len(steps) == state.evals,
        repeated_candidates=reduction.get('repeated_candidates'), unevaluated_peels=state.unevaluated_peels,
        total_seconds=total_seconds,
        stage3_seconds=(state.t4 - started) if state.t4 is not None else None,
        stage4_seconds=(time.perf_counter() - state.t4) if state.t4 is not None else None,
        side_measurements=state.side_done, side_errors=state.side_errors, other_solves=state.other_solves,
        peak_working_set_gb=_peak_working_set_gb(), free_gb_at_end=free_gb(),
        final_safety_factor=(float(result.best_feasible.safety_factor) if result.best_feasible else None),
        final_cells=(int(result.best_feasible.mask.sum()) if result.best_feasible else None),
        final_mask_hash=(result.best_feasible.mask_hash if result.best_feasible else None))
    rewrite_header(args.out, header)
    print(json.dumps({k: v for k, v in header.items() if k != 'event'}, ensure_ascii=False, default=float), flush=True)
    return 0


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


R0_COLUMNS = (('< 1e-3', 1e-3), ('< 1e-2', 1e-2), ('< 0.1', 0.1), ('< 1', 1.0))
RATIO_BINS = ((0.0, 1e-4, '0 以上 1e-4 未満'), (1e-4, 1e-3, '1e-4 以上 1e-3 未満'), (1e-3, 1e-2, '1e-3 以上 1e-2 未満'),
              (1e-2, 0.1, '1e-2 以上 0.1 未満'), (0.1, 1.0, '0.1 以上 1 未満'), (1.0, float('inf'), '1 以上'))
REMOVED_BINS = ((1, 1, '1'), (2, 5, '2〜5'), (6, 20, '6〜20'), (21, 100, '21〜100'), (101, float('inf'), '101 以上'))
THETAS = (1e-3, 1e-2, 0.03, 0.1, 0.3, 1.0, float('inf'))


def ratio_bin(r0):
    return next(i for i, (lo, hi, _) in enumerate(RATIO_BINS) if lo <= r0 < hi)


def _median(values):
    return float(np.median(values)) if values else None


def _fmt(value, spec='.3f'):
    return '–' if value is None else format(value, spec)


def distribution_row(label, records):
    """r0 が分かる評価(FEM を解き、親の変位がある評価)だけを数える。"""
    values = [r['r0'] for r in records if r.get('r0') is not None]
    if not values:
        return f'| {label} | 0 | – | – | – | – | – |'
    cells = [f'{sum(v < edge for v in values) / len(values) * 100:.1f}% ({sum(v < edge for v in values)})'
             for _, edge in R0_COLUMNS]
    high = sum(v >= 1.0 for v in values)
    return f'| {label} | {len(values)} | ' + ' | '.join(cells) + f' | {high / len(values) * 100:.1f}% ({high}) |'


def distribution_table(title_column, groups):
    lines = [f'| {title_column} | r0 が分かる評価 | ' + ' | '.join(f'r0 {name}' for name, _ in R0_COLUMNS) + ' | r0 ≥ 1 |',
             '|---|---|' + '---|' * (len(R0_COLUMNS) + 1)]
    lines += [distribution_row(label, records) for label, records in groups]
    return '\n'.join(lines)


def ratio_by_bin(points):
    """points = [(r0, 比)]。kcurve の表4と同じ区間ごとの点数・中央値・最小・最大。"""
    rows, medians = [], []
    for index, (_, _, label) in enumerate(RATIO_BINS):
        ratios = [ratio for r0, ratio in points if ratio_bin(r0) == index]
        medians.append(_median(ratios))
        rows.append(f'| {label} | {len(ratios)} | {_fmt(medians[-1])} | {_fmt(min(ratios) if ratios else None)} | '
                    f'{_fmt(max(ratios) if ratios else None)} |')
    return rows, medians


def reduction_rows(records):
    """段階4の cold の CG 反復数の合計に対する、「r0 < θ のときだけ warm」の削減。

    副測定した評価は実測の warm 反復数、していない評価は同じ r0 区間の実測比の中央値で推定する
    (その区間に実測が無ければ推定できない評価に数える)。
    """
    measured = [(r['r0'], r['warm']['ratio']) for r in records if r.get('warm') and r['warm'].get('ratio') is not None]
    medians = ratio_by_bin(measured)[1]
    cold_total = sum(r.get('cold_iterations') or 0 for r in records)
    seconds_total = sum(r.get('cold_seconds') or 0.0 for r in records)
    rows = []
    for theta in THETAS:
        saved_measured = saved_estimated = seconds_saved = unestimable = 0.0
        n_measured = n_estimated = n_unestimable = 0
        for r in records:
            r0, iterations = r.get('r0'), r.get('cold_iterations') or 0
            if r0 is None or r0 >= theta or not iterations:
                continue
            per_iteration = (r.get('cold_seconds') or 0.0) / iterations
            if r.get('warm') and r['warm'].get('iterations') is not None:
                saved = iterations - r['warm']['iterations']
                saved_measured += saved
                n_measured += 1
            elif medians[ratio_bin(r0)] is not None:
                saved = iterations * (1.0 - medians[ratio_bin(r0)])
                saved_estimated += saved
                n_estimated += 1
            else:
                unestimable += iterations
                n_unestimable += 1
                continue
            seconds_saved += saved * per_iteration
        name = '常に warm' if theta == float('inf') else f'{theta:g}'
        rows.append(f'| {name} | {n_measured} | {saved_measured / cold_total * 100:.1f}% | {n_estimated} | '
                    f'{saved_estimated / cold_total * 100:.1f}% | {(saved_measured + saved_estimated) / cold_total * 100:.1f}% | '
                    f'{n_unestimable} ({unestimable / cold_total * 100:.1f}%) | {seconds_saved / seconds_total * 100:.1f}% |')
    return cold_total, rows


def analyze(paths, kcurve_dir, phase_n):
    print('r0 の分布・副測定・削減率(`--analyze`。判断はしない)\n')
    runs = []
    for path in paths:
        header, records = read_records(path)
        runs.append((path.stem, records))
        counts = {}
        for r in records:
            counts[r['outcome']] = counts.get(r['outcome'], 0) + 1
        cold_total = sum(r.get('cold_iterations') or 0 for r in records)
        print(f'## {path.name}(要求 {header["spacing_requested"]}mm・実 {header["actual_spacing_mm"]:.3f}mm・'
              f'格子 {header["grid_shape"]}・設計空間 {header["design_cells"]:,} セル)\n')
        print(f'- 段階4の評価 {len(records)}({counts})・段階3の評価 {header.get("stage3_evaluations")}・停止 '
              f'runner={header.get("runner_stop")} / stop_reason={header.get("stop_reason")} / termination={header.get("termination")}・'
              f'段階4 {(header.get("stage4_seconds") or 0) / 60:.1f}分(段階3 {(header.get("stage3_seconds") or 0) / 60:.1f}分)・'
              f'cold の CG 反復数の合計 {cold_total:,}・副測定 {sum(1 for r in records if r.get("warm"))} 件・'
              f'記録と診断の照合 {header.get("record_count_matches")}(不一致 {header.get("record_step_mismatches")})・'
              f'採用形状 {header.get("final_cells")} セル / 安全率 {header.get("final_safety_factor")}\n')
        values = [r['r0'] for r in records if r.get('r0') is not None]
        fallbacks = (sum(1 for r in records if r.get('cold_fallbacks')),
                     sum(1 for r in records if (r.get('warm') or {}).get('fallbacks')))
        spread = (f'r0 の最小 {min(values):.3g}・中央値 {np.median(values):.3g}・最大 {max(values):.3g}'
                  if values else 'r0 が分かる評価が無い')
        print(f'- {spread}・CG が収束せず直接法に落ちた評価: cold {fallbacks[0]} 件・warm {fallbacks[1]} 件'
              f'(比・削減率の計算はこれを区別しない)\n')
        target = header['target_safety_factor']
        upper, lower = phase_n
        phases = [('序盤', [r for r in records if r['parent_sf'] / target >= upper]),
                  ('中盤', [r for r in records if lower <= r['parent_sf'] / target < upper]),
                  ('終盤', [r for r in records if r['parent_sf'] / target < lower])]
        print(f'**局面ごとの評価数と cold の CG 反復数**(序盤 = 親の安全率が目標の {upper:g} 倍以上・中盤 = {lower:g} 倍以上 {upper:g} 倍未満・'
              f'終盤 = {lower:g} 倍未満)\n')
        print('| 局面 | 評価数 | 採用 | 不合格 | 幾何却下・FEM なし | cold 反復の合計 | 割合 | cold の秒の合計 |\n'
              '|---|---|---|---|---|---|---|---|')
        for name, group in phases:
            outcome = lambda key: sum(1 for r in group if r['outcome'] == key)
            iterations = sum(r.get('cold_iterations') or 0 for r in group)
            print(f'| {name} | {len(group)} | {outcome("accepted")} | {outcome("rejected")} | '
                  f'{outcome("geometry_rejected") + outcome("no_fem")} | {iterations:,} | {iterations / cold_total * 100:.1f}% | '
                  f'{sum(r.get("cold_seconds") or 0.0 for r in group):.0f} |')
        print('\n**r0 の分布**\n')
        groups = [('全体', records)] + phases
        groups += [(f'結果 {name}', [r for r in records if r['outcome'] == key])
                   for name, key in (('採用', 'accepted'), ('不合格', 'rejected'))]
        print(distribution_table('区分', groups) + '\n')
        print(distribution_table('抜けたセル数', [(label, [r for r in records if lo <= r['removed'] <= hi])
                                              for lo, hi, label in REMOVED_BINS]) + '\n')
        print(distribution_table('局面 × 結果', [(f'{phase} {name}', [r for r in group if r['outcome'] == key])
                                             for phase, group in phases
                                             for name, key in (('採用', 'accepted'), ('不合格', 'rejected'))]) + '\n')
        print(distribution_table('候補のセル数', [
            ('50,000 以下', [r for r in records if r.get('cells', 0) <= SIDE_CELL_LIMIT]),
            ('50,001 以上', [r for r in records if r.get('cells', 0) > SIDE_CELL_LIMIT])]) + '\n')
        measured = [(r['r0'], r['warm']['ratio']) for r in records if r.get('warm') and r['warm'].get('ratio') is not None]
        rows, _ = ratio_by_bin(measured)
        print('**副測定の比(warm / cold の反復数)を r0 の区間ごとに**\n')
        print('| r0 | 点数 | 比 中央値 | 最小 | 最大 |\n|---|---|---|---|---|\n' + '\n'.join(rows) + '\n')
        differences = [r['warm']['max_relative_difference'] for r in records if r.get('warm')]
        print(f'副測定の cold と warm の最大相対差: 最大 {max(differences) if differences else None}\n')
        cold_total, rows = reduction_rows(records)
        print(f'**「r0 < θ のときだけ warm」の削減**(段階4の cold の CG 反復数の合計 {cold_total:,} に対する割合)\n')
        print('| θ | 実測分の評価数 | 実測分の削減 | 推定分の評価数 | 推定分の削減 | 合計の削減 | '
              '推定できない評価(cold 反復の割合) | 秒の削減(参考) |\n|---|---|---|---|---|---|---|---|\n'
              + '\n'.join(rows) + '\n')
    if kcurve_dir is not None:
        kcurve = {}
        for name in ('2.4mm', '2.0mm'):
            record = json.loads((kcurve_dir / f'{name}.json').read_text(encoding='utf-8'))
            kcurve[name] = [p for p in record['points'] if 'initial_relative_residual' in p]
        groups = [(f'kcurve {name}', [dict(r0=p['initial_relative_residual']) for p in points])
                  for name, points in kcurve.items()]
        groups.append(('kcurve 合計', [r for _, rs in groups for r in rs]))
        groups += [(f'実探索 {name}', records) for name, records in runs]
        groups.append(('実探索 合計', [r for _, records in runs for r in records]))
        print('## 合成の親(kcurve)と実探索の軌道での r0 の分布\n')
        print(distribution_table('測定', groups) + '\n')
        pooled = [(r['r0'], r['warm']['ratio']) for _, records in runs for r in records
                  if r.get('warm') and r['warm'].get('ratio') is not None]
        synthetic = [(p['initial_relative_residual'], p['iteration_ratio']) for points in kcurve.values()
                     for p in points if p.get('iteration_ratio') is not None]
        print('## 比(warm / cold の反復数)を r0 の区間ごとに: 実探索の副測定(2 格子まとめ)と kcurve(表4)\n')
        print('| r0 | 実探索 点数 | 中央値 | 最小 | 最大 | kcurve 点数 | 中央値 | 最小 | 最大 |\n'
              '|---|---|---|---|---|---|---|---|---|')
        for (_, _, label), row_a, row_b in zip(RATIO_BINS, ratio_by_bin(pooled)[0], ratio_by_bin(synthetic)[0]):
            print(f'| {label} |' + row_a.split('|', 2)[2] + row_b.split('|', 2)[2])
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--spacing', type=float)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--head', default='unknown', help='測定に使ったコードの HEAD のハッシュ(記録用。実行したソースは検証しない)')
    parser.add_argument('--max-evals', type=int, default=150)
    parser.add_argument('--time-limit-min', type=float, default=150.0)
    parser.add_argument('--min-free-gb', type=float, default=14.0)
    parser.add_argument('--analyze', type=Path, nargs='+', metavar='JSONL')
    parser.add_argument('--kcurve', type=Path, default=None,
                        help='--analyze で並べる kcurve の記録の置き場(2.4mm.json・2.0mm.json)')
    parser.add_argument('--phase-n', type=float, nargs=2, default=(1.5625, 1.01), metavar=('UPPER', 'LOWER'),
                        help='--analyze の局面の境界(親の安全率 / 目標)。既定は、歩幅 1-sqrt(目標/安全率) が上限 0.2 を'
                             '離れる 1.5625 と、親セル数の 0.5%% を下回る 1.01')
    args = parser.parse_args()
    if args.analyze:
        return analyze(args.analyze, args.kcurve, args.phase_n)
    if args.spacing is None or args.out is None:
        parser.error('--spacing と --out が要る(または --analyze)')
    if args.max_evals > 900:
        parser.error('--max-evals は 900 以下(段階3を含む探索の評価予算は find_best_feasible の既定 1000 のままで、それを超えると先に止まる)')
    if args.out.exists():
        raise SystemExit(f'{args.out} は既にある(探索は途中から再開できない。別の名前にするか消す)')
    free = free_gb()
    if free < args.min_free_gb:
        print(f'空きメモリ {free:.1f}GB が下限 {args.min_free_gb}GB 未満。測らずに終わる', flush=True)
        return 3
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())

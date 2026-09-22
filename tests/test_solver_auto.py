"""`solver='auto'`(自由な自由度と nnz で direct / cg を選ぶ)の選び方と、選んだ解法が報告と再試行に効くこと。"""
import numpy as np
import pytest

from duocad.topopt import acceptance, binary_search as bs, fem, stress
from test_binary_search import bar_conditions


def test_choose_solver_applies_both_thresholds_inclusively():
    """落ちたら、切り替えの条件が docs/TOPOPT_SPEED.md 表E′の根拠(≤ 5 万自由度 かつ nnz/自由度 ≤ 60)と違う向きに動いている。"""
    n, r = fem.AUTO_DIRECT_MAX_DOFS, fem.AUTO_DIRECT_MAX_NNZ_PER_DOF
    assert fem.choose_solver('auto', n, int(r * n)) == 'direct'
    assert fem.choose_solver('auto', n + 1, int(r * (n + 1))) == 'cg'
    assert fem.choose_solver('auto', n, int(r * n) + 1) == 'cg'
    assert fem.choose_solver('auto', 0, 0) == 'cg'
    assert fem.choose_solver('cg', 10, 10) == 'cg' and fem.choose_solver('direct', 10 ** 7, 10 ** 9) == 'direct'


def test_auto_resolves_at_prepare_and_the_answers_agree_with_both_solvers(monkeypatch):
    """落ちたら、`auto` が選んだ解法の解が cg / direct と一致していない(合否が解法で変わる)。"""
    model = fem.Model(8, 3, 3)
    rho = np.ones((8, 3, 3))
    prepared = model.prepare(rho, 3.0, solver='auto')
    assert prepared.policy == 'auto'
    assert prepared.solver == fem.choose_solver('auto', prepared.kf.shape[0], prepared.kf.nnz)
    monkeypatch.setattr(fem, 'AUTO_DIRECT_MAX_NNZ_PER_DOF', float('inf'))
    assert model.prepare(rho, 3.0, solver='auto').solver == 'direct'
    monkeypatch.setattr(fem, 'AUTO_DIRECT_MAX_DOFS', 1)
    assert model.prepare(rho, 3.0, solver='auto').solver == 'cg'
    monkeypatch.undo()
    u_direct = model.solve(rho, 3.0, solver='direct')
    scale = float(np.abs(u_direct).max())
    for solver in ('auto', 'cg'):
        assert np.abs(model.solve(rho, 3.0, solver=solver) - u_direct).max() / scale < 1e-8
    with pytest.raises(ValueError, match='未知の解法'):
        model.prepare(rho, 3.0, solver='mg')


def test_the_solver_actually_used_is_reported_next_to_the_policy(monkeypatch):
    """落ちたら、6条件の報告の `safety.solver` が方針(`auto`)のままで、どちらで解いたか記録から分からない。"""
    model = fem.Model(8, 3, 3)
    rho = np.ones((8, 3, 3))
    info = {}
    stress.solve_displacement(model, rho, solver='auto', info=info)
    assert info['solver'] in ('cg', 'direct')
    monkeypatch.setattr(fem, 'AUTO_DIRECT_MAX_DOFS', 1)
    info = {}
    stress.solve_displacement(model, rho, solver='auto', info=info)
    assert info['solver'] == 'cg'
    monkeypatch.undo()

    conditions = bar_conditions(solver='auto')
    full = np.ones((4, 2, 2), bool)
    report = acceptance.inspect(full, full, conditions.grid, **conditions.inspect_kwargs())
    safety = report['checks']['safety']
    assert safety['solver_policy'] == 'auto' and safety['solver'] in ('cg', 'direct')


def test_a_failed_auto_solve_is_retried_with_the_solver_it_did_not_use(monkeypatch):
    """落ちたら、`auto` が選んだ解法が失敗したあと同じ解法で再試行している(§4.5 の再試行が空振りになる)。"""
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape, solver='auto')

    def fails_after_choosing_direct(model, rho, penal=3.0, *, solver='cg', cg_rtol=1e-9, info=None):
        if info is not None:
            info['solver'] = 'direct'
        raise fem.SolveResidualError('injected failure for the test')

    monkeypatch.setattr(stress, 'solve_displacement', fails_after_choosing_direct)
    result = bs.find_initial_feasible(np.ones(shape, bool), conditions, max_evaluations=5)
    assert result.status == 'no_feasible_found'
    first = result.history[0]
    retried = [t for t in result.history if t.operation.startswith('solver_retry:')]
    assert retried and retried[0].operation == 'solver_retry:cg' and retried[0].mask_hash == first.mask_hash


def test_the_solver_chosen_before_a_failing_solve_survives_in_the_report_and_steers_the_retry(monkeypatch):
    """落ちたら、解く前に記録した解法が失敗の報告に残っていない、または再試行が「使った解法の反対側」ではなく
    方針 `auto` から見た固定の側(direct)へ行っている。"""
    shape = (4, 2, 2)
    conditions = bar_conditions(shape=shape, solver='auto')
    full = np.ones(shape, bool)

    def solve_fails(self, rhs, **kwargs):
        raise fem.SolveResidualError('injected failure for the test')

    monkeypatch.setattr(fem.PreparedSystem, 'solve', solve_fails)
    safety = acceptance.inspect(full, full, conditions.grid, **conditions.inspect_kwargs())['checks']['safety']
    assert safety['status'] == 'unknown' and safety['solver_policy'] == 'auto' and safety['solver'] in ('cg', 'direct')
    result = bs.find_initial_feasible(full, conditions, max_evaluations=5)
    retried = [t for t in result.history if t.operation.startswith('solver_retry:')]
    other = 'cg' if safety['solver'] == 'direct' else 'direct'
    assert retried and retried[0].operation == f'solver_retry:{other}'

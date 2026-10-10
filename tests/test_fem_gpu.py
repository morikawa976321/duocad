"""CG を GPU で解く経路(`fem.FEM_DEVICE` / `gpu_cg`)。前半は GPU が無くても走り、後半は CuPy と GPU があるときだけ走る。"""
import warnings

import numpy as np
import pytest

from duocad.topopt import binary_search as bs, fem, gpu_cg, trace
from topopt_support import bar_conditions


@pytest.fixture(autouse=True)
def _fresh_gpu_state():
    gpu_cg.reset()
    yield
    gpu_cg.reset()


def _solve(model, device, **kw):
    rho = np.ones((model.nelx, model.nely, model.nelz))
    with fem.collect_solve_stats() as stats:
        u = model.prepare(rho, 3.0, solver='cg', device=device, **kw).solve(model.f[None, :])[0]
    return u, stats


def _linear_solve_records(rec):
    return [trace.load_snapshot(p) for p in sorted(rec.path.glob('*-linear-solve.json'))]


def test_the_device_setting_defaults_to_cpu_and_ignores_unknown_values(monkeypatch):
    """落ちたら、環境変数が無い・読めない機械で GPU を使いにいく(既定は CPU という方針に反する)。"""
    monkeypatch.delenv(fem.FEM_DEVICE_ENV, raising=False)
    assert fem._default_device() == 'cpu'
    monkeypatch.setenv(fem.FEM_DEVICE_ENV, ' GPU ')
    assert fem._default_device() == 'gpu'
    monkeypatch.setenv(fem.FEM_DEVICE_ENV, 'cuda')
    with pytest.warns(RuntimeWarning, match='cpu で解く'):
        assert fem._default_device() == 'cpu'
    with pytest.raises(ValueError, match='未知の装置'):
        fem.Model(4, 2, 2).prepare(np.ones((4, 2, 2)), 3.0, device='cuda')


@pytest.mark.parametrize('sticky', [False, True])
def test_when_the_gpu_cannot_solve_the_cpu_solves_from_scratch_and_the_reason_is_recorded(monkeypatch, sticky):
    """落ちたら、GPU で解けなかったときに CPU の答えと違うものを返すか、CPU へ戻したことが記録に残らない。"""
    model = fem.Model(10, 4, 4)
    expected, _ = _solve(model, 'cpu')

    def unavailable(*_a, **_k):
        raise gpu_cg.Unavailable('vram: テスト', sticky=sticky)

    monkeypatch.setattr(gpu_cg, 'solve', unavailable)
    with trace.recording() as rec, pytest.warns(RuntimeWarning, match='CPU で解き直す: vram: テスト'):
        u, stats = _solve(model, 'gpu')
    assert np.array_equal(u, expected)
    assert stats['gpu_solves'] == 0 and stats['gpu_fallbacks'] == 1 and stats['cg_iterations'] > 0
    (record,) = _linear_solve_records(rec)
    assert record['device_requested'] == 'gpu' and record['device'] == 'cpu'
    assert record['gpu_fallback'] == 'vram: テスト'


def test_a_converged_gpu_solution_that_fails_the_residual_check_is_solved_again_on_the_cpu(monkeypatch):
    """落ちたら、GPU が返した壊れた解(収束と言いながら残差が大きい)をそのまま使うか、残差検算の例外で探索を止める。"""
    model = fem.Model(10, 4, 4)
    expected, _ = _solve(model, 'cpu')
    monkeypatch.setattr(gpu_cg, 'solve', lambda kf, ff, **_k: (np.ones_like(ff), 3, 0))
    with pytest.warns(RuntimeWarning, match='residual'):
        u, stats = _solve(model, 'gpu')
    assert np.array_equal(u, expected) and stats['gpu_fallbacks'] == 1


def test_a_startup_check_mismatch_disables_the_gpu_for_the_rest_of_the_process(monkeypatch):
    """落ちたら、起動時の自己検査(小さい系の CG を CPU と比べる)に落ちても GPU で解き続ける。"""
    calls = []
    monkeypatch.setattr(gpu_cg, '_cupy', lambda: (calls.append('cupy'), (None, {}))[1])
    monkeypatch.setattr(gpu_cg, 'openblas_threads', lambda: 1)
    monkeypatch.setattr(gpu_cg, '_cg', lambda kf, ff, threads, rtol: (np.zeros_like(ff), 1, 0))
    model = fem.Model(10, 4, 4)
    expected, _ = _solve(model, 'cpu')
    with pytest.warns(RuntimeWarning, match='startup-check'):
        u, stats = _solve(model, 'gpu')
    assert np.array_equal(u, expected) and stats['gpu_fallbacks'] == 1
    calls.clear()
    with pytest.warns(RuntimeWarning, match='startup-check'):
        _solve(model, 'gpu')
    assert calls == []  # 2回目は CuPy に触らず即座に CPU へ


def test_a_missing_cupy_falls_back_once_and_is_remembered(monkeypatch):
    """落ちたら、CuPy の無い機械(ThinkPad 等)で gpu を設定すると solve のたびに読み込みを試し直すか、落ちる。"""
    import builtins

    real_import = builtins.__import__

    def no_cupy(name, *args, **kwargs):
        if name == 'cupy' or name.startswith('cupy.'):
            raise ImportError('テスト: cupy は無い')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(gpu_cg, '_kernels', None)
    monkeypatch.setattr(builtins, '__import__', no_cupy)
    model = fem.Model(10, 4, 4)
    with pytest.warns(RuntimeWarning, match='cupy: ImportError'):
        _u, stats = _solve(model, 'gpu')
    assert stats['gpu_fallbacks'] == 1 and gpu_cg._disabled.startswith('cupy: ImportError')


def _gpu_available() -> bool:
    try:
        gpu_cg._cupy()
    except gpu_cg.Unavailable:
        return False
    return True


needs_gpu = pytest.mark.skipif(not _gpu_available(), reason='CuPy と CUDA の GPU が無い')


@needs_gpu
@pytest.mark.parametrize('threads', [1, 3])
def test_the_gpu_solution_is_bit_identical_to_the_cpu_one(threads):
    """落ちたら、GPU の CG の演算順序か丸めが scipy の cg とずれた(numpy / scipy / OpenBLAS の更新を疑う)。
    自由度は OpenBLAS が内積をスレッドに割る長さ(1 万)を超える。"""
    model = fem.Model(30, 12, 12)
    assert model.free.size > gpu_cg.SINGLE_THREAD_MAX
    expected, cpu = _solve(model, 'cpu', threads=threads)
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        u, gpu = _solve(model, 'gpu', threads=threads)
    assert gpu['gpu_solves'] == 1 and gpu['gpu_fallbacks'] == 0
    assert np.array_equal(u, expected)
    assert gpu['cg_iterations_each'] == cpu['cg_iterations_each']


@needs_gpu
def test_a_dot_order_that_does_not_match_numpy_falls_back_without_disabling_the_gpu(monkeypatch):
    """落ちたら、内積の自己検査が足し順のずれ(ここではスレッド数の取り違え)を見逃している。"""
    model = fem.Model(30, 12, 12)
    expected, _ = _solve(model, 'cpu')
    real = gpu_cg.openblas_threads()
    monkeypatch.setattr(gpu_cg, 'openblas_threads', lambda: real + 1)
    with pytest.warns(RuntimeWarning, match='dot-mismatch'):
        u, stats = _solve(model, 'gpu')
    assert np.array_equal(u, expected) and stats['gpu_fallbacks'] == 1
    assert gpu_cg._disabled is None


@needs_gpu
def test_a_search_takes_the_same_path_on_the_gpu_as_on_the_cpu(monkeypatch):
    """落ちたら、GPU で解くと探索の試行(形・合否・安全率)が CPU と変わる —— 過去の結果と比べられなくなる。"""
    conditions = bar_conditions(shape=(12, 4, 4), solver='cg')
    design = np.ones((12, 4, 4), bool)

    def run(device):
        monkeypatch.setattr(fem, 'FEM_DEVICE', device)
        with fem.collect_solve_stats() as stats:
            result = bs.find_best_feasible(design, conditions, max_evaluations=12)
        return [(t.mask_hash, t.outcome, t.safety_factor) for t in result.history], stats

    cpu_history, _ = run('cpu')
    gpu_history, gpu = run('gpu')
    assert gpu['gpu_solves'] > 0 and gpu['gpu_fallbacks'] == 0
    assert gpu_history == cpu_history

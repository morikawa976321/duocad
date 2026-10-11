"""CG を GPU で解く経路(`fem.FEM_DEVICE` / `gpu_cg`)。前半は GPU が無くても走り、後半は CuPy と GPU があるときだけ走る。"""
import asyncio
import json
import warnings

import numpy as np
import pytest

from duocad import local_settings
from duocad.server import Server, Session
from duocad.topopt import binary_search as bs, fem, gpu_cg, trace
from session_support import FakeConnection
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


def test_the_device_setting_defaults_to_cpu_and_survives_a_restart(tmp_path):
    """落ちたら、設定ファイルが無い・壊れた・知らない値の clone で GPU を使いにいくか(既定は CPU という方針に反する)、
    画面で選んだ装置がサーバを立て直すと消える。"""
    assert fem.FEM_DEVICE == 'cpu'
    assert local_settings.fem_device() == 'cpu'
    local_settings.PATH.write_text('{broken', encoding='utf-8')
    assert local_settings.fem_device() == 'cpu'
    local_settings.PATH.write_text('{"fem_device": "cuda"}', encoding='utf-8')
    assert local_settings.fem_device() == 'cpu'
    local_settings.PATH.write_text('{"other": 1}', encoding='utf-8')
    local_settings.update(fem_device='gpu')
    assert local_settings.read() == {'other': 1, 'fem_device': 'gpu'}
    Server(Session(None, 0.2, models_dir=tmp_path / 'models', exports_dir=tmp_path / 'export'))
    assert fem.FEM_DEVICE == 'gpu'
    with pytest.raises(ValueError, match='未知の装置'):
        fem.Model(4, 2, 2).prepare(np.ones((4, 2, 2)), 3.0, device='cuda')


def test_the_settings_dialog_switches_the_device_for_every_screen(tmp_path, monkeypatch):
    """落ちたら、設定ダイアログの切り替えが次の計算に効かない・ファイルに残らない・他の画面へ配られないか、
    cpu を選んでいる機械で CuPy を読んで GPU のメモリを取る。"""
    described = []
    monkeypatch.setattr(gpu_cg, 'describe', lambda: described.append(1) or {'name': 'テストGPU', 'problem': None})
    monkeypatch.setattr(gpu_cg, 'cupy_installed', lambda: True)
    gpu_cg._disabled = 'cupy: 前に失敗した'
    server = Server(Session(None, 0.2, models_dir=tmp_path / 'models', exports_dir=tmp_path / 'export'))

    async def scenario():
        a, b = FakeConnection(), FakeConnection()
        server.clients.update({a, b})
        await server._dispatch(a, json.dumps({'cmd': 'fem_get_status'}))
        first = list(a.payloads)
        await server._dispatch(a, json.dumps({'cmd': 'fem_set_device', 'device': 'gpu'}))
        await server._dispatch(a, json.dumps({'cmd': 'fem_set_device', 'device': 'cuda'}))
        return first, a.payloads[len(first):], b.payloads

    first, sent, others = asyncio.run(scenario())
    assert first == [{'type': 'fem_status', 'device': 'cpu', 'cupy_installed': True,
                      'gpu_name': None, 'gpu_problem': None}]
    assert sent[0] == {'type': 'fem_status', 'device': 'gpu', 'cupy_installed': True,
                       'gpu_name': 'テストGPU', 'gpu_problem': None}
    assert sent[1]['device'] == 'gpu' and 'cuda' in sent[1]['error']
    assert others == sent
    assert len(described) == 2  # cpu のときは CuPy に触らない
    assert fem.FEM_DEVICE == 'gpu' and local_settings.fem_device() == 'gpu'
    assert gpu_cg._disabled is None  # gpu を選び直したら、前に使わないと決めた理由を忘れて試し直す


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


def test_a_gpu_cg_that_does_not_converge_is_recorded_as_a_fallback_not_as_a_gpu_solve(monkeypatch):
    """落ちたら、GPU の CG が収束せず CPU の直接解法が解いたのに、GPU で解けたと数えている(秒の集計が混ざる)。"""
    model = fem.Model(10, 4, 4)
    expected = model.solve(np.ones((10, 4, 4)), 3.0, solver='direct')
    monkeypatch.setattr(gpu_cg, 'solve', lambda kf, ff, **_k: (np.zeros_like(ff), 7, 7))
    with trace.recording() as rec, pytest.warns(RuntimeWarning, match='直接解法へフォールバック'):
        u, stats = _solve(model, 'gpu')
    assert np.allclose(u, expected, rtol=1e-12, atol=0)
    assert stats['gpu_solves'] == 0 and stats['gpu_fallbacks'] == 1 and stats['fallbacks'] == 1
    (record,) = _linear_solve_records(rec)
    assert record['device'] == 'cpu' and record['gpu_fallback'].startswith('not-converged')


@pytest.mark.parametrize('solved_on_gpu', [False, True])
def test_an_error_while_releasing_vram_falls_back_to_the_cpu(monkeypatch, solved_on_gpu):
    """落ちたら、VRAM を返す処理で表に出た CUDA のエラーが CPU への戻りをすり抜け、探索の評価ごと落ちるか、
    GPU で解き終えた後のエラーなのに GPU で解けたと数えている。"""

    class BrokenPool:
        def free_all_blocks(self):
            raise RuntimeError('テスト: 非同期の CUDA エラー')

    class FakeCupy:
        def get_default_memory_pool(self):
            return BrokenPool()

    monkeypatch.setattr(gpu_cg, '_kernels', {'cp': FakeCupy()})
    monkeypatch.setattr(gpu_cg, '_cupy', lambda: (None, {}))
    monkeypatch.setattr(gpu_cg, 'openblas_threads', lambda: 1)
    monkeypatch.setattr(gpu_cg, '_check_startup', lambda *_a: None)
    monkeypatch.setattr(gpu_cg, '_dot_matches', lambda n, threads: solved_on_gpu)
    monkeypatch.setattr(gpu_cg, '_cg', lambda kf, ff, threads, rtol: (np.zeros_like(ff), 1, 0))
    model = fem.Model(10, 4, 4)
    expected, _ = _solve(model, 'cpu')
    with pytest.warns(RuntimeWarning, match='VRAM の解放' if solved_on_gpu else 'dot-mismatch'):
        u, stats = _solve(model, 'gpu')
    assert np.array_equal(u, expected) and stats['gpu_fallbacks'] == 1
    assert gpu_cg._disabled.startswith('gpu-error: VRAM の解放')


@needs_gpu
def test_vram_is_returned_after_a_failure_inside_the_gpu_cg(monkeypatch):
    """落ちたら、GPU の CG が途中で失敗したとき、例外が握った GPU の配列のせいで VRAM が返らず CPU で解く間も抱えている。"""
    import cupy as cp

    def fails_after_allocating(kf, ff, threads, rtol):
        held = cp.ones(32 * 2**20 // 8)  # noqa: F841 —— 例外のフレームに残る 32MB
        raise cp.cuda.memory.OutOfMemoryError(2**40, 0)

    monkeypatch.setattr(gpu_cg, '_cg', fails_after_allocating)
    monkeypatch.setattr(gpu_cg, '_startup_checked', True)
    model = fem.Model(10, 4, 4)
    with pytest.warns(RuntimeWarning, match='vram'):
        _u, stats = _solve(model, 'gpu')
    assert stats['gpu_fallbacks'] == 1 and gpu_cg._disabled is None
    assert cp.get_default_memory_pool().total_bytes() == 0


def _matrix_free_system():
    from duocad.topopt import anchor

    material = np.ones((8, 4, 4), bool)
    material[3:5, 0, :] = False
    floating = anchor.floating_dofs(material)
    return fem.Model(8, 4, 4, extra_fixed_dofs=floating, active_mask=material), material.astype(float)


def test_the_matrix_free_cg_falls_back_to_the_cpu_when_the_gpu_cannot_solve(monkeypatch):
    """落ちたら、組み立てない CG の GPU 版が使えないとき CPU で解き直さず落ちるか、戻した理由を数えていない。"""
    model, density = _matrix_free_system()
    expected = model.solve(density, 3.0, solver='matrix_free')

    def unavailable(*args, **kwargs):
        raise gpu_cg.Unavailable('vram: test', sticky=False)

    monkeypatch.setattr(gpu_cg, 'solve_matrix_free', unavailable)
    stats = fem.new_solve_stats()
    monkeypatch.setattr(fem, '_SOLVE_STATS', stats)
    with pytest.warns(RuntimeWarning, match='CPU で解き直す'):
        got = model.prepare(density, 3.0, solver='matrix_free', device='gpu').solve(model.f[None, :])[0]
    assert np.array_equal(got, expected) and stats['gpu_solves'] == 0 and stats['gpu_fallbacks'] == 1


@needs_gpu
def test_the_matrix_free_cg_on_the_gpu_gives_the_cpu_displacement(monkeypatch):
    """落ちたら、組み立てない CG の GPU 版の行列ベクトル積・前処理・拘束が CPU 版とずれている(足し順の差しか許さない)。"""
    model, density = _matrix_free_system()
    expected = model.solve(density, 3.0, solver='matrix_free')
    stats = fem.new_solve_stats()
    monkeypatch.setattr(fem, '_SOLVE_STATS', stats)
    got = model.prepare(density, 3.0, solver='matrix_free', device='gpu').solve(model.f[None, :])[0]
    assert stats['gpu_solves'] == 1 and stats['gpu_fallbacks'] == 0
    assert np.allclose(got, expected, rtol=1e-6, atol=1e-9 * np.abs(expected).max())

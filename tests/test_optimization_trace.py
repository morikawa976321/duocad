import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from duocad.topopt import trace, fem, mesh


@pytest.fixture
def recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, 'DEFAULT_ROOT', tmp_path)
    monkeypatch.setattr(trace.Recorder, 'provenance', lambda self: None)
    return tmp_path


def snapshot(recorder, name):
    return trace.load_snapshot(next(recorder.path.glob(f'*-{name}.json')))


def test_arrays_roundtrip_exactly_and_deduplicate(recordings):
    a = np.arange(24,dtype=np.float64).reshape(4,6)[:,::2]
    with trace.recording() as rec:
        rec.snapshot('arrays',dict(a=a,b=a.copy(),empty=np.empty((0,3)),scalar=np.array(2.),nan=float('nan')))
    out = snapshot(rec,'arrays')
    assert np.array_equal(out['a'],a)
    assert np.array_equal(out['b'],a)
    assert out['empty'].shape == (0,3)
    assert out['scalar'].shape == ()
    assert np.isnan(out['nan'])
    assert len(list((rec.path/'arrays').glob('*.npz'))) == 3
    assert json.loads((rec.path/'manifest.json').read_text())['status']=='complete'


def test_corrupt_array_is_rejected(recordings):
    with trace.recording() as rec:
        rec.snapshot('arrays',dict(a=np.array([1.,2.])))
    path=next((rec.path/'arrays').glob('*.npz'))
    np.savez_compressed(path,data=np.array([3.,4.]))
    with pytest.raises(ValueError,match='ハッシュ'): snapshot(rec,'arrays')


def test_repeated_solves_are_recorded_reproducibly(recordings):
    """`trace.recording()` は録っても数値を変えず(録ることが計算に影響しない)、同じ
    `@trace.stage`/`trace.snapshot` の複数回の呼び出しをそれぞれ別スナップショットとして残す。

    旧SIMPの設計反復(`simp.optimize`)を追っていた検査の後継。二値探索
    (`binary_search`/`acceptance`)は候補ごとにトレースを打たない(段階6の契約)ので、
    代わりに `fem.Model.solve_many` が1回の呼び出しの中で複数右辺を逐次解く経路
    (`fem.py` の `linear-solve`/`linear-residual` が右辺ごとに1回出る)で同じ性質を見る。
    """
    model=fem.Model(6,3,3)
    rho=np.ones((6,3,3))
    rhs=np.stack([model.f,model.f,model.f])
    expected=model.solve_many(rho,3.0,rhs,solver='direct')
    with trace.recording() as rec:
        actual=model.solve_many(rho,3.0,rhs,solver='direct')
    assert np.array_equal(actual,expected)
    solves=sorted(rec.path.glob('*-linear-solve.json'))
    residuals=sorted(rec.path.glob('*-linear-residual.json'))
    assert len(solves)==len(residuals)==3
    for index,path in enumerate(solves):
        assert trace.load_snapshot(path)['rhs_index']==index


def test_surface_replay_and_all_smoothing_substeps(recordings):
    rho=np.zeros((8,8,8),dtype=bool);rho[2:6,2:6,2:6]=True
    expected=mesh.build_surface_from_mask(rho,n_iter=3)
    with trace.recording() as rec:
        actual=mesh.build_surface_from_mask(rho,n_iter=3)
    saved=snapshot(rec,'surface-from-mask-output')
    assert np.array_equal(expected.verts_smooth,actual.verts_smooth)
    assert np.array_equal(saved['verts_raw'],actual.verts_raw)
    assert len(list(rec.path.glob('*-taubin-lambda.json')))==3
    assert len(list(rec.path.glob('*-taubin-mu.json')))==3
    fields=snapshot(rec,'surface-fields')
    v,f,_=mesh.run_marching_cubes(fields['padded'])
    assert np.array_equal(v,actual.verts_raw)
    assert np.array_equal(f,actual.faces)


def test_failures_keep_completed_checkpoints_and_reset_context(recordings):
    @trace.stage('broken')
    def broken():
        trace.snapshot('before-error',rho=np.ones((2,2,2)))
        raise RuntimeError('deliberate')
    with pytest.raises(RuntimeError,match='deliberate'):
        with trace.recording() as rec: broken()
    assert json.loads((rec.path/'manifest.json').read_text())['status']=='failed'
    assert snapshot(rec,'before-error')['rho'].sum()==8
    assert 'deliberate' in snapshot(rec,'broken-error')['traceback']
    assert trace.current() is None


def test_cancelled_status(recordings):
    from duocad.optimize_run import OptimizeCancelled
    with pytest.raises(OptimizeCancelled):
        with trace.recording() as rec: raise OptimizeCancelled('stop')
    assert json.loads((rec.path/'manifest.json').read_text())['status']=='cancelled'


def test_concurrent_runs_are_isolated(recordings):
    def worker(i):
        with trace.recording() as rec: trace.snapshot('identity',value=i)
        return rec.path,snapshot(rec,'identity')['value']
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(worker,range(2)))
    assert results[0][0]!=results[1][0]
    assert [x[1] for x in results]==[0,1]


def test_save_failure_does_not_stop_the_run_and_is_counted(recordings,monkeypatch):
    """落ちたら、記録の保存失敗が計算まで巻き込んで止めている、または黙って消えて数に残っていない。"""
    with trace.recording() as rec:
        def fail(*args): raise OSError('disk full')
        monkeypatch.setattr(trace,'_atomic',fail)
        assert rec.snapshot('important',np.ones(3)) is None
    assert trace.current() is None
    assert rec.write_failures >= 1
    assert rec.manifest['write_failures'] == rec.write_failures


def test_a_non_io_encoding_error_still_propagates(recordings):
    """落ちたら、記録できない値(object配列)を渡した書き方の誤りまで握りつぶしている。"""
    with pytest.raises(TypeError):
        with trace.recording() as rec:
            rec.snapshot('bad',dict(a=np.array([object()],dtype=object)))


def test_the_array_directory_is_recreated_when_it_disappears_mid_run(recordings):
    """落ちたら、実行中に arrays/ が消えたあと記録が回復せず、その実行の残り全部が落ちる。"""
    with trace.recording() as rec:
        rec.snapshot('first',dict(a=np.ones(3)))
        shutil.rmtree(rec.path/'arrays')
        rec.snapshot('second',dict(a=np.arange(5.)))
    assert np.array_equal(snapshot(rec,'second')['a'],np.arange(5.))
    assert rec.write_failures == 0


def test_product_input_error_is_archived(recordings):
    from duocad.session import Session
    from duocad import optimize_run
    s=Session(None,.2)
    with pytest.raises(optimize_run.OptimizeInputError): optimize_run.run(s.document)
    run=next(recordings.iterdir())
    assert (run/'input.json').exists()
    assert json.loads((run/'manifest.json').read_text(encoding='utf-8'))['status']=='failed'


def test_real_cad_and_source_archive_are_complete(tmp_path):
    import hashlib
    import zipfile
    from duocad.session import Session
    from duocad import kernel
    s=Session(None,.2)
    sketch=s.sketch_create('XY')['sketch_id']
    s.add_rectangle(sketch,[0.,0.],2.,3.)
    s.modify_selection([{'sketch':sketch,'index':1}],[])
    s.extrude(4.,mode='new')
    with trace.recording(s.document,root=tmp_path) as rec: pass
    assert not list(rec.path.rglob('*.tmp'))
    with zipfile.ZipFile(rec.path/'source.zip') as archive:
        assert 'duocad/topopt/trace.py' in archive.namelist()
    manifest=json.loads((rec.path/'manifest.json').read_text(encoding='utf-8'))
    assert hashlib.sha256((rec.path/'source.zip').read_bytes()).hexdigest()==manifest['source_sha256']
    restored=kernel.read_step(str(next((rec.path/'cad').glob('*.step'))))
    assert kernel.volume(restored)==pytest.approx(24.)


def test_git_provenance_returns_while_another_thread_blocks_on_piped_stdin():
    """stdio の MCP サーバ(別スレッドが stdin を readline 待ち)でも記録の git が返る。落ちたら、
    UI から走らせた実行が冒頭で15秒待って failed になる。"""
    code = (
        'import os, sys, threading, time\n'
        'from duocad.topopt import trace\n'
        'threading.Thread(target=sys.stdin.buffer.readline, daemon=True).start()\n'
        'time.sleep(0.3)\n'
        "print(trace._git('rev-parse','HEAD')['returncode'], flush=True)\n"
        'os._exit(0)\n'
    )
    child = subprocess.Popen([sys.executable,'-c',code], cwd=trace.ROOT,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        try:
            child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail('git が返らず、子プロセスが終わらない')
        assert child.stdout.read().strip(), 'git が結果を返さずに子プロセスが終わった'
    finally:
        if sys.platform == 'win32':
            subprocess.run(['taskkill','/F','/T','/PID',str(child.pid)], capture_output=True)
        else:
            child.kill()
        child.stdin.close()
        child.stdout.close()
        child.wait()

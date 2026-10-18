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
    """落ちたら、記録すると数値が変わるか、同じ段の複数回の呼び出し(`solve_many` の右辺ごとの
    `linear-solve`/`linear-residual`)が別々のスナップショットとして残っていない。"""
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
        name = rec.snapshot('important',np.ones(3))
    assert trace.current() is None
    assert not (rec.path/name).exists()
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
        rec.flush()
        shutil.rmtree(rec.path/'arrays')
        rec.snapshot('second',dict(a=np.arange(5.)))
    assert np.array_equal(snapshot(rec,'second')['a'],np.arange(5.))
    assert rec.write_failures == 0


def _grid_and_triangle():
    from duocad.topopt.voxelize import Grid
    return (Grid(origin=(0.,0.,0.), spacing=2., shape=(1,1,1)),
            np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]]), np.array([[0,1,2]]))


def test_the_final_stl_is_written_with_its_hash_in_the_manifest(recordings):
    """落ちたら、最終メッシュの記録(final.stl とその内容ハッシュ)が残っていない。"""
    import hashlib
    grid, vertices, faces = _grid_and_triangle()
    with trace.recording() as rec:
        trace.final_mesh(vertices,vertices,faces,grid,final_vertices=vertices,final_faces=faces)
    manifest=json.loads((rec.path/'manifest.json').read_text(encoding='utf-8'))
    assert hashlib.sha256((rec.path/'final.stl').read_bytes()).hexdigest()==manifest['final_stl_sha256']
    assert rec.write_failures == 0


def test_the_final_stl_write_failure_does_not_lose_the_solved_shape(recordings,monkeypatch):
    """落ちたら、探索が終わった後の final.stl の書き込み失敗が最適化全体を巻き込んで落としている。"""
    grid, vertices, faces = _grid_and_triangle()
    real=trace._atomic
    def fail(path,write):
        if path.name=='final.stl': raise OSError('disk full')
        return real(path,write)
    with trace.recording() as rec:
        monkeypatch.setattr(trace,'_atomic',fail)
        trace.final_mesh(vertices,vertices,faces,grid,final_vertices=vertices,final_faces=faces)
    manifest=json.loads((rec.path/'manifest.json').read_text(encoding='utf-8'))
    assert 'final_stl_sha256' not in manifest
    assert manifest['write_failures'] == rec.write_failures == 1
    assert manifest['status'] == 'complete'


def test_a_failed_input_json_does_not_stop_the_run_from_starting(recordings,monkeypatch):
    """落ちたら、記録の開始時の書き込み失敗だけで実行が始まらない(方針が snapshot と食い違っている)。"""
    from duocad.session import Session
    session=Session(.2)
    real=trace._atomic
    def fail(path,write):
        if path.name=='input.json': raise OSError('disk full')
        return real(path,write)
    monkeypatch.setattr(trace,'_atomic',fail)
    with trace.recording(session.document) as rec:
        trace.snapshot('after-start',value=1)
    manifest=json.loads((rec.path/'manifest.json').read_text(encoding='utf-8'))
    assert 'input_sha256' not in manifest
    assert manifest['write_failures'] == 1 and manifest['status'] == 'complete'
    assert snapshot(rec,'after-start')['value'] == 1


def test_product_input_error_is_archived(recordings):
    from duocad.session import Session
    from duocad import optimize_run
    s=Session(.2)
    with pytest.raises(optimize_run.OptimizeInputError): optimize_run.run(s.document)
    run=next(recordings.iterdir())
    assert (run/'input.json').exists()
    assert json.loads((run/'manifest.json').read_text(encoding='utf-8'))['status']=='failed'


def test_an_unwritable_run_folder_does_not_stop_the_run(tmp_path, monkeypatch):
    """落ちたら、記録のフォルダが作れない(権限・ディスク)だけで最適化が始まらない。"""
    monkeypatch.setattr(trace.Recorder, 'provenance', lambda self: None)
    blocker = tmp_path/'not-a-directory'
    blocker.write_text('')
    with trace.recording(root=blocker) as rec:
        trace.snapshot('value', a=np.ones(2))
    assert rec.write_failures >= 2


def test_git_that_is_missing_or_hangs_is_recorded_instead_of_raised(monkeypatch):
    """落ちたら、git が無い・15秒で返らないだけで最適化が始まらない(`TimeoutExpired` は `OSError` ではない)。"""
    for error in (FileNotFoundError('git'), subprocess.TimeoutExpired(['git'], 15)):
        def run(*args, **kwargs):
            raise error
        monkeypatch.setattr(trace.subprocess, 'run', run)
        result = trace._git('rev-parse', 'HEAD')
        assert result['returncode'] is None and type(error).__name__ in result['error']


def test_failed_provenance_document_and_cad_writes_are_counted_and_the_run_goes_on(tmp_path, monkeypatch):
    """落ちたら、記録の開始時の source.zip・入力JSON・STEP の書き出し失敗で実行ごと止まる。"""
    from duocad import kernel
    from duocad.session import Session
    s=Session(.2)
    sketch=s.sketch_create('XY')['sketch_id']
    s.add_rectangle(sketch,[0.,0.],2.,3.)
    s.modify_selection([{'sketch':sketch,'index':1}],[])
    s.extrude(4.,mode='new')
    real_atomic = trace._atomic

    def atomic(path, write):
        if path.suffix in ('.zip', '.json') and path.name != 'manifest.json' and '-' not in path.name:
            raise OSError('disk full')
        return real_atomic(path, write)

    def write_step(*args):
        raise RuntimeError('STEP を書けない形')

    monkeypatch.setattr(trace, '_atomic', atomic)
    monkeypatch.setattr(kernel, 'write_step', write_step)
    with trace.recording(s.document, root=tmp_path) as rec:
        pass
    manifest = json.loads((rec.path/'manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'complete'
    assert manifest['write_failures'] == rec.write_failures == 3
    assert 'source_sha256' not in manifest and 'input_sha256' not in manifest


def test_real_cad_and_source_archive_are_complete(tmp_path):
    import hashlib
    import zipfile
    from duocad.session import Session
    from duocad import kernel
    s=Session(.2)
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


def test_a_snapshot_keeps_the_values_it_was_given_even_if_the_caller_changes_them_afterwards(recordings):
    """落ちたら、書き出しスレッドが書く前に呼び出し側が配列を書き換えると、記録が書き換え後の値になる。"""
    work = np.zeros(1000)
    with trace.recording() as rec:
        for value in range(3):
            work[:] = value
            rec.snapshot(f'step{value}', dict(work=work))
    for value in range(3):
        assert np.all(snapshot(rec, f'step{value}')['work'] == value)
    events = trace.RunArchive(rec.path).snapshots()
    assert [e['name'] for e in events][-3:] == ['step0', 'step1', 'step2']


def test_an_array_that_could_not_be_written_is_written_by_the_next_snapshot_that_uses_it(recordings, monkeypatch):
    """落ちたら、書けなかった・消えた・符号化の途中で投げた記録の配列を「書いた」と覚え、後の JSON が無い配列を参照する。"""
    real = trace._atomic
    calls = {'n': 0}

    def fail_once(path, write):
        if path.suffix == '.npz' and calls['n'] == 0:
            calls['n'] += 1
            raise OSError('一時的に書けない')
        return real(path, write)

    shared = np.arange(7.)
    with trace.recording() as rec:
        monkeypatch.setattr(trace, '_atomic', fail_once)
        rec.snapshot('first', dict(a=shared))
        rec.snapshot('second', dict(a=shared))
        rec.flush()
        monkeypatch.setattr(trace, '_atomic', real)
        shutil.rmtree(rec.path/'arrays')
        rec.snapshot('third', dict(a=shared))
        with pytest.raises(TypeError):
            rec.snapshot('broken', dict(good=np.arange(3.), bad=np.array([object()], dtype=object)))
        rec.snapshot('fourth', dict(good=np.arange(3.)))
    assert rec.write_failures == 1
    assert not list(rec.path.glob('*-first.json'))
    for name in ('second', 'third'):
        assert np.array_equal(snapshot(rec, name)['a'], shared)
    assert np.array_equal(snapshot(rec, 'fourth')['good'], np.arange(3.))


def test_an_event_is_encoded_when_it_is_recorded(recordings):
    """落ちたら、イベントの値が積んだ後の書き換えで変わるか、記録できない値の誤りが別の記録の時点で投げられる。"""
    values = [1]
    with trace.recording() as rec:
        rec.event('custom', values=values)
        values[0] = 99
        with pytest.raises(TypeError):
            rec.event('custom', values=[object()])
    custom = [e for e in trace.RunArchive(rec.path).events if e['kind'] == 'custom']
    assert [e['values'] for e in custom] == [[1]]


def test_an_error_in_the_run_reaches_the_caller_and_the_writer_is_closed(recordings):
    """落ちたら、記録の後始末が計算の例外を別の例外で隠すか、書き出しスレッドが残る。"""
    with pytest.raises(RuntimeError, match='計算の失敗'):
        with trace.recording() as rec:
            for index in range(10):
                rec.snapshot(f'step{index}', dict(a=np.full(1000, float(index))))
            raise RuntimeError('計算の失敗')
    assert rec._writer._shutdown
    assert json.loads((rec.path/'manifest.json').read_text(encoding='utf-8'))['status'] == 'failed'
    assert snapshot(rec, 'step9')['a'][0] == 9.


def test_a_snapshot_whose_shared_array_disappears_before_it_is_written_is_not_published(recordings):
    """落ちたら、積んでから書くまでの間に既存の配列が消えたとき、参照の切れた JSON を成功扱いで残している。"""
    import threading

    shared = np.arange(5.)
    gate = threading.Event()
    with trace.recording() as rec:
        rec.snapshot('first', dict(a=shared))
        rec.flush()
        rec._submit(gate.wait)
        name = rec.snapshot('second', dict(a=shared))
        shutil.rmtree(rec.path/'arrays')
        gate.set()
        rec.flush()
        rec.snapshot('third', dict(a=shared))
    assert not (rec.path/name).exists()
    assert rec.write_failures == 1
    assert np.array_equal(snapshot(rec, 'third')['a'], shared)


def test_a_name_that_cannot_be_recorded_is_rejected_when_the_snapshot_is_taken(recordings):
    """落ちたら、スナップショットの名前などイベント行に入る値の誤りが、その場で投げられず書き出しスレッドで数えられて消える。"""
    with trace.recording() as rec:
        with pytest.raises(TypeError):
            rec.snapshot(np.int64(7), {'value': 1})
    assert rec.write_failures == 0


def test_a_snapshot_is_not_published_when_one_of_its_arrays_disappears_while_another_is_written(recordings, monkeypatch):
    """落ちたら、新しい配列を書いている間に既存の配列が消えたとき、参照の切れた JSON を成功扱いで残している。"""
    old, new = np.arange(4.), np.arange(6.)
    real = trace._atomic

    def remove_arrays_while_writing(path, write):
        if path.suffix == '.npz':
            for existing in path.parent.glob('*.npz'):
                existing.unlink()
        return real(path, write)

    with trace.recording() as rec:
        rec.snapshot('first', dict(a=old))
        rec.flush()
        monkeypatch.setattr(trace, '_atomic', remove_arrays_while_writing)
        name = rec.snapshot('second', dict(a=old, b=new))
        rec.flush()
        monkeypatch.setattr(trace, '_atomic', real)
        rec.snapshot('third', dict(a=old, b=new))
    assert not (rec.path/name).exists()
    assert rec.write_failures == 1
    third = snapshot(rec, 'third')
    assert np.array_equal(third['a'], old) and np.array_equal(third['b'], new)

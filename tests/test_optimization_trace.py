import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from duocad.topopt import trace, simp, mesh


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


def test_simp_recording_keeps_numerics_and_density_timing(recordings):
    kwargs=dict(nelx=6,nely=3,nelz=3,max_iter=3,solver='direct',threads=1,rmin=1.5)
    expected=simp.optimize(**kwargs)
    with trace.recording() as rec:
        actual=simp.optimize(**kwargs)
    assert np.array_equal(actual.rho,expected.rho)
    assert [i.compliance for i in actual.history]==[i.compliance for i in expected.history]
    inputs=sorted(rec.path.glob('*-iteration-input.json'))
    outputs=sorted(rec.path.glob('*-iteration-output.json'))
    analyses=sorted(rec.path.glob('*-iteration-analysis.json'))
    assert len(inputs)==len(outputs)==len(analyses)==3
    for i in range(2):
        assert np.array_equal(trace.load_snapshot(outputs[i])['rho'],trace.load_snapshot(inputs[i+1])['rho'])
    assert np.array_equal(trace.load_snapshot(outputs[-1])['rho'],actual.rho)
    initial=snapshot(rec,'simp-initial')
    analysis=trace.load_snapshot(analyses[0])
    assert np.isclose(initial['model']['f']@analysis['u'],analysis['compliance'])
    assert len(list(rec.path.glob('*-linear-residual.json')))==3


def test_surface_replay_and_all_smoothing_substeps(recordings):
    rho=np.zeros((8,8,8));rho[2:6,2:6,2:6]=.8
    expected=mesh.build_surface(rho,n_iter=3)
    with trace.recording() as rec:
        actual=mesh.build_surface(rho,n_iter=3)
    saved=snapshot(rec,'surface-output')
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


def test_save_failure_is_not_silently_ignored(recordings,monkeypatch):
    with pytest.raises(OSError,match='disk full'):
        with trace.recording() as rec:
            def fail(*args): raise OSError('disk full')
            monkeypatch.setattr(trace,'_atomic',fail)
            rec.snapshot('important',np.ones(3))
    assert trace.current() is None


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

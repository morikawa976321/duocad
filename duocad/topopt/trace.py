"""実製品の1実行に閉じた診断記録。配列は損失なし・内容ハッシュで重複排除する。

ContextVarは製品の実行器スレッド内だけに設定する。数値関数を単独で呼ぶと記録しない。
保存失敗は例外として伝え、記録が欠けた実行を成功と表示しない。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from functools import wraps
import hashlib
import inspect
import json
import math
import marshal
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import traceback
from uuid import uuid4
import zipfile
from importlib import metadata

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = ROOT / 'output' / 'optimization_runs'
_current = ContextVar('optimization_trace', default=None)


def current():
    return _current.get()


def _atomic(path, write):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as stream:
        write(stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Recorder:
    def __init__(self, root=None):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        self.path = Path(root or DEFAULT_ROOT) / f'{stamp}-{uuid4().hex[:12]}'
        (self.path / 'arrays').mkdir(parents=True)
        self.started = time.perf_counter()
        self.write_seconds = 0.
        self.sequence = 0
        self.stack = []
        self.manifest = dict(schema_version=1, run_id=self.path.name, status='running',
                             process_id=os.getpid(),
                             started_utc=datetime.now(timezone.utc).isoformat(),
                             array_format='lossless numpy NPZ; allow_pickle=False',
                             coordinate_rule='world_mm = grid.origin + grid.spacing * mesh_vertices; cell center i+0.5',
                             omitted=['assembled stiffness matrices and multigrid caches (rebuild from model inputs)',
                                      'native CAD objects in stage snapshots (exact per-body STEP and input.json saved separately)'])
        self._manifest()

    def _json(self, path, value):
        data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        _atomic(path, lambda stream: stream.write(data))

    def _manifest(self):
        self.manifest.update(elapsed_seconds=time.perf_counter()-self.started,
                             write_seconds=self.write_seconds, records=self.sequence)
        self._json(self.path/'manifest.json', self.manifest)

    def event(self, kind, **values):
        row = dict(kind=kind, elapsed_seconds=time.perf_counter()-self.started,
                   phase=self.stack[-1] if self.stack else None, **values)
        with (self.path/'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
            stream.flush()
        self.manifest['last_event'] = row
        self._manifest()

    def _encode(self, value, seen):
        if isinstance(value, np.ndarray):
            if value.dtype.hasobject:
                raise TypeError('object配列は診断記録に保存できない')
            array = np.asarray(value, order='C')
            digest = hashlib.sha256(json.dumps([array.dtype.str, array.shape]).encode())
            digest.update(memoryview(array.ravel()).cast('B'))
            key = digest.hexdigest()
            relative = f'arrays/{key}.npz'
            path = self.path/relative
            if not path.exists():
                _atomic(path, lambda stream: np.savez_compressed(stream, data=array))
            return {'$array':relative, 'shape':list(value.shape), 'dtype':value.dtype.str}
        if isinstance(value, np.generic):
            return self._encode(value.item(), seen)
        if isinstance(value, float) and not math.isfinite(value):
            return {'$float':str(value)}
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(k):self._encode(v, seen) for k,v in value.items()}
        if isinstance(value, (tuple, list)):
            return [self._encode(v, seen) for v in value]
        if is_dataclass(value):
            return {'$type':type(value).__name__, **{f.name:self._encode(getattr(value,f.name),seen)
                    for f in fields(value) if f.name != 'cache'}}
        if type(value).__name__ == 'Model' and hasattr(value, 'ndof'):
            names = ('nelx','nely','nelz','ndof','E','nu','emin','fixed','free','f','active',
                     '_base_active','_base_fixed','dynamic_active','ke')
            return {'$type':'FEMModel', **{n:self._encode(getattr(value,n),seen) for n in names}}
        if isinstance(value, BaseException):
            return {'$error':type(value).__name__, 'message':str(value),
                    'details':self._encode(vars(value),seen)}
        return {'$omitted_type':f'{type(value).__module__}.{type(value).__name__}'}

    def snapshot(self, name, value):
        started = time.perf_counter()
        self.sequence += 1
        filename = f'{self.sequence:06d}-{name}.json'
        self._json(self.path/filename, self._encode(value, {}))
        self.write_seconds += time.perf_counter()-started
        self.event('snapshot', name=name, file=filename)
        return filename

    @contextmanager
    def phase(self, name, inputs):
        self.sequence += 1
        phase = f'{self.sequence:06d}-{name}'
        self.event('begin', name=name, child=phase)
        self.stack.append(phase)
        try:
            self.snapshot(name+'-input', inputs)
            yield
        except BaseException as error:
            self.snapshot(name+'-error', {'exception':error,'traceback':traceback.format_exc()})
            self.event('error', name=name)
            raise
        else:
            self.event('end', name=name)
        finally:
            self.stack.pop()

    def provenance(self):
        versions = {d.metadata['Name']:d.version for d in metadata.distributions() if d.metadata['Name']}
        def git(*args):
            result = subprocess.run(['git',*args], cwd=ROOT, capture_output=True, timeout=15)
            return {'returncode':result.returncode,'stdout':result.stdout.decode('utf-8','replace'),
                    'stderr':result.stderr.decode('utf-8','replace')}
        runtime = {}
        for name, module in list(sys.modules.items()):
            if name.startswith('duocad.') and module is not None:
                runtime[name] = {key:hashlib.sha256(marshal.dumps(inspect.unwrap(value).__code__)).hexdigest()
                                 for key,value in vars(module).items() if inspect.isfunction(value)}
        self.snapshot('environment', dict(python=sys.version, executable=sys.executable,
                      loaded_function_hashes=runtime,
                      platform=platform.platform(), cpu_count=os.cpu_count(), packages=versions,
                      threads={k:os.environ[k] for k in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS') if k in os.environ},
                      git_head=git('rev-parse','HEAD'), git_diff=git('diff','--','duocad','pyproject.toml'),
                      git_status=git('status','--short')))
        def write_sources(stream):
            with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted((ROOT/'duocad').rglob('*.py')):
                    archive.write(path,path.relative_to(ROOT).as_posix())
                for name in ('pyproject.toml','uv.lock'):
                    if (ROOT/name).exists(): archive.write(ROOT/name,name)
        _atomic(self.path/'source.zip',write_sources)
        self.manifest['source_sha256'] = hashlib.sha256((self.path/'source.zip').read_bytes()).hexdigest()

    def document(self, doc, name):
        data = doc.log.to_json().encode('utf-8')
        _atomic(self.path/f'{name}.json',lambda stream:stream.write(data))
        self.manifest[name+'_sha256'] = hashlib.sha256(data).hexdigest()
        self.snapshot(name+'-state', dict(parameters=doc.parameters, optimize=doc.optimize,
                      bodies=[dict(id=b.id,name=b.name,kind=b.kind) for b in doc.bodies]))

    def finish(self, status, **values):
        self.manifest.update(status=status, finished_utc=datetime.now(timezone.utc).isoformat(), **values)
        self._manifest()


@contextmanager
def recording(doc=None, options=None, root=None):
    recorder = Recorder(root)
    token = _current.set(recorder)
    try:
        recorder.provenance()
        if doc is not None:
            recorder.document(doc,'input')
            from .. import kernel
            cad = recorder.path/'cad'
            cad.mkdir()
            for body in doc.bodies:
                if body.shape is not None:
                    target = cad/f'{body.id}.step'
                    if target.resolve().parent != cad.resolve():
                        raise ValueError('CAD保存先のボディIDが不正')
                    temporary = target.with_suffix('.step.tmp')
                    kernel.write_step(body.shape,str(temporary))
                    temporary.replace(target)
        recorder.snapshot('requested-options', options)
        yield recorder
    except BaseException as error:
        try:
            recorder.snapshot('failure', {'exception':error,'traceback':traceback.format_exc()})
            recorder.finish('cancelled' if type(error).__name__=='OptimizeCancelled' else 'failed',
                            error=f'{type(error).__name__}: {error}')
        except Exception:
            pass  # 保存障害の最中でも、元の例外を別の保存例外で隠さない。
        raise
    else:
        if recorder.manifest['status'] == 'running':
            recorder.finish('complete')
    finally:
        _current.reset(token)


def stage(name):
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        def wrapped(*args, **kwargs):
            recorder = current()
            if recorder is None:
                return function(*args,**kwargs)
            bound = signature.bind(*args,**kwargs)
            bound.apply_defaults()
            with recorder.phase(name,bound.arguments):
                result = function(*args,**kwargs)
                recorder.snapshot(name+'-output',result)
                return result
        return wrapped
    return decorate


def snapshot(name, **values):
    recorder = current()
    if recorder is not None:
        recorder.snapshot(name,values)


def final_mesh(raw, smooth, faces, grid, *, final_vertices=None, final_faces=None):
    recorder = current()
    if recorder is None:
        return
    raw = np.asarray(raw)*grid.spacing + np.asarray(grid.origin)
    smooth = np.asarray(smooth)*grid.spacing + np.asarray(grid.origin)
    if final_vertices is not None:
        recorder.snapshot('pre-boolean-mesh-world',dict(raw_vertices=raw,vertices=smooth,faces=faces,grid=grid))
        smooth, faces = np.asarray(final_vertices), np.asarray(final_faces)
        recorder.snapshot('final-mesh-world',dict(vertices=smooth,faces=faces,grid=grid,
                                                source='boolean CAD tessellation'))
    else:
        recorder.snapshot('final-mesh-world',dict(raw_vertices=raw,vertices=smooth,faces=faces,grid=grid))
    def write(stream):
        stream.write(b'solid optimized\n')
        for triangle in smooth[np.asarray(faces)]:
            text = 'facet normal 0 0 0\nouter loop\n'
            text += ''.join('vertex '+' '.join(format(float(v),'.17g') for v in point)+'\n' for point in triangle)
            stream.write((text+'endloop\nendfacet\n').encode('ascii'))
        stream.write(b'endsolid optimized\n')
    _atomic(recorder.path/'final.stl',write)
    recorder.manifest['final_stl_sha256'] = hashlib.sha256((recorder.path/'final.stl').read_bytes()).hexdigest()


def load_snapshot(path):
    """記録を辞書と配列として読む。クラスの復元やpickleの実行はしない。"""
    path = Path(path)
    root = path.parent.resolve()
    def decode(value):
        if isinstance(value,list): return [decode(v) for v in value]
        if not isinstance(value,dict): return value
        if '$array' in value:
            target = (root/value['$array']).resolve()
            if not target.is_relative_to(root): raise ValueError('配列パスが実行フォルダの外')
            with np.load(target,allow_pickle=False) as data: array = data['data']
            digest = hashlib.sha256(json.dumps([array.dtype.str,array.shape]).encode())
            digest.update(memoryview(np.ascontiguousarray(array).ravel()).cast('B'))
            if digest.hexdigest()!=target.stem: raise ValueError('配列の内容ハッシュが一致しない')
            if list(array.shape)!=value['shape'] or array.dtype.str!=value['dtype']:
                raise ValueError('配列の形または型が一致しない')
            return array
        if '$float' in value: return float(value['$float'])
        return {k:decode(v) for k,v in value.items()}
    return decode(json.loads(path.read_text(encoding='utf-8')))

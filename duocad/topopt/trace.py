"""実製品の1実行に閉じた診断記録。配列は損失なし・内容ハッシュで重複排除する。

ContextVarは製品の実行器スレッド内だけに設定する。数値関数を単独で呼ぶと記録しない。
**記録の書き出しが失敗しても計算を止めない**(スナップショット・イベント・manifest・source.zip と git・
document の JSON・入力ボディの STEP・final.stl のすべてが best effort)。記録は診断のための仕組みであり、
解けた形状を捨てる権利を持たない(`arrays/` が実行中に消えた `OSError` が `acceptance.safety_check` の
`except Exception` に落ち、解けている候補を20回続けて不合格にした実績がある)。失敗は握りつぶさず
`write_failures` に数えて manifest と stderr へ出す。`_encode` の `TypeError` は伝える —— 記録できない値を
渡したのはこちらの書き方の誤りで、飲むと永久に気づけない。
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
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
import threading
import time
import traceback
from uuid import uuid4
import zipfile
from importlib import metadata

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = ROOT / 'output' / 'optimization_runs'

#: 記録の書き込み失敗を stderr へ出す上限。同じ障害は何百回も続くので、全部出すと本当のエラーが埋もれる。
#: 回数そのものは manifest の `write_failures` が持つ。
_FAILURE_REPORT_LIMIT = 3
#: 書き出しスレッドに積んでおける仕事(スナップショット・イベント)の数。プロセスごと落ちると、ここまでの数の記録が残らない
#: (例外で抜けたときは `finish` が書き終えるのを待つ)。
_PENDING_LIMIT = 4
_current = ContextVar('optimization_trace', default=None)


def current():
    return _current.get()


def _atomic(path, write):
    # 書く直前に親を作る(開始時に1回作るだけでは、実行中にディレクトリが消えると最後まで回復しない)。
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as stream:
        write(stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _fem_device():
    from . import fem  # fem が trace を import するので、ここで遅れて読む
    return fem.FEM_DEVICE


def _git(*args):
    # stdin を継承させない: MCP を stdio で受けるサーバは別スレッドが stdin を readline し続けており、
    # Windows では継承した git がその読みが返るまで(次の1行が届くまで)返らず15秒で時間切れになる。
    # git が無い(`FileNotFoundError`)・時間切れ(`TimeoutExpired` は `OSError` ではない)は記録に残して続ける。
    try:
        result = subprocess.run(['git',*args], cwd=ROOT, capture_output=True, stdin=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        return {'returncode':None,'error':f'{type(error).__name__}: {error}'}
    return {'returncode':result.returncode,'stdout':result.stdout.decode('utf-8','replace'),
            'stderr':result.stderr.decode('utf-8','replace')}


class Recorder:
    def __init__(self, root=None):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        self.path = Path(root or DEFAULT_ROOT) / f'{stamp}-{uuid4().hex[:12]}'
        self.started = time.perf_counter()
        self.write_seconds = 0.
        self.sequence = 0
        self.write_failures = 0
        self.stack = []
        # 書き出しは1本のスレッドが積まれた順に行う(配列 → スナップショットの JSON → イベントの行)。だから「書き終えた配列を
        # 参照する JSON だけを公開する」は保たれる。圧縮(zlib)は GIL を放すので、その間も計算は進む(1 評価 0.6 秒。
        # docs/TOPOPT_SPEED.md)。値の JSON 化と配列の写しは積む前に呼び出し側で済ませる(書き方の誤りはその場で投げ、
        # 積んだ後に呼び出し側が値を書き換えても記録は変わらない)。積んだ仕事は例外を外へ出さない(数えて捨てる)。
        # `manifest`・失敗の数・書けた配列の鍵・`write_seconds` は両方のスレッドが触るので `_lock` の中で触る。
        self._lock = threading.RLock()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix='trace-writer')
        self._pending = deque()
        self._written_arrays = set()
        self.manifest = dict(schema_version=1, run_id=self.path.name, status='running',
                             process_id=os.getpid(),
                             started_utc=datetime.now(timezone.utc).isoformat(),
                             array_format='lossless numpy NPZ; allow_pickle=False',
                             coordinate_rule='world_mm = grid.origin + grid.spacing * mesh_vertices; cell center i+0.5',
                             omitted=['assembled stiffness matrices (rebuild from model inputs)',
                                      'native CAD objects in stage snapshots (exact per-body STEP and input.json saved separately)'])
        try:
            (self.path / 'arrays').mkdir(parents=True)
        except OSError as error:
            self._failed('run folder', error)
        self._manifest()

    @staticmethod
    def _dumps(value):
        return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')

    def _submit(self, job):
        def guarded():
            try:
                job()
            except Exception as error:  # noqa: BLE001 —— 記録は best effort。書き出しスレッドの例外で計算を止めない
                self._failed('writer', error)

        while len(self._pending) >= _PENDING_LIMIT:
            self._pending.popleft().result()
        self._pending.append(self._writer.submit(guarded))

    def flush(self):
        """積んだ書き出しを全部書き終えるまで待つ。**同じプロセスで実行中の記録を読むときは先に呼ぶ**(`snapshot` が返す
        ファイルは、まだ書かれていないことがある)。"""
        while self._pending:
            self._pending.popleft().result()

    def close(self):
        """書き終えるのを待ってスレッドを閉じる。投げない(`recording` の後始末が元の例外を隠さないように)。"""
        try:
            self.flush()
        finally:
            self._writer.shutdown(wait=True)

    def _failed(self, where, error):
        """記録の書き込みが落ちたことを数え、最初の数回だけ stderr へ出す。

        **ここから記録を書きに行かない**(`_manifest` を呼ぶと、落ちて再入する)。
        """
        with self._lock:
            self.write_failures += 1
            self.manifest['write_failures'] = self.write_failures
            report = self.write_failures <= _FAILURE_REPORT_LIMIT
        if report:
            print(f'[trace] 記録の書き込みに失敗(計算は続ける): {where}: {type(error).__name__}: {error}',
                  file=sys.stderr, flush=True)

    def _manifest(self):
        with self._lock:
            self.manifest.update(elapsed_seconds=time.perf_counter()-self.started,
                                 write_seconds=self.write_seconds, records=self.sequence)
            data = self._dumps(self.manifest)
            try:
                _atomic(self.path/'manifest.json', lambda stream: stream.write(data))
            except OSError as error:
                self._failed('manifest', error)

    def event(self, kind, **values):
        row = dict(kind=kind, elapsed_seconds=time.perf_counter()-self.started,
                   phase=self.stack[-1] if self.stack else None, **values)
        line = json.dumps(row, ensure_ascii=False, allow_nan=False)
        self._submit(lambda: self._write_event(kind, line))

    def _write_event(self, kind, line):
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            with (self.path/'events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(line+'\n')
                stream.flush()
        except OSError as error:
            self._failed(f'event:{kind}', error)
            return
        with self._lock:
            self.manifest['last_event'] = json.loads(line)
            self._manifest()

    def _encode(self, value, seen, arrays):
        """`value` を JSON にできる形へ直し、参照する配列を `arrays` へ `(鍵, パス, 写し)` で足す。書き出しスレッドが書けたと
        確かめた配列で、ファイルも残っているものは写しを取らない(写しは None。書けなかった・消えた配列は次の記録で書き直す)。"""
        if isinstance(value, np.ndarray):
            if value.dtype.hasobject:
                raise TypeError('object配列は診断記録に保存できない')
            array = np.asarray(value, order='C')
            digest = hashlib.sha256(json.dumps([array.dtype.str, array.shape]).encode())
            digest.update(memoryview(array.ravel()).cast('B'))
            key = digest.hexdigest()
            relative = f'arrays/{key}.npz'
            path = self.path/relative
            with self._lock:
                written = key in self._written_arrays
            arrays.append((key, path, None if written and path.exists() else np.array(array, copy=True)))
            return {'$array':relative, 'shape':list(value.shape), 'dtype':value.dtype.str}
        if isinstance(value, np.generic):
            return self._encode(value.item(), seen, arrays)
        if isinstance(value, float) and not math.isfinite(value):
            return {'$float':str(value)}
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(k):self._encode(v, seen, arrays) for k,v in value.items()}
        if isinstance(value, (tuple, list)):
            return [self._encode(v, seen, arrays) for v in value]
        if is_dataclass(value):
            return {'$type':type(value).__name__, **{f.name:self._encode(getattr(value,f.name),seen,arrays)
                    for f in fields(value) if f.name != 'cache'}}
        if type(value).__name__ == 'Model' and hasattr(value, 'ndof'):
            names = ('nelx','nely','nelz','ndof','E','nu','emin','fixed','free','f','active','ke')
            return {'$type':'FEMModel', **{n:self._encode(getattr(value,n),seen,arrays) for n in names}}
        if isinstance(value, BaseException):
            return {'$error':type(value).__name__, 'message':str(value),
                    'details':self._encode(vars(value),seen,arrays)}
        return {'$omitted_type':f'{type(value).__module__}.{type(value).__name__}'}

    def snapshot(self, name, value):
        """値を JSON と配列へ直すところまでをこのスレッドで行い(`TypeError` はここで伝える)、書き出しは積む。
        書けなかったら(`OSError` だけ)数えて、その JSON とイベントは残さない。戻り値は書く予定のファイル名。"""
        started = time.perf_counter()
        self.sequence += 1
        filename = f'{self.sequence:06d}-{name}.json'
        arrays = []
        data = self._dumps(self._encode(value, {}, arrays))
        line = json.dumps(dict(kind='snapshot', elapsed_seconds=time.perf_counter()-self.started,
                               phase=self.stack[-1] if self.stack else None, name=name, file=filename),
                          ensure_ascii=False, allow_nan=False)
        with self._lock:
            self.write_seconds += time.perf_counter()-started
        self._submit(lambda: self._write_snapshot(name, filename, data, arrays, line))
        return filename

    def _write_snapshot(self, name, filename, data, arrays, line):
        started = time.perf_counter()
        try:
            for key, path, array in arrays:
                if array is not None and not path.exists():
                    _atomic(path, lambda stream, array=array: np.savez_compressed(stream, data=array))
            # 写しを取らなかった配列が、積んでから書くまでの間に消えていたら、参照の切れた JSON は公開しない(JSON を書く直前に全部見る)。
            missing = [(key, path) for key, path, _array in arrays if not path.exists()]
            with self._lock:
                self._written_arrays.difference_update(key for key, _path in missing)
                self._written_arrays.update(key for key, path, _array in arrays if (key, path) not in missing)
            if missing:
                raise FileNotFoundError(f'記録の配列が消えた: {missing[0][1].name}')
            _atomic(self.path/filename, lambda stream: stream.write(data))
        except OSError as error:
            with self._lock:
                self.write_seconds += time.perf_counter()-started
            self._failed(f'snapshot:{name}', error)
            return
        with self._lock:
            self.write_seconds += time.perf_counter()-started
        self._write_event('snapshot', line)

    def save(self, name, write, *, digest):
        """記録用のファイルを1つ書き、書いた後に読み返した内容のハッシュを manifest の `digest` へ入れる。

        `snapshot` と同じく best effort。読み返して数えるので、**manifest にハッシュがある = 最後まで書けている**。
        """
        try:
            _atomic(self.path/name, write)
            hashed = hashlib.sha256((self.path/name).read_bytes()).hexdigest()
            with self._lock:
                self.manifest[digest] = hashed
        except OSError as error:
            self._failed(f'save:{name}', error)
            return False
        return True

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
        runtime = {}
        for name, module in list(sys.modules.items()):
            if name.startswith('duocad.') and module is not None:
                runtime[name] = {key:hashlib.sha256(marshal.dumps(inspect.unwrap(value).__code__)).hexdigest()
                                 for key,value in vars(module).items() if inspect.isfunction(value)}
        self.snapshot('environment', dict(python=sys.version, executable=sys.executable,
                      loaded_function_hashes=runtime,
                      platform=platform.platform(), cpu_count=os.cpu_count(), packages=versions,
                      threads={k:os.environ[k] for k in ('OMP_NUM_THREADS','MKL_NUM_THREADS','OPENBLAS_NUM_THREADS') if k in os.environ},
                      fem_device=_fem_device(),
                      git_head=_git('rev-parse','HEAD'), git_diff=_git('diff','--','duocad','pyproject.toml'),
                      git_status=_git('status','--short')))
        def write_sources(stream):
            with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted((ROOT/'duocad').rglob('*.py')):
                    archive.write(path,path.relative_to(ROOT).as_posix())
                for name in ('pyproject.toml','uv.lock'):
                    if (ROOT/name).exists(): archive.write(ROOT/name,name)
        self.save('source.zip',write_sources,digest='source_sha256')

    def document(self, doc, name):
        data = doc.log.to_json().encode('utf-8')
        self.save(f'{name}.json',lambda stream:stream.write(data),digest=name+'_sha256')
        self.snapshot(name+'-state', dict(parameters=doc.parameters, optimize=doc.optimize,
                      bodies=[dict(id=b.id,name=b.name,kind=b.kind) for b in doc.bodies]))

    def finish(self, status, **values):
        self.flush()
        with self._lock:
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
            _write_input_cad(recorder, doc)
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
        recorder.close()


def _write_input_cad(recorder, doc):
    """入力ボディを1つずつ STEP で残す。**書けなかったボディは数えて飛ばす**(形の種類によって OCCT の
    書き出しが投げる例外は `OSError` に限らないので、ボディ単位で全部受ける)。"""
    from .. import kernel
    cad = recorder.path/'cad'
    try:
        cad.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        recorder._failed('cad', error)
        return
    for body in doc.bodies:
        if body.shape is None:
            continue
        target = cad/f'{body.id}.step'
        try:
            if target.resolve().parent != cad.resolve():
                raise ValueError('CAD保存先のボディIDが不正')
            temporary = target.with_suffix('.step.tmp')
            kernel.write_step(body.shape,str(temporary))
            temporary.replace(target)
        except Exception as error:  # noqa: BLE001 - 記録の欠けとして数える
            recorder._failed(f'cad:{body.id}', error)


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


def final_mesh(raw, smooth, faces, grid, *, final_vertices, final_faces):
    recorder = current()
    if recorder is None:
        return
    raw = np.asarray(raw)*grid.spacing + np.asarray(grid.origin)
    smooth = np.asarray(smooth)*grid.spacing + np.asarray(grid.origin)
    recorder.snapshot('pre-boolean-mesh-world',dict(raw_vertices=raw,vertices=smooth,faces=faces,grid=grid))
    smooth, faces = np.asarray(final_vertices), np.asarray(final_faces)
    recorder.snapshot('final-mesh-world',dict(vertices=smooth,faces=faces,grid=grid,
                                            source='mesh boolean (manifold3d)'))
    def write(stream):
        stream.write(b'solid optimized\n')
        for triangle in smooth[np.asarray(faces)]:
            text = 'facet normal 0 0 0\nouter loop\n'
            text += ''.join('vertex '+' '.join(format(float(v),'.17g') for v in point)+'\n' for point in triangle)
            stream.write((text+'endloop\nendfacet\n').encode('ascii'))
        stream.write(b'endsolid optimized\n')
    recorder.save('final.stl',write,digest='final_stl_sha256')


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


class RunArchive:
    """実行フォルダ(`Recorder` の出力)の読み手。検査・分析スクリプトが `events.jsonl` を読む定型はこれ1つにする。

    前提にするのは `events.jsonl` と `manifest.json` だけ —— `final.stl` / `source.zip` / `input.json` /
    `cad/*.step` は書けなかった記録がありうる(manifest の `write_failures` に回数、書けたときだけ
    `final_stl_sha256` / `source_sha256` / `<name>_sha256` が入る)。読みは UTF-8 固定(書き手と同じ。
    Windows の既定 cp932 で読むと壊れる)。**末尾の不完全な行は捨てて `incomplete_event_line` に残す**
    (実行中・強制終了直後の記録も読めるように)。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.manifest = json.loads((self.path / 'manifest.json').read_text(encoding='utf-8'))
        self.events = []
        self.incomplete_event_line = False
        for line in (self.path / 'events.jsonl').read_text(encoding='utf-8').splitlines():
            try:
                self.events.append(json.loads(line))
            except json.JSONDecodeError:
                self.incomplete_event_line = True

    def snapshots(self, name=None, phase=None):
        """`kind == 'snapshot'` の event(名前・段階で絞れる)。"""
        return [e for e in self.events if e['kind'] == 'snapshot'
                and (name is None or e['name'] == name) and (phase is None or e['phase'] == phase)]

    def begins(self, name=None):
        return [e for e in self.events if e['kind'] == 'begin' and (name is None or e['name'] == name)]

    def load(self, event):
        """snapshot の event を `load_snapshot` で読む(配列を復元する)。"""
        return load_snapshot(self.path / event['file'])

    def raw(self, event):
        """snapshot の event の素の JSON(配列は `$array` の参照のまま)。"""
        return json.loads((self.path / event['file']).read_text(encoding='utf-8'))

    def records(self, name, phase=None):
        return [self.load(e) for e in self.snapshots(name, phase)]

    def last(self, name):
        """その名前の最後の snapshot(無ければ None)。"""
        found = self.snapshots(name)
        return None if not found else self.load(found[-1])

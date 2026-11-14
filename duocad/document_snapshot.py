"""文書を開くときに再生を省くための、再生結果のスナップショット(issue #309)。

操作ログが正で、これはそこから作り直せる派生物。文書を開くたびにログを OCCT で計算し直すと、49 操作の文書で 10 秒前後かかる
(docs/PERFORMANCE.md 8章)ので、再生し終えた文書(ボディの形・スケッチ・パラメータ・最適化条件)を `models/.snapshots/` に預ける。

- **鍵**は「文書のファイルのバイト列の SHA-256」と「このパッケージのコードの指紋」。ファイルが1バイトでも違う(別の編集・別の版の保存)、
  コードが変わった(形状演算の結果が変わりうる)、OCP・Python の版が違うスナップショットは使わず、通常の再生へ落とす。
- 形は BinTools(面の添字の数え方が保たれる。三角形分割は載せない)、それ以外は pickle。pickle の中の `TopoDS_Shape` と `Mesh` は
  別の塊へ出す(`persistent_id`)。読む側は `duocad` の自前のクラスだけを組み立てる(`_Reader.find_class`)。
- 表示・非表示は保存の対象外(`Body.visible` の docstring)なので、載せるときに表示へ戻す。
- **載せないもの**: undo 用の「操作前の形」(`verbs/_common.SNAPSHOTS`)。最初に必要になった時点でログを再生して作り直す
  (`verbs/_common.HYDRATORS`、`Session._register_history_hydrator`)。
"""
from __future__ import annotations

import functools
import hashlib
import io
import json
import os
import pickle
import struct
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import kernel_brep
from .document import Body, Mesh
from .parameter_table import ParameterTable

FORMAT_VERSION = 1
_MAGIC = b"DUOCADSNAP\n"

#: 再生にこれ以上かかる文書にだけスナップショットを作る(小さな文書は再生が速く、毎回の書き出しが無駄になる)。[秒]
#: 実測の目安: 押し出し2回の文書で約2秒、押し出し4・フィレット4・面取り1の文書で約10秒、数十 ms の文書は不要。
#: 環境変数 `DUOCAD_SNAPSHOT_MIN_BUILD_SECONDS` で変えられる(先読みの子プロセスにも継がれる。テストが 0 にする)。
MIN_BUILD_SECONDS = float(os.environ.get("DUOCAD_SNAPSHOT_MIN_BUILD_SECONDS", "0.5"))

_SHAPE = "s"
_MESH = "m"


class SnapshotError(Exception):
    """スナップショットを使えない(壊れている・鍵が合わない・版が違う)。呼び手は通常の再生へ落とす。"""


class SnapshotLight(SnapshotError):
    """鍵は合うが、中身の無い「再生が速い」の印(`light_marker`)。再生で開くのが正しく、捨てない。"""


@functools.lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """このパッケージのコード・OCP・Python の版の指紋。どれかが変わればスナップショットは使わない(形状演算の結果が変わりうる)。"""
    try:
        ocp_version = kernel_brep.ocp_version()
    except Exception:  # noqa: BLE001 - 版が取れなくても鍵は作る
        ocp_version = "?"
    digest = hashlib.sha256()
    digest.update(f"{FORMAT_VERSION}|{sys.version_info[:3]}|{ocp_version}".encode())
    root = Path(__file__).resolve().parent
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def file_digest(data: bytes) -> str:
    """文書のファイルのバイト列の鍵。"""
    return hashlib.sha256(data).hexdigest()


class Memo:
    """塊(形・メッシュ)の直列化の使い回し。**同じ物のままなら書き直さない**(操作のたびに全ボディの BinTools を書かない)。

    鍵は物の id で、物そのものを持って `is` で照合する(id の使い回しで別の物に当たらない)。
    """

    def __init__(self) -> None:
        self._entries: Dict[int, tuple] = {}

    def get(self, obj: Any, make: Any) -> bytes:
        hit = self._entries.get(id(obj))
        if hit is not None and hit[0] is obj:
            return hit[1]
        data = make(obj)
        self._entries[id(obj)] = (obj, data)
        return data

    def keep_only(self, objs: List[Any]) -> None:
        """今の文書にある物だけを残す(消えたボディの形を持ち続けない)。"""
        live = {id(obj) for obj in objs}
        for key in [key for key in self._entries if key not in live]:
            del self._entries[key]


class _Writer(pickle.Pickler):
    def __init__(self, stream: io.BytesIO, memo: Memo) -> None:
        super().__init__(stream, protocol=pickle.HIGHEST_PROTOCOL)
        self._memo = memo
        self.blobs: List[tuple] = []  # (kind, bytes)
        self.objects: List[Any] = []
        self._index: Dict[int, int] = {}

    def persistent_id(self, obj: Any) -> Any:
        if kernel_brep.is_shape(obj):
            kind, make = _SHAPE, kernel_brep.shape_to_bytes
        elif isinstance(obj, Mesh):
            kind, make = _MESH, lambda mesh: pickle.dumps(mesh, protocol=pickle.HIGHEST_PROTOCOL)
        else:
            return None
        number = self._index.get(id(obj))
        if number is None:
            number = len(self.blobs)
            self._index[id(obj)] = number
            self.blobs.append((kind, self._memo.get(obj, make)))
            self.objects.append(obj)
        return (kind, number)


class _Reader(pickle.Unpickler):
    def __init__(self, stream: io.BytesIO, blobs: List[Any]) -> None:
        super().__init__(stream)
        self._blobs = blobs

    def persistent_load(self, pid: Any) -> Any:
        kind, number = pid
        return self._blobs[number]

    def find_class(self, module: str, name: str) -> Any:
        """`duocad` の自前のクラスと、組み込みの入れ物だけを組み立てる(`models/` に置かれたファイルから任意のコードを走らせない)。"""
        if module == "builtins" and name in {"set", "frozenset", "tuple", "list", "dict", "complex", "slice", "range"}:
            return super().find_class(module, name)
        if module == "duocad" or module.startswith("duocad."):
            found = super().find_class(module, name)
            if isinstance(found, type) and found.__module__ == module:
                return found
        raise pickle.UnpicklingError(f"スナップショットに許していない型: {module}.{name}")


@dataclass
class Captured:
    """ファイルへ書く前の写し(主スレッドで作り、書き手スレッドで `pack` する)。"""

    header: dict
    state: bytes
    blobs: List[tuple]

    def pack(self, file_sha256: str) -> bytes:
        header = dict(self.header, file_sha256=file_sha256, state=len(self.state), blobs=[[kind, len(data)] for kind, data in self.blobs])
        head = json.dumps(header, ensure_ascii=False).encode("utf-8")
        parts = [_MAGIC, struct.pack("<I", len(head)), head, self.state]
        parts.extend(data for _, data in self.blobs)
        return b"".join(parts)


@dataclass
class Restored:
    bodies: List[Body]
    sketches: list
    parameters: ParameterTable
    optimize: dict
    numbering: Dict[str, int]
    build_seconds: float
    operation_count: int


def light_marker() -> Captured:
    """「この鍵の文書は再生が速く、スナップショットは要らない」の印(先読みが毎回調べ直さないため。`snapshot_prebuild`)。"""
    return Captured(header={"format": FORMAT_VERSION, "code": code_fingerprint(), "light": True}, state=b"", blobs=[])


def header_status(data: bytes, file_sha256: str) -> str:
    """鍵の合うスナップショットか(読み込まずに先頭だけ見る)。"ready"(使える)・"light"(速いの印)・"missing"(無い・合わない・壊れている)。"""
    try:
        if not data.startswith(_MAGIC):
            return "missing"
        offset = len(_MAGIC)
        (head_length,) = struct.unpack_from("<I", data, offset)
        header = json.loads(data[offset + 4 : offset + 4 + head_length].decode("utf-8"))
        if header.get("format") != FORMAT_VERSION or header.get("code") != code_fingerprint() or header.get("file_sha256") != file_sha256:
            return "missing"
        return "light" if header.get("light") else "ready"
    except Exception:  # noqa: BLE001 - 壊れたものは無いのと同じ
        return "missing"


def capture(document: Any, *, memo: Memo, build_seconds: float) -> Captured:
    """今の文書の状態を写す(主スレッドで呼ぶ。書き換えと競合しないよう、読むだけで速く済む形にしてある)。

    例外は呼び手が握りつぶしてよい(スナップショットは無くても文書は開ける)。
    """
    bodies = [replace(body, visible=True) for body in document.bodies]
    sketches = [replace(sketch, visible=True) for sketch in document.sketches]
    state = {
        "bodies": bodies,
        "sketches": sketches,
        "parameters": dict(document.parameters),
        "optimize": dict(vars(document.optimize)),
    }
    stream = io.BytesIO()
    writer = _Writer(stream, memo)
    writer.dump(state)
    memo.keep_only(writer.objects)
    header = {
        "format": FORMAT_VERSION,
        "code": code_fingerprint(),
        "build_seconds": float(build_seconds),
        "operations": len(document.log.entries),
        "numbering": document.log.numbering(),
    }
    return Captured(header=header, state=stream.getvalue(), blobs=writer.blobs)


def restore(data: bytes, *, file_sha256: str, operation_count: int) -> Restored:
    """`Captured.pack` の出力から文書の状態を作る。使えなければ `SnapshotError`(壊れている・鍵が合わない・版が違う)。"""
    try:
        return _restore(data, file_sha256=file_sha256, operation_count=operation_count)
    except SnapshotError:
        raise
    except Exception as error:  # noqa: BLE001 - 壊れ方は問わず「使えない」に畳む
        raise SnapshotError(f"スナップショットを読めない: {type(error).__name__}: {error}") from error


def _restore(data: bytes, *, file_sha256: str, operation_count: int) -> Restored:
    if not data.startswith(_MAGIC):
        raise SnapshotError("スナップショットの形式ではない")
    offset = len(_MAGIC)
    (head_length,) = struct.unpack_from("<I", data, offset)
    offset += 4
    header = json.loads(data[offset : offset + head_length].decode("utf-8"))
    offset += head_length
    if header.get("format") != FORMAT_VERSION:
        raise SnapshotError("スナップショットの版が違う")
    if header.get("code") != code_fingerprint():
        raise SnapshotError("スナップショットを作ったコード・OCP の版が今と違う")
    if header.get("file_sha256") != file_sha256:
        raise SnapshotError("スナップショットを作った後に文書のファイルが変わった")
    if header.get("light"):
        raise SnapshotLight("再生が速い文書の印")
    if header.get("operations") != operation_count:
        raise SnapshotError("スナップショットの操作数が文書と合わない")
    state_end = offset + int(header["state"])
    state_bytes = data[offset:state_end]
    offset = state_end
    blobs: List[Any] = []
    for kind, length in header["blobs"]:
        chunk = data[offset : offset + int(length)]
        if len(chunk) != int(length):
            raise SnapshotError("スナップショットが途中で切れている")
        offset += int(length)
        blobs.append(kernel_brep.shape_from_bytes(chunk) if kind == _SHAPE else _Reader(io.BytesIO(chunk), []).load())
    if offset != len(data):
        raise SnapshotError("スナップショットの長さが合わない")
    state = _Reader(io.BytesIO(state_bytes), blobs).load()
    parameters = ParameterTable(state["parameters"])
    return Restored(
        bodies=list(state["bodies"]),
        sketches=list(state["sketches"]),
        parameters=parameters,
        optimize=dict(state["optimize"]),
        numbering={str(kind): int(number) for kind, number in (header.get("numbering") or {}).items()},
        build_seconds=float(header.get("build_seconds", 0.0)),
        operation_count=int(header["operations"]),
    )

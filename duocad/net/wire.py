"""状態とプレビューの配信の形: 1枚のバイナリフレームと、接続が既に持っているものを省く規則(issue #166・#170)。

JSON テキストのままだと PC ケース(27 部品)の初回配信が 6.1 MB で、サーバの `json.dumps` 72〜77 ms・画面の
`JSON.parse` と数の写しが受信のたびにかかり、カメラを1回動かすたびにも 4.5 MB 流れていた。

**フレーム**(`encode` / `decode`)。三角形(`positions` と `indices` を両方持つ辞書の2つの数の並び)だけを生のバイトにし、
残りは JSON のまま運ぶ:

    [u32 LE: ヘッダの長さ H][ヘッダ: UTF-8 の JSON。末尾を空白で埋め、H を 4 の倍数にする][配列の生バイト...]

ヘッダは配りものそのもので、数の並びを `{"$f32": [バイト位置, 個数]}` / `{"$u32": [バイト位置, 個数]}` に置き換える
(バイト位置は配列の生バイトの先頭から)。`positions` は float32(画面がこれまで `Float32Array` に写していたのと同じ丸め
なので、STL の書き出しとのビット単位の一致は変わらない)、`indices` は uint32。バイト順はリトルエンディアン。

**省く規則**(`Delivered`)。接続ごとに、送り届けたものを覚えて次の配信から省く:

- 面: 版の変わったボディでも、前回送った面と同じもの(`serial` が同じ。`view._FaceTessellations` が測るたびに振る)は
  `{"index", "serial", "detail"}` だけにする。カメラを動かして測り方の変わる面は一部なので、残りを送らずに済む
- 操作ログ: 前回と同じなら `state` から `log` を落とす(長方形 150 個のスケッチで領域を1つ選ぶたびに 33 KB 流れていた)。
  変わったときも前回と同じ先頭は省き、`log_keep`(前回の先頭から残す件数)と `log_tail`(その後ろに足す分)だけ送る
  (操作は末尾に足すか、undo で末尾を削るので、ふつうは数件)
- スケッチ: 前回と同じなら `{"sketch_id", "unchanged": true}` だけにする(同 78 KB)

**層をまたぐ契約**: 読む側は `web/src/wire.ts`(画面。省かれたものを前回の写しで埋め戻す)と `tests/session_support.py`
(テストの接続)。省くのは「この接続へ送り届けたと記録したもの」だけで、記録は送信が成功してから(`Server._push`)。
接続が切れたら両側とも忘れる。
"""
from __future__ import annotations

import json
import struct
import sys
from array import array
from typing import Any, Dict, List, Optional

#: 1枚のバイナリフレームで送る上りの種類(他は JSON テキストのまま)。
BINARY_TYPES = frozenset({"state", "preview"})

if array("I").itemsize != 4 or array("f").itemsize != 4:  # pragma: no cover - 主要な環境ではどれも 4 バイト
    raise ImportError("array('I') / array('f') が 4 バイトでない環境では三角形を uint32 / float32 で詰められない")


def encode(payload: dict) -> bytes:
    """配りものを1枚のフレームにする(形はモジュールの docstring)。`payload` は書き換えない。"""
    chunks: List[bytes] = []
    size = 0

    def pack(values: list, code: str, marker: str) -> dict:
        nonlocal size
        packed = array(code, values)
        if sys.byteorder != "little":
            packed.byteswap()
        data = packed.tobytes()
        chunks.append(data)
        at, size = size, size + len(data)
        return {marker: [at, len(packed)]}

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            if isinstance(value.get("positions"), list) and isinstance(value.get("indices"), list):
                return {
                    key: pack(item, "f", "$f32") if key == "positions"
                    else pack(item, "I", "$u32") if key == "indices"
                    else walk(item)
                    for key, item in value.items()
                }
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)) and value and isinstance(value[0], (dict, list, tuple)):
            return [walk(item) for item in value]
        return value

    header = json.dumps(walk(payload), ensure_ascii=False).encode("utf-8")
    header += b" " * (-len(header) % 4)
    return b"".join([struct.pack("<I", len(header)), header, *chunks])


def decode(frame: bytes) -> dict:
    """`encode` の逆(テストの接続と計測が使う)。数の並びは Python の list に戻す(float32 に丸めた値)。"""
    (length,) = struct.unpack_from("<I", frame, 0)
    base = 4 + length

    def revive(value: dict) -> Any:
        if len(value) == 1:
            for marker, code in (("$f32", "f"), ("$u32", "I")):
                if marker in value:
                    at, count = value[marker]
                    values = array(code)
                    values.frombytes(frame[base + at:base + at + count * 4])
                    if sys.byteorder != "little":
                        values.byteswap()
                    return values.tolist()
        return value

    return json.loads(frame[4:base].decode("utf-8"), object_hook=revive)


def _common_prefix(before: list, after: list) -> int:
    """2つの操作ログの、先頭から同じ件数。"""
    for at, (old, new) in enumerate(zip(before, after)):
        if old != new:
            return at
    return min(len(before), len(after))


class Delivered:
    """1つの接続へ送り届けたもの(`Server._push` が接続ごとに持つ)。次の配信で、同じものを省く。"""

    def __init__(self) -> None:
        #: body_id -> 版(`Session.delta` の基準)
        self.revisions: Dict[str, int] = {}
        #: body_id -> {面の添字: serial}(前回送ったそのボディの面。serial の無い面 = メッシュボディの面は入れない)
        self.faces: Dict[str, Dict[int, int]] = {}
        self.log: Optional[list] = None
        #: sketch_id -> 前回送ったそのスケッチの配りもの
        self.sketches: Dict[str, dict] = {}

    def compose(self, meta: dict, bodies: list) -> dict:
        """`state` メッセージの中身(`{"state", "delta"}`)。送り届けたものと同じ部分を省く。`meta` / `bodies` は書き換えない。"""
        state = dict(meta)
        if self.log is not None:
            keep = _common_prefix(self.log, meta["log"])
            del state["log"]
            if keep < len(self.log) or keep < len(meta["log"]):
                state["log_keep"] = keep
                state["log_tail"] = meta["log"][keep:]
        state["sketches"] = [
            {"sketch_id": sketch["sketch_id"], "unchanged": True}
            if self.sketches.get(sketch["sketch_id"]) == sketch else sketch
            for sketch in meta["sketches"]
        ]
        return {"state": state, "delta": {"bodies": [self._omit_known_faces(body) for body in bodies]}}

    def _omit_known_faces(self, body: dict) -> dict:
        geometry = body.get("geometry")
        known = self.faces.get(body["body_id"])
        if not geometry or not known:
            return body
        faces = [
            {"index": face["index"], "serial": face["serial"], "detail": face.get("detail")}
            if face.get("serial") is not None and known.get(face["index"]) == face["serial"] else face
            for face in geometry["faces"]
        ]
        return dict(body, geometry=dict(geometry, faces=faces))

    def record(self, meta: dict, bodies: list) -> None:
        """`compose` で作った配りものを送り終えた(**送信が成功してから**呼ぶ)。`bodies` は省く前のもの。"""
        self.revisions = {body["body_id"]: body["revision"] for body in meta["bodies"]}
        for body in bodies:
            geometry = body.get("geometry")
            self.faces[body["body_id"]] = {
                face["index"]: face["serial"] for face in geometry["faces"] if face.get("serial") is not None
            } if geometry else {}
        for body_id in [key for key in self.faces if key not in self.revisions]:
            del self.faces[body_id]
        self.log = meta["log"]
        self.sketches = {sketch["sketch_id"]: sketch for sketch in meta["sketches"]}

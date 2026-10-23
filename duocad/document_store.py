"""文書ファイルと付随するサムネイル、書き出し先の検査。文書の編集中状態や保存キューは持たない。"""
from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import time
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Optional

from .document_name import check_characters, check_writable_length
from .oplog import OpLog

#: Windowsの予約デバイス名(拡張子の有無に関わらず特別扱い。大文字小文字を区別しない)。
_RESERVED_WINDOWS_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

THUMBNAIL_DIR = ".thumbnails"
#: 受け取るPNGの上限[バイト]。画面が撮るのは128px四方で、RGBAを圧縮せず持っても64KiB。
THUMBNAIL_MAX_BYTES = 256 * 1024
_PNG_SIGNATURE = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])


def _mtime_or_none(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class DocumentStore:
    """保存先を所有し、文書ファイルの操作を行う。書き込み順序と排他は呼び手が調整する。"""

    def __init__(self, models_dir: Path, exports_dir: Path, warnings: list[str] | None = None) -> None:
        self.models_dir = Path(models_dir).resolve()
        self.exports_dir = Path(exports_dir).resolve()
        self.warnings = warnings if warnings is not None else []

    def ensure_models_dir(self) -> None:
        self.models_dir.mkdir(parents=True, exist_ok=True)

    def read_document_file(self, source: Path) -> dict:
        if not source.exists():
            raise FileNotFoundError(f"開けるファイルが無い: {source}")
        return json.loads(source.read_text(encoding="utf-8"))

    def rename_document_file(self, source: Path, target: Path, log: OpLog, created: float | None) -> None:
        """検査済みのパス間で改名する。元ファイルが外で消されていればログから作り直す。"""
        if source.exists():
            os.replace(source, target)
        else:
            target.write_text(log.to_json(created=created), encoding="utf-8")
        self.drop_thumbnail(target)
        self.carry_thumbnail(source, target, move=True)

    def delete_document_file(self, target: Path) -> None:
        """検査済みの文書ファイルを消す。付随ファイルの後始末は呼び手が調整する。"""
        if not target.exists():
            raise FileNotFoundError(f"削除するファイルが無い: {target.name}")
        target.unlink()

    def resolve_document_name(self, name: Any) -> Path:
        """ファイル名だけを受け取り、`models_dir` 配下に閉じたパスへ解決する(`models_dir` が無ければ作る)。

        保存先が起動場所で変わらないよう起点を `models_dir` に固定してある。文字列だけの拒否では脱出経路が残る(いずれも実測):

        - `Path("D:outside.json").is_absolute()` は False(Windowsのドライブ相対パス)。しかも `models_dir / "D:outside.json"` は
          pathlib が「ドライブ付きセグメント」を新しいアンカーとして扱い、`models_dir` を捨てて `D:\\outside.json` に解決される。
          コロン1文字が入口で、NTFSの代替データストリーム(`a.json:x`)も同じ。
        - 予約デバイス名(`CON`/`COM1`等、拡張子を付けても特別扱い)・末尾のドット/空白(Windowsが黙って落とすため
          `foo.json.` と `foo.json` が同じ実体になりうる)。
        - 既存のシンボリックリンク/ジャンクションを指すファイル名は、文字列検査では `models_dir` の外へ解決されるのを塞げない。
        - 制御文字・`<>"|?*`・孤立サロゲートは `document_name.check_characters` が断る(通すと一時ファイルを開く段で、
          絶対パス入りの OSError になる)。書き込む名前の長さは `resolve_writable_name`。

        判定は2段: ①明らかに拒まれる形を早く言う文字列検査、②解決後のパスが `models_dir` 配下か(`is_relative_to`。
        `resolve()` はリンクを辿る)。①だけに頼らない。
        """
        text = str(name)
        stripped = text.strip()
        if stripped == "":
            raise ValueError("ファイル名が空")
        if text != stripped:
            raise ValueError(f"前後の空白は不可: {text!r}")
        if "/" in text or "\\" in text:
            raise ValueError(f"ファイル名だけを受け取る(区切り文字は不可): {text!r}")
        if ":" in text:
            # ドライブ指定("D:outside.json")もNTFS代替データストリーム("a.json:x")も
            # コロン1文字が入口(上のdocstring参照)。
            raise ValueError(f"ファイル名だけを受け取る(':'は不可): {text!r}")
        if text in (".", ".."):
            raise ValueError(f"ファイル名として使えない: {text!r}")
        if text.endswith(".") or text.endswith(" "):
            raise ValueError(f"末尾のドット/空白は不可(Windowsが黙って落とすため): {text!r}")
        base_name = text.split(".", 1)[0].rstrip(" ").upper()
        if base_name in _RESERVED_WINDOWS_NAMES:
            raise ValueError(f"予約されたデバイス名は使えない: {text!r}")
        if not text.lower().endswith(".json"):
            # 一覧(`list_documents`)は `*.json` しか見ない。拡張子違いを受理すると、保存はできるのに一覧から二度と開けないファイルができる。
            # 黙って `.json` を補うより、打ち間違いをその場で返す。
            raise ValueError(f"拡張子は.json(一覧はモデルの*.jsonだけを見る): {text!r}")
        check_characters(text)
        candidate = Path(text)
        if candidate.is_absolute():
            raise ValueError(f"ファイル名だけを受け取る(絶対パスは不可): {text!r}")
        self.models_dir.mkdir(parents=True, exist_ok=True)
        root = self.models_dir.resolve()
        resolved = (root / text).resolve()
        # 正の判定はここ(シンボリックリンク越しの脱出も含めて最終的にここで止まる)。
        if not resolved.is_relative_to(root):
            raise ValueError(f"models/の外を指している: {text!r} -> {resolved}")
        return resolved

    def resolve_writable_name(self, name: Any) -> Path:
        """`resolve_document_name` に、書き込む名前だけへ掛ける長さの検査を足したもの(保存・改名・複製先・自動保存)。
        開く・消す・サムネイルは今ある名前を扱うので掛けない(長い名前のファイルを開けなくしない)。
        """
        target = self.resolve_document_name(name)
        check_writable_length(target.name)
        return target

    @staticmethod
    def write_document_file(target: Path, payload: Any) -> None:
        """一時ファイルへ書いてから差し替える(呼び手が書き込みの錠を持つ)。素の上書きは既存の中身を先に切り詰めるので、
        途中で落ちると唯一の永続形式である前回の保存が半端なJSONで潰れる。`os.replace` は同じディレクトリ内なら不可分。
        `payload` は文字列か、`OpLog.to_file_chunks` の断片の並び(同じ中身。文字列の改行は書き込みで `os.linesep` になる)。
        """
        staging = target.with_name(target.name + ".writing")
        if isinstance(payload, list):
            with open(staging, "wb") as stream:
                stream.writelines(payload)
        else:
            staging.write_text(payload, encoding="utf-8")
        os.replace(staging, target)

    def unused_untitled_name(self) -> str:
        """`untitled-1.json` から順に、`models_dir` にまだ無い最初の名前。"""
        self.models_dir.mkdir(parents=True, exist_ok=True)
        number = 1
        while (self.models_dir / f"untitled-{number}.json").exists():
            number += 1
        return f"untitled-{number}.json"

    def list_documents(self, *, read_lock: AbstractContextManager | None = None) -> dict:
        """`models_dir` の中身を一覧にする(名前/更新日時/作成日時/サムネイルの版)。verbではない。

        読めるファイルだけを載せる判定は `OpLog.from_payload` のパースだけで行う(形状の再生はしない)。
        実測でファイルあたり約30ms(4.8MBのPCケースを3件で0.09s)。壊れたファイル(手で触られた・書き込み途中の `.writing` 等)は
        省く。旧形式(version 1)のファイルも `from_payload` が ValueError にするため黙って省かれる(次に版を上げるときは
        「一覧に出ないだけで理由が分からない」になる)。
        `created` は入れ物の作成日時(無いファイルは None)。`thumb` はサムネイルの更新日時で、画像そのものは載せない
        (一覧は自動保存のたびに取り直されるので、載せると全件の画像を毎回送る)。画像は `get_thumbnail` で1件ずつ取る。
        ファイルは1件ずつ呼び手から受け取る錠を持って読む: 編集中のファイルを書き手スレッド(や実行器スレッドの自動保存)が
        `os.replace` している間に開くと、Windows では書く側が `PermissionError` になり、自動保存が失敗する。
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for path in sorted(self.models_dir.glob("*.json")):
            try:
                with read_lock if read_lock is not None else nullcontext():
                    mtime = path.stat().st_mtime
                    payload = json.loads(path.read_text(encoding="utf-8"))
                OpLog.from_payload(payload)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            files.append({
                "name": path.name,
                "mtime": mtime,
                "created": OpLog.created_of(payload),
                "thumb": _mtime_or_none(self._thumbnail_path(path)),
            })
        return {"action": "list", "path": str(self.models_dir), "files": files}

    def copy_document_file(self, origin: Path, target: Path) -> dict:
        """検査済みのパス間で複製する。既存の複製先は拒否し、作成日時は今にする。
        サムネイルは更新日時ごと写す(画面は版で取り直しを決める)。
        """
        if not origin.exists():
            raise FileNotFoundError(f"複製元が無い: {origin.name}")
        if target.exists():
            raise ValueError(f"{target.name} は既にある(別の名前にする)")
        log = OpLog.from_json(origin.read_text(encoding="utf-8"))
        self.write_document_file(target, log.to_json(created=time.time()))
        self.drop_thumbnail(target)
        self.carry_thumbnail(origin, target, move=False)
        return {
            "action": "copy",
            "path": str(target),
            "name": target.name,
            "source": origin.name,
        }

    # ---------------------------------------------------------------- サムネイル
    # 一覧の絵は画面(three.js)が撮って預ける(`web/src/thumbnail.ts`)。置き場は `models/.thumbnails/<文書のファイル名>.png` の付随ファイルで、
    # 文書のJSONには入れない(描き直せる写し)。だから付随ファイルの失敗は元の操作を失敗にしない。
    def _thumbnail_path(self, document: Path) -> Path:
        """文書のファイル(`models_dir` 直下)に付くサムネイルのパス。"""
        return document.parent / THUMBNAIL_DIR / f"{document.name}.png"

    def drop_thumbnail(self, document: Path) -> None:
        try:
            self._thumbnail_path(document).unlink(missing_ok=True)
        except OSError as error:
            self.warnings.append(f"サムネイルを消せなかった: {error}")

    def carry_thumbnail(self, source: Path, target: Path, *, move: bool) -> None:
        """改名(`move`)・複製でサムネイルも付いていかせる。複製は更新日時ごと写す(画面は版で取り直しを決める)。"""
        origin = self._thumbnail_path(source)
        if not origin.exists():
            return
        try:
            if move:
                os.replace(origin, self._thumbnail_path(target))
            else:
                shutil.copy2(origin, self._thumbnail_path(target))
        except OSError as error:
            self.warnings.append(f"サムネイルを{'移せ' if move else '写せ'}なかった: {error}")

    def save_thumbnail(self, name: Any, png: Any) -> dict:
        """画面が撮ったPNG(base64)を `name` のサムネイルとして書く。verbではない。
        `png` が空文字なら消す(見えている立体が1つも無い文書)。文書のファイルが無い・書けない(OSError)ときも断らず
        `saved: False` を返す(撮ってから送るまでに改名が挟まりうるし、Windows では別のプログラムが開いているだけで
        `os.replace` が落ちる)。断るのは中身が壊れているとき(画面側の不具合)だけ。
        """
        target = self.resolve_document_name(name)
        thumbnail = self._thumbnail_path(target)
        reply = {"action": "save_thumbnail", "path": str(thumbnail), "name": target.name}
        text = str(png)
        if text == "":
            try:
                thumbnail.unlink(missing_ok=True)
            except OSError as error:
                self.warnings.append(f"サムネイルを消せなかった: {error}")
                return {**reply, "saved": False, "thumb": None}
            return {**reply, "saved": True, "thumb": None}
        try:
            data = base64.b64decode(text, validate=True)
        except binascii.Error as error:
            raise ValueError(f"サムネイルがbase64ではない: {error}") from None
        if not data.startswith(_PNG_SIGNATURE):
            raise ValueError("サムネイルがPNGではない")
        if len(data) > THUMBNAIL_MAX_BYTES:
            raise ValueError(f"サムネイルが大きすぎる({len(data)} > {THUMBNAIL_MAX_BYTES}バイト)")
        if not target.exists():
            return {**reply, "saved": False, "thumb": None}
        try:
            thumbnail.parent.mkdir(exist_ok=True)
            staging = thumbnail.with_name(thumbnail.name + ".writing")
            staging.write_bytes(data)
            os.replace(staging, thumbnail)
            thumb = thumbnail.stat().st_mtime
        except OSError as error:
            self.warnings.append(f"サムネイルを書けなかった: {error}")
            return {**reply, "saved": False, "thumb": None}
        return {**reply, "saved": True, "thumb": thumb}

    def get_thumbnail(self, name: Any) -> dict:
        """`name` のサムネイルをbase64で返す(無い・読めなければ `png: None`)。**verbではない**。"""
        target = self.resolve_document_name(name)
        thumbnail = self._thumbnail_path(target)
        reply = {"action": "thumbnail", "path": str(thumbnail), "name": target.name}
        try:
            thumb = thumbnail.stat().st_mtime
            data = thumbnail.read_bytes()
        except OSError:
            return {**reply, "png": None, "thumb": None}
        return {**reply, "png": base64.b64encode(data).decode("ascii"), "thumb": thumb}

    def resolve_export_path(self, path: Any, suffixes: tuple[str, ...]) -> Path:
        """書き出し先を絶対パスにする。相対は `exports_dir` 基準(その外へ逃げる経路は拒否)。絶対パスはそのまま(テスト・MCPの明示パス用)。

        絶対パスは自由だが、書き出しは既存の中身を黙って潰すので、**形式の拡張子(`suffixes`)以外は断り、保存済みの文書
        (`models_dir` の `*.json`)を指す先も断る**。拡張子は解決後のパスで見る(リンク越しに `*.json` を指す名前も止まる)。
        画面の書き出しは必ず拡張子を付けて送るので、人の操作には当たらない。
        """
        text = str(path)
        stripped = text.strip()
        if stripped == "":
            raise ValueError("書き出し先が空")
        if text != stripped:
            raise ValueError(f"前後の空白は不可: {text!r}")
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            target = candidate.resolve()
            if not target.parent.exists():
                raise ValueError(f"書き出し先のディレクトリが無い: {target.parent}")
        else:
            self.exports_dir.mkdir(parents=True, exist_ok=True)
            root = self.exports_dir.resolve()
            target = (root / text).resolve()
            if not target.is_relative_to(root):
                raise ValueError(f"export/の外を指している: {text!r} -> {target}")
        check_characters(target.name)
        if target.suffix.lower() not in suffixes:
            raise ValueError(
                f"書き出し先の拡張子は {' か '.join(suffixes)} にする(保存済みの文書などを潰さないため): {text!r}"
            )
        self._refuse_overwriting_a_document(target)
        if not candidate.is_absolute():
            target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _refuse_overwriting_a_document(self, target: Path) -> None:
        """`target` が保存済みの文書(`models_dir` の `*.json`)と同じファイルなら断る。拡張子の検査をすり抜ける
        ハードリンクでも、書けば文書の中身を書き換えてしまう。"""
        if not target.exists():
            return
        for document in self.models_dir.glob("*.json"):
            try:
                same = target.samefile(document)
            except OSError:
                # 照合できないのはその文書(壊れたジャンクション・消えかけのファイル)だけ。検査全体を打ち切ると、
                # 後ろに並ぶ本物の文書へのハードリンクを素通しする。
                continue
            if same:
                raise ValueError(f"保存済みの文書 {document.name} は書き出しで上書きできない: {str(target)!r}")


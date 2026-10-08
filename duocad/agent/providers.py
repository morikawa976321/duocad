"""LLMプロバイダのアダプタ —— **キーの詰め替えだけを担当する層**。

- `openai_compat`  … llama-server などのOpenAI互換(`POST /v1/chat/completions`)
- `anthropic`      … Claude API(`POST /v1/messages`)。APIキーで課金される

**中立の往復型がここが層の境目**: ループは `UserTurn` / `AssistantTurn` / `ToolResultTurn` の
3つだけを積み、**プロバイダ固有の本文の形はアダプタの中の話**である。逆にアダプタは
`DocumentTools` も `Session` も知らない —— 受け取るのは `descriptors()` が返したツール定義
そのままである(定義を書き写す場所を作らない)。

**APIキーの取り回し(セキュリティ要件)**:

- 読み口は `load_api_key()` の1つだけ(環境変数 → gitignore済みのローカル設定ファイル)。
  `build_provider` はキーを `anthropic` にだけ渡し、`openai_compat` には渡さない。
- 画面(設定ダイアログ)からは**書くだけ**(`save_api_key` / `delete_api_key` がファイルへ書く)。
  画面へ返すのは `api_key_status()` の3つ(置いてあるか・どこから読んだか・末尾4文字)だけ。
- `anthropic` の送り先は**固定**(`build_provider` は `base_url` を渡さない)。送り先を差し替えられると、
  `x-api-key` に載った実キーがそのホストへ届く。
- **値はどこにも出さない** —— `repr()` にも例外にもログにも。外へ出る文字列は `_redact()` を通す。

公式SDK(`openai` / `anthropic`)は入れず、標準ライブラリで素のHTTPを書く。応答は**ストリーミングで
受けて組み立てる**(`StreamingTransport`。理由は表示ではなく打ち切り)。

**OCP / build123d をimportしない**。
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Iterator, Optional, Sequence

#: llama-server の既定(`llama-server -c 16384 --port 8080` を想定)。
DEFAULT_OPENAI_COMPAT_BASE_URL = "http://127.0.0.1:8080/v1"

#: 既定のClaudeモデル。**日付サフィックスを付けない**(この綴りで完結している)。
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"
#: 既定モデルを差し替える環境変数(画面からは選ばせていない)。
ANTHROPIC_MODEL_ENV = "DUOCAD_ANTHROPIC_MODEL"
#: Claude APIの版ヘッダ(`anthropic-version`)。
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
#: Claudeへの1リクエストの出力上限(思考+本文)。**自分で置いた値**(実測ではない)。
#: `loop.DEFAULT_MAX_TOKENS`(1024)は llama-server の `--reasoning-budget` が思考を切る前提の値で、
#: Claudeには思考をトークン数で切る口が無い(深さは `effort` でしか動かない)。1024のままだと
#: 思考で上限を使い切り、本文もツール呼び出しも無いまま `max_tokens` で返る。
ANTHROPIC_MAX_TOKENS = 8192

#: APIキーを読む環境変数。**`ANTHROPIC_API_KEY` にしない** —— Claude Code はその名前の環境変数を
#: 見つけるとサブスクの代わりにそのキーで従量課金する(非対話の `claude -p` では確認なしに)。
#: duocadのためにユーザー環境へ置くと、同じマシンの Claude Code まで課金先が変わる。
ANTHROPIC_API_KEY_ENV = "DUOCAD_ANTHROPIC_API_KEY"
#: リポジトリ直下のローカル設定ファイル(**gitignore済み**)。形は `{"DUOCAD_ANTHROPIC_API_KEY": "..."}`。
SECRETS_FILENAME = "duocad_secrets.json"
SECRETS_PATH = Path(__file__).resolve().parents[2] / SECRETS_FILENAME

#: 再試行する応答(Claude API)。429はレート制限、529は過負荷。値と待ち方は公式SDK(Python)の既定に合わせた。
RETRYABLE_STATUSES = frozenset({408, 409, 429}) | frozenset(range(500, 600))
MAX_RETRIES = 2
INITIAL_RETRY_DELAY = 0.5
MAX_RETRY_DELAY = 8.0
#: `retry-after` をこの秒数まで信じる(超える指示は無視して上の間隔で待つ。SDKと同じ)。
MAX_RETRY_AFTER = 60.0

#: 失敗の本文をこの長さで切る(モデルへ返す文が無限に伸びないように)。
MAX_ERROR_CHARS = 400

#: llama-server が応答に付ける `timings` のうち、`usage` へ写す欄。**往復をまたいで足して意味が
#: 残るものだけ**(`*_per_second` のような率は足せない)。`cache_n` はキャッシュから使い回した
#: トークン数、`prompt_n` は新しく読んだ数、`predicted_n` は書いた数。
TIMING_KEYS = ("cache_n", "prompt_n", "prompt_ms", "predicted_n", "predicted_ms")

#: 固定プレフィックスを保存するスロットファイル(llama-server の `--slot-save-path` の下)。
#: **1つを上書きし続ける** —— 名前を内容ごとに変えると、ツール定義を変えるたびに古いファイルが
#: 相手のディスクに残り、こちらからは消せない。
SLOT_FILE = "duocad-prefix.bin"
#: スロットの保存・復元1回の待ち上限(秒)。ハングの保険として自分で置いた値(実測は0.1秒)。
SLOT_TIMEOUT = 60.0
#: ウォームアップで読んだトークン数がこれ以下なら「戻したスロットが今の定義と同じだった」と見る。
#: 実測: 同じ本文の再送で読むのは1トークン。8 は版による数トークンの差を許すために自分で置いた余裕。
WARM_HIT_MAX_TOKENS = 8

#: HTTPの実体(`(url, 本文, ヘッダ, 応答全体の待ち上限) -> 非streamingと同じ形の応答`)。
#: テストは差し替える(外部を叩かないため)。
Transport = Callable[[str, dict, dict, float], dict]


class ProviderError(RuntimeError):
    """プロバイダとの1往復が成立しなかった(HTTP・接続・応答の形)。

    **メッセージに鍵を含めない**。生成元は必ず `_redact()` を通す。
    """

    def __init__(
        self, message: str, *, status: Optional[int] = None, retry_after: Optional[float] = None
    ) -> None:
        super().__init__(message)
        self.status = status
        #: 相手が `retry-after` で指示した待ち(秒)。無ければ `None`。
        self.retry_after = retry_after


class ProviderTimeout(ProviderError):
    """**待ち時間切れ**だけを表す。

    接続拒否やHTTP 5xxと**同じ型にしない** —— 呼び出し側(`AgentLoop`)は、自分が
    合計時間の予算で待ちを縮めたときだけ、これを「こちらの都合による打ち切り」と
    読み替える。型が同じだと、本物の障害まで `timeout` と名乗ることになり、
    直す場所を見誤らせる。`ProviderError` を捕まえている既存の経路はそのまま効く。
    """


class ProviderContextOverflow(ProviderError):
    """依頼が相手の文脈長に収まらなかった(相手は処理を始めずに断る)。

    障害と同じ型にしない —— 呼び出し側は古い履歴を捨てて同じ往復をやり直せる。
    """


#: 文脈あふれを名乗る本文の目印(llama-server の `exceed_context_size_error`、OpenAI の
#: `context_length_exceeded`、Claude の `prompt is too long`)。
CONTEXT_OVERFLOW_MARKS = ("exceed_context_size", "context_length_exceeded", "prompt is too long")


def _failure(
    message: str, body: str, status: Optional[int] = None, retry_after: Optional[float] = None
) -> ProviderError:
    kind = ProviderContextOverflow if any(mark in body for mark in CONTEXT_OVERFLOW_MARKS) else ProviderError
    return kind(f"{message}: {body[:MAX_ERROR_CHARS]}", status=status, retry_after=retry_after)


def _seconds(header: Optional[str]) -> Optional[float]:
    """`retry-after` の秒数(HTTP日付の形は読まない)。読めなければ `None`。"""
    try:
        return float(header) if header else None
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# 中立の往復型(ループとアダプタの間の言葉)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolCall:
    """モデルが「これを呼びたい」と言ってきた1件。

    `parse_error` が入っているものは**引数がJSONとして壊れていた**もので、
    ループはツールを実行せず `invalid_argument` の封筒を返す(黙って捨てない)。
    """

    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    parse_error: Optional[str] = None


@dataclass(frozen=True)
class ToolResult:
    """ツール1件の実行結果(モデルへ返す本文は既に文字列になっている)。"""

    call_id: str
    name: str
    content: str
    ok: bool


@dataclass(frozen=True)
class UserTurn:
    text: str


@dataclass(frozen=True)
class AssistantTurn:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    #: プロバイダが返した生の本文。`anthropic` はこれを**そのまま送り返す**(thinkingブロックを
    #: 壊さない)。`openai_compat` は使わず組み直す。
    raw: Any = None


@dataclass(frozen=True)
class ToolResultTurn:
    results: tuple[ToolResult, ...]


Turn = Any  # UserTurn | AssistantTurn | ToolResultTurn


@dataclass(frozen=True)
class ProviderReply:
    """1往復の結果(中立)。"""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    #: プロバイダが名乗った停止理由の生値(`end_turn` / `tool_use` / `stop` / `length` ...)。
    stop_reason: str = ""
    #: 中立に畳んだ停止理由。ループはこちらで分岐する。
    #: `tool_use` / `end_turn` / `max_tokens` / `refusal` / `other`
    stop: str = "end_turn"
    usage: dict = field(default_factory=dict)
    raw_message: Any = None


# --------------------------------------------------------------------------------------
# 外へ出る文字列(鍵を載せない)
# --------------------------------------------------------------------------------------
def _redact(text: str, secret: Optional[str]) -> str:
    """例外メッセージから鍵を消す。

    HTTPライブラリは不正なヘッダ値を例外文へそのまま載せる(例: 改行入りのキー)。**載せるときは
    `repr` の形**(`b'sk-...\\n...'`)なので、元の文字列との一致だけでは改行入りのキーを消せない。
    外へ出る文字列は**必ず**ここを通す。
    """
    if not secret:
        return text
    for form in (secret, repr(secret)[1:-1], repr(secret.encode("utf-8"))[2:-1]):
        text = text.replace(form, "***")
    return text


def _redacted(error: Exception, secret: Optional[str]) -> ProviderError:
    """外へ投げる例外へ詰め替える(鍵を消す)。

    **`ProviderError` の型をそのまま保って作り直す**。`ProviderError(...)` と書くと
    `ProviderTimeout` がただの `ProviderError` へ潰れる —— 呼び出し側は待ち時間切れだけを
    別扱いするので、ここで型が消えると本物の障害と区別できなくなる。
    """
    if isinstance(error, ProviderError):
        return type(error)(_redact(str(error), secret), status=error.status)
    return ProviderError(_redact(f"{type(error).__name__}: {error}", secret))


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------
def _origin(url: str) -> str:
    """エラーメッセージに載せてよい程度に短くしたURL(クエリを落とす)。"""
    return url.split("?", 1)[0]


def _json_object(body: str, url: str) -> dict:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise ProviderError(
            f"{_origin(url)} の応答がJSONではない: {body[:MAX_ERROR_CHARS]}"
        ) from None
    if not isinstance(parsed, dict):
        raise ProviderError(f"{_origin(url)} の応答がJSONオブジェクトではない")
    return parsed


def _assemble(chunks: Iterator[dict], url: str) -> dict:
    """ストリーミングの断片を、非streamingの応答と同じ形の辞書へ畳む(`parse_reply` が読む形)。

    ツール呼び出しの引数は**文字列の断片で届く**ので、`index` ごとに連結する。`usage` と
    `timings` は最後の断片に載る(llama-server。`stream_options.include_usage` が要る)。
    """
    content: list[str] = []
    calls: dict[int, dict] = {}
    finish_reason = ""
    usage: dict = {}
    timings: dict = {}
    for chunk in chunks:
        if "error" in chunk:
            raise _failure(
                f"{_origin(url)} が途中で失敗した", json.dumps(chunk["error"], ensure_ascii=False)
            )
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        if isinstance(chunk.get("timings"), dict):
            timings = chunk["timings"]
        choices = chunk.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            delta = delta if isinstance(delta, dict) else {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            raw_calls = delta.get("tool_calls")
            for raw in raw_calls if isinstance(raw_calls, list) else []:
                slot = calls.setdefault(
                    raw.get("index", len(calls)), {"id": "", "name": "", "arguments": ""}
                )
                function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
                slot["id"] = raw.get("id") or slot["id"]
                slot["name"] += function.get("name") or ""
                if isinstance(function.get("arguments"), str):
                    slot["arguments"] += function["arguments"]
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
    message: dict = {"role": "assistant", "content": "".join(content) or None}
    if calls:
        message["tool_calls"] = [
            {
                "id": calls[index]["id"],
                "type": "function",
                "function": {"name": calls[index]["name"], "arguments": calls[index]["arguments"]},
            }
            for index in sorted(calls)
        ]
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage,
        "timings": timings,
    }


def _assemble_anthropic(events: Iterator[dict], url: str) -> dict:
    """Claude のストリーミング断片を、非streamingの `POST /v1/messages` と同じ形の辞書へ畳む。

    **終わりは `message_stop` で知る** —— `[DONE]` は来ないので、そこで読むのをやめないと
    接続の終わりを「途中で切れた」と取る。

    thinkingブロックは**本文が空でも署名ごと残す**(既定では思考の本文は返らず `signature` だけが来る)。
    次の往復でそのまま送り返す約束なので(`AnthropicProvider.render_messages`)、ここで落とすとその会話の
    思考が捨てられる。ツール呼び出しの引数は文字列の断片で届くので、ブロックが閉じたときにparseする。
    """
    message: dict = {}
    blocks: dict[int, dict] = {}
    pieces: dict[int, list[str]] = {}
    for event in events:
        kind = event.get("type")
        if kind == "error":
            raise _failure(
                f"{_origin(url)} が途中で失敗した", json.dumps(event.get("error"), ensure_ascii=False)
            )
        index = event.get("index")
        if kind == "message_start" and isinstance(event.get("message"), dict):
            message = dict(event["message"])
        elif kind == "content_block_start" and isinstance(event.get("content_block"), dict):
            blocks[index] = dict(event["content_block"])
        elif kind == "content_block_delta" and index in blocks:
            block, delta = blocks[index], event.get("delta") or {}
            step = delta.get("type")
            if step == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif step == "input_json_delta":
                pieces.setdefault(index, []).append(delta.get("partial_json", ""))
            elif step == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif step == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif step == "citations_delta":
                block.setdefault("citations", []).append(delta.get("citation"))
        elif kind == "content_block_stop" and index in pieces:
            text = "".join(pieces.pop(index))
            if text.strip():
                try:
                    blocks[index]["input"] = json.loads(text)
                except json.JSONDecodeError:
                    blocks[index]["input"] = text
        elif kind == "message_delta":
            message.update(event.get("delta") or {})
            if isinstance(event.get("usage"), dict):
                message["usage"] = {**(message.get("usage") or {}), **event["usage"]}
        elif kind == "message_stop":
            break
    message["content"] = [blocks[index] for index in sorted(blocks)]
    return message


#: ストリーミングの断片を非streamingの形へ畳む関数(`_assemble` / `_assemble_anthropic`)。
Assemble = Callable[[Iterator[dict], str], dict]


class StreamingTransport:
    """POSTをストリーミングで受け、非streamingと同じ形の辞書にして返す(畳み方は `assemble`)。

    **ストリーミングで受ける理由は表示ではなく打ち切りである。** llama-server は非streamingの
    リクエストを、クライアントが接続を閉じても最後まで生成し続ける(実測: 1,500トークンの生成を
    3秒で閉じても残り27秒ぶんを書き切り、その間スロットを塞いだ。ストリーミングは閉じて0.2秒で
    止まる)。だから待ち時間切れと中断は、どちらも**接続を閉じる**ことで効かせる。

    `timeout` は**応答全体**の待ち上限である。ストリーミングは断片が届くたびにソケットの待ちが
    仕切り直されるので、締切を自前で数えて残り時間をソケットへ渡す。
    """

    def __init__(self, assemble: Assemble = _assemble, cancelled: Optional[threading.Event] = None) -> None:
        self._assemble = assemble
        #: 持ち主が立てて持ち主が下ろす中断の印(`AnthropicProvider`)。自分の `_aborted` と違って往復の頭で
        #: 下ろさないので、往復が始まる前に届いた中断がここに残る。
        self._cancelled = cancelled
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._aborted = False

    def _stopped(self) -> bool:
        return self._aborted or (self._cancelled is not None and self._cancelled.is_set())

    def abort(self) -> None:
        """走っている往復の接続を閉じる(**別スレッドから呼んでよい**)。走っていなければ何もしない。"""
        with self._lock:
            self._aborted = True
            sock = self._sock
        if sock is not None:
            # `close()` では、別スレッドで読み取りを待っている側が起きない(Linux)。
            # `shutdown` は待っている読み取りをEOFで返させる。
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def __call__(self, url: str, payload: dict, headers: dict, timeout: float) -> dict:
        deadline = monotonic() + timeout
        parts = urllib.parse.urlsplit(url)
        factory = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        connection = factory(parts.hostname or "", parts.port, timeout=timeout)
        with self._lock:
            self._aborted = False
        try:
            # **送る前に繋いでソケットを控え、そこで中断を見る。** 控えた後の中断は `abort` が接続を
            # 閉じ、控える前の中断はここで拾うので、中断した依頼が送られる隙間が無い(課金される相手では
            # 1回の送信がそのまま請求になる)。サーバが接続を閉じる応答(`Connection: close`)だと
            # `getresponse()` が `connection.sock` を `None` にするので、応答の後からは取れない。
            # HTTPS の握手の最中はまだ控えていないので、その間の中断は握手が終わるか待ち時間が切れるまで
            # 効かない(終われば下で止まり、送りはしない)。
            connection.connect()
            with self._lock:
                self._sock = connection.sock
                if self._stopped():
                    raise ProviderError("中断したので送らなかった")
            connection.request(
                "POST",
                parts.path + (f"?{parts.query}" if parts.query else ""),
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={**headers, "content-type": "application/json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise _failure(
                    f"HTTP {response.status} from {_origin(url)}",
                    response.read().decode("utf-8", "replace"),
                    response.status,
                    _seconds(response.getheader("retry-after")),
                )
            if "text/event-stream" not in (response.getheader("content-type") or ""):
                # ストリーミングに応じないサーバは本文を1つ返す。
                return _json_object(response.read().decode("utf-8", "replace"), url)
            return self._assemble(self._events(response, deadline, url), url)
        except ProviderError:
            raise
        except TimeoutError:
            raise ProviderTimeout(f"{_origin(url)} が {timeout:g}s で返らなかった") from None
        except (OSError, http.client.HTTPException) as error:
            if self._stopped():
                raise ProviderError("中断したので接続を閉じた") from None
            raise ProviderError(f"{_origin(url)} へ接続できない: {error}") from None
        finally:
            with self._lock:
                self._sock = None
            connection.close()

    def _events(self, response: http.client.HTTPResponse, deadline: float, url: str) -> Iterator[dict]:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock = self._sock
            if sock is not None:
                sock.settimeout(remaining)
            line = response.readline()
            if not line:
                if self._stopped():
                    raise ProviderError("中断したので接続を閉じた")
                raise ProviderError(f"{_origin(url)} の応答が途中で切れた")
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("data:"):
                continue
            data = text[5:].strip()
            if data == "[DONE]":
                return
            chunk = _json_object(data, url)
            yield chunk


def post_json(url: str, payload: Optional[dict], headers: dict, timeout: float) -> dict:
    """JSONを送ってJSONを受け取る(本文が1つで返る口のため。対話は `StreamingTransport`)。
    `payload` が `None` なら GET。失敗は `ProviderError`。
    """
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "content-type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:MAX_ERROR_CHARS]
        raise ProviderError(
            f"HTTP {error.code} from {_origin(url)}: {detail}", status=error.code
        ) from None
    except urllib.error.URLError as error:
        # **待ち時間切れが `URLError` に包まれて来る経路がある**。
        # 接続の段階で切れると urllib は `URLError(reason=TimeoutError(...))` を投げ、
        # この節が下の `except TimeoutError` より先に捕まえてしまう。包みの中を見て
        # 名前を付け直さないと、こちらの予算による打ち切りが「接続できない」になる。
        if isinstance(error.reason, TimeoutError):
            raise ProviderTimeout(
                f"{_origin(url)} が {timeout:g}s で返らなかった"
            ) from None
        raise ProviderError(f"{_origin(url)} へ接続できない: {error.reason}") from None
    except TimeoutError:
        raise ProviderTimeout(f"{_origin(url)} が {timeout:g}s で返らなかった") from None
    return _json_object(body, url)


# --------------------------------------------------------------------------------------
# アダプタ
# --------------------------------------------------------------------------------------
class Provider:
    """プロバイダ1つ。**ループが使うのは `name` / `model` / `complete` / `abort` だけ**。"""

    #: `openai_compat` / `anthropic`
    name = "provider"

    def abort(self) -> None:
        """走っている往復を捨てる(中断フラグが立ったとき**別スレッドから**呼ばれる)。既定は何もしない。"""

    def rearm(self) -> None:
        """前の依頼で立った中断の印を下ろす(`AgentLoop.run` が依頼の頭で呼ぶ)。既定は何もしない。"""

    def prepare(self, *, system: str, tools: Sequence[dict], timeout: float) -> dict:
        """固定プレフィックス(system + tools)を相手のキャッシュへ載せておく。既定は何もしない。"""
        return {}

    def complete(
        self,
        *,
        system: str,
        turns: Sequence[Turn],
        tools: Sequence[dict],
        max_tokens: int,
        timeout: float,
    ) -> ProviderReply:
        raise NotImplementedError


#: 「まだ読んでいない」の印(`None` は「読んだが分からなかった」に使う)。
_UNREAD = object()


class OpenAICompatProvider(Provider):
    """OpenAI互換(`POST /v1/chat/completions`・ストリーミング)。llama-server がこれ。

    詰め替えは3つだけ:

    - ツール定義 … `{"type":"function","function":{name, description, parameters}}` で包む
    - ツール呼び出し … `message.tool_calls`(引数は**JSON文字列**なので自分でparseする)
    - ツール結果 … `role:"tool"` + `tool_call_id` を**1件ずつ**

    APIキーは既定で不要(ローカル)。要るサーバのために `api_key` を受けるが、
    **`build_provider` は渡さない**(送り先を画面から変えられるので、鍵を乗せると外へ出る)。値は
    `repr()` にも例外にも出さない(`_redact()`)。
    """

    name = "openai_compat"

    def __init__(
        self,
        *,
        model: str = "local",
        base_url: str = DEFAULT_OPENAI_COMPAT_BASE_URL,
        api_key: Optional[str] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        #: 既定の実体は**プロバイダごとに1つ**(走っている接続を覚えていて `abort` で閉じるため)。
        self._transport: Transport = StreamingTransport() if transport is None else transport
        self._n_ctx: Any = _UNREAD

    def __repr__(self) -> str:  # pragma: no cover - 表示だけ
        return f"OpenAICompatProvider(model={self.model!r}, base_url={self.base_url!r})"

    def abort(self) -> None:
        abort = getattr(self._transport, "abort", None)
        if callable(abort):
            abort()

    # ------------------------------------------------------------------ 整形
    def render_tools(self, tools: Sequence[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["inputSchema"],
                },
            }
            for tool in tools
        ]

    def render_messages(self, system: str, turns: Sequence[Turn]) -> list[dict]:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        for turn in turns:
            if isinstance(turn, UserTurn):
                messages.append({"role": "user", "content": turn.text})
            elif isinstance(turn, AssistantTurn):
                # **生のメッセージは戻さず組み直す**(サーバごとに増える
                # `reasoning_content` のような欄を送り返すと弾く実装がある)。
                message: dict = {"role": "assistant", "content": turn.text or None}
                if turn.tool_calls:
                    message["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in turn.tool_calls
                    ]
                messages.append(message)
            elif isinstance(turn, ToolResultTurn):
                for result in turn.results:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.call_id,
                            "name": result.name,
                            "content": result.content,
                        }
                    )
            else:  # pragma: no cover
                raise TypeError(f"未知のターン: {turn!r}")
        return messages

    def build_payload(
        self,
        *,
        system: str,
        turns: Sequence[Turn],
        tools: Sequence[dict],
        max_tokens: int,
    ) -> dict:
        payload: dict = {
            "model": self.model,
            "max_tokens": int(max_tokens),
            "messages": self.render_messages(system, turns),
            "stream": True,
            # 無いと最後の断片に `usage` が載らない(OpenAI互換の約束)。
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = self.render_tools(tools)
            payload["tool_choice"] = "auto"
        return payload

    def parse_reply(self, response: dict) -> ProviderReply:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("応答に choices が無い")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}

        calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls")
        for index, raw in enumerate(raw_calls if isinstance(raw_calls, list) else []):
            if not isinstance(raw, dict):
                continue
            function = raw.get("function")
            function = function if isinstance(function, dict) else {}
            arguments_text = function.get("arguments")
            arguments: dict = {}
            parse_error: Optional[str] = None
            if isinstance(arguments_text, dict):
                # 仕様は文字列だが、辞書で返す実装がある(llama-serverの一部の版)。
                arguments = arguments_text
            elif isinstance(arguments_text, str) and arguments_text.strip():
                try:
                    decoded = json.loads(arguments_text)
                except json.JSONDecodeError as error:
                    parse_error = f"引数がJSONとして壊れている: {error}"
                else:
                    if isinstance(decoded, dict):
                        arguments = decoded
                    else:
                        parse_error = "引数がJSONオブジェクトではない"
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                    parse_error=parse_error,
                )
            )

        content = message.get("content")
        finish_reason = str(choice.get("finish_reason") or "")
        usage = dict(response["usage"]) if isinstance(response.get("usage"), dict) else {}
        timings = response.get("timings")
        if isinstance(timings, dict):
            usage.update(
                {key: timings[key] for key in TIMING_KEYS if isinstance(timings.get(key), (int, float))}
            )
        return ProviderReply(
            text=(content or "").strip() if isinstance(content, str) else "",
            tool_calls=tuple(calls),
            stop_reason=finish_reason,
            stop=_neutral_stop(finish_reason, bool(calls), {"length"}),
            usage=usage,
            raw_message=message,
        )

    # ------------------------------------------------------------------ 1往復
    def complete(
        self,
        *,
        system: str,
        turns: Sequence[Turn],
        tools: Sequence[dict],
        max_tokens: int,
        timeout: float,
    ) -> ProviderReply:
        payload = self.build_payload(
            system=system, turns=turns, tools=tools, max_tokens=max_tokens
        )
        response = self._call(
            f"{self.base_url}/chat/completions", payload, self._headers(), timeout
        )
        return self.parse_reply(response)

    def _headers(self) -> dict:
        return {"authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    # ------------------------------------------------------------------ ウォームアップ
    def prepare(self, *, system: str, tools: Sequence[dict], timeout: float) -> dict:
        """固定プレフィックスをサーバのキャッシュへ載せておく(会話には何も足さない)。

        1. 保存済みのスロットがあれば戻す(`--slot-save-path` 付きの llama-server)。
        2. system + tools + `"."` で1トークンだけ生成させる。戻したものが今の定義と同じなら
           読むのは1トークンで済み、違えば食い違った所から読み直す。**古いスロットを戻しても
           結果は狂わない** —— キャッシュはトークン列の前方一致でしか使われない。
        3. 読み直しが起きていたら保存し直す。

        スロットの口が無いサーバ(`--slot-save-path` 無し・他のOpenAI互換)では2だけが効く。
        """
        restored = self._slot("restore")
        payload = self.build_payload(system=system, turns=[UserTurn(".")], tools=tools, max_tokens=1)
        reply = self.parse_reply(
            self._call(f"{self.base_url}/chat/completions", payload, self._headers(), timeout)
        )
        read = reply.usage.get("prompt_n")
        saved = (read is None or read > WARM_HIT_MAX_TOKENS) and self._slot("save")
        return {"restored": restored, "read": read, "reused": reply.usage.get("cache_n"), "saved": saved}

    def _slot(self, action: str) -> bool:
        """スロット0を保存/復元する(llama-server 固有の口)。**口が無ければ黙って `False`**。"""
        try:
            post_json(
                f"{self._root()}/slots/0?action={action}",
                {"filename": SLOT_FILE},
                self._headers(),
                SLOT_TIMEOUT,
            )
        except ProviderError:
            return False
        return True

    def _root(self) -> str:
        """llama-server 固有の口(`/slots`・`/props`)が生える根(`/v1` の外)。"""
        return self.base_url[: -len("/v1")] if self.base_url.endswith("/v1") else self.base_url

    def context_size(self) -> Optional[int]:
        """相手の文脈長(トークン)。llama-server の `/props` から1度だけ読む。分からなければ `None`。"""
        if self._n_ctx is _UNREAD:
            try:
                props = post_json(f"{self._root()}/props", None, self._headers(), SLOT_TIMEOUT)
                settings = props.get("default_generation_settings")
                n_ctx = settings.get("n_ctx") if isinstance(settings, dict) else None
            except ProviderError:
                n_ctx = None
            self._n_ctx = n_ctx if isinstance(n_ctx, int) and n_ctx > 0 else None
        return self._n_ctx

    def _call(self, url: str, payload: dict, headers: dict, timeout: float) -> dict:
        try:
            return self._transport(url, payload, headers, timeout)
        except Exception as error:
            raise _redacted(error, self._api_key) from None


def load_api_key() -> Optional[str]:
    """ClaudeのAPIキーを読む。**環境変数が先、次にgitignore済みのローカル設定ファイル**。

    見つからなければ `None`(未設定は「使えない」であって異常ではない)。設定ファイルが
    壊れていても値の推測はせず、**例外も上げない** —— `Server` は起動時にここを読むので、
    投げるとClaudeを使わない人のサーバまで立たなくなる。

    文字コードはBOMで見分ける(外部の罠): Windows PowerShell 5.1 の `>` / `Out-File` は UTF-16LE、
    `Set-Content -Encoding utf8` はBOM付きUTF-8で書く。
    """
    value = (os.environ.get(ANTHROPIC_API_KEY_ENV) or "").strip()
    if value:
        return value
    return str(_read_secrets().get(ANTHROPIC_API_KEY_ENV) or "").strip() or None


def _read_secrets() -> dict:
    """`duocad_secrets.json` の中身。無い・読めない・形が違うときは空の辞書(例外にしない)。"""
    try:
        raw = SECRETS_PATH.read_bytes()
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
        data = json.loads(text)
    except (OSError, ValueError):  # UnicodeDecodeError と JSONDecodeError はどちらも ValueError
        return {}
    return data if isinstance(data, dict) else {}


def api_key_configured() -> bool:
    return load_api_key() is not None


def api_key_status() -> dict:
    """**画面に出してよいのはこれだけ**: 置いてあるか・どこから読んだか・末尾4文字。値そのものは返さない。

    `source` は `env`(環境変数。ファイルより優先される)/ `file` / `None`。
    """
    if (os.environ.get(ANTHROPIC_API_KEY_ENV) or "").strip():
        source = "env"
    else:
        source = "file" if load_api_key() else None
    key = load_api_key()
    return {"configured": key is not None, "source": source, "hint": key[-4:] if key else None}


def key_problem(key: str) -> Optional[str]:
    """送ってはいけないキーなら理由(**キーの値は含めない**)。問題が無ければ `None`。

    改行や空白の混じったキー(貼り付けの崩れ)は、HTTPライブラリがヘッダ値ごと例外文へ載せる。
    """
    if not key:
        return "APIキーが空"
    if any(ch.isspace() or not ch.isprintable() for ch in key):
        return "APIキーに空白か改行が混じっている(貼り付けを確かめる)"
    return None


def save_api_key(key: str) -> None:
    """画面から受け取ったキーを `duocad_secrets.json` へ書く(他の欄は残す)。

    書き方は一時ファイル → 置き換え(書きかけのファイルを読まれない)。問題のあるキーは `ValueError`。
    """
    key = key.strip()
    problem = key_problem(key)
    if problem:
        raise ValueError(problem)
    _write_secrets({**_read_secrets(), ANTHROPIC_API_KEY_ENV: key})


def delete_api_key() -> None:
    """`duocad_secrets.json` からキーを消す。他の欄が残らなければファイルごと消す。"""
    rest = {name: value for name, value in _read_secrets().items() if name != ANTHROPIC_API_KEY_ENV}
    if rest:
        _write_secrets(rest)
    else:
        SECRETS_PATH.unlink(missing_ok=True)


def _write_secrets(data: dict) -> None:
    staging = SECRETS_PATH.with_name(SECRETS_PATH.name + ".tmp")
    staging.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(staging, SECRETS_PATH)


def _retry_wait(error: Exception, attempt: int) -> Optional[float]:
    """この失敗をやり直すなら何秒待つか。やり直さないものは `None`。

    待ち時間切れはやり直さない(合計時間の予算で縮めた待ちなので、やり直しても予算を食うだけ)。
    """
    if not isinstance(error, ProviderError) or isinstance(error, ProviderTimeout):
        return None
    if error.status not in RETRYABLE_STATUSES:
        return None
    if error.retry_after is not None and 0 <= error.retry_after <= MAX_RETRY_AFTER:
        return error.retry_after
    return min(INITIAL_RETRY_DELAY * 2**attempt, MAX_RETRY_DELAY)


class AnthropicProvider(Provider):
    """Claude API(`POST /v1/messages`・ストリーミング)。

    詰め替えは3つだけ:

    - ツール定義 … `descriptors()` の `inputSchema` を `input_schema` へ(他はそのまま)
    - ツール呼び出し … `content` の `tool_use` ブロック
    - ツール結果 … `role:"user"` の `tool_result` ブロック(`tool_use_id` で対応づける)

    **送らないもの**: `temperature` / `top_p` / `top_k` と `thinking`。現行モデルは既定値以外の
    サンプリング指定と `thinking.budget_tokens` に400を返す。思考は既定(適応的)のまま触らない ——
    Haikuの深さは `effort` で動かすもので、指定しなければ相手の既定(`medium`)になる。

    **キャッシュの区切りは2つ置く**: system の末尾(tools + system。毎回同じ部分が、
    会話の書き換えに関係なく読み出せる)と、リクエスト直下の自動区切り(伸びていく会話の末尾)。
    ツール定義だけで毎往復数千トークンあり(docs/AI_INTEGRATION.md の実測)、キャッシュ読みは入力の1/10で済む。

    一時的な失敗(429・529・5xx)は、合計時間の残りに収まる範囲で `MAX_RETRIES` 回までやり直す。
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        base_url: str = ANTHROPIC_BASE_URL,
        transport: Optional[Transport] = None,
    ) -> None:
        if not api_key:
            raise ValueError("APIキーが未設定")
        # 値は属性に持つが、`repr` にも例外にも出さない(下の __repr__ と _redacted)。
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        #: 中断が立ったか。**下ろすのは依頼の頭(`rearm`)だけ** —— 往復の頭で下ろすと、ループが
        #: 中断を確かめてから送るまでの間に届いた中断が消え、課金される往復が1回走る。
        #: `StreamingTransport` と共有する(送る直前にもこれを見る)。
        self._aborted = threading.Event()
        self._transport: Transport = (
            StreamingTransport(_assemble_anthropic, cancelled=self._aborted)
            if transport is None
            else transport
        )

    def __repr__(self) -> str:  # pragma: no cover - 表示だけ
        return f"AnthropicProvider(model={self.model!r}, base_url={self.base_url!r})"

    def abort(self) -> None:
        self._aborted.set()
        abort = getattr(self._transport, "abort", None)
        if callable(abort):
            abort()

    def rearm(self) -> None:
        self._aborted.clear()

    # ------------------------------------------------------------------ 整形
    def render_tools(self, tools: Sequence[dict]) -> list[dict]:
        return [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool["inputSchema"],
            }
            for tool in tools
        ]

    def render_messages(self, turns: Sequence[Turn]) -> list[dict]:
        messages: list[dict] = []
        for turn in turns:
            if isinstance(turn, UserTurn):
                messages.append({"role": "user", "content": turn.text})
            elif isinstance(turn, AssistantTurn):
                # **生の本文をそのまま返す** —— thinkingブロックを1つでも落とす・書き換えると、
                # その会話の思考が捨てられるか400になる。
                content = turn.raw
                if not isinstance(content, list) or not content:
                    # 生の本文が無いとき(ループが代わりに呼んだ問い合わせ・畳んだ履歴)は組み直す。
                    # **tool_useブロックを落とさない** —— 落とすと次のtool_resultが対応先を失って弾かれる。
                    content = []
                    if turn.text:
                        content.append({"type": "text", "text": turn.text})
                    content.extend(
                        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                        for call in turn.tool_calls
                    )
                    if not content:
                        continue
                messages.append({"role": "assistant", "content": content})
            elif isinstance(turn, ToolResultTurn):
                # 複数のツール結果は**1つのuserメッセージにまとめる**(分けると並列呼びが止む)。
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": result.call_id,
                                "content": result.content,
                                "is_error": not result.ok,
                            }
                            for result in turn.results
                        ],
                    }
                )
            else:  # pragma: no cover - 型を増やしたときに気づくため
                raise TypeError(f"未知のターン: {turn!r}")
        return messages

    def build_payload(
        self,
        *,
        system: str,
        turns: Sequence[Turn],
        tools: Sequence[dict],
        max_tokens: int,
    ) -> dict:
        cache = {"type": "ephemeral"}
        payload: dict = {
            "model": self.model,
            "max_tokens": int(max_tokens),
            "messages": self.render_messages(turns),
            "stream": True,
            "cache_control": cache,
        }
        if tools:
            payload["tools"] = self.render_tools(tools)
        if system:
            payload["system"] = [{"type": "text", "text": system, "cache_control": cache}]
        elif tools:
            payload["tools"][-1]["cache_control"] = cache
        return payload

    def parse_reply(self, response: dict) -> ProviderReply:
        content = response.get("content")
        blocks = content if isinstance(content, list) else []
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text", "")))
            elif kind == "tool_use":
                arguments = block.get("input")
                calls.append(
                    ToolCall(
                        id=str(block.get("id", "")),
                        name=str(block.get("name", "")),
                        arguments=arguments if isinstance(arguments, dict) else {},
                        parse_error=(
                            None
                            if isinstance(arguments, dict)
                            else "tool_use.input がJSONオブジェクトではない"
                        ),
                    )
                )
        stop_reason = str(response.get("stop_reason") or "")
        return ProviderReply(
            text="\n".join(text for text in texts if text).strip(),
            tool_calls=tuple(calls),
            stop_reason=stop_reason,
            stop=_neutral_stop(stop_reason, bool(calls), {"max_tokens", "model_context_window_exceeded"}),
            usage=dict(response["usage"]) if isinstance(response.get("usage"), dict) else {},
            raw_message=blocks,
        )

    # ------------------------------------------------------------------ 1往復
    def complete(
        self,
        *,
        system: str,
        turns: Sequence[Turn],
        tools: Sequence[dict],
        max_tokens: int,
        timeout: float,
    ) -> ProviderReply:
        payload = self.build_payload(system=system, turns=turns, tools=tools, max_tokens=max_tokens)
        return self.parse_reply(self._call(f"{self.base_url}/v1/messages", payload, timeout))

    def _call(self, url: str, payload: dict, timeout: float) -> dict:
        """1往復(一時的な失敗はやり直す)。**外へ出る例外は必ずキーを消してから**投げ直す。"""
        headers = {"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_API_VERSION}
        deadline = monotonic() + timeout
        attempt = 0
        while True:
            if self._aborted.is_set():
                raise ProviderError("中断したので送らなかった")
            try:
                response = self._transport(url, payload, headers, max(deadline - monotonic(), 0.001))
            except Exception as error:
                wait = _retry_wait(error, attempt)
                if (
                    wait is None
                    or attempt >= MAX_RETRIES
                    or monotonic() + wait >= deadline
                    or self._aborted.wait(wait)
                ):
                    raise _redacted(error, self._api_key) from None
                attempt += 1
                continue
            # 差し替えた transport(印を共有しないもの)が中断を素通りさせても、応答は使わない。
            if self._aborted.is_set():
                raise ProviderError("中断したので応答を捨てた")
            return response


def _neutral_stop(raw: str, has_tool_calls: bool, truncated: set) -> str:
    """プロバイダの停止理由 → ループが分岐する4語彙 + `other`。

    **打ち切りの判定がツール呼び出しより先**である。上限で切られた応答にも
    `tool_calls` は載りうるが、その引数のJSONは**途中で切れている可能性がある**
    —— `tool_use` に丸めると、ループは切れた引数のまま
    ツールを実行してしまう(`loop.py` がここで止める理由)。
    """
    if raw in truncated:
        return "max_tokens"
    if raw == "refusal":
        return "refusal"
    if has_tool_calls:
        return "tool_use"
    if raw in ("end_turn", "stop", "stop_sequence", ""):
        return "end_turn"
    return "other"


def default_model(kind: str) -> str:
    """モデルを明示しなかったときに使う名前(`build_provider` と画面の表示が同じ規則で引く)。"""
    if kind == "anthropic":
        return os.environ.get(ANTHROPIC_MODEL_ENV) or DEFAULT_ANTHROPIC_MODEL
    return "local"


def build_provider(
    kind: str,
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    transport: Optional[Transport] = None,
) -> Provider:
    """名前からプロバイダを1つ作る(アプリ内UIが選ぶ入口)。

    APIキーは**呼び出し側から渡させない** —— `load_api_key()` を読むのはここだけで、
    渡す先は `anthropic` だけである。

    - `anthropic` … `base_url` を**受け取っても使わない**(送り先は公式APIに固定。差し替えられると
      実キーがそのホストへ届く)。モデルは 引数 → 環境変数 `DUOCAD_ANTHROPIC_MODEL` → 既定 の順
      (`default_model`)。
    - `openai_compat` … キーを渡さないので `_api_key` は常に `None`。`base_url` を任意の外部ホストへ
      向けても、飛ぶのはキーの無いリクエストだけになる。
    """
    if kind == "anthropic":
        api_key = load_api_key()
        if not api_key:
            unreadable = (
                f"。{SECRETS_FILENAME} はあるが読めなかった(JSONの形とキー名を確かめる)"
                if SECRETS_PATH.exists()
                else ""
            )
            raise ProviderError(
                f"ClaudeのAPIキーが未設定(環境変数 {ANTHROPIC_API_KEY_ENV} か、リポジトリ直下の "
                f"{SECRETS_FILENAME} に {{\"{ANTHROPIC_API_KEY_ENV}\": \"...\"}} の形で置く){unreadable}"
            )
        problem = key_problem(api_key)
        if problem:
            raise ProviderError(problem)
        return AnthropicProvider(api_key, model=model or default_model(kind), transport=transport)
    if kind == "openai_compat":
        return OpenAICompatProvider(
            model=model or default_model(kind),
            base_url=base_url or DEFAULT_OPENAI_COMPAT_BASE_URL,
            transport=transport,
        )
    raise ValueError(f"未知のプロバイダ: {kind!r} (openai_compat / anthropic)")

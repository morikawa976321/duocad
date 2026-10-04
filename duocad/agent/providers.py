"""LLMプロバイダのアダプタ —— **キーの詰め替えだけを担当する層**。

- `openai_compat`  … llama-server などのOpenAI互換(`POST /v1/chat/completions`)

**中立の往復型がここが層の境目**: ループは `UserTurn` / `AssistantTurn` / `ToolResultTurn` の
3つだけを積み、**プロバイダ固有の本文の形はアダプタの中の話**である。逆にアダプタは
`DocumentTools` も `Session` も知らない —— 受け取るのは `descriptors()` が返したツール定義
そのままである(定義を書き写す場所を作らない)。

**アダプタが1つでもこの層は畳まない** —— `Provider` / `build_provider` は
次のプロバイダの差込口である。

**この層はAPIキーを持たない**。Claudeを使う道はMCP(サブスク認証のClaude Codeが
`/mcp` へ繋ぐ)だけである。
`openai_compat` はキーを要求するサーバのために `api_key` を受けられるが、`build_provider`
は渡さない(画面から鍵を受け取る経路を作らないため)。外へ出る文字列は `_redact()` を通す。

公式SDK(`openai`)は入れず、標準ライブラリで素のHTTPを書く。応答は**ストリーミングで受けて
組み立てる**(`StreamingTransport`。理由は表示ではなく打ち切り)。

**OCP / build123d をimportしない**。
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable, Iterator, Optional, Sequence

#: llama-server の既定(`llama-server -c 16384 --port 8080` を想定)。
DEFAULT_OPENAI_COMPAT_BASE_URL = "http://127.0.0.1:8080/v1"

#: 失敗の本文をこの長さで切る(モデルへ返す文が無限に伸びないように)。
MAX_ERROR_CHARS = 400

#: llama-server が応答に付ける `timings` のうち、`usage` へ写す欄。**往復をまたいで足して意味が
#: 残るものだけ**(`*_per_second` のような率は足せない)。`cache_n` はキャッシュから使い回した
#: トークン数、`prompt_n` は新しく読んだ数、`predicted_n` は書いた数。
TIMING_KEYS = ("cache_n", "prompt_n", "prompt_ms", "predicted_n", "predicted_ms")

#: HTTPの実体(`(url, 本文, ヘッダ, 応答全体の待ち上限) -> 非streamingと同じ形の応答`)。
#: テストは差し替える(外部を叩かないため)。
Transport = Callable[[str, dict, dict, float], dict]


class ProviderError(RuntimeError):
    """プロバイダとの1往復が成立しなかった(HTTP・接続・応答の形)。

    **メッセージに鍵を含めない**。生成元は必ず `_redact()` を通す。
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class ProviderTimeout(ProviderError):
    """**待ち時間切れ**だけを表す。

    接続拒否やHTTP 5xxと**同じ型にしない** —— 呼び出し側(`AgentLoop`)は、自分が
    合計時間の予算で待ちを縮めたときだけ、これを「こちらの都合による打ち切り」と
    読み替える。型が同じだと、本物の障害まで `timeout` と名乗ることになり、
    直す場所を見誤らせる。`ProviderError` を捕まえている既存の経路はそのまま効く。
    """


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
    #: プロバイダが返した生の本文(そのまま返す必要のあるアダプタのため。現行の
    #: `openai_compat` は使わず組み直す)。
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

    HTTPライブラリは不正なヘッダ値を例外文へそのまま載せる(例: 改行入りのキー)。
    外へ出る文字列は**必ず**ここを通す。
    """
    if not secret:
        return text
    return text.replace(secret, "***")


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
            detail = json.dumps(chunk["error"], ensure_ascii=False)[:MAX_ERROR_CHARS]
            raise ProviderError(f"{_origin(url)} が途中で失敗した: {detail}")
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


class StreamingTransport:
    """`POST /chat/completions` をストリーミングで受け、非streamingと同じ形の辞書にして返す。

    **ストリーミングで受ける理由は表示ではなく打ち切りである。** llama-server は非streamingの
    リクエストを、クライアントが接続を閉じても最後まで生成し続ける(実測: 1,500トークンの生成を
    3秒で閉じても残り27秒ぶんを書き切り、その間スロットを塞いだ。ストリーミングは閉じて0.2秒で
    止まる)。だから待ち時間切れと中断は、どちらも**接続を閉じる**ことで効かせる。

    `timeout` は**応答全体**の待ち上限である。ストリーミングは断片が届くたびにソケットの待ちが
    仕切り直されるので、締切を自前で数えて残り時間をソケットへ渡す。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._aborted = False

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
            connection.request(
                "POST",
                parts.path + (f"?{parts.query}" if parts.query else ""),
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={**headers, "content-type": "application/json"},
            )
            # **ソケットはここで控える。** サーバが接続を閉じる応答(`Connection: close`)だと
            # `getresponse()` が `connection.sock` を `None` にするので、後からは取れない。
            with self._lock:
                self._sock = connection.sock
                if self._aborted:
                    raise ProviderError("中断したので接続を閉じた")
            response = connection.getresponse()
            if response.status != 200:
                detail = response.read().decode("utf-8", "replace")[:MAX_ERROR_CHARS]
                raise ProviderError(
                    f"HTTP {response.status} from {_origin(url)}: {detail}", status=response.status
                )
            if "text/event-stream" not in (response.getheader("content-type") or ""):
                # ストリーミングに応じないサーバは本文を1つ返す。
                return _json_object(response.read().decode("utf-8", "replace"), url)
            return _assemble(self._events(response, deadline, url), url)
        except ProviderError:
            raise
        except TimeoutError:
            raise ProviderTimeout(f"{_origin(url)} が {timeout:g}s で返らなかった") from None
        except (OSError, http.client.HTTPException) as error:
            if self._aborted:
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
                if self._aborted:
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


def post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    """JSONをPOSTしてJSONを受け取る(本文が1つで返る口のため。対話は `StreamingTransport`)。
    失敗は `ProviderError`。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "content-type": "application/json"},
        method="POST",
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

    #: `openai_compat`(増えたらここに足す)
    name = "provider"

    def abort(self) -> None:
        """走っている往復を捨てる(中断フラグが立ったとき**別スレッドから**呼ばれる)。既定は何もしない。"""

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


class OpenAICompatProvider(Provider):
    """OpenAI互換(`POST /v1/chat/completions`・ストリーミング)。llama-server がこれ。

    詰め替えは3つだけ:

    - ツール定義 … `{"type":"function","function":{name, description, parameters}}` で包む
    - ツール呼び出し … `message.tool_calls`(引数は**JSON文字列**なので自分でparseする)
    - ツール結果 … `role:"tool"` + `tool_call_id` を**1件ずつ**

    APIキーは既定で不要(ローカル)。要るサーバのために `api_key` を受けるが、
    **`build_provider` は渡さない** —— 画面から鍵を受け取る経路は作らない。値は
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
        headers = {}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        response = self._call(
            f"{self.base_url}/chat/completions", payload, headers, timeout
        )
        return self.parse_reply(response)

    def _call(self, url: str, payload: dict, headers: dict, timeout: float) -> dict:
        try:
            return self._transport(url, payload, headers, timeout)
        except ProviderError as error:
            # **型をそのまま保って作り直す**。秘匿のために新しい例外へ
            # 詰め替えるので、`ProviderError(...)` と書くと `ProviderTimeout` が
            # ただの `ProviderError` へ潰れる —— 呼び出し側は待ち時間切れだけを
            # 別扱いするので、ここで型が消えると本物の障害と区別できなくなる。
            raise type(error)(
                _redact(str(error), self._api_key), status=error.status
            ) from None
        except Exception as error:
            raise ProviderError(
                _redact(f"{type(error).__name__}: {error}", self._api_key)
            ) from None


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


def build_provider(
    kind: str,
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    transport: Optional[Transport] = None,
) -> Provider:
    """名前からプロバイダを1つ作る(アプリ内UIが選ぶ入口)。

    **ここでAPIキーを読まない** —— `api_key` を渡していないので
    `OpenAICompatProvider._api_key` は常に `None` である。`base_url` を任意の外部ホストへ
    向けても、飛ぶのはキーの無いリクエストだけになる。
    """
    if kind == "openai_compat":
        return OpenAICompatProvider(
            model=model or "local",
            base_url=base_url or DEFAULT_OPENAI_COMPAT_BASE_URL,
            transport=transport,
        )
    raise ValueError(f"未知のプロバイダ: {kind!r} (openai_compat)")

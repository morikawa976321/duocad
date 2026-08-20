"""LLMプロバイダのアダプタ —— **キーの詰め替えだけを担当する層**。

アプリ内エージェント(`duocad/agent/loop.py`)は「モデルへ道具の一覧と会話を渡し、
呼びたいツールの一覧を受け取る」ことしかしない。その1往復をプロバイダごとの
リクエスト/レスポンスの形へ翻訳するのがこのファイルである。当面の対象は2つ:

- `anthropic`      … Claude API(`POST /v1/messages`)
- `openai_compat`  … llama-server などのOpenAI互換(`POST /v1/chat/completions`)

## 中立の往復型(ここが層の境目)

ループは `UserTurn` / `AssistantTurn` / `ToolResultTurn` の3つだけを積む。
**`tool_use` ブロックか `tool_calls` かはアダプタの中の話**であり、ループは知らない。
逆にアダプタは `DocumentTools` も `Session` も知らない —— 受け取るのは
`descriptors()` が返したツール定義そのままである(定義を書き写す場所を作らない)。

## 依存を増やさない

公式SDK(`anthropic` / `openai`)は入れず、`urllib.request` で素のHTTPを書く
(pyproject は依存を4つに固定している。MCPの公式SDKを蹴ったのと同じ理由)。
v1は**非streaming** —— streamingを入れる段でだけ依存追加を検討する。

## APIキーの取り回し(セキュリティ要件)

- 読む順は 環境変数 → gitignore済みのローカル設定ファイル(`SECRETS_FILENAME`)。
- **値はどこにも出さない** —— `repr()` にも例外メッセージにもログにも。
  外へ出す前に必ず `_redact()` を通す(HTTPライブラリはヘッダ値を例外文へ載せることがある)。
- 画面に出してよいのは `api_key_configured()` の2値だけ(ブラウザへ値を送らない)。

**OCP / build123d をimportしない**(`tests/test_boundaries.py` が検査する)。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

#: 既定のClaudeモデル。**日付サフィックスを付けない**(この綴りで完結している)。
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
#: Claude APIの版ヘッダ(`anthropic-version`)。
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
#: llama-server の既定(`llama-server -c 16384 --port 8080` を想定)。
DEFAULT_OPENAI_COMPAT_BASE_URL = "http://127.0.0.1:8080/v1"

#: 環境変数名。
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
#: リポジトリ直下のローカル設定ファイル(**gitignore済み**)。
#: 形は `{"ANTHROPIC_API_KEY": "..."}` の素のJSON。
SECRETS_FILENAME = "duocad_secrets.json"

#: 失敗の本文をこの長さで切る(モデルへ返す文が無限に伸びないように)。
MAX_ERROR_CHARS = 400

#: HTTPの実体。テストは差し替える(実キーで外部を叩かないため)。
Transport = Callable[[str, dict, dict, float], dict]


class ProviderError(RuntimeError):
    """プロバイダとの1往復が成立しなかった(HTTP・接続・応答の形)。

    **メッセージにAPIキーを含めない**。生成元は必ず `_redact()` を通す。
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


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
    #: プロバイダが返した生の本文。**Claudeはこれをそのまま返す**(thinkingブロックを壊さない)。
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
# APIキー(値を持ち歩かない)
# --------------------------------------------------------------------------------------
def repo_root() -> Path:
    """リポジトリ直下(`duocad/agent/providers.py` から2つ上)。"""
    return Path(__file__).resolve().parents[2]


def secrets_path() -> Path:
    """ローカル設定ファイルの場所(存在しなくてよい)。"""
    return repo_root() / SECRETS_FILENAME


def load_api_key(
    env_name: str = ANTHROPIC_API_KEY_ENV,
    *,
    environ: Optional[dict] = None,
    path: Optional[Path] = None,
) -> Optional[str]:
    """APIキーを読む。**環境変数が先、次にgitignore済みのローカル設定ファイル**。

    見つからなければ None(例外にしない —— 未設定は「使えない」であって異常ではない)。
    設定ファイルが壊れていても値の推測はせず None を返す。
    """
    env = os.environ if environ is None else environ
    value = (env.get(env_name) or "").strip()
    if value:
        return value

    secrets = secrets_path() if path is None else path
    try:
        raw = secrets.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    value = str(data.get(env_name) or "").strip()
    return value or None


def api_key_configured(
    env_name: str = ANTHROPIC_API_KEY_ENV,
    *,
    environ: Optional[dict] = None,
    path: Optional[Path] = None,
) -> bool:
    """**画面に出してよいのはこの2値だけ**(値は返さない)。"""
    return load_api_key(env_name, environ=environ, path=path) is not None


def _redact(text: str, secret: Optional[str]) -> str:
    """例外メッセージからAPIキーを消す。

    HTTPライブラリは不正なヘッダ値を例外文へそのまま載せる(例: 改行入りのキー)。
    外へ出る文字列は**必ず**ここを通す。
    """
    if not secret:
        return text
    return text.replace(secret, "***")


# --------------------------------------------------------------------------------------
# HTTP(素のurllib。streamingはv1の範囲外)
# --------------------------------------------------------------------------------------
def _origin(url: str) -> str:
    """エラーメッセージに載せてよい程度に短くしたURL(クエリを落とす)。"""
    return url.split("?", 1)[0]


def post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    """JSONをPOSTしてJSONを受け取る。失敗は `ProviderError`。

    **タイムアウトは必ず効かせる**(矛盾した指示でモデルが600秒返ってこなかった実測がある)。
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
        raise ProviderError(f"{_origin(url)} へ接続できない: {error.reason}") from None
    except TimeoutError:
        raise ProviderError(f"{_origin(url)} が {timeout:g}s で返らなかった") from None

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise ProviderError(
            f"{_origin(url)} の応答がJSONではない: {body[:MAX_ERROR_CHARS]}"
        ) from None
    if not isinstance(parsed, dict):
        raise ProviderError(f"{_origin(url)} の応答がJSONオブジェクトではない")
    return parsed


# --------------------------------------------------------------------------------------
# アダプタ
# --------------------------------------------------------------------------------------
class Provider:
    """プロバイダ1つ。**ループはこの3つしか使わない**(`name` / `model` / `complete`)。"""

    #: `anthropic` / `openai_compat`
    name = "provider"

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


def _text_of(turn: AssistantTurn) -> str:
    return turn.text or ""


class AnthropicProvider(Provider):
    """Claude API(`POST /v1/messages`・非streaming)。

    詰め替えは3つだけ:

    - ツール定義 … `descriptors()` の `inputSchema` を `input_schema` へ(他はそのまま)
    - ツール呼び出し … `content` の `tool_use` ブロック
    - ツール結果 … `role:"user"` の `tool_result` ブロック(`tool_use_id` で対応づける)

    **送らないもの**: `temperature` / `top_p` / `top_k` と `thinking.budget_tokens`
    (現行モデルは400を返す)。`thinking` は既定(適応的)のまま触らない ——
    明示的に切ると**ツール呼び出しが本文のテキストへ化ける**既知の失敗があり、
    ツールを回すこのループでは黙って何も実行されない事故になる。
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        base_url: str = DEFAULT_ANTHROPIC_BASE_URL,
        effort: Optional[str] = None,
        transport: Transport = post_json,
    ) -> None:
        if not api_key:
            raise ValueError("APIキーが未設定(環境変数かローカル設定ファイルに置く)")
        # 値は属性に持つが、`repr` にも例外にも出さない(下の __repr__ と _redact)。
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        #: `output_config.effort`(`low`|`medium`|`high`|`xhigh`|`max`)。None なら送らない。
        self.effort = effort
        self._transport = transport

    def __repr__(self) -> str:  # pragma: no cover - 表示だけ
        return f"AnthropicProvider(model={self.model!r}, base_url={self.base_url!r})"

    # ------------------------------------------------------------------ 整形
    def render_tools(self, tools: Sequence[dict]) -> list[dict]:
        """`descriptors()` → Claudeの `tools`(キー1つ違い)。"""
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
                # **生の本文をそのまま返す** —— thinkingブロックを落とすと次のターンが壊れる。
                content = turn.raw
                if not isinstance(content, list) or not content:
                    # 生の本文が無いとき(偽プロバイダ・別プロバイダの履歴)は組み直す。
                    # **tool_useブロックを落とさない** —— 落とすと次のtool_resultが
                    # 対応先を失って弾かれる。
                    content = []
                    text = _text_of(turn)
                    if text:
                        content.append({"type": "text", "text": text})
                    content.extend(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
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
        payload: dict = {
            "model": self.model,
            "max_tokens": int(max_tokens),
            "messages": self.render_messages(turns),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self.render_tools(tools)
        if self.effort:
            payload["output_config"] = {"effort": self.effort}
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
            text="\n".join(t for t in texts if t).strip(),
            tool_calls=tuple(calls),
            stop_reason=stop_reason,
            stop=_neutral_stop(stop_reason, bool(calls), {"max_tokens"}),
            usage=response.get("usage") if isinstance(response.get("usage"), dict) else {},
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
        payload = self.build_payload(
            system=system, turns=turns, tools=tools, max_tokens=max_tokens
        )
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        }
        response = self._call(f"{self.base_url}/v1/messages", payload, headers, timeout)
        return self.parse_reply(response)

    def _call(self, url: str, payload: dict, headers: dict, timeout: float) -> dict:
        """**外へ出る例外は必ずキーを消してから**投げ直す。"""
        try:
            return self._transport(url, payload, headers, timeout)
        except ProviderError as error:
            raise ProviderError(
                _redact(str(error), self._api_key), status=error.status
            ) from None
        except Exception as error:  # HTTPライブラリがヘッダ値を例外文へ載せる場合がある
            raise ProviderError(
                _redact(f"{type(error).__name__}: {error}", self._api_key)
            ) from None


class OpenAICompatProvider(Provider):
    """OpenAI互換(`POST /v1/chat/completions`・非streaming)。llama-server がこれ。

    詰め替えは3つだけ:

    - ツール定義 … `{"type":"function","function":{name, description, parameters}}` で包む
    - ツール呼び出し … `message.tool_calls`(引数は**JSON文字列**なので自分でparseする)
    - ツール結果 … `role:"tool"` + `tool_call_id` を**1件ずつ**

    APIキーは既定で不要(ローカル)。要るサーバのために `api_key` を受けるが、
    値の扱いはClaude側と同じ(出さない)。
    """

    name = "openai_compat"

    def __init__(
        self,
        *,
        model: str = "local",
        base_url: str = DEFAULT_OPENAI_COMPAT_BASE_URL,
        api_key: Optional[str] = None,
        transport: Transport = post_json,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        self._transport = transport

    def __repr__(self) -> str:  # pragma: no cover - 表示だけ
        return f"OpenAICompatProvider(model={self.model!r}, base_url={self.base_url!r})"

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
                message: dict = {"role": "assistant", "content": _text_of(turn) or None}
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
            "stream": False,
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
        return ProviderReply(
            text=(content or "").strip() if isinstance(content, str) else "",
            tool_calls=tuple(calls),
            stop_reason=finish_reason,
            stop=_neutral_stop(finish_reason, bool(calls), {"length"}),
            usage=response.get("usage") if isinstance(response.get("usage"), dict) else {},
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
            raise ProviderError(
                _redact(str(error), self._api_key), status=error.status
            ) from None
        except Exception as error:
            raise ProviderError(
                _redact(f"{type(error).__name__}: {error}", self._api_key)
            ) from None


def _neutral_stop(raw: str, has_tool_calls: bool, truncated: set) -> str:
    """プロバイダの停止理由 → ループが分岐する4語彙 + `other`。

    **打ち切りの判定がツール呼び出しより先**である。上限で切られた応答にも
    `tool_use` ブロック / `tool_calls` は載りうるが、その引数のJSONは**途中で
    切れている可能性がある** —— `tool_use` に丸めると、ループは切れた引数のまま
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
    environ: Optional[dict] = None,
    secrets: Optional[Path] = None,
    transport: Transport = post_json,
) -> Provider:
    """名前からプロバイダを1つ作る(アプリ内UI=タスクGが選ぶ入口)。

    APIキーは**呼び出し側から渡さない** —— ここが唯一の読み口である
    (画面から受け取る経路を作らないため)。
    """
    if kind == "anthropic":
        api_key = load_api_key(environ=environ, path=secrets)
        if not api_key:
            raise ProviderError(
                f"{ANTHROPIC_API_KEY_ENV} が未設定"
                f"(環境変数か {SECRETS_FILENAME} に置く。値は画面へ出さない)"
            )
        return AnthropicProvider(
            api_key,
            model=model or DEFAULT_ANTHROPIC_MODEL,
            base_url=base_url or DEFAULT_ANTHROPIC_BASE_URL,
            transport=transport,
        )
    if kind == "openai_compat":
        return OpenAICompatProvider(
            model=model or "local",
            base_url=base_url or DEFAULT_OPENAI_COMPAT_BASE_URL,
            transport=transport,
        )
    raise ValueError(f"未知のプロバイダ: {kind!r} (anthropic / openai_compat)")

"""llama-server の機構を、duocad が実際に送る本文で測る(モデルの賢さは測らない)。

    python tools/llm_probe.py --base-url http://127.0.0.1:8080 --scenario all --out probe.json

見るのは**トークン数**(処理した数 `prompt_n` と、キャッシュから使い回した数 `cache_n`)で、
秒数は添えるだけである —— トークン数は機種をまたいで同じになり、秒数はならない。
会話は台本で固定する(モデルの判断で分岐すると、条件どうしを比べられない)。`agent` だけは
本物の `AgentLoop` に缶詰のツール結果を返して回し、製品が送る通りの履歴で測る。

シナリオと、要るサーバ引数:

    facts    timings の欄と、固定プレフィックス(system + tools)のトークン数
    agent    AgentLoop の往復ごとの再利用
    warmup   先に system + tools だけ投げたときの、最初の依頼の処理量
    breakers キャッシュが外れる条件(ツールの順序・増減、状態の置き場所、別の会話の割り込み)
    trim     履歴の切り詰め方(滑らせる / まとめて落とす)の総処理量
    cancel   接続を閉じたら生成が止まるか
    save     スロットを保存する            (--slot-save-path)
    restore  保存したスロットを戻して測る   (--slot-save-path。サーバを立て直した後に)

2026-10-04 の実測と読み方は docs/AI_INTEGRATION.md「llama-server の機構の実測」。
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from time import monotonic, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from duocad import catalog
from duocad.agent import AgentLoop, Limits, OpenAICompatProvider
from duocad.agent.providers import (
    AssistantTurn,
    ProviderError,
    ToolCall,
    ToolResult,
    ToolResultTurn,
    UserTurn,
    post_json,
)

SYSTEM = catalog.agent_instructions()
TOOLS = catalog.tool_descriptors()
QUESTION = "body-0001 の面数を調べて。"
#: 生成を長引かせるための依頼(中断の検査用)。
LONG_TASK = "1から2000まで、数字を1つずつ改行して書き出して。途中で省略しない。"
SCENARIOS = ("facts", "agent", "warmup", "breakers", "trim", "cancel", "save", "restore")
JSON_HEADERS = {"content-type": "application/json"}


def encoded(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Server:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        parts = urllib.parse.urlsplit(self.base)
        self.host, self.port = parts.hostname, parts.port or 80

    def call(self, method: str, path: str, payload=None, timeout: float = 900.0):
        data = None if payload is None else encoded(payload)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=JSON_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, {"error": error.read().decode("utf-8", "replace")[:300]}
        except urllib.error.URLError as error:
            return 0, {"error": str(error.reason)}

    def chat(self, label: str, payload: dict) -> dict:
        started = monotonic()
        status, body = self.call("POST", "/v1/chat/completions", payload)
        if status != 200:
            raise SystemExit(f"{label}: HTTP {status} {body}")
        return record(label, body, monotonic() - started)

    def forget(self) -> None:
        """全スロットのキャッシュを捨てる(次のリクエストを cold にする)。"""
        status, slots = self.call("GET", "/slots", timeout=10)
        for slot in slots if status == 200 else [{"id": 0}]:
            erased, _ = self.call("POST", f"/slots/{slot['id']}?action=erase", {}, timeout=30)
            if erased != 200:
                # erase を持たないサーバ向け: 無関係な本文で上書きして共通の接頭辞を無くす。
                self.call(
                    "POST",
                    "/v1/chat/completions",
                    {"messages": [{"role": "user", "content": f"scrub {monotonic()}"}], "max_tokens": 1},
                )

    def busy(self) -> bool:
        status, slots = self.call("GET", "/slots", timeout=10)
        return status == 200 and any(slot.get("is_processing") for slot in slots)

    def wait_idle(self, limit: float = 240.0):
        """生成が止まるまでの秒数(止まらなければ None)。"""
        started = monotonic()
        while monotonic() - started < limit:
            if not self.busy():
                return round(monotonic() - started, 2)
            sleep(0.2)
        return None


def record(label: str, body: dict, wall: float) -> dict:
    timings = body.get("timings") or {}
    usage = body.get("usage") or {}
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    total = usage.get("prompt_tokens")
    processed = timings.get("prompt_n")
    cached = timings.get("cache_n")
    if cached is None and total is not None and processed is not None:
        cached = total - processed
    return {
        "label": label,
        "total": total,
        "cached": cached,
        "processed": processed,
        "prompt_ms": round(timings.get("prompt_ms") or 0.0),
        "generated": timings.get("predicted_n", usage.get("completion_tokens")),
        "generated_ms": round(timings.get("predicted_ms") or 0.0),
        "wall_s": round(wall, 2),
        "finish": choice.get("finish_reason"),
        "thinking_chars": len(message.get("reasoning_content") or ""),
        "tools": [call["function"]["name"] for call in message.get("tool_calls") or []],
    }


def canned(name: str, arguments: dict) -> dict:
    """缶詰のツール結果(形は本物に寄せるが、文書は無い)。"""
    if name == "describe_document":
        return {
            "units": "mm",
            "active_body_id": "body-0001",
            "bodies": [
                {
                    "body_id": f"body-{index:04d}",
                    "name": f"部品{index}",
                    "kind": "solid",
                    "visible": True,
                    "face_count": 6 + index,
                    "volume": 8000.0 + 125.5 * index,
                }
                for index in range(1, 7)
            ],
            "sketches": [],
            "parameters": [],
        }
    if name == "describe_shape":
        return {
            "body_id": arguments.get("body_id", "body-0001"),
            "face_count": 6,
            "edge_count": 12,
            "faces_by_kind": {"plane": 6},
            "volume": 8000.0,
            "bbox": {"min": [0.0, 0.0, 0.0], "max": [20.0, 20.0, 20.0], "size": [20.0, 20.0, 20.0]},
        }
    if name.endswith("_selection"):
        items = [{"kind": "edge", "body_id": "body-0003", "index": index} for index in range(12)]
        return {"selection": {"count": len(items), "items": items}}
    return {"ok": True, "tool": name}


class CannedTools:
    """`ToolSurface` を満たす偽物(版は動かさない = 人は何も触っていない)。"""

    def descriptors(self) -> list:
        return TOOLS

    def call(self, name: str, arguments: dict) -> dict:
        return canned(name, arguments)

    def call_and_seq(self, name: str, arguments: dict) -> tuple:
        return canned(name, arguments), None, 0

    def error_envelope(self, tool: str, error: Exception) -> dict:
        return {"code": "internal", "message": str(error), "tool": tool, "exception": type(error).__name__}

    def current_seq(self) -> int:
        return 0


def run_turns(index: int, state: str = "") -> list:
    """台本の1依頼ぶん(依頼 → describe_shape → 結果 → 返答)。"""
    body_id = f"body-{index:04d}"
    call = ToolCall(id=f"call_{index}", name="describe_shape", arguments={"body_id": body_id})
    result = json.dumps(canned("describe_shape", {"body_id": body_id}), ensure_ascii=False)
    return [
        UserTurn(f"{body_id} の面数を調べて。{state}"),
        AssistantTurn(text="", tool_calls=(call,)),
        ToolResultTurn((ToolResult(call_id=call.id, name=call.name, content=result, ok=True),)),
        AssistantTurn(text=f"{body_id} は面が6個です。"),
    ]


def history(indexes, state=None) -> list:
    turns: list = []
    for index in indexes:
        turns += run_turns(index, state(index) if state else "")
    return turns


def state_text(index: int) -> str:
    return (
        f"\n\n[現在の状態] ボディ{6 + index}個 / 選択{index}件 / アクティブ body-{index:04d} / "
        "表示中: body-0001, body-0002, body-0003, body-0004 / 単位 mm / 直前の操作: describe_shape"
    )


class Probe:
    def __init__(self, server: Server, extra: dict, slot_file: str, runs: int, keep: int) -> None:
        self.server = server
        self.provider = OpenAICompatProvider(base_url=f"{server.base}/v1")
        self.extra = extra
        self.slot_file = slot_file
        self.runs = runs
        self.keep = keep

    def payload(self, turns, *, system=None, tools=None, max_tokens: int = 1) -> dict:
        body = self.provider.build_payload(
            system=SYSTEM if system is None else system,
            turns=turns,
            tools=TOOLS if tools is None else tools,
            max_tokens=max_tokens,
        )
        return {**body, **self.extra}

    # ------------------------------------------------------------------ シナリオ
    def facts(self) -> list:
        server = self.server
        server.forget()
        status, body = server.call("POST", "/v1/chat/completions", self.payload([UserTurn(".")], tools=[]))
        if status != 200:
            raise SystemExit(f"facts: HTTP {status} {body}")
        bare = record("system のみ(ツールなし)", body, 0.0)
        bare["timing_keys"] = sorted((body.get("timings") or {}).keys())
        bare["usage_keys"] = sorted((body.get("usage") or {}).keys())
        server.forget()
        full = server.chat(f"system + ツール{len(TOOLS)}個", self.payload([UserTurn(".")]))
        full["tool_tokens"] = full["total"] - bare["total"]
        again = server.chat("同じ本文をもう一度", self.payload([UserTurn(".")]))
        return [bare, full, again, self._stream_fields()]

    def _stream_fields(self) -> dict:
        body = {**self.payload([UserTurn(".")], max_tokens=8), "stream": True, "stream_options": {"include_usage": True}}
        connection = http.client.HTTPConnection(self.server.host, self.server.port, timeout=300)
        connection.request("POST", "/v1/chat/completions", body=encoded(body), headers=JSON_HEADERS)
        chunks = []
        for raw in connection.getresponse():
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                chunks.append(json.loads(line[6:]))
        connection.close()
        with_timings = [index for index, chunk in enumerate(chunks) if chunk.get("timings")]
        with_usage = [index for index, chunk in enumerate(chunks) if chunk.get("usage")]
        return {
            "label": "stream: timings / usage を持つチャンク",
            "chunks": len(chunks),
            "timings_at": with_timings,
            "usage_at": with_usage,
            "timing_keys": sorted(chunks[with_timings[-1]]["timings"].keys()) if with_timings else [],
        }

    def agent(self) -> list:
        records: list = []
        position = {"run": 0, "turn": 0}

        def transport(url, body, headers, timeout):
            position["turn"] += 1
            started = monotonic()
            response = post_json(url, {**body, **self.extra}, headers, timeout)
            label = f"依頼{position['run']}・往復{position['turn']}"
            records.append(record(label, response, monotonic() - started))
            return response

        loop = AgentLoop(
            CannedTools(),
            OpenAICompatProvider(base_url=f"{self.server.base}/v1", transport=transport),
            limits=Limits(max_turns=5, max_tokens=768, request_timeout=600.0, max_total_seconds=1800.0),
        )
        self.server.forget()
        for prompt in (
            "文書の概要を調べて、ボディがいくつあるか教えて。",
            "body-0003 の形状を調べて、面の数を教えて。",
            "body-0003 の辺を全部選んで、半径2mmでフィレットして。",
        ):
            position["run"] += 1
            position["turn"] = 0
            run = loop.run(prompt)
            records[-1]["run_status"] = run.status
        previous = 0
        for item in records:
            # appended = 前回のプロンプトから伸びた分。processed がこれと同じなら「前回の
            # プロンプト末尾まで」再利用できている(作り直した assistant の文は処理し直す)。
            item["appended"] = item["total"] - previous
            previous = item["total"]
        return records

    def warmup(self) -> list:
        server = self.server
        server.forget()
        cold = server.chat("cold: 最初の依頼", self.payload([UserTurn(QUESTION)]))
        server.forget()
        warm = server.chat("ウォームアップ(system + tools + '.')", self.payload([UserTurn(".")]))
        first = server.chat("ウォームアップ後の最初の依頼", self.payload([UserTurn(QUESTION)]))
        return [cold, warm, first]

    def breakers(self) -> list:
        base = history(range(3))
        grown = base + run_turns(3)
        swap_tail = TOOLS[:-2] + [TOOLS[-1], TOOLS[-2]]
        swap_head = [TOOLS[1], TOOLS[0]] + TOOLS[2:]
        stated = history(range(3), state_text)
        other = {
            "messages": [
                {"role": "system", "content": "別のアプリの会話。"},
                {"role": "user", "content": "今日の予定を3行で。"},
            ],
            "max_tokens": 1,
        }
        cases = [
            ("同じ本文を再送", self.payload(base), self.payload(base), None),
            ("末尾に1依頼を足す(通常の伸び)", self.payload(base), self.payload(grown), None),
            ("ツールの順序: 末尾2個を入れ替え", self.payload(base), self.payload(grown, tools=swap_tail), None),
            ("ツールの順序: 先頭2個を入れ替え", self.payload(base), self.payload(grown, tools=swap_head), None),
            ("ツールを1個外す: 末尾", self.payload(base), self.payload(grown, tools=TOOLS[:-1]), None),
            ("ツールを1個外す: 先頭", self.payload(base), self.payload(grown, tools=TOOLS[1:]), None),
            (
                "状態を system に入れて毎回書き換える",
                self.payload(base, system=SYSTEM + state_text(2)),
                self.payload(grown, system=SYSTEM + state_text(3)),
                None,
            ),
            (
                "状態を各依頼の末尾に付ける",
                self.payload(stated),
                self.payload(stated + run_turns(3, state_text(3))),
                None,
            ),
            ("別の会話が割り込む", self.payload(base), self.payload(grown), other),
        ]
        records = []
        for label, first, second, between in cases:
            self.server.forget()
            self.server.chat(label + " (下地)", first)
            if between is not None:
                self.server.chat(label + " (割り込み)", between)
            records.append(self.server.chat(label, second))
        return records

    def trim(self) -> list:
        """`keep` 依頼を超えたら古い側を落とす。落とし方だけを変えて総処理量を比べる。"""
        records = []
        for policy in ("切らない", "滑らせる(毎回1依頼落とす)", "まとめて落とす(半分)"):
            self.server.forget()
            kept: list = []
            steps = []
            for index in range(self.runs):
                kept.append(index)
                if policy.startswith("滑らせる") and len(kept) > self.keep:
                    kept = kept[1:]
                if policy.startswith("まとめて") and len(kept) > self.keep:
                    kept = kept[self.keep // 2 :]
                steps.append(self.server.chat(f"{policy} #{index}", self.payload(history(kept))))
            records.append(
                {
                    "label": policy,
                    "requests": len(steps),
                    "processed_sum": sum(step["processed"] for step in steps),
                    "prompt_ms_sum": sum(step["prompt_ms"] for step in steps),
                    "max_total": max(step["total"] for step in steps),
                    "processed_each": [step["processed"] for step in steps],
                }
            )
        return records

    def cancel(self) -> list:
        server = self.server
        body = {"model": "local", "messages": [{"role": "user", "content": LONG_TASK}], "max_tokens": 1500}
        headers = JSON_HEADERS
        hold = 3.0
        server.forget()
        full = server.chat("最後まで生成(比較の基準)", body)
        records = [full]

        def measured(label: str, was_busy: bool) -> dict:
            return {"label": label, "busy_before_close": was_busy, "idle_after_s": server.wait_idle()}

        server.forget()
        connection = http.client.HTTPConnection(server.host, server.port, timeout=600)
        connection.request("POST", "/v1/chat/completions", body=encoded(body), headers=headers)
        sleep(hold)
        was_busy = server.busy()
        connection.close()
        records.append(measured("非ストリーミング: 応答を待たずに接続を閉じる", was_busy))

        server.forget()
        connection = http.client.HTTPConnection(server.host, server.port, timeout=600)
        connection.request("POST", "/v1/chat/completions", body=encoded({**body, "stream": True}), headers=headers)
        response = connection.getresponse()
        started = monotonic()
        while monotonic() - started < hold:
            response.readline()
        was_busy = server.busy()
        connection.close()
        records.append(measured("ストリーミング: 途中で接続を閉じる", was_busy))

        server.forget()
        try:
            post_json(f"{server.base}/v1/chat/completions", body, {}, hold)
        except ProviderError as error:
            outcome = type(error).__name__
        else:
            outcome = "返ってきた"
        item = measured("製品の経路: post_json が待ち時間切れ", server.busy())
        item["outcome"] = outcome
        records.append(item)
        return records

    def save(self) -> list:
        server = self.server
        server.forget()
        warm = server.chat("ウォームアップ(system + tools + '.')", self.payload([UserTurn(".")]))
        started = monotonic()
        status, body = server.call("POST", "/slots/0?action=save", {"filename": self.slot_file})
        saved = {"label": "スロットを保存", "status": status, "wall_s": round(monotonic() - started, 2), **body}
        return [warm, saved]

    def restore(self) -> list:
        server = self.server
        started = monotonic()
        status, body = server.call("POST", "/slots/0?action=restore", {"filename": self.slot_file})
        restored = {"label": "スロットを復元", "status": status, "wall_s": round(monotonic() - started, 2), **body}
        first = server.chat("復元後の最初の依頼", self.payload([UserTurn(QUESTION)]))
        return [restored, first]


def show(name: str, records: list) -> None:
    print(f"\n== {name}")
    for item in records:
        if "processed" in item and "total" in item:
            line = (
                f"  {item['label']:<44} total={item['total']!s:>6} cached={item['cached']!s:>6} "
                f"processed={item['processed']!s:>6} prompt_ms={item['prompt_ms']!s:>7} "
                f"gen={item['generated']!s:>5} wall={item['wall_s']!s:>7}s"
            )
            rest = {
                key: value
                for key, value in item.items()
                if key in ("appended", "tools", "thinking_chars", "finish", "run_status", "tool_tokens") and value not in (None, [], 0)
            }
            print(line + (f"  {rest}" if rest else ""))
        else:
            print("  " + json.dumps(item, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--scenario", default="all", help=f"カンマ区切り。all = {','.join(SCENARIOS[:6])}")
    parser.add_argument("--out", default="")
    parser.add_argument("--extra-json", default="{}", help="全リクエストの本文へ足す欄(例: 思考を切る指定)")
    parser.add_argument("--slot-file", default="duocad_prefix.bin")
    parser.add_argument("--runs", type=int, default=18, help="trim で送る依頼の数(文脈長に収まる数にする)")
    parser.add_argument("--keep", type=int, default=6, help="trim で残す依頼の数")
    args = parser.parse_args()

    server = Server(args.base_url)
    status, props = server.call("GET", "/props", timeout=10)
    if status != 200:
        raise SystemExit(f"{args.base_url} に llama-server が居ない(HTTP {status})")
    settings = props.get("default_generation_settings") or {}
    about = {
        "model": Path(str(props.get("model_path") or "")).name,
        "build": props.get("build_info"),
        "n_ctx": settings.get("n_ctx"),
        "slots": props.get("total_slots"),
        "extra": json.loads(args.extra_json),
        "tools": len(TOOLS),
    }
    print(json.dumps(about, ensure_ascii=False))

    probe = Probe(server, about["extra"], args.slot_file, args.runs, args.keep)
    names = SCENARIOS[:6] if args.scenario == "all" else tuple(args.scenario.split(","))
    results = {}
    for name in names:
        if name not in SCENARIOS:
            raise SystemExit(f"未知のシナリオ: {name} ({', '.join(SCENARIOS)})")
        results[name] = getattr(probe, name)()
        show(name, results[name])
        if args.out:
            Path(args.out).write_text(
                json.dumps({"server": about, "scenarios": results}, ensure_ascii=False, indent=1), encoding="utf-8"
            )


if __name__ == "__main__":
    main()

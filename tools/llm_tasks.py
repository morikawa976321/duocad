"""固定の課題集で、アプリ内AIの往復回数を測る(変更の前後を比べるための目盛り)。

    python tools/llm_tasks.py --base-url http://127.0.0.1:8080 --repeat 3 --out before.json

**本物の `AgentLoop` と本物の道具**で回す(板と柱の2ボディを置いた文書を課題ごとに作り直す)。
見るのは依頼ごとの往復回数・読んだ量・書いた量・成否で、どれも回数とトークン数なので機種を
またいで比べられる(秒数は添えるだけ)。**成否は文書の状態で判定する**(モデルの言い分ではなく)。

時間の上限は製品より長く取ってある —— 測りたいのは往復であって、遅い機種で時間切れになること
ではない。機構(キャッシュ・中断・スロット)の計測は `tools/llm_probe.py`。
"""
from __future__ import annotations

import argparse
import atexit
import json
import re
import shutil
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from time import monotonic
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from duocad.agent import AgentLoop, Limits, OpenAICompatProvider
from duocad.agent.loop import FIRST_LOOK
from duocad.mcp_server import DocumentTools
from duocad.server import Session

LIMITS = Limits(max_turns=12, max_tokens=1024, request_timeout=600.0, max_total_seconds=900.0)
#: 板(40×30×10)と柱(10×10×25)。面はどちらも6、縦の辺は4本ずつ。
PLATE = ((0.0, 0.0), 40.0, 30.0, 10.0)
POST = ((60.0, 0.0), 10.0, 10.0, 25.0)


@dataclass
class World:
    tools: DocumentTools
    plate: str
    post: str
    #: 板の上面(+Z)の面番号。
    plate_top: int

    def bodies(self) -> dict:
        return {body["body_id"]: body for body in self.tools.call("describe_document", {})["bodies"]}

    def selection(self) -> list:
        return self.tools.call("get_selection", {})["selection"]["items"]


def build_world() -> World:
    # 保存先を渡さない Session は、操作のたびの自動保存でリポジトリの models/ へ untitled-<n>.json を溜める。
    scratch = Path(tempfile.mkdtemp(prefix="duocad-llm-tasks-"))
    atexit.register(shutil.rmtree, scratch, True)
    session = Session(0.2, models_dir=scratch / "models", exports_dir=scratch / "export")
    for corner, width, depth, height in (PLATE, POST):
        sketch_id = session.sketch_create("XY")["sketch_id"]
        session.add_rectangle(sketch_id, list(corner), width, depth)
        regions = len(session.state_meta()["sketches"][-1]["regions"])
        session.clear_selection()
        session.modify_selection([{"sketch": sketch_id, "index": regions}], [])
        session.extrude(height, mode="new")
        session.clear_selection()
    tools = DocumentTools(session)
    plate, post = (body["body_id"] for body in tools.call("describe_document", {})["bodies"])
    upward = {"kind": "face", "body": plate, "where": [{"op": "normal_dot_gt", "axis": "Z", "value": 0.9}]}
    (top,) = tools.call("set_selection", {"predicate": upward})["selection"]["items"]
    session.clear_selection()
    return World(tools, plate, post, top["index"])


def said(runs: list, pattern: str) -> bool:
    text = unicodedata.normalize("NFKC", runs[-1].text).replace(",", "")
    return re.search(pattern, text) is not None


def face_counts(world: World) -> tuple:
    bodies = world.bodies()
    return bodies[world.plate]["face_count"], bodies[world.post]["face_count"]


@dataclass
class Task:
    name: str
    prompts: tuple
    check: Callable[[World, list], bool]


BIG = "体積がいちばん大きいボディ"
ROUND_VERTICAL = f"{BIG}の、Z軸に平行な辺を4本とも半径2mmで丸めて。"
TASKS = (
    Task("数える", ("文書にボディがいくつあるか教えて。",), lambda world, runs: said(runs, r"2|二")),
    Task("面の数", (f"{BIG}の面の数を教えて。",), lambda world, runs: said(runs, r"6|六")),
    Task(
        "体積の合計",
        ("2つのボディの体積の合計を教えて。",),
        lambda world, runs: said(runs, r"14500|14499\.9|1\.45\s*[×x*]\s*10"),
    ),
    Task(
        "上面を選ぶ",
        (f"{BIG}の上面(+Zを向いた面)だけを選択して。",),
        lambda world, runs: [(item["body"], item["kind"], item["index"]) for item in world.selection()]
        == [(world.plate, "face", world.plate_top)],
    ),
    Task("縦の辺を丸める", (ROUND_VERTICAL,), lambda world, runs: face_counts(world) == (10, 6)),
    Task(
        "隠す",
        ("体積が小さいほうのボディを非表示にして。",),
        lambda world, runs: [world.bodies()[body]["visible"] for body in (world.plate, world.post)] == [True, False],
    ),
    Task(
        "箱を作る",
        ("XY平面に新しいスケッチを作り、(100, 0) を角にして幅20mm・奥行き20mmの長方形を描き、15mm押し出して新しいボディにして。",),
        lambda world, runs: sorted(round(body["volume"]) for body in world.bodies().values()) == [2500, 6000, 12000],
    ),
    Task(
        "続きの依頼",
        (ROUND_VERTICAL, "体積が小さいほうのボディにも同じようにして。"),
        lambda world, runs: face_counts(world) == (10, 10),
    ),
    Task(
        "寸法を言わない依頼",
        (f"{BIG}の角を、いい感じに丸めておいて。",),
        lambda world, runs: face_counts(world) == (6, 6),
    ),
)


def run_task(task: Task, provider: OpenAICompatProvider) -> dict:
    world = build_world()
    loop = AgentLoop(world.tools, provider, limits=LIMITS, first_look=FIRST_LOOK)
    started = monotonic()
    runs = [loop.run(prompt) for prompt in task.prompts]
    usage: dict = {}
    for run in runs:
        for key, value in run.usage.items():
            usage[key] = usage.get(key, 0) + value
    try:
        passed = bool(task.check(world, runs))
    except Exception as error:
        passed = False
        usage["check_error"] = f"{type(error).__name__}: {error}"
    return {
        "task": task.name,
        "passed": passed,
        "statuses": [run.status for run in runs],
        "round_trips": sum(run.turns for run in runs),
        "tools": [("" if outcome.ok else "✗") + outcome.name for run in runs for outcome in run.tool_calls],
        "failures": [
            f"{outcome.name} {json.dumps(outcome.arguments, ensure_ascii=False)[:120]} -> "
            f"{outcome.result['error'].get('code')}: {str(outcome.result['error'].get('message'))[:160]}"
            for run in runs
            for outcome in run.tool_calls
            if not outcome.ok
        ],
        "read": usage.get("prompt_n"),
        "reused": usage.get("cache_n"),
        "written": usage.get("predicted_n", usage.get("completion_tokens")),
        "wall_s": round(monotonic() - started, 1),
        "answer": runs[-1].text[:200],
        "detail": [run.detail for run in runs if run.detail],
    }


def main() -> None:
    # 失敗した呼び出しの印「✗」は cp932 に無い。標準出力がパイプだと日本語 Windows では cp932 で書かれ、落ちる。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--repeat", type=int, default=3, help="課題ごとの試行回数(モデルの揺れを見るため)")
    parser.add_argument("--only", default="", help="課題名の一部(カンマ区切り)。空なら全部")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    provider = OpenAICompatProvider(base_url=f"{args.base_url.rstrip('/')}/v1")
    warm = AgentLoop(build_world().tools, provider, limits=LIMITS).prepare()
    if "error" in warm:
        raise SystemExit(f"{args.base_url} の llama-server に繋がらない: {warm['error']}")
    wanted = [part for part in args.only.split(",") if part]
    tasks = [task for task in TASKS if not wanted or any(part in task.name for part in wanted)]

    records = []
    for task in tasks:
        for _ in range(args.repeat):
            item = run_task(task, provider)
            records.append(item)
            mark = "○" if item["passed"] else "×"
            print(
                f"{mark} {item['task']:<10} 往復={item['round_trips']:>2} 読={item['read']!s:>5} "
                f"書={item['written']!s:>5} {item['wall_s']:>6}s {'/'.join(item['statuses'])}  {' '.join(item['tools'])}",
                flush=True,
            )
            if args.out:
                Path(args.out).write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n課題          成功   往復(平均)  読(平均)  書(平均)")
    for task in tasks:
        mine = [item for item in records if item["task"] == task.name]
        averages = [mean(item[key] or 0 for item in mine) for key in ("round_trips", "read", "written")]
        print(
            f"{task.name:<10} {sum(item['passed'] for item in mine):>3}/{len(mine)}  {averages[0]:>8.1f} "
            f"{averages[1]:>9.0f} {averages[2]:>9.0f}"
        )
    print(
        f"合計          {sum(item['passed'] for item in records):>3}/{len(records)}  "
        f"{mean(item['round_trips'] for item in records):>8.1f}"
    )


if __name__ == "__main__":
    main()

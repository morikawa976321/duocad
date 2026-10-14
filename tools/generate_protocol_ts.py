"""`duocad/catalog.py` の宣言から `web/src/generated/protocol.ts` を作る。

    python tools/generate_protocol_ts.py          書き出す
    python tools/generate_protocol_ts.py --check  今のファイルが宣言と一致しているかだけを見る(違えば終了コード1)

宣言(コマンド名・分類・WebSocket が受け取る引数・verb が応答に載せる値・状態配信のキー)を
TypeScript の型へ写す。JSON の境界のずれは `tsc` が検出できないので、手で写さず生成する。
生成物は git に入れる(画面のビルドに Python を要らせないため)ので、宣言を変えたらここを走らせる
(走らせ忘れは `tests/test_protocol.py` が落とす)。

`duocad/catalog.py` はパッケージを経由せずに読む(`duocad/__init__.py` は OCP まで import するが、宣言は純データ)。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "web" / "src" / "generated" / "protocol.ts"

#: JSON Schema の型名 -> TypeScript の型。`integer` は JSON では数と区別が付かない。配列・オブジェクトの
#: 中身は宣言が持たないので、配列は `unknown[]`(座標の `[number, number]` が入る)、オブジェクトは
#: `object`(参照の `{body, index}` が入る。`Record<string, unknown>` にすると interface を代入できない)。
_TS_TYPES = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
    "array": "unknown[]",
    "object": "object",
    "null": "null",
}


def _catalog():
    name = "duocad_catalog_static"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "duocad" / "catalog.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # `@dataclass` が型注釈の解決に `sys.modules` を引くので、exec より先
    spec.loader.exec_module(module)
    return module


def _ts_type(schema_type) -> str:
    names = [schema_type] if isinstance(schema_type, str) else list(schema_type)
    return " | ".join(_TS_TYPES[name] for name in names)


def _arguments(command) -> str:
    schema = command.ws_schema()
    required = set(schema.get("required") or [])
    fields = [
        f"{name}{'' if name in required else '?'}: {_ts_type(spec['type'])}"
        for name, spec in schema["properties"].items()
    ]
    return "{ " + "; ".join(fields) + " }" if fields else "Record<string, never>"


def _member(base: str, tag: str, value: str, results) -> str:
    fields = [f"{tag}: '{value}'"] + [f"{result.name}: {result.type}" for result in results]
    return f"  | ({base} & {{ " + "; ".join(fields) + " })"


def render() -> str:
    catalog = _catalog()
    commands = [command for command in catalog.COMMANDS if command.ws != "none"]
    verbs = [command for command in commands if command.returns]

    lines = [
        "// 生成物 —— **手で直さない**。正は `duocad/catalog.py` の宣言で、",
        "// 作り直すのは `python tools/generate_protocol_ts.py`(走らせ忘れは `tests/test_protocol.py` が落とす)。",
        "import type { CommandState, ModeState, SelectionState } from '../session'",
        "",
        "/** WebSocket から踏めるコマンド -> 分類(`catalog.Command.category`)。 */",
        "export const COMMAND_CATEGORY = {",
        *[f"  {command.name}: '{command.category}'," for command in commands],
        "} as const",
        "",
        "export type CommandName = keyof typeof COMMAND_CATEGORY",
        "",
        "/** コマンドが受け取る引数の名前(宣言の全引数)。画面が共有の作業空間へ出す引数をこれで絞る(`CommandHost.svelte`)。 */",
        "export const COMMAND_PARAMS: Record<CommandName, readonly string[]> = {",
        *[f"  {command.name}: [{', '.join(repr(p.name) for p in command.params)}]," for command in commands],
        "}",
        "",
        "/** コマンドが WebSocket で受け取る引数(`cmd` と `client_op` を除く。`?` は省略できる)。 */",
        "export interface CommandArguments {",
        *[f"  {command.name}: {_arguments(command)}" for command in commands],
        "}",
        "",
        "/**",
        " * verb の適用に共通するフィールド。`change?: undefined` は実行時には存在しない ——",
        " * `applied.change === 'selection'` のように、`verb` で絞る前に触れるようにするための置き石。",
        " */",
        "interface AppliedVerbBase {",
        "  seq: number",
        "  client_op: number | null",
        "  change?: undefined",
        "}",
        "",
        "/** 変更イベント(verb ではない。操作ログに載らない)に共通するフィールド。 */",
        "interface AppliedChangeBase {",
        "  client_op: number | null",
        "  verb?: undefined",
        "  seq?: undefined",
        "}",
        "",
        "/** 操作を出した画面への確認応答。`verb` か `change` で絞り込むと、その応答が載せる値が見える。 */",
        "export type Applied =",
        *[_member("AppliedVerbBase", "verb", command.name, command.returns) for command in verbs],
        *[
            _member("AppliedChangeBase", "change", change, results)
            for change, results in catalog.CHANGE_EVENTS.items()
        ],
        "",
        "/** 状態配信(`ServerState`)が持つキー。 */",
        "export type ServerStateKey =",
        *[f"  | '{key}'" for key in catalog.STATE_KEYS],
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current.replace("\r\n", "\n") != text:
            print(f"{OUTPUT} が宣言と一致していない(python tools/generate_protocol_ts.py を走らせること)")
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"書き出した: {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

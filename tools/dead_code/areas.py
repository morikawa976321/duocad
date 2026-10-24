"""`sweep.py` / `ts_sweep.mjs` / `dangling.py` の出力を担当領域ごとの brief(JSON)に切り分ける。

    .venv/Scripts/python.exe tools/dead_code/areas.py [出力フォルダ]     # 既定は output/dead_code

`briefs/<領域>.json` と `areas.json` を書く。brief は `workflow.js` の調査員が最初に読む(担当ファイル・その領域の動的呼び出しの癖・機械候補)。
領域の割当て(`area_of`)と `HINTS` は duocad の今の構成に依存するので、フォルダを動かしたら直す。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SP = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "dead_code"
(SP / "briefs").mkdir(parents=True, exist_ok=True)

cand = json.loads((SP / "candidates.json").read_text(encoding="utf-8"))
tsc = json.loads((SP / "ts_candidates.json").read_text(encoding="utf-8"))
dangling = json.loads((SP / "dangling.json").read_text(encoding="utf-8"))
files = [f for f in subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0") if f]


def nlines(f: str) -> int:
    try:
        return len((ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


CODE_EXT = (".py", ".ts", ".svelte", ".mjs", ".js")
code_files = [f for f in files if f.endswith(CODE_EXT) and not f.startswith(("docs/", "tests/fixtures/"))]

KERNEL = {"kernel", "kernel_brep", "kernel_chains", "kernel_extrude", "kernel_meshing", "kernel_shell", "kernel_sketch",
          "mesh_geometry", "boundary_check", "mesh_intersections", "meshbody", "query"}
TOPOPT_SEARCH = {"binary_search", "design_space", "complete", "acceptance", "final_check", "final_width", "finalize",
                 "min_width", "neck", "neck_persistence", "thin_voids", "material"}
WEB_VIEWPORT = {"viewport", "viewportTypes", "viewportConstants", "viewportMaterials", "planeGrid", "gridPlanes", "gridScale",
                "originGizmo", "viewCube", "viewCubeOverlay", "sectionView", "selectionBand", "vertexMarkers", "ghostLayer",
                "faceCulling", "bodyMesh", "sceneBatch", "screenPick", "planeMath", "textSprite", "thumbnail",
                "sketchCursorOverlay", "measureAnnotation", "optimizeAnnotations", "optimizeDebug"}
WEB_COMMANDS_EXTRA = {"ghostArgs", "sketchDraws", "sketchLine", "expression"}


def area_of(f: str) -> str | None:
    p = Path(f)
    stem = p.stem
    if f.startswith("duocad/"):
        rest = f[len("duocad/"):]
        if rest in ("catalog.py", "session.py", "view.py"):
            return "catalog_session_view"
        if rest.startswith("verbs/"):
            return "verbs"
        if "/" not in rest and stem in KERNEL:
            return "kernel"
        if rest.startswith(("net/", "agent/")) or rest in ("mcp_server.py", "server.py", "channel.py", "launcher.py",
                                                            "local_settings.py", "paths.py"):
            return "net_mcp_agent"
        if rest in ("optimize.py", "optimize_run.py", "optimize_steps.py"):
            return "optimize"
        if rest.startswith("topopt/"):
            return "topopt_search" if stem in TOPOPT_SEARCH else "topopt_numeric"
        return "core_model"
    if f.startswith("tools/thread_sim/"):
        return "thread_sim"
    if f.startswith("tools/"):
        return "tools"
    if f.startswith("web/src/"):
        if f.startswith("web/src/commands/") or stem in WEB_COMMANDS_EXTRA:
            return "web_commands"
        if f.endswith(".svelte") or f.endswith(".svelte.ts"):
            return "web_app"
        if stem in WEB_VIEWPORT:
            return "web_viewport"
        return "web_session_misc"
    if f.startswith("web/scripts/") or (f.startswith("tests/") and f.endswith(".mjs")):
        return "web_scripts"
    if f.startswith("tests/"):
        if stem.endswith("_support") or stem == "conftest":
            return "tests_support"
        return "tests_body_a" if stem < "test_n" else "tests_body_b"
    if f.startswith("topopt_checks/idle_validation/"):
        return "checks_idle"
    if f.startswith(("topopt_checks/stress_gate/", "topopt_checks/speed_")):
        return "checks_stress_speed"
    if f.startswith("topopt_checks/"):
        return "checks_root"
    return None


META = {
    "core_model": ("コア: 文書・操作ログ・スケッチ・選択・述語", "document / oplog / oplog_upgrade / sketch / region / parameters / selection / predicates / speculative / naming / resolution / workspace / measure / entry / io_step / svg_import"),
    "catalog_session_view": ("コア: catalog・Session・DocumentView", "verb の単一宣言 catalog.py(2453行)、Session、DocumentView"),
    "verbs": ("verb 層", "duocad/verbs/*(1 verb 1 モジュール)"),
    "kernel": ("幾何カーネル層", "kernel*.py / mesh_geometry / boundary_check / mesh_intersections / meshbody / query"),
    "net_mcp_agent": ("通信・MCP・アプリ内AI", "net/ / mcp_server / agent/ / server(再輸出) / channel / launcher / local_settings / paths"),
    "optimize": ("形状最適化の配線", "optimize.py / optimize_run.py / optimize_steps.py"),
    "topopt_search": ("topopt: 探索・塊・最終化", "binary_search / design_space / complete / acceptance / final_* / finalize / min_width / neck* / thin_voids / material"),
    "topopt_numeric": ("topopt: 数値・FEM・ボクセル", "anchor / voxelize / fem / gpu_cg / mesh / stress / trace / __init__ / output"),
    "tools": ("tools/ のスクリプト", "bench_server_ops / check_code_unchanged / generate_protocol_ts / llm_probe / llm_tasks / measure_bloat / upgrade_models / thread_prototype / stable/"),
    "thread_sim": ("tools/thread_sim", "ねじのシミュレーション一式"),
    "web_viewport": ("web: three.js の囲いと描画部品", "viewport*.ts / planeGrid / gridPlanes / viewCube* / sectionView / bodyMesh ほか"),
    "web_app": ("web: Svelte 画面", "App.svelte ほか *.svelte と *.svelte.ts"),
    "web_commands": ("web: コマンド層", "web/src/commands/* と ghostArgs / sketchDraws / sketchLine / expression"),
    "web_session_misc": ("web: セッション・エンティティ表・生成物・その他の ts", "session.ts / entityKinds.ts / generated/protocol.ts / wire.ts ほか"),
    "web_scripts": ("web/scripts と tests/*.mjs", "smoke*.mjs / harness / bench / shoot / eval_*.mjs"),
    "tests_support": ("tests: 支援モジュールと conftest", "*_support.py / conftest.py と fixture"),
    "tests_body_a": ("tests: テスト本体 前半(test_a〜test_m)", "tests/test_a*〜test_m*.py(非テスト関数の死んだヘルパ・永続スキップ・消えた機能のテスト)"),
    "tests_body_b": ("tests: テスト本体 後半(test_n〜test_z)", "tests/test_n*〜test_z*.py(同上)"),
    "checks_root": ("topopt_checks 直下", "check_*.py(受入検査)と measure_* / analyze_* / verify_* ほかの単発スクリプト"),
    "checks_idle": ("topopt_checks/idle_validation", "idle_validation の検証スクリプト一式(72ファイル)"),
    "checks_stress_speed": ("topopt_checks/stress_gate と speed_20261011", "応力ゲートと速度検証のスクリプト"),
}

areas: dict[str, list[str]] = {k: [] for k in META}
for f in code_files:
    a = area_of(f)
    if a:
        areas[a].append(f)
# 設定・依存
areas["deps_config"] = [f for f in ("pyproject.toml", "web/package.json", ".mcp.json", "web/vite.config.ts", "web/svelte.config.js", "web/tsconfig.json") if f in files]
META["deps_config"] = ("依存と設定", "pyproject.toml / web/package.json / .mcp.json / web の設定。宣言された依存のうち import されないもの、設定ファイルの死んだ項目")


# tests の未使用 fixture
def unused_fixtures() -> list[dict]:
    fx: dict[str, tuple[str, int, bool]] = {}
    used: set[str] = set()
    trees = {}
    for f in files:
        if not (f.startswith("tests/") and f.endswith(".py")):
            continue
        try:
            trees[f] = ast.parse((ROOT / f).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
    for f, t in trees.items():
        for n in ast.walk(t):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in n.decorator_list:
                    base = d.func if isinstance(d, ast.Call) else d
                    nm = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                    if nm == "fixture":
                        autouse = isinstance(d, ast.Call) and any(k.arg == "autouse" for k in d.keywords)
                        fx[n.name] = (f, n.lineno, autouse)
                for a in n.args.args + n.args.kwonlyargs + n.args.posonlyargs:
                    used.add(a.arg)
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                used.add(n.value)
    return [{"file": f, "line": ln, "fixture": name} for name, (f, ln, auto) in fx.items() if name not in used and not auto]


unused_fx = unused_fixtures()

HINTS = {
    "verbs": "verb は `@entry(\"verb名\")`(duocad/entry.py)で登録され `Session.apply` が名前で引く。`register_verb()` で oplog のレジストリ(do/undo)にも入る。`duocad/verbs/__init__.py` の import と `__all__` は登録のために必須。catalog.COMMANDS の宣言と1対1。",
    "catalog_session_view": "`Session` には `entries()` から `setattr(Session, verb, ...)` でメソッドが動的に生える(session.py 末尾)。ws.py は `getattr(self.session, command)` で呼ぶ。`catalog.Command.preview_session` などの文字列がメソッド名。`catalog.py` の宣言から `web/src/generated/protocol.ts` が生成される(tools/generate_protocol_ts.py)。",
    "net_mcp_agent": "McpServer は `catalog.tool_names()` の名前と同じ綴りのメソッドを `getattr(self, name)` で引く。JSON-RPC のメソッド名は文字列。`WebHandler.do_*` は http.server が名前で呼ぶ。`duocad/server.py` の `noqa: F401` の再輸出は意図的(tests と mcp_server が名前を取る)で消さない。",
    "kernel": "kernel* は OCP を直接叩く層。`getattr(source, getter)` や `setattr(target, name, ...)` での動的アクセスが kernel.py / kernel_meshing.py にある。",
    "core_model": "`duocad/__init__.py` の import は verb の登録(`register_verb`)のために必須。`oplog_upgrade` は古い保存形式の読み込み時移行。`Operation.args` のキーは JSON の保存形式で、読み手が無くても過去ファイルとの互換のため残っていることがある。",
    "optimize": "`getattr(setup, role)`(role は 'preserve'/'exclude' などの文字列)で動的に属性を引く箇所がある。WebSocket の protocol 口(ws.py)から呼ばれる。",
    "web_app": "Svelte のテンプレートからの参照は ts の識別子検索では見えない(`{foo}` `onclick={foo}` `bind:` `class:` など)。`testHooks.svelte.ts` は `window.__duocad` へ生やすフックで、web/scripts/smoke*.mjs と tests/ が使う。",
    "web_commands": "コマンドの引数仕様は `COMMAND_PARAMS` などのテーブルから文字列キーで引かれる。`needsEnter` などは tests/test_ui_command_wiring.py と tests/eval_command_defs.mjs が固定する。",
    "web_viewport": "viewport.ts は three.js を囲う狭い `Viewport` インタフェースを返す(viewportTypes.ts)。メンバの呼び元は App.svelte / commands / testHooks にある。",
    "web_session_misc": "`generated/protocol.ts` は catalog.py からの生成物で手で直さない。session.ts の payload 型のフィールドはサーバ応答の形の宣言で、読み手が無い=即デッドとは限らないが、UI が読まないフィールドは候補。",
    "web_scripts": "`web/scripts/harness.mjs` の `runSmoke` が骨格。smoke*.mjs は README §3 の1本ずつ/全部の実行で使われる。tests/eval_*.mjs は tests/test_ts_evaluators.py が走らせる。",
    "tests_support": "pytest の fixture は引数名で解決される(未使用判定は引数名と文字列を見た)。",
    "tests_body_a": "テスト関数(test_*)自体は pytest が拾うので候補ではない。見るのは非テストの補助関数・常に skip されるテスト・消えた機能を叩いているテスト。",
    "tests_body_b": "テスト関数(test_*)自体は pytest が拾うので候補ではない。見るのは非テストの補助関数・常に skip されるテスト・消えた機能を叩いているテスト。",
    "checks_root": "README §3 に書かれた check_*.py は受入検査(最後の行が `OK:`)。それ以外の measure_*/analyze_* は単発の実験スクリプトで、docs/ が結論と再現コマンドを持つ。docs/ が再現コマンドとして挙げているものは生きている扱い。",
    "checks_idle": "idle_validation は docs/topopt_idle_validation/ の実験の再現スクリプト一式。run_*/build_*/analyze_* の番号付き段階がある。docs が参照しているか、後続の段階が入力に使っているかで生死を見る。",
    "checks_stress_speed": "stress_gate と speed_20261011 は応力ゲートと速度検証の実験スクリプト。docs/ の記述と他スクリプトからの参照で生死を見る。",
    "thread_sim": "tools/thread_sim は docs/THREAD_SIM.md が使い方を持つ。`study.py` などから段階的に呼ばれる。docs の再現手順(7章)に出るコマンドは生きている扱い。",
    "tools": "tools/*.py は単独実行のスクリプト。`generate_protocol_ts.py` は tests/test_protocol.py が鮮度検査に使う。README/docs が手順として挙げるものは生きている扱い。",
    "topopt_search": "`SafetyReport` や `PeelStats` のようなデータクラスのフィールドは `__dataclass_fields__` / asdict / trace の `_encode(getattr(...))` で動的に読まれることがある(binary_search.py:632, trace.py:217)。",
    "topopt_numeric": "trace.py は dataclass のフィールドを `getattr` で全部書き出す。fem.py / gpu_cg.py は CuPy があるときだけ走る経路を持つ(importlib.util.find_spec('cupy'))。",
    "deps_config": "pyproject.toml の依存と、import されるトップレベル名の対応(例: cadquery-ocp → OCP)を突き合わせる。web/package.json の dependencies / devDependencies と import / ビルド設定も同様。",
}

summary = []
for key, flist in areas.items():
    fset = set(flist)
    brief = {
        "area": key,
        "title": META[key][0],
        "description": META[key][1],
        "hint": HINTS.get(key, ""),
        "files": [{"path": f, "lines": nlines(f)} for f in sorted(flist)],
        "mechanical": {
            "py_defs": [r for r in cand["py_defs"] if r["file"] in fset],
            "py_weak_refs": [r for r in cand["py_weak_refs"] if r["file"] in fset],
            "py_unused_imports": [r for r in cand["py_unused_imports"] if r["file"] in fset and r["from"] != "__future__"],
            "py_unused_params": [r for r in cand["py_unused_params"] if r["file"] in fset],
            "py_unused_locals": [r for r in cand["py_unused_locals"] if r["file"] in fset],
            "py_commented_code": [r for r in cand["py_commented_code"] if r["file"] in fset],
            "py_orphan_modules": [r for r in cand["py_all_modules_ref_table"] if r["file"] in fset and r["n_importers"] == 0 and r["str_refs"] == 0 and r["text_refs"] == 0],
            "ts_defs": [r for r in tsc["results"] if r["file"] in fset],
            "ts_export_only_used_locally": [r for r in tsc["localOnly"] if r["file"] in fset],
            "ts_iface_members_low_use": [r for r in cand["ts_iface_members"] if r["file"] in fset],
            "ts_test_only_exports": [r for r in cand["ts_test_only_exports"] if r["file"] in fset],
            "css_unused": [r for r in cand["css_unused"] if r["file"] in fset or (key == "web_app" and r["file"].endswith(".css"))],
            "tests_unused_fixtures": [r for r in unused_fx if r["file"] in fset],
            "dangling_imports": [r for r in dangling if r["file"] in fset],
        },
    }
    out = SP / "briefs" / f"{key}.json"
    out.write_text(json.dumps(brief, ensure_ascii=False, indent=1), encoding="utf-8")
    m = brief["mechanical"]
    n_items = sum(len(v) for v in m.values())
    summary.append({"key": key, "title": META[key][0], "description": META[key][1], "hint": HINTS.get(key, ""),
                    "brief": str(out), "nfiles": len(flist), "nlines": sum(x["lines"] for x in brief["files"]), "n_candidates": n_items})

(SP / "areas.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
total_files = sum(s["nfiles"] for s in summary)
print("areas:", len(summary), "files:", total_files, "of", len(code_files))
for s in summary:
    print(f'{s["key"]:22s} files={s["nfiles"]:3d} lines={s["nlines"]:6d} cand={s["n_candidates"]:4d}')
missing = [f for f in code_files if area_of(f) is None]
print("unassigned:", missing[:20])

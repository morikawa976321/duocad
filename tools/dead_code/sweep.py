"""Python のデッドコード候補を名前ベースで機械的に洗い出す(標準ライブラリの ast だけ。vulture 等は入っていない)。

    .venv/Scripts/python.exe tools/dead_code/sweep.py [出力フォルダ]     # 既定は output/dead_code

出力は `candidates.json`(カテゴリごとの候補)と標準出力の件数表。**判定器ではなく候補の生成器**:
同名の定義(`get` など)があると参照ありに見えて見逃し、`getattr`・`@entry` 登録・文字列で引かれるものは誤検出になる。
やり方と誤検出の型は docs/DEV_NOTES.md「デッドコードの洗い出し」。TS/Svelte は `ts_sweep.mjs`、壊れた import は
`dangling.py`、担当領域ごとの brief は `areas.py`、エージェントを並べる脚本は `workflow.js`。
"""
from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "dead_code"
OUT.mkdir(parents=True, exist_ok=True)

raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8")
FILES = [f for f in raw.split("\0") if f]


def zone(path: str) -> str:
    if path.startswith("duocad/"):
        return "prod"
    if path.startswith("tests/"):
        return "tests"
    if path.startswith("tools/"):
        return "tools"
    if path.startswith("topopt_checks/"):
        return "checks"
    if path.startswith("web/src/"):
        return "web"
    if path.startswith("web/scripts/"):
        return "webscripts"
    if path.endswith(".md"):
        return "docs"
    return "other"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8", errors="replace")


WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def strip_js_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"(?<![:\\\"'`])//[^\n]*", "", text)


# ---------------------------------------------------------------------------------------
# Python: 定義と参照
# ---------------------------------------------------------------------------------------
PY = [f for f in FILES if f.endswith(".py")]
# 参照: token -> list[(file, line)] / 文字列参照 / テキスト参照
real_refs: dict[str, list] = defaultdict(list)
str_refs: dict[str, list] = defaultdict(list)
text_refs: dict[str, dict] = defaultdict(lambda: defaultdict(int))  # token -> zone -> count
doc_refs: dict[str, int] = defaultdict(int)
import_tokens: dict[str, set] = defaultdict(set)
defs: list[dict] = []
unused_imports: list[dict] = []
unused_params: list[dict] = []
unused_locals: list[dict] = []
unreachable: list[dict] = []
commented_code: list[dict] = []
decorators_seen: dict[str, int] = defaultdict(int)
parse_failures: list[str] = []

REEXPORT_FILES = {"duocad/__init__.py", "duocad/server.py", "duocad/verbs/__init__.py", "duocad/agent/__init__.py",
                  "duocad/net/__init__.py", "duocad/topopt/__init__.py", "tools/thread_sim/__init__.py"}
SKIP_NAMES = {"main", "run", "handle", "emit", "default", "setUp", "tearDown", "format"}


def docstring_nodes(tree: ast.AST) -> set:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def dec_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return "?"


def analyze_python(path: str) -> None:
    text = read(path)
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        parse_failures.append(f"{path}: {exc}")
        return
    z = zone(path)
    docs = docstring_nodes(tree)
    lines = text.splitlines()

    # --- 名前の読み出し(ファイル内)
    loaded: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Load, ast.Del)):
            loaded.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            for w in WORD.findall(node.value) if len(node.value) <= 120 else ([node.value] if WORD.fullmatch(node.value) else []):
                loaded.add(w)
    # `__all__`
    exported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                exported |= {e.value for e in node.value.elts if isinstance(e, ast.Constant)}

    # --- 参照の収集
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Load, ast.Del)):
            real_refs[node.id].append((path, node.lineno))
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Load, ast.Del)):
            real_refs[node.attr].append((path, node.lineno))
        elif isinstance(node, ast.keyword) and node.arg:
            str_refs[node.arg].append((path, getattr(node, "lineno", 0) or 0))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            v = node.value
            ws = WORD.findall(v) if len(v) <= 120 else ([v] if WORD.fullmatch(v) else [])
            for w in ws:
                str_refs[w].append((path, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            for part in (node.module or "").split("."):
                if part:
                    import_tokens[part].add(path)
            for alias in node.names:
                import_tokens[alias.name.split(".")[0]].add(path)
                local = alias.asname or alias.name
                line_text = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
                end_line_text = lines[(node.end_lineno or node.lineno) - 1]
                noqa = "noqa" in line_text or "noqa" in end_line_text
                reexport = path in REEXPORT_FILES or local in exported
                if local in loaded or reexport:
                    real_refs[alias.name].append((path, node.lineno))
                elif alias.name != "*" and not noqa:
                    unused_imports.append({"file": path, "line": node.lineno, "name": local,
                                           "from": node.module or ""})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for part in alias.name.split("."):
                    import_tokens[part].add(path)
                local = (alias.asname or alias.name).split(".")[0]
                line_text = lines[node.lineno - 1]
                if local not in loaded and path not in REEXPORT_FILES and "noqa" not in line_text and local not in exported:
                    unused_imports.append({"file": path, "line": node.lineno, "name": local, "from": ""})

    # --- 定義の収集
    def add(kind, name, qual, node, decorators=()):
        decorators_seen_names = [dec_name(d) for d in decorators]
        for d in decorators_seen_names:
            decorators_seen[d] += 1
        defs.append({"file": path, "zone": z, "kind": kind, "name": name, "qual": qual, "line": node.lineno,
                     "end": node.end_lineno or node.lineno, "decorators": decorators_seen_names})

    def walk_body(body, prefix, in_class, in_func):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not in_func:
                    add("method" if in_class else "function", node.name, prefix + node.name, node, node.decorator_list)
                walk_body(node.body, prefix + node.name + ".", False, True)
            elif isinstance(node, ast.ClassDef):
                if not in_func:
                    add("class", node.name, prefix + node.name, node, node.decorator_list)
                walk_body(node.body, prefix + node.name + ".", True, in_func)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)) and not in_func:
                for attr in ("body", "orelse", "finalbody"):
                    walk_body(getattr(node, attr, []) or [], prefix, in_class, in_func)
                for h in getattr(node, "handlers", []):
                    walk_body(h.body, prefix, in_class, in_func)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and not in_func and not in_class:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and not (t.id.startswith("__") and t.id.endswith("__")):
                        add("const", t.id, prefix + t.id, node)
            elif isinstance(node, ast.AnnAssign) and in_class and not in_func:
                if isinstance(node.target, ast.Name):
                    add("field", node.target.id, prefix + node.target.id, node)

    walk_body(tree.body, "", False, False)

    # --- 関数ごとの検査: 未使用パラメータ・未使用ローカル・到達不能コード
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body_loads = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Load, ast.Del))}
        body_strs = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                body_strs |= set(WORD.findall(n.value)) if len(n.value) <= 120 else set()
        args = fn.args
        params = [a.arg for a in (args.posonlyargs + args.args + args.kwonlyargs)]
        dnames = [dec_name(d) for d in fn.decorator_list]
        if params and not (fn.name.startswith("__") and fn.name.endswith("__")):
            for p in params:
                if p in ("self", "cls") or p.startswith("_"):
                    continue
                if p not in body_loads and p not in body_strs and not any(n in ("entry", "fixture", "overload", "abstractmethod") for n in dnames):
                    # 本体が `...`/pass/raise だけの関数(プロトコル・スタブ)は除く
                    stmts = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant))]
                    if stmts and not all(isinstance(s, (ast.Pass, ast.Raise)) for s in stmts):
                        unused_params.append({"file": path, "line": fn.lineno, "function": fn.name, "param": p})
        # 未使用ローカル(単一代入のみ)
        stores: dict[str, int] = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                stores.setdefault(n.targets[0].id, n.lineno)
        global_names = {name for n in ast.walk(fn) if isinstance(n, (ast.Global, ast.Nonlocal)) for name in n.names}
        for name, ln in stores.items():
            if name.startswith("_") or name in global_names:
                continue
            if name not in body_loads and name not in body_strs:
                unused_locals.append({"file": path, "line": ln, "function": fn.name, "name": name})

    # 到達不能(同じブロックの return/raise/continue/break の後ろ)
    for node in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(node, attr, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                    nxt = block[i + 1]
                    unreachable.append({"file": path, "line": nxt.lineno, "after": type(stmt).__name__, "after_line": stmt.lineno})
                    break
        if isinstance(node, (ast.If, ast.While)):
            t = node.test
            if isinstance(t, ast.Constant) and not t.value:
                unreachable.append({"file": path, "line": node.lineno, "after": "constant-false-condition", "after_line": node.lineno})

    # コメントアウトされたコード
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            body = tok.string.lstrip("#").strip()
            if len(body) < 6 or not body.isascii() or body.startswith(("noqa", "type:", "pragma", "-*-", "!", "TODO", "FIXME", "NOTE")):
                continue
            if not re.search(r"(=|\(.*\)|^return\b|^import\b|^from\b.*\bimport\b|^def\b|^class\b|^(if|for|while|with|try|elif|else)\b.*:)", body):
                continue
            try:
                parsed = ast.parse(body)
            except SyntaxError:
                continue
            if not parsed.body:
                continue
            first = parsed.body[0]
            if isinstance(first, ast.Expr) and not isinstance(first.value, (ast.Call, ast.Await)):
                continue
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Call) and " " in body.split("(")[0].strip():
                continue
            commented_code.append({"file": path, "line": tok.start[0], "text": body[:120]})
    except (tokenize.TokenError, IndentationError):
        pass


for f in PY:
    analyze_python(f)

# ---------------------------------------------------------------------------------------
# 非Python のテキスト(ts / svelte / mjs / json / html / css / md)のトークン
# ---------------------------------------------------------------------------------------
TEXT_EXT = (".ts", ".svelte", ".mjs", ".js", ".json", ".html", ".css", ".md", ".ps1", ".toml", ".cmd")
TS_FILES = [f for f in FILES if f.startswith("web/src/") and f.endswith((".ts", ".svelte"))]
ts_text: dict[str, str] = {}
for f in FILES:
    if f.endswith(".py") or not f.endswith(TEXT_EXT) or f.endswith("package-lock.json") or f == "uv.lock":
        continue
    if f.startswith("docs/topopt_idle_validation") and f.endswith(".json"):
        continue
    t = read(f)
    z = zone(f)
    if f.endswith((".ts", ".svelte", ".mjs", ".js", ".css")):
        t = strip_js_comments(t)
        ts_text[f] = t
    if z == "docs":
        for w in WORD.findall(t):
            doc_refs[w] += 1
    else:
        for w in WORD.findall(t):
            text_refs[w][z] += 1

# ---------------------------------------------------------------------------------------
# Python: 到達性の判定(固定点反復)
# ---------------------------------------------------------------------------------------
LIVE_ZONES = {"prod": {"prod"}, "tools": {"tools", "prod"}, "checks": {"checks", "prod"}, "tests": {"tests"},
              "webscripts": {"webscripts"}, "other": {"other"}}
by_file: dict[str, list] = defaultdict(list)
for i, d in enumerate(defs):
    d["id"] = i
    by_file[d["file"]].append(d)

nonlive: set[int] = set()


def inside_nonlive(path: str, line: int) -> bool:
    for d in by_file.get(path, ()):
        if d["id"] in nonlive and d["line"] <= line <= d["end"]:
            return True
    return False


def own_span(d, path, line):
    return path == d["file"] and d["line"] <= line <= d["end"]


def is_excluded(d: dict) -> bool:
    n = d["name"]
    if n.startswith("__") and n.endswith("__"):
        return True
    if n in SKIP_NAMES or n.startswith(("do_", "pytest_")):
        return True
    if d["zone"] == "tests" and (n.startswith(("test_", "Test")) or n in ("conftest",)):
        return True
    if any(x in ("entry", "fixture", "overload", "abstractmethod", "setter", "getter", "deleter") or "register" in x or "route" in x
           for x in d["decorators"]):
        return True
    return False


def evaluate(d: dict) -> dict:
    name = d["name"]
    live = scripts = tests = 0
    live_sites, script_sites, test_sites = [], [], []
    for path, line in real_refs.get(name, ()):
        if own_span(d, path, line) or inside_nonlive(path, line):
            continue
        z = zone(path)
        if z in LIVE_ZONES[d["zone"]]:
            live += 1
            live_sites.append(f"{path}:{line}")
        elif z == "tests":
            tests += 1
            test_sites.append(f"{path}:{line}")
        else:
            scripts += 1
            script_sites.append(f"{path}:{line}")
    strs = 0
    str_sites = []
    for path, line in str_refs.get(name, ()):
        if own_span(d, path, line) or inside_nonlive(path, line):
            continue
        if zone(path) == "tests":
            tests += 1
            test_sites.append(f"{path}:{line}")
        else:
            strs += 1
            str_sites.append(f"{path}:{line}")
    texts = sum(c for z, c in text_refs.get(name, {}).items() if z != "tests")
    test_text = text_refs.get(name, {}).get("tests", 0)
    return {"live": live, "scripts": scripts, "tests": tests + test_text, "strs": strs, "texts": texts,
            "docs": doc_refs.get(name, 0), "sites": (live_sites + script_sites)[:3], "test_sites": test_sites[:3],
            "str_sites": str_sites[:3]}


candidates_defs = [d for d in defs if not is_excluded(d)]
changed = True
rounds = 0
while changed:
    changed = False
    rounds += 1
    for d in candidates_defs:
        if d["id"] in nonlive:
            continue
        e = evaluate(d)
        if e["live"] == 0 and e["strs"] == 0 and e["texts"] == 0:
            nonlive.add(d["id"])
            changed = True

results = []
for d in candidates_defs:
    if d["id"] not in nonlive:
        # 動的呼び出し(文字列・他言語)でしか参照されない: live が 0 のものだけ
        e = evaluate(d)
        if e["live"] == 0 and (e["strs"] or e["texts"]):
            cat = "STR_ONLY"
        else:
            continue
    else:
        e = evaluate(d)
        if e["scripts"]:
            cat = "SCRIPT_ONLY"
        elif e["tests"]:
            cat = "TEST_ONLY"
        else:
            cat = "DEAD"
    if d["kind"] == "field" and cat != "DEAD":
        continue
    results.append({**{k: d[k] for k in ("file", "kind", "qual", "line", "end", "decorators")}, "category": cat,
                    "live": e["live"], "scripts": e["scripts"], "tests": e["tests"], "strs": e["strs"],
                    "texts": e["texts"], "docs": e["docs"], "sites": e["sites"], "test_sites": e["test_sites"],
                    "str_sites": e["str_sites"]})

# ---------------------------------------------------------------------------------------
# Python: 孤立モジュール
# ---------------------------------------------------------------------------------------
orphans = []
for f in PY:
    if f.endswith("__init__.py") or f.startswith("tests/"):
        continue
    base = Path(f).stem
    if base in ("conftest",):
        continue
    importers = {p for p in import_tokens.get(base, ()) if p != f}
    other_str = [p for p, _ in str_refs.get(base, ()) if p != f]
    in_docs = doc_refs.get(base, 0)
    has_main = "__main__" in read(f)
    orphans.append({"file": f, "imported_by": sorted(importers)[:5], "n_importers": len(importers), "str_refs": len(other_str),
                    "text_refs": sum(text_refs.get(base, {}).values()), "doc_refs": in_docs, "has_main": has_main})
orphan_modules = [o for o in orphans if o["n_importers"] == 0 and o["str_refs"] == 0 and o["text_refs"] == 0]

# ---------------------------------------------------------------------------------------
# TypeScript / Svelte: export と interface メンバ
# ---------------------------------------------------------------------------------------
EXPORT_DECL = re.compile(r"^export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:function\*?|const|let|class|interface|type|enum|abstract class)\s+(\w+)", re.M)
EXPORT_LIST = re.compile(r"^export\s*\{([^}]*)\}", re.M | re.S)


def word_count_in(text: str, name: str) -> int:
    return len(re.findall(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", text))


universe = {f: t for f, t in ts_text.items()}
py_blob_tests = "\n".join(read(f) for f in PY if f.startswith("tests/"))
py_blob_prod = "\n".join(read(f) for f in PY if not f.startswith("tests/"))
ts_exports = []
for f in TS_FILES:
    t = ts_text.get(f, "")
    names = set(EXPORT_DECL.findall(t))
    for m in EXPORT_LIST.findall(t):
        for part in m.split(","):
            part = part.strip()
            if not part:
                continue
            seg = part.split(" as ")[-1].strip() if " as " in part else part
            seg = seg.removeprefix("type ").strip()
            if seg:
                names.add(seg)
    for name in names:
        if name == "default":
            continue
        own = word_count_in(t, name)
        other_ts = other_scripts = other_tests = 0
        other_ts_files = []
        for g, gt in universe.items():
            if g == f:
                continue
            c = word_count_in(gt, name)
            if not c:
                continue
            if g.startswith("web/src/"):
                other_ts += c
                other_ts_files.append(g)
            elif g.startswith("web/scripts/"):
                other_scripts += c
            elif g.startswith("tests/"):
                other_tests += c
        py_mentions = word_count_in(py_blob_prod, name) + word_count_in(py_blob_tests, name)
        ts_exports.append({"file": f, "name": name, "own": own, "other_ts": other_ts, "other_scripts": other_scripts,
                           "other_tests": other_tests, "py_mentions": py_mentions, "other_ts_files": other_ts_files[:3]})
# .svelte のコンポーネント(ファイル全体の参照)
svelte_unused_components = []
for f in TS_FILES:
    if not f.endswith(".svelte"):
        continue
    base = Path(f).stem
    users = [g for g, gt in universe.items() if g != f and re.search(r"[\"'/]" + re.escape(base) + r"(\.svelte)?[\"']", gt)]
    if not users:
        svelte_unused_components.append(f)
ts_orphans = []
for f in TS_FILES:
    if f.endswith(".svelte"):
        continue
    base = Path(f).stem
    users = [g for g, gt in universe.items() if g != f and re.search(r"[\"'/]" + re.escape(base) + r"(\.ts|\.js)?[\"']", gt)]
    py_users = [] if base not in py_blob_tests else ["py"]
    if not users:
        ts_orphans.append({"file": f, "py_mention": bool(py_users)})

# 未使用の export 候補を層別
ts_dead = [e for e in ts_exports if e["other_ts"] == 0 and e["other_scripts"] == 0 and e["other_tests"] == 0 and e["own"] <= 1 and e["py_mentions"] == 0]
ts_local_only = [e for e in ts_exports if e["other_ts"] == 0 and e["other_scripts"] == 0 and e["other_tests"] == 0 and e["own"] > 1]
ts_test_only = [e for e in ts_exports if e["other_ts"] == 0 and (e["other_tests"] > 0 or e["other_scripts"] > 0)]

# interface メンバ: 宣言(+実装)以外から1度も参照されない
iface_members = []
IFACE = re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)[^{]*\{", re.M)
for f in TS_FILES:
    if not f.endswith(".ts"):
        continue
    t = ts_text[f]
    for m in IFACE.finditer(t):
        i = m.end()
        depth = 1
        j = i
        while j < len(t) and depth:
            c = t[j]
            depth += c == "{"
            depth -= c == "}"
            j += 1
        block = t[i:j - 1]
        d0 = 0
        member_re = re.compile(r"^(\s*)(?:readonly\s+)?(\w+)\??\s*[(:<]", re.M)
        # 深さ1の行だけ
        offset = 0
        for line in block.split("\n"):
            stripped = line.strip()
            mm = member_re.match(line)
            if mm and d0 == 0:
                name = mm.group(2)
                total = sum(word_count_in(gt, name) for gt in universe.values())
                if total <= 2:
                    ln = t[:i].count("\n") + block[:offset].count("\n") + 1
                    iface_members.append({"file": f, "interface": m.group(1), "member": name, "line": ln, "total_occurrences": total})
            d0 += line.count("{") + line.count("(") - line.count("}") - line.count(")")
            if d0 < 0:
                d0 = 0
            offset += len(line) + 1

# CSS: styles.css のクラスが他で使われているか
css_unused = []
css_files = [f for f in FILES if f.endswith(".css") and f.startswith("web/")]
all_ts_blob = "\n".join(t for f, t in ts_text.items() if not f.endswith(".css")) + "\n" + read("web/index.html")
for f in css_files:
    t = read(f)
    for cls in sorted(set(re.findall(r"\.([A-Za-z_][\w-]*)", re.sub(r"/\*.*?\*/", "", t, flags=re.S)))):
        if re.search(r"(?<![\w-])" + re.escape(cls) + r"(?![\w-])", all_ts_blob):
            continue
        # 動的な組み立て(`tool-${x}` など)を拾うため、前方一致の断片が出てくるかを見る
        prefix = re.split(r"[-_]", cls)[0]
        dynamic = bool(re.search(r"[\"'`]" + re.escape(prefix) + r"[-_]", all_ts_blob)) if prefix != cls else False
        css_unused.append({"file": f, "class": cls, "maybe_dynamic": dynamic})

from collections import Counter as _C

_name_defs = _C(d["name"] for d in candidates_defs)
weak = []
for d in candidates_defs:
    if d["id"] in nonlive or d["zone"] not in ("prod", "tools") or d["kind"] == "field":
        continue
    e = evaluate(d)
    if 1 <= e["live"] <= 2 and (d["kind"] == "method" or _name_defs[d["name"]] > 1):
        weak.append({"file": d["file"], "kind": d["kind"], "qual": d["qual"], "line": d["line"], "live_refs": e["live"],
                     "sites": e["sites"], "same_name_defs": _name_defs[d["name"]]})

out = {
    "py_weak_refs": weak,
    "py_defs": results,
    "py_unused_imports": unused_imports,
    "py_unused_params": unused_params,
    "py_unused_locals": unused_locals,
    "py_unreachable": unreachable,
    "py_commented_code": commented_code,
    "py_orphan_modules": orphan_modules,
    "py_all_modules_ref_table": orphans,
    "ts_dead_exports": ts_dead,
    "ts_local_only_exports": ts_local_only,
    "ts_test_only_exports": ts_test_only,
    "ts_orphans": ts_orphans,
    "svelte_unused_components": svelte_unused_components,
    "ts_iface_members": iface_members,
    "css_unused": css_unused,
    "decorators_seen": dict(sorted(decorators_seen.items(), key=lambda kv: -kv[1])),
    "parse_failures": parse_failures,
}
(OUT / "candidates.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

# 件数表
from collections import Counter

c = Counter((r["category"], r["file"].split("/")[0]) for r in results)
print("py_defs:", dict(c))
print("py_weak_refs", len(weak))
for k in ("py_unused_imports", "py_unused_params", "py_unused_locals", "py_unreachable", "py_commented_code",
          "py_orphan_modules", "ts_dead_exports", "ts_local_only_exports", "ts_test_only_exports", "ts_orphans",
          "svelte_unused_components", "ts_iface_members", "css_unused"):
    print(k, len(out[k]))
print("fixed-point rounds:", rounds, "decorators:", list(out["decorators_seen"])[:25])
print("parse failures:", parse_failures)

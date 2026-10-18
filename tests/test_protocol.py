"""コアの宣言(`duocad/catalog.py`)が、実際に配るものと TypeScript の生成物の両方と一致することを見る。

1. **宣言 ⇔ 実際の応答** —— verb を1つずつ走らせ、応答(`applied`)のキーと型が `Command.returns` /
   `CHANGE_EVENTS` のとおりであること。状態配信のキーが `STATE_KEYS` のとおりであること。
   落ちたら、入口の戻り値と宣言のどちらかが古い(画面は `undefined` を読む)。
2. **宣言 ⇔ 生成物** —— `protocol.ts` が今の宣言から作ったものと一致し、画面のコードがその型で通ること。
   落ちたら、宣言を変えたのに `python tools/generate_protocol_ts.py` を走らせていないか、
   画面のコードが宣言と合っていない。
"""
from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from duocad import catalog, kernel
from duocad.entry import entries
from duocad.paths import REPO_ROOT
from duocad.server import Session
from session_support import SIDE, box as _box, select as _select

DEVIATION = 0.2
#: 20mm立方体の辺に対して**通る**半径(`tests/test_feature_ui.py` の実測値と同じ)。
RADIUS_THAT_WORKS = 3.0

#: どの応答にも付くので `Command.returns` には書かないキー。`client_op` は `net/ws.py` が後から載せる。
COMMON_KEYS = frozenset({"verb", "seq", "change", "client_op"})


def _verbs() -> list[str]:
    """応答に載せる値を宣言している verb(`undo` / `redo` を含む)。"""
    return [command.name for command in catalog.COMMANDS if command.returns]


def _change_commands() -> list[str]:
    """ブラウザから踏めて文書・選択・可視性・作業空間を変えるが、verb ではないもの(応答は `change` のイベント)。

    `apply_command` は除く: 構えていた verb の封筒をそのまま返す(verb 側の検査が形を見る。`catalog` の note)。
    """
    return [
        command.name
        for command in catalog.COMMANDS
        if command.ws != "none"
        and command.changes_document
        and not command.returns
        and command.name != "apply_command"
    ]


# 型の突き合わせ(`Result.type` は TypeScript の型の書き方)
#: **`boolean` と `number` を取り違えない** —— Pythonの `bool` は `int` の派生なので、
#: `number` 側で明示的に弾かないと `True` が `number` として通ってしまう。
_PRIMITIVES: dict[str, Callable[[Any], bool]] = {
    "string": lambda value: isinstance(value, str),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
}


def _type_mismatch(declared: str, value: Any) -> Optional[str]:
    """実値 `value` が宣言の型に収まらない理由。収まっていれば `None`。独自型は中身を見ない。"""
    arms = [arm.strip() for arm in declared.split("|")]
    if value is None:
        return None if "null" in arms else f"実応答が null だが宣言は `{declared}`(`| null` が要る)"
    arrays = [arm[:-2] for arm in arms if arm.endswith("[]")]
    if isinstance(value, list):
        if not arrays:
            return f"実応答が配列だが宣言は `{declared}`(`T[]` が要る)"
        checks = [_PRIMITIVES[element] for element in arrays if element in _PRIMITIVES]
        bad = [item for item in value if checks and not any(check(item) for check in checks)]
        return f"配列の要素 {bad[0]!r} が宣言 `{declared}` の要素型に合わない" if bad else None
    if arrays:
        return f"実応答が配列でない({value!r})のに宣言は `{declared}`"
    checks = [_PRIMITIVES[arm] for arm in arms if arm in _PRIMITIVES]
    if checks and not any(check(value) for check in checks):
        return f"実応答 {value!r} が宣言 `{declared}` に合わない"
    return None


def _compare(what: str, results, applied: dict) -> None:
    """実応答 `applied` を宣言 `results` と突き合わせる(在不在 + 型)。名前が `?` で終わるものは省略できる。"""
    declared = {result.name.rstrip("?"): result for result in results}
    present = set(applied) - COMMON_KEYS
    undeclared = present - set(declared)
    assert not undeclared, f"{what} の実応答に、宣言していないキーがある: {sorted(undeclared)}"
    missing = {name for name, result in declared.items() if not result.name.endswith("?")} - present
    assert not missing, f"{what} が宣言しているのに実応答に無いキーがある: {sorted(missing)}"
    problems = [
        f"{name}: {reason}"
        for name in sorted(present)
        if (reason := _type_mismatch(declared[name].type, applied[name]))
    ]
    assert not problems, f"{what} の型が宣言と食い違う:\n  " + "\n  ".join(problems)


_RUNNERS: dict[str, Callable[..., dict]] = {}


def _runner(name: str) -> Callable[[Callable[..., dict]], Callable[..., dict]]:
    def register(function: Callable[..., dict]) -> Callable[..., dict]:
        _RUNNERS[name] = function
        return function

    return register


def _sketch(session: Session) -> str:
    return session.sketch_create("XY")["sketch_id"]


def _step_file(tmp_path: Path) -> Path:
    """1ソリッドのSTEPを作って返す(`tests/test_files_ui.py` と同じ作り)。"""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    path = tmp_path / "part.step"
    kernel.write_step(BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape(), str(path))
    return path


SVG_BODY = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">
  <circle cx="30" cy="30" r="10"/>
  <rect x="100" y="10" width="20" height="20"/>
</svg>
"""


@_runner("sketch_create")
def _run_sketch_create(session: Session, tmp_path: Path) -> dict:
    return session.sketch_create("XY")


@_runner("sketch_create_offset")
def _run_sketch_create_offset(session: Session, tmp_path: Path) -> dict:
    return session.sketch_create_offset(5.0, "XY")


@_runner("add_point")
def _run_add_point(session: Session, tmp_path: Path) -> dict:
    return session.add_point(_sketch(session), [5.0, 5.0])


@_runner("add_line")
def _run_add_line(session: Session, tmp_path: Path) -> dict:
    return session.add_line(_sketch(session), [0.0, 0.0], [10.0, 0.0])


@_runner("add_circle")
def _run_add_circle(session: Session, tmp_path: Path) -> dict:
    return session.add_circle(_sketch(session), [0.0, 0.0], 10.0)


@_runner("add_ellipse")
def _run_add_ellipse(session: Session, tmp_path: Path) -> dict:
    return session.add_ellipse(_sketch(session), [0.0, 0.0], 10.0, 4.0, 30.0)


@_runner("add_rectangle")
def _run_add_rectangle(session: Session, tmp_path: Path) -> dict:
    return session.add_rectangle(_sketch(session), [0.0, 0.0], 10.0, 10.0)


@_runner("add_arc")
def _run_add_arc(session: Session, tmp_path: Path) -> dict:
    return session.add_arc(_sketch(session), [0.0, 0.0], 5.0, 0.0, 90.0)


@_runner("set_parameter")
def _run_set_parameter(session: Session, tmp_path: Path) -> dict:
    session.add_circle(_sketch(session), [1.0, 2.0], 6.0)
    name = sorted(session.document.parameters)[0]
    return session.set_parameter(name, 4.0)


@_runner("pattern_rectangular")
def _run_pattern_rectangular(session: Session, tmp_path: Path) -> dict:
    sketch_id = _sketch(session)
    entity_id = session.add_circle(sketch_id, [0.0, 0.0], 4.0)["entity_id"]
    return session.pattern_rectangular(sketch_id, [entity_id], 10.0, 10.0, 2, 2)


@_runner("pattern_circular")
def _run_pattern_circular(session: Session, tmp_path: Path) -> dict:
    sketch_id = _sketch(session)
    entity_id = session.add_circle(sketch_id, [10.0, 0.0], 4.0)["entity_id"]
    return session.pattern_circular(sketch_id, [entity_id], [0.0, 0.0], 4)


@_runner("import_svg")
def _run_import_svg(session: Session, tmp_path: Path) -> dict:
    path = tmp_path / "shapes.svg"
    path.write_text(SVG_BODY, encoding="utf-8")
    return session.import_svg(_sketch(session), str(path))


@_runner("extrude")
def _run_extrude(session: Session, tmp_path: Path) -> dict:
    sketch_id = _sketch(session)
    session.add_rectangle(sketch_id, [0.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    return session.extrude(SIDE, mode="join")


@_runner("revolve")
def _run_revolve(session: Session, tmp_path: Path) -> dict:
    sketch_id = _sketch(session)
    session.add_rectangle(sketch_id, [0.0, 1.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    return session.revolve(axis="x", angle=90.0, mode="join")


@_runner("loft")
def _run_loft(session: Session, tmp_path: Path) -> dict:
    bottom = _sketch(session)
    session.add_rectangle(bottom, [0.0, 0.0], SIDE, SIDE)
    top = session.sketch_create_offset(SIDE, "XY")["sketch_id"]
    session.add_circle(top, [SIDE / 2, SIDE / 2], SIDE)
    _select(session, [{"sketch": bottom, "index": 1}, {"sketch": top, "index": 1}])
    return session.loft(mode="join")


@_runner("fillet")
def _run_fillet(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    return session.fillet(RADIUS_THAT_WORKS)


@_runner("chamfer")
def _run_chamfer(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    return session.chamfer(RADIUS_THAT_WORKS)


@_runner("shell_body")
def _run_shell_body(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session.shell_body(RADIUS_THAT_WORKS)


@_runner("boolean_body")
def _run_boolean_body(session: Session, tmp_path: Path) -> dict:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    from duocad.document import Body, new_body_id

    target, _ = _box(session)
    tool_id = new_body_id()
    session.document.add_body(
        Body(
            id=tool_id,
            name="tool",
            kind="solid",
            shape=BRepPrimAPI_MakeBox(gp_Pnt(15.0, 15.0, 15.0), 5.0, 5.0, 5.0).Shape(),
        )
    )
    _select(session, [{"body": tool_id, "kind": "face", "index": 1}])
    return session.boolean_body(
        {"body": target.id, "kind": "face", "index": 1},
        "union",
    )


@_runner("move_sketch_entities")
def _run_move_sketch_entities(session: Session, tmp_path: Path) -> dict:
    sketch_id = _sketch(session)
    entity_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    return session.move_sketch_entities(3.0, 4.0)


@_runner("move_body")
def _run_move_body(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session.move_body(3.0, 4.0, 5.0)


@_runner("mirror_sketch_entities")
def _run_mirror_sketch_entities(session: Session, tmp_path: Path) -> dict:
    """軸の線分は**対象に入れない**(入れるとverbが黙って外し、`created` が空になりうる)。"""
    sketch_id = _sketch(session)
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [0.0, 10.0])["entity_id"]
    entity_id = session.add_circle(sketch_id, [5.0, 5.0], 4.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    return session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})


@_runner("envelope_regions")
def _run_envelope_regions(session: Session, tmp_path: Path) -> dict:
    sketch_id = _sketch(session)
    session.add_rectangle(sketch_id, [0.0, 0.0], 10.0, 10.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    return session.envelope_regions(1.0, 2.0)


@_runner("mirror_body")
def _run_mirror_body(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session.mirror_body(base_plane="YZ")


@_runner("delete")
def _run_delete(session: Session, tmp_path: Path) -> dict:
    """**ボディと図形を混ぜて**走らせる —— 片方だけで走らせると、宣言の
    `bodies` / `entities` / `sketch_ids` のどれかが一度も中身を持たないまま緑になる。"""
    body, sketch_id = _box(session)
    entity_id = session.add_circle(sketch_id, [5.0, 5.0], 4.0)["entity_id"]
    _select(
        session,
        [
            {"body": body.id, "kind": "face", "index": 1},
            {"sketch": sketch_id, "entity": entity_id, "point": None},
        ],
    )
    return session.delete()


@_runner("delete_items")
def _run_delete_items(session: Session, tmp_path: Path) -> dict:
    """ボディとスケッチを混ぜて走らせる(`delete` と同じ理由)。"""
    body, sketch_id = _box(session)
    return session.delete_items(bodies=[body.id], sketches=[sketch_id])


@_runner("rename")
def _run_rename(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    return session.rename(body.id, "台座")


def _preserve(session: Session, body) -> None:
    """条件面は**保持ソリッドの面にしか置けない**(2026-09-18〜)ので、面を指す前に保持にする。"""
    _select(session, [{"body": body.id, "kind": "body"}])
    session.set_preserve_bodies()


@_runner("set_preserve_bodies")
def _run_set_preserve_bodies(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "body"}])
    return session.set_preserve_bodies()


@_runner("set_exclude_bodies")
def _run_set_exclude_bodies(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "body"}])
    return session.set_exclude_bodies()


@_runner("set_fixtures")
def _run_set_fixtures(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _preserve(session, body)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session.set_fixtures()


@_runner("set_loads")
def _run_set_loads(session: Session, tmp_path: Path) -> dict:
    """**ベクトルの向きで走らせる** —— `direction_vector` が `null` になる `'normal'` で
    走らせると、宣言の配列側(`number[]`)が一度も観測されないまま緑になる。"""
    body, _ = _box(session)
    _preserve(session, body)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session.set_loads(250.0, [0.0, 0.0, -1.0])


@_runner("clear_loads")
def _run_clear_loads(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _preserve(session, body)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.set_loads(250.0, [0.0, 0.0, -1.0])
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session.clear_loads()


@_runner("import_step")
def _run_import_step(session: Session, tmp_path: Path) -> dict:
    return session.import_step(str(_step_file(tmp_path)))


@_runner("undo")
def _run_undo(session: Session, tmp_path: Path) -> dict:
    session.add_circle(_sketch(session), [0.0, 0.0], 10.0)
    return session.undo()


@_runner("redo")
def _run_redo(session: Session, tmp_path: Path) -> dict:
    session.add_circle(_sketch(session), [0.0, 0.0], 10.0)
    session.undo()
    return session.redo()


@_runner("set_visible")
def _run_set_visible(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    return session.set_visible(body.id, False)


@_runner("set_sketch_visible")
def _run_set_sketch_visible(session: Session, tmp_path: Path) -> dict:
    """`change: 'visibility'` の `sketch_id` 側(`body_id` と対で、どちらも欠けうる)。"""
    _, sketch_id = _box(session)
    return session.set_sketch_visible(sketch_id, False)


@_runner("modify_selection")
def _run_modify_selection(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    return session.modify_selection([{"body": body.id, "kind": "face", "index": 1}], [])


@_runner("toggle_selection")
def _run_toggle_selection(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    return session.toggle_selection([{"body": body.id, "kind": "face", "index": 1}])


@_runner("clear_selection")
def _run_clear_selection(session: Session, tmp_path: Path) -> dict:
    return session.clear_selection()


@_runner("begin_command")
def _run_begin_command(session: Session, tmp_path: Path) -> dict:
    _box(session)
    return session.begin_command("fillet", {"radius": 1.0})


@_runner("set_command_args")
def _run_set_command_args(session: Session, tmp_path: Path) -> dict:
    _box(session)
    session.begin_command("fillet")
    return session.set_command_args({"radius": 1.0})


@_runner("end_command")
def _run_end_command(session: Session, tmp_path: Path) -> dict:
    """`change: 'command'` の `command` が null で来る経路。"""
    _box(session)
    session.begin_command("fillet")
    return session.end_command()


@_runner("enter_sketch")
def _run_enter_sketch(session: Session, tmp_path: Path) -> dict:
    _box(session)
    created = session.apply("sketch_create", {"base_plane": "XY"})
    session.leave_sketch()
    return session.enter_sketch(created["sketch_id"])


@_runner("leave_sketch")
def _run_leave_sketch(session: Session, tmp_path: Path) -> dict:
    _box(session)
    session.apply("sketch_create", {"base_plane": "XY"})
    return session.leave_sketch()


@_runner("enter_optimize")
def _run_enter_optimize(session: Session, tmp_path: Path) -> dict:
    _box(session)
    return session.enter_optimize()


@_runner("leave_optimize")
def _run_leave_optimize(session: Session, tmp_path: Path) -> dict:
    _box(session)
    session.enter_optimize()
    return session.leave_optimize()


@_runner("new_document")
def _run_new_document(session: Session, tmp_path: Path) -> dict:
    """**`path` / `name` が `null` で来る唯一の経路**(開いたファイルが無い)。

    `change: 'document'` の `| null` が実測で裏づくのはここだけなので、`open_document`
    (どちらも文字列)と対にして残しておくこと。
    """
    return session.new_document()


@_runner("close_document")
def _run_close_document(session: Session, tmp_path: Path) -> dict:
    _box(session)
    return session.close_document()


@_runner("open_document")
def _run_open_document(session: Session, tmp_path: Path) -> dict:
    session.add_circle(_sketch(session), [0.0, 0.0], 10.0)
    session.save_document("applied-type-sync.json")
    return session.open_document("applied-type-sync.json")


@_runner("add_mesh_body")
def _run_add_mesh_body(session: Session, tmp_path: Path) -> dict:
    tetrahedron = [0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0]
    return session.add_mesh_body(tetrahedron, [0, 2, 1, 0, 1, 3, 1, 2, 3, 2, 0, 3])


@pytest.fixture(scope="module")
def applied_by_command(tmp_path_factory) -> dict[str, Any]:
    """対象コマンドを**全部1回ずつ**走らせて `applied` を集める。"""
    results: dict[str, Any] = {}
    for name in _verbs() + _change_commands():
        runner = _RUNNERS.get(name)
        if runner is None:
            continue  # `test_every_target_command_has_a_runner` が落とす
        try:
            session = Session(DEVIATION, models_dir=tmp_path_factory.mktemp(name))
            results[name] = runner(session, tmp_path_factory.mktemp(f"{name}-files"))
        except Exception as error:  # noqa: BLE001 —— 受け側で赤くする
            results[name] = error
    return results


def _applied(command: str, applied_by_command: dict[str, Any]) -> dict:
    """1コマンドの実応答を取り出す。**走らせられていなければ落とす(skipしない)。**"""
    if command not in applied_by_command:
        pytest.fail(
            f"{command!r} を走らせる手続きが無い —— tests/test_protocol.py の `_RUNNERS` に登録すること"
        )
    applied = applied_by_command[command]
    if isinstance(applied, BaseException):
        raise AssertionError(
            f"{command!r} の走らせ方が失敗した(`_RUNNERS` の手続きを直すこと): {applied!r}"
        ) from applied
    return applied


def test_every_target_command_has_a_runner() -> None:
    """対象を走らせる手続きが全部ある(無いものを黙って飛ばすと、そのコマンドだけ誰も見ない)。"""
    targets = _verbs() + _change_commands()
    assert len(targets) >= 30, f"対象を導けていない: {targets}"
    assert sorted(set(targets) - set(_RUNNERS)) == []


def test_every_verb_declares_what_it_returns() -> None:
    """入口を持つ verb は、応答に載せる値を宣言している(宣言が無いと画面の型に現れない)。"""
    undeclared = sorted(name for name in entries() if not catalog.BY_NAME[name].returns)
    assert undeclared == []


@pytest.mark.parametrize("verb", _verbs())
def test_the_response_of_a_verb_matches_its_declaration(verb: str, applied_by_command: dict) -> None:
    """落ちたら、入口の戻り値と `Command.returns` のどちらかが古い。"""
    applied = _applied(verb, applied_by_command)
    assert applied.get("verb") == verb, f"{verb} の応答が verb の封筒でない: {applied}"
    _compare(verb, catalog.BY_NAME[verb].returns, applied)


@pytest.mark.parametrize("command", _change_commands())
def test_the_event_of_a_change_matches_its_declaration(command: str, applied_by_command: dict) -> None:
    """verb でない変更(選択・可視性・文書の差し替え)は `change` のイベントで、形は `CHANGE_EVENTS` のとおり。"""
    applied = _applied(command, applied_by_command)
    assert "verb" not in applied, f"{command} が verb の封筒を返している: {applied}"
    assert applied.get("change") in catalog.CHANGE_EVENTS, f"{command} の change が宣言に無い: {applied}"
    _compare(command, catalog.CHANGE_EVENTS[applied["change"]], applied)


def test_every_declared_change_is_actually_emitted(applied_by_command: dict) -> None:
    """宣言にだけ残った `change` が無い(省略できるキーも、どれかのコマンドが実際に載せる)。"""
    seen: dict[str, set] = {}
    for command in _change_commands():
        applied = _applied(command, applied_by_command)
        seen.setdefault(applied["change"], set()).update(applied)
    assert set(seen) == set(catalog.CHANGE_EVENTS)
    for change, results in catalog.CHANGE_EVENTS.items():
        never = sorted(result.name for result in results if result.name.rstrip("?") not in seen[change])
        assert never == [], f"change: {change} のキー {never} を載せるコマンドが1つも無い"


def test_state_keys_match_the_runtime_state(tmp_path) -> None:
    """`Session.state_meta()` が実際に返すキーと `catalog.STATE_KEYS` が一致する(画面の `ServerState` は
    型検査がこの宣言と突き合わせる)。"""
    session = Session(DEVIATION, models_dir=tmp_path / "models")
    _box(session)
    assert set(session.state_meta()) == set(catalog.STATE_KEYS)
    assert len(catalog.STATE_KEYS) == len(set(catalog.STATE_KEYS))


def test_the_generated_typescript_is_up_to_date() -> None:
    """落ちたら、宣言を変えたあと `python tools/generate_protocol_ts.py` を走らせていない。"""
    generator = runpy.run_path(str(REPO_ROOT / "tools" / "generate_protocol_ts.py"))
    current = generator["OUTPUT"].read_text(encoding="utf-8").replace("\r\n", "\n")
    assert current == generator["render"](), "web/src/generated/protocol.ts が宣言と一致していない"


def test_the_web_code_type_checks_against_the_declaration() -> None:
    """画面のコードが、生成した型(コマンド名・WebSocket の引数・`Applied`・状態のキー)で通る。

    落ちたら、`commands/*.ts` の `id` や `build` の引数、`session.ts` の `ServerState` が宣言と合っていない。
    **skip しない** —— `web/node_modules` が無いときの skip は、無人実行では緑と区別が付かない。
    """
    tsc = REPO_ROOT / "web" / "node_modules" / "typescript" / "bin" / "tsc"
    assert tsc.exists(), f"TypeScript が無い({tsc})。`npm install --prefix web` を走らせること"
    done = subprocess.run(
        ["node", str(tsc), "--noEmit", "-p", str(REPO_ROOT / "web" / "tsconfig.json")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]

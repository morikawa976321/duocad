"""可変解像度(ズーム連動のテセレーション) — 変換式・配りもの・プロトコルの契約。

**「寄っても多角形の角が出ない」こと自体は pytest では判定できない**(見た目は目でしか
分からない)。実機での確認は `web/scripts/smoke-resolution.mjs` が数値で行う。ここが固定する
のは、その手前にある**機械で見える3つ**である:

1. **変換式が2つの言語で同じ**こと。スケッチの円はブラウザ側(`web/src/adaptiveResolution.ts`)、
   ソリッドと辺はこちら(`duocad/resolution.py`)で折られるので、**定数が片方だけ動くと
   同じ画面で滑らかさが食い違う** —— 例外もコンソールエラーも出ない壊れ方なので、
   `test_entity_kind_tables.py` と同じ「TSのソースを静的に読む」やり方で塞ぐ。
2. **配りもの(`DocumentView`)が目標精度に従う**こと。ソリッド・辺・スケッチ領域の3経路
   すべてで、キャッシュを跨いで効き、版(`body_revision`)が上がって差分配信が届くこと。
3. **画面の設定が文書に漏れない**こと。`Session.deviation`(画面の土台)はズームで動かない。
   書き出しはこれすら見ない(`io_step.STL_DEVIATION` 固定)ので、**書き出す形の粗さは
   カメラでも設定でも変わらない**。
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import subprocess
from pathlib import Path

import pytest

from duocad import io_step, kernel, resolution
from duocad.server import Server, Session

REPO_ROOT = Path(__file__).resolve().parent.parent
TS_SOURCE = REPO_ROOT / "web" / "src" / "adaptiveResolution.ts"

DEVIATION = 0.2
SIDE = 20.0
RADIUS = 10.0


# --------------------------------------------------------------------------------------
# 1. 変換式が2つの言語で同じ
# --------------------------------------------------------------------------------------
#: 一致していなければならない定数(Python側の名前 -> TS側の名前)。
#:
#: **`DEFAULT_DEVIATION` / `DEFAULT_ANGULAR_DEFLECTION` も入れる** —— TS側はこの2つを
#: `kernel.py` から読めないので写している。写しがずれると角度偏差のスケールがずれる。
MIRRORED_CONSTANTS = {
    "TOLERANCE_PIXELS": "TOLERANCE_PIXELS",
    "DEVIATION_MIN_ABS": "DEVIATION_MIN_ABS",
    "DEVIATION_MAX_ABS": "DEVIATION_MAX_ABS",
    "DEVIATION_MIN_RELATIVE": "DEVIATION_MIN_RELATIVE",
    "DEVIATION_MAX_RELATIVE": "DEVIATION_MAX_RELATIVE",
    "ANGULAR_MIN": "ANGULAR_MIN",
    "ANGULAR_MAX": "ANGULAR_MAX",
}


def _ts_constants() -> dict:
    """`export const NAME = <数値>` を拾う(TSを実行せずソースを読む)。"""
    text = TS_SOURCE.read_text(encoding="utf-8")
    found: dict = {}
    for match in re.finditer(
        r"^export const ([A-Z_][A-Z0-9_]*) = (-?[0-9.]+(?:e-?[0-9]+)?)$", text, re.M
    ):
        found[match.group(1)] = float(match.group(2))
    return found


def test_the_ts_side_of_the_formula_is_readable() -> None:
    """先に**走査そのものが効いている**ことを見る(0件なら下の一致検査は常に緑になる)。"""
    constants = _ts_constants()
    assert len(constants) >= len(MIRRORED_CONSTANTS), (
        f"{TS_SOURCE} から定数を読めていない: {sorted(constants)}。"
        "書き方を `export const NAME = 0.35` の形から変えていないか"
    )


def test_the_conversion_constants_match_between_python_and_typescript() -> None:
    """**式の定数がPythonとTSで一致する**(片方だけ動くと同じ画面で滑らかさが食い違う)。"""
    ts = _ts_constants()
    # TS側は kernel の既定値2つを写しているので、そこも突き合わせる。
    expected = {
        "DEFAULT_DEVIATION": kernel.DEFAULT_DEVIATION,
        "DEFAULT_ANGULAR_DEFLECTION": kernel.DEFAULT_ANGULAR_DEFLECTION,
    }
    for python_name, ts_name in MIRRORED_CONSTANTS.items():
        expected[ts_name] = getattr(resolution, python_name)

    mismatched = {
        name: (value, ts.get(name))
        for name, value in expected.items()
        if ts.get(name) != pytest.approx(value, rel=0, abs=0)
    }
    assert not mismatched, (
        "duocad/resolution.py と web/src/adaptiveResolution.ts の定数が食い違う"
        f"(名前 -> (Python, TS)): {mismatched}"
    )


# --------------------------------------------------------------------------------------
# 1b. 定数だけでなく**式**が2つの言語で同じ
# --------------------------------------------------------------------------------------
#: TS側を実際に評価する口(`tests/eval_adaptive_resolution.mjs` のdocstring)。
TS_EVALUATOR = Path(__file__).resolve().parent / "eval_adaptive_resolution.mjs"

#: 非有限をJSONで運ぶときの綴り(JSONに非有限の数値リテラルが無いため)。
_NON_FINITE = {"NaN": float("nan"), "Infinity": float("inf"), "-Infinity": float("-inf")}


def _to_float(value) -> float:
    return _NON_FINITE[value] if isinstance(value, str) else float(value)


def _equivalence_cases() -> list:
    """突き合わせる入力の組。**表を手で書かない** —— 上下限は定数から導く。

    含めるのは(a)クランプの境界のすぐ内・ちょうど・すぐ外、(b)`DEFAULT_DEVIATION` に
    ちょうど落ちる点(角度偏差が既定へ戻る折り返し)、(c)桁をまたぐ極端値、(d)0近傍と
    非有限。**境界を跨がない値だけを並べると、クランプの向きを間違えても両方が同じに
    間違えたときにしか落ちない**ので、必ず両側を踏む。
    """
    cases: list = []
    for size in (0.0, 0.01, 1.0, 20.0, 620.0, 10_000.0):
        low = max(resolution.DEVIATION_MIN_ABS, size * resolution.DEVIATION_MIN_RELATIVE)
        high = max(
            low,
            min(resolution.DEVIATION_MAX_ABS, size * resolution.DEVIATION_MAX_RELATIVE)
            if size > 0
            else resolution.DEVIATION_MAX_ABS,
        )
        for edge in (low, high, kernel.DEFAULT_DEVIATION):
            # この `world_per_pixel` でちょうど `edge` になる、を中心に前後へ振る。
            base = edge / resolution.TOLERANCE_PIXELS
            for factor in (0.5, 0.999999, 1.0, 1.000001, 2.0):
                cases.append([base * factor, size])
        for extreme in (1.0e-12, 1.0e-6, 1.0, 1.0e6, 1.0e12):
            cases.append([extreme, size])
        for bad in (0.0, -1.0, "NaN", "Infinity", "-Infinity"):
            cases.append([bad, size])
    # `size_hint` 側の異常値(こちらは比の上下限を使わない経路へ落ちる)。
    for bad_size in (-1.0, "NaN", "Infinity"):
        cases.append([0.05, bad_size])
    return cases


def _evaluate_in_typescript(cases: list) -> list:
    """TS側の実装を **node で実際に走らせて** 評価する(ソースの文字列比較はしない)。

    **skipしない** —— 足場が無いときのskipは無人実行では緑と区別が付かず、
    「式の一致を誰も見ていない」状態をそのまま通してしまう。
    """
    assert TS_EVALUATOR.exists(), f"評価の口が無い: {TS_EVALUATOR}"
    try:
        completed = subprocess.run(
            ["node", str(TS_EVALUATOR)],
            input=json.dumps(cases).encode("utf-8"),
            capture_output=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
    except FileNotFoundError as error:  # noqa: PERF203
        raise AssertionError(
            "node が見つからない。TS側の式を評価できないので、両言語の一致を検査できない"
        ) from error
    if completed.returncode != 0:
        raise AssertionError(
            "TS側の評価に失敗した(Nodeが .ts を読めない可能性がある。23.6以降なら既定で読める):"
            + completed.stderr.decode("utf-8", "replace")
        )
    return json.loads(completed.stdout.decode("utf-8"))


def test_the_conversion_formula_matches_between_python_and_typescript() -> None:
    """**定数ではなく式そのもの**が2つの言語で同じ値を出す。

    定数の一致(上の検査)だけでは、`sqrt` を落とす・掛け算を足し算にする・クランプの
    向きを入れ替える、といった**式の形の食い違い**が全部素通りする。素通りすると、
    同じ画面でスケッチの円(ブラウザで折る)とソリッド・辺(サーバで折る)の粗さがずれる。

    比較は**厳密一致**。四則と `sqrt` はIEEE754で正しく丸められ、JSONの往復は倍精度を
    そのまま運ぶので、桁が1つでも違えば式が違う。
    """
    cases = _equivalence_cases()
    assert len(cases) >= 100, f"突き合わせる入力が少なすぎる: {len(cases)}件"

    from_ts = _evaluate_in_typescript(cases)
    assert len(from_ts) == len(cases), "TS側が返した件数が合わない"

    mismatched = []
    for (raw_per_pixel, raw_size), ts_values in zip(cases, from_ts):
        per_pixel = _to_float(raw_per_pixel)
        size = _to_float(raw_size)
        deviation, angular = resolution.view_resolution(per_pixel, size)
        python_values = [
            deviation,
            angular,
            resolution.clamp_deviation(per_pixel, size),
            resolution.angular_for_deviation(per_pixel),
        ]
        if python_values != ts_values:
            mismatched.append((raw_per_pixel, raw_size, python_values, ts_values))

    assert not mismatched, (
        "duocad/resolution.py と web/src/adaptiveResolution.ts の**式**が食い違う"
        f"({len(mismatched)}/{len(cases)}件。(world_per_pixel, size_hint, Python, TS)): "
        f"{mismatched[:5]}"
    )


def test_zooming_in_asks_for_a_finer_deviation_and_zooming_out_a_coarser_one() -> None:
    """**寄れば細かく、引けば粗く。** 目標偏差は `world_per_pixel` に単調に従う。"""
    fine, _ = resolution.view_resolution(0.001, SIDE)
    middle, _ = resolution.view_resolution(0.05, SIDE)
    coarse, _ = resolution.view_resolution(1.0, SIDE)
    assert fine < middle < coarse


def test_the_default_deviation_maps_back_to_the_default_angular_deflection() -> None:
    """**既定値の意味を変えていない** —— 線形が既定なら角度も既定に戻る。

    `tests/test_feature_ui.py` の小半径円弧の下限は `kernel.DEFAULT_ANGULAR_DEFLECTION`
    から導かれている。この対応が崩れると、あちらの検査の意図(角度偏差1本で下限が決まる)
    が静かに壊れる。
    """
    assert resolution.angular_for_deviation(kernel.DEFAULT_DEVIATION) == pytest.approx(
        kernel.DEFAULT_ANGULAR_DEFLECTION
    )


def test_the_angular_deflection_follows_the_linear_one_and_stays_in_radians() -> None:
    """角度偏差は線形偏差の平方根に比例し、**ラジアンの範囲を出ない**(度ではない)。"""
    fine = resolution.angular_for_deviation(kernel.DEFAULT_DEVIATION / 4.0)
    assert fine == pytest.approx(kernel.DEFAULT_ANGULAR_DEFLECTION / 2.0)
    for deviation in (1.0e-9, 1.0e-4, 0.2, 5.0, 1.0e9):
        angle = resolution.angular_for_deviation(deviation)
        assert resolution.ANGULAR_MIN <= angle <= resolution.ANGULAR_MAX
        assert angle < math.pi  # ラジアンの値であって度ではない


def test_absurd_camera_values_fall_back_instead_of_producing_a_useless_mesh() -> None:
    """非有限・0以下は**既定の粗さへ落ちる**(0を渡して偏差0のメッシュを頼まない)。"""
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        assert resolution.view_resolution(bad, SIDE) == (
            kernel.DEFAULT_DEVIATION,
            kernel.DEFAULT_ANGULAR_DEFLECTION,
        )


def test_the_deviation_is_clamped_both_absolutely_and_relative_to_the_model() -> None:
    """上下限が効く。**上限が下限を割る小さなモデルでも範囲が反転しない**。"""
    assert resolution.clamp_deviation(1.0e-12, SIDE) == resolution.DEVIATION_MIN_ABS
    assert resolution.clamp_deviation(1.0e9, SIDE) == pytest.approx(
        SIDE * resolution.DEVIATION_MAX_RELATIVE
    )
    # 外接対角 0.01mm のモデル: 比の上限(2e-4)が絶対の下限(1e-3)を下回る。
    tiny = resolution.clamp_deviation(1.0e9, 0.01)
    assert tiny == resolution.DEVIATION_MIN_ABS
    assert resolution.clamp_deviation(1.0e-12, 0.01) == resolution.DEVIATION_MIN_ABS
    # 大きなモデルでは比の下限が絶対の下限に勝つ(全体を1ミクロンで測り直さない)。
    assert resolution.clamp_deviation(1.0e-12, 10_000.0) == pytest.approx(
        10_000.0 * resolution.DEVIATION_MIN_RELATIVE
    )


# --------------------------------------------------------------------------------------
# 2〜3. 配りものが目標精度に従い、文書の土台は動かない
# --------------------------------------------------------------------------------------
@pytest.fixture()
def cylinder_session() -> tuple:
    """円(半径10mm)を押し出した円柱を1つ持つセッション。**曲面と円い辺の両方がある**。"""
    session = Session(None, DEVIATION)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], RADIUS)
    session.clear_selection()
    session.modify_selection([{"sketch": sketch_id, "index": 1}], [])
    session.extrude(SIDE, mode="new")
    session.clear_selection()
    return session, session.document.bodies[-1].id, sketch_id


def _edge_points(session: Session, body_id: str) -> int:
    """円い辺1本ぶんの点数(円柱の上下2本は同じ点数になる)。"""
    curved = [item for item in session.edges(body_id)["edges"] if item["kind"] == "circle"]
    assert curved, "円柱に円い辺が1本も無いのはおかしい"
    return max(len(item["points"]) // 3 for item in curved)


def _triangles(session: Session, body_id: str) -> int:
    return sum(len(face["indices"]) // 3 for face in session.geometry(body_id)["faces"])


def test_the_view_follows_the_camera_for_solids_edges_and_regions(cylinder_session) -> None:
    """**3経路すべて**(ソリッド・辺・スケッチ領域)がズームに追随する。"""
    session, body_id, sketch_id = cylinder_session

    base_deviation = session.geometry(body_id)["deviation"]
    base_edge_points = _edge_points(session, body_id)
    base_triangles = _triangles(session, body_id)
    base_region_points = len(_region(session, sketch_id)["positions"])
    assert base_deviation == pytest.approx(DEVIATION), "ズーム値が来る前は土台の粗さ"

    # 寄る(1画素 = 0.002mm)
    assert session.set_view_resolution(0.002, SIDE) is True
    fine_deviation = session.geometry(body_id)["deviation"]
    assert fine_deviation < base_deviation
    assert _edge_points(session, body_id) > base_edge_points
    assert _triangles(session, body_id) > base_triangles
    assert len(_region(session, sketch_id)["positions"]) > base_region_points

    # 引く(1画素 = 2mm。20mmの円柱が画面上10画素になるほど引いた状態)
    assert session.set_view_resolution(2.0, SIDE) is True
    coarse_deviation = session.geometry(body_id)["deviation"]
    assert coarse_deviation > base_deviation
    assert _edge_points(session, body_id) < base_edge_points
    assert _triangles(session, body_id) < base_triangles


def _region(session: Session, sketch_id: str) -> dict:
    sketch = next(
        item for item in session.state_meta()["sketches"] if item["sketch_id"] == sketch_id
    )
    assert sketch["regions"], "円のスケッチに領域が無いのはおかしい"
    return sketch["regions"][0]


def test_the_same_camera_twice_is_a_no_op(cylinder_session) -> None:
    """同じ値なら False(**ズーム中の連打で27部品ぶんのメッシュを流し直さない**)。"""
    session, _, _ = cylinder_session
    assert session.set_view_resolution(0.01, SIDE) is True
    assert session.set_view_resolution(0.01, SIDE) is False
    # 丸めで同じ目標精度に落ちる値も配信し直さない(上下限に張り付いた領域)。
    assert session.set_view_resolution(1.0e-9, SIDE) is True
    assert session.set_view_resolution(1.0e-12, SIDE) is False


def test_changing_the_camera_bumps_every_body_revision(cylinder_session) -> None:
    """版が上がる。**上がらないと差分配信が「変わっていない」と判断して古い形を残す**。"""
    session, body_id, _ = cylinder_session
    before = session.body_revision(body_id)
    session.set_view_resolution(0.002, SIDE)
    after = session.body_revision(body_id)
    assert after == before + 1
    # 版だけ上がって中身が同じ、では意味が無い(配る形も変わっていること)。
    assert session.geometry(body_id)["deviation"] < DEVIATION


def test_the_cached_geometry_is_thrown_away_when_the_camera_changes(cylinder_session) -> None:
    """**キャッシュを跨いで効く** —— 先に測っておいた形が残っていても粗さが変わる。"""
    session, body_id, _ = cylinder_session
    first = session.geometry(body_id)
    assert session.geometry(body_id) is first, "同じ形状ならキャッシュから返る前提が崩れた"
    session.set_view_resolution(0.002, SIDE)
    second = session.geometry(body_id)
    assert second is not first
    assert second["deviation"] < first["deviation"]


def test_the_camera_never_touches_the_document_baseline(cylinder_session, tmp_path) -> None:
    """**書き出す形の粗さはカメラで変わらない**(固定値で書き、土台もズームで動かない)。"""
    session, _, _ = cylinder_session
    session.set_view_resolution(0.002, SIDE)
    assert session.deviation == pytest.approx(DEVIATION)
    assert session.view.deviation == pytest.approx(DEVIATION)
    written = session.export_stl_file(tmp_path / "part.stl")
    assert written["deviation"] == pytest.approx(io_step.STL_DEVIATION)
    # 文書そのものにも何も残らない(操作ログに載らない = undoの単位ではない)。
    assert [entry.verb for entry in session.document.log.entries].count(
        "set_view_resolution"
    ) == 0


def test_the_baseline_comes_back_untouched_for_a_fresh_session() -> None:
    """ズーム値が来ていないセッションは**入れる前と同じ粗さ**で測る(既定の回帰)。"""
    session = Session(None, DEVIATION)
    assert session.view.view_deviation == pytest.approx(DEVIATION)
    assert session.view.view_angular_deflection == pytest.approx(
        kernel.DEFAULT_ANGULAR_DEFLECTION
    )


# --------------------------------------------------------------------------------------
# プロトコル(WebSocket)
# --------------------------------------------------------------------------------------
class FakeConnection:
    """`Server` が使うのは `send` だけ(`tests/test_feature_ui.py` と同じ形)。"""

    def __init__(self) -> None:
        self.payloads: list = []

    async def send(self, raw: str) -> None:
        self.payloads.append(json.loads(raw))


def _browser(session: Session, script):
    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        client.payloads.clear()

        async def send(command: dict) -> None:
            await server._dispatch(client, json.dumps(command))

        await script(send)
        return client

    return asyncio.run(scenario())


def test_the_websocket_command_redelivers_the_geometry_at_the_new_resolution(
    cylinder_session,
) -> None:
    """`set_view_resolution` で**新しい粗さのgeometryが届く**(要求元だけでなく状態配信)。"""
    session, body_id, _ = cylinder_session

    async def script(send):
        await send({"cmd": "set_view_resolution", "world_per_pixel": 0.002, "size_hint": SIDE})

    client = _browser(session, script)
    states = [payload for payload in client.payloads if payload.get("type") == "state"]
    assert states, f"状態が配られていない: {[p.get('type') for p in client.payloads]}"
    bodies = states[-1]["delta"]["bodies"]
    sent = {body["body_id"]: body["geometry"]["deviation"] for body in bodies}
    assert body_id in sent, f"版が上がったのに geometry が届いていない: {sorted(sent)}"
    assert sent[body_id] < DEVIATION
    # **`applied` は付かない**(文書を変えていない = undoの単位ではない)。
    assert "applied" not in states[-1]


def test_the_websocket_command_is_silent_when_nothing_changes(cylinder_session) -> None:
    """同じ値の再送では**何も配らない**(ズーム中の連打でメッシュを流し直さない)。"""
    session, _, _ = cylinder_session

    async def script(send):
        message = {"cmd": "set_view_resolution", "world_per_pixel": 0.002, "size_hint": SIDE}
        await send(message)
        await send(dict(message))

    client = _browser(session, script)
    states = [payload for payload in client.payloads if payload.get("type") == "state"]
    assert len(states) == 1, f"2回目でも配信している: {len(states)}件"


def test_the_size_hint_may_be_omitted(cylinder_session) -> None:
    """`size_hint` は省略できる(だから宣言は `ws=\"special\"` 側にある)。"""
    session, _, _ = cylinder_session

    async def script(send):
        await send({"cmd": "set_view_resolution", "world_per_pixel": 0.002})

    client = _browser(session, script)
    errors = [payload for payload in client.payloads if payload.get("type") == "error"]
    assert not errors, f"省略できるはずの引数でエラー: {errors}"
    assert session.view.view_deviation < DEVIATION

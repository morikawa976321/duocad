"""形状最適化の配線(`duocad/optimize_run.py`)を**端から端まで**回す受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_optimize_wiring.py

`tests/test_optimize_backend.py` は**SIMPを1回も回さない**(pytest を重くしないため)。
ここが「4条件を置いた duocad の文書から、実際にメッシュボディが出る」を確かめる唯一の場所で、
だから**この1本だけは本物を回す**(約25〜40秒。熱状態で1.7倍振れる)。

検査の構成:

0. 題材 —— 離れた2つのパッド(拘束側 10x40x40 / 荷重側 10x40x10)を duocad の verb で作り、
   4条件を置く。**条件面は別々のボディに載る**ので、面添字をまとめる経路を必ず通る。
1. **端から端まで** —— `state == "done"`、メッシュボディが1つ増える、**元のボディは
   1つも消えず・形も差し替わらない**(`is` で見る)。戻り値が**素の値だけ**でできている
   (`json.dumps` が通る = そのままWebSocketへ載せられる)ことも見る。
2. **結果メッシュの位置** —— ワールド座標[mm]で、格子の箱の中にあり、拘束側と荷重側の
   両方のパッドの近くまで届いている。**要素添字空間のまま入れると原点付近に縮む**ので、
   この検査が座標変換の門になる。
3. **荷重が選んだ面に入っている** —— 荷重面アンカーの節点の重心が、選んだ面の重心の近くに
   ある。**面添字を取り違えても例外は出ない**(荷重が別の場所に入り、最適化はもっともらしく
   収束する)ので、実際に走らせた結果で突き合わせる。
4. **進捗** —— 設計反復ごと(`phase="optimize"`)と候補材料量ごと(`phase="search"`)の
   両方が来る。
5. **中断** —— 進捗の途中で旗を立てると `OptimizeCancelled` になり、**文書は1ビットも
   変わらない**。
6. **応力を測る経路** —— `keep_model` が通っているので安全率が出る。許容応力は
   40MPa 固定で、その導出(PETG 50MPa × 0.8)が引ける。
7. **事後検出** —— この解像度で1要素も掴めない保持ソリッドがあると、`simp` を呼ぶ前に
   `state == "unrepresented_regions"` で戻り、メッシュボディは足されない。
8. **条件面からの要求要素サイズ** —— `optimize_run` が使う式が
   `anchor.AnchorTable.empty_face_diagnosis` の `suggested_max_spacing_mm` と同じ値になる
   (2箇所に書いた式は片方だけ直るので、値そのものを突き合わせる)。
"""

from __future__ import annotations

import json
import sys
import time

import numpy as np

# Windowsの既定コンソール(cp932)で記号1つ落ちるだけで検査が1件も走らず異常終了する。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad import optimize_run, query
from duocad.server import Session
from duocad.topopt import anchor as an
from duocad.topopt import voxelize as vx

FAILURES: list[str] = []
RESULTS: dict[str, object] = {}

DEVIATION = 0.2
# 検証用に縮めた設定。**本番の既定ではない** —— 既定(`max_iter=40` / `max_evaluations=15`)の
# ままだと1回の実行が数分になる(`20260904-1437` の契約③)。
CHECK_MAX_ITER = 6
CHECK_MAX_EVALUATIONS = 3
CHECK_RMIN_MM = 6.0  # 要素サイズの上限 = 6 / 1.5 = 4mm


def check(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


# ---------------------------------------------------------------------------
# 0. 題材
# ---------------------------------------------------------------------------
def _select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def _box(session: Session, width: float, depth: float, height: float, corner=(0.0, 0.0)):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, list(corner), width, depth)
    regions = len(session.state_meta()["sketches"][-1]["regions"])
    _select(session, [{"sketch": sketch_id, "index": regions}])
    session.extrude(height, mode="new")
    session.clear_selection()
    return session.document.bodies[-1]


def _face_with_normal(body, nx: float, ny: float, nz: float) -> int:
    for face in query.describe_faces(body.shape):
        n = face["normal"]
        if n[0] * nx + n[1] * ny + n[2] * nz > 0.9:
            return int(face["index"])
    raise SystemExit(f"法線 ({nx},{ny},{nz}) の面が見つからない")


def build_case(extra_tiny_keep: bool = False) -> tuple:
    """離れた2つのパッドに4条件を置いた文書。`extra_tiny_keep` で極小の保持ソリッドを足す。"""
    session = Session(None, DEVIATION)
    left = _box(session, 10.0, 40.0, 40.0)                      # x 0..10
    right = _box(session, 10.0, 40.0, 10.0, corner=(60.0, 0.0))  # x 60..70
    _select(session, [{"body": right.id, "kind": "body"}])
    session.move_body(0.0, 0.0, 30.0)                            # z 30..40
    session.clear_selection()
    right = session.document.get_body(right.id)

    keep_refs = [
        {"body": left.id, "kind": "body"},
        {"body": right.id, "kind": "body"},
    ]
    tiny = None
    if extra_tiny_keep:
        tiny = _box(session, 1.0, 1.0, 1.0, corner=(30.0, 20.0))
        _select(session, [{"body": tiny.id, "kind": "body"}])
        session.move_body(0.0, 0.0, 20.0)
        session.clear_selection()
        tiny = session.document.get_body(tiny.id)
        keep_refs.append({"body": tiny.id, "kind": "body"})

    fixed = _face_with_normal(left, -1.0, 0.0, 0.0)
    load = _face_with_normal(right, 0.0, 0.0, 1.0)
    _select(session, keep_refs)
    session.set_preserve_bodies()
    _select(session, [{"body": left.id, "kind": "face", "index": fixed}])
    session.set_fixtures()
    _select(session, [{"body": right.id, "kind": "face", "index": load}])
    session.set_loads(500.0, [0.0, 0.0, -1.0])
    session.clear_selection()
    return session, left, right, tiny


# ---------------------------------------------------------------------------
# 1..6 本番の1回
# ---------------------------------------------------------------------------
def run_once() -> tuple:
    print("[0] 4条件を置いた文書を作り、端から端まで1回走らせる")
    session, left, right, _tiny = build_case()
    report = session.optimize_conditions()
    if not report["ready"]:
        raise SystemExit(f"条件がそろっていない: {report['problems']}")
    limits = session.optimize_resolution(rmin_mm=CHECK_RMIN_MM)
    print(
        f"  要素サイズ: 上限 {limits['max_element_size_mm']:.4g}mm / "
        f"下限 {limits['min_element_size_mm']:.4g}mm / "
        f"予想 {limits['estimate']['seconds_typical']:.0f}秒(既定の反復数で)"
    )

    before_ids = [body.id for body in session.document.bodies]
    before_shapes = {body.id: body.shape for body in session.document.bodies}
    events: list[dict] = []
    t0 = time.perf_counter()
    out = session.run_optimization(
        rmin_mm=CHECK_RMIN_MM,
        target_safety_factor=1.2,
        max_iter=CHECK_MAX_ITER,
        max_evaluations=CHECK_MAX_EVALUATIONS,
        progress=events.append,
    )
    seconds = time.perf_counter() - t0
    print(
        f"  完了: {seconds:.1f}秒 / 状態 {out['state']} / 三角形 {out['triangle_count']} / "
        f"安全率 {out['safety_factor']:.3f} / 評価 {out['evaluations']}回"
    )
    RESULTS["run_seconds"] = round(seconds, 1)
    RESULTS["triangles"] = out["triangle_count"]
    RESULTS["evaluations"] = out["evaluations"]
    RESULTS["grid"] = out["grid"]["shape"]
    RESULTS["element_size_mm"] = round(out["element_size_mm"], 6)
    RESULTS["design_elements"] = out["design_elements"]
    RESULTS["safety_factor"] = round(out["safety_factor"], 4)
    RESULTS["material_mm3"] = round(out["material_mm3"], 3)
    RESULTS["progress_events"] = len(events)
    return session, left, right, out, events, before_ids, before_shapes, limits


def check_end_to_end(session, out, before_ids, before_shapes) -> None:
    print("\n[1] 端から端まで(結果は新しいボディとして足すだけ)")
    check("状態", out["state"] == "done", f"state={out['state']}")
    bodies = session.document.bodies
    added = [body for body in bodies if body.id not in before_ids]
    check(
        "ボディが1つだけ増えた",
        len(added) == 1 and added[0].id == out["body_id"],
        f"{len(before_ids)} -> {len(bodies)} (増えたのは {[b.name for b in added]})",
    )
    check(
        "増えたのはメッシュボディ",
        bool(added) and added[0].kind == "mesh" and added[0].mesh is not None,
        f"kind={added[0].kind if added else None} / name={added[0].name if added else None}",
    )
    survived = [body_id for body_id in before_ids if body_id in {b.id for b in bodies}]
    check("元のボディが全部残っている", survived == before_ids, f"{len(survived)}/{len(before_ids)}")
    same = all(
        session.document.get_body(body_id).shape is shape
        for body_id, shape in before_shapes.items()
    )
    check("元のボディの形が差し替わっていない", same, "shape の同一性(`is`)で確認")
    check(
        "三角形が入っている",
        (out["triangle_count"] or 0) > 100
        and added[0].mesh.triangle_count == out["triangle_count"],
        f"{out['triangle_count']} 枚",
    )
    # **画面(`20260904-1705`)はこの戻り値をWebSocketへそのまま載せる。** 実体
    # (`DesignSpace` / `AnchorTable`)が混ざっていたらここで落ちる。
    wire = dict(out)
    wire.pop("applied", None)
    try:
        payload = json.dumps(wire, ensure_ascii=False)
        ok, detail = True, f"{len(payload)} バイト"
    except TypeError as error:
        ok, detail = False, str(error)
    check("戻り値が素の値だけでできている(json.dumps が通る)", ok, detail)


def check_placement(session, left, right, out) -> None:
    print("\n[2] 結果メッシュの位置(要素添字空間 -> ワールド[mm])")
    from duocad import kernel

    body = session.document.get_body(out["body_id"])
    points = np.asarray(body.mesh.positions, dtype=float).reshape(-1, 3)
    lo, hi = points.min(axis=0), points.max(axis=0)
    grid = out["grid"]
    origin = np.asarray(grid["origin"], dtype=float)
    box_hi = origin + np.asarray(grid["shape"], dtype=float) * grid["spacing"]
    slack = grid["spacing"]
    check(
        "格子の箱の中にある",
        bool(np.all(lo >= origin - slack) and np.all(hi <= box_hi + slack)),
        f"メッシュ {np.round(lo, 2).tolist()}..{np.round(hi, 2).tolist()} / "
        f"格子 {np.round(origin, 2).tolist()}..{np.round(box_hi, 2).tolist()}",
    )
    left_box = kernel.bounding_box(left.shape)
    right_box = kernel.bounding_box(right.shape)
    # **両方のパッドに届いている**こと。片方だけだと「拘束と荷重をつなぐ材料が出ていない」。
    reaches_left = lo[0] <= left_box["max"][0] + slack
    reaches_right = hi[0] >= right_box["min"][0] - slack
    check(
        "拘束側と荷重側の両方に届いている",
        bool(reaches_left and reaches_right),
        f"x {lo[0]:.2f}..{hi[0]:.2f} / 拘束パッド x<= {left_box['max'][0]:.2f} / "
        f"荷重パッド x>= {right_box['min'][0]:.2f}",
    )
    # 要素添字空間のまま入れると、原点付近(0..格子の要素数)に縮む。
    span = float(np.max(hi - lo))
    check(
        "要素添字空間のままではない",
        span > 4.0 * grid["spacing"],
        f"メッシュの最大の広がり {span:.2f}mm(要素サイズ {grid['spacing']:.3f}mm)",
    )


def check_load_lands_on_the_chosen_face(session, out) -> None:
    print("\n[3] 荷重・拘束が選んだ面に入っている(面添字の取り違えは例外を出さない)")
    measured = out["anchor_check"]
    check(
        "実行時にアンカーの重心を測っている",
        bool(measured) and "load_mm" in measured and "fixed_mm" in measured,
        f"{measured}",
    )
    if not measured:
        return
    check(
        "荷重面アンカーの重心が選んだ面の重心の近くにある",
        measured["load_mm"] <= measured["limit_mm"],
        f"{measured['load_mm']:.3f}mm <= {measured['limit_mm']:.3f}mm",
    )
    check(
        "拘束面アンカーの重心が選んだ面の重心の近くにある",
        measured["fixed_mm"] <= measured["limit_mm"],
        f"{measured['fixed_mm']:.3f}mm <= {measured['limit_mm']:.3f}mm",
    )
    # **検出力**: 荷重面を別のボディの面へすり替えると、その距離は 50mm 級になる
    # (2つのパッドは x で 50mm 離れている)。ここでは実測値がその桁でないことを確かめる。
    other = np.asarray(session.document.optimize.fixtures[0]["center"], dtype=float)
    load = np.asarray(session.document.optimize.loads[0]["center"], dtype=float)
    separation = float(np.linalg.norm(other - load))
    check(
        "取り違えたら気づける距離になっている",
        separation > 2.0 * measured["limit_mm"],
        f"2つの条件面の重心は {separation:.1f}mm 離れており、許容 "
        f"{measured['limit_mm']:.3f}mm の {separation / measured['limit_mm']:.1f}倍"
        "(荷重面と拘束面を取り違えたら必ず落ちる)",
    )
    # **面ごとの距離は並びで来る**(`_check_anchor_centroids` の `*_mm_each`)。
    # 丸めをスカラー前提で書くと `TypeError` で落ちる —— 条件面が複数枚あるのは通常の入力である。
    RESULTS["anchor_check"] = {
        key: [round(item, 4) for item in value] if isinstance(value, list) else round(value, 4)
        for key, value in measured.items()
    }
    check(
        "面ごとの距離が条件面の枚数ぶん出ている(合計や平均で潰していない)",
        len(measured["fixed_mm_each"]) == len(session.document.optimize.fixtures)
        and len(measured["load_mm_each"]) == len(session.document.optimize.loads),
        f"拘束 {measured['fixed_mm_each']} / 荷重 {measured['load_mm_each']}",
    )


def check_progress(events) -> None:
    print("\n[4] 進捗(設計反復ごと・候補材料量ごとの2段)")
    phases = [event["phase"] for event in events]
    check("設計反復ごとの進捗が来る", phases.count("optimize") > 0, f"{phases.count('optimize')}件")
    check("候補材料量ごとの進捗が来る", phases.count("search") > 1, f"{phases.count('search')}件")
    iterations = [event for event in events if event["phase"] == "optimize"]
    check(
        "反復の進捗が反復数とコンプライアンスを持つ",
        bool(iterations)
        and all(
            "iteration" in event and "compliance" in event for event in iterations
        ),
        f"例: {iterations[0] if iterations else None}",
    )
    RESULTS["progress_phases"] = {name: phases.count(name) for name in sorted(set(phases))}


def check_cancel() -> None:
    print("\n[5] 中断(進捗の途中で旗を立てる)")
    session, _left, _right, _tiny = build_case()
    before_ids = [body.id for body in session.document.bodies]
    applied = session.applied_count
    seen: list[dict] = []

    def progress(event: dict) -> None:
        seen.append(event)
        if len(seen) >= 3:
            session.cancel_optimization()

    t0 = time.perf_counter()
    try:
        session.run_optimization(
            rmin_mm=CHECK_RMIN_MM,
            target_safety_factor=1.2,
            max_iter=CHECK_MAX_ITER,
            max_evaluations=CHECK_MAX_EVALUATIONS,
            progress=progress,
        )
    except optimize_run.OptimizeCancelled as error:
        seconds = time.perf_counter() - t0
        check("OptimizeCancelled で降りた", True, f"{error}({seconds:.1f}秒 / 進捗{len(seen)}件)")
        RESULTS["cancel_seconds"] = round(seconds, 1)
    else:
        check("OptimizeCancelled で降りた", False, "中断されずに最後まで走った")
    check(
        "文書が1ビットも変わっていない",
        [body.id for body in session.document.bodies] == before_ids
        and session.applied_count == applied,
        f"ボディ {len(session.document.bodies)}個 / applied_count {session.applied_count}",
    )


def check_stress_path(out) -> None:
    print("\n[6] 応力を測る経路(keep_model)と許容応力の導出")
    allowable = out["allowable"]
    check(
        "安全率が出ている(keep_model が通っている)",
        out["safety_factor"] is not None and out["safety_factor"] > 0.0,
        f"安全率 {out['safety_factor']:.4f} >= 目標 {out['target_safety_factor']}",
    )
    check(
        "目標安全率を満たしている",
        out["safety_factor"] >= out["target_safety_factor"],
        f"{out['safety_factor']:.4f} >= {out['target_safety_factor']}",
    )
    check(
        "許容応力 40MPa の導出が引ける",
        abs(allowable["allowable_mpa"] - 40.0) < 1e-9
        and abs(allowable["yield_mpa"] * allowable["print_derate"] - 40.0) < 1e-9,
        f"{allowable['material']} 降伏 {allowable['yield_mpa']}MPa x "
        f"{allowable['print_derate']} = {allowable['allowable_mpa']}MPa",
    )


# ---------------------------------------------------------------------------
# 7. 事後検出
# ---------------------------------------------------------------------------
def check_unrepresented_regions() -> None:
    print("\n[7] 事後検出(この解像度では表現できていない保持ソリッド)")
    session, _left, _right, tiny = build_case(extra_tiny_keep=True)
    before = len(session.document.bodies)
    t0 = time.perf_counter()
    out = session.run_optimization(
        rmin_mm=CHECK_RMIN_MM,
        target_safety_factor=1.2,
        max_iter=CHECK_MAX_ITER,
        max_evaluations=CHECK_MAX_EVALUATIONS,
    )
    seconds = time.perf_counter() - t0
    named = [item for item in out["unrepresented_regions"] if item["body"] == tiny.id]
    check(
        "表現できていない保持ソリッドを名指しする",
        bool(named),
        f"{[ (item['name'], item['elements']) for item in out['unrepresented_regions'] ]}",
    )
    check(
        "最適化を回さずに戻る",
        out["state"] == "unrepresented_regions" and out["safety_factor"] is None,
        f"state={out['state']} / {seconds:.1f}秒(端から端までなら十数秒)",
    )
    check(
        "メッシュボディを足していない",
        len(session.document.bodies) == before and out["body_id"] is None,
        f"ボディ {before} -> {len(session.document.bodies)}",
    )
    check(
        "警告文に次の一手が書いてある",
        "解像度を上げる" in (out["warning"] or ""),
        (out["warning"] or "")[:80],
    )
    RESULTS["unrepresented_seconds"] = round(seconds, 1)


# ---------------------------------------------------------------------------
# 8. 条件面からの要求要素サイズの式
# ---------------------------------------------------------------------------
def check_face_requirement_formula() -> None:
    print("\n[8] 条件面からの要求要素サイズが probe の目安と同じ式である")
    from build123d import Box, Pos

    session, _left, _right, _tiny = build_case()
    limits = session.optimize_resolution()
    # 実際に「節点が付かない面」を作って、probe 側の目安と突き合わせる。題材は
    # `check_anchor.py` の稜線の規則の検査と同じ(0.5mm の突起 ≪ 要素サイズ 5mm)。
    tiny = Box(40, 24, 10) + Pos(0, 0, 5.25) * Box(0.5, 0.5, 0.5)
    model = vx.voxelize(tiny, 8)
    table = an.build_anchors(model.soup, model.grid)
    empty = table.empty_faces()
    if not empty:
        check("空になる面が作れた", False, "この格子では空の面が1つも出なかった")
        return
    diagnosis = table.empty_face_diagnosis(empty[0])
    mine = optimize_run._face_max_spacing_mm(diagnosis["cad_area_mm2"])
    check(
        "式が一致する(sqrt(CAD面積))",
        abs(mine - diagnosis["suggested_max_spacing_mm"]) < 1e-12,
        f"optimize_run {mine:.6f} / anchor {diagnosis['suggested_max_spacing_mm']:.6f}",
    )
    check(
        "条件面の目安が resolution_limits にも同じ形で出ている",
        all(
            abs(
                item["suggested_max_spacing_mm"]
                - optimize_run._face_max_spacing_mm(item["cad_area_mm2"])
            )
            < 1e-12
            for item in limits["condition_faces"]
        ),
        f"{[round(item['suggested_max_spacing_mm'], 3) for item in limits['condition_faces']]}",
    )


def main() -> int:
    session, left, right, out, events, before_ids, before_shapes, _limits = run_once()
    check_end_to_end(session, out, before_ids, before_shapes)
    check_placement(session, left, right, out)
    check_load_lands_on_the_chosen_face(session, out)
    check_progress(events)
    check_cancel()
    check_stress_path(out)
    check_unrepresented_regions()
    check_face_requirement_formula()

    print("\n" + "=" * 72)
    print("MEASURED " + json.dumps(RESULTS, ensure_ascii=False, default=float))
    print("=" * 72)
    if FAILURES:
        print(f"NG: {len(FAILURES)}件が不合格")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("OK: 全検査に合格")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

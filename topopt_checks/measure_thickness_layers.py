"""厚み方向 1〜4 層での安全率のずれ(A)と、「個数」判定と層数の対応(B)を実測する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_thickness_layers.py
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_thickness_layers.py --section a --reference-n 8
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_thickness_layers.py --section b
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_thickness_layers.py --out C:\\tmp\\thickness.json

タスク `20260918-0239` の測定専用スクリプト。**判定はしない**(閾値 `MIN_REGION_ELEMENTS` が
妥当かどうかは書かない)。結果は `docs/TOPOPT_THICKNESS.md` に転記する。

出力は既定で標準出力の JSON だけ(`--out` を渡さない限り `output/` を含めどこにもファイルを
残さない)。

## A(曲げでの応力の取りこぼし)

`check_stress.py` の `build_cantilever` / `bending_ratio` の作りを流用するが、**既存ファイルは
書き換えない**契約のため `node_id` / `face_weights` / `build_cantilever` はここへ複写した
(`fem._build_edof` の局所節点順と一致していることは `check_stress.py` の [0] が別途押さえている
ので、ここでは再検証しない)。梁のアスペクト比を長さ:厚み:幅 = 16:1:2 に固定し、
`nelx=16n, nely=n, nelz=2n` で n=1..4 と参照解を解く。

## B(ボクセル化での「個数」と「層数」の対応)

`optimize_run._region_representation` は閾値未満のときしか要素数を返さない(合否だけが要る
製品経路の設計)。ここでは閾値を超えた場合の生の値と、厚み方向の層数(その関数が見ていない
軸別の分布)の両方が要るので、同じ計算(`vx.triangles_from_shape` → `vx.occupancy`)をもう一度
呼ぶ。**得た値が製品関数の判定と食い違っていないかは `verify_against_product` で毎回照合する**
(食い違えばその場で例外にする — 静かに別の値を報告しない)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
import _record
from duocad.topopt import fem
from duocad.topopt import stress

RESULTS: dict = {}

# 安全率の比だけを見る測定なので、許容応力は無次元1.0で固定する(実寸換算はしない。
# `stress.safety_factor` は単なる除算で単位を見ないので、比を取る分には支障が無い)。
ALLOWABLE = 1.0


# ---------------------------------------------------------------------------
# A. 曲げでの応力の取りこぼし
# ---------------------------------------------------------------------------
def node_id(i: int, j: int, k: int, nely: int, nelz: int) -> int:
    """`fem._build_edof` と同じ節点番号の規約(`check_stress.py` から複写)。"""
    return i * (nely + 1) * (nelz + 1) + j * (nelz + 1) + k


def face_weights(n1: int, n2: int) -> np.ndarray:
    """一様面圧の整合節点荷重の重み(`check_stress.py` から複写)。"""
    wy = np.where((np.arange(n1 + 1) == 0) | (np.arange(n1 + 1) == n1), 0.5, 1.0)
    wz = np.where((np.arange(n2 + 1) == 0) | (np.arange(n2 + 1) == n2), 0.5, 1.0)
    return np.outer(wy, wz)


def build_cantilever(nelx: int, nely: int, nelz: int) -> fem.Model:
    """x=0 面を全自由度固定し、x=nelx 面へ -y 方向の合力1を一様に配る(`check_stress.py` から複写)。"""
    ndof = 3 * (nelx + 1) * (nely + 1) * (nelz + 1)
    fixed = []
    for j in range(nely + 1):
        for k in range(nelz + 1):
            n = node_id(0, j, k, nely, nelz)
            fixed += [3 * n, 3 * n + 1, 3 * n + 2]
    weights = face_weights(nely, nelz)
    f = np.zeros(ndof)
    for j in range(nely + 1):
        for k in range(nelz + 1):
            f[3 * node_id(nelx, j, k, nely, nelz) + 1] = -weights[j, k]
    f /= -f.sum()
    return fem.Model(nelx, nely, nelz, fixed_dofs=np.unique(np.array(fixed)), load=f)


def measure_bending(n: int) -> dict:
    """厚み方向 n 層の梁を解いて、A節の5つの値を測る。

    断面は根元から梁せい1つぶん先(`check_stress.py` の `bending_ratio` と同じ理由 ——
    根元は固定端の局所効果が残る)。
    """
    nelx, nely, nelz = 16 * n, n, 2 * n
    model = build_cantilever(nelx, nely, nelz)
    rho = np.ones((nelx, nely, nelz))
    t0 = time.perf_counter()
    u = model.solve(rho, 1.0, solver="cg", cg_rtol=1e-11)
    field = stress.element_stress(model, u)
    seconds = time.perf_counter() - t0

    h = float(nely)
    inertia = float(nelz) * h**3 / 12.0
    c = h / 2.0
    isec = nely  # 梁せい1つぶん先(断面の中心は x = nely + 0.5)
    x_sec = isec + 0.5
    moment = 1.0 * (nelx - x_sec)
    ys = (np.arange(nely) + 0.5) - c

    v1_outer_vm = float(field.von_mises[isec, nely - 1, nelz // 2])
    rep = stress.representative_stress(field, rho)  # 製品と同じ規則、rho は全て1
    v2_representative = float(rep.value)
    v3_theory_surface = float(moment * c / inertia)
    v4_theory_at_center = float(moment * ys[-1] / inertia)  # 表面から要素半分だけ内側

    sf1 = stress.safety_factor(v1_outer_vm, ALLOWABLE)
    sf2 = stress.safety_factor(v2_representative, ALLOWABLE)
    sf3 = stress.safety_factor(v3_theory_surface, ALLOWABLE)

    # n=1 は唯一の要素の中心(c-0.5)がちょうど中立軸に一致するので理論値が厳密に0になる
    # (1〜1/4の比較不能はバグではなく、この幾何の帰結)。比は出さず None を書く。
    ratio_1_4 = None if v4_theory_at_center == 0.0 else v1_outer_vm / v4_theory_at_center

    return {
        "n": n,
        "grid": f"{nelx}x{nely}x{nelz}",
        "elements": int(nelx * nely * nelz),
        "seconds": seconds,
        "v1_outer_layer_vm": v1_outer_vm,
        "v2_representative": v2_representative,
        "v3_theory_surface": v3_theory_surface,
        "v4_theory_at_center": v4_theory_at_center,
        "ratio_1_3": v1_outer_vm / v3_theory_surface,
        "ratio_2_3": v2_representative / v3_theory_surface,
        "ratio_1_4": ratio_1_4,
        "sf1_from_outer_layer": sf1,
        "sf2_from_representative": sf2,
        "sf3_from_theory_surface": sf3,
        "sf_ratio_1_3": sf1 / sf3,
        "sf_ratio_2_3": sf2 / sf3,
    }


def run_section_a(ns: list[int], reference_n: int) -> dict:
    print(f"\n[A] 曲げでの応力の取りこぼし(n={ns} / 参照 n={reference_n})", flush=True)
    rows = []
    for n in ns + [reference_n]:
        row = measure_bending(n)
        rows.append(row)
        ratio_1_4_text = "n/a(中心=中立軸)" if row["ratio_1_4"] is None else f"{row['ratio_1_4']:.4f}"
        print(
            f"    n={n:>3d} {row['grid']:>14s}  outer/理論面={row['ratio_1_3']:.4f}  "
            f"代表/理論面={row['ratio_2_3']:.4f}  outer/理論中心={ratio_1_4_text}  "
            f"({row['seconds']:.1f}秒)",
            flush=True,
        )
    # 生の v1/sf1 を参照解と直接比べない: n で梁の絶対サイズが変わり無次元理論値が
    # ~1/n^2 で動くので、比べるのは寸法非依存の sf_ratio_1_3(理論値で正規化済み)にする
    # (詳細と数値根拠は docs/TOPOPT_THICKNESS.md「安全率のずれ」)。
    ref = next(r for r in rows if r["n"] == reference_n)
    for row in rows:
        row["vs_reference"] = {
            "sf_ratio_1_3_over_reference": row["sf_ratio_1_3"] / ref["sf_ratio_1_3"],
            "sf_ratio_2_3_over_reference": row["sf_ratio_2_3"] / ref["sf_ratio_2_3"],
        }
    return {"reference_n": reference_n, "rows": rows}


# ---------------------------------------------------------------------------
# B. ボクセル化での「個数」と「層数」の対応
# ---------------------------------------------------------------------------
PLATE_XY = 50.0  # mm
PAD_SIZE = 10.0  # mm(固定・荷重パッド。個数判定を測りたいのは板の方なので大きめに取る)
PAD_GAP_X = 40.0  # mm(パッド中心の x。板の x 範囲 [-25,25] と重ならない)


def build_region_case(*, thickness: float, spacing: float, z_shift: float):
    """板1枚(保持ソリッド)+ 固定・荷重パッドの最小の文書と、その塊を作る。

    パッドが要るのは `design_space.Conditions` が拘束面・荷重面を1枚以上要求するため
    (`fixed_faces` / `load_faces` が空だと `ValueError`)。板だけでは条件が組めない。
    パッドは十分大きい(`MIN_REGION_ELEMENTS` を要素サイズによらず必ず超える)ので、
    `_region_representation` の診断は板の行だけを見ればよい。
    """
    from build123d import Box, Pos

    from duocad import optimize_run
    from duocad.document import Body, Document
    from duocad.topopt import anchor as an
    from duocad.topopt import design_space as ds
    from duocad.topopt import voxelize as vx

    plate = Pos(0, 0, z_shift) * Box(PLATE_XY, PLATE_XY, thickness)
    fixed_pad = Pos(-PAD_GAP_X, 0, 0) * Box(PAD_SIZE, PAD_SIZE, PAD_SIZE)
    load_pad = Pos(PAD_GAP_X, 0, 0) * Box(PAD_SIZE, PAD_SIZE, PAD_SIZE)
    keep = plate + fixed_pad + load_pad

    soup = vx.triangles_from_shape(vx._unwrap(keep), deviation=0.2)
    fixed_hits = an.faces_matching(
        soup, kind="plane", normal=(-1, 0, 0), near_point=(-PAD_GAP_X - PAD_SIZE / 2, 0, 0),
        normal_tol=1e-6, near_tol=1e-4,
    )
    load_hits = an.faces_matching(
        soup, kind="plane", normal=(1, 0, 0), near_point=(PAD_GAP_X + PAD_SIZE / 2, 0, 0),
        normal_tol=1e-6, near_tol=1e-4,
    )
    if len(fixed_hits) != 1 or len(load_hits) != 1:
        raise RuntimeError(
            f"パッドの外側面を一意に選べなかった(fixed={fixed_hits} load={load_hits})"
        )

    bodies = (
        ds.condition_body("fixed_pad", fixed_pad, roles=("keep", "fixed")),
        ds.condition_body("load_pad", load_pad, roles=("keep", "load")),
        ds.condition_body("plate", plate, roles=("keep",)),
    )
    conditions = ds.Conditions(
        keep=keep, fixed_faces=fixed_hits[0], load_faces=load_hits[0], bodies=bodies
    )
    space = ds.derive(conditions, spacing=spacing)

    doc = Document()
    doc.add_body(Body(id="fixed_pad", name="fixed_pad", kind="solid", shape=vx._unwrap(fixed_pad)))
    doc.add_body(Body(id="load_pad", name="load_pad", kind="solid", shape=vx._unwrap(load_pad)))
    doc.add_body(Body(id="plate", name="plate", kind="solid", shape=vx._unwrap(plate)))
    bundle = optimize_run.ConditionBundle(
        conditions=conditions,
        anchor_body_ids=["fixed_pad", "load_pad"],
        face_offsets={},
        fixed_faces=conditions.fixed_faces,
        load_faces=conditions.load_faces,
        keep_body_ids=["fixed_pad", "load_pad", "plate"],
        exclude_body_ids=[],
        forces=(),
        load_n=1.0,
    )
    return doc, bundle, space


def measure_plate_region(doc, space, body_id: str = "plate") -> dict:
    """板の occupancy を測って、要素数(製品と同じ計算)と厚み方向の層数を出す。

    `deviation` の決め方は `optimize_run._region_representation` と同一の式
    (`space.stats.get("deviation")` が無ければ `spacing * DEFAULT_DEVIATION_RATIO`)。
    """
    from duocad.topopt import voxelize as vx

    body = doc.get_body(body_id)
    deviation = float(
        space.stats.get("deviation") or (space.grid.spacing * vx.DEFAULT_DEVIATION_RATIO)
    )
    soup = vx.triangles_from_shape(body.shape, deviation=deviation)
    try:
        mask, _stats = vx.occupancy(soup, space.grid)
    except ValueError:
        mask = np.zeros(space.grid.shape, dtype=bool)
    design = np.asarray(space.mask, dtype=bool)
    grid_elements = int(np.count_nonzero(mask))
    inside_design = int(np.count_nonzero(mask & design))
    # 板は z 方向が薄い(厚み軸)。occupancy のレイは +z 方向に飛ぶ(voxelize.py の規約)ので、
    # 層数は z 軸を潰した「その列に1要素でも占有があるか」の枚数で数える。
    layer_occupied = np.any(mask, axis=(0, 1))
    thickness_layers = int(np.count_nonzero(layer_occupied))
    return {
        "grid_elements": grid_elements,
        "inside_design": inside_design,
        "thickness_layers": thickness_layers,
        "spacing_mm": float(space.grid.spacing),
    }


def verify_against_product(doc, bundle, space, body_id: str, measured: dict) -> None:
    """`optimize_run._region_representation` の判定と、ここで測った値が食い違っていないか照合する。

    食い違えば例外にする(静かに別の値を報告しない)。閾値未満のときは診断エントリの
    `elements` と `measured["inside_design"]` が一致すること、閾値以上のときはそもそも
    診断に出てこないこと(合格は「載らない」設計)を見る。
    """
    from duocad import optimize_run

    diagnoses = optimize_run._region_representation(doc, bundle, space)
    entry = next((d for d in diagnoses if d["body"] == body_id), None)
    threshold = optimize_run.MIN_REGION_ELEMENTS
    counted = measured["inside_design"]
    if counted >= threshold:
        if entry is not None:
            raise RuntimeError(
                f"照合失敗: counted={counted}>=threshold={threshold} なのに診断に出た: {entry}"
            )
    else:
        if entry is None or entry["elements"] != counted:
            raise RuntimeError(
                f"照合失敗: counted={counted} threshold={threshold} 診断={entry}"
            )


def verdict_of(counted: int, threshold: int) -> str:
    if counted == 0:
        return "stop(0要素)"
    if counted < threshold:
        return f"warn(1~{threshold - 1}要素)"
    return f"pass({threshold}要素以上)"


def run_section_b(thickness: float, ratios: list[float]) -> dict:
    from duocad import optimize_run

    print(f"\n[B] ボクセル化での「個数」と「層数」の対応(厚み={thickness}mm)", flush=True)
    threshold = optimize_run.MIN_REGION_ELEMENTS
    rows = []
    for ratio in ratios:
        spacing = thickness / ratio
        offsets = [0.0, spacing / 4.0, spacing / 2.0]
        offset_rows = []
        for shift in offsets:
            t0 = time.perf_counter()
            doc, bundle, space = build_region_case(
                thickness=thickness, spacing=spacing, z_shift=shift
            )
            measured = measure_plate_region(doc, space)
            verify_against_product(doc, bundle, space, "plate", measured)
            seconds = time.perf_counter() - t0
            offset_rows.append(
                {
                    "z_shift_mm": shift,
                    "z_shift_over_spacing": shift / spacing,
                    "actual_spacing_mm": measured["spacing_mm"],
                    "grid_elements": measured["grid_elements"],
                    "inside_design": measured["inside_design"],
                    "thickness_layers": measured["thickness_layers"],
                    "verdict": verdict_of(measured["inside_design"], threshold),
                    "seconds": seconds,
                }
            )
        rows.append({"ratio_thickness_over_spacing": ratio, "requested_spacing_mm": spacing, "offsets": offset_rows})
        for row in offset_rows:
            print(
                f"    比={ratio:>4.2f} spacing={row['actual_spacing_mm']:.4f}mm "
                f"shift={row['z_shift_over_spacing']:.2f}*sp  "
                f"個数(格子/塊内)={row['grid_elements']}/{row['inside_design']}  "
                f"層数={row['thickness_layers']}  判定={row['verdict']}  "
                f"({row['seconds']:.1f}秒)",
                flush=True,
            )
    return {"thickness_mm": thickness, "threshold": threshold, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="厚み1〜4層での安全率のずれ、と個数/層数の対応を測る")
    parser.add_argument("--section", choices=["a", "b", "all"], default="all")
    parser.add_argument("--ns", type=int, nargs="+", default=[1, 2, 3, 4])
    # docs/TOPOPT_THICKNESS.md の主要な表・再現手順は 16 を参照解として使っている
    # (52.6秒で完走、n=8への切り下げは不要だった)。既定をそれと揃える。
    parser.add_argument("--reference-n", type=int, default=16)
    parser.add_argument("--thickness", type=float, default=5.0)
    parser.add_argument(
        "--ratios", type=float, nargs="+", default=[0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]
    )
    parser.add_argument("--out", type=str, default=None, help="結果JSONの書き出し先(既定は書き出さない)")
    args = parser.parse_args()

    started = time.perf_counter()
    if args.section in ("a", "all"):
        RESULTS["section_a"] = run_section_a(args.ns, args.reference_n)
    if args.section in ("b", "all"):
        RESULTS["section_b"] = run_section_b(args.thickness, args.ratios)
    RESULTS["total_seconds"] = time.perf_counter() - started

    print("\n" + "=" * 72)
    print(f"合計 {RESULTS['total_seconds']:.1f} 秒")
    if args.out:
        from pathlib import Path

        out_path = Path(args.out)
        _record.write(out_path, RESULTS, default=float)
        print(f"書き出し: {out_path}")
    else:
        print("MEASURED " + json.dumps(RESULTS, ensure_ascii=False, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

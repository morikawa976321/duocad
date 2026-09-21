"""`voxelize.py`(B-repソリッド → 規則格子)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_voxelize.py

**このファイルが合否そのものである。** 1件でも落ちればプロセスは非ゼロで終わる。
検査で使う形状は **build123d でその場生成する合成ソリッドだけ**で、リポジトリ外の
私物STEPには依存しない(私物STEP依存でテスト151件を捨てた実例がある)。実物STEPを1回通した実測は docs/TOPOPT.md にある。

検査の構成:

1. **OCCT の神託との一致** — `BRepClass3d_SolidClassifier` の判定と高速経路(レイの巻き数)が
   一致する。無作為に選んだ要素2,000個と、**表面の近くに偏らせて選んだ要素**2,000個。
   比べるのは**要素の中心**であり任意の点ではない — 高速経路が決めているのは中心の内外で、
   任意の点を要素の占有と比べると「中心と点が表面をまたぐ」だけで不一致になる
   (最初にそれをやって4.9%の偽の不一致を出した)。
   **表面から要素サイズの5%以内の中心は集計から除く**(三角形分割の弦誤差 = 要素サイズの2%
   より大きく取ってある)。除いた件数も表示する。
2. **既知形状の体積が収束する** — 円筒と傾けた箱で、格子に対する形状の位置を**サブセル単位で
   ずらした10通りの平均誤差**が、解像度を上げると単調に減ることを見る。1つの解像度の
   1点の誤差では駄目 — 格子と面の当たり方は解像度ごとに当たり外れがあり、実測でも
   円筒の相対誤差は n=16 で 0.4%、n=24 で 3.5% と**増える**(位置をずらして平均すると
   2.6倍ずつ単調に減る)。格子は `Grid` を作って渡してずらす。
3. **貫通穴が埋まらない** — 穴のある板で、穴の内側の要素が1つも占有されない
   (かつ板の実体側は隙間なく埋まる。「全部空」で通らないように両側を見る)。
4. **壊れた入力で黙って通らない** — 開いたシェル・向きを裏返した面を含むシェル・
   裏返したソリッドの3種で `ValueError` になる。
5. **保持/障害物マスクが設計空間の内側に収まる** — 外にはみ出した分が捨てられ、
   捨てた件数が報告され、保持と障害物が重なったら落ちる。
6. **決定性** — 同じ入力で2回呼ぶと格子もマスクも完全に一致する。
7. **セル中心規約の整合** — 要素サイズの整数倍の辺を持つ軸整列の箱は、**どの解像度でも
   体積が厳密に一致する**。原点や中心の取り方が半セルずれていると必ず崩れる
   (1〜6はどれも半セルの平行移動に鈍感なので、これを別に置く)。あわせて `Grid` の
   3つの口(`centers_1d`/`centers_of`/
   `center_of`)が同じ中心を返すことを照合する — 規約の定義が分裂すると片方だけ壊れる。
8. **縮退したレイでも壊れない** — 面や辺が列の中心と厳密に一致する姿勢で、レイのずらしを
   切っても体積が厳密に一致する(巻き数で数えていることの確認。偶奇だと5%低く出る)。
9. **表面までの最短距離(`closest_triangle`)** — stage2 が面へ戻る経路。退化した三角形での
   反例 / 細かく標本化した参照値との突き合わせ / 三角形の重心から `face_index` で元の面へ戻る。
10. **トリムされた曲面のソリッドの bbox が実寸を返す** — `_shape_bbox` を三角形分割の箱と
    突き合わせる(三角形の頂点は必ず真の面の上にあるので、実寸の独立した根拠になる)。

所要は約4秒(SIMPは回さないので check_mesh より桁違いに速い)。
"""

from __future__ import annotations

import json
import math
import sys
import time

import numpy as np

# Windowsの既定コンソール(cp932)で記号1つ落ちるだけで検査が1件も走らず異常終了するのを防ぐ。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        try:
            _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

from build123d import Box, Cone, Cylinder, Pos, Rotation  # noqa: E402
from scipy.spatial import cKDTree  # noqa: E402
from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepClass3d import BRepClass3d_SolidClassifier  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.TopAbs import TopAbs_FACE, TopAbs_IN, TopAbs_ON  # noqa: E402
from OCP.TopExp import TopExp_Explorer  # noqa: E402
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shell  # noqa: E402
from OCP.TopAbs import TopAbs_SHELL  # noqa: E402

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import voxelize as vx  # noqa: E402
from _fixtures import voxel_model  # noqa: E402

FAILURES: list[str] = []
RESULTS: dict[str, object] = {}

# 曖昧と見なす帯(要素サイズに対する比)。三角形分割の既定の弦誤差(2%)より大きく取る。
AMBIGUOUS_BAND = 0.05


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。落ちても即死せず全件出してから終わる。"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _faces_of(shape) -> list:
    solid = vx._unwrap(shape)
    out = []
    explorer = TopExp_Explorer(solid, TopAbs_FACE)
    while explorer.More():
        out.append(TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    return out


def _shell_of(faces) -> TopoDS_Shell:
    builder = BRep_Builder()
    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    for face in faces:
        builder.Add(shell, face)
    return shell


# ---------------------------------------------------------------------------
# 1. OCCT の神託(BRepClass3d_SolidClassifier)との一致
# ---------------------------------------------------------------------------
def _sample_cells(model: vx.VoxelModel, rng: np.random.Generator, count: int) -> np.ndarray:
    """格子全体から無作為に要素を選ぶ。"""
    nx, ny, nz = model.grid.shape
    return np.stack(
        [rng.integers(0, nx, count), rng.integers(0, ny, count), rng.integers(0, nz, count)],
        axis=1,
    )


def _sample_cells_near_surface(
    model: vx.VoxelModel, rng: np.random.Generator, count: int
) -> np.ndarray:
    """表面の近く(±1.5要素)に偏らせて要素を選ぶ。面積で重み付けした三角形上の一様点を使う。"""
    a, b, c = model.soup.triangle_vertices()
    areas = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    weight = areas / areas.sum()
    over = count * 3
    ti = rng.choice(len(areas), over, p=weight)
    u = rng.random((over, 1))
    v = rng.random((over, 1))
    flip = (u + v) > 1.0
    u[flip] = 1.0 - u[flip]
    v[flip] = 1.0 - v[flip]
    on_surface = a[ti] + u * (b[ti] - a[ti]) + v * (c[ti] - a[ti])
    normal = np.cross(b[ti] - a[ti], c[ti] - a[ti])
    normal /= np.linalg.norm(normal, axis=1, keepdims=True)
    h = model.grid.spacing
    points = on_surface + normal * rng.uniform(-1.5 * h, 1.5 * h, (over, 1))
    idx = model.grid.index_of(points)
    inside = np.all((idx >= 0) & (idx < np.array(model.grid.shape)), axis=1)
    return idx[inside][:count]


def check_against_occt_oracle() -> None:
    print("[1] OCCT の神託(BRepClass3d_SolidClassifier)との一致")
    cases = [
        ("回転した箱 - 円筒の穴 (n=24)", (Rotation(15, 25, 0) * Box(40, 20, 10)) - Cylinder(6, 40), 24),
        ("回転した箱 - 円筒の穴 (n=48)", (Rotation(15, 25, 0) * Box(40, 20, 10)) - Cylinder(6, 40), 48),
        ("円筒 (n=32)", Cylinder(10, 30), 32),
        ("軸整列の箱 (n=20)", Box(40, 20, 10), 20),
        # 円錐の頂点は三角形が数十枚集まる点で、レイがそこを厳密に貫くと巻き数だけでは
        # 守れない(検査8がその縮退を直接見る)。神託と突き合わせるのは、ずらしを入れた
        # 結果が**正しい**ことの確認。
        ("円錐 (n=34)", Cone(10, 0, 20), 34),
        ("円錐+板 (n=20)", Cone(10, 0, 20) + Box(30, 30, 4), 20),
    ]
    total_compared = 0
    total_mismatch = 0
    total_excluded = 0
    for label, shape, n in cases:
        try:
            model = voxel_model(shape, n)
        except Exception as exc:  # 落ちたらトレースバックで死なずに1件のFAILとして出す
            check(f"神託と一致: {label}", False, f"voxelize が落ちた: {type(exc).__name__}: {str(exc)[:110]}")
            continue
        h = model.grid.spacing
        rng = np.random.default_rng(20260830)
        cells = np.vstack(
            [_sample_cells(model, rng, 2000), _sample_cells_near_surface(model, rng, 2000)]
        )
        centers = model.grid.centers_of(cells)
        distance = vx.distance_to_surface(model.soup, centers)
        band = AMBIGUOUS_BAND * h
        usable = distance > band
        excluded_band = int(np.count_nonzero(~usable))

        fast = model.design[cells[:, 0], cells[:, 1], cells[:, 2]]
        classifier = BRepClass3d_SolidClassifier(vx._unwrap(shape))
        oracle = np.zeros(len(cells), dtype=bool)
        excluded_on = 0
        t0 = time.perf_counter()
        for i in np.flatnonzero(usable):
            classifier.Perform(gp_Pnt(*centers[i]), 1e-9)
            state = classifier.State()
            if state == TopAbs_ON:  # OCCT自身が「表面上」と言った点も曖昧なので除く
                excluded_on += 1
                usable[i] = False
                continue
            oracle[i] = state == TopAbs_IN
        oracle_seconds = time.perf_counter() - t0

        compared = int(np.count_nonzero(usable))
        mismatch = int(np.count_nonzero(usable & (fast != oracle)))
        total_compared += compared
        total_mismatch += mismatch
        total_excluded += excluded_band + excluded_on
        rate = mismatch / max(compared, 1)
        check(
            f"神託と一致: {label}",
            mismatch == 0 and compared > 3000,
            f"照合{compared}件 / 不一致{mismatch}件 (率{rate:.5f}) / "
            f"除外: 表面から{AMBIGUOUS_BAND:.0%}以内{excluded_band}件 + OCCTがONと判定{excluded_on}件 / "
            f"要素サイズ{h:.4f}mm / 神託{oracle_seconds:.2f}秒",
        )
    RESULTS["oracle_compared"] = total_compared
    RESULTS["oracle_mismatch"] = total_mismatch
    RESULTS["oracle_excluded"] = total_excluded
    RESULTS["oracle_mismatch_rate"] = total_mismatch / max(total_compared, 1)


# ---------------------------------------------------------------------------
# 2. 既知形状の体積が収束する(位置をずらした平均の単調減少)
# ---------------------------------------------------------------------------
# 格子に対する形状の位置を要素サイズ単位でずらす量。乱数ではなく固定表(決定性のため)。
# 余白が1要素あるので |ずらし| < 0.5 なら形状は格子からはみ出さない。
SUBCELL_SHIFTS = [
    (0.00, 0.00, 0.00),
    (0.37, 0.11, -0.29),
    (-0.41, 0.23, 0.17),
    (0.19, -0.33, 0.43),
    (-0.07, 0.44, -0.13),
    (0.28, -0.19, -0.39),
    (-0.31, -0.09, 0.31),
    (0.44, 0.36, 0.07),
    (-0.22, 0.05, -0.45),
    (0.09, -0.42, 0.25),
]


def _mean_volume_error(shape, analytic: float, n: int) -> tuple[float, float]:
    base = voxel_model(shape, n)
    grid = base.grid
    errors = []
    for shift in SUBCELL_SHIFTS:
        shifted = vx.Grid(
            origin=tuple(grid.origin[i] + shift[i] * grid.spacing for i in range(3)),
            spacing=grid.spacing,
            shape=grid.shape,
        )
        model = voxel_model(shape, n, grid=shifted)
        errors.append(abs(model.design_volume - analytic) / analytic)
    arr = np.array(errors)
    return float(arr.mean()), float(arr.max())


def check_volume_converges() -> None:
    print("[2] 既知形状の体積が収束する(位置をずらした平均誤差の単調減少)")
    ladder = (8, 16, 32, 64)
    cases = [
        ("円筒 r=10 h=30", Cylinder(10, 30), float(np.pi * 10.0**2 * 30.0)),
        ("傾けた箱 40x20x10", Rotation(30, 20, 10) * Box(40, 20, 10), 8000.0),
    ]
    for label, shape, analytic in cases:
        means = []
        maxes = []
        for n in ladder:
            mean_err, max_err = _mean_volume_error(shape, analytic, n)
            means.append(mean_err)
            maxes.append(max_err)
        monotone_mean = all(means[i + 1] < means[i] for i in range(len(means) - 1))
        monotone_max = all(maxes[i + 1] < maxes[i] for i in range(len(maxes) - 1))
        detail = " → ".join(f"n={n}:{e:.5f}" for n, e in zip(ladder, means))
        check(
            f"平均誤差が単調に減る: {label}",
            monotone_mean,
            f"{detail} (10通りの位置ずらしの平均)",
        )
        check(
            f"最悪誤差も単調に減る: {label}",
            monotone_max,
            " → ".join(f"n={n}:{e:.5f}" for n, e in zip(ladder, maxes)),
        )
        check(
            f"最細で誤差1%未満: {label}",
            means[-1] < 0.01,
            f"n={ladder[-1]} 平均誤差 {means[-1]:.5f}",
        )
        RESULTS[f"convergence_{label.split()[0]}"] = [round(v, 6) for v in means]


# ---------------------------------------------------------------------------
# 3. 貫通穴が埋まらない
# ---------------------------------------------------------------------------
def check_through_hole() -> None:
    print("[3] 貫通穴が埋まらない")
    radius = 5.0
    plate = Box(40, 40, 6) - Cylinder(radius, 20)
    model = voxel_model(plate, 40)
    grid = model.grid
    h = grid.spacing
    cx = grid.centers_1d(0)[:, None, None]
    cy = grid.centers_1d(1)[None, :, None]
    cz = grid.centers_1d(2)[None, None, :]
    r = np.sqrt(cx**2 + cy**2)
    in_plate = np.abs(cz) < 3.0 - h          # 板の厚みの内側(境界の1層は曖昧なので外す)
    hole = (r < radius - h) & in_plate       # 穴の内側(境界から1要素内側)
    ring = (r > radius + h) & (r < 20.0 - h) & in_plate  # 実体側

    filled_in_hole = int(np.count_nonzero(model.design & hole))
    empty_in_ring = int(np.count_nonzero(~model.design & ring))
    check(
        "穴の内側が占有されない",
        filled_in_hole == 0 and int(hole.sum()) > 50,
        f"穴の内側の要素{int(hole.sum())}個のうち占有{filled_in_hole}個",
    )
    check(
        "実体側は隙間なく埋まる(「全部空」で通らないことの確認)",
        empty_in_ring == 0 and int(ring.sum()) > 500,
        f"実体側の要素{int(ring.sum())}個のうち空{empty_in_ring}個",
    )
    RESULTS["hole_cells"] = int(hole.sum())
    RESULTS["hole_filled"] = filled_in_hole


# ---------------------------------------------------------------------------
# 4. 壊れた入力で黙って通らない
# ---------------------------------------------------------------------------
def check_broken_input_raises() -> None:
    print("[4] 壊れた入力で黙って通らない")
    box = Box(30, 20, 10)
    faces = _faces_of(box)
    cases = [
        ("開いたシェル(1面欠け)", _shell_of(faces[:-1])),
        ("向きを裏返した面を含むシェル", _shell_of(faces[:-1] + [faces[-1].Reversed()])),
        ("裏返したソリッド(法線が内向き)", vx._unwrap(box).Reversed()),
    ]
    for label, broken in cases:
        try:
            model = voxel_model(broken, 12)
        except ValueError as exc:
            check(f"壊れた入力で例外: {label}", True, f"ValueError: {str(exc).splitlines()[0][:90]}")
            continue
        except Exception as exc:  # 想定外の型の例外は「黙って通らない」を満たすが不親切
            check(
                f"壊れた入力で例外: {label}",
                False,
                f"ValueError 以外で落ちた: {type(exc).__name__}: {exc}",
            )
            continue
        check(
            f"壊れた入力で例外: {label}",
            False,
            f"例外にならず格子を返した(占有{model.stats['design_elements']}要素) — "
            "もっともらしいゴミを返している",
        )
    # **内部空洞の殻だけを裏返した入力**(2026-08-31のQA指摘)。総符号付き体積は正のまま
    # なので、全体を1つの数で見る判定では見抜けず、空洞が材料として埋まる。
    hollow = Box(40, 40, 40) - Box(20, 20, 20)
    shells = []
    explorer = TopExp_Explorer(vx._unwrap(hollow), TopAbs_SHELL)
    while explorer.More():
        shells.append(TopoDS.Shell_s(explorer.Current()))
        explorer.Next()
    inner = min(
        shells, key=lambda sh: abs(vx.triangles_from_shape(sh, deviation=0.5).signed_volume())
    )
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for sh in shells:
        builder.Add(compound, sh.Reversed() if sh.IsSame(inner) else sh)
    try:
        broken = voxel_model(compound, 20)
        check(
            "壊れた入力で例外: 内部空洞の殻だけを裏返した中空の箱",
            False,
            f"例外にならず占有{broken.stats['design_elements']}要素を返した"
            f"(体積{broken.design_volume:.0f} mm^3 / 健全なら56000)。空洞が埋まっている",
        )
    except ValueError as exc:
        check(
            "壊れた入力で例外: 内部空洞の殻だけを裏返した中空の箱",
            True,
            f"ValueError: {str(exc).splitlines()[0][:90]}",
        )
    # 健全な中空の箱は通り、空洞が空のままであること(上の検査が「空洞のある形は全部落とす」
    # 実装で緑にならないことの確認)
    good_hollow = voxel_model(hollow, 20)
    surface = good_hollow.stats["surface"]
    check(
        "健全な中空の箱は通り、空洞が空のまま(殻ごとの符号と入れ子の深さが取れている)",
        good_hollow.design_volume == 56000.0
        and surface["components"] == 2
        and surface["component_depths"] == [0, 1]
        and surface["component_volumes"][1] < 0,
        f"体積{good_hollow.design_volume:.0f} mm^3(期待56000) / 殻{surface['components']}個 "
        f"体積{surface['component_volumes']} 深さ{surface['component_depths']}",
    )

    # `inspect_soup` を通さずに `occupancy` を直接呼んだ場合の守り。上の3件は
    # `inspect_soup` が先に落とすので、占有判定側の守り(巻き数が0に戻らない列を
    # 見つけたら落ちる)は素通りしている。stage2 が `occupancy` を直に呼ぶことは
    # ありうるので、ここだけ切り出して確かめる。
    good = vx.triangles_from_shape(vx._unwrap(Box(30, 20, 10)), deviation=0.05)
    grid = vx.grid_for_bbox(*good.bbox, 12)
    open_soup = vx.TriangleSoup(
        positions=good.positions,
        triangles=good.triangles[:-2],   # 上面の三角形2枚を落として穴を開ける
        face_index=good.face_index[:-2],
        face_kind=good.face_kind,
        deviation=good.deviation,
    )
    try:
        vx.occupancy(open_soup, grid)
        check("占有判定も、閉じていない三角形群を渡されたら落ちる", False, "例外にならなかった")
    except ValueError as exc:
        check(
            "占有判定も、閉じていない三角形群を渡されたら落ちる",
            True,
            f"ValueError: {str(exc)[:80]}",
        )

    # 健全な入力は通ること(検査4が「常に落ちる」実装で通らないことの確認)
    try:
        voxel_model(box, 12)
        check("健全な入力は通る(検査4が常に落ちる実装でないことの確認)", True, "Box(30,20,10) n=12 OK")
    except Exception as exc:
        check("健全な入力は通る(検査4が常に落ちる実装でないことの確認)", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 5. 保持/障害物マスクが設計空間の内側に収まる
# ---------------------------------------------------------------------------
def check_keep_and_obstacle() -> None:
    print("[5] 保持/障害物マスクが設計空間の内側に収まる")
    design = Box(40, 20, 10)
    keep = Pos(14, 0, 0) * Box(12, 20, 10)       # +x 端まで(設計空間の内側)
    obstacle = Pos(-10, 0, 0) * Cylinder(4, 10)  # 設計空間を z に貫く円筒
    model = voxel_model(design, 24, keep=keep, obstacle=obstacle)

    outside_keep = int(np.count_nonzero(model.keep & ~model.design))
    outside_obstacle = int(np.count_nonzero(model.obstacle & ~model.design))
    check(
        "保持領域が設計空間の外に出ていない",
        outside_keep == 0 and model.stats["keep_elements"] > 0,
        f"保持{model.stats['keep_elements']}要素 / 設計空間の外{outside_keep}要素",
    )
    check(
        "障害物領域が設計空間の外に出ていない",
        outside_obstacle == 0 and model.stats["obstacle_elements"] > 0,
        f"障害物{model.stats['obstacle_elements']}要素 / 設計空間の外{outside_obstacle}要素",
    )
    raised = None
    try:
        voxel_model(design, 24, keep=Pos(18, 0, 0) * Box(12, 20, 10))  # +x 側へ 4mm はみ出す
    except vx.OutsideDesignSpaceError as exc:
        raised = exc
    check(
        "はみ出した保持を黙って切り詰めず、要素数つきで拒否する",
        raised is not None and raised.outside.get("keep", 0) > 0,
        f"outside={getattr(raised, 'outside', None)}",
    )
    free = model.free
    check(
        "free = 設計空間 - 保持 - 障害物 が整合する",
        int(np.count_nonzero(free & (model.keep | model.obstacle))) == 0
        and int(np.count_nonzero(free)) > 0,
        f"free{int(np.count_nonzero(free))}要素 / "
        f"設計{model.stats['design_elements']} - 保持{model.stats['keep_elements']} "
        f"- 障害物{model.stats['obstacle_elements']}",
    )
    try:
        voxel_model(design, 24, keep=Box(20, 20, 10), obstacle=Box(10, 10, 10))
        check("保持と障害物が重なったら落ちる", False, "例外にならなかった")
    except ValueError as exc:
        check("保持と障害物が重なったら落ちる", True, f"ValueError: {str(exc).splitlines()[0][:70]}")

    # 設計空間とまったく重ならない領域(2026-08-31のQA指摘)。黙って空にすると、支えの無い
    # 構造がもっともらしく出てくる。**どの領域が / どこにあって / 格子はどこか**が分かる
    # 文言で落ちること(占有判定の奥から出る「交点が1つも無い」では呼び出し側に分からない)。
    for label, kwargs in (
        ("保持", {"keep": Pos(100, 0, 0) * Box(10, 10, 10)}),
        ("障害物", {"obstacle": Pos(0, 0, 90) * Box(5, 5, 5)}),
    ):
        try:
            voxel_model(design, 24, **kwargs)
            check(f"設計空間の外にある{label}領域を拒否する", False, "例外にならなかった")
        except ValueError as exc:
            message = str(exc).splitlines()[0]
            check(
                f"設計空間の外にある{label}領域を、何が悪いか分かる文言で拒否する",
                label in message and "格子の範囲" in message,
                f"ValueError: {message[:110]}",
            )
    RESULTS["keep_elements"] = model.stats["keep_elements"]
    RESULTS["keep_outside_rejected"] = None if raised is None else raised.outside.get("keep")


# ---------------------------------------------------------------------------
# 6. 決定性
# ---------------------------------------------------------------------------
def check_deterministic() -> None:
    print("[6] 決定性")
    shape = (Box(40, 40, 6) - Cylinder(5, 20)) - Pos(12, 0, 0) * Cylinder(3, 20)
    keep = Pos(-16, 0, 0) * Box(8, 40, 6)
    obstacle = Pos(0, 15, 0) * Box(10, 10, 6)
    first = voxel_model(shape, 36, keep=keep, obstacle=obstacle)
    second = voxel_model(shape, 36, keep=keep, obstacle=obstacle)
    same = (
        first.grid == second.grid
        and np.array_equal(first.design, second.design)
        and np.array_equal(first.keep, second.keep)
        and np.array_equal(first.obstacle, second.obstacle)
    )
    check(
        "同じ入力で2回呼ぶと格子もマスクも完全に一致する",
        same,
        f"格子{first.grid.shape} / 占有{first.stats['design_elements']}要素 / "
        f"差分{int(np.count_nonzero(first.design != second.design))}要素",
    )

    # **同じ形状オブジェクトを使い回したときも**同じでなければならない。OCCT は
    # 三角形分割を形状に貼り付けたままにするので、bbox を「既にある三角形分割から」
    # 取ると 1回目と2回目で違う値になり、そこから決まる要素サイズ・分割の細かさ・
    # 三角形群まで変わる(2026-08-31にこの欠陥を実測で発見。実物STEPで三角形数が
    # 62264 → 62168 → 62138 と動いた)。曲面のある形でないと露見しないので円筒を使い、
    # **別の解像度を1回挟んでから**同じ解像度をやり直す。三角形群まで比べるのは、
    # 占有マスクが同じでも stage2 が使う面添字つき三角形が変われば再現しないため。
    curved = vx._unwrap(Cylinder(10, 30))
    runs = []
    for n in (37, 61, 37, 37):
        model = voxel_model(curved, n)
        runs.append(
            (
                n,
                model.soup.triangle_count,
                int(np.count_nonzero(model.design)),
                model.grid.shape,
                model.grid.spacing,
                float(model.soup.positions.sum()),
            )
        )
    repeats = [r for r in runs if r[0] == 37]
    check(
        "同じ形状オブジェクトを使い回しても(別解像度を挟んでも)結果が変わらない",
        repeats[0] == repeats[1] == repeats[2],
        f"n=37 の3回: 三角形数{[r[1] for r in repeats]} 占有{[r[2] for r in repeats]} "
        f"要素サイズ{[r[4] for r in repeats]}",
    )


# ---------------------------------------------------------------------------
# 7. セル中心規約の整合(半セルずれの検出)
# ---------------------------------------------------------------------------
def check_cell_center_convention() -> None:
    print("[7] セル中心規約の整合(半セルずれの検出)")
    # (a) 辺長が要素サイズの整数倍になる軸整列の箱は、どの解像度でも体積が厳密に一致する。
    #     幅 L の区間に入るセル中心の数は、L が要素サイズの整数倍なら位置によらず L/h 個。
    #     原点や中心の取り方が半セルずれると必ず崩れる。
    exact = []
    for n in (8, 16, 20, 32, 40, 64):
        model = voxel_model(Box(40, 20, 10), n)
        exact.append((n, model.design_volume))
    worst = max(abs(v - 8000.0) for _n, v in exact)
    check(
        "要素サイズの整数倍の箱は全解像度で体積が厳密一致",
        worst == 0.0,
        " / ".join(f"n={n}:{v:.4f}" for n, v in exact) + f" (期待8000, 最大誤差{worst:g})",
    )

    # (b) 占有要素の中心が張る範囲が、セル中心規約の期待どおりの位置にある。
    #     期待: 箱 [-20,20] に対して、中心の最小 = -20 + h/2、最大 = 20 - h/2。
    #     半セルずれていれば ±h ずれる(閾値 0.25h でどちらとも明確に区別できる)。
    model = voxel_model(Box(40, 20, 10), 20)
    h = model.grid.spacing
    idx = np.argwhere(model.design)
    centers = model.grid.centers_of(idx)
    got_min = centers.min(axis=0)
    got_max = centers.max(axis=0)
    expected_min = np.array([-20.0, -10.0, -5.0]) + h / 2.0
    expected_max = np.array([20.0, 10.0, 5.0]) - h / 2.0
    err = max(
        float(np.max(np.abs(got_min - expected_min))),
        float(np.max(np.abs(got_max - expected_max))),
    )
    check(
        "占有要素の中心の範囲がセル中心規約と一致(半セルずれていない)",
        err < 0.25 * h,
        f"min={got_min.tolist()}(期待{expected_min.tolist()}) "
        f"max={got_max.tolist()}(期待{expected_max.tolist()}) 誤差{err:.6g}mm (要素{h}mm)",
    )

    # (c) Grid の変換が往復する: 中心 → 添字 → 中心。
    rng = np.random.default_rng(7)
    nx, ny, nz = model.grid.shape
    sample = np.stack(
        [rng.integers(0, nx, 500), rng.integers(0, ny, 500), rng.integers(0, nz, 500)], axis=1
    )
    roundtrip = model.grid.index_of(model.grid.centers_of(sample))
    check(
        "Grid.centers_of → Grid.index_of が往復する",
        np.array_equal(roundtrip, sample),
        f"500点中 不一致{int(np.count_nonzero(np.any(roundtrip != sample, axis=1)))}点",
    )

    # (d2) **B-rep の bbox** に対して1要素以上の余白があること(2026-08-31のQA指摘)。
    #      三角形分割の bbox から格子を作り直すと、弦が真の面より内側に来るぶんだけ
    #      余白が1要素を割る。曲面のある形でしか露見しないので円筒と円錐で見る。
    worst = None
    for shape, name in ((Cylinder(10, 30), "円筒"), (Cone(10, 0, 20), "円錐"), (Box(40, 20, 10), "箱")):
        for n in (17, 37, 64):
            model_n = voxel_model(shape, n)
            brep_lo, brep_hi = vx._shape_bbox(vx._unwrap(shape))
            grid_lo, grid_hi = model_n.grid.world_box()
            margin = float(
                np.concatenate([brep_lo - grid_lo, grid_hi - brep_hi]).min()
            ) / model_n.grid.spacing
            if worst is None or margin < worst[0]:
                worst = (margin, name, n)
    check(
        "B-repのバウンディングボックスに対して1要素以上の余白がある",
        worst[0] >= 1.0 - 1e-9,
        f"最小の余白 {worst[0]:.6f} 要素 ({worst[1]} n={worst[2]})",
    )
    RESULTS["min_margin_elements"] = round(worst[0], 6)

    # (d3) 原点から遠いソリッドでも符号付き体積が桁落ちしない(2026-08-31のQA指摘)。
    #      絶対座標のまま三重積を足すと、薄い形では符号が反転して健全な形状を拒否する。
    volumes = []
    for offset in (0.0, 1e4, 1e6, 1e8, 1e10):
        far = vx.triangles_from_shape(
            vx._unwrap(Pos(offset, offset, offset) * Box(40, 20, 10)), deviation=0.5
        )
        volumes.append(far.signed_volume())
    check(
        "原点から遠いソリッドでも符号付き体積が桁落ちしない",
        all(abs(v - 8000.0) < 1e-6 for v in volumes),
        f"原点/1e4/1e6/1e8/1e10 mm での符号付き体積 {[round(v, 6) for v in volumes]}(期待8000)",
    )

    # (d4) **殻ごとの**符号付き体積も、その殻自身の点を基準点にすること(2026-08-31のQA第2
    #      ラウンドの指摘)。三角形の側だけ部分集合にして基準点を soup 全体から取ると、
    #      離れた位置にある小さな殻でその殻の座標が基準点から大きく離れ、桁落ちが戻ってくる。
    #      軸整列した箱では三重積が偶然きれいに相殺してしまうので、**傾けた**小箱で見る。
    far_shells = []
    for source in (Box(40, 40, 40) - Box(20, 20, 20), Pos(1e7, 1e7, 1e7) * (Rotation(17, 23, 31) * Box(4, 4, 4))):
        explorer = TopExp_Explorer(vx._unwrap(source), TopAbs_SHELL)
        while explorer.More():
            far_shells.append(TopoDS.Shell_s(explorer.Current()))
            explorer.Next()
    builder = BRep_Builder()
    far_compound = TopoDS_Compound()
    builder.MakeCompound(far_compound)
    for sh in far_shells:
        builder.Add(far_compound, sh)
    far_soup = vx.triangles_from_shape(far_compound, deviation=0.5)
    assert far_soup.signed_volume(np.array([], dtype=int)) == 0.0, (
        "三角形が1枚も無い部分集合の符号付き体積は0でなければならない"
        "(stage2 が signed_volume を直に呼ぶ。基準点を空配列から取ると落ちる)"
    )
    far_report = vx.inspect_soup(far_soup)
    far_volumes = sorted(far_report["component_volumes"])
    check(
        "殻ごとの符号付き体積も、遠くに離れた小さな殻で桁落ちしない",
        far_report["components"] == 3
        and abs(far_volumes[0] + 8000.0) < 1e-6
        and abs(far_volumes[1] - 64.0) < 1e-6
        and abs(far_volumes[2] - 64000.0) < 1e-6,
        f"殻{far_report['components']}個 体積{far_volumes}(期待 [-8000, 64, 64000]。"
        "基準点を soup 全体から取ると 1e7mm 先の小箱が 3542.34 になる)",
    )

    # (d) 余白がちょうど pad 要素ぶんある(格子が形状にぴったり張り付いていない)。
    lo, hi = model.grid.world_box()
    margin_lo = np.array([-20.0, -10.0, -5.0]) - lo
    margin_hi = hi - np.array([20.0, 10.0, 5.0])
    check(
        "バウンディングボックスの周囲に1要素以上の余白がある",
        float(margin_lo.min()) >= h - 1e-9 and float(margin_hi.min()) >= h - 1e-9,
        f"余白(低い側)={np.round(margin_lo, 6).tolist()} (高い側)={np.round(margin_hi, 6).tolist()} "
        f"/ 要素{h}mm",
    )
    # (e) 規約の定義が分裂していないこと。`centers_1d`(軸ごと)と `centers_of`(添字の配列)は
    #     同じセル中心を返さなければならない。2026-08-31の変異検査で、`centers_1d` 側の
    #     +0.5 だけを +1.0 に壊しても全検査が緑のまま通ったため、この照合を足した。
    grid = model.grid
    nx, ny, nz = grid.shape
    idx = np.stack(np.meshgrid(np.arange(nx), np.arange(ny), np.arange(nz), indexing="ij"), -1)
    from_of = grid.centers_of(idx.reshape(-1, 3)).reshape(nx, ny, nz, 3)
    from_1d = np.stack(
        np.meshgrid(grid.centers_1d(0), grid.centers_1d(1), grid.centers_1d(2), indexing="ij"),
        axis=-1,
    )
    single = np.array([grid.center_of(1, 2, 3), grid.centers_of(np.array([[1, 2, 3]]))[0]])
    check(
        "Grid の3つの口(centers_1d / centers_of / center_of)が同じ中心を返す",
        np.allclose(from_of, from_1d, atol=1e-12) and np.allclose(single[0], single[1], atol=1e-12),
        f"最大差 {float(np.max(np.abs(from_of - from_1d))):.3g}mm / "
        f"center_of(1,2,3)={single[0].tolist()}",
    )
    RESULTS["cell_center_err_mm"] = err


# ---------------------------------------------------------------------------
# 8. 縮退したレイ(面や辺が列の中心と厳密に一致する場合)で壊れない
# ---------------------------------------------------------------------------
def check_degenerate_rays() -> None:
    print("[8] 縮退したレイでも壊れない(巻き数で数えていることの確認)")
    # 40x40 の上面は2枚の三角形に割れ、その対角線 y=x が列の中心 (±1,±1),(±3,±3),... を
    # **厳密に**通る。すると1本のレイが2枚の三角形の共有辺に当たり、同じ z に交点が2つ出る。
    # 偶奇で数えるとその列の内外が反転する(実測15200 mm^3 / 正解16000 mm^3)。
    # 巻き数なら「入る/出る」の符号が相殺して正しいまま。既定のジッタも同じ縮退を外すが、
    # 二重の守りのどちらか片方が消えても検出できるようにジッタを切って確かめる。
    soup = vx.triangles_from_shape(vx._unwrap(Box(40, 40, 10)), deviation=0.04)
    grid = vx.grid_for_bbox(*soup.bbox, 20)
    jittered, stats_j = vx.occupancy(soup, grid)
    plain, stats_p = vx.occupancy(soup, grid, jitter=(0.0, 0.0))
    exact = 40.0 * 40.0 * 10.0
    check(
        "レイのずらしを切っても体積が厳密に一致する",
        plain.sum() * grid.element_volume == exact,
        f"ずらし無し={plain.sum() * grid.element_volume:.1f} / "
        f"ずらし有り={jittered.sum() * grid.element_volume:.1f} / 正解{exact:.1f} mm^3",
    )
    check(
        "その姿勢で実際に縮退が起きている(検査が空振りでないことの確認)",
        stats_p["crossings"] > stats_j["crossings"],
        f"交点数 ずらし無し{stats_p['crossings']} > ずらし有り{stats_j['crossings']} "
        "(差が共有辺の二重計上)",
    )
    check(
        "巻き数が0に戻らない列が無い",
        stats_p["unbalanced_columns"] == 0 and stats_j["unbalanced_columns"] == 0,
        f"ずらし無し{stats_p['unbalanced_columns']}本 / ずらし有り{stats_j['unbalanced_columns']}本",
    )
    RESULTS["degenerate_extra_crossings"] = stats_p["crossings"] - stats_j["crossings"]

    # 巻き数だけでは守れない縮退もある: 円錐の頂点は三角形が数十枚集まる点で、レイが
    # そこを厳密に貫くと「出る」ばかりが数十回数えられ、巻き数が0に戻らない。ここは
    # レイを列の中心からわずかにずらす守り(RAY_JITTER)が効く。**両方の守りが要る**
    # ことをこの2件で示す(2026-08-31、円錐+板がずらし無しでは n=6〜40 の全解像度で
    # 落ちることを実測して発見)。
    apex = Cone(10, 0, 20) + Box(30, 30, 4)
    apex_soup = vx.triangles_from_shape(vx._unwrap(apex), deviation=0.02)
    apex_grid = vx.grid_for_bbox(*apex_soup.bbox, 20)
    try:
        with_jitter, stats_w = vx.occupancy(apex_soup, apex_grid)
    except ValueError as exc:
        # 既定のずらしが効いていない(例: RAY_JITTER が両軸とも0)と、ここで落ちる。
        check(
            "既定のレイのずらしで円錐+板のボクセル化が通る",
            False,
            f"既定の設定で ValueError: {str(exc)[:110]}",
        )
        return
    degenerated = False
    detail = ""
    try:
        without, stats_o = vx.occupancy(apex_soup, apex_grid, jitter=(0.0, 0.0))
        degenerated = not np.array_equal(with_jitter, without)
        detail = (
            f"ずらし無しは例外にならなかったが {int(np.count_nonzero(with_jitter != without))} "
            f"要素ちがう (巻き数が戻らない列{stats_o['unbalanced_columns']}本)"
        )
    except ValueError as exc:
        degenerated = True
        detail = f"ずらし無しは ValueError: {str(exc)[:60]}"
    check(
        "頂点に三角形が集まる形(円錐)では、レイのずらしが実際に効いている",
        degenerated and stats_w["unbalanced_columns"] == 0,
        f"{detail} / ずらし有りは正常(巻き数が戻らない列{stats_w['unbalanced_columns']}本)",
    )
    # この形の**量的な**正しさは検査1(「円錐+板 (n=20)」の神託照合)が見ている。ここで
    # 体積を突き合わせないのは、厚さ4mmの板が要素サイズで割り切れず、n=96(要素0.31mm)
    # でも量子化誤差が±4%で振動するため — 閾値を置くと形の正しさではなく板の厚みと
    # 要素サイズの当たり外れを判定してしまう。
    # **ずらし幅が1つの固定値では守れない**(2026-08-31のQA指摘)。既定のずらし量に
    # ちょうど合わせた格子を作れば、その1回目は必ず縮退する。決定的な列で数回やり直す
    # 経路が実際に効いていること(1回目で落ちる格子を渡して最終的に成功すること)を見る。
    first = vx.RAY_JITTER_SEQUENCE[0]
    base = vx.grid_for_bbox(*apex_soup.bbox, 20)
    h = base.spacing
    aligned = vx.Grid(
        origin=(
            0.0 - h * (10 + 0.5 + first[0]),   # 円錐の頂点 (x=0,y=0) を列の中心が厳密に貫く
            0.0 - h * (10 + 0.5 + first[1]),
            base.origin[2],
        ),
        spacing=h,
        shape=base.shape,
    )
    single_failed = False
    try:
        vx.occupancy(apex_soup, aligned, jitter=first)
    except ValueError:
        single_failed = True
    try:
        retried, stats_r = vx.occupancy(apex_soup, aligned)
        recovered = stats_r["unbalanced_columns"] == 0 and int(retried.sum()) > 0
        attempts = stats_r["jitter_attempts"]
        detail = f"やり直し{attempts}回目のずらし{stats_r['jitter']}で成功、占有{int(retried.sum())}要素"
    except ValueError as exc:
        recovered = False
        attempts = len(vx.RAY_JITTER_SEQUENCE)
        detail = f"やり直しても失敗: {str(exc)[:70]}"
    check(
        "既定のずらしに合わせた格子でも、やり直しで最終的に成功する",
        single_failed and recovered and attempts >= 2,
        f"1回目のずらし単体では{'落ちた' if single_failed else '落ちなかった'} / {detail}",
    )
    RESULTS["jitter_retry_attempts"] = attempts

    check(
        "ずらし有りの円錐+板が空でないマスクを返す",
        int(with_jitter.sum()) > 0,
        f"占有{int(with_jitter.sum())}要素 / 交点{stats_w['crossings']}個",
    )


# ---------------------------------------------------------------------------
# 9. 表面までの最短距離(stage2 が面へ戻る経路)
# ---------------------------------------------------------------------------
def check_closest_triangle() -> None:
    print("[9] 表面までの最短距離(closest_triangle)")
    # (a) **退化した三角形**(2頂点が重なる)でも正しい。2026-08-31のQA指摘の反例そのもの。
    #     教科書の領域分けは分岐が排他であることを前提にしていて、退化すると複数の領域が
    #     同時に真になり、後から上書きしたほうが勝って誤った距離を返す。退化三角形は
    #     OCCTの三角形分割に実際に現れる(円錐の頂点まわりなど)ので、入力で弾けない。
    degenerate = vx.TriangleSoup(
        positions=np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [5.0, 4.0, 4.0]]),
        triangles=np.array([[0, 1, 2]]),
        face_index=np.array([1]),
    )
    point = np.array([[10.0, 4.0, -8.0]])
    got = float(vx.closest_triangle(degenerate, point)[0][0])
    expected = 12.638010058771675  # 線分 (0,0,0)-(5,4,4) への厳密な距離
    check(
        "退化した三角形(2頂点が重なる)でも最短距離が正しい",
        abs(got - expected) < 1e-9,
        f"{got:.9f}(期待 {expected:.9f}。領域分けの上書き順の誤りだと 13.416407865 が返る)",
    )

    # (b) 三角形の上を細かく標本化した距離と突き合わせる(実装とは独立な参照)。
    #     自前の値は必ず標本の最小値以下(標本は必ず三角形上の点なので上界)で、
    #     標本の刻みぶんの誤差より小さいところまで一致していること。
    soup = vx.triangles_from_shape(vx._unwrap(Cylinder(10, 30) - Box(6, 40, 40)), deviation=0.4)
    rng = np.random.default_rng(31)
    probes = rng.uniform(-22.0, 22.0, (300, 3))
    mine, tri = vx.closest_triangle(soup, probes)
    a, b, c = soup.triangle_vertices()
    steps = 12
    grid_uv = [
        (i / steps, j / steps) for i in range(steps + 1) for j in range(steps + 1 - i)
    ]
    samples = np.concatenate([a + u * (b - a) + v * (c - a) for u, v in grid_uv], axis=0)
    reference = cKDTree(samples).query(probes)[0]
    longest_edge = float(
        np.max(
            [
                np.linalg.norm(b - a, axis=1),
                np.linalg.norm(c - b, axis=1),
                np.linalg.norm(a - c, axis=1),
            ]
        )
    )
    tolerance = longest_edge / steps
    check(
        "最短距離が標本化した参照値と一致する(自前 <= 参照、差は標本の刻み以内)",
        bool(np.all(mine <= reference + 1e-9))
        and float(np.max(reference - mine)) <= tolerance,
        f"300点 / 最大の超過 {float(np.max(mine - reference)):.3g}mm / "
        f"最大の不足 {float(np.max(reference - mine)):.4f}mm (刻みの上界 {tolerance:.4f}mm)",
    )

    # (c) 面へ戻れること(stage2 が使う経路)。表面上の点は必ずその面の三角形に当たる。
    face_ids = np.unique(soup.face_index)
    ok = 0
    for face in face_ids:
        members = soup.triangles_of_face(int(face))
        centroid = soup.positions[soup.triangles[members[0]]].mean(axis=0)
        _d, hit = vx.closest_triangle(soup, centroid[None, :])
        if int(soup.face_index[hit[0]]) == int(face):
            ok += 1
    check(
        "三角形の重心から face_index で元の面へ戻れる(stage2 の経路)",
        ok == len(face_ids),
        f"{ok}/{len(face_ids)} 面",
    )


# ---------------------------------------------------------------------------
# 10. トリムされた曲面のソリッドの bbox が実寸を返す
# ---------------------------------------------------------------------------
# 三角形分割の弦誤差[mm]。**許容差にも同じ値を使う** —— 三角形分割の箱が真の寸法より
# 小さく出る量の上界がこれだから(実測の膨らみは箱0.000mm・円筒0.012mm)。
TRIMMED_BBOX_DEVIATION = 0.2


def _torus_segment():
    """トリムされたトーラス面だけでできたソリッド(半径100mmの円に沿った1/12周の曲がったバー)。

    `BRepPrimAPI_MakeTorus` の第3引数は掃引角。**面のトリムを見ない bbox はここで化ける** ——
    実寸 32.06 x 55.00 x 20.00mm に対して `BRepBndLib::Add` は 41.12 x 59.53 x 20.00mm を返す
    (2026-09-07実測)。この検査ファイルの他の題材(箱・円筒・円錐)ではこの差が出ない。
    """
    return BRepPrimAPI_MakeTorus(100.0, 10.0, math.pi / 6).Shape()


def _bbox_outward_mm(shape, deviation: float) -> np.ndarray:
    """`_shape_bbox` の6面が三角形分割の箱より**どれだけ外側にあるか**[mm]。負なら内側。

    三角形の頂点は必ず真の面の上にあるので、三角形分割の箱は真の寸法を超えない。
    正しい bbox はそれを必ず包み(6面とも0以上)、弦が内側へ入るぶん(≤ deviation)より
    大きくは膨らまない。
    """
    solid = vx._unwrap(shape)
    low, high = vx._shape_bbox(solid)
    tri_low, tri_high = vx.triangles_from_shape(solid, deviation=deviation).bbox
    return np.concatenate([tri_low - low, high - tri_high])


def check_trimmed_curved_bbox() -> None:
    print("[10] トリムされた曲面のソリッドの bbox が実寸を返す")
    deviation = TRIMMED_BBOX_DEVIATION
    subjects = (
        (Box(40, 20, 10), "箱"),
        (Cylinder(10, 30), "円筒"),
        (_torus_segment(), "トーラス片"),
    )
    slack: dict[str, float] = {}
    for shape, name in subjects:
        outward = _bbox_outward_mm(shape, deviation)
        slack[name] = float(outward.max())
        check(
            f"{name}: bbox が三角形の頂点を1つも切り落とさない",
            float(outward.min()) >= -1e-9,
            f"最も内側の面 {float(outward.min()):+.6f}mm",
        )

    for name in ("箱", "円筒"):
        check(
            f"{name}: bbox の膨らみが三角形分割の弦誤差以内",
            slack[name] <= deviation,
            f"膨らみ {slack[name]:.6f}mm (許容 {deviation}mm)",
        )

    check(
        "トーラス片: bbox の膨らみが三角形分割の弦誤差以内",
        slack["トーラス片"] <= deviation,
        f"膨らみ {slack['トーラス片']:.6f}mm (許容 {deviation}mm)",
    )
    RESULTS["trimmed_torus_bbox_slack_mm"] = round(slack["トーラス片"], 6)
    RESULTS["untrimmed_bbox_slack_mm"] = round(max(slack["箱"], slack["円筒"]), 6)


def main() -> int:
    print("=" * 72)
    print("check_voxelize.py : B-repソリッド → 規則格子(voxelize.py)の受入検査")
    print("=" * 72)
    started = time.perf_counter()
    check_against_occt_oracle()
    check_volume_converges()
    check_through_hole()
    check_broken_input_raises()
    check_keep_and_obstacle()
    check_deterministic()
    check_cell_center_convention()
    check_degenerate_rays()
    check_closest_triangle()
    check_trimmed_curved_bbox()
    RESULTS["total_seconds"] = round(time.perf_counter() - started, 1)

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

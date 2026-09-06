"""`anchor.py`(CAD の面 → 格子の節点の対応表)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_anchor.py

**このファイルが合否そのものである。** 1件でも落ちればプロセスは非ゼロで終わる。
検査で使う形状は **build123d でその場生成する合成ソリッドだけ**で、リポジトリ外の
私物STEPには依存しない(stage1 と同じ理由。engine `duocad-drops-private-step-test-dependency`)。

検査の構成:

0. **節点番号と境界条件の口の契約** — `NodeGrid` の通し番号・自由度番号が
   `simp._build_edof` と一致する。ここがずれると荷重は別の場所に入り、それでも最適化は
   「もっともらしく」収束するので、下の1〜7が全部緑でも意味が無い。**最初に置くのはそのため。**
   あわせて、**`void_mask` だけを渡した `optimize`** が既定の拘束を明示した場合と
   `rho` ビット単位で一致すること(`Model` を直に組んで `fixed` の包含関係を見るだけでは、
   置き換えの回帰が素通りする)と、**拘束不足が拘束不足と名指しして落ちる**ことを見る。
1. **総荷重の保存** — 解像度を2倍にしても、節点荷重の合計が指定値と一致する(相対1e-12)。
   面が複数のときも、荷重が斜めのときも見る。あわせて**圧力の合力が `p x cad_area` と
   一致する**(稜線で捨てた面積に依らないこと。担当面積から出していた修正前のコードに
   当てると 11.7% / 4.5% ずれて落ちる)。
2. **解像度非依存** — 同じ面の節点集合の重心と合計担当面積が、解像度を4倍にすると
   真の面の重心・面積へ**半分以下の誤差**まで近づく(平面と曲面の両方)。
3. **面の取り違えが無い** — 板の上面と側面で節点集合が排他になり、どの節点も自分の面から
   殻の半径以内にあり、それぞれの重心が正しい面の側にある。
4. **法線が合っている** — 平面の面は CAD の法線と厳密に一致し(節点集合の主成分分析から
   出した面の向きとも一致する)、曲面は面積重み平均どうしで一致する。
5. **偽の集中が出ていない** — (a) どの節点も要素1個ぶん(h^2)の1.5倍を超えて担当しない。
   (b) 荷重密度をガウス平滑化して見たとき、面の内側で一様(ずれ1.5%以下)。
   (b) は**面積重みを外した対照(節点で等分)を同じ検査に掛けて、実際に落ちることを
   確認する**(検査が効いている証拠。実測 3.4% / 3.5% で閾値の2倍以上)。
6. **稜線の扱いが宣言どおり** — 全面の節点集合が互いに排他で、競合した節点はすべて
   「最も多く面積を配った面(同点なら面添字の小さい面)」へ行っている。あわせて、
   その帰結として**要素サイズより小さい面は節点が空になる**こと、そのとき荷重・拘束の
   口が黙って0を返さず落ちることを見る。
7. **端から端まで** — 合成ブラケットで「ソリッド → ボクセル化 → 面アンカーで固定/荷重 →
   SIMP → marching cubes → STL」が繋がり、`check_simp.py` と同等の収束指標
   (目標体積±1%・コンプライアンス減少・終盤10反復の変動1%未満)を満たす。

所要は約2分(大半は7の最適化)。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

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

from build123d import Box, Cylinder, Pos, Rotation  # noqa: E402

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import OUTPUT_DIR
from duocad.topopt import anchor as an  # noqa: E402
from duocad.topopt import bench  # noqa: E402
from duocad.topopt import simp  # noqa: E402
from duocad.topopt import voxelize as vx  # noqa: E402

FAILURES: list[str] = []
RESULTS: dict[str, object] = {}

# 検査に使う形。傾けた箱は「軸に整列していない平面」を、半円柱は「曲面」を持つ。
TILTED = Rotation(12.0, 31.0, 7.0) * Box(40, 24, 16)
PLATE = Box(40, 24, 10)
HALF_CYLINDER = Cylinder(10, 30) & Pos(10, 0, 0) * Box(20, 30, 40)
# 検査[5]の曲面。**モジュール定数にしてあるのは `_table` のメモ化を効かせるため**
# (呼ぶたびに `Cylinder(12, 40)` を作ると別物になり、同じ格子を作り直すことになる)。
FULL_CYLINDER = Cylinder(12, 40)


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。落ちても即死せず全件出してから終わる。"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


_TABLES: dict = {}


def _table(shape, n: int, **kw) -> tuple[vx.VoxelModel, an.AnchorTable]:
    """同じ (形, 解像度) は作り直さない。**返す値は素の構築と同一** —— 検査の意味は変えず、
    同じ格子を何度も組み直していたぶんだけを削っている。"""
    key = (id(shape), n, tuple(sorted(kw.items())))
    if key not in _TABLES:
        model = vx.voxelize(shape, n)
        _TABLES[key] = (model, an.build_anchors(model.soup, model.grid, **kw))
    return _TABLES[key]


def _face_of(soup: vx.TriangleSoup, kind: str, normal=None, near=None) -> int:
    found = an.faces_matching(
        soup, kind=kind, normal=normal, normal_tol=1e-3, near_point=near, near_tol=1e-3
    )
    if len(found) != 1:
        raise AssertionError(f"面を一意に選べなかった: kind={kind} normal={normal} → {found}")
    return found[0]


def _tilted_face(soup: vx.TriangleSoup) -> int:
    """傾けた箱のうち、元の +z 面(40x24)に当たる面。回転後の法線で名指しする。"""
    rot = Rotation(12.0, 31.0, 7.0).wrapped.Transformation()
    mat = np.array([[rot.Value(i + 1, j + 1) for j in range(3)] for i in range(3)])
    return _face_of(soup, "plane", normal=mat @ np.array([0.0, 0.0, 1.0]))


# ---------------------------------------------------------------------------
# 0. 節点番号の契約(simp._build_edof と一致するか)
# ---------------------------------------------------------------------------
def check_node_numbering() -> None:
    print("[0] 節点番号と境界条件の口の契約(simp._build_edof / Model と一致)")
    grid = vx.Grid(origin=(-3.0, 1.5, 7.25), spacing=0.75, shape=(5, 4, 3))
    ng = an.NodeGrid(grid)
    nelx, nely, nelz = grid.shape
    check(
        "節点格子の形が要素格子+1",
        ng.shape == (nelx + 1, nely + 1, nelz + 1),
        f"{ng.shape} (要素 {grid.shape})",
    )

    edof = simp._build_edof(nelx, nely, nelz)
    # `_build_edof` の要素の並びは meshgrid(indexing="ij") の ravel = (i,j,k) のC順。
    ex, ey, ez = np.meshgrid(
        np.arange(nelx), np.arange(nely), np.arange(nelz), indexing="ij"
    )
    elements = np.stack([ex.ravel(), ey.ravel(), ez.ravel()], axis=1)
    worst = 0
    for l, (a, b, c) in enumerate(
        [(a, b, c) for a in (0, 1) for b in (0, 1) for c in (0, 1)]
    ):
        corners = elements + np.array([a, b, c])
        mine = ng.ids_of(corners)
        theirs = edof[:, 3 * l] // 3
        worst = max(worst, int(np.abs(mine - theirs).max()))
    check(
        "8隅の節点番号が simp の DOF 写像と一致",
        worst == 0,
        f"要素 {elements.shape[0]} 個 x 8隅で最大差 {worst}",
    )

    ids = ng.ids_of(np.array([[2, 1, 2], [0, 0, 0], [5, 4, 3]]))
    check(
        "通し番号 → 添字の往復が一致",
        bool((ng.indices_of_ids(ids) == np.array([[2, 1, 2], [0, 0, 0], [5, 4, 3]])).all()),
        f"ids={ids.tolist()}",
    )
    dofs = ng.dofs_of(np.array([7]), axes=(0, 2))
    check(
        "自由度番号が 3n+axis",
        bool((dofs == np.array([21, 23])).all()),
        f"node 7 axes(0,2) → {dofs.tolist()}",
    )

    # 節点(i,j,k) の座標は origin + spacing*(i,j,k)。**要素中心の +0.5 が付いていないこと。**
    pos = ng.positions_of(np.array([[0, 0, 0], [1, 2, 3]]))
    want = np.array([[-3.0, 1.5, 7.25], [-3.0 + 0.75, 1.5 + 1.5, 7.25 + 2.25]])
    check(
        "節点座標に半セルのずれが無い",
        float(np.abs(pos - want).max()) == 0.0,
        f"max|得-期待|={np.abs(pos - want).max():.3e}",
    )
    centre = grid.center_of(0, 0, 0)
    check(
        "要素中心は節点から半セルずれている(規約の取り違えを検出)",
        bool(np.allclose(centre - pos[0], 0.5 * grid.spacing)),
        f"中心-節点={np.round(centre - pos[0], 6).tolist()}",
    )

    # `void_mask` だけを渡したとき、**既定の片持ち梁の拘束が消えないこと**を
    # **`optimize` を実際に通して**見る。`Model` を直に組んで `fixed` の包含関係だけを
    # 見ていると、「optimize が拘束を置き換えてしまう」種類の回帰は素通りする
    # (`optimize` こそが置き換えていた場所なので、そこを通らない検査には意味が無い)。
    void = np.zeros((6, 4, 4), dtype=bool)
    void[:, 0, :] = True                      # y の1層を空にして浮いた節点を作る
    default_fixed = simp.Model(6, 4, 4).fixed
    opts = dict(max_iter=3, solver="direct", void_mask=void)
    detail = ""
    ok = False
    try:
        implicit = simp.optimize(6, 4, 4, **opts)
        explicit = simp.optimize(6, 4, 4, fixed_dofs=default_fixed, **opts)
        same = bool((implicit.rho == explicit.rho).all())
        void_empty = float(implicit.rho[void].max()) == 0.0
        finite = bool(np.isfinite(implicit.history[-1].compliance)) and (
            implicit.history[-1].compliance > 0
        )
        ok = same and void_empty and finite
        detail = (
            f"暗黙 vs 既定を明示した場合の rho がビット一致={same} / "
            f"空領域の最大密度={implicit.rho[void].max():.3e} / "
            f"C={implicit.history[-1].compliance:.6f}"
        )
    except ValueError as exc:
        # 既定の拘束が消えると拘束不足になり、残差の検算がここで落とす(それも不合格)。
        detail = f"optimize が例外で落ちた(拘束が消えている疑い): {exc!s:.120}"
    check(
        "void_mask だけを渡した optimize が、既定の拘束を明示した場合と厳密に一致する",
        ok,
        detail,
    )

    # 拘束不足の系を**黙って解かない**こと。1自由度だけ拘束すると K は特異になるが、
    # 「非空で範囲内」の検査は通ってしまう。実測ではCGが収束しないまま
    # max|u| = 1.2e16 の有限値が返り、最適化はその値を信じて進んだ末に
    # OC 更新の ZeroDivisionError で落ちていた(原因から遠い場所で落ちるのが最悪)。
    raised = ""
    try:
        simp.optimize(6, 4, 4, max_iter=3, fixed_dofs=np.array([0]))
    except ValueError as exc:
        raised = str(exc)
    check(
        "拘束不足(1自由度だけ拘束)を、拘束不足と名指しした例外で落とす",
        "K U = F が解けていない" in raised and "拘束不足" in raised,
        " ".join(raised.split())[:110] if raised else "例外が出なかった(黙って解いている)",
    )


# ---------------------------------------------------------------------------
# 1. 総荷重の保存
# ---------------------------------------------------------------------------
def check_load_conservation() -> None:
    print("[1] 総荷重の保存(解像度を2倍にしても指定値と一致)")
    worst = 0.0
    rows = []
    for n in (16, 32):
        model, table = _table(PLATE, n)
        top = _face_of(model.soup, "plane", normal=(0, 0, 1))
        side = _face_of(model.soup, "plane", normal=(1, 0, 0))
        for label, faces, force in (
            ("単一面・軸方向", [top], (0.0, 0.0, -1000.0)),
            ("単一面・斜め", [top], (12.5, -3.25, -7.75)),
            ("2面同時", [top, side], (0.0, -250.0, -1000.0)),
        ):
            f = table.load_vector(faces, force)
            got = np.array([f[0::3].sum(), f[1::3].sum(), f[2::3].sum()])
            want = np.asarray(force, dtype=float)
            rel = float(np.abs(got - want).max() / np.abs(want).max())
            worst = max(worst, rel)
            nodes = table.node_ids(faces).size
            rows.append((n, label, nodes, rel))
            # 荷重が入った節点が、その面の節点集合と厳密に一致すること(取りこぼし・はみ出しなし)
            touched = np.unique(np.flatnonzero(f != 0.0) // 3)
            same = np.array_equal(touched, table.node_ids(faces))
            if not same:
                check(f"n={n} {label}: 荷重の載った節点が面の節点集合と一致", False,
                      f"荷重={touched.size} 節点集合={table.node_ids(faces).size}")
    for n, label, nodes, rel in rows:
        check(
            f"n={n:3d} {label}(節点{nodes})の合計が指定値と一致",
            rel < 1e-12,
            f"相対誤差 {rel:.3e}",
        )
    RESULTS["load_conservation_worst_rel"] = worst

    # 解像度を変えても総和が変わらないこと自体を、同じ力で並べて確認する。
    totals = []
    for n in (16, 32):
        model, table = _table(PLATE, n)
        top = _face_of(model.soup, "plane", normal=(0, 0, 1))
        f = table.load_vector(top, (0.0, 0.0, -1000.0))
        totals.append(float(f[2::3].sum()))
    # **圧力の合力は「真の面積」から出ていること。** 担当面積は稜線で隣の面に取られたぶん
    # 数%小さい(実測 10.7%)。`load_vector` は指定した合力を担当面積の**比**で配るので
    # その差は正規化で消えるが、圧力は `p x 面積` が合力そのものなので**そのまま不足**する
    # (粗い格子ほど大きく、解像度を上げると黙って合力が増える)。
    pressure_rows = []
    for n in (16, 32):
        model, table = _table(PLATE, n)
        top = _face_of(model.soup, "plane", normal=(0, 0, 1))
        a = table[top]
        p_load = 0.25
        f = table.pressure_vector(top, p_load)
        got = np.array([f[0::3].sum(), f[1::3].sum(), f[2::3].sum()])
        want = -p_load * a.cad_area * (a.cad_normal / np.linalg.norm(a.cad_normal))
        rel = float(np.abs(got - want).max() / np.abs(want).max())
        # 担当面積で出していたら何%ずれるか(=修正前の値)。検査が効く幅を示す。
        gap = 1.0 - a.total_area / a.cad_area
        pressure_rows.append((n, rel, gap, a.cad_area, a.total_area))
    for n, rel, gap, cad, assigned in pressure_rows:
        check(
            f"n={n:3d} 圧力の合力が p x cad_area と一致(稜線で捨てた面積に依らない)",
            rel < 1e-12,
            f"相対誤差 {rel:.3e}(担当面積 {assigned:.3f} で出していたら {gap:.2%} 不足していた。"
            f"CAD面積 {cad:.3f})",
        )
    RESULTS["pressure_resultant"] = [
        {"n": n, "rel_err": rel, "assigned_area_shortfall": gap} for n, rel, gap, _c, _a in pressure_rows
    ]

    drift = abs(totals[1] - totals[0]) / abs(totals[0])
    check(
        "解像度16と32で総荷重が同じ(相対1e-12以内)",
        drift < 1e-12 and abs(totals[0] + 1000.0) / 1000.0 < 1e-12,
        f"{totals[0]:.15f} / {totals[1]:.15f} (相対差 {drift:.3e})",
    )


# ---------------------------------------------------------------------------
# 2. 解像度非依存(重心と担当面積が真値へ収束する)
# ---------------------------------------------------------------------------
def check_resolution_convergence() -> None:
    print("[2] 解像度非依存(重心・担当面積が真の面へ収束する)")
    res = bench.ANCHOR_CONVERGENCE["resolutions"]
    cases = (
        ("傾けた箱の平面", TILTED, _tilted_face, res),
        ("半円柱の曲面", HALF_CYLINDER, lambda s: _face_of(s, "cylinder_partial"), res),
    )
    for label, shape, pick, resolutions in cases:
        area_err = []
        centroid_err = []
        nodes = []
        for n in resolutions:
            model, table = _table(shape, n)
            a = table[pick(model.soup)]
            area_err.append(abs(a.total_area - a.cad_area) / a.cad_area)
            centroid_err.append(
                float(np.linalg.norm(a.centroid(table.node_grid) - a.cad_centroid))
                / max(model.grid.spacing, 1e-12)
            )
            nodes.append(a.node_count)
        RESULTS[f"convergence_{label}"] = {
            "resolutions": list(resolutions),
            "nodes": nodes,
            "area_rel_err": [round(v, 6) for v in area_err],
            "centroid_err_over_h": [round(v, 6) for v in centroid_err],
        }
        check(
            f"{label}: 担当面積の誤差が解像度4倍で半分以下",
            area_err[2] <= 0.5 * area_err[0],
            f"{area_err[0]:.5f} → {area_err[1]:.5f} → {area_err[2]:.5f} "
            f"(節点 {nodes[0]} → {nodes[1]} → {nodes[2]})",
        )
        check(
            f"{label}: 重心の誤差が解像度4倍で半分以下(絶対値。要素サイズは1/4になる)",
            centroid_err[2] * 0.25 <= 0.5 * centroid_err[0],
            "h単位で "
            f"{centroid_err[0]:.5f} → {centroid_err[1]:.5f} → {centroid_err[2]:.5f}",
        )


# ---------------------------------------------------------------------------
# 3. 面の取り違えが無い
# ---------------------------------------------------------------------------
def check_faces_do_not_mix() -> None:
    print("[3] 面の取り違えが無い(隣接2面の節点集合が排他・正しい側にある)")
    model, table = _table(PLATE, 32)
    h = model.grid.spacing
    top = _face_of(model.soup, "plane", normal=(0, 0, 1))     # z = +5
    side = _face_of(model.soup, "plane", normal=(1, 0, 0))    # x = +20
    a_top, a_side = table[top], table[side]

    shared = np.intersect1d(a_top.node_ids, a_side.node_ids)
    check("上面と側面の節点集合が排他", shared.size == 0, f"共有 {shared.size} 節点")

    p_top = table.node_grid.positions_of(a_top.nodes)
    p_side = table.node_grid.positions_of(a_side.nodes)
    check(
        "上面の節点がすべて z=+5 から殻の半径以内",
        float(np.abs(p_top[:, 2] - 5.0).max()) <= table.radius + 1e-9,
        f"最大 |z-5| = {np.abs(p_top[:, 2] - 5.0).max():.4f} (半径 {table.radius:.4f})",
    )
    check(
        "側面の節点がすべて x=+20 から殻の半径以内",
        float(np.abs(p_side[:, 0] - 20.0).max()) <= table.radius + 1e-9,
        f"最大 |x-20| = {np.abs(p_side[:, 0] - 20.0).max():.4f}",
    )
    c_top = a_top.centroid(table.node_grid)
    c_side = a_side.centroid(table.node_grid)
    check(
        "上面の重心が上面の側にある",
        abs(c_top[2] - 5.0) < 0.35 * h and abs(c_top[0]) < 0.35 * h,
        f"重心={np.round(c_top, 4).tolist()} (真値 [0,0,5], h={h:.4f})",
    )
    check(
        "側面の重心が側面の側にある",
        abs(c_side[0] - 20.0) < 0.35 * h and abs(c_side[2]) < 0.35 * h,
        f"重心={np.round(c_side, 4).tolist()} (真値 [20,0,0])",
    )

    # 全面について「自分の面から半径以内」を確認する(1面でも取り違えると落ちる)。
    worst = 0.0
    for f in table.face_indices():
        a = table[f]
        tris = model.soup.triangles_of_face(f)
        sub = vx.TriangleSoup(
            positions=model.soup.positions,
            triangles=model.soup.triangles[tris],
            face_index=model.soup.face_index[tris],
        )
        d, _ = vx.closest_triangle(sub, table.node_grid.positions_of(a.nodes))
        worst = max(worst, float(d.max()))
    check(
        "どの節点も自分の面の三角形から殻の半径以内",
        worst <= table.radius + 1e-9,
        f"最大距離 {worst:.6f} / 半径 {table.radius:.6f}",
    )
    RESULTS["max_node_to_own_face"] = worst


# ---------------------------------------------------------------------------
# 4. 法線が合っている
# ---------------------------------------------------------------------------
def check_normals() -> None:
    print("[4] 法線が合っている")
    model, table = _table(TILTED, 32)
    f = _tilted_face(model.soup)
    a = table[f]
    unit = a.mean_normal() / np.linalg.norm(a.mean_normal())
    cad = a.cad_normal / np.linalg.norm(a.cad_normal)
    dot = float(np.dot(unit, cad))
    check(
        "傾けた平面: 節点法線の面積重み平均が CAD 法線と一致",
        dot > 1.0 - 1e-9,
        f"dot={dot:.12f} 法線={np.round(unit, 6).tolist()}",
    )
    check(
        "傾けた平面: 平均法線の長さが 1(節点ごとの法線が揃っている)",
        abs(float(np.linalg.norm(a.mean_normal())) - 1.0) < 1e-9,
        f"|平均|={np.linalg.norm(a.mean_normal()):.12f}",
    )

    # 節点の**位置**だけから面の向きを出す(法線を三角形から引き継いだのではない証拠)。
    pts = table.node_grid.positions_of(a.nodes)
    centred = pts - pts.mean(axis=0)
    _, _, vh = np.linalg.svd(centred, full_matrices=False)
    pca = vh[2]
    pca_dot = abs(float(np.dot(pca, cad)))
    check(
        "傾けた平面: 節点位置の主成分分析から出した面の向きが CAD 法線と一致",
        pca_dot > 0.999,
        f"|dot|={pca_dot:.8f}",
    )

    model_c, table_c = _table(HALF_CYLINDER, 32)
    fc = _face_of(model_c.soup, "cylinder_partial")
    ac = table_c[fc]
    tris = model_c.soup.triangles_of_face(fc)
    p, q, r = model_c.soup.triangle_vertices()
    cross = np.cross(q[tris] - p[tris], r[tris] - p[tris])
    tri_mean = cross.sum(axis=0) / (2.0 * (0.5 * np.linalg.norm(cross, axis=1)).sum())
    got = ac.mean_normal()
    dot_c = float(
        np.dot(got / np.linalg.norm(got), tri_mean / np.linalg.norm(tri_mean))
    )
    check(
        "曲面: 節点法線の面積重み平均の向きが、面の三角形の面積重み平均と一致",
        dot_c > 1.0 - 1e-6,
        f"dot={dot_c:.9f} 節点={np.round(got, 5).tolist()} 三角形={np.round(tri_mean, 5).tolist()}",
    )
    # 半円柱の外向き法線の面積重み平均は解析的に 2/pi(軸に垂直な向き)。
    analytic = 2.0 / np.pi
    check(
        "曲面: 平均法線の大きさが解析値 2/pi の 2% 以内",
        abs(float(np.linalg.norm(got)) - analytic) / analytic < 0.02,
        f"|平均|={np.linalg.norm(got):.6f} 解析値={analytic:.6f}",
    )
    RESULTS["normals"] = {
        "tilted_dot": dot,
        "tilted_pca_dot": pca_dot,
        "curved_dot": dot_c,
        "curved_magnitude": float(np.linalg.norm(got)),
    }


# ---------------------------------------------------------------------------
# 5. 偽の集中が出ていない
# ---------------------------------------------------------------------------
def _smoothed_uniformity(
    model: vx.VoxelModel,
    table: an.AnchorTable,
    face: int,
    loads: np.ndarray | None = None,
    cache: dict | None = None,
) -> tuple[np.ndarray, int]:
    """節点荷重をガウス平滑化し、面の内側での荷重密度のばらつきを返す。

    比べる相手は**同じ核で平滑化した真の面積分布**(三角形を細かく標本化したもの)なので、
    面の縁や曲率の効果は分母と分子で相殺する。残るのは「節点への配り方の偏り」だけ。
    """
    a = table[face]
    h = model.grid.spacing
    sigma = 2.0 * h
    inv = 1.0 / (2.0 * sigma * sigma)
    X = table.node_grid.positions_of(a.nodes)
    L = a.areas if loads is None else loads

    tris = model.soup.triangles_of_face(face)
    p, q, r = model.soup.triangle_vertices()
    pts_list, area_list = [], []
    for pts, areas, _parents in an._iter_samples(
        p[tris], q[tris], r[tris], target=0.9 * h
    ):
        pts_list.append(pts)
        area_list.append(areas)
    P = np.concatenate(pts_list)
    A = np.concatenate(area_list)

    # `smooth_true` は `loads` に依存しない(節点位置と真の面積分布だけで決まる)。
    # **同じ面を本番と対照で2回通すので、ここを共有しないと支配項を2度払う**
    # (実測: 円筒の面で 1回 25秒。節点3,708 x 標本178,848 の exp が費用の全部)。
    smooth_load = np.empty(X.shape[0])
    smooth_true = cache.get("smooth_true") if cache is not None else None
    want_true = smooth_true is None
    if want_true:
        smooth_true = np.empty(X.shape[0])
    for s in range(0, X.shape[0], 128):
        x = X[s : s + 128]
        d2 = ((x[:, None, :] - X[None, :, :]) ** 2).sum(axis=2)
        smooth_load[s : s + 128] = (np.exp(-d2 * inv) * L).sum(axis=1)
        if want_true:
            d2 = ((x[:, None, :] - P[None, :, :]) ** 2).sum(axis=2)
            smooth_true[s : s + 128] = (np.exp(-d2 * inv) * A).sum(axis=1)
    if want_true and cache is not None:
        cache["smooth_true"] = smooth_true
    # 縁の効果が入らない節点だけを見る(核が面からはみ出していない = 真の平滑値が最大付近)。
    interior = smooth_true >= 0.985 * smooth_true.max()
    ratio = smooth_load[interior] / smooth_true[interior]
    return ratio / np.median(ratio), int(interior.sum())


def check_no_false_concentration() -> None:
    print("[5] 偽の集中が出ていない(節点あたりの担当が偏っていない)")
    peak = {}
    for label, shape, pick, n in (
        ("軸に整列した平面", PLATE, lambda s: _face_of(s, "plane", normal=(0, 0, 1)), 32),
        ("傾けた平面", TILTED, _tilted_face, 32),
        ("曲面(円筒)", FULL_CYLINDER, lambda s: _face_of(s, "cylinder_full"), 24),
    ):
        model, table = _table(shape, n)
        a = table[pick(model.soup)]
        cell = model.grid.spacing ** 2
        ratio = float(a.areas.max() / cell)
        peak[label] = ratio
        check(
            f"{label}: 1節点の担当が要素1個ぶんの1.5倍以下",
            ratio <= 1.5,
            f"最大 {ratio:.4f} x h^2 (中央値 {np.median(a.areas) / cell:.4f}、節点 {a.node_count})",
        )
    RESULTS["peak_node_area_over_cell"] = peak

    spread = {}
    for label, shape, pick, n in (
        ("傾けた平面", TILTED, _tilted_face, 32),
        ("曲面(円筒)", FULL_CYLINDER, lambda s: _face_of(s, "cylinder_full"), 32),
    ):
        model, table = _table(shape, n)
        face = pick(model.soup)
        shared: dict = {}
        ratio, count = _smoothed_uniformity(model, table, face, cache=shared)
        dev = float(np.abs(ratio - 1.0).max())
        # 対照: 面積重みを外して節点で等分する(これが落ちることで検査が効いていると分かる)。
        a = table[face]
        equal = np.full(a.areas.shape, a.areas.sum() / a.areas.size)
        ratio_eq, _ = _smoothed_uniformity(model, table, face, loads=equal, cache=shared)
        dev_eq = float(np.abs(ratio_eq - 1.0).max())
        spread[label] = {"anchor": dev, "equal_split_control": dev_eq, "nodes": count}
        check(
            f"{label}: 平滑化した荷重密度が面の内側で一様(ずれ1.5%以下)",
            dev <= 0.015,
            f"最大ずれ {dev:.4%} (内側の節点 {count})",
        )
        check(
            f"{label}: 面積重みを外した対照が実際に落ちる(検査が効いている証拠)",
            dev_eq > 0.015,
            f"等分にすると最大ずれ {dev_eq:.4%}",
        )
    RESULTS["smoothed_uniformity"] = spread


# ---------------------------------------------------------------------------
# 6. 稜線の扱いが宣言どおり
# ---------------------------------------------------------------------------
def check_edge_rule() -> None:
    print("[6] 稜線の扱いが宣言どおり(最も多く配った面が独占。同点は面添字の小さい面)")
    model, table = _table(PLATE, 24)
    faces = table.face_indices()

    all_ids = np.concatenate([table[f].node_ids for f in faces])
    check(
        "全面の節点集合が互いに排他(重複ゼロ)",
        np.unique(all_ids).size == all_ids.size,
        f"節点 {all_ids.size} / 相異なり {np.unique(all_ids).size}",
    )

    # 生の配り先から規則を再現し、実際の持ち主と突き合わせる。
    best_area: dict[int, float] = {}
    best_face: dict[int, int] = {}
    for f in faces:  # 昇順。同点なら先に入った小さい面が残る
        a = table[f]
        for node, area in zip(a.raw_node_ids.tolist(), a.raw_areas.tolist(), strict=True):
            if area > best_area.get(node, 0.0):
                best_area[node] = area
                best_face[node] = f
    mismatch = 0
    owned = 0
    for f in faces:
        for node in table[f].node_ids.tolist():
            owned += 1
            if best_face.get(node) != f:
                mismatch += 1
    check(
        "持ち主が「最も多く面積を配った面」と一致",
        mismatch == 0 and owned == len(best_face),
        f"不一致 {mismatch} / 持ち主のいる節点 {owned} (規則から出した数 {len(best_face)})",
    )

    touched = np.concatenate([table[f].raw_node_ids for f in faces])
    _, counts = np.unique(touched, return_counts=True)
    contested = np.flatnonzero(counts > 1)
    check(
        "複数の面が取り合った節点が実際に存在する(検査が空振りしていない)",
        len(contested) > 0,
        f"取り合った節点 {len(contested)} / 全 {len(best_face)}",
    )

    # 面積の湧き出しが無い: どの面も自分の三角形の総面積を超えて担当しない。
    over = [
        (f, table[f].total_area, table[f].triangle_area)
        for f in faces
        if table[f].total_area > table[f].triangle_area * (1 + 1e-9)
    ]
    check(
        "どの面も自分の三角形の総面積を超えて担当しない",
        not over,
        f"超過 {len(over)} 面" if over else f"全 {len(faces)} 面で担当 <= 三角形の面積",
    )

    total_assigned = sum(table[f].total_area for f in faces)
    total_tri = sum(table[f].triangle_area for f in faces)
    leak = 1.0 - total_assigned / total_tri
    check(
        "全体で失われた面積は稜線の帯ぶんだけ(要素サイズで説明がつく大きさ)",
        0.0 < leak < 0.25,
        f"こぼれ {leak:.4%} (担当 {total_assigned:.3f} / 三角形 {total_tri:.3f})",
    )
    RESULTS["edge_rule"] = {
        "contested_nodes": len(contested),
        "total_leak": leak,
        "contested_assignments": table.stats["contested_assignments"],
    }

    # **稜線の規則の帰結**: 要素サイズより小さい面は隣の面に節点を全部取られて空になる。
    # そのとき黙って「荷重0」「拘束なし」を返さず落ちること(そうしないと、拘束の無い系や
    # 荷重の無い系がそのまま最適化に入る)。実測: 231面のSTEPを h=1.54mm で割ると22面が空。
    tiny = Box(40, 24, 10) + Pos(0, 0, 5.25) * Box(0.5, 0.5, 0.5)
    model_t, table_t = _table(tiny, 8)   # h=5.0mm ≫ 0.5mm の突起
    empty = table_t.empty_faces()
    check(
        "要素サイズより小さい面は節点が付かず、それが `empty_faces()` に出る",
        len(empty) == 5,
        f"空の面 {empty} (h={model_t.grid.spacing:.4g} mm, 突起の面は 0.5x0.5mm)",
    )
    raised = ""
    try:
        table_t.load_vector(empty[0], (0.0, 0.0, -1.0))
    except ValueError as exc:
        raised = str(exc)
    check(
        "空の面を名指しして荷重を掛けると、黙って0にせず落ちる",
        "節点が1つも付いていない" in raised,
        raised[:90] if raised else "例外が出なかった(黙って0荷重になっている)",
    )
    raised = ""
    try:
        table_t.fixed_dofs(empty[0])
    except ValueError as exc:
        raised = str(exc)
    check(
        "空の面を名指しして拘束すると、黙って拘束なしにせず落ちる",
        "節点が1つも付いていない" in raised,
        raised[:90] if raised else "例外が出なかった(黙って拘束なしになっている)",
    )

    # **混ぜたときが本番**。合計で
    # 見ていると、大きい面と一緒に指した空の面だけが黙って落ちる —— 2面を固定したつもりで
    # 1面しか効いていない系が、例外も通知も無いまま最適化へ入る。
    big = next(f for f in table_t.face_indices() if f not in empty)
    raised = ""
    try:
        table_t.fixed_dofs([big, empty[0]])
    except ValueError as exc:
        raised = str(exc)
    check(
        "大きい面と空の面を一緒に指しても落ちる(空の面だけが黙って無視されない)",
        "節点が1つも付いていない" in raised and f"面{empty[0]}" in raised,
        raised[:90] if raised else "例外が出なかった(空の面が黙って無視されている)",
    )

    # 稜線を共有する2面の荷重が二重に入っていないこと(節点が排他なので合計は素直に足せる)。
    top = _face_of(model.soup, "plane", normal=(0, 0, 1))
    side = _face_of(model.soup, "plane", normal=(1, 0, 0))
    f_both = table.load_vector([top, side], (0.0, 0.0, -100.0))
    check(
        "稜線を共有する2面へ同時に荷重を掛けても合計が指定値のまま",
        abs(float(f_both[2::3].sum()) + 100.0) < 1e-12,
        f"合計 {f_both[2::3].sum():.15f}",
    )


# ---------------------------------------------------------------------------
# 7. 端から端まで
# ---------------------------------------------------------------------------
def check_end_to_end() -> None:
    print("[7] 端から端まで(ソリッド → 格子 → 面アンカー → SIMP → marching cubes → STL)")
    out_path = OUTPUT_DIR / "bracket.stl"
    t0 = time.perf_counter()
    out = an.run_pipeline(
        elements_long_axis=bench.ANCHOR_END_TO_END["elements_long_axis"],
        max_iter=bench.ANCHOR_END_TO_END["max_iter"],
        volfrac=0.3,
        stl_path=out_path,
    )
    seconds = time.perf_counter() - t0
    model = out["model"]
    result = out["result"]
    history = [it.compliance for it in result.history]

    check(
        "面アンカーで固定面と荷重面を名指しできた",
        set(out["fixed_faces"]).isdisjoint(out["load_faces"]),
        f"固定=面{out['fixed_faces']} 荷重=面{out['load_faces']} "
        f"(荷重面の節点 {out['table'][out['load_faces'][0]].node_count})",
    )
    vol_rel = abs(out["achieved_volume"] - out["target_volume"]) / out["target_volume"]
    check(
        "目標体積の±1%以内(check_simp の完了条件1-(a)と同じ基準)",
        vol_rel < 0.01,
        f"{out['achieved_volume']:.3f} 要素 / 目標 {out['target_volume']:.3f} (相対 {vol_rel:.3e})",
    )
    check(
        "コンプライアンスが減少した",
        history[-1] < history[0],
        f"C {history[0]:.4f} → {history[-1]:.4f} ({len(history)}反復)",
    )
    tail = np.array(history[-bench.TAIL_WINDOW :])
    swing = float((tail.max() - tail.min()) / tail.mean())
    check(
        "終盤10反復の変動が1%未満(check_simp の完了条件1-(b)と同じ基準)",
        swing < 0.01,
        f"(max-min)/mean = {swing:.4%}",
    )
    check(
        "コンプライアンスが有限で正",
        bool(np.isfinite(history).all() and (np.array(history) > 0).all()),
        f"min={min(history):.4f}",
    )
    check(
        "設計空間の外に材料が残っていない",
        float(result.rho[~model.design].max()) == 0.0,
        f"外の最大密度 {result.rho[~model.design].max():.3e}",
    )
    keep_near = out["table"].element_mask(
        list(out["fixed_faces"]) + list(out["load_faces"]), layers=1
    ) & model.design
    check(
        "荷重・拘束の近傍が非設計領域(ρ=1)に固定されている",
        float(result.rho[keep_near].min()) == 1.0,
        f"近傍 {int(keep_near.sum())} 要素の最小密度 {result.rho[keep_near].min():.6f}",
    )
    surface = out["surface"]
    check(
        "marching cubes が空でない面を返した",
        surface.verts_smooth.shape[0] > 0 and surface.faces.shape[0] > 0,
        f"頂点 {surface.verts_smooth.shape[0]} / 三角形 {surface.faces.shape[0]}",
    )
    # バイナリSTL: 80バイトヘッダ + uint32三角形数 + 1三角形50バイト(`mesh.write_stl`)。
    size = out_path.stat().st_size if out_path.exists() else 0
    declared = -1
    if size >= 84:
        with out_path.open("rb") as fh:
            fh.seek(80)
            declared = int(np.frombuffer(fh.read(4), dtype="<u4")[0])
    check(
        "STL が書けていて、三角形数もサイズも面と一致する",
        declared == surface.faces.shape[0] and size == 84 + 50 * declared,
        f"{out_path.name} {size} バイト / 宣言 {declared} 三角形 / 面 {surface.faces.shape[0]}",
    )

    # 荷重が実際に系へ入っていること(拘束DOFへ全部落ちていたら simp が例外にするが、
    # ここでも合計で確かめる)。
    load = out["table"].load_vector(out["load_faces"], (0.0, 0.0, -1.0))
    check(
        "荷重ベクトルの合計が指定値(パイプライン内でも保存されている)",
        abs(float(load[2::3].sum()) + 1.0) < 1e-12,
        f"合計 {load[2::3].sum():.15f}",
    )

    RESULTS["end_to_end"] = {
        "grid": list(model.grid.shape),
        "spacing": round(model.grid.spacing, 6),
        "design_elements": out["design_elements"],
        "movable_elements": out["free_elements"],
        "solid_elements": out["solid_elements"],
        "void_elements": out["void_elements"],
        "iterations": len(history),
        "compliance": [round(history[0], 4), round(history[-1], 4)],
        "tail_swing": round(swing, 6),
        "volume_rel_err": vol_rel,
        "timings": {k: round(v, 3) for k, v in out["timings"].items()},
        "seconds": round(seconds, 1),
        "stl_bytes": size,
    }


def main() -> int:
    print("=" * 72)
    print("check_anchor.py : CAD の面 → 格子の節点の対応表(anchor.py)の受入検査")
    print("=" * 72)
    started = time.perf_counter()
    check_node_numbering()
    check_load_conservation()
    check_resolution_convergence()
    check_faces_do_not_mix()
    check_normals()
    check_no_false_concentration()
    check_edge_rule()
    check_end_to_end()
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

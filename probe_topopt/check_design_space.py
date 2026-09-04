"""塊(設計空間)の受入検査。

    ..\\.venv\\Scripts\\python.exe check_design_space.py

**このファイルが合否そのものである。** どれか1つでも落ちればプロセスは非ゼロで終わる。

- **[1]〜[4]**: タスク `20260903-2216-topopt-split-design-space-from-anchor-soup`
  (塊と面アンカーの対象ソリッドの分離)の完了条件1〜3。
- **[5]〜[9]**: タスク `20260903-2234-topopt-design-space-from-bbox`
  (案1: 4条件から外接直方体+余白で塊を導出する)の受入検査。**[5]〜[9]は変異検査を通してある**
  —— 実装を壊すと実際に落ちることを1件ずつ確認した(対応表は README「変異検査」)。

[1]〜[4] の構成:

1. **塊がソリッドから来ない** — 渡した bool マスクが要素単位でそのまま `VoxelModel.design`
   になる(かつ呼び出し側の配列と共有していない)。
2. **面アンカーが塊に依存しない** — 格子と soup を固定したまま塊だけを差し替えても、
   `anchors_for` が返す面添字の集合・節点の通し番号・担当面積が完全に一致する。
3. **はみ出しが例外になる** — 保持ソリッドが塊から出る配置で `OutsideDesignSpaceError` が
   上がり、メッセージにはみ出し要素数が入る。**同じ配置を旧経路(`voxelize()`)に通すと
   例外が出ずに黙って切り詰められることを先に確認する**(この検査の検出力の確認)。
4. **塊が入力ソリッドに縛られていない(本丸)** — 塊をブラケットより大きい箱にし、面は
   ブラケットのものを使って SIMP を回すと、**元のブラケットの外に ρ>0.5 の要素ができる**。
   同じ格子・同じ面で旧経路(塊=ブラケット)を回すと、`void = ~design` により構造的に0個。
   収束を見る検査ではないので格子は粗く、反復も少なくてよい。

[5]〜[9] の構成(案1):

5. **4条件だけで塊が出る** — 設計空間ソリッドを渡す口が型に無いことを見たうえで、
   4条件 → 塊 → 面アンカー → SIMP → marching cubes → STL を通し、**条件のどのソリッドにも
   入っていない空間に材料ができる**ことを見る。
6. **保持ソリッドが塊に入っている** — はみ出し0要素。**「はみ出し0」だけでは足りない**ので、
   保持が塊の中で1要素以上に表現されていること・格子そのものの内側にあることも見る。さらに
   除外が保持を食う配置を作り、**黙って切り詰めず例外になる**ことを見る。
7. **拘束面・荷重面のアンカーが空にならない** — `empty_faces()` に条件面が入らない。
   保持ソリッドから離れた面を条件にした配置も1つ通す(点群が保持の頂点だけだと落ちる)。
8. **除外ソリッドが引かれている** — 除外の領域に塊の要素が1つも無いことを、除外の占有を
   独立に計算し直して見る。**除外は点群に入らない**(遠くに足しても外接直方体が動かない)。
9. **余白を外すと壊れる** — 比率を下げていくと必ず壊れる比率がある。既定値はそこから離れている。

[10] の構成(連結性ガード。タスク `20260903-2235`):

10. **拘束と荷重が塊の中でひと続きか** — 正常系が通る / 拘束と荷重の間を除外ソリッドで
    断ち切ると `DesignSpaceDisconnectedError`(文面に成分数と両者の成分が出る。
    `DesignSpaceTooSmallError` ではない)/ 塊を直接2つの島にすると落ちる /
    **角1点だけで接している2島は落ちる**(6近傍の定義が実装と一致していることの検査。
    26近傍なら1成分になることを同じコードで見せる)/ 片側のアンカー要素が塊に無い場合も落ちる。
    **変異検査は2つ**: `derive` の呼び出しを外す(10-2 が素通り)/ `assert_load_path` の
    判定を外す(10-2・10-3・10-4 が素通り)。README「変異検査」に結果がある。
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from pathlib import Path

import numpy as np

# **Windowsの既定コンソール(cp932)で落ちないようにする。** 記号が1つ混ざるだけで
# `UnicodeEncodeError` になり、検査が1件も走らないまま異常終了する。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import anchor as an
import bench
import design_space as ds
import voxelize as vx

FAILURES: list[str] = []
RESULTS: dict[str, object] = {}

# 検査4の格子。60mm のブラケットを一回り大きい箱で包む。**収束ではなく分離を見るので粗い。**
LONG_AXIS = bench.DESIGN_SPACE_BOX["elements_long_axis"]
MAX_ITER = bench.DESIGN_SPACE_BOX["max_iter"]
VOLFRAC = 0.3
# 塊にする箱(ワールド座標)。ブラケットの bbox は x -30..30 / y -12..12 / z -12..18。
DESIGN_BOX_LO = (-31.0, -13.0, -13.0)
DESIGN_BOX_HI = (31.0, 13.0, 25.0)


def check(name: str, ok: bool, detail: str) -> None:
    """1件の検査結果を出力する。**落ちても即死せず全件を出してから終わる。**"""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def box_mask(grid: vx.Grid, lo, hi) -> np.ndarray:
    """ワールド座標の軸整列箱を、要素中心が入るかどうかで塊のマスクにする。

    **OCCT を通さない。** 塊がソリッド由来でないことがこの検査の要点なので、ここで
    `occupancy()` を使うと確かめたいことが確かめられなくなる。
    """
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    axes = [grid.centers_1d(a) for a in range(3)]
    inside = [(axes[a] >= lo[a]) & (axes[a] <= hi[a]) for a in range(3)]
    return inside[0][:, None, None] & inside[1][None, :, None] & inside[2][None, None, :]


def bracket_grid(margin: float, elements_long_axis: int) -> tuple[vx.Grid, vx.TriangleSoup]:
    """ブラケットの bbox を `margin` mm 広げた格子と、そのブラケットの三角形群。"""
    bracket = an.demo_bracket()
    soup = vx.triangles_from_shape(bracket.wrapped, deviation=0.2)
    lo, hi = soup.bbox
    grid = vx.grid_for_bbox(lo - margin, hi + margin, elements_long_axis)
    return grid, soup


# ---------------------------------------------------------------------------
# 1. 塊がソリッドから来ない
# ---------------------------------------------------------------------------
def check_mask_is_the_design_space() -> None:
    print("\n[1] 塊を bool マスクで与えられる(ソリッド由来でない)")
    grid, soup = bracket_grid(6.0, 20)
    # ソリッドの占有とは似ても似つかないマスク(市松に間引いた箱)にして、
    # 「どこかで占有計算に差し替えられていたら必ず落ちる」ようにする。
    mask = box_mask(grid, DESIGN_BOX_LO, DESIGN_BOX_HI)
    mask[::2, ::2, ::2] = False
    model = vx.model_from_design_mask(grid, mask, anchor_shape=soup)

    check(
        "design が渡したマスクと要素単位で一致",
        bool(np.array_equal(model.design, mask)),
        f"一致しない要素 {int(np.count_nonzero(model.design != mask))} / {mask.size}",
    )
    check(
        "design_elements が実数と一致",
        model.stats["design_elements"] == int(np.count_nonzero(mask)),
        f"stats={model.stats['design_elements']} 実数={int(np.count_nonzero(mask))}",
    )
    check(
        "格子は渡したものがそのまま使われる",
        model.grid == grid,
        f"{model.grid.shape} spacing={model.grid.spacing:.6f}",
    )
    check(
        "面アンカーの対象は渡した soup そのもの",
        model.soup is soup,
        f"三角形 {soup.triangle_count} 枚",
    )
    # 呼び出し側の配列と共有していたら、あとから書き換えた瞬間に model の中身が黙って変わる。
    before = int(np.count_nonzero(model.design))
    mask[...] = False
    check(
        "呼び出し側のマスクを書き換えても model.design が変わらない",
        int(np.count_nonzero(model.design)) == before,
        f"{before} → {int(np.count_nonzero(model.design))} 要素",
    )
    RESULTS["check1_design_elements"] = before

    # かたち違い・空のマスクは受け取らない(黙って全体を塊にしない)。
    bad_shape = np.ones((grid.shape[0] + 1,) + tuple(grid.shape[1:]), dtype=bool)
    check(
        "格子とかたちの違うマスクを拒否",
        _raises(vx.model_from_design_mask, ValueError, grid, bad_shape, anchor_shape=soup),
        "ValueError",
    )
    check(
        "空のマスクを拒否",
        _raises(
            vx.model_from_design_mask,
            ValueError,
            grid,
            np.zeros(grid.shape, dtype=bool),
            anchor_shape=soup,
        ),
        "ValueError",
    )


def _raises(fn, exc, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception as e:  # noqa: BLE001 — 別の例外なら不合格として扱う
        print(f"      (想定外の例外 {type(e).__name__}: {e})")
        return False
    return False


# ---------------------------------------------------------------------------
# 2. 面アンカーが塊に依存しない
# ---------------------------------------------------------------------------
def check_anchors_independent_of_design() -> None:
    print("\n[2] 塊を差し替えても面アンカーが変わらない")
    grid, soup = bracket_grid(6.0, 20)
    occupied, _ = vx.occupancy(soup, grid)
    bigger = box_mask(grid, DESIGN_BOX_LO, DESIGN_BOX_HI)
    check(
        "2つの塊が本当に別物",
        not np.array_equal(occupied, bigger),
        f"ブラケットの占有 {int(occupied.sum())} 要素 / 一回り大きい箱 {int(bigger.sum())} 要素",
    )

    t0 = time.perf_counter()
    table_a = an.anchors_for(vx.model_from_design_mask(grid, occupied, anchor_shape=soup))
    table_b = an.anchors_for(vx.model_from_design_mask(grid, bigger, anchor_shape=soup))
    seconds = time.perf_counter() - t0

    check(
        "面添字の集合が一致",
        table_a.face_indices() == table_b.face_indices(),
        f"{len(table_a.face_indices())}面 / {len(table_b.face_indices())}面",
    )
    worst_area = 0.0
    node_diff = 0
    for f in table_a.face_indices():
        a, b = table_a[f], table_b[f]
        if not np.array_equal(a.node_ids, b.node_ids):
            node_diff += 1
            continue
        if a.areas.size:
            worst_area = max(worst_area, float(np.abs(a.areas - b.areas).max()))
    check(
        "面ごとの節点(通し番号)が全面で一致",
        node_diff == 0,
        f"食い違った面 {node_diff} / {len(table_a.face_indices())}",
    )
    check(
        "面ごとの節点の担当面積が全面で一致(厳密)",
        worst_area == 0.0,
        f"最大差 {worst_area:.3e} mm^2 ({seconds:.1f}秒)",
    )
    RESULTS["check2_faces"] = len(table_a.face_indices())
    RESULTS["check2_area_max_diff"] = worst_area


# ---------------------------------------------------------------------------
# 3. はみ出しが例外になる(旧経路が素通りすることを先に見る)
# ---------------------------------------------------------------------------
def check_outside_raises() -> None:
    print("\n[3] 保持ソリッドが塊からはみ出したら例外")
    from build123d import Box, Pos

    grid, _soup = bracket_grid(6.0, 24)
    bracket = an.demo_bracket()
    # 箱の上面(z=12)をまたぐ保持ソリッド。下半分は塊の中、上半分は外。
    keep = Pos(0, 0, 14) * Box(10, 10, 8)

    # (a) 旧経路 —— 例外は出ず、件数だけが stats に残る。**この検査の検出力の確認。**
    old = vx.voxelize(bracket, 24, keep=keep, grid=grid)
    dropped_old = int(old.stats["keep_dropped_outside"])
    # 新経路には**旧経路が作ったのと同じ塊・同じ soup**を渡す。片方だけ三角形分割の細かさが
    # 違うと、比べているのが「はみ出しの扱い」ではなく「分割の差」になる。
    occupied, soup = old.design, old.soup
    check(
        "旧経路(voxelize)は同じ配置を例外にせず黙って切り詰める(検出力の確認)",
        dropped_old > 0 and int(old.stats["keep_elements"]) > 0,
        f"はみ出し {dropped_old} 要素を捨て、残り {int(old.stats['keep_elements'])} 要素で正常終了",
    )

    # (b) 新経路 —— 既定で例外。メッセージにはみ出し要素数が入る。
    raised = None
    try:
        vx.model_from_design_mask(grid, occupied, anchor_shape=soup, keep=keep)
    except vx.OutsideDesignSpaceError as e:
        raised = e
    check(
        "新経路は既定で OutsideDesignSpaceError",
        raised is not None,
        "上がった" if raised is not None else "例外が上がらなかった",
    )
    if raised is not None:
        check(
            "OutsideDesignSpaceError は ValueError の派生(既存の捕まえ方が効く)",
            isinstance(raised, ValueError),
            type(raised).__mro__[1].__name__,
        )
        check(
            "メッセージにはみ出し要素数が入っている",
            str(dropped_old) in str(raised),
            f"はみ出し {dropped_old} 要素 / 文面: {raised}",
        )
        check(
            "outside から要素数を機械で引ける",
            raised.outside.get("keep") == dropped_old,
            f"outside={raised.outside}",
        )
        RESULTS["check3_outside_elements"] = dropped_old

    # (c) 明示的に切り詰めを頼めば、旧経路と同じ結果になる。
    clipped = vx.model_from_design_mask(
        grid, occupied, anchor_shape=soup, keep=keep, on_outside="clip"
    )
    check(
        "on_outside='clip' なら旧経路と同じ切り詰め結果",
        bool(np.array_equal(clipped.keep, old.keep))
        and int(clipped.stats["keep_dropped_outside"]) == dropped_old,
        f"keep {int(clipped.keep.sum())} 要素 / はみ出し {int(clipped.stats['keep_dropped_outside'])}",
    )
    check(
        "clip でも保持は塊の内側に収まる",
        not bool((clipped.keep & ~clipped.design).any()),
        f"外に出た保持要素 {int((clipped.keep & ~clipped.design).sum())}",
    )

    # (d) 除外ソリッドも同じ扱い。**両方はみ出したら両方を文面に出す**(片方だけ直して
    # もう一度落ちるより、最初に全部見えたほうが直す順を決められる)。
    obstacle = Pos(0, 0, -14) * Box(10, 10, 8)
    both = None
    try:
        vx.model_from_design_mask(
            grid, occupied, anchor_shape=soup, keep=keep, obstacle=obstacle
        )
    except vx.OutsideDesignSpaceError as e:
        both = e
    check(
        "除外ソリッドのはみ出しも例外になり、保持と一緒に報告される",
        both is not None
        and set(both.outside) == {"keep", "obstacle"}
        and both.outside["obstacle"] > 0
        and "障害物" in str(both)
        and "保持" in str(both),
        f"outside={getattr(both, 'outside', None)}",
    )


# ---------------------------------------------------------------------------
# 4. 塊が入力ソリッドに縛られていない(本丸)
# ---------------------------------------------------------------------------
def check_material_leaves_the_input_solid() -> None:
    print("\n[4] 塊を大きい箱にすると、材料が元のソリッドの外へ出る")
    grid, soup = bracket_grid(6.0, LONG_AXIS)
    occupied, _ = vx.occupancy(soup, grid)
    design = box_mask(grid, DESIGN_BOX_LO, DESIGN_BOX_HI)
    fixed_face, load_face = an.demo_faces(soup)
    print(
        f"  格子 {grid.shape} spacing={grid.spacing:.3f}mm / "
        f"塊 {int(design.sum())} 要素(ブラケットの占有は {int(occupied.sum())} 要素) / "
        f"面 固定={fixed_face} 荷重={load_face} / {MAX_ITER}反復",
        flush=True,
    )
    check(
        "塊はブラケットの占有を真に含む(足場の前提)",
        bool((occupied & ~design).sum() == 0 and (design & ~occupied).sum() > 0),
        f"外に出た占有 {int((occupied & ~design).sum())} / 塊だけの要素 {int((design & ~occupied).sum())}",
    )

    t0 = time.perf_counter()
    new = an.run_pipeline(
        shape=soup,
        design_mask=design,
        design_grid=grid,
        max_iter=MAX_ITER,
        volfrac=VOLFRAC,
        fixed_face=fixed_face,
        load_face=load_face,
        stl_path=Path(__file__).resolve().parent / "output" / "design_space_box.stl",
    )
    seconds_new = time.perf_counter() - t0

    rho = new["result"].rho
    # 荷重・拘束の近傍は run_pipeline が ρ=1 に固定する。**その分を数えると検査が自明に
    # 通ってしまう**ので、最適化が自分で置いた材料だけを見る。
    grown = (rho > 0.5) & ~occupied & ~new["solid"]
    check(
        "元のブラケットの外に ρ>0.5 の要素ができた(固定された近傍を除く)",
        int(grown.sum()) >= 1,
        f"{int(grown.sum())} 要素 (全体の ρ>0.5 は {int((rho > 0.5).sum())} 要素、{seconds_new:.1f}秒)",
    )
    if int(grown.sum()):
        idx = np.argwhere(grown)
        pts = grid.centers_of(idx)
        print(
            f"      外に出た材料の範囲: {np.round(pts.min(axis=0), 2).tolist()}"
            f" .. {np.round(pts.max(axis=0), 2).tolist()}"
        )
        RESULTS["check4_outside_bbox"] = [
            np.round(pts.min(axis=0), 3).tolist(),
            np.round(pts.max(axis=0), 3).tolist(),
        ]

    # 旧経路(塊=ブラケット)を同じ格子・同じ面で回す。void = ~design なので構造的に0個。
    t0 = time.perf_counter()
    old = an.run_pipeline(
        shape=soup,
        design_mask=occupied,
        design_grid=grid,
        max_iter=MAX_ITER,
        volfrac=VOLFRAC,
        fixed_face=fixed_face,
        load_face=load_face,
        # 既定の出力先は `output/bracket.stl` で、**check_anchor.py の成果物を上書きしてしまう**。
        stl_path=Path(__file__).resolve().parent / "output" / "design_space_bracket.stl",
    )
    seconds_old = time.perf_counter() - t0
    old_out = int(((old["result"].rho > 0.5) & ~occupied).sum())
    check(
        "塊=ブラケットなら外に材料は1つも出ない(比較対象)",
        old_out == 0,
        f"{old_out} 要素 ({seconds_old:.1f}秒)",
    )

    RESULTS["check4_grid_shape"] = list(grid.shape)
    RESULTS["check4_spacing"] = float(grid.spacing)
    RESULTS["check4_design_elements"] = int(design.sum())
    RESULTS["check4_bracket_elements"] = int(occupied.sum())
    RESULTS["check4_max_iter"] = MAX_ITER
    RESULTS["check4_outside_elements"] = int(grown.sum())
    RESULTS["check4_outside_elements_old_path"] = old_out
    RESULTS["check4_seconds_new"] = seconds_new
    RESULTS["check4_seconds_old"] = seconds_old
    RESULTS["check4_compliance_new"] = new["compliance_last"]
    RESULTS["check4_compliance_old"] = old["compliance_last"]


# ===========================================================================
# 案1(外接直方体+余白): 4条件だけから塊を導出する
#   タスク 20260903-2234。以下は `design_space.py` の受入検査で、
#   **どれも「実装を壊すと実際に落ちる」ことを変異検査で確認してある**(README に記録)。
# ===========================================================================
# 検査の格子。**収束ではなく導出の性質を見るので粗い。**
CASE1_LONG_AXIS = bench.DESIGN_SPACE_CASE1["elements_long_axis"]
CASE1_MAX_ITER = bench.DESIGN_SPACE_CASE1["max_iter"]
_CASE1: dict = {}


def case1_space():
    """案1の導出を1回だけ回して使い回す(検査5〜8が同じものを見る)。"""
    if "space" not in _CASE1:
        cond = ds.demo_conditions(0.0)
        _CASE1["conditions"] = cond
        _CASE1["space"] = ds.derive(cond, elements_long_axis=CASE1_LONG_AXIS)
    return _CASE1["conditions"], _CASE1["space"]


# ---------------------------------------------------------------------------
# 5. 4条件だけで塊が出て、端から端まで通る
# ---------------------------------------------------------------------------
def check_case1_end_to_end() -> None:
    print("\n[5] 案1: 4条件だけ(設計空間ソリッド無し)で端から端まで通る")
    cond, space = case1_space()
    # `Conditions` に設計空間ソリッドを入れる場所が無いことを型で見る。**渡していない**の
    # 根拠を「渡さなかった」という自己申告に置かない。
    field_names = {f.name for f in dataclasses.fields(ds.Conditions)}
    check(
        "条件は4つだけ(設計空間ソリッドを受け取る口が無い)",
        field_names == {"keep", "fixed_face", "load_face", "obstacle", "anchor_shape"},
        f"{sorted(field_names)}",
    )
    print(
        f"  格子 {space.grid.shape} spacing={space.grid.spacing:.3f}mm / "
        f"塊 {space.element_count} 要素 / 余白 {space.margin:.2f}mm(比率 {space.margin_ratio}) / "
        f"面 固定={space.fixed_face} 荷重={space.load_face}",
        flush=True,
    )

    stl = Path(__file__).resolve().parent / "output" / "check_design_space_case1.stl"
    if stl.exists():
        stl.unlink()
    t0 = time.perf_counter()
    out = ds.run_topopt(space, max_iter=CASE1_MAX_ITER, volfrac=VOLFRAC, stl_path=stl)
    seconds = time.perf_counter() - t0
    check(
        "STL まで出る(塊 → 面アンカー → SIMP → marching cubes → STL)",
        stl.exists() and stl.stat().st_size > 0 and out["iterations"] == CASE1_MAX_ITER,
        f"{stl.name} {stl.stat().st_size if stl.exists() else 0} bytes / "
        f"{out['iterations']}反復 / {seconds:.1f}秒",
    )

    # **本丸**: 条件のどのソリッドにも入っていない空間に材料が置かれた = 塊が条件から作られ、
    # かつ最適化がその中で自由に形を作った。ここが0だと「保持ソリッドを塗っただけ」になる。
    keep_occ, _ = vx.occupancy(
        vx.triangles_from_shape(vx._unwrap(cond.keep), deviation=space.stats["deviation"]),
        space.grid,
    )
    grown = (out["result"].rho > 0.5) & ~keep_occ & ~np.asarray(out["solid"], dtype=bool)
    check(
        "条件のソリッドが無い空間に材料ができた(塊が条件だけから起きている)",
        int(grown.sum()) >= 1,
        f"{int(grown.sum())} 要素 (全体の ρ>0.5 は {int((out['result'].rho > 0.5).sum())} 要素)",
    )
    _CASE1["out"] = out
    _CASE1["keep_occ"] = keep_occ
    RESULTS["case1_grid_shape"] = list(space.grid.shape)
    RESULTS["case1_spacing"] = float(space.grid.spacing)
    RESULTS["case1_design_elements"] = space.element_count
    RESULTS["case1_margin_mm"] = round(space.margin, 3)
    RESULTS["case1_grown_elements"] = int(grown.sum())
    RESULTS["case1_seconds"] = round(seconds, 1)


# ---------------------------------------------------------------------------
# 6. 保持ソリッドが塊に入っている
# ---------------------------------------------------------------------------
def check_case1_keep_inside() -> None:
    print("\n[6] 案1: 保持ソリッドが塊に完全に含まれている")
    _cond, space = case1_space()
    outside = int(np.count_nonzero(space.model.keep & ~space.mask))
    dropped = int(space.model.stats["keep_dropped_outside"])
    check(
        "保持のはみ出しが0要素",
        outside == 0 and dropped == 0,
        f"塊の外に出た保持 {outside} 要素 / voxelize が捨てた {dropped} 要素",
    )
    # **「はみ出し0」だけでは足りない。** 解像度が粗すぎて保持が1要素も立たないと、はみ出しも
    # 0になって検査が自明に通る(前タスクのQAが指摘した「構造的恒真」と同じ穴)。
    keep_elements = int(np.count_nonzero(space.model.keep))
    check(
        "保持が塊の中で1要素以上に表現されている(はみ出し0が自明に通るのを防ぐ)",
        keep_elements > 0,
        f"{keep_elements} 要素",
    )
    # 格子の外は voxelize が見ていないので、格子が保持を覆っていることも別に見る。
    g_lo, g_hi = space.grid.world_box()
    k_lo, k_hi = vx._shape_bbox(vx._unwrap(_cond.keep))
    check(
        "保持ソリッドが格子そのものの内側にある(格子の外は誰も数えていない)",
        bool(np.all(k_lo >= g_lo - 1e-9) and np.all(k_hi <= g_hi + 1e-9)),
        f"保持 {np.round(k_lo, 2).tolist()}..{np.round(k_hi, 2).tolist()} / "
        f"格子 {np.round(g_lo, 2).tolist()}..{np.round(g_hi, 2).tolist()}",
    )
    RESULTS["case1_keep_elements"] = keep_elements

    # **黙って切り詰めないこと。** 除外が保持に重なると、塊 = 直方体 − 除外 の結果として保持が
    # 塊からはみ出す。ここで `on_outside="clip"` に倒れていると、保持ソリッドの一部が黙って
    # 消えたまま最適化が回ってしまう(利用者は塊を見ないので永久に気づけない)。
    from build123d import Box, Pos

    eater = Pos(-30, 0, 0) * Box(10, 30, 30)  # 固定側パッドを丸ごと覆う除外
    greedy = ds.Conditions(
        keep=_cond.keep,
        fixed_face=_cond.fixed_face,
        load_face=_cond.load_face,
        obstacle=eater,
    )
    raised = None
    try:
        ds.derive(greedy, elements_long_axis=CASE1_LONG_AXIS)
    except ds.DesignSpaceTooSmallError as e:
        raised = e
    check(
        "除外が保持を食うと例外になる(黙って切り詰めない)",
        raised is not None and isinstance(raised, vx.OutsideDesignSpaceError),
        f"{type(raised).__name__ if raised else '例外が上がらなかった'}",
    )
    if raised is not None:
        check(
            "文面が除外ソリッドの重なりを名指しする(「座標系が違う」で誤誘導しない)",
            "除外ソリッド" in str(raised) and "margin_ratio" in str(raised),
            f"{str(raised)[:120]}",
        )


# ---------------------------------------------------------------------------
# 7. 拘束面・荷重面のアンカーが空にならない
# ---------------------------------------------------------------------------
def check_case1_anchors_not_empty() -> None:
    print("\n[7] 案1: 拘束面・荷重面のアンカーが空にならない")
    _cond, space = case1_space()
    table = an.build_anchors(space.soup, space.grid)
    empty = table.empty_faces()
    bad = [f for f in (space.fixed_face, space.load_face) if f in empty]
    check(
        "拘束面・荷重面が empty_faces() に入っていない",
        not bad,
        f"空になった条件面 {bad} / 空の面は全体で {len(empty)} 面",
    )
    n_fixed = table[space.fixed_face].node_count
    n_load = table[space.load_face].node_count
    check(
        "両面とも節点が付いている",
        n_fixed > 0 and n_load > 0,
        f"拘束面 {n_fixed} 節点 / 荷重面 {n_load} 節点",
    )
    check(
        "拘束面から固定自由度が引ける(SIMP に渡せる)",
        table.fixed_dofs(space.fixed_face).size >= 3,
        f"{table.fixed_dofs(space.fixed_face).size} 自由度",
    )
    RESULTS["case1_fixed_nodes"] = int(n_fixed)
    RESULTS["case1_load_nodes"] = int(n_load)

    # **条件面は保持ソリッドの外にも置ける。** 点群が保持ソリッドの頂点だけで作られていると、
    # 離れた場所にある拘束面が塊に入らず、そこにアンカーを張れない。デモでは面が保持の上に
    # 乗っているのでこの穴は見えない —— 面が離れている配置を1つ作って直接突く。
    from build123d import Box, Pos

    remote = Pos(70, 0, 0) * Box(6, 20, 20)  # 保持ではない、拘束面だけを提供する板
    anchor_shape = _cond.keep + remote
    a_soup = vx.triangles_from_shape(vx._unwrap(anchor_shape), deviation=0.2)
    far_fixed = an.faces_matching(
        a_soup, kind="plane", normal=(1, 0, 0), near_point=(73, 0, 0), near_tol=1e-4
    )
    far_load = an.faces_matching(
        a_soup, kind="plane", normal=(0, 0, 1), near_point=(26, 0, 18), near_tol=1e-4
    )
    check(
        "足場: 離れた板の面を一意に選べた",
        len(far_fixed) == 1 and len(far_load) == 1,
        f"拘束={far_fixed} 荷重={far_load}",
    )
    if len(far_fixed) == 1 and len(far_load) == 1:
        far_space = ds.derive(
            ds.Conditions(
                keep=_cond.keep,
                fixed_face=far_fixed[0],
                load_face=far_load[0],
                obstacle=_cond.obstacle,
                anchor_shape=anchor_shape,
            ),
            elements_long_axis=CASE1_LONG_AXIS,
        )
        check(
            "保持ソリッドの外にある条件面が外接直方体に入っている",
            float(far_space.bbox_max[0]) >= 73.0 - 1e-6,
            f"外接直方体の x 上限 {float(far_space.bbox_max[0]):.3f}mm "
            f"(保持だけなら 30.0mm、板の面は 73.0mm)",
        )
        far_table = an.build_anchors(far_space.soup, far_space.grid)
        check(
            "離れた条件面にもアンカーが張れている",
            far_space.fixed_face not in far_table.empty_faces()
            and far_table[far_space.fixed_face].node_count > 0,
            f"{far_table[far_space.fixed_face].node_count} 節点",
        )


# ---------------------------------------------------------------------------
# 8. 除外ソリッドが引かれている / 点群には入らない / 塊の外の除外は no-op
# ---------------------------------------------------------------------------
def check_case1_obstacle() -> None:
    print("\n[8] 案1: 除外ソリッドが塊から引かれている")
    from build123d import Box, Pos

    cond, space = case1_space()
    # 塊の中の除外領域を**独立に**計算し直す(design_space.py が引いた結果を再利用しない)。
    obs_soup = vx.triangles_from_shape(
        vx._unwrap(cond.obstacle), deviation=space.stats["deviation"]
    )
    obs, _ = vx.occupancy(obs_soup, space.grid)
    overlap = int(np.count_nonzero(space.mask & obs))
    removed = int(space.stats["obstacle_elements_removed"])
    check(
        "除外の領域に塊の要素が1つも無い",
        overlap == 0,
        f"重なり {overlap} 要素(引かれたのは {removed} 要素)",
    )
    check(
        "除外は実際に何かを引いている(検査が自明に通っていない)",
        removed > 0 and int(obs.sum()) > 0,
        f"除外の占有 {int(obs.sum())} 要素 / うち直方体の中 {removed} 要素",
    )

    # **除外は点群に入らない。** 遠くに除外を足しても外接直方体は1mmも動かない。
    far = Pos(300, 0, 0) * Box(20, 20, 20)
    cond_far = ds.Conditions(
        keep=cond.keep,
        fixed_face=cond.fixed_face,
        load_face=cond.load_face,
        obstacle=cond.obstacle + far,
    )
    space_far = ds.derive(cond_far, elements_long_axis=CASE1_LONG_AXIS)
    check(
        "遠くの除外を足しても条件の外接直方体が変わらない(除外は塊を広げない)",
        bool(
            np.allclose(space_far.bbox_min, space.bbox_min)
            and np.allclose(space_far.bbox_max, space.bbox_max)
        ),
        f"{np.round(space_far.bbox_min, 3).tolist()}..{np.round(space_far.bbox_max, 3).tolist()}",
    )
    check(
        "塊の外の除外はただの no-op(例外にせず、塊も変わらない)",
        bool(np.array_equal(space_far.mask, space.mask)),
        f"塊 {space_far.element_count} 要素 (元は {space.element_count} 要素)",
    )

    # **設計判断の記録**: 同じ除外を `model_from_design_mask` の `obstacle` に渡すと、塊から
    # 引き切ってあるぶん**必ず全要素はみ出して**例外になる。だから渡さない(design_space.py の
    # docstring)。ここが将来通るようになったら、その判断の前提が消えたということ。
    raised = None
    try:
        vx.model_from_design_mask(
            space.grid,
            space.mask,
            anchor_shape=space.soup,
            keep=cond.keep,
            obstacle=cond.obstacle,
        )
    except vx.OutsideDesignSpaceError as e:
        raised = e
    check(
        "引き切った除外を obstacle として渡すと必ず例外になる(渡さない判断の根拠)",
        raised is not None and raised.outside.get("obstacle", 0) > 0,
        f"outside={getattr(raised, 'outside', None)}",
    )
    check(
        "導出した塊の VoxelModel は obstacle を持たない(除外は塊に畳んである)",
        not bool(space.model.obstacle.any()),
        f"obstacle {int(space.model.obstacle.sum())} 要素",
    )
    RESULTS["case1_obstacle_removed"] = removed


# ---------------------------------------------------------------------------
# 9. 余白を外すと壊れる(余白が自由パラメータであることを検査で固定する)
# ---------------------------------------------------------------------------
def check_case1_margin_is_load_bearing() -> None:
    print("\n[9] 案1: 余白を外すと壊れる(余白が自由パラメータであることの固定)")
    cond, _space = case1_space()
    ladder = [ds.DEFAULT_MARGIN_RATIO, 0.10, 0.05, 0.02, 0.0, -0.01, -0.02, -0.05, -0.10]
    verdicts: list[tuple[float, str]] = []
    for ratio in ladder:
        try:
            sp = ds.derive(cond, margin_ratio=ratio, elements_long_axis=CASE1_LONG_AXIS)
        except ds.DesignSpaceTooSmallError as e:
            verdicts.append((ratio, "格子外" if "格子" in str(e) else "保持がはみ出す"))
            continue
        except ValueError as e:  # noqa: PERF203 — 比率ごとに理由を残す
            verdicts.append((ratio, f"ValueError({str(e)[:30]})"))
            continue
        table = an.build_anchors(sp.soup, sp.grid)
        empty = [f for f in (sp.fixed_face, sp.load_face) if f in table.empty_faces()]
        verdicts.append((ratio, "アンカーが空" if empty else "OK"))
    for ratio, verdict in verdicts:
        print(f"      余白比率 {ratio:+.2f} → {verdict}")

    broke = [r for r, v in verdicts if v != "OK"]
    check(
        "余白を削っていくと必ず壊れる比率がある(余白は自由パラメータで、外すと壊れる)",
        bool(broke),
        f"壊れた比率 {broke}",
    )
    if broke:
        cliff = max(broke)
        # ここが言えるのは「壊れる比率は既定より下にある」までで、**既定が安全であることの
        # 裏付けにはならない**。cliff は負(-0.05)なので、既定を 0.02 まで下げてもこの assert は
        # 通る。既定 0.25 の根拠は検査ではなく実測(README「余白比率をどう決めたか」)にある。
        check(
            "壊れる比率は既定より下にある",
            cliff < ds.DEFAULT_MARGIN_RATIO,
            f"最も大きい壊れる比率 {cliff:+.2f} / 既定 {ds.DEFAULT_MARGIN_RATIO} "
            f"(隔たり {ds.DEFAULT_MARGIN_RATIO - cliff:.2f})",
        )
        RESULTS["case1_margin_cliff"] = float(cliff)
    ok_ratios = [r for r, v in verdicts if v == "OK"]
    check(
        "既定の余白比率では壊れない",
        ds.DEFAULT_MARGIN_RATIO in ok_ratios,
        f"通った比率 {ok_ratios}",
    )
    # **格子の外は voxelize が数えていない。** 余白を大きく削ると保持が格子そのものから出るが、
    # 格子の外にはセル中心が無いので、はみ出しは件数にも例外にも現れない(前タスクのQA指摘)。
    # design_space.py はそれを自分で見て落とす。ここが消えたら黙って削れるようになる。
    check(
        "余白を大きく削ると「格子の外」として落ちる(件数に現れないはみ出しを塞いである)",
        "格子外" in [v for _r, v in verdicts],
        f"{[(r, v) for r, v in verdicts if v != 'OK']}",
    )
    # **既定値が「壊れない一番小さい値」ではない**ことを記録する。検査2・3は「保持が入るか」
    # しか見ておらず、「答えの居場所が残っているか」は見ていない —— 余白ゼロでも検査は通る。
    # 既定値の根拠は検査ではなく README の実測(壁に接する材料の量)にある。
    RESULTS["case1_margin_verdicts"] = [[float(r), v] for r, v in verdicts]


# ===========================================================================
# 連結性ガード: 塊の中で拘束と荷重がひと続きか
#   タスク 20260903-2235。**変異検査で検出力を確認してある**(README「変異検査」)。
# ===========================================================================
def _two_islands(shape=(10, 6, 6)):
    """x 方向に隙間を空けた2つの島。拘束は左の島、荷重は右の島に置く。

    **導出ロジックを経由しない直接入力。** 塊を bool マスクで与えられる口(検査[1])があるので、
    連結性の判定そのものを、答えが手で分かっているマスクで突ける。
    """
    design = np.zeros(shape, dtype=bool)
    design[0:4] = True
    design[6:10] = True  # x=4,5 が空 → 6近傍でも26近傍でも2成分
    fixed = np.zeros(shape, dtype=bool)
    fixed[0, 0, 0] = True
    load = np.zeros(shape, dtype=bool)
    load[9, 5, 5] = True
    return design, fixed, load


def _corner_touch(shape=(8, 8, 8)):
    """**角1点だけで接している2つの島。** 6近傍なら2成分、26近傍なら1成分になる。

    要素 (3,3,3) と (4,4,4) が共有するのは節点1つだけ。構造としてはピン接合(機構)で、
    力の道にはならない —— だから 6近傍で判定する。
    """
    design = np.zeros(shape, dtype=bool)
    design[0:4, 0:4, 0:4] = True
    design[4:8, 4:8, 4:8] = True
    fixed = np.zeros(shape, dtype=bool)
    fixed[0, 0, 0] = True
    load = np.zeros(shape, dtype=bool)
    load[7, 7, 7] = True
    return design, fixed, load


def check_case1_connectivity() -> None:
    print("\n[10] 連結性: 塊の中で拘束面と荷重面がひと続きか")
    _cond, space = case1_space()

    # --- 10-1 正常系が通る ------------------------------------------------
    report = space.stats.get("connectivity", {})
    check(
        "正常系(案1の導出)で例外が上がらず、拘束と荷重が同じ連結成分にいる",
        bool(report.get("shared_labels"))
        and report.get("components") == 1
        and report.get("neighbourhood") == ds.NEIGHBOURHOOD,
        f"{report}",
    )
    check(
        "検査が自明に通っていない(両側のアンカー要素が塊の中に実際にある)",
        int(report.get("fixed_elements", 0)) > 0 and int(report.get("load_elements", 0)) > 0,
        f"拘束側 {report.get('fixed_elements')} 要素 / 荷重側 {report.get('load_elements')} 要素",
    )

    # --- 10-2 除外ソリッドで塞ぐと落ちる ----------------------------------
    raised = None
    try:
        ds.derive(ds.severed_conditions(0.0), elements_long_axis=CASE1_LONG_AXIS)
    except ds.DesignSpaceDisconnectedError as e:
        raised = e
    check(
        "拘束と荷重の間を除外ソリッドで断ち切ると例外になる",
        raised is not None,
        f"{type(raised).__name__ if raised else '例外が上がらなかった'}",
    )
    if raised is not None:
        check(
            "文面に成分の数と、拘束側/荷重側がどの成分にいるかが出る",
            "連結成分は 2 個" in str(raised)
            and raised.components == 2
            and raised.fixed_labels
            and raised.load_labels
            and not set(raised.fixed_labels) & set(raised.load_labels),
            f"成分{raised.components} 拘束={raised.fixed_labels} 荷重={raised.load_labels} / "
            f"{str(raised)[:90]}",
        )
        # **型を分けてあることを見る。** `DesignSpaceTooSmallError` は「余白を上げろ」と言う
        # 文面で、分断には効かない直し方を指す(余白を上げても塞がれた道は開かない)。
        check(
            "分断は DesignSpaceTooSmallError ではない(余白を上げろと誤誘導しない)",
            not isinstance(raised, ds.DesignSpaceTooSmallError)
            and isinstance(raised, ValueError),
            f"{type(raised).__mro__[1].__name__} を継承 / "
            f"「margin_ratio」を含む={('margin_ratio' in str(raised))}",
        )
    # 対照: 同じ条件から壁だけを外すと通る(落ちた原因が壁であることの裏取り)。
    check(
        "壁を外すと同じ条件が通る(落とした原因が除外ソリッドの壁であることの裏取り)",
        ds.derive(ds.demo_conditions(0.0), elements_long_axis=CASE1_LONG_AXIS).stats[
            "connectivity"
        ]["components"]
        == 1,
        "壁なしの demo_conditions は1成分",
    )

    # --- 10-3 塊を直接2つの島にすると落ちる -------------------------------
    design, fixed, load = _two_islands()
    check(
        "塊を直接2つの島にして渡すと例外(導出ロジックを経由しない直接入力)",
        _raises(ds.assert_load_path, ds.DesignSpaceDisconnectedError, design, fixed, load),
        f"{design.shape} の塊を x=4,5 で切った",
    )
    joined = design.copy()
    joined[4:6] = True  # 隙間を埋める
    ok_report = ds.assert_load_path(joined, fixed, load)
    check(
        "隙間を埋めると同じ入力が通る(検査3が構造的に落ちているのではない)",
        ok_report["components"] == 1 and ok_report["shared_labels"] == [1],
        f"{ok_report['components']} 成分 / 共通 {ok_report['shared_labels']}",
    )

    # --- 10-4 6近傍と26近傍の違いが効いている ------------------------------
    corner, c_fixed, c_load = _corner_touch()
    n6 = ds.element_components(corner, neighbourhood=6)[1]
    n26 = ds.element_components(corner, neighbourhood=26)[1]
    check(
        "足場: 角1点だけで接する2島は 6近傍で2成分・26近傍で1成分",
        n6 == 2 and n26 == 1,
        f"6近傍 {n6} 成分 / 26近傍 {n26} 成分",
    )
    check(
        "角1点で接しているだけの配置は落ちる(明文化した 6近傍の定義と実装が一致している)",
        ds.NEIGHBOURHOOD == 6
        and _raises(
            ds.assert_load_path, ds.DesignSpaceDisconnectedError, corner, c_fixed, c_load
        ),
        f"NEIGHBOURHOOD={ds.NEIGHBOURHOOD}",
    )

    # --- 10-5 アンカー要素が塊の中に1つも無い場合も落ちる ------------------
    empty_side = np.zeros_like(design)
    check(
        "片側のアンカー要素が塊の中に1つも無い場合も落ちる(黙って通さない)",
        _raises(ds.assert_load_path, ds.DesignSpaceDisconnectedError, joined, fixed, empty_side),
        "荷重側のアンカー要素が0個",
    )

    RESULTS["connectivity_neighbourhood"] = int(ds.NEIGHBOURHOOD)
    RESULTS["case1_connectivity"] = {
        "components": report.get("components"),
        "fixed_elements": report.get("fixed_elements"),
        "load_elements": report.get("load_elements"),
    }
    if raised is not None:
        RESULTS["severed_components"] = int(raised.components)


def main() -> int:
    print("=" * 72)
    print("check_design_space.py : 塊(設計空間)の受入検査")
    print("  [1]-[4] 分離 / [5]-[9] 案1(外接直方体+余白) / [10] 連結性ガード")
    print("=" * 72)
    # **1件が例外で落ちても残りを走らせる。** 実装を壊したときに最初の1件で止まると、
    # 何がどこまで壊れているかが1周では分からない(`check()` と同じ方針を関数の粒度でも取る)。
    for section in (
        check_mask_is_the_design_space,
        check_anchors_independent_of_design,
        check_outside_raises,
        check_material_leaves_the_input_solid,
        check_case1_end_to_end,
        check_case1_keep_inside,
        check_case1_anchors_not_empty,
        check_case1_obstacle,
        check_case1_margin_is_load_bearing,
        check_case1_connectivity,
    ):
        try:
            section()
        except Exception as exc:  # noqa: BLE001 — 検査中の例外も不合格として記録する
            import traceback

            traceback.print_exc()
            check(f"{section.__name__} が例外で落ちた", False, f"{type(exc).__name__}: {exc}")

    print("\n" + "=" * 72)
    print("MEASURED " + json.dumps(RESULTS, ensure_ascii=False, default=float))
    print("=" * 72)
    if FAILURES:
        print(f"NG: {len(FAILURES)}件が不合格")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK: 全検査に合格")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

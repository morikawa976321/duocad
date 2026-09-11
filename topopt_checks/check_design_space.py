"""塊(設計空間)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_design_space.py

**このファイルが合否そのものである。** どれか1つでも落ちればプロセスは非ゼロで終わる。

- **[1]〜[4]**: タスク `20260903-2216-topopt-split-design-space-from-anchor-soup`
  (塊と面アンカーの対象ソリッドの分離)の完了条件1〜3。
- **[5]〜[9]**: タスク `20260903-2234-topopt-design-space-from-bbox`
  (案1: 4条件から外接直方体+余白で塊を導出する)の受入検査。**[5]〜[9]は変異検査を通してある**
  —— 実装を壊すと実際に落ちることを1件ずつ確認した(対応表は docs/TOPOPT.md「変異検査」)。

[1]〜[4] の構成:

1. **塊がソリッドから来ない** — 渡した bool マスクが要素単位でそのまま `VoxelModel.design`
   になる(かつ呼び出し側の配列と共有していない)。
2. **面アンカーが塊に依存しない** — 格子と soup を固定したまま塊だけを差し替えても、
   `anchors_for` が返す面添字の集合・節点の通し番号・担当面積が完全に一致する。
3. **はみ出しが例外になる** — 保持ソリッドが塊から出る配置で `OutsideDesignSpaceError` が
   上がり、メッセージにはみ出し要素数が入る。**同じ配置を `voxelize()` に通すと
   例外が出ずに黙って切り詰められることを先に確認する**(検出力の確認)。
4. **塊が入力ソリッドに縛られていない(本丸)** — 塊をブラケットより大きい箱にし、面は
   ブラケットのものを使って SIMP を回すと、**元のブラケットの外に ρ>0.5 の要素ができる**。
   同じ格子・同じ面で塊=ブラケットの経路を回すと、`void = ~design` により構造的に0個。
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
    判定を外す(10-2・10-3・10-4 が素通り)。docs/TOPOPT.md「変異検査」に結果がある。

[13] の構成(既定の導出。タスク `20260904-1416`):

13. **既定が連続値保持で、入口が1つ** — `derive_default` が連続値保持を返す(参照 run と k と
    回廊の記録が付く)/ **塊が材料量について単調に増える**(外側の二分探索の前提)/
    **`material_mm3` と `reference` が素通しに
    なっている**(後続の安全率の外側ループがこの2つを使う。参照を回し直していないことを
    秒数の一致で見る)/ **格子の違う参照は例外**(黙って別の場所を切り出さない)/ 案1が
    残っていて既定値も動いていない(連続値保持の土台であり比較のベースライン)/
    `derivation="bbox"` で戻せる / 未知の名前は例外 / **導出にこだわりのない呼び出し側
    (`safety_loop.py`)が導出を名指ししていない** —— 名指しが1つでも残っていると、次に既定を
    倒すときにそこだけ取り残される。

[14] の構成(空の条件面のメッセージ。タスク `20260904-1556`):

14. **拘束面・荷重面のアンカーが空でも例外にせず、メッセージが返る** — `anchor.run_pipeline` /
    `design_space.run_topopt` の両方で、荷重側・拘束側それぞれが空のときに
    `empty_condition_faces`(側つきの診断)が戻り値に載ること。診断の数値(要素サイズ・
    CAD面積・代表寸法)とメッセージの文言(面添字・逃げ道2つ)を見る / `design_space` 側は
    `DesignSpace.stats` にも同じ診断が載ること / **通常経路(空の条件面なし)は影響を受けない**
    (回帰確認)/ **変異検査**(空面診断を外すと元の `ValueError` に戻ることを毎回自動で確認。
    docs/TOPOPT.md「変異検査の記録(2026-09-05)」)。
    **14-6/14-7**: `run_topopt` の早期return(`"result"` が `None`)を受け取る側が
    `.rho` へ無条件に触ると `AttributeError` になる。既定の導出でその位置に居るのは
    `build_reference`(参照 run)なので、そこが `ReferenceRunConditionFacesEmptyError` を
    上げること・`derive_default`(既定)経由でも同じ例外になること・変異検査(ガードを
    無力化すると `AttributeError` に戻ること)を見る。
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import math
import sys
import time

import numpy as np

# **Windowsの既定コンソール(cp932)で落ちないようにする。** 記号が1つ混ざるだけで
# `UnicodeEncodeError` になり、検査が1件も走らないまま異常終了する。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from _bootstrap import _REPO_ROOT
from duocad.topopt import OUTPUT_DIR
from duocad.topopt import anchor as an
from duocad.topopt import bench
from duocad.topopt import design_space as ds
from duocad.topopt import voxelize as vx

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
# 3. はみ出しが例外になる(`voxelize()` が素通りすることを先に見る)
# ---------------------------------------------------------------------------
def check_outside_raises() -> None:
    print("\n[3] 保持ソリッドが塊からはみ出したら例外")
    from build123d import Box, Pos

    grid, _soup = bracket_grid(6.0, 24)
    bracket = an.demo_bracket()
    # 箱の上面(z=12)をまたぐ保持ソリッド。下半分は塊の中、上半分は外。
    keep = Pos(0, 0, 14) * Box(10, 10, 8)

    # (a) voxelize() —— 例外は出ず、件数だけが stats に残る。**検出力の確認。**
    old = vx.voxelize(bracket, 24, keep=keep, grid=grid)
    dropped_old = int(old.stats["keep_dropped_outside"])
    # model_from_design_mask には voxelize() が作ったのと同じ塊・同じ soup を渡す。片方だけ
    # 三角形分割の細かさが違うと、比べているのが「はみ出しの扱い」ではなく「分割の差」になる。
    occupied, soup = old.design, old.soup
    check(
        "voxelize() は同じ配置を例外にせず黙って切り詰める(検出力の確認)",
        dropped_old > 0 and int(old.stats["keep_elements"]) > 0,
        f"はみ出し {dropped_old} 要素を捨て、残り {int(old.stats['keep_elements'])} 要素で正常終了",
    )

    # (b) model_from_design_mask —— 既定で例外。メッセージにはみ出し要素数が入る。
    raised = None
    try:
        vx.model_from_design_mask(grid, occupied, anchor_shape=soup, keep=keep)
    except vx.OutsideDesignSpaceError as e:
        raised = e
    check(
        "model_from_design_mask は既定で OutsideDesignSpaceError",
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

    # (c) 明示的に切り詰めを頼めば、voxelize() と同じ結果になる。
    clipped = vx.model_from_design_mask(
        grid, occupied, anchor_shape=soup, keep=keep, on_outside="clip"
    )
    check(
        "on_outside='clip' なら voxelize() と同じ切り詰め結果",
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
        fixed_faces=fixed_face,
        load_faces=load_face,
        stl_path=OUTPUT_DIR / "design_space_box.stl",
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

    # 塊=ブラケットの経路を同じ格子・同じ面で回す。void = ~design なので構造的に0個。
    t0 = time.perf_counter()
    old = an.run_pipeline(
        shape=soup,
        design_mask=occupied,
        design_grid=grid,
        max_iter=MAX_ITER,
        volfrac=VOLFRAC,
        fixed_faces=fixed_face,
        load_faces=load_face,
        # 既定の出力先は `output/bracket.stl` で、**check_anchor.py の成果物を上書きしてしまう**。
        stl_path=OUTPUT_DIR / "design_space_bracket.stl",
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
#   **どれも「実装を壊すと実際に落ちる」ことを変異検査で確認してある**(docs/TOPOPT.md に記録)。
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
        "条件は5つだけ(設計空間ソリッドを受け取る口が無い。load_n は荷重の実際の大きさ[N])"
        "。**面が複数になっても口の数は増えない** —— この検査が守っているのは"
        "「塊を外から渡させない」ことであって、面の枚数ではない",
        field_names == {"keep", "fixed_faces", "load_faces", "obstacle", "anchor_shape", "load_n"},
        f"{sorted(field_names)}",
    )
    print(
        f"  格子 {space.grid.shape} spacing={space.grid.spacing:.3f}mm / "
        f"塊 {space.element_count} 要素 / 余白 {space.margin:.2f}mm(比率 {space.margin_ratio}) / "
        f"面 固定={space.fixed_faces} 荷重={space.load_faces}",
        flush=True,
    )

    stl = OUTPUT_DIR / "check_design_space_case1.stl"
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
        fixed_faces=_cond.fixed_faces,
        load_faces=_cond.load_faces,
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
    bad = [f for f in tuple(space.fixed_faces) + tuple(space.load_faces) if f in empty]
    check(
        "拘束面・荷重面が empty_faces() に入っていない",
        not bad,
        f"空になった条件面 {bad} / 空の面は全体で {len(empty)} 面",
    )
    n_fixed = sum(table[f].node_count for f in space.fixed_faces)
    n_load = sum(table[f].node_count for f in space.load_faces)
    check(
        "両面とも節点が付いている",
        n_fixed > 0 and n_load > 0,
        f"拘束面 {n_fixed} 節点 / 荷重面 {n_load} 節点",
    )
    check(
        "拘束面から固定自由度が引ける(SIMP に渡せる)",
        table.fixed_dofs(space.fixed_faces).size >= 3,
        f"{table.fixed_dofs(space.fixed_faces).size} 自由度",
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
                fixed_faces=far_fixed[0],
                load_faces=far_load[0],
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
            far_space.fixed_faces[0] not in far_table.empty_faces()
            and far_table[far_space.fixed_faces[0]].node_count > 0,
            f"{far_table[far_space.fixed_faces[0]].node_count} 節点",
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
        fixed_faces=cond.fixed_faces,
        load_faces=cond.load_faces,
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
        empty = [f for f in tuple(sp.fixed_faces) + tuple(sp.load_faces) if f in table.empty_faces()]
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
        # 通る。既定 0.25 の根拠は検査ではなく実測(docs/TOPOPT.md「余白比率をどう決めたか」)にある。
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

    # ---- 格子外ガードの**逆側**: はみ出していない保持を「はみ出した」と言わないこと ----
    # ガードが使う bbox(`vx._shape_bbox`)が実寸を返すことを、三角形の節点を基準に見る
    # (節点は必ず真の面の上にあるので、実寸の独立した根拠になる)。**面のトリムを見ない
    # bbox だとトーラス面の部品が実寸の10倍以上に化け、ガードが嘘の理由で落とす**
    # (実測: `models/hanger_bar_attachment.json` の 35mm 角の部品が 482.8mm と報告された)。
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus

    segment = BRepPrimAPI_MakeTorus(100.0, 10.0, math.pi / 6).Shape()
    seg_lo, seg_hi = vx._shape_bbox(segment)
    seg_soup = vx.triangles_from_shape(segment, deviation=0.2)
    seg_points = np.asarray(seg_soup.positions, dtype=float).reshape(-1, 3)
    check(
        "格子外ガードの bbox は三角形分割と一致する(実寸を返している)",
        bool(
            np.all(np.abs(seg_lo - seg_points.min(axis=0)) < 0.2)
            and np.all(np.abs(seg_hi - seg_points.max(axis=0)) < 0.2)
        ),
        f"bbox {np.round(seg_lo, 2).tolist()}..{np.round(seg_hi, 2).tolist()} / "
        f"三角形 {np.round(seg_points.min(axis=0), 2).tolist()}..{np.round(seg_points.max(axis=0), 2).tolist()}",
    )
    # **既定値が「壊れない一番小さい値」ではない**ことを記録する。検査2・3は「保持が入るか」
    # しか見ておらず、「答えの居場所が残っているか」は見ていない —— 余白ゼロでも検査は通る。
    # 既定値の根拠は検査ではなく docs/TOPOPT.md の実測(壁に接する材料の量)にある。
    RESULTS["case1_margin_verdicts"] = [[float(r), v] for r, v in verdicts]


# ===========================================================================
# 連結性ガード: 塊の中で拘束と荷重がひと続きか
#   タスク 20260903-2235。**変異検査で検出力を確認してある**(docs/TOPOPT.md「変異検査」)。
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


# ===========================================================================
# 既定の導出(入口を1つにする): タスク 20260904-1416
# ===========================================================================
def check_default_derivation() -> None:
    print("\n[13] 既定の導出(入口が1つ)")

    cond = ds.demo_conditions(0.0)

    # 連続値保持(既定)を回すための引数。**格子はここだけ `DESIGN_SPACE_CASE1` の 14要素より
    # 上げる。** 14要素の格子で容量の8%(参照の既定)を目標にすると密度が低すぎて `simp` が
    # 残差検算で落ちる(文面は「拘束不足の可能性が高い」になるが、実際は低密度で系が悪条件に
    # なっているだけ)。30要素なら参照の構造が1成分で出て、k は出発値 1.5 のまま通る。
    ref_kw = {"elements_long_axis": 30}
    case1 = ds.derive(cond, **ref_kw)
    case1_capacity = case1.element_count * float(case1.grid.spacing) ** 3
    ref_material = 0.15 * case1_capacity
    reference = ds.build_reference(cond, **ref_kw)

    # --- 13-1 既定は連続値保持 ----------------------------------------------
    check(
        "13-1 既定の導出が連続値保持(reference)",
        ds.DEFAULT_DERIVATION == "reference" and ds.DEFAULT_DERIVATION in ds.DERIVATIONS,
        f"DEFAULT_DERIVATION={ds.DEFAULT_DERIVATION!r} / 選べるのは {sorted(ds.DERIVATIONS)}",
    )
    default_space = ds.derive_default(
        cond, material_mm3=ref_material, reference=reference, **ref_kw
    )
    check(
        "13-1 入口が返す塊が連続値保持のもの(参照 run と k の記録が付いている)",
        default_space.stats.get("derivation") == "reference"
        and isinstance(default_space.stats.get("reference"), dict)
        and default_space.stats["reference"]["iterations"] >= 1
        and default_space.stats["k"] > 1.0,
        f"derivation={default_space.stats.get('derivation')!r} / "
        f"参照 {default_space.stats.get('reference', {}).get('iterations')} 反復 / "
        f"k={default_space.stats.get('k')}",
    )
    # **予算は3つの床の最大。** 端数は1要素まで許す(要素数は整数)。
    ev = case1.grid.element_volume
    budget = default_space.stats["budget"]
    check(
        "13-1 塊は摘み・回廊・材料の3つの床の最大以上",
        default_space.element_count >= max(budget.values()) - 1,
        f"塊 {default_space.element_count} 要素 / 予算 {budget} / "
        f"効いたのは {default_space.stats['budget_binding']!r}",
    )
    # **摘みは材料量に依存しない**(これが今回の直しの本体)。定数の固定は 13-2 が持つので、
    # ここは振る舞いで見る —— 材料量を2倍にしても摘みの項が1本も動かないこと。
    doubled = ds.derive_default(
        cond, material_mm3=2.0 * ref_material, reference=reference, **ref_kw
    )
    check(
        "13-1 予算の摘みが材料量に依存しない(材料を2倍にしても動かない)",
        doubled.stats["budget"]["pick"] == budget["pick"]
        # 端数は1要素まで許す(`round(2x)` と `2*round(x)` は .5 で1ずれる)。
        and abs(doubled.stats["budget"]["material"] - 2 * budget["material"]) <= 1,
        f"摘み {budget['pick']} → {doubled.stats['budget']['pick']} / "
        f"材料 {budget['material']} → {doubled.stats['budget']['material']}",
    )
    # **幅つき回廊が本当に配線されているか。** `build_reference` が旧の6近傍へ戻っても
    # 予算の算術だけを見る検査は全部緑のままなので、ここは実際の参照に対して測り直して照合する。
    table = reference.base.table
    cond_mask = ds._condition_elements(reference.base, table)
    recomputed, diagnosis = ds.width_corridor_length(
        reference.order, reference.base, table, cond_mask, rmin=reference.rmin
    )
    check(
        "13-1 参照の回廊が幅つき・全条件のもの(6近傍より厳しく、記録と一致する)",
        int(reference.corridor) == int(recomputed)
        and bool(diagnosis["width"])
        and int(reference.corridor) > int(reference.plain_corridor),
        f"記録 {reference.corridor} / 測り直し {recomputed} / "
        f"6近傍 {reference.plain_corridor} / 幅で見た={diagnosis['width']}",
    )
    # **回廊が実際に予算を決める段を1つ作る。** 摘みが常に勝つ設定だけを見ていると、
    # 床としての回廊は一度も効かないまま緑になる。
    # 摘みを 0 にしたうえで、材料の床が回廊を下回る量を**回廊から逆算して**選ぶ
    # (`k_mat × 材料量 / 要素体積 < 回廊`)。題材の要素数に当てはめた定数を置かない。
    floor_material = 0.5 * reference.corridor * ev / ds.K_MATERIAL
    floored = ds.derive_default(
        cond, material_mm3=floor_material, reference=reference, pick_ratio=0.0, **ref_kw
    )
    # **回廊がそのまま塊になる段では、床を1本削ると述語が破れる**(= 床が最小であり、
    # かつその本数が実際に塊へ届いている)。塊の側で述語を測り直すのは二分探索の結論の
    # 言い換えにしかならないので、破れる側で見る。
    check(
        "13-1 回廊が予算を決める段があり、その床は1本削ると満たせない",
        floored.stats["budget_binding"] == "corridor"
        and floored.stats["ranked_elements"] == reference.corridor
        and _raises(
            ds.width_corridor_length,
            ds.DesignSpaceDisconnectedError,
            reference.order[: reference.corridor - 1],
            reference.base,
            table,
            cond_mask,
            rmin=reference.rmin,
        ),
        f"効いた床 {floored.stats['budget_binding']!r} / 取った本数 "
        f"{floored.stats['ranked_elements']}(回廊 {reference.corridor})",
    )
    # **塊は材料量について単調でなければならない**(外側の二分探索がそれを前提にしている)。
    # 材料量ごとに「繋がるまで k を上げる」作りだとここが落ちる —— 繋がった時点で止まるぶん
    # 塊が上下する(bracket 20要素で 45%→2,927要素 / 50%→2,502要素 の実測がある)。
    sizes = [
        ds.derive_default(
            cond, material_mm3=case1_capacity * frac, reference=reference, **ref_kw
        ).element_count
        for frac in (0.20, 0.30, 0.40, 0.50, 0.60)
    ]
    check(
        "13-1 塊が材料量について単調に増える(二分探索が前提にしている)",
        all(sizes[i] <= sizes[i + 1] for i in range(len(sizes) - 1)),
        f"材料 20/30/40/50/60% → 塊 {sizes} 要素",
    )

    # --- 13-2 案1は残っている(連続値保持の土台・比較のベースライン) --------
    check(
        "13-2 案1(derive)が残っていて、既定より大きい塊を返す",
        "reference" not in case1.stats
        and case1.element_count > default_space.element_count,
        f"案1 {case1.element_count} 要素 / 既定 {default_space.element_count} 要素",
    )
    sig = inspect.signature(ds.derive)
    defaults = {
        "margin_ratio": sig.parameters["margin_ratio"].default,
        "elements_long_axis": sig.parameters["elements_long_axis"].default,
    }
    check(
        "13-2 案1の既定値が動いていない(余白比率 0.25 / 長辺 40要素)",
        defaults == {"margin_ratio": ds.DEFAULT_MARGIN_RATIO, "elements_long_axis": 40}
        and ds.DEFAULT_MARGIN_RATIO == 0.25,
        f"{defaults}",
    )
    # **連続値保持の既定値も動かさない**(すべて hanger の実測で決めた値。
    # `design_space.py` の定数のところに根拠が書いてある)。
    reference_defaults = {
        "REFERENCE_MATERIAL_RATIO": ds.REFERENCE_MATERIAL_RATIO,
        "REFERENCE_MAX_ITER": ds.REFERENCE_MAX_ITER,
        "K_MATERIAL": ds.K_MATERIAL,
        "PICK_RATIO": ds.PICK_RATIO,
    }
    check(
        "13-2 連続値保持の既定値が動いていない(参照 容量の8%・10反復 / k_mat 2.0 / p 0.08)",
        reference_defaults
        == {
            "REFERENCE_MATERIAL_RATIO": 0.08,
            "REFERENCE_MAX_ITER": 10,
            "K_MATERIAL": 2.0,
            "PICK_RATIO": 0.08,
        },
        f"{reference_defaults}",
    )

    # --- 13-3 案1を名指しする口と、未知の名前 --------------------------------
    # **`derivation="bbox"` は `optimize_run.run` が実際に名指しする口**(塊の容量と保持体積を
    # 測るのに使う。既定の導出は材料量を要求するので、材料量の探索範囲を決める道具にできない)。
    fallback = ds.derive_default(cond, derivation="bbox", **ref_kw)
    check(
        "13-3 derivation='bbox' で案1を名指しできる",
        "reference" not in fallback.stats and fallback.element_count == case1.element_count,
        f"{fallback.element_count} 要素(案1 {case1.element_count} 要素)",
    )
    check(
        "13-3 知らない導出名は例外(黙って既定へ落ちない)",
        _raises(ds.derive_default, ValueError, cond, derivation="nope"),
        "derivation='nope'",
    )

    # --- 13-4 material_mm3 と reference が素通しになっている -----------------
    # **後続(安全率の外側ループ)がこの2つを使う。** `material_mm3` を塞ぐと材料の床が別の量で
    # 立つことになり、`reference` を塞ぐと材料量ごとに参照を作り直すことになる。
    # **材料の床が他の2つを超える量を選ぶ**(`k_mat × 材料量` が `p × 案1` と回廊の両方より
    # 大きい量。この題材では回廊 2,197本 が摘み 856本 より大きいので、容量の
    # `回廊/(k_mat × 案1)` = 10.3% 超が条件)—— 摘みか回廊が効いている範囲では塊は材料量で
    # 動かないので、素通しを塊の大きさで見ようとすると何を渡しても緑になる。
    wanted = 0.45 * case1_capacity
    passed = ds.derive_default(cond, material_mm3=wanted, reference=reference, **ref_kw)
    check(
        "13-4 material_mm3 が切り出しまで届いている(材料の床が効く量で見る)",
        abs(float(passed.stats["material_mm3"]) - wanted) / wanted < 1e-9
        and passed.stats["budget_binding"] == "material"
        and passed.element_count > default_space.element_count,
        f"渡した {wanted:.1f} mm^3 / 記録 {passed.stats['material_mm3']:.1f} mm^3 / "
        f"効いた床 {passed.stats['budget_binding']!r} / "
        f"塊 {passed.element_count} 要素(材料 {ref_material:.1f} mm^3 なら "
        f"{default_space.element_count} 要素)",
    )
    check(
        "13-4 reference が届いていて、参照 run を回し直していない",
        passed.stats["reference"]["seconds"] == reference.stats["seconds"],
        f"塊の記録 {passed.stats['reference']['seconds']:.3f}秒 / "
        f"渡した参照 {reference.stats['seconds']:.3f}秒",
    )
    # **同じ本数の塊を作り直さない。** 安全率の探索は材料量を振るが、摘みか回廊が効いている段
    # では本数が1本も動かない —— そこで `model_from_design_mask` を払い直すのは丸ごと無駄に
    # なる。**並びが意味を持つ**: 控えは直近1件だけなので、同じ本数を続けて要求して見る。
    # **5%/8% は材料の床(`k_mat × 材料量`)が回廊 2,197本 に届かない量**で、どちらも回廊で
    # 同じ本数になる。45% は材料の床が勝つので本数が変わる。
    first = ds.derive_default(cond, material_mm3=0.05 * case1_capacity, reference=reference, **ref_kw)
    same = ds.derive_default(cond, material_mm3=0.08 * case1_capacity, reference=reference, **ref_kw)
    bigger = ds.derive_default(cond, material_mm3=0.45 * case1_capacity, reference=reference, **ref_kw)
    check(
        "13-4 本数が同じ材料量では塊を作り直さない(本数が変われば作り直す)",
        same.stats["ranked_elements"] == first.stats["ranked_elements"]
        and same.model is first.model
        and bigger.stats["ranked_elements"] != first.stats["ranked_elements"]
        and bigger.model is not first.model,
        f"材料 15%/20% はどちらも {first.stats['ranked_elements']} 本 → "
        f"使い回し {same.model is first.model} / "
        f"材料 45% は {bigger.stats['ranked_elements']} 本 → "
        f"使い回し {bigger.model is first.model}",
    )
    check(
        "13-4 格子の違う参照は黙って使わない(例外)",
        _raises(
            ds.derive_default,
            ValueError,
            cond,
            material_mm3=wanted,
            reference=reference,
            elements_long_axis=ref_kw["elements_long_axis"] + 4,
        ),
        "参照と違う elements_long_axis",
    )
    achieved = float(passed.stats["material_mm3"])

    # --- 13-5 呼び出し側が導出を名指ししていない ----------------------------
    # **ソースを読む検査。** 名指しが1つでも残っていると、次に既定を倒したときにそこだけ
    # 取り残される。**読むのは `duocad/topopt/` の中だけ** —— 製品の経路は
    # `check_optimize_wiring.py` が実際に走らせて見る。
    for fname in ("safety_loop.py",):
        text = (_REPO_ROOT / "duocad" / "topopt" / fname).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        # **`ds.build_reference(` は名指しに数えない。** 参照を探索の最初に1回だけ作るのは
        # 外側ループの仕事で、導出の中からは1回にできない。`_build_reference` は既定が
        # `"reference"` でなければ `None` を返すので、既定を倒しても取り残されない。
        check(
            f"13-5 {fname} が導出を名指ししていない",
            "ds.derive(" not in code and "ds.derive_from_reference(" not in code,
            f"入口 derive_default 経由: {'derive_default' in code}",
        )

    RESULTS["default_derivation"] = {
        "name": ds.DEFAULT_DERIVATION,
        "default_elements": default_space.element_count,
        "case1_elements": case1.element_count,
        "material_mm3": achieved,
        "k": round(float(default_space.stats["k"]), 4),
        "budget": dict(default_space.stats["budget"]),
        "budget_binding": default_space.stats["budget_binding"],
        "corridor": int(default_space.stats["corridor"]),
        "plain_corridor": int(default_space.stats["plain_corridor"]),
    }


# ---------------------------------------------------------------------------
# 14. 拘束面・荷重面のアンカーが空のときにメッセージを出す(タスク 20260904-1556)
# ---------------------------------------------------------------------------
def check_empty_condition_face_warning() -> None:
    print(
        "\n[14] 拘束面・荷重面のアンカーが空のときにメッセージが出る"
        "(落とさない — メッセージを出すだけ)"
    )
    from build123d import Box, Pos

    # `check_anchor.py` [6] と同じ形: 40x24x10 の箱に 0.5x0.5x0.5 の小突起。突起の面は
    # 要素サイズ(elements_long_axis=8 → 5mm 前後)に対して小さすぎて空になる。
    tiny = Box(40, 24, 10) + Pos(0, 0, 5.25) * Box(0.5, 0.5, 0.5)
    scaffold_model = vx.voxelize(tiny, 8)
    scaffold_table = an.build_anchors(scaffold_model.soup, scaffold_model.grid)
    empty = scaffold_table.empty_faces()
    big = [f for f in scaffold_table.face_indices() if f not in empty]
    check(
        "足場: 突起の面が空になり、箱本体の面は空にならない",
        len(empty) >= 1 and len(big) >= 2,
        f"空の面 {empty} / 空でない面 {big}",
    )
    if not (empty and len(big) >= 2):
        return
    empty_face = empty[0]
    big_face = big[0]
    scratch_stl = OUTPUT_DIR / "_check_empty_condition_face.stl"

    # --- 14-1: anchor.run_pipeline(荷重面が空) -----------------------------
    out = an.run_pipeline(
        shape=tiny,
        elements_long_axis=8,
        fixed_faces=big_face,
        load_faces=empty_face,
        max_iter=2,
        stl_path=scratch_stl,
    )
    check(
        "14-1 荷重面が空でも run_pipeline は例外にならない(落とさない)",
        out.get("result") is None and out.get("stl") is None,
        f"result={out.get('result')!r} stl={out.get('stl')!r}",
    )
    diag = out.get("empty_condition_faces") or []
    check(
        "14-1 empty_condition_faces に荷重側の診断が1件載る",
        len(diag) == 1 and diag[0]["side"] == "load" and diag[0]["face_index"] == empty_face,
        f"{diag}",
    )
    if diag:
        d = diag[0]
        check(
            "14-1 診断に要素サイズ・CAD面積・代表寸法が正の数で入っている",
            d["element_size_mm"] > 0
            and d["cad_area_mm2"] > 0
            and math.isfinite(d["representative_length_mm"])
            and d["representative_length_mm"] > 0,
            f"element_size_mm={d['element_size_mm']} cad_area_mm2={d['cad_area_mm2']} "
            f"representative_length_mm={d['representative_length_mm']}",
        )
        msg = an.format_condition_face_warning(diag)
        check(
            "14-1 メッセージに面添字と逃げ道2つ(別の面/要素サイズ)が文字として入っている",
            f"面{empty_face}" in msg and "別の" in msg and "要素サイズを" in msg,
            msg,
        )
    RESULTS["empty_condition_face_load"] = diag

    # --- 14-2: 拘束面が空(側の表示が入れ替わることの確認) --------------------
    out_fixed = an.run_pipeline(
        shape=tiny,
        elements_long_axis=8,
        fixed_faces=empty_face,
        load_faces=big_face,
        max_iter=2,
        stl_path=scratch_stl,
    )
    diag_fixed = out_fixed.get("empty_condition_faces") or []
    check(
        "14-2 拘束面が空だと side='fixed' で載る(荷重側と取り違えない)",
        len(diag_fixed) == 1 and diag_fixed[0]["side"] == "fixed"
        and diag_fixed[0]["face_index"] == empty_face,
        f"{diag_fixed}",
    )

    # --- 14-3: design_space.run_topopt からも同じ警告が返り、stats にも載る ---
    cond = ds.Conditions(keep=tiny, fixed_faces=big_face, load_faces=empty_face, obstacle=None)
    space = ds.derive(cond, elements_long_axis=8, margin_ratio=0.1)
    check(
        "足場: derive() の connectivity も空の条件面をスキップと報告する(前提の確認)",
        space.stats.get("connectivity", {}).get("skipped") == "empty_anchor_faces",
        f"{space.stats.get('connectivity')}",
    )
    out_ds = ds.run_topopt(space, max_iter=2, stl_path=scratch_stl)
    check(
        "14-3 design_space.run_topopt も例外にならず、戻り値に診断が載る",
        out_ds.get("result") is None and bool(out_ds.get("empty_condition_faces")),
        f"result={out_ds.get('result')!r} empty_condition_faces={out_ds.get('empty_condition_faces')}",
    )
    check(
        "14-3 DesignSpace.stats にも同じ診断が載る(戻り値を経由しなくても後から見える)",
        space.stats.get("empty_condition_faces_warning") == out_ds.get("empty_condition_faces"),
        f"stats={space.stats.get('empty_condition_faces_warning')}",
    )

    # --- 14-4: 通常経路(空の条件面なし)は影響を受けない(回帰確認) -----------
    normal_out = an.run_pipeline(
        shape=tiny,
        elements_long_axis=8,
        fixed_faces=big[0],
        load_faces=big[1],
        max_iter=2,
        stl_path=scratch_stl,
    )
    check(
        "14-4 空の条件面が無ければ empty_condition_faces は空リストで、通常どおり最後まで解く",
        normal_out.get("empty_condition_faces") == [] and normal_out.get("result") is not None,
        f"empty_condition_faces={normal_out.get('empty_condition_faces')} "
        f"result_is_none={normal_out.get('result') is None}",
    )

    # --- 14-5: 変異検査 — 診断を外すと元の _require_nodes 例外に戻る(検出力) --
    original = an.AnchorTable.condition_face_diagnoses
    an.AnchorTable.condition_face_diagnoses = lambda self, fixed_faces, load_faces: []
    raised = ""
    try:
        an.run_pipeline(
            shape=tiny,
            elements_long_axis=8,
            fixed_faces=big_face,
            load_faces=empty_face,
            max_iter=2,
            stl_path=scratch_stl,
        )
    except ValueError as exc:
        raised = str(exc)
    finally:
        an.AnchorTable.condition_face_diagnoses = original
    check(
        "14-5 変異検査: 空面診断を外すと _require_nodes の元の例外に戻る(検出力の確認)",
        "節点が1つも付いていない" in raised,
        raised[:120] if raised else "例外が出なかった(検出力なし — 診断を外しても壊れたことに気づけない)",
    )

    # --- 14-6: 参照 run(連続値保持)で条件面が空だと AttributeError ではなく
    #           構造化された例外になる ---
    # **`run_topopt` の早期return(`"result"` が `None`)を受ける側は、必ず自分で見てから
    # `.rho` へ触る。** 既定の導出でその位置に居るのは `build_reference` である
    # (`derive_from_reference` は参照が無ければここを通る)。参照 run は本番と同じ格子で
    # 回るので、`cond`(突起の面が荷重)をそのまま渡せば踏む。
    ref_kw = {"elements_long_axis": 8, "margin_ratio": 0.1}

    # 正常系を先に見る。**空の条件面が無いときにガードが誤爆しないこと**を見ておかないと、
    # 「常に上げる」実装でも下の例外の検査は緑になる。
    cond_ok = ds.Conditions(keep=tiny, fixed_faces=big[0], load_faces=big[1], obstacle=None)
    base_ok = ds.derive(cond_ok, **ref_kw)
    # **材料量は保持ソリッドを含む総体積である**(`topopt-absolute-volume-includes-the-keep-region`)。
    # 題材の `tiny` は塊の 46% が保持なので、容量に対する分率で置くと保持を下回って
    # `VolumeTargetError(below_minimum)` になる。達成可能区間の中で取る。
    ev = float(base_ok.grid.element_volume)
    solid_mm3 = (
        int(np.count_nonzero(np.asarray(base_ok.model.keep, dtype=bool) & base_ok.mask)) * ev
    )
    material_ok = solid_mm3 + 0.30 * (base_ok.element_count * ev - solid_mm3)
    normal_reference = ds.build_reference(
        cond_ok, material_mm3=material_ok, max_iter=2, stl_path=scratch_stl, **ref_kw
    )
    check(
        "14-6 正常系: 空の条件面が無ければ build_reference は例外にならず参照を返す",
        normal_reference.rho.size == base_ok.mask.size and normal_reference.order.size > 0,
        f"ρ {normal_reference.rho.size} 要素 / 順位 {normal_reference.order.size} 本 / "
        f"回廊 {normal_reference.corridor} 本",
    )

    raised_type = ""
    raised_faces: list = []
    raised_msg = ""
    try:
        ds.build_reference(cond, material_mm3=material_ok, max_iter=2, stl_path=scratch_stl, **ref_kw)
    except ds.ReferenceRunConditionFacesEmptyError as exc:
        raised_type = type(exc).__name__
        raised_faces = exc.empty_condition_faces
        raised_msg = str(exc)
    except Exception as exc:  # noqa: BLE001 — 想定外の例外型も記録して不合格にする
        raised_type = type(exc).__name__
        raised_msg = str(exc)
    check(
        "14-6 build_reference(参照 run)で条件面が空でも AttributeError にならない",
        raised_type == "ReferenceRunConditionFacesEmptyError",
        f"{raised_type}: {raised_msg[:120]}",
    )
    check(
        "14-6 例外に構造化された診断(empty_condition_faces)が乗っている",
        len(raised_faces) == 1
        and raised_faces[0]["side"] == "load"
        and raised_faces[0]["face_index"] == empty_face,
        f"{raised_faces}",
    )
    check(
        "14-6 文面が参照 run の直し方(要素サイズ)を名指ししている",
        "要素サイズ" in raised_msg and f"面{empty_face}" in raised_msg,
        raised_msg[:150],
    )

    # **入口(`derive_default` = 既定の導出)を通しても同じ例外になる。** 製品の経路は
    # 参照を渡さずにここへ入ることがあり、そのとき参照は `derive_from_reference` の中で作られる。
    default_raised_type = ""
    try:
        ds.derive_default(cond, material_mm3=material_ok, reference_max_iter=2, **ref_kw)
    except ds.ReferenceRunConditionFacesEmptyError as exc:
        default_raised_type = type(exc).__name__
    except Exception as exc:  # noqa: BLE001
        default_raised_type = type(exc).__name__
    check(
        "14-6 入口(derive_default = 既定の導出)を通しても同じ例外になる",
        default_raised_type == "ReferenceRunConditionFacesEmptyError",
        f"{default_raised_type}",
    )

    # --- 14-7: 変異検査 — build_reference のガードを外すと元の AttributeError に戻る ---
    original_run_topopt = ds.run_topopt

    def _strip_empty_condition_faces(*args, **kwargs):
        out = original_run_topopt(*args, **kwargs)
        if "empty_condition_faces" in out:
            out = dict(out)
            out["empty_condition_faces"] = []  # 診断が無かったことにする(ガードを無力化)
        return out

    ds.run_topopt = _strip_empty_condition_faces
    raised_type2 = ""
    try:
        ds.build_reference(cond, material_mm3=material_ok, max_iter=2, stl_path=scratch_stl, **ref_kw)
    except Exception as exc:  # noqa: BLE001
        raised_type2 = type(exc).__name__
    finally:
        ds.run_topopt = original_run_topopt
    check(
        "14-7 変異検査: build_reference のガードを外すと元の AttributeError に戻る(検出力の確認)",
        raised_type2 == "AttributeError",
        f"{raised_type2}(空リストで無力化した場合)",
    )


# ===========================================================================
# 15. 幅つき・全条件の回廊(タスク 20260911-1811)
# ===========================================================================
class _FakeTable:
    """`width_corridor_length` が触る面アンカー表の口だけを持つ足場。

    導出を経由せずに塊と条件を手で置くための道具(検査[10]の `_two_islands` と同じ方針)。
    """

    def __init__(self, masks: dict) -> None:
        self._masks = masks

    def empty_faces(self) -> set:
        return set()

    def element_mask(self, faces, layers: int = 1) -> np.ndarray:
        out = None
        for face in faces:
            m = self._masks[int(face)]
            out = m.copy() if out is None else (out | m)
        return out


def _bridged_blocks(*, thick: bool):
    """左右の塊を**細い橋**(1x1)で繋いだ足場。`thick=True` なら太い橋(5x5)も足す。

    細い橋は 6近傍では立派な道だが、`rmin=2.0` の筆は入らない —— 収縮すると消える。
    **これがこの節で見たい病理そのもの**(塊は繋がっているのに材料が通らない)。
    """
    shape = (28, 11, 11)
    left = np.zeros(shape, dtype=bool)
    left[2:10, 3:8, 3:8] = True
    right = np.zeros(shape, dtype=bool)
    right[18:26, 3:8, 3:8] = True
    thin = np.zeros(shape, dtype=bool)
    thin[10:18, 5:6, 5:6] = True
    wide = np.zeros(shape, dtype=bool)
    wide[10:18, 3:8, 3:8] = True

    mask = left | right | thin | (wide if thick else np.zeros(shape, dtype=bool))
    fixed = np.zeros(shape, dtype=bool)
    fixed[2, 5, 5] = True
    load = np.zeros(shape, dtype=bool)
    load[25, 5, 5] = True
    keep = np.zeros(shape, dtype=bool)
    keep[3:5, 4:7, 4:7] = True  # 保持ソリッドは左側(荷重から遠い側)に置く

    # **順位は「細い橋が先・太い橋が後」に並べる。** 6近傍の回廊は細い橋で満たされるので、
    # 幅つきの回廊がそれより多くの本数を要ることが数で出る。
    def flat(block):
        return np.flatnonzero((block & mask).ravel())

    groups = [flat(left), flat(thin), flat(right)] + ([flat(wide & ~thin)] if thick else [])
    order = np.concatenate(groups)
    grid = vx.Grid(origin=(0.0, 0.0, 0.0), spacing=1.0, shape=shape)
    base = ds.DesignSpace(
        grid=grid,
        mask=mask,
        model=_KeepOnly(keep),
        soup=None,
        conditions=ds.Conditions(keep=None, fixed_faces=0, load_faces=1),
        bbox_min=np.zeros(3),
        bbox_max=np.ones(3),
        margin=0.0,
        margin_ratio=0.0,
    )
    table = _FakeTable({0: fixed, 1: load})
    conditions_mask = fixed | load | keep
    return base, table, order, conditions_mask


class _KeepOnly:
    """`condition_seeds` が見る `VoxelModel` の口(`keep`)だけを持つ足場。"""

    def __init__(self, keep: np.ndarray) -> None:
        self.keep = keep


def check_width_corridor() -> None:
    print("\n[15] 幅つき・全条件の回廊(筆が通る太さで全部が荷重と繋がる)")
    rmin = 2.0

    # --- 15-1 正常系: 太い橋があれば満たせる。6近傍より多くの本数が要る ------
    base, table, order, cond_mask = _bridged_blocks(thick=True)
    plain = ds.corridor_length(order, base, table, cond_mask)
    width, diagnosis = ds.width_corridor_length(order, base, table, cond_mask, rmin=rmin)
    check(
        "15-1 正常系: 太い橋がある塊では幅つき回廊が求まる(幅で測ったと報告する)",
        0 < width <= int(order.size) and diagnosis["width"] is True,
        f"幅つき {width} 本 / 順位 {int(order.size)} 本(6近傍の回廊は {plain} 本)/ "
        f"診断 {diagnosis}",
    )
    check(
        "15-1 幅つきは 6近傍より多くの本数を要る(同じ順位・同じ塊で比べる)",
        width > plain > 0,
        f"幅つき {width} 本 > 6近傍 {plain} 本",
    )
    # **最小性**: 1本足りない順位では満たせない(足りる順位ではちょうどその本数を返す)。
    # 公開の口だけで見るために、順位そのものを切って渡す。
    check(
        "15-1 求まる本数は最小(1本削ると満たせない / 足りる順位ではその本数を返す)",
        ds.width_corridor_length(order[:width], base, table, cond_mask, rmin=rmin)[0] == width
        and _raises(
            ds.width_corridor_length,
            ds.DesignSpaceDisconnectedError,
            order[: width - 1],
            base,
            table,
            cond_mask,
            rmin=rmin,
        ),
        f"{width} 本ちょうどで満たせ、{width - 1} 本では満たせない",
    )

    # --- 15-2 細い橋しかない塊は例外。6近傍では通ることを先に見る ------------
    thin_base, thin_table, thin_order, thin_cond = _bridged_blocks(thick=False)
    plain_report = ds.assert_faces_connected(
        thin_base.mask, thin_table, thin_base.fixed_faces, thin_base.load_faces
    )
    check(
        "15-2 足場: 細い橋だけの塊は 6近傍では1成分(検出力の前提)",
        plain_report["components"] == 1 and plain_report["shared_labels"] == [1],
        f"成分 {plain_report['components']} / 共通 {plain_report['shared_labels']}",
    )
    raised = None
    try:
        ds.width_corridor_length(thin_order, thin_base, thin_table, thin_cond, rmin=rmin)
    except ds.DesignSpaceDisconnectedError as exc:
        raised = exc
    except Exception as exc:  # noqa: BLE001 — 想定外の例外型も記録して不合格にする
        raised = exc
    check(
        "15-2 細い橋だけの塊は DesignSpaceDisconnectedError(型が分かれている)",
        isinstance(raised, ds.DesignSpaceDisconnectedError)
        and not isinstance(raised, ds.DesignSpaceTooSmallError)
        and isinstance(raised, ValueError),
        f"{type(raised).__name__ if raised else '例外が上がらなかった'}",
    )
    check(
        "15-2 構造化診断: 荷重から届かなかった条件を名指ししている",
        raised is not None
        and set(getattr(raised, "unreached", [])) == {"fixed_face_0", "keep_1"},
        f"unreached={getattr(raised, 'unreached', None)}",
    )
    check(
        "15-2 文面が「道ではなく道の太さ」を名指ししている(余白を上げろと誤誘導しない)",
        raised is not None and "太さ" in str(raised) and "margin_ratio" not in str(raised),
        str(raised)[:110] if raised else "",
    )

    # --- 15-3 変異検査: 述語を6近傍だけに戻すと検出できなくなる --------------
    original_erode = ds._erode
    ds._erode = lambda mask, rmin: np.asarray(mask, dtype=bool)  # 収縮しない = 6近傍だけ
    try:
        mutated = ds.width_corridor_length(
            thin_order, thin_base, thin_table, thin_cond, rmin=rmin
        )[0]
        mutated_raised = None
    except ds.DesignSpaceDisconnectedError as exc:
        mutated, mutated_raised = None, exc
    finally:
        ds._erode = original_erode
    check(
        "15-3 変異検査: 収縮(`_erode`)を恒等に差し替えると細い橋を見逃す(検出力の確認)",
        mutated_raised is None and mutated is not None,
        f"{mutated} 本で満たしたことになる"
        if mutated_raised is None
        else "収縮を外しても落ちた(この検査は幅を見ていない)",
    )

    # --- 15-4 解像度そのものが足りないときは別の診断 -------------------------
    # 塊のどこにも筆が入らない配置。**条件の置き方の問題ではない**ので、文面は要素サイズを指す。
    flat_shape = (20, 20, 7)
    flat_mask = np.zeros(flat_shape, dtype=bool)
    flat_mask[:, :, 2:5] = True  # 3要素厚の板。rmin=2.0 の筆はどこにも入らない
    flat_fixed = np.zeros(flat_shape, dtype=bool)
    flat_fixed[1, 1, 3] = True
    flat_load = np.zeros(flat_shape, dtype=bool)
    flat_load[18, 18, 3] = True
    flat_base = ds.DesignSpace(
        grid=vx.Grid(origin=(0.0, 0.0, 0.0), spacing=1.0, shape=flat_shape),
        mask=flat_mask,
        model=_KeepOnly(flat_fixed),
        soup=None,
        conditions=ds.Conditions(keep=None, fixed_faces=0, load_faces=1),
        bbox_min=np.zeros(3),
        bbox_max=np.ones(3),
        margin=0.0,
        margin_ratio=0.0,
    )
    flat_length, flat_diagnosis = ds.width_corridor_length(
        np.flatnonzero(flat_mask.ravel()),
        flat_base,
        _FakeTable({0: flat_fixed, 1: flat_load}),
        flat_fixed | flat_load,
        rmin=rmin,
    )
    # **落とさない。** 収縮が空なのは「ρ が 1 に届くセルが無い」であって「材料が出ない」では
    # ない(メッシュの閾値は 0.5 で、3要素幅でも ρ=0.829 出る)。幅を外した全条件・6近傍の
    # 床へ落ちて、保証が取れなかったことを報告する。
    check(
        "15-4 どこにも筆が入らない解像度では落とさず、幅を外した床と理由を返す",
        flat_diagnosis["width"] is False
        and flat_diagnosis.get("reason") == "rmin_does_not_fit_anywhere"
        and 0 <= flat_length <= int(flat_mask.sum()),
        f"床 {flat_length} 本 / 診断 {flat_diagnosis}",
    )
    # 同じ板で条件を切り離すと、幅を外した述語でもちゃんと落ちる(床が無力化していない)。
    split = flat_mask.copy()
    split[10, :, :] = False
    split_base = dataclasses.replace(flat_base, mask=split)
    check(
        "15-4 幅を外した床でも、条件が別成分に落ちる塊は例外になる",
        _raises(
            ds.width_corridor_length,
            ds.DesignSpaceDisconnectedError,
            np.flatnonzero(split.ravel()),
            split_base,
            _FakeTable({0: flat_fixed, 1: flat_load}),
            flat_fixed | flat_load,
            rmin=rmin,
        ),
        "3要素厚の板を x=10 で断ち切った",
    )
    # **幅を外した側で種を膨張させると、隙間で切れた条件を「届いた」と読む。**
    # 拘束の種を本体から2要素離して置く: 素の種なら別成分、膨張(rmin=2.0)なら芯に触る。
    gapped = np.zeros(flat_shape, dtype=bool)
    gapped[0, 5, 3] = True          # 孤立した1セル(拘束の種)
    gapped[2:, :, 2:5] = True       # 本体(x=2 以降)。x=1 が空いている
    gap_fixed = np.zeros(flat_shape, dtype=bool)
    gap_fixed[0, 5, 3] = True
    gap_load = np.zeros(flat_shape, dtype=bool)
    gap_load[18, 18, 3] = True
    gap_base = dataclasses.replace(flat_base, mask=gapped, model=_KeepOnly(gap_load))
    check(
        "15-4 幅を外した床では種を膨張させない(2要素の隙間で切れた条件を見逃さない)",
        _raises(
            ds.width_corridor_length,
            ds.DesignSpaceDisconnectedError,
            np.flatnonzero(gapped.ravel()),
            gap_base,
            _FakeTable({0: gap_fixed, 1: gap_load}),
            gap_fixed | gap_load,
            rmin=rmin,
        ),
        "拘束の種を本体から2要素離した(膨張を使うと届いたことになる)",
    )

    # --- 15-6 塊に要素を持たない条件は落とし、落とした名前を持ち帰る ----------
    # **除外ソリッドが条件面のアンカーを全部削った配置。** 落とすだけで記録しないと、
    # 「拘束面が1枚だけ塊から消えている」が誰にも要求されないまま無言で通る。
    base2, table2, order2, cond2 = _bridged_blocks(thick=True)
    outside = np.zeros(base2.mask.shape, dtype=bool)
    outside[0, 0, 0] = True  # 塊(x>=2)の外
    table_dropped = _FakeTable(
        {0: table2.element_mask([0]) | outside, 1: table2.element_mask([1]), 2: outside}
    )
    base_dropped = dataclasses.replace(
        base2, conditions=ds.Conditions(keep=None, fixed_faces=(0, 2), load_faces=1)
    )
    length2, diag2 = ds.width_corridor_length(
        order2, base_dropped, table_dropped, cond2, rmin=rmin
    )
    check(
        "15-6 塊に要素を持たない条件面は落とし、名前を診断に残す(未到達には数えない)",
        diag2.get("dropped_seeds") == ["fixed_face_2"] and length2 == width,
        f"落とした種 {diag2.get('dropped_seeds')} / 本数 {length2}(落とさない側は {width})",
    )

    # --- 15-7 幅を測っていない参照は「保証あり」を騙らない --------------------
    # `stats` を持たない参照(手で組んだもの・控えを知らない版の pickle)は既定で False。
    cond15 = ds.demo_conditions(0.0)
    kw15 = {"elements_long_axis": CASE1_LONG_AXIS}
    base15 = ds.derive(cond15, **kw15)
    bare = ds.Reference(
        base=base15,
        rho=np.zeros(base15.mask.shape),
        order=np.flatnonzero(np.asarray(base15.mask).ravel()),
        material_mm3=1.0,
        corridor=0,
        conditions=cond15,
        derive_kwargs=dict(kw15, angular_deflection=vx.DEFAULT_ANGULAR_DEFLECTION),
    )
    # **`pick_ratio=1.0` を明示する。** この参照は ρ が全部 0・順位が素の走査順・回廊 0 の
    # 手組みなので、**切り出す本数を減らすと塊が繋がるかどうかが走査順まかせになる**
    # (既定の摘みで切ると `DesignSpaceDisconnectedError` になる)。見たいのは
    # `corridor_width_guaranteed` の値だけなので、箱を丸ごと取って連結性を題材から外す。
    bare_space = ds.derive_from_reference(
        cond15,
        material_mm3=0.20 * base15.element_count * base15.grid.element_volume,
        reference=bare,
        pick_ratio=1.0,
        **kw15,
    )
    check(
        "15-7 stats の無い参照から切り出した塊は corridor_width_guaranteed が False",
        bare_space.stats["corridor_width_guaranteed"] is False,
        f"{bare_space.stats['corridor_width_guaranteed']!r}",
    )

    # --- 15-5 rmin が層をまたぐ契約として記録・検算される --------------------
    cond = ds.demo_conditions(0.0)
    scratch_stl = OUTPUT_DIR / "_check_width_corridor.stl"
    recorded = ds.derive(cond, elements_long_axis=CASE1_LONG_AXIS, rmin=3.0)
    plain_space = ds.derive(cond, elements_long_axis=CASE1_LONG_AXIS)
    check(
        "15-5 rmin を渡した塊は stats に要素単位で記録される(渡さなければ記録しない)",
        recorded.stats.get("rmin") == 3.0 and "rmin" not in plain_space.stats,
        f"記録 {recorded.stats.get('rmin')!r} / 渡さない側 {plain_space.stats.get('rmin')!r}",
    )
    check(
        "15-5 違う rmin で回そうとすると run_topopt が落とす",
        _raises(
            ds.run_topopt, ValueError, recorded, rmin=2.5, max_iter=1, stl_path=scratch_stl
        ),
        "塊は rmin=3.0 / 回そうとしたのは rmin=2.5",
    )
    # **mm で来ても同じ半径なら通る**(格子の spacing で割ってから比べている)。
    same_mm = 3.0 * float(recorded.grid.spacing)
    ran = ds.run_topopt(recorded, rmin_mm=same_mm, max_iter=1, stl_path=scratch_stl)
    check(
        "15-5 同じ半径を mm で渡した呼び出しは通る(spacing で割ってから比べている)",
        ran.get("result") is not None,
        f"rmin_mm={same_mm:.4g}(= 3.0要素 × {float(recorded.grid.spacing):.4g}mm)",
    )
    check(
        "15-5 記録の無い塊は何も確かめない(案1を素で導出した既存の呼び出しが動く)",
        ds.run_topopt(plain_space, rmin=1.9, max_iter=1, stl_path=scratch_stl).get("result")
        is not None,
        "rmin を記録していない塊に別の rmin を渡しても通る",
    )

    RESULTS["width_corridor"] = {
        "rmin": rmin,
        "plain": int(plain),
        "width": int(width),
        "order": int(order.size),
        "unreached": list(getattr(raised, "unreached", [])),
        "flat_plate": {"length": int(flat_length), **flat_diagnosis},
    }


def main() -> int:
    print("=" * 72)
    print("check_design_space.py : 塊(設計空間)の受入検査")
    print("  [1]-[4] 分離 / [5]-[9] 案1(外接直方体+余白) / [10] 連結性ガード")
    print("  [13] 既定の導出(入口が1つ) / [14] 空の条件面のメッセージ")
    print("  [15] 幅つき・全条件の回廊")
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
        check_default_derivation,
        check_empty_condition_face_warning,
        check_width_corridor,
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

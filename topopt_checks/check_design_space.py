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
    判定を外す(10-2・10-3・10-4 が素通り)。docs/TOPOPT.md「変異検査」に結果がある。

[11] の構成(案5: 粗く1回回して膨らませる。タスク `20260904-1400`):

11. **粗い段の答えから塊を起こす** — 再サンプルが**物理座標**で対応を取る(添字の割り算だと
    別の答えになることを同じ配置で見せる)/ 膨張が **mm** で効く(要素サイズが違う2つの格子で
    層数が変わり物理距離が同じ)/ 案1と同じ型の `DesignSpace` が返り `run_topopt` がそのまま
    通る / 案1より小さい / **4条件の要素が必ず塊に入る**(膨張0でも入る。粗い段から届かない
    条件の要素が実際にあることも見る)/ 除外が引かれている / 膨張を増やすと塊が単調に増える /
    連結性ガードを通っている(**閾値0.99・膨張0では案5の塊が切れて落ちる** —— 案1の箱は同じ
    条件で通るので、案5が自分でガードを通していなければ素通りする)/ 引数の検証。

[13] の構成(既定の導出。タスク `20260904-1416`):

13. **既定が案5で、入口が1つ** — `derive_default` が案5を返す / **`coarse_material_mm3` が
    素通しになっている**(粗い段が実際にその量で解かれたことを achieved で見る。後続の
    安全率の外側ループがこの引数を使う)/ 案1(`derive`)が残っていて既定値も動いていない
    (比較のベースラインであり退避経路)/ `derivation="bbox"` で案1へ戻せる / 未知の名前は
    例外 / **導出にこだわりのない呼び出し側(`perf.py` / `safety_loop.py`)が導出を名指ししていない**
    —— 名指しが1つでも残っていると、次に既定を倒すときにそこだけ取り残される。

[14] の構成(空の条件面のメッセージ。タスク `20260904-1556`):

14. **拘束面・荷重面のアンカーが空でも例外にせず、メッセージが返る** — `anchor.run_pipeline` /
    `design_space.run_topopt` の両方で、荷重側・拘束側それぞれが空のときに
    `empty_condition_faces`(側つきの診断)が戻り値に載ること。診断の数値(要素サイズ・
    CAD面積・代表寸法)とメッセージの文言(面添字・逃げ道2つ)を見る / `design_space` 側は
    `DesignSpace.stats` にも同じ診断が載ること / **通常経路(空の条件面なし)は影響を受けない**
    (回帰確認)/ **変異検査**(空面診断を外すと元の `ValueError` に戻ることを毎回自動で確認。
    docs/TOPOPT.md「変異検査の記録(2026-09-05)」)。
    **14-6/14-7(2026-09-05 QAのfollow-up指摘への対応)**: `derive_from_coarse_run`(案5・
    既定の導出)の**粗い段**は `run_topopt(coarse, ...)` の戻り値の `"result"` を無条件に
    使っていたため、粗い段だけ条件面のアンカーが空になると `AttributeError` になっていた
    (14の早期returnが作った新しい穴)。`CoarseRunConditionFacesEmptyError` を新設して検知し、
    `derive_default`(既定の入口)経由でも同じ例外になることと、変異検査(ガードを外すと
    `AttributeError` に戻ること)を見る。
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import math
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

    # 旧経路(塊=ブラケット)を同じ格子・同じ面で回す。void = ~design なので構造的に0個。
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
    # OCCT の `BRepBndLib::Add`(= `vx._shape_bbox`)は**面のトリムを見ず、下地のサーフェス
    # 全体で囲む**。トーラス面の部品では実寸の10倍以上に化け、ガードが**嘘の理由で**落とす
    # (実測: `models/hanger_bar_attachment.json` の 35mm 角の部品が 482.8mm と報告された)。
    # ここは「その罠が現に存在すること」と「ガードが罠を踏まない口を使っていること」を固定する。
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus

    segment = BRepPrimAPI_MakeTorus(100.0, 10.0, math.pi / 6).Shape()
    loose_lo, loose_hi = vx._shape_bbox(segment)
    tight_lo, tight_hi = vx._shape_bbox_trimmed(segment)
    seg_soup = vx.triangles_from_shape(segment, deviation=0.2)
    seg_points = np.asarray(seg_soup.positions, dtype=float).reshape(-1, 3)
    check(
        "トリムを見ない bbox は実寸より大きい(この罠が現に存在する。無くなったら下の分岐は要らない)",
        bool(np.any(loose_hi > tight_hi + 1e-6) or np.any(loose_lo < tight_lo - 1e-6)),
        f"Add {np.round(loose_hi - loose_lo, 1).tolist()} / "
        f"AddOptimal {np.round(tight_hi - tight_lo, 1).tolist()}",
    )
    check(
        "トリムを見る bbox は三角形分割と一致する(実寸を返している)",
        bool(
            np.all(np.abs(tight_lo - seg_points.min(axis=0)) < 0.2)
            and np.all(np.abs(tight_hi - seg_points.max(axis=0)) < 0.2)
        ),
        f"AddOptimal {np.round(tight_lo, 2).tolist()}..{np.round(tight_hi, 2).tolist()} / "
        f"三角形 {np.round(seg_points.min(axis=0), 2).tolist()}..{np.round(seg_points.max(axis=0), 2).tolist()}",
    )
    # **ソースを読む検査**([13-5] と同じ理由)。上の2つが緑でも、`derive` の呼び先が
    # `_shape_bbox` へ戻れば誤検知は復活する —— そのとき落ちるのはここだけである。
    check(
        "格子外ガードがトリムを見る bbox を呼んでいる(呼び先が戻ると誤検知が復活する)",
        "_shape_bbox_trimmed(vx._unwrap(conditions.keep))" in inspect.getsource(ds.derive),
        "derive のソースに `_shape_bbox_trimmed(vx._unwrap(conditions.keep))` がある",
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
# 案5(粗く1回回して膨らませる): タスク 20260904-1400
# ===========================================================================
CASE5 = bench.DESIGN_SPACE_COARSE


def _case5(**over):
    """検査の題材で案5を1回導出する。大きさは `bench.DESIGN_SPACE_COARSE` が正。"""
    kw = {
        "elements_long_axis": CASE5["elements_long_axis"],
        "coarse_elements_long_axis": CASE5["coarse_elements_long_axis"],
        "coarse_max_iter": CASE5["coarse_max_iter"],
        "dilate_mm": CASE5["dilate_mm"],
    }
    kw.update(over)
    return ds.derive_from_coarse_run(ds.demo_conditions(0.0), **kw)


def check_case5_coarse_run() -> None:
    print("\n[11] 案5: 粗く1回回して膨らませた塊")

    # --- 11-1 再サンプルは物理座標で対応を取る(単体) ----------------------
    # 原点も要素サイズも違う2つの格子。**添字の割り算だと別の答えになる**配置にしてある。
    coarse = vx.Grid(origin=(0.0, 0.0, 0.0), spacing=10.0, shape=(4, 1, 1))
    fine = vx.Grid(origin=(1.0, 0.0, 0.0), spacing=4.0, shape=(8, 1, 1))
    src = np.zeros(coarse.shape, dtype=bool)
    src[1, 0, 0] = True  # 中心 x=15mm(範囲 10..20mm)
    got = ds.resample_nearest(src, coarse, fine)
    # 本番のセル中心は 3,7,11,15,19,23,27,31mm。最も近い粗い中心(5,15,25,35)が 15 なのは 11/15/19。
    want = np.zeros(fine.shape, dtype=bool)
    want[2:5, 0, 0] = True
    check(
        "再サンプルが物理座標で対応する(原点も要素サイズも違う格子)",
        np.array_equal(got, want),
        f"True の添字 {np.flatnonzero(got[:, 0, 0]).tolist()} (期待 [2, 3, 4])",
    )
    # 検出力: 添字の割り算(本番8要素 / 粗い4要素 = 2)だと [2, 3] になる。
    naive = np.zeros(fine.shape, dtype=bool)
    naive[(np.arange(8) // 2) == 1, 0, 0] = True
    check(
        "検出力: 添字の割り算で済ませた場合と答えが違う(この検査が対応の取り方を見ている)",
        not np.array_equal(naive, want),
        f"割り算だと {np.flatnonzero(naive[:, 0, 0]).tolist()}",
    )
    # 粗い格子の外に出た本番セルは塊にしない(端で1列ぶん値を引き伸ばさない)。
    # 粗い格子が覆うのは 0..40mm。本番のセル中心は 32/36/40/44mm なので、44mm の1つだけが
    # 「最も近い粗い中心(35mm)から要素の半分(5mm)より遠い」= 格子の外になる。
    outside = vx.Grid(origin=(30.0, 0.0, 0.0), spacing=4.0, shape=(4, 1, 1))
    src_all = np.ones(coarse.shape, dtype=bool)
    edge = ds.resample_nearest(src_all, coarse, outside)[:, 0, 0]
    check(
        "粗い格子の外へ出た本番セルは塊に入らない(端で値を引き伸ばさない)",
        edge.tolist() == [True, True, True, False],
        f"中心 32/36/40/44mm の結果 {edge.tolist()} (粗い格子が覆うのは 0..40mm)",
    )

    # --- 11-2 膨張は mm(要素サイズが違っても同じ物理距離)------------------
    g_coarse = vx.Grid(origin=(0.0, 0.0, 0.0), spacing=2.0, shape=(11, 1, 1))
    g_fine = vx.Grid(origin=(0.0, 0.0, 0.0), spacing=1.0, shape=(22, 1, 1))
    seed_c = np.zeros(g_coarse.shape, dtype=bool)
    seed_c[5, 0, 0] = True
    seed_f = np.zeros(g_fine.shape, dtype=bool)
    seed_f[11, 0, 0] = True
    grown_c = ds.grow_by_mm(seed_c, g_coarse, 4.0)
    grown_f = ds.grow_by_mm(seed_f, g_fine, 4.0)
    layers_c = int(grown_c.sum() - 1) // 2
    layers_f = int(grown_f.sum() - 1) // 2
    check(
        "膨張は mm で効く(要素 2mm なら 2層 / 要素 1mm なら 4層 = どちらも 4mm)",
        layers_c == 2 and layers_f == 4,
        f"要素2mm → {layers_c}層 / 要素1mm → {layers_f}層",
    )
    check(
        "膨張0は1要素も広げない",
        np.array_equal(ds.grow_by_mm(seed_f, g_fine, 0.0), seed_f),
        f"{int(ds.grow_by_mm(seed_f, g_fine, 0.0).sum())} 要素(種は1要素)",
    )

    # --- 11-3 案1と同じ型・同じ不変条件の DesignSpace が返る ---------------
    space = _case5()
    st = space.stats
    check(
        "案5の入口が DesignSpace を返す(案1と同じ型)",
        isinstance(space, ds.DesignSpace)
        and tuple(space.mask.shape) == tuple(space.grid.shape)
        and st.get("derivation") == "coarse_run",
        f"塊 {space.element_count} 要素 / 格子 {space.grid.shape} / "
        f"spacing {space.grid.spacing:.3f}mm / derivation={st.get('derivation')}",
    )
    check(
        "案1と同じ不変条件: model.obstacle が全 False(除外は塊から引き切っている)",
        not np.asarray(space.model.obstacle).any(),
        f"obstacle {int(np.asarray(space.model.obstacle).sum())} 要素",
    )

    # --- 11-4 run_topopt がそのまま通る(表示側を改造せずに受けられる)------
    stl = OUTPUT_DIR / "check_design_space_case5.stl"
    if stl.exists():
        stl.unlink()
    t0 = time.perf_counter()
    out = ds.run_topopt(space, max_iter=CASE5["max_iter"], volfrac=VOLFRAC, stl_path=stl)
    seconds = time.perf_counter() - t0
    check(
        "案5の塊がそのまま run_topopt を通る(塊 → 面アンカー → SIMP → STL)",
        stl.exists() and stl.stat().st_size > 0 and out["iterations"] == CASE5["max_iter"],
        f"{stl.name} {stl.stat().st_size if stl.exists() else 0} bytes / "
        f"{out['iterations']}反復 / {seconds:.1f}秒 / C={out['compliance_last']:.4f}",
    )

    # --- 11-5 案1より小さい(案5の存在理由)-------------------------------
    check(
        "案5の塊が案1の塊より小さい",
        space.element_count < int(st["case1_elements"]),
        f"案1 {st['case1_elements']} → 案5 {space.element_count} 要素 "
        f"({st['shrink_vs_case1']:.3f}倍)",
    )

    # --- 11-6 4条件の要素が必ず塊に入る ------------------------------------
    keep_outside = int(np.count_nonzero(np.asarray(space.model.keep) & ~space.mask))
    dropped = int(space.model.stats["keep_dropped_outside"])
    check(
        "保持ソリッドの要素が1つも塊から落ちていない",
        keep_outside == 0 and dropped == 0 and int(np.asarray(space.model.keep).sum()) > 0,
        f"はみ出し {keep_outside} 要素 / 格子外 {dropped} 要素 / "
        f"保持 {int(np.asarray(space.model.keep).sum())} 要素",
    )
    table = space.table if space.table is not None else an.build_anchors(space.soup, space.grid)
    anchors = table.element_mask(list(space.fixed_faces) + list(space.load_faces), layers=1)
    check(
        "拘束・荷重のアンカー要素が1つも塊から落ちていない",
        int(np.count_nonzero(anchors & ~space.mask)) == 0 and int(anchors.sum()) > 0,
        f"はみ出し {int(np.count_nonzero(anchors & ~space.mask))} 要素 / "
        f"アンカー {int(anchors.sum())} 要素",
    )
    # 検出力: 膨張を0にすると、**粗い段の材料からは届かない条件の要素**が出る。それでも
    # 上のはみ出しが0のままなら、無条件の OR が実際に効いている証拠になる。
    bare = _case5(dilate_mm=0.0)
    bare_anchors = (
        (bare.table or an.build_anchors(bare.soup, bare.grid))
        .element_mask(list(bare.fixed_faces) + list(bare.load_faces), layers=1)
    )
    check(
        "検出力: 膨張0でも条件の要素は塊に入る(粗い段の材料が届かない要素が実際にある)",
        int(bare.stats["condition_only_elements"]) > 0
        and int(np.count_nonzero(np.asarray(bare.model.keep) & ~bare.mask)) == 0
        and int(np.count_nonzero(bare_anchors & ~bare.mask)) == 0,
        f"粗い段からは入らなかった条件の要素 {bare.stats['condition_only_elements']} 個 / "
        f"はみ出し 保持{int(np.count_nonzero(np.asarray(bare.model.keep) & ~bare.mask))}・"
        f"アンカー{int(np.count_nonzero(bare_anchors & ~bare.mask))}",
    )

    # --- 11-7 除外ソリッドが引かれている ----------------------------------
    obs_soup = vx.triangles_from_shape(
        vx._unwrap(space.conditions.obstacle), deviation=space.stats["deviation"]
    )
    obs, _ = vx.occupancy(obs_soup, space.grid)
    check(
        "除外ソリッドの領域に案5の塊の要素が1つも無い(独立に計算し直して確認)",
        int(np.count_nonzero(obs & space.mask)) == 0 and int(obs.sum()) > 0,
        f"重なり {int(np.count_nonzero(obs & space.mask))} 要素 / 除外 {int(obs.sum())} 要素",
    )

    # --- 11-8 膨張が効いている(単調性)------------------------------------
    counts = {0.0: bare.element_count, CASE5["dilate_mm"]: space.element_count}
    wide = _case5(dilate_mm=CASE5["dilate_mm"] + 4.0)
    counts[CASE5["dilate_mm"] + 4.0] = wide.element_count
    ordered = [counts[k] for k in sorted(counts)]
    check(
        "膨張の量を増やすと塊が単調に大きくなる(膨張が結果を動かしている)",
        ordered[0] < ordered[1] < ordered[2] <= int(st["case1_elements"]),
        f"{{{', '.join(f'{k:g}mm: {counts[k]}' for k in sorted(counts))}}} / "
        f"案1 {st['case1_elements']}",
    )

    # --- 11-9 連結性ガードを通っている ------------------------------------
    report = st.get("connectivity", {})
    check(
        "案5の塊でも拘束と荷重が同じ連結成分にいることを確認してから返している",
        bool(report.get("shared_labels"))
        and report.get("neighbourhood") == ds.NEIGHBOURHOOD
        and int(report.get("fixed_elements", 0)) > 0
        and int(report.get("load_elements", 0)) > 0,
        f"成分 {report.get('components')} 個 / 共通 {report.get('shared_labels')} / "
        f"拘束側 {report.get('fixed_elements')} 要素 / 荷重側 {report.get('load_elements')} 要素",
    )
    # 検出力: 閾値を上げて粗い段の材料をほぼ全部落とすと、**案1の箱は通るのに案5の塊は切れる**。
    # 案5がガードを自分で通していなければ、ここは黙って通ってしまう。
    severed = None
    try:
        _case5(threshold=0.99, dilate_mm=0.0)
    except ds.DesignSpaceDisconnectedError as e:
        severed = e
    check(
        "検出力: 閾値0.99・膨張0で塊が切れると案5も落ちる(案1の箱は同じ条件で通る)",
        severed is not None and not isinstance(severed, ds.DesignSpaceTooSmallError),
        f"{type(severed).__name__ if severed else '例外が上がらなかった'}"
        + (f" / 成分 {severed.components} 個" if severed is not None else ""),
    )

    # --- 11-10 引数の検証 --------------------------------------------------
    cond = ds.demo_conditions(0.0)
    check(
        "閾値が 0 < t < 1 の外なら例外(0 や 1 を黙って受けない)",
        _raises(ds.derive_from_coarse_run, ValueError, cond, threshold=0.0)
        and _raises(ds.derive_from_coarse_run, ValueError, cond, threshold=1.0),
        "threshold=0.0 / 1.0",
    )
    check(
        "膨張が負なら例外",
        _raises(ds.derive_from_coarse_run, ValueError, cond, dilate_mm=-1.0),
        "dilate_mm=-1.0",
    )
    check(
        "粗い段の体積目標は volfrac と material_mm3 の排他(両方渡すと例外)",
        _raises(
            ds.derive_from_coarse_run,
            ValueError,
            cond,
            coarse_volfrac=0.3,
            coarse_material_mm3=1000.0,
        ),
        "coarse_volfrac と coarse_material_mm3 を両方渡した",
    )

    RESULTS["case5"] = {
        "case1_elements": int(st["case1_elements"]),
        "design_elements": space.element_count,
        "shrink_vs_case1": round(float(st["shrink_vs_case1"]), 4),
        "threshold": st["threshold"],
        "dilate_mm": st["dilate_mm"],
        "coarse_seconds": round(float(st["coarse"]["seconds"]), 2),
        "dilate_elements": {f"{k:g}": counts[k] for k in sorted(counts)},
    }


# ---------------------------------------------------------------------------
# 12. 性能の基準線の題材が4つとも端から端まで通る
# ---------------------------------------------------------------------------
FOUR = bench.DESIGN_SPACE_FOUR_CASES


def check_four_benchmark_cases() -> None:
    print("\n[12] 性能の基準線の題材(4つ)が端から端まで通る")

    from duocad.topopt import perf

    # --- 12-1 表が4つあり、①は既存の `demo_conditions` のまま ---------------
    names = list(ds.CASES)
    check(
        "12-1 題材が4つ表に載っている",
        len(names) == 4 and names[0] == "bracket",
        f"{names}",
    )
    check(
        "12-1 ①は既存の demo_conditions をそのまま指している",
        ds.CASES["bracket"].conditions is ds.demo_conditions,
        "足すのであって置き換えではない(既存の検査が demo_conditions に依存している)",
    )

    # --- 12-2〜12-4 4題材とも導出 → ガード → SIMP → STL ---------------------
    measured: dict[str, dict] = {}
    for name in names:
        case = ds.CASES[name]
        # **導出を名指ししない**(`derive_space` は既定の入口を通る)。基準線が既定で
        # 回るのだから、その題材が端から端まで通ることの検査も既定で回す。
        # 粗い段の大きさは `bench.DESIGN_SPACE_FOUR_CASES` が正。
        space = case.derive_space(
            elements_long_axis=FOUR["elements_long_axis"],
            coarse_elements_long_axis=FOUR["coarse_elements_long_axis"],
            coarse_max_iter=FOUR["coarse_max_iter"],
            coarse_volfrac=0.3,
        )
        table = space.table
        empty = sorted(int(f) for f in table.empty_faces())
        conn = space.stats.get("connectivity")
        stl = OUTPUT_DIR / f"check_case_{name}.stl"
        out = ds.run_topopt(
            space,
            volfrac=0.3,
            max_iter=FOUR["max_iter"],
            force=tuple(float(v) for v in case.force),
            stl_path=stl,
        )
        # 拘束アンカーの要素が、塊の上半分で何個の塊に分かれているか(= 支持が何箇所か)。
        # `branching` の U 字面は下の帯で繋がっているので、**上半分に限って**数える。
        fixed_elements = table.element_mask(list(space.fixed_faces), layers=1) & space.mask
        z_centers = space.grid.centers_1d(2)
        upper = np.zeros(space.grid.shape, dtype=bool)
        upper[:, :, z_centers > 0.5 * (z_centers.min() + z_centers.max())] = True
        _, support_clusters = ds.element_components(fixed_elements & upper)

        rho = np.asarray(out["result"].rho)
        # **条件のどのソリッドにも入っていない空間に材料が出ている**か。塊が効いていない
        # (= 保持だけが残った)答えを「通った」と数えないため。
        free_material = int(np.count_nonzero((rho > 0.5) & ~np.asarray(out["solid"], dtype=bool)))
        measured[name] = {
            "grid": list(space.grid.shape),
            "derivation": str(space.stats.get("derivation", "bbox")),
            "design_elements": space.element_count,
            "fixed_dofs": int(table.fixed_dofs(space.fixed_faces).size),
            "load_nodes": sum(int(table[f].node_count) for f in space.load_faces),
            "support_clusters": int(support_clusters),
            "obstacle_removed": int(space.stats.get("obstacle_elements_removed", 0)),
            "components": conn.get("components") if isinstance(conn, dict) else None,
            "shared": len(conn.get("shared_labels", [])) if isinstance(conn, dict) else 0,
            "compliance": float(out["compliance_last"]),
            "free_material": free_material,
            "empty_condition_faces": [
                f for f in empty if f in tuple(space.fixed_faces) + tuple(space.load_faces)
            ],
            "stl_bytes": stl.stat().st_size if stl.exists() else 0,
        }
        m = measured[name]
        check(
            f"12-2 {name}: 連結性ガードを通った",
            isinstance(conn, dict)
            and not conn.get("skipped")
            and int(m["components"] or 0) >= 1
            and m["shared"] >= 1,
            f"成分 {m['components']} / 共通ラベル {m['shared']}",
        )
        check(
            f"12-3 {name}: 条件面のアンカーが空でない",
            not m["empty_condition_faces"] and m["fixed_dofs"] > 0 and m["load_nodes"] > 0,
            f"拘束DOF {m['fixed_dofs']} / 荷重節点 {m['load_nodes']} / 空の条件面 {m['empty_condition_faces']}",
        )
        check(
            f"12-4 {name}: SIMP → STL まで通り、条件の外に材料が出た",
            np.isfinite(m["compliance"]) and m["free_material"] > 0 and m["stl_bytes"] > 0,
            f"C={m['compliance']:.4g} / 条件外の材料 {m['free_material']}要素 / STL {m['stl_bytes']}バイト",
        )

    RESULTS["four_cases"] = measured

    # --- 12-5 4題材が互いに別物である(似たものを3つ作っていない) -------------
    # **「4つ通った」だけでは足りない。** 同じ性質の題材を4つ並べても全部通るので、
    # 覆っている性質が実際に違うことを数字で見る。
    shapes = {name: tuple(m["grid"]) for name, m in measured.items()}
    check(
        "12-5 4題材の格子の形がすべて違う",
        len(set(shapes.values())) == 4,
        f"{shapes}",
    )
    aspect = {
        name: max(m["grid"]) / min(m["grid"]) for name, m in measured.items()
    }
    others = [v for k, v in aspect.items() if k != "plate"]
    check(
        "12-5 薄板だけアスペクト比が突出している",
        aspect["plate"] >= 2.0 * max(others),
        f"plate {aspect['plate']:.2f} / 他 {[round(v, 2) for v in others]}",
    )
    # **支持が本当に2箇所に分かれているか**を、拘束DOFの多寡ではなく連結成分で見る。
    # DOFの数は面積が広いだけでも増えるので、「分岐している」の証拠にならない。
    legs = {name: m["support_clusters"] for name, m in measured.items()}
    check(
        "12-5 多点支持の題材だけ拘束アンカーが2つの脚に分かれている",
        legs["branching"] == 2 and all(v == 1 for k, v in legs.items() if k != "branching"),
        f"{legs}(塊の上半分での6近傍成分数)",
    )
    removed = {name: m["obstacle_removed"] for name, m in measured.items()}
    check(
        "12-5 斜め姿勢の題材の除外が塊を一番大きく削っている",
        removed["tilted"] >= 5 * max(v for k, v in removed.items() if k != "tilted"),
        f"{removed}(削った要素数)",
    )

    # --- 12-6 各題材が「何を覆うために居るか」が実測の記録にある ---------------
    # **この1行が無いと、後で「似ているから減らそう」と言われたときに判断できない。**
    doc = (_REPO_ROOT / "docs" / "TOPOPT.md").read_text(encoding="utf-8")
    missing = [name for name, case in ds.CASES.items() if case.covers not in doc]
    check(
        "12-6 4題材の「覆う性質」がそのまま docs/TOPOPT.md にある",
        not missing,
        f"欠けている題材: {missing}" if missing else "4件とも一致",
    )

    # --- 12-7 性能測定の題材の大きさが受入検査のそれと分かれている -------------
    check(
        "12-7 性能の基準線は2段以上の解像度で測る",
        len(perf.RESOLUTIONS) >= 2,
        f"perf.RESOLUTIONS = {perf.RESOLUTIONS}",
    )
    check(
        "12-7 性能測定の解像度は受入検査の解像度より大きい(巻き込まれていない)",
        min(perf.RESOLUTIONS) > FOUR["elements_long_axis"],
        f"perf {perf.RESOLUTIONS} > 受入 {FOUR['elements_long_axis']}",
    )


# ===========================================================================
# 既定の導出(入口を1つにする): タスク 20260904-1416
# ===========================================================================
def check_default_derivation() -> None:
    print("\n[13] 既定の導出(入口が1つ)")

    cond = ds.demo_conditions(0.0)
    small = {
        "elements_long_axis": CASE5["elements_long_axis"],
        "coarse_elements_long_axis": CASE5["coarse_elements_long_axis"],
        "coarse_max_iter": CASE5["coarse_max_iter"],
        "dilate_mm": CASE5["dilate_mm"],
    }

    # --- 13-1 既定は案5 ----------------------------------------------------
    check(
        "13-1 既定の導出が案5(coarse_run)",
        ds.DEFAULT_DERIVATION == "coarse_run" and ds.DEFAULT_DERIVATION in ds.DERIVATIONS,
        f"DEFAULT_DERIVATION={ds.DEFAULT_DERIVATION!r} / 選べるのは {sorted(ds.DERIVATIONS)}",
    )
    default_space = ds.derive_default(cond, **small)
    check(
        "13-1 入口が返す塊が案5のもの(粗い段の記録が付いている)",
        default_space.stats.get("derivation") == "coarse_run"
        and isinstance(default_space.stats.get("coarse"), dict)
        and default_space.stats["coarse"]["iterations"] >= 1,
        f"derivation={default_space.stats.get('derivation')!r} / "
        f"粗い段 {default_space.stats.get('coarse', {}).get('iterations')} 反復",
    )

    # --- 13-2 案1は残っている(ベースライン兼退避経路) ----------------------
    case1 = ds.derive(cond, elements_long_axis=CASE5["elements_long_axis"])
    check(
        "13-2 案1(derive)が残っていて、案5より大きい塊を返す",
        "coarse" not in case1.stats and case1.element_count > default_space.element_count,
        f"案1 {case1.element_count} 要素 / 案5 {default_space.element_count} 要素",
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
    # **案5の既定値も動かさない**(すべて `20260904-1400` が実測で決めた値)。
    case5_defaults = {
        "coarse_elements_long_axis": ds.COARSE_ELEMENTS_LONG_AXIS,
        "coarse_max_iter": ds.COARSE_MAX_ITER,
        "threshold": ds.COARSE_THRESHOLD,
        "dilate_mm": ds.COARSE_DILATE_MM,
    }
    check(
        "13-2 案5の既定値が動いていない(粗い段 14要素・20反復 / 閾値 0.3 / 膨張 12mm)",
        case5_defaults
        == {
            "coarse_elements_long_axis": 14,
            "coarse_max_iter": 20,
            "threshold": 0.3,
            "dilate_mm": 12.0,
        },
        f"{case5_defaults}",
    )

    # --- 13-3 退避経路と未知の名前 ------------------------------------------
    fallback = ds.derive_default(
        cond, derivation="bbox", elements_long_axis=CASE5["elements_long_axis"]
    )
    check(
        "13-3 derivation='bbox' で案1へ戻せる(退避経路)",
        "coarse" not in fallback.stats and fallback.element_count == case1.element_count,
        f"{fallback.element_count} 要素(案1 {case1.element_count} 要素)",
    )
    check(
        "13-3 知らない導出名は例外(黙って既定へ落ちない)",
        _raises(ds.derive_default, ValueError, cond, derivation="nope"),
        "derivation='nope'",
    )

    # --- 13-4 coarse_material_mm3 が素通しになっている -----------------------
    # **後続(安全率の外側ループ)がこの引数を使う。** 塞ぐと「本番と同じ材料の量で粗い段を
    # 解く」ができなくなり、別の形の周りを膨らませた塊になる。
    coarse_box = ds.derive(cond, elements_long_axis=CASE5["coarse_elements_long_axis"])
    coarse_capacity = coarse_box.element_count * float(coarse_box.grid.spacing) ** 3
    # 既定(volfrac 0.3)と**はっきり違う量**にする —— 同じ量だと素通ししていなくても通る。
    wanted = 0.15 * coarse_capacity
    passed = ds.derive_default(cond, coarse_material_mm3=wanted, **small)
    achieved = float(passed.stats["coarse"]["material_mm3"])
    check(
        "13-4 coarse_material_mm3 が粗い段まで届いている",
        abs(achieved - wanted) / wanted < 0.01,
        f"渡した {wanted:.1f} mm^3 / 粗い段が出した {achieved:.1f} mm^3 "
        f"(既定の volfrac 0.3 なら {default_space.stats['coarse']['material_mm3']:.1f})",
    )
    check(
        "13-4 粗い段の体積目標は排他のまま(両方渡すと例外)",
        _raises(
            ds.derive_default, ValueError, cond, coarse_volfrac=0.3, coarse_material_mm3=1.0
        ),
        "coarse_volfrac と coarse_material_mm3 の同時指定",
    )

    # --- 13-5 呼び出し側が導出を名指ししていない ----------------------------
    # **ソースを読む検査。** 名指しが1つでも残っていると、次に既定を倒したときにそこだけ
    # 取り残される。**読むのは `duocad/topopt/` の中だけ** —— 2026-09-05まで実験場
    # `probe_topopt/server.py` にも同じ不変条件があったが、そこを読むと本体の検査が実験場に
    # 依存するので読まないことにしてあった(実験場は同日に削除した)。
    # **製品の経路 `duocad/optimize_run.py` はここで読んでいない**(既定の入口を通っている
    # ことは `check_optimize_wiring.py` が実際に走らせて見る)。
    for fname in ("perf.py", "safety_loop.py"):
        text = (_REPO_ROOT / "duocad" / "topopt" / fname).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        check(
            f"13-5 {fname} が導出を名指ししていない",
            "ds.derive(" not in code and "ds.derive_from_coarse_run(" not in code,
            f"入口 derive_default/derive_space 経由: {'derive_default' in code or 'derive_space' in code}",
        )
    check(
        "13-5 題材(BenchCase.derive_space)が既定の入口を通る",
        ds.CASES["bracket"]
        .derive_space(
            elements_long_axis=CASE5["elements_long_axis"],
            coarse_elements_long_axis=CASE5["coarse_elements_long_axis"],
            coarse_max_iter=CASE5["coarse_max_iter"],
        )
        .stats.get("derivation")
        == ds.DEFAULT_DERIVATION,
        "derive_space が DEFAULT_DERIVATION の塊を返す",
    )

    RESULTS["default_derivation"] = {
        "name": ds.DEFAULT_DERIVATION,
        "case5_elements": default_space.element_count,
        "case1_elements": case1.element_count,
        "coarse_material_mm3": achieved,
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

    # --- 14-6: derive_from_coarse_run(案5・既定の導出)の粗い段で条件面が空だと
    #           AttributeErrorではなく構造化された例外になる(2026-09-05 QA指摘の修正) ---
    # 粗い段(coarse_elements_long_axis)は本番解像度より要素サイズが大きいので、本番では
    # 空でない条件面が粗い段でだけ空になりうる。ここではどちらも同じ8要素/軸にして
    # 確実に踏む(スカッフォールドで確認済み)。
    raised_type = ""
    raised_faces: list = []
    raised_msg = ""
    try:
        ds.derive_from_coarse_run(
            cond,
            elements_long_axis=8,
            coarse_elements_long_axis=8,
            coarse_max_iter=2,
            margin_ratio=0.1,
        )
    except ds.CoarseRunConditionFacesEmptyError as exc:
        raised_type = type(exc).__name__
        raised_faces = exc.empty_condition_faces
        raised_msg = str(exc)
    except Exception as exc:  # noqa: BLE001 — 想定外の例外型も記録して不合格にする
        raised_type = type(exc).__name__
        raised_msg = str(exc)
    check(
        "14-6 derive_from_coarse_run(案5)の粗い段で条件面が空でも AttributeError にならない",
        raised_type == "CoarseRunConditionFacesEmptyError",
        f"{raised_type}: {raised_msg[:120]}",
    )
    check(
        "14-6 例外に構造化された診断(empty_condition_faces)が乗っている",
        len(raised_faces) == 1
        and raised_faces[0]["side"] == "load"
        and raised_faces[0]["face_index"] == empty_face,
        f"{raised_faces}",
    )

    # 案5が既定の導出(DEFAULT_DERIVATION)なので、derive_default 経由でも同じ例外になることを
    # 実際に確かめる(実務上ここを通るのは derive_default 経由のほうが多い)。
    default_raised_type = ""
    try:
        ds.derive_default(
            cond,
            derivation="coarse_run",
            elements_long_axis=8,
            coarse_elements_long_axis=8,
            coarse_max_iter=2,
            margin_ratio=0.1,
        )
    except ds.CoarseRunConditionFacesEmptyError as exc:
        default_raised_type = type(exc).__name__
    except Exception as exc:  # noqa: BLE001
        default_raised_type = type(exc).__name__
    check(
        "14-6 既定の入口(derive_default, DEFAULT_DERIVATION='coarse_run')を通しても同じ例外になる",
        default_raised_type == "CoarseRunConditionFacesEmptyError",
        f"{default_raised_type}",
    )

    # --- 14-7: 変異検査 — derive_from_coarse_run のガードを外すと元の AttributeError に
    #           戻る(検出力の確認。今回のfollow-up修正そのものの検出力) ---
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
        ds.derive_from_coarse_run(
            cond,
            elements_long_axis=8,
            coarse_elements_long_axis=8,
            coarse_max_iter=2,
            margin_ratio=0.1,
        )
    except Exception as exc:  # noqa: BLE001
        raised_type2 = type(exc).__name__
    finally:
        ds.run_topopt = original_run_topopt
    check(
        "14-7 変異検査: derive_from_coarse_runのガードを外すと元のAttributeErrorに戻る(検出力の確認)",
        raised_type2 == "AttributeError",
        f"{raised_type2}(空リストで無力化した場合)",
    )


def main() -> int:
    print("=" * 72)
    print("check_design_space.py : 塊(設計空間)の受入検査")
    print("  [1]-[4] 分離 / [5]-[9] 案1(外接直方体+余白) / [10] 連結性ガード")
    print("  [11] 案5(粗く1回回して膨らませる) / [12] 性能の基準線の題材(4つ)")
    print("  [13] 既定の導出(入口が1つ) / [14] 空の条件面のメッセージ")
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
        check_case5_coarse_run,
        check_four_benchmark_cases,
        check_default_derivation,
        check_empty_condition_face_warning,
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

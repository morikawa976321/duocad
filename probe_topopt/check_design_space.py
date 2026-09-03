"""塊(設計空間)と面アンカーの対象ソリッドの分離の受入検査。

    ..\\.venv\\Scripts\\python.exe check_design_space.py

**このファイルが合否そのものである。** タスク
`20260903-2216-topopt-split-design-space-from-anchor-soup` の完了条件1〜3を assert する。
どれか1つでも落ちればプロセスは非ゼロで終わる。

検査の構成:

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
"""

from __future__ import annotations

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
import voxelize as vx

FAILURES: list[str] = []
RESULTS: dict[str, object] = {}

# 検査4の格子。60mm のブラケットを一回り大きい箱で包む。**収束ではなく分離を見るので粗い。**
LONG_AXIS = 26
MAX_ITER = 30
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


def main() -> int:
    print("=" * 72)
    print("check_design_space.py : 塊と面アンカーの対象ソリッドの分離の受入検査")
    print("=" * 72)
    check_mask_is_the_design_space()
    check_anchors_independent_of_design()
    check_outside_raises()
    check_material_leaves_the_input_solid()

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

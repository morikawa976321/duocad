"""塊(設計空間)の受入検査。

    .\\.venv\\Scripts\\python.exe topopt_checks\\check_design_space.py
    .\\.venv\\Scripts\\python.exe topopt_checks\\check_design_space.py --run output\\optimization_runs\\<実行ID>

**このファイルが合否そのものである。** どれか1つでも落ちればプロセスは非ゼロで終わる。

- **[1]〜[4]**: 塊と面アンカーの対象ソリッドの分離(`20260903-2216`)。
- **[5]〜[10]**: 固定凸包による導出(`design_space.derive`。2026-09-16)。材料量によらず
  1回で決まる塊の性質と、条件ごとの接続を見る。
- **[11]**: 実物(hanger)の基準実行との突き合わせ。`--run` で実行フォルダを明示したときだけ走る。
- **[14]**: 空の条件面のメッセージ(`20260904-1556`)。

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

[5]〜[10] の構成(固定凸包):

5. **4条件だけで塊が出る** — 設計空間ソリッドを渡す口が型に無いことを見たうえで、
   4条件 → 塊 → 面アンカー → SIMP → marching cubes → STL を通し、**条件のどのソリッドにも
   入っていない空間に材料ができる**ことを見る。余白付き凸包が外枠より小さいこと、最適化が
   塊を書き換えないこと(読み取り専用・ハッシュ不変)も見る。
6. **保持ソリッドが塊に入っている** — はみ出し0要素、保持が1要素以上に表現されていること、
   ボディごとの占有が名前付きで記録されること。除外が保持を食う配置は
   `DesignSpaceConflictError`(入力矛盾を名指し。「余白を上げろ」で誤誘導しない)。
7. **拘束面・荷重面のアンカーが空にならない** — 保持ソリッドから離れた板の面を条件にし、
   その板を `bodies` に入れると凸包がそこまで伸びてアンカーが張れる。
8. **除外ソリッドが引かれている** — 除外の占有を独立に計算し直して重なり0。除外は点群に
   入らない(遠くに足しても外接箱も塊も動かない)。格子と重ならない除外は例外にならない。
9. **幾何** — 支持平面が単位法線で点群を内側に持つ / 塊の全セル中心が押し出した半空間の中 /
   余白 = 比率 × 最長辺 / 余白0でも保持は入る / 負の余白は拒否 / 厚み0の点群は摂動せず落とす /
   外接箱は B-rep から取り、事前の三角形分割で変わらない。
10. **連結性** — 全条件(保持ボディごと・条件面ごと)が個別に共通の成分へ届く。壁で断つと
    `DesignSpaceDisconnectedError`(取り残された条件の名前入り。`DesignSpaceTooSmallError` では
    ない)/ 直接入力の2島は落ちる / 角1点の接触は 6近傍でつながっていない / 塊の中に要素を
    持たない条件は名前付きで落ちる / 条件を含まない成分だけ落ち、条件が分かれたら最大成分を
    選ばず落ちる。

[11] の構成(`--run`):

11. **実物の基準実行** — 実行フォルダの `input.json` から製品と同じ `build_conditions` →
    `derive` を通し、全条件の接続・保持のはみ出し0・前回採用密度の空間外0 mm^3 を見る。
    既知の実行(`HANGER_EXPECTED`)ならセル数・成分・格子形状の再現も見る。

[14] の構成(空の条件面のメッセージ):

14. **拘束面・荷重面のアンカーが空でも例外にせず、メッセージが返る** — `anchor.run_pipeline` /
    `design_space.run_topopt` の両方で、荷重側・拘束側それぞれが空のときに
    `empty_condition_faces`(側つきの診断)が戻り値に載ること。診断の数値と文言を見る /
    `design_space` 側は `DesignSpace.stats` にも同じ診断が載ること / 通常経路は影響を受けない /
    変異検査(空面診断を外すと元の `ValueError` に戻る)。
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
# 固定凸包: 4条件だけから塊を1回で導出する(`design_space.derive`)
# ===========================================================================
# 検査の格子。**収束ではなく導出の性質を見るので粗い。**
CASE1_LONG_AXIS = bench.DESIGN_SPACE_CASE1["elements_long_axis"]
CASE1_MAX_ITER = bench.DESIGN_SPACE_CASE1["max_iter"]
_CASE1: dict = {}


def case1_space():
    """導出を1回だけ回して使い回す(検査5〜10が同じものを見る。製品経路と同じ使い方)。"""
    if "space" not in _CASE1:
        cond = ds.demo_conditions(0.0)
        _CASE1["conditions"] = cond
        _CASE1["space"] = ds.derive(cond, elements_long_axis=CASE1_LONG_AXIS)
    return _CASE1["conditions"], _CASE1["space"]


# ---------------------------------------------------------------------------
# 5. 4条件だけで塊が出て、端から端まで通る
# ---------------------------------------------------------------------------
def check_case1_end_to_end() -> None:
    print("\n[5] 4条件だけ(設計空間ソリッド無し)で塊が出て、端から端まで通る")
    cond, space = case1_space()
    # `Conditions` に設計空間ソリッドを入れる場所が無いことを型で見る。**渡していない**の
    # 根拠を「渡さなかった」という自己申告に置かない。
    field_names = {f.name for f in dataclasses.fields(ds.Conditions)}
    check(
        "条件は4条件 + 荷重の大きさ + ボディの外接箱だけ(設計空間ソリッドを受け取る口が無い)",
        field_names
        == {"keep", "fixed_faces", "load_faces", "obstacle", "anchor_shape", "load_n", "bodies"},
        f"{sorted(field_names)}",
    )
    cells = space.stats["cells"]
    print(
        f"  格子 {space.grid.shape} spacing={space.grid.spacing:.3f}mm / "
        f"外枠 {cells['frame']} → 凸包 {cells['hull']} → 除外後 {cells['minus_obstacle']} → "
        f"接続 {cells['connected']} 要素 / 余白 {space.margin:.2f}mm(比率 {space.margin_ratio}) / "
        f"面 固定={space.fixed_faces} 荷重={space.load_faces}",
        flush=True,
    )
    check(
        "導出方式名が記録され、余白付き凸包は外枠より小さい(外接直方体をそのまま使っていない)",
        space.stats["derivation"] == ds.DERIVATION and cells["hull"] < cells["frame"],
        f"{space.stats['derivation']} / 凸包 {cells['hull']} < 外枠 {cells['frame']}",
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
    check(
        "最適化を回しても塊は書き換わらない(ハッシュ不変・読み取り専用)",
        space.mask_hash == space.stats["mask_hash"] and not space.mask.flags.writeable,
        f"{space.mask_hash[:12]}",
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
    RESULTS["case1_cells"] = dict(cells)
    RESULTS["case1_margin_mm"] = round(space.margin, 3)
    RESULTS["case1_grown_elements"] = int(grown.sum())
    RESULTS["case1_seconds"] = round(seconds, 1)


# ---------------------------------------------------------------------------
# 6. 保持ソリッドが塊に入っている
# ---------------------------------------------------------------------------
def check_case1_keep_inside() -> None:
    print("\n[6] 保持ソリッドが塊に完全に含まれている / 除外との衝突は名前付きで落ちる")
    _cond, space = case1_space()
    outside = int(np.count_nonzero(space.model.keep & ~space.mask))
    dropped = int(space.model.stats["keep_dropped_outside"])
    check(
        "保持のはみ出しが0要素",
        outside == 0 and dropped == 0,
        f"塊の外に出た保持 {outside} 要素 / voxelize が捨てた {dropped} 要素",
    )
    # **「はみ出し0」だけでは足りない。** 解像度が粗すぎて保持が1要素も立たないと、はみ出しも
    # 0になって検査が自明に通る。
    keep_elements = int(np.count_nonzero(space.model.keep))
    check(
        "保持が塊の中で1要素以上に表現されている(はみ出し0が自明に通るのを防ぐ)",
        keep_elements > 0,
        f"{keep_elements} 要素",
    )
    bodies = {item["name"]: item for item in space.stats["bodies"]}
    check(
        "保持ボディごとの占有が名前付きで記録され、どれも0でない",
        set(bodies) == {"fixed_pad", "load_pad"}
        and all(item["keep_elements"] > 0 for item in bodies.values())
        and space.stats["unrepresented_bodies"] == [],
        f"{ {k: v['keep_elements'] for k, v in bodies.items()} }",
    )
    g_lo, g_hi = space.grid.world_box()
    k_lo, k_hi = vx._shape_bbox(vx._unwrap(_cond.keep))
    check(
        "保持ソリッドが格子そのものの内側にある(格子の外は誰も数えていない)",
        bool(np.all(k_lo >= g_lo - 1e-9) and np.all(k_hi <= g_hi + 1e-9)),
        f"保持 {np.round(k_lo, 2).tolist()}..{np.round(k_hi, 2).tolist()} / "
        f"格子 {np.round(g_lo, 2).tolist()}..{np.round(g_hi, 2).tolist()}",
    )
    RESULTS["case1_keep_elements"] = keep_elements

    # **黙って切り詰めないこと。** 除外が保持に重なると、そのセルは CAD の保持と除外が交わって
    # いる。格子で除外を保持で埋め戻すと最終ブーリアン(除外優先)と違う塊を最適化することに
    # なるので、入力矛盾として名前付きで落とす。
    from build123d import Box, Pos

    eater = Pos(-30, 0, 0) * Box(10, 30, 30)  # 固定側パッドを丸ごと覆う除外
    greedy = ds.Conditions(
        keep=_cond.keep,
        fixed_faces=_cond.fixed_faces,
        load_faces=_cond.load_faces,
        obstacle=eater,
        bodies=_cond.bodies,
    )
    raised = None
    try:
        ds.derive(greedy, elements_long_axis=CASE1_LONG_AXIS)
    except ds.DesignSpaceConflictError as e:
        raised = e
    check(
        "除外が保持を食うと DesignSpaceConflictError になる(黙って切り詰めない)",
        raised is not None and raised.bodies.get("fixed_pad", 0) > 0 and "load_pad" not in raised.bodies,
        f"{type(raised).__name__ if raised else '例外が上がらなかった'} / bodies={getattr(raised, 'bodies', None)}",
    )
    if raised is not None:
        check(
            "文面が入力矛盾(CADの保持と除外が交わっている)を名指しする(「余白を上げろ」で誤誘導しない)",
            "入力矛盾" in str(raised) and "margin_ratio" not in str(raised),
            f"{str(raised)[:120]}",
        )


# ---------------------------------------------------------------------------
# 7. 拘束面・荷重面のアンカーが空にならない / 保持以外のボディの条件面も凸包に入る
# ---------------------------------------------------------------------------
def check_case1_anchors_not_empty() -> None:
    print("\n[7] 拘束面・荷重面のアンカーが空にならない")
    _cond, space = case1_space()
    table = space.table
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

    # **条件面は保持ソリッドの外にも置ける。** その面が属するボディの外接箱を `bodies` に
    # 入れると凸包がそこまで伸び、離れた面にもアンカーが張れる。
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
                bodies=_cond.bodies + (ds.condition_body("remote", remote, roles=("fixed",)),),
            ),
            elements_long_axis=CASE1_LONG_AXIS,
        )
        check(
            "保持ソリッドの外にある条件面のボディまで点群の外接箱が伸びている",
            float(far_space.bbox_max[0]) >= 73.0 - 1e-6,
            f"外接箱の x 上限 {float(far_space.bbox_max[0]):.3f}mm "
            f"(保持だけなら 30.0mm、板は 73.0mm)",
        )
        far_table = far_space.table
        check(
            "離れた条件面にもアンカーが張れていて、全条件が1つの成分でつながっている",
            far_space.fixed_faces[0] not in far_table.empty_faces()
            and far_table[far_space.fixed_faces[0]].node_count > 0
            and bool(far_space.stats["connectivity"].get("common_labels")),
            f"{far_table[far_space.fixed_faces[0]].node_count} 節点 / "
            f"{far_space.stats['connectivity'].get('seeds', {}).keys()}",
        )


# ---------------------------------------------------------------------------
# 8. 除外ソリッドが引かれている / 点群には入らない / 塊の外の除外は no-op
# ---------------------------------------------------------------------------
def check_case1_obstacle() -> None:
    print("\n[8] 除外ソリッドが塊から引かれている")
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
        f"除外の占有 {int(obs.sum())} 要素 / うち凸包の中 {removed} 要素",
    )

    # **除外は点群に入らない。** 遠くに除外を足しても外接箱も塊も1要素も動かない。
    far = Pos(300, 0, 0) * Box(20, 20, 20)
    cond_far = ds.Conditions(
        keep=cond.keep,
        fixed_faces=cond.fixed_faces,
        load_faces=cond.load_faces,
        obstacle=cond.obstacle + far,
        bodies=cond.bodies,
    )
    space_far = ds.derive(cond_far, elements_long_axis=CASE1_LONG_AXIS)
    check(
        "遠くの除外を足しても点群の外接箱が変わらない(除外は塊を広げない)",
        bool(
            np.allclose(space_far.bbox_min, space.bbox_min)
            and np.allclose(space_far.bbox_max, space.bbox_max)
        ),
        f"{np.round(space_far.bbox_min, 3).tolist()}..{np.round(space_far.bbox_max, 3).tolist()}",
    )
    check(
        "塊の外の除外はただの no-op(例外にせず、塊も変わらない)",
        bool(np.array_equal(space_far.mask, space.mask)) and space_far.mask_hash == space.mask_hash,
        f"塊 {space_far.element_count} 要素 (元は {space.element_count} 要素)",
    )
    # 格子とまったく重ならない除外だけでも同じ(occupancy の縮退として落とさない)。
    only_far = ds.derive(
        ds.Conditions(
            keep=cond.keep,
            fixed_faces=cond.fixed_faces,
            load_faces=cond.load_faces,
            obstacle=far,
            bodies=cond.bodies,
        ),
        elements_long_axis=CASE1_LONG_AXIS,
    )
    check(
        "格子とまったく重ならない除外は引くものが無いだけで、例外にならない",
        only_far.stats["obstacle_elements_removed"] == 0
        and only_far.stats["obstacle_occupancy"].get("skipped") == "outside_grid",
        f"{only_far.stats['obstacle_occupancy']}",
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
# 9. 幾何: 凸包・余白・外接箱
# ---------------------------------------------------------------------------
def check_case1_geometry() -> None:
    print("\n[9] 幾何: 余白付き凸包の定義どおりに塊ができている")
    cond, space = case1_space()
    planes = space.hull_planes
    check(
        "支持平面の法線は単位長で、`ax+by+cz+d <= 0` が凸包の内側(点群の全頂点が内側)",
        bool(np.allclose(np.linalg.norm(planes[:, :3], axis=1), 1.0))
        and bool(np.all(space.corners @ planes[:, :3].T + planes[:, 3] <= 1e-9)),
        f"{planes.shape[0]} 面 / 点群 {space.corners.shape[0]} 点",
    )
    centers = space.grid.centers_of(np.argwhere(space.mask))
    distance = centers @ planes[:, :3].T + planes[:, 3]
    check(
        "塊の全セル中心は、支持平面を余白ぶん押し出した半空間の内側にある(定義の再計算)",
        bool(np.all(distance <= space.margin + 1e-9)),
        f"最大の超過 {float(distance.max() - space.margin):.3e}mm",
    )
    check(
        "余白 = 比率 × 点群の外接箱の最長辺",
        space.margin
        == pytest_approx(ds.DEFAULT_MARGIN_RATIO * float(np.max(space.bbox_max - space.bbox_min))),
        f"{space.margin:.4f}mm = {ds.DEFAULT_MARGIN_RATIO} × {float(np.max(space.bbox_max - space.bbox_min)):.4f}mm",
    )
    # **余白0でも保持は入る**(凸包は保持の外接箱の頂点から起きているので、頂点を落とさない)。
    zero = ds.derive(cond, margin_ratio=0.0, elements_long_axis=CASE1_LONG_AXIS)
    check(
        "余白0でも保持ははみ出さず、塊は余白付きより小さい",
        int(np.count_nonzero(zero.model.keep & ~zero.mask)) == 0
        and zero.element_count < space.element_count,
        f"余白0 {zero.element_count} 要素 < 既定 {space.element_count} 要素",
    )
    raised = None
    try:
        ds.derive(cond, margin_ratio=-0.05, elements_long_axis=CASE1_LONG_AXIS)
    except ValueError as e:
        raised = e
    check(
        "負の余白は受け付けない(凸包を縮める代替経路は無い)",
        raised is not None and not isinstance(raised, ds.DesignSpaceTooSmallError),
        f"{type(raised).__name__ if raised else '例外が上がらなかった'}",
    )
    # 点群が潰れていれば摂動せずに落ちる(平面状の条件でも有限の厚みがあれば通る)。
    sheet = ds.ConditionBody("sheet", ("fixed",), (0.0, 0.0, 0.0), (10.0, 10.0, 0.0))
    check(
        "厚み0の点群は DesignSpaceGeometryError(ランダム摂動で通さない)",
        _raises(ds.hull_planes, ds.DesignSpaceGeometryError, ds.condition_corners([sheet])),
        "10x10x0 の板",
    )
    plate = ds.ConditionBody("plate", ("fixed",), (0.0, 0.0, 0.0), (10.0, 10.0, 0.5))
    thin_planes, _info = ds.hull_planes(ds.condition_corners([plate]))
    check(
        "有限の厚み(0.5mm)があれば平面状の部品でも凸包が起こせる",
        thin_planes.shape[0] >= 6,
        f"{thin_planes.shape[0]} 面",
    )

    # ---- 外接箱は B-rep から取る(トリムを見ない bbox だとトーラス面の部品が10倍に化ける) ----
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus

    segment = BRepPrimAPI_MakeTorus(100.0, 10.0, math.pi / 6).Shape()
    body = ds.condition_body("segment", segment, roles=("keep",))
    seg_soup = vx.triangles_from_shape(segment, deviation=0.2)
    seg_points = np.asarray(seg_soup.positions, dtype=float).reshape(-1, 3)
    check(
        "条件ボディの外接箱は三角形分割と一致する(実寸を返している)",
        bool(
            np.all(np.abs(np.asarray(body.bbox_min) - seg_points.min(axis=0)) < 0.2)
            and np.all(np.abs(np.asarray(body.bbox_max) - seg_points.max(axis=0)) < 0.2)
        ),
        f"bbox {np.round(body.bbox_min, 2).tolist()}..{np.round(body.bbox_max, 2).tolist()} / "
        f"三角形 {np.round(seg_points.min(axis=0), 2).tolist()}..{np.round(seg_points.max(axis=0), 2).tolist()}",
    )
    again = ds.condition_body("segment", segment, roles=("keep",))
    check(
        "表示用の三角形分割が付いた後でも外接箱は変わらない",
        again.bbox_min == body.bbox_min and again.bbox_max == body.bbox_max,
        f"{again.bbox_min == body.bbox_min}",
    )
    RESULTS["case1_hull_facets"] = int(planes.shape[0])
    RESULTS["case1_margin_zero_elements"] = int(zero.element_count)


def pytest_approx(value: float, rel: float = 1e-9):
    class _Approx:
        def __eq__(self, other: object) -> bool:
            return abs(float(other) - value) <= rel * max(1.0, abs(value))

    return _Approx()


# ===========================================================================
# 10. 連結性: 全条件を個別に見て、条件を含まない成分を落とす
# ===========================================================================
def _seed(shape, *cells) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    for cell in cells:
        mask[cell] = True
    return mask


def check_case1_connectivity() -> None:
    print("\n[10] 連結性: 全条件(保持ボディ・拘束面・荷重面)が個別に同じ成分へ届くか")
    _cond, space = case1_space()

    # --- 10-1 正常系が通る ------------------------------------------------
    report = space.stats.get("connectivity", {})
    seeds = report.get("seeds", {})
    check(
        "正常系で例外が上がらず、全条件が共通の成分を持つ",
        bool(report.get("common_labels"))
        and report.get("neighbourhood") == ds.NEIGHBOURHOOD
        and set(seeds)
        == {
            f"fixed_face_{space.fixed_faces[0]}",
            f"load_face_{space.load_faces[0]}",
            "keep_fixed_pad",
            "keep_load_pad",
        },
        f"components={report.get('components')} common={report.get('common_labels')} seeds={sorted(seeds)}",
    )
    check(
        "検査が自明に通っていない(全条件が塊の中に実際に要素を持つ)",
        all(item["elements"] > 0 for item in seeds.values()),
        f"{ {k: v['elements'] for k, v in seeds.items()} }",
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
            "文面と属性に成分の数と取り残された条件の名前が出る",
            "連結成分は 2 個" in str(raised)
            and raised.components == 2
            and raised.unreached
            and all(name in str(raised) for name in raised.unreached)
            and all(name in raised.seeds for name in raised.unreached),
            f"成分{raised.components} 取り残し={raised.unreached} / {str(raised)[:90]}",
        )
        check(
            "分断は DesignSpaceTooSmallError ではない(余白を上げろと誤誘導しない)",
            not isinstance(raised, ds.DesignSpaceTooSmallError)
            and isinstance(raised, ValueError)
            and "margin_ratio" not in str(raised),
            f"{type(raised).__mro__[1].__name__} を継承",
        )
    check(
        "壁を外すと同じ条件が通る(落とした原因が除外ソリッドの壁であることの裏取り)",
        bool(
            ds.derive(ds.demo_conditions(0.0), elements_long_axis=CASE1_LONG_AXIS).stats[
                "connectivity"
            ]["common_labels"]
        ),
        "壁なしの demo_conditions は共通成分を持つ",
    )

    # --- 10-3 塊を直接2つの島にすると落ちる(導出ロジックを経由しない直接入力) ------
    shape = (10, 6, 6)
    design = np.zeros(shape, dtype=bool)
    design[0:4] = True
    design[6:10] = True  # x=4,5 が空 → 6近傍でも26近傍でも2成分
    islands = {"fixed_face_1": _seed(shape, (0, 0, 0)), "load_face_2": _seed(shape, (9, 5, 5))}
    check(
        "塊を直接2つの島にして渡すと例外",
        _raises(ds.connect_seeds, ds.DesignSpaceDisconnectedError, design, islands),
        f"{shape} の塊を x=4,5 で切った",
    )
    joined = design.copy()
    joined[4:6] = True
    final, ok_report = ds.connect_seeds(joined, islands)
    check(
        "隙間を埋めると同じ入力が通り、塊は1つも減らない",
        ok_report["components"] == 1
        and ok_report["common_labels"] == [1]
        and bool(np.array_equal(final, joined)),
        f"{ok_report['components']} 成分 / 共通 {ok_report['common_labels']}",
    )

    # --- 10-4 6近傍と26近傍の違いが効いている ------------------------------
    corner = np.zeros((8, 8, 8), dtype=bool)
    corner[0:4, 0:4, 0:4] = True
    corner[4:8, 4:8, 4:8] = True
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
            ds.connect_seeds,
            ds.DesignSpaceDisconnectedError,
            corner,
            {"fixed_face_1": _seed((8, 8, 8), (0, 0, 0)), "load_face_2": _seed((8, 8, 8), (7, 7, 7))},
        ),
        f"NEIGHBOURHOOD={ds.NEIGHBOURHOOD}",
    )

    # --- 10-5 塊の中に要素を持たない条件は名前付きで落ちる ------------------
    none_inside = {"fixed_face_1": _seed(shape, (0, 0, 0)), "load_face_2": _seed(shape, (4, 0, 0))}
    raised_none = None
    try:
        ds.connect_seeds(design, none_inside)
    except ds.DesignSpaceDisconnectedError as e:
        raised_none = e
    check(
        "片側の要素が塊の中に1つも無い場合も落ち、その名前が unreached に出る",
        raised_none is not None and raised_none.unreached == ["load_face_2"],
        f"{getattr(raised_none, 'unreached', None)}",
    )

    # --- 10-6 条件を含まない成分だけ落ち、一部の条件しか持たない成分では落ちる ------
    three = np.zeros((12, 4, 4), dtype=bool)
    three[0:3] = True
    three[5:7] = True
    three[9:12] = True
    all_in_a = {
        "fixed_face_1": _seed(three.shape, (0, 0, 0)),
        "load_face_2": _seed(three.shape, (2, 3, 3)),
        "keep_pad": _seed(three.shape, (1, 1, 1)),
    }
    pruned, pruned_report = ds.connect_seeds(three, all_in_a)
    check(
        "条件を含まない成分だけが落ちる(条件のある成分は残る)",
        pruned_report["dropped_components"] == 2
        and int(pruned.sum()) == 3 * 16
        and pruned_report["design_elements"] == 3 * 16,
        f"落ちた成分 {pruned_report['dropped_components']} / 残り {int(pruned.sum())} 要素",
    )
    split = dict(all_in_a, keep_pad=_seed(three.shape, (10, 1, 1)))
    raised_split = None
    try:
        ds.connect_seeds(three, split)
    except ds.DesignSpaceDisconnectedError as e:
        raised_split = e
    check(
        "条件が別々の成分に分かれたら、最大成分を選んで成功にせず取り残しを名指しする",
        raised_split is not None and raised_split.unreached == ["keep_pad"],
        f"{getattr(raised_split, 'unreached', None)}",
    )

    RESULTS["connectivity_neighbourhood"] = int(ds.NEIGHBOURHOOD)
    RESULTS["case1_connectivity"] = {
        "components": report.get("components"),
        "dropped_components": report.get("dropped_components"),
        "seeds": {k: v["elements"] for k, v in seeds.items()},
    }
    if raised is not None:
        RESULTS["severed_components"] = int(raised.components)
        RESULTS["severed_unreached"] = list(raised.unreached)


# ===========================================================================
# 11. 実物(hanger)の基準実行との突き合わせ(`--run <実行フォルダ>` を明示したときだけ)
# ===========================================================================
# 2026-09-16 に採用した案の予備計測(`analysis/convex_domain_proposal.json`)。理由なく更新しない
# —— セル数が違ったら格子・箱・境界判定の差を説明すること(docs/TOPOPT.md「固定凸包」)。
HANGER_EXPECTED = {
    "20260914T133910-1d5adc93ee4d": {
        "connected": 522_424,
        "minus_obstacle": 535_254,
        "component_sizes": [522_424, 12_830],
        "grid_shape": [78, 138, 109],
    }
}


def check_hanger_run(run_dir: str) -> None:
    from pathlib import Path

    from duocad import optimize_run
    from duocad.session import Session
    from duocad.topopt.trace import load_snapshot

    run = Path(run_dir)
    print(f"\n[11] 実物(hanger)の基準実行との突き合わせ: {run.name}")
    session = Session(None, 0.2, models_dir=run)
    session.open_document("input.json")
    resolved = load_snapshot(next(run.glob("*-resolved-conditions.json")))
    spacing = float(resolved["spacing"])
    bundle = optimize_run.build_conditions(session.document)
    t0 = time.perf_counter()
    space = ds.derive(bundle.conditions, spacing=spacing)
    seconds = time.perf_counter() - t0
    st = space.stats
    cells = st["cells"]
    sizes = st["connectivity"]["component_sizes"]
    print(
        f"  余白 {st['margin']:.4f}mm / 格子 {st['grid']['shape']} spacing={st['grid']['spacing']:.6f} / "
        f"外枠 {cells['frame']} → 凸包 {cells['hull']} → 除外後 {cells['minus_obstacle']} → "
        f"接続 {cells['connected']} / 成分 {sizes} / {seconds:.1f}秒",
        flush=True,
    )
    check(
        "保持ボディ4つ・条件面4枚が全部同じ成分に届き、取りこぼしが無い",
        bool(st["connectivity"].get("common_labels"))
        and st["unrepresented_bodies"] == []
        and all(item["elements"] > 0 for item in st["connectivity"]["seeds"].values()),
        f"seeds={ {k: v['elements'] for k, v in st['connectivity']['seeds'].items()} }",
    )
    check(
        "保持のはみ出し0",
        int(np.count_nonzero(space.model.keep & ~space.mask)) == 0,
        f"{int(space.model.keep.sum())} 要素の保持",
    )
    expected = HANGER_EXPECTED.get(run.name)
    if expected is not None:
        check(
            "採用案の予備計測を再現する(接続後のセル数・除外後のセル数・成分・格子形状)",
            cells["connected"] == expected["connected"]
            and cells["minus_obstacle"] == expected["minus_obstacle"]
            and sizes == expected["component_sizes"]
            and st["grid"]["shape"] == expected["grid_shape"],
            f"接続 {cells['connected']} (期待 {expected['connected']}) / 除外後 {cells['minus_obstacle']} "
            f"(期待 {expected['minus_obstacle']}) / 成分 {sizes} (期待 {expected['component_sizes']})",
        )
    surface_inputs = sorted(run.glob("*-surface-input.json"))
    if surface_inputs:
        rho = load_snapshot(surface_inputs[-1])["rho"]
        if rho.shape == space.mask.shape:
            outside_mm3 = float(rho[~space.mask].sum() * space.grid.element_volume)
            check(
                "前回の採用密度のうち空間外の積算量が 0 mm^3(採用案は前回の答えを切っていない)",
                outside_mm3 == 0.0,
                f"{outside_mm3} mm^3 / ρ>0.5 の空間外セル {int(((rho > 0.5) & ~space.mask).sum())}",
            )
    RESULTS["hanger"] = {
        "run": run.name,
        "cells": dict(cells),
        "component_sizes": list(sizes),
        "margin_mm": st["margin"],
        "grid": st["grid"],
        "mask_hash": st["mask_hash"],
        "seconds": round(seconds, 1),
    }


# ===========================================================================
# 14. 拘束面・荷重面のアンカーが空のときにメッセージを出す(落とさない)
# ===========================================================================
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
    cond = ds.Conditions(
        keep=tiny,
        fixed_faces=big_face,
        load_faces=empty_face,
        obstacle=None,
        bodies=(ds.condition_body("tiny", tiny, roles=("keep", "fixed", "load")),),
    )
    space = ds.derive(cond, elements_long_axis=8, margin_ratio=0.1)
    check(
        "足場: derive() の connectivity も空の条件面をスキップと報告する(前提の確認)",
        space.stats.get("connectivity", {}).get("skipped") == "empty_anchor_faces"
        and space.stats["connectivity"].get("empty_faces") == [empty_face],
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


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="塊(設計空間)の受入検査")
    parser.add_argument(
        "--run",
        default=None,
        help="実物(hanger)の実行フォルダ(input.json を含む)。渡したときだけ [11] を走らせる",
    )
    args = parser.parse_args()

    print("=" * 72)
    print("check_design_space.py : 塊(設計空間)の受入検査")
    print("  [1]-[4] 分離 / [5]-[9] 固定凸包(外接箱の頂点の凸包 + 余白) / [10] 連結性")
    print("  [11] 実物の基準実行(--run) / [14] 空の条件面のメッセージ")
    print("=" * 72)
    sections = [
        check_mask_is_the_design_space,
        check_anchors_independent_of_design,
        check_outside_raises,
        check_material_leaves_the_input_solid,
        check_case1_end_to_end,
        check_case1_keep_inside,
        check_case1_anchors_not_empty,
        check_case1_obstacle,
        check_case1_geometry,
        check_case1_connectivity,
        check_empty_condition_face_warning,
    ]
    if args.run:
        sections.append(lambda: check_hanger_run(args.run))
    # **1件が例外で落ちても残りを走らせる。** 実装を壊したときに最初の1件で止まると、
    # 何がどこまで壊れているかが1周では分からない(`check()` と同じ方針を関数の粒度でも取る)。
    for section in sections:
        try:
            section()
        except Exception as exc:  # noqa: BLE001 — 検査中の例外も不合格として記録する
            import traceback

            traceback.print_exc()
            name = getattr(section, "__name__", "check_hanger_run")
            check(f"{name} が例外で落ちた", False, f"{type(exc).__name__}: {exc}")

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

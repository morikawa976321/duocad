"""性能の基準線を測る道具。**受入検査ではない**(合否を出さない)。

    ..\\.venv\\Scripts\\python.exe perf.py --json output\\perf-before.json

`design_space.CASES` の4題材 × 複数解像度を回し、docs/TOPOPT.md の2つの表をそのまま吐く。
マルチグリッド前処理(`20260904-1414`)と CPU 並列化(`20260904-1415`)は**このファイルを
実行して新旧を比べる**。手で測って手で表を書くと、条件が揃っているかを誰も確かめられない。

**`bench.py` と混同しないこと。** あちらは**受入検査**が使う題材の大きさの正で、検証の1周を
速く保つために意図的に小さい。こちらは**性能測定**の題材の大きさの正で、大きさが要る。
同じ数字を両方から引くと、受入検査を速くするたびに性能の基準線が黙って別物になる。

## 何を物差しにするか(基準線の取り方)

秒数はこの機械では熱状態で **1.2〜1.7倍振れる**(engine
`benchmark-seconds-swing-with-thermal-state`)。だから:

1. **`CG反復/solve` が第一の物差し。** 前処理の質だけで決まり、機械の状態に依らない
   (同じコード・同じ題材・同じ解像度なら**何度測っても同じ整数**)。マルチグリッドの効果は
   ここに出る。
2. **秒は「設計反復あたりの中央値」で見る。** 1回の実行の中に設計反復が `--max-iter` 回
   あるので、1回走らせるだけで分布が取れる。**平均ではなく中央値**にしてあるのは、最初の
   1反復だけ極端に遅い(キャッシュが冷えている・`x0=0` から解く)ためで、平均だと反復数を
   変えただけで値が動く。表には最小〜最大も併記してある —— **その幅より小さい差を
   「速くなった」と言わないこと。**
3. **並列化の効果は「秒/設計反復」に出て、`CG反復/solve` には出ない。** 逆にマルチグリッドは
   反復数を減らす。**両方の欄があって初めてどちらが効いたか切り分けられる。**
4. **新旧は同じセッション内で測る。** 別の日に測った秒数どうしを比べない
   (`--baseline` は同じセッションで撮り直した JSON を渡すためにある)。

## 塊の導出は既定(`design_space.derive_default`)を通る

**題材は導出を名指ししない。** 基準線は「既定で回したときにどうなるか」なので、既定が倒れたら
ここも一緒に倒れるのが正しい(2026-09-04に既定は案5になった)。旧い導出の基準線を**同じ
セッションで**撮り直す口が `--derivation bbox`(`--solver cg` と同じ用途)。
**`seconds_derive` は導出の全部**なので、案5では粗い段の SIMP もそこに入る。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

# **Windowsの既定コンソール(cp932)で落ちないようにする。**
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from . import design_space as ds
from . import simp

_HERE = Path(__file__).resolve().parent

# 基準線を取る解像度(最長軸方向の要素数)。**2段以上**でないと伸びの変化が見えない
# (マルチグリッドの効きは格子が細かいほど大きい)。値の決め方は docs/TOPOPT.md「基準線の測り方」。
RESOLUTIONS: tuple[int, ...] = (30, 44)

# 設計反復の回数。**基準線は収束を見ていない** —— 見ているのは1反復の費用と CG の反復数で、
# どちらも数反復で十分に安定する。増やすと測定時間が比例して伸びるだけ。
# **`--max-iter` を変えたら新旧の両方を同じ値で測り直すこと**(1反復目だけ x0=0 から解くので、
# 反復数を変えると中央値も動く)。
MAX_ITER = 8

# 体積目標。**題材ごとに塊の大きさが違うので分率で渡す**(絶対量で揃えると、塊の小さい題材で
# 達成不能になる)。**コンプライアンスを題材どうしで比べてはならない** —— 材料の絶対量も
# 荷重の大きさも題材ごとに違う。比べてよいのは「同じ題材の新旧」だけ。
VOLFRAC = 0.3

# SpMV の実効帯域を測るときの繰り返し回数。1回では計測の分解能に埋もれる。
SPMV_REPEAT = 20


def _model_for_spmv(space: ds.DesignSpace, force) -> tuple[simp.Model, np.ndarray]:
    """SpMV を測るための `Model` と初期密度を組む。

    **`simp.optimize` が内部で足す `extra_fixed_dofs`(空要素にしか接していない浮いた節点)は
    入っていない。** 自由度が数十だけ多くなるが、測っているのは1回の行列ベクトル積が動かす
    バイト数と時間なので、この差は帯域の値に効かない。**表の「拘束DOF」は面アンカーが
    拘束した自由度の数**であって、この差し引きは含まない。
    """
    table = space.table
    if table is None:
        raise ValueError("space.table が無い(導出を通っていない DesignSpace)")
    nx, ny, nz = space.grid.shape
    fixed_dofs = table.fixed_dofs(space.fixed_face)
    load = table.load_vector(space.load_face, force)
    model = simp.Model(nx, ny, nz, fixed_dofs=fixed_dofs, load=load)
    near = table.element_mask([space.fixed_face, space.load_face], layers=1)
    solid = space.model.keep | (near & space.mask)
    void = ~space.mask
    rho = np.full((nx, ny, nz), VOLFRAC)
    rho[void] = 0.0
    rho[solid] = 1.0
    return model, rho


def _spmv_bandwidth(
    model: simp.Model, rho: np.ndarray, repeat: int = SPMV_REPEAT, threads: int = 1
) -> dict:
    """1回の行列ベクトル積の秒数と実効帯域。

    **帯域の定義**: `(行列のバイト数 + x + y) / 秒`。行列は CSC の `data`/`indices`/`indptr`
    をそのまま数える(`data` は float64、`indices` は scipy が選んだ整数型)。x の再読み込みや
    キャッシュヒットは数えない —— **絶対値を他所と比べるための数字ではなく、同じ機械で
    「メモリ律速に張り付いているか」を見るための数字**である。

    `threads>1` は `simp.parallel_matvec` を使う(CG/V-cycle が内部で使うのと同じ行ブロック
    分割)。**CPU並列化のスケーリング曲線(頭打ちがどこで起きるか)はここが素の数字**——
    設計反復1回分の秒数には組立・RAP・splu も混ざるので、SpMV 単体の帯域のほうが
    「並列化した部分だけ」の頭打ちを素直に見せる。
    """
    kg = model.assemble(rho, 3.0)
    kf = kg[model.free, :][:, model.free].tocsc()
    x = np.ones(kf.shape[1])
    if threads > 1:
        kf_csr = kf.tocsr()
        mv = simp._BlockedMatvec(kf_csr, threads)  # noqa: SLF001 — 同じプロセス内の使い切り測定
    else:
        mv = kf.__matmul__
    mv(x)  # 初回はキャッシュを暖めるぶんだけ遅いので捨てる
    best = float("inf")
    for _ in range(int(repeat)):
        t0 = time.perf_counter()
        mv(x)
        best = min(best, time.perf_counter() - t0)
    moved = int(kf.data.nbytes + kf.indices.nbytes + kf.indptr.nbytes + x.nbytes * 2)
    return {
        "rows": int(kf.shape[0]),
        "nnz": int(kf.nnz),
        "bytes": moved,
        "seconds": best,
        "gbps": moved / best / 1e9,
        "threads": int(threads),
    }


def measure_case(
    name: str,
    resolution: int,
    *,
    max_iter: int = MAX_ITER,
    volfrac: float = VOLFRAC,
    spmv_repeat: int = SPMV_REPEAT,
    solver: str | None = None,
    threads: int | None = None,
    derivation: str | None = None,
) -> dict:
    """題材1つ × 解像度1つ。**端から端まで(導出 → 連結性ガード → SIMP → STL)通す。**

    `derivation` は塊の導出の上書き(既定 `None` = `design_space.DEFAULT_DERIVATION`)。
    """
    case = ds.CASES[name]
    resolved = ds.DEFAULT_DERIVATION if derivation is None else str(derivation)
    kwargs: dict = {"elements_long_axis": int(resolution), "derivation": derivation}
    if resolved == "coarse_run":
        # **粗い段の体積目標は本番と同じにする。** 別の量で解くと「別の形の周り」を
        # 膨らませた塊を測ることになる(`derive_from_coarse_run` の docstring)。
        kwargs["coarse_volfrac"] = float(volfrac)
    t0 = time.perf_counter()
    space = case.derive_space(**kwargs)
    derive_seconds = time.perf_counter() - t0

    table = space.table
    empty = sorted(int(f) for f in table.empty_faces()) if table is not None else []
    condition_empty = [f for f in empty if f in (space.fixed_face, space.load_face)]
    if condition_empty:
        # **空の面のまま回すと、条件が消えた別の問題を測ることになる**(しかも最適化は
        # もっともらしく収束する)。基準線としては無価値なので、ここで落とす。
        raise ValueError(
            f"{name} n={resolution}: 条件面のアンカーが空 {condition_empty}"
            "(要素サイズより小さい面には節点が付かない)"
        )
    fixed_dofs = int(table.fixed_dofs(space.fixed_face).size)
    load_nodes = int(table[space.load_face].node_count)

    stl = _HERE / "output" / f"perf_{name}_n{int(resolution)}.stl"
    t0 = time.perf_counter()
    with simp.collect_solve_stats() as stats:
        out = ds.run_topopt(
            space,
            volfrac=float(volfrac),
            max_iter=int(max_iter),
            force=tuple(float(v) for v in case.force),
            stl_path=stl,
            solver=solver,
            threads=threads,
        )
    run_seconds = time.perf_counter() - t0

    history = out["result"].history
    per_iter = [float(h.seconds) for h in history]
    each = [int(v) for v in stats["cg_iterations_each"]]
    if stats["solves"] == 0 or (sum(each) == 0 and stats["direct_solves"] < stats["solves"]):
        # **数え口が外れたら黙って 0 が出る。** 解法を差し替えたタスクがここで気づけるように
        # 落とす(0 のまま表へ出ると「反復が減った」と読めてしまう)。
        raise ValueError(
            f"{name} n={resolution}: CG の反復数が数えられていない"
            f"(solves={stats['solves']} 合計反復={sum(each)})。"
            "解法を差し替えたなら simp._SOLVE_STATS へ積む処理も移すこと"
        )

    model, rho0 = _model_for_spmv(space, case.force)
    spmv = _spmv_bandwidth(
        model, rho0, repeat=spmv_repeat, threads=int(threads) if threads else 1
    )

    grid = space.grid
    solve_seconds = float(stats["solve_seconds"])
    assemble_seconds = float(stats["assemble_seconds"])
    slice_seconds = float(stats["slice_seconds"])
    step_seconds = assemble_seconds + slice_seconds + solve_seconds
    return {
        "case": name,
        "solver": solver or "(既定)",
        "derivation": str(space.stats.get("derivation", "bbox")),
        "threads": int(threads) if threads else int(simp.PARALLEL_THREADS),
        "covers": case.covers,
        "resolution": int(resolution),
        "margin_ratio": float(case.margin_ratio),
        "angle_deg": float(case.angle_deg),
        "force": [float(v) for v in case.force],
        "grid": [int(v) for v in grid.shape],
        "spacing_mm": float(grid.spacing),
        "grid_elements": int(np.prod(grid.shape)),
        "design_elements": int(space.element_count),
        "solid_elements": int(out["solid_elements"]),
        "ndof": int(3 * (grid.shape[0] + 1) * (grid.shape[1] + 1) * (grid.shape[2] + 1)),
        "fixed_dofs": fixed_dofs,
        "load_nodes": load_nodes,
        "obstacle_removed": int(space.stats.get("obstacle_elements_removed", 0)),
        "components": (
            space.stats.get("connectivity", {}).get("components")
            if isinstance(space.stats.get("connectivity"), dict)
            else None
        ),
        "iterations": len(history),
        "cg_iterations_mean": sum(each) / len(each),
        "cg_iterations_first": each[0],
        "cg_iterations_last": each[-1],
        "cg_iterations_each": each,
        "cg_fallbacks": int(stats["fallbacks"]),
        "seconds_per_iteration": statistics.median(per_iter),
        "seconds_per_iteration_min": min(per_iter),
        "seconds_per_iteration_max": max(per_iter),
        "seconds_derive": derive_seconds,
        "seconds_run": run_seconds,
        "seconds_total": derive_seconds + run_seconds,
        "assemble_seconds": assemble_seconds,
        "slice_seconds": slice_seconds,
        "solve_seconds": solve_seconds,
        # マルチグリッドが**密度が変わるたびに**組み直す各段の作用素(RAP)と最粗格子の LU の
        # 費用。`solve_seconds` の内数で、Jacobi 経路では 0。**並列化の当たりを決める数字**
        # (反復側を速くしても、ここが solve の半分なら全体は2倍までしか速くならない)。
        "mg_setup_seconds": float(stats["mg_setup_seconds"]),
        "mg_setup_share": (
            float(stats["mg_setup_seconds"]) / solve_seconds if solve_seconds else 0.0
        ),
        "assemble_share": assemble_seconds / step_seconds,
        "slice_share": slice_seconds / step_seconds,
        "cg_share": solve_seconds / step_seconds,
        "step_share_of_iteration": step_seconds / sum(per_iter),
        "compliance_first": float(out["compliance_first"]),
        "compliance_last": float(out["compliance_last"]),
        "achieved_volfrac": float(out["achieved_volfrac"]),
        "stl": str(stl),
        "spmv": spmv,
    }


def growth_exponents(rows: list[dict]) -> dict[str, dict]:
    """題材ごとの伸びの指数 p(量 ∝ DOF^p)を最小二乗で出す。

    **「速くなった」ではなく「指数がいくつからいくつになったか」を出すための口。**
    前処理を変えると `CG反復/solve` の指数が変わり(対角前処理は DOF^0.41〜0.45、
    格子非依存なら 0)、それがそのまま合計費用の指数に効く。解像度が1点しかない題材は
    `None` を返す(2点以上ないと直線が引けない)。
    """
    out: dict[str, dict] = {}
    for name in dict.fromkeys(r["case"] for r in rows):
        group = sorted((r for r in rows if r["case"] == name), key=lambda r: r["ndof"])
        if len(group) < 2:
            out[name] = {"cg": None, "seconds": None, "points": len(group)}
            continue
        x = np.log([float(r["ndof"]) for r in group])
        out[name] = {
            "cg": float(np.polyfit(x, np.log([r["cg_iterations_mean"] for r in group]), 1)[0]),
            "seconds": float(
                np.polyfit(x, np.log([r["seconds_per_iteration"] for r in group]), 1)[0]
            ),
            "points": len(group),
            "ndof": [int(r["ndof"]) for r in group],
            "cg_iterations": [float(r["cg_iterations_mean"]) for r in group],
        }
    return out


def _fmt_ratio(new: float, old: float | None) -> str:
    if old is None or not old:
        return ""
    return f" ({new / old:.2f}x)"


def print_tables(rows: list[dict], baseline: dict | None = None) -> None:
    """docs/TOPOPT.md にそのまま貼れる2つの表を出す。"""
    base = {}
    if baseline:
        base = {(r["case"], r["resolution"]): r for r in baseline.get("rows", [])}

    print("\n### 基準線\n")
    kinds = sorted({str(r.get("derivation", "bbox")) for r in rows})
    print(f"塊の導出: {', '.join(kinds)}(既定は `design_space.DEFAULT_DERIVATION`)")
    print(
        "| 題材 | 格子 | 要素数(塊/格子) | DOF | 拘束DOF | 荷重節点 | CG反復/solve | "
        "秒/設計反復(中央値) | 最小〜最大 | 合計[秒] | コンプライアンス |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        b = base.get((r["case"], r["resolution"]))
        print(
            f"| {r['case']} n={r['resolution']} "
            f"| {r['grid'][0]}x{r['grid'][1]}x{r['grid'][2]} "
            f"| {r['design_elements']:,} / {r['grid_elements']:,} "
            f"| {r['ndof']:,} | {r['fixed_dofs']:,} | {r['load_nodes']:,} "
            f"| {r['cg_iterations_mean']:.1f}"
            f"{_fmt_ratio(r['cg_iterations_mean'], b['cg_iterations_mean'] if b else None)} "
            f"| {r['seconds_per_iteration']:.3f}"
            f"{_fmt_ratio(r['seconds_per_iteration'], b['seconds_per_iteration'] if b else None)} "
            f"| {r['seconds_per_iteration_min']:.3f}〜{r['seconds_per_iteration_max']:.3f} "
            f"| {r['seconds_total']:.1f} | {r['compliance_last']:.4g} |"
        )

    print("\n### 1回の solve の内訳(本番の全 solve の合計から)\n")
    print(
        "| 題材 | 格子 | 組立 | スライス | CG | うち前処理の組み直し(CG欄の内数) | "
        "solve が設計反復に占める割合 | 非零要素 | SpMV 1回[ms] | 実効帯域[GB/s] |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        s = r["spmv"]
        print(
            f"| {r['case']} n={r['resolution']} "
            f"| {r['grid'][0]}x{r['grid'][1]}x{r['grid'][2]} "
            f"| {r['assemble_share']:.1%} | {r['slice_share']:.1%} | {r['cg_share']:.1%} "
            f"| {r.get('mg_setup_share', 0.0):.1%} "
            f"| {r['step_share_of_iteration']:.1%} "
            f"| {s['nnz']:,} | {s['seconds'] * 1e3:.2f} | {s['gbps']:.1f} |"
        )

    print("\n### 伸びの指数(量 ∝ DOF^p。解像度2点以上の題材だけ)\n")
    now = growth_exponents(rows)
    was = growth_exponents(baseline.get("rows", [])) if baseline else {}
    print("| 題材 | DOF | CG反復/solve | CG反復の指数 p | 秒/設計反復の指数 p |")
    print("|---|---|---|---|---|")
    for name, g in now.items():
        if g["cg"] is None:
            continue
        old_g = was.get(name)
        old_cg = f" (旧 {old_g['cg']:.3f})" if old_g and old_g.get("cg") is not None else ""
        old_sec = (
            f" (旧 {old_g['seconds']:.3f})" if old_g and old_g.get("seconds") is not None else ""
        )
        dofs = " → ".join(f"{v:,}" for v in g["ndof"])
        iters = " → ".join(f"{v:.1f}" for v in g["cg_iterations"])
        print(
            f"| {name} | {dofs} | {iters} | {g['cg']:.3f}{old_cg} | {g['seconds']:.3f}{old_sec} |"
        )

    print("\n### 題材が覆う性質\n")
    print("| 題材 | 余白比率 | 姿勢 | 力 | 覆う性質 |")
    print("|---|---|---|---|---|")
    seen: set[str] = set()
    for r in rows:
        if r["case"] in seen:
            continue
        seen.add(r["case"])
        f = r["force"]
        print(
            f"| {r['case']} | {r['margin_ratio']} | z軸{r['angle_deg']:.0f}° "
            f"| ({f[0]:g}, {f[1]:g}, {f[2]:g}) | {r['covers']} |"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="性能の基準線(4題材 × 複数解像度)")
    parser.add_argument("--cases", nargs="*", default=list(ds.CASES), help="題材名")
    parser.add_argument("--resolutions", nargs="*", type=int, default=list(RESOLUTIONS))
    parser.add_argument("--max-iter", type=int, default=MAX_ITER)
    parser.add_argument("--volfrac", type=float, default=VOLFRAC)
    parser.add_argument("--spmv-repeat", type=int, default=SPMV_REPEAT)
    parser.add_argument(
        "--solver",
        type=str,
        default=None,
        choices=("mg", "cg", "direct"),
        help="解法を上書きする(既定は simp 側の既定 = マルチグリッド)。"
        "**旧い解法の基準線を同じセッションで撮り直すため**の口: --solver cg",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="CG/V-cycle のスレッド数を上書きする(既定は None = simp.PARALLEL_THREADS)。"
        "**スケーリング曲線を撮るための口**: --threads 1 / 2 / 4 / 8 / 14",
    )
    parser.add_argument(
        "--derivation",
        type=str,
        default=None,
        choices=tuple(sorted(ds.DERIVATIONS)),
        help="塊の導出を上書きする(既定は design_space.DEFAULT_DERIVATION)。"
        "**旧い導出の基準線を同じセッションで撮り直すため**の口: --derivation bbox",
    )
    parser.add_argument("--json", type=str, default=None, help="測定値の書き出し先")
    parser.add_argument(
        "--baseline",
        type=str,
        default=None,
        help="以前の --json を渡すと CG反復と秒に倍率を併記する(**同じセッションで撮ったものだけ**)",
    )
    args = parser.parse_args(argv)

    unknown = [c for c in args.cases if c not in ds.CASES]
    if unknown:
        parser.error(f"知らない題材: {unknown}(あるのは {list(ds.CASES)})")

    baseline = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))

    rows: list[dict] = []
    started = time.perf_counter()
    for resolution in args.resolutions:
        for name in args.cases:
            print(f"... {name} n={resolution}", file=sys.stderr, flush=True)
            row = measure_case(
                name,
                resolution,
                max_iter=args.max_iter,
                volfrac=args.volfrac,
                spmv_repeat=args.spmv_repeat,
                solver=args.solver,
                threads=args.threads,
                derivation=args.derivation,
            )
            rows.append(row)
            print(
                f"    塊 {row['design_elements']:,}要素 / CG {row['cg_iterations_mean']:.1f}回"
                f" / {row['seconds_per_iteration']:.3f}秒per反復",
                file=sys.stderr,
                flush=True,
            )

    payload = {
        "solver": args.solver or "(既定)",
        "derivation": args.derivation or ds.DEFAULT_DERIVATION,
        "threads": int(args.threads) if args.threads else int(simp.PARALLEL_THREADS),
        "max_iter": args.max_iter,
        "volfrac": args.volfrac,
        "seconds_wall": time.perf_counter() - started,
        "rows": rows,
    }
    print_tables(rows, baseline)
    print(f"\n合計 {payload['seconds_wall']:.1f}秒 (threads={payload['threads']})")
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

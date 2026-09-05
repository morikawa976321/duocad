"""比較スクショの**重い段** —— 面ごとに最適化を回して三角形メッシュをJSONへ落とす。

**直接は呼ばない。** 呼ぶのは `web/scripts/shoot-topopt.mjs`(撮影台の入口)で、そちらが
1本のコマンドとしてこの段と描画段をまとめる。ここを分けてあるのは、最適化が Python 側
(`duocad.topopt` / `duocad.optimize_run`)にしか無いからであって、手順を増やすためではない。

    python web/scripts/shoot_topopt_export.py --out <データ置き場> \
        --case tilted --panel "old:rmin=1.5,sigma=1.0,iters=12" \
                      --panel "new:rmin=2.5,sigma=0,iters=30"

**2つの題材の出所がある。**

- `--case <name>` —— `duocad.topopt.design_space.CASES` の合成題材
  (`bracket` / `plate` / `branching` / `tilted`)。アルゴリズムの設定
  (`rmin` / `sigma` / `iters` / `n`)を突き合わせるための土俵で、
  `probe_topopt/screenshots/smooth_compare/` の16枚はこれである。
- `--model <ファイル名>` —— **利用者が実際に作った文書**(`--models-dir` 配下の
  操作ログ)。条件は文書に置いてあるものをそのまま使い、製品が実際に持っている
  つまみ(`rmin_mm` / `element_mm`)だけを面ごとに変える。**`sigma` / `iters` は効かない**
  (製品の経路にそのつまみが無い。指定したら明示的に断る —— 黙って無視しない)。

出力は `<out>/panel-<i>.json`(頂点は**mm**)と `<out>/manifest.json`。

**`--case` と `--model` で頂点の原点が違う**(`--case` は格子の中心を原点へ寄せる /
`--model` は文書のワールド座標のまま)。**1回の書き出しは必ずどちらか一方**なので比較の土俵は
崩れないが、**同じ `--out` へ交互に撮ると `_data` が丸ごと上書きされる** ——
`shoot-topopt.mjs` の `--skip-export` は manifest の `subject` を照合して、
別の題材のデータを使い回すのを断る。
**同じ設定の面が2つ以上あれば最適化は1回しか回さない**(σ や Taubin 反復数だけが違う面は、
同じ密度場から2本切るのが正しい)。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

#: 合成題材の既定。`smooth_compare/` の16枚と同じ土俵(README の「題材と方向」)。
DEFAULT_VOLFRAC = 0.3
DEFAULT_MAX_ITER = 40


def parse_panel(spec: str) -> dict:
    """`"名前:rmin=2.5,sigma=0,iters=30,n=40"` を dict にする。

    **ラベルに `:` は使えない**(最初の `:` で切る)。値は `iters` / `n` が整数、
    それ以外は実数。知らないキーは**そのまま持ち回して呼び出し側が断る** ——
    ここで捨てると、綴りを間違えた設定が黙って既定で撮られる。
    """
    label, _, rest = spec.partition(":")
    out: dict = {"label": label}
    for part in rest.split(","):
        if not part.strip():
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        out[key] = int(value) if key in ("iters", "n") else float(value)
    return out


def _encode(verts: np.ndarray, faces: np.ndarray) -> dict:
    return {
        "verts_b64": base64.b64encode(verts.astype(np.float32).tobytes()).decode("ascii"),
        "faces_b64": base64.b64encode(faces.astype(np.uint32).tobytes()).decode("ascii"),
        "n_verts": int(verts.shape[0]),
        "n_faces": int(faces.shape[0]),
    }


# --------------------------------------------------------------------------------------
# 合成題材(`design_space.CASES`)
# --------------------------------------------------------------------------------------
def export_case(case: str, panels: list[dict], out: Path) -> list[dict]:
    from duocad.topopt import design_space as ds
    from duocad.topopt import mesh

    if case not in ds.CASES:
        raise SystemExit(f"題材 {case!r} が無い(あるのは {', '.join(sorted(ds.CASES))})")

    known = {"label", "rmin", "sigma", "iters", "n"}
    runs: dict = {}
    written: list[dict] = []
    for index, panel in enumerate(panels):
        unknown = set(panel) - known
        if unknown:
            raise SystemExit(f"--case では使えないつまみ: {', '.join(sorted(unknown))}")
        rmin = panel.get("rmin")
        sigma = float(panel.get("sigma", 0.0))
        iters = int(panel.get("iters", mesh.DEFAULT_ITERS))
        n_long = int(panel.get("n", ds.DEFAULT_ELEMENTS_LONG_AXIS))

        started = time.perf_counter()
        # **同じ `rmin` と解像度なら最適化を回し直さない。** `rmin` は最適化そのものを
        # 変えるので1つの密度場から切り分けられないが、σ と反復数はメッシュ化だけの話で、
        # 同じ密度場から2本切るのが正しい(別々に回すと「別の最適化の結果」になる)。
        key = (float(-1.0 if rmin is None else rmin), n_long)
        if key not in runs:
            space = ds.CASES[case].derive_space(
                elements_long_axis=n_long,
                coarse_volfrac=DEFAULT_VOLFRAC,
                coarse_stl_path=out / "_coarse.stl",
            )
            result = ds.run_topopt(
                space,
                volfrac=DEFAULT_VOLFRAC,
                rmin=rmin,
                max_iter=DEFAULT_MAX_ITER,
                force=tuple(float(v) for v in ds.CASES[case].force),
                stl_path=out / f"_panel-{index}.stl",
            )
            runs[key] = (space, result)
        space, result = runs[key]

        rho = result["result"].rho
        element_mm = float(space.grid.element_volume ** (1.0 / 3.0))
        surface = mesh.build_surface(
            rho,
            smooth_sigma=sigma,
            n_iter=iters,
            keep_mask=result["solid"],
            zero_mask=result["void"],
        )
        verts = (surface.verts_smooth * element_mm).astype(np.float32)
        # **格子の中心を原点へ寄せる**(材料の重心で寄せると面ごとに別の場所を向く)。
        centre = (np.array(rho.shape, dtype=np.float64) - 1.0) * 0.5 * element_mm
        verts = (verts - centre).astype(np.float32)

        payload = dict(
            {
                "index": index,
                "label": panel["label"],
                "subject": case,
                "element_mm": element_mm,
                "grid": [int(v) for v in rho.shape],
                "rmin_elem": float(result["result"].rmin),
                "rmin_mm": float(result["result"].rmin_mm),
                "sigma": sigma,
                "iters": iters,
                "seconds": time.perf_counter() - started,
            },
            **_encode(verts, surface.faces),
        )
        (out / f"panel-{index}.json").write_text(json.dumps(payload), encoding="utf-8")
        written.append(payload)
        print(
            f"  panel-{index} [{panel['label']}]: {payload['n_faces']} 三角形 "
            f"({payload['seconds']:.1f}秒)",
            flush=True,
        )
    return written


# --------------------------------------------------------------------------------------
# 利用者の文書(`--models-dir` 配下の操作ログ)
# --------------------------------------------------------------------------------------
def export_model(name: str, models_dir: Path, panels: list[dict], out: Path) -> list[dict]:
    """**製品の経路をそのまま通す**(`optimize_run.run`)。

    ここが合成題材と決定的に違うのは、**塊も条件も文書から来る**ことである ——
    「実物の部品で変な答えが出た」を撮れないなら、撮影台を本体へ移した意味が無い。
    """
    from duocad.server import Session

    known = {"label", "rmin_mm", "element_mm"}
    written: list[dict] = []
    for index, panel in enumerate(panels):
        unknown = set(panel) - known
        if unknown:
            raise SystemExit(
                f"--model で使えないつまみ: {', '.join(sorted(unknown))}"
                "(製品の経路には σ と Taubin 反復数のつまみが無い。"
                "それを振りたいなら --case を使うこと)"
            )
        started = time.perf_counter()
        # **面ごとに文書を開き直す。** 前の面が足した結果ボディが次の面の条件に混ざる
        # (保持ソリッドの体積が変わる)のを防ぐ —— 同じ土俵で比べられなくなる。
        session = Session(None, 0.2, models_dir=models_dir)
        session.open_document(name)
        report = session.optimize_conditions()
        if not report["ready"]:
            raise SystemExit(
                f"文書 {name!r} は条件がそろっていない:\n  " + "\n  ".join(report["problems"])
            )
        result = session.run_optimization(
            rmin_mm=panel.get("rmin_mm"),
            element_size_mm=panel.get("element_mm"),
        )
        if result["state"] != "done":
            raise SystemExit(f"panel-{index} [{panel['label']}]: {result['warning']}")
        body = session.document.get_body(result["body_id"])
        verts = np.asarray(body.mesh.positions, dtype=np.float32).reshape(-1, 3)
        faces = np.asarray(body.mesh.indices, dtype=np.uint32).reshape(-1, 3)

        payload = dict(
            {
                "index": index,
                "label": panel["label"],
                "subject": name,
                "element_mm": float(result["element_size_mm"]),
                "grid": [int(v) for v in result["grid"]["shape"]],
                "rmin_elem": None,
                "rmin_mm": None if panel.get("rmin_mm") is None else float(panel["rmin_mm"]),
                "sigma": None,
                "iters": None,
                "safety_factor": result["safety_factor"],
                "seconds": time.perf_counter() - started,
            },
            **_encode(verts, faces),
        )
        (out / f"panel-{index}.json").write_text(json.dumps(payload), encoding="utf-8")
        written.append(payload)
        print(
            f"  panel-{index} [{panel['label']}]: {payload['n_faces']} 三角形 "
            f"({payload['seconds']:.1f}秒)",
            flush=True,
        )
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--case", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--models-dir", default=None)
    ap.add_argument("--panel", action="append", default=[])
    args = ap.parse_args()

    if (args.case is None) == (args.model is None):
        raise SystemExit("--case か --model のどちらか一方を指定すること")
    if len(args.panel) < 2:
        raise SystemExit("--panel は2つ以上(左右に並べるための最小)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    panels = [parse_panel(spec) for spec in args.panel]

    if args.case is not None:
        written = export_case(args.case, panels, out)
        subject = args.case
    else:
        models_dir = Path(args.models_dir) if args.models_dir else None
        if models_dir is None:
            from duocad.paths import DEFAULT_MODELS_DIR

            models_dir = DEFAULT_MODELS_DIR
        written = export_model(args.model, models_dir, panels, out)
        # **`.json` を落とす**(呼び出し側の `--skip-export` の照合と揃える)。
        subject = args.model[:-5] if args.model.lower().endswith(".json") else args.model

    manifest = {
        "subject": subject,
        "kind": "case" if args.case is not None else "model",
        "panels": [
            {key: value for key, value in payload.items() if not key.endswith("_b64")}
            for payload in written
        ],
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

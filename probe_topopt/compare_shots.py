"""密度フィルタ/平滑化の設定を2つ並べた比較スクリーンショットを作る。

`probe_topopt/screenshots/smooth_compare/` の16枚(4題材 × 4方向)を撮り直すための道具。
**2026-09-05に `20260904-1650` が使った使い捨てスクリプトを、再現できるようにコミット
したもの**(前回は撮り直しの手順が散文でしか残らず、書き直しが要った)。

3段に分かれている。段ごとに独立に走らせられる(重い段をやり直さずに済む):

    python -m probe_topopt.compare_shots export --left "old:rmin=1.5,sigma=1.0,iters=12" \
                                                --right "new:rmin=2.5,sigma=0,iters=12"
    node probe_topopt/compare_shots.mjs
    python -m probe_topopt.compare_shots join

- **export** — 4題材を左右それぞれの設定で実際に最適化し、できた三角形メッシュを
  `screenshots/smooth_compare/_data/<題材>_<左右>.json` へ書く(頂点は **mm**。
  解像度が違う設定どうしでも同じ土俵で並ぶようにするため)。**左右で最適化を2回回す**
  —— `rmin` は最適化そのものを変えるので、1つの密度場から2本の面を切ることはできない
  (`20260904-1650` の前後比較は σ だけを変えたので1つの密度場で足りた)。
- **render**(mjs) — 固定カメラ・固定光源で1枚ずつPNGにする。
- **join** — 左右を1枚に並べてラベルを焼き、`<題材>_<方向>.png` として保存する。

**カメラ・光源は `probe_topopt/web/index.html` の既定表示と同じ値**(`compare_shots.mjs`
の先頭に定数として書いてある)。左右の画角は**題材ごとに両方の外接箱を合わせてから**
決めるので、左右で1画素もずれない。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from duocad.topopt import design_space as ds  # noqa: E402
from duocad.topopt import mesh  # noqa: E402

SHOTS = _HERE / "screenshots" / "smooth_compare"
DATA = SHOTS / "_data"
CASES = ["bracket", "plate", "branching", "tilted"]
VIEWS = ["iso", "side", "threeq", "zoom"]
VOLFRAC = 0.3
MAX_ITER = 40


def parse_variant(spec: str) -> dict:
    """`"名前:rmin=2.5,sigma=0,iters=12,n=40"` を dict にする。"""
    label, _, rest = spec.partition(":")
    out = {"label": label, "rmin": None, "sigma": 0.0, "iters": mesh.DEFAULT_ITERS,
           "n": ds.DEFAULT_ELEMENTS_LONG_AXIS}
    for part in rest.split(","):
        if not part.strip():
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        if k == "iters" or k == "n":
            out[k] = int(v)
        else:
            out[k] = float(v)
    return out


def export(variants: dict[str, dict], cases: list[str]) -> None:
    """`variants` は `{"left": {...}, "mid": {...}, "right": {...}}`(mid は省略可)。

    **同じ `rmin` と解像度の面が2つ以上あれば最適化は1回しか回さない**(σ や Taubin 反復数
    だけが違う面は、同じ密度場から2本切るのが正しい)。
    """
    DATA.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"cases": cases, "views": VIEWS, "variants": variants}
    for name in cases:
        runs: dict = {}
        for side, var in variants.items():
            t0 = time.perf_counter()
            key = (float(var["rmin"] or -1.0), int(var["n"]))
            if key not in runs:
                space = ds.CASES[name].derive_space(
                    elements_long_axis=int(var["n"]),
                    coarse_volfrac=VOLFRAC,
                    coarse_stl_path=DATA / "_coarse.stl",
                )
                out = ds.run_topopt(
                    space,
                    volfrac=VOLFRAC,
                    rmin=var["rmin"],
                    max_iter=MAX_ITER,
                    force=tuple(float(v) for v in ds.CASES[name].force),
                    stl_path=DATA / f"{name}_{side}.stl",
                )
                runs[key] = (space, out)
            space, out = runs[key]
            rho = out["result"].rho
            elem_mm = float(space.grid.element_volume ** (1.0 / 3.0))
            mr = mesh.build_surface(
                rho,
                smooth_sigma=var["sigma"],
                n_iter=int(var["iters"]),
                keep_mask=out["solid"],
                zero_mask=out["void"],
            )
            verts = (mr.verts_smooth * elem_mm).astype(np.float32)
            # 題材ごとの中心を揃えるため、左右とも**格子の中心**を原点へ寄せる
            # (材料の重心で寄せると左右で別の場所を向く)。
            center = (np.array(rho.shape, dtype=np.float64) - 1.0) * 0.5 * elem_mm
            verts = (verts - center).astype(np.float32)
            payload = {
                "case": name,
                "side": side,
                "label": var["label"],
                "element_mm": elem_mm,
                "grid": [int(v) for v in rho.shape],
                "rmin_elem": float(out["result"].rmin),
                "rmin_mm": float(out["result"].rmin_mm),
                "sigma": float(var["sigma"]),
                "iters": int(var["iters"]),
                "compliance": float(out["result"].history[-1].compliance),
                "verts_b64": base64.b64encode(verts.tobytes()).decode("ascii"),
                "faces_b64": base64.b64encode(mr.faces.astype(np.uint32).tobytes()).decode("ascii"),
                "n_verts": int(verts.shape[0]),
                "n_faces": int(mr.faces.shape[0]),
            }
            (DATA / f"{name}_{side}.json").write_text(json.dumps(payload), encoding="utf-8")
            print(f"  {name}/{side}: {payload['n_faces']} 三角形 "
                  f"({time.perf_counter() - t0:.1f}秒)", flush=True)
    (DATA / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                        encoding="utf-8")


def join(cases: list[str]) -> None:
    from PIL import Image, ImageDraw

    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    sides = [s for s in ("left", "mid", "right") if s in manifest["variants"]]
    for name in cases:
        for view in VIEWS:
            imgs = [Image.open(DATA / f"{name}_{view}_{s}.png").convert("RGB") for s in sides]
            w, h = imgs[0].size
            band = 34
            gap = 6
            canvas = Image.new("RGB", (w * len(imgs) + gap * (len(imgs) - 1), h + band),
                               (10, 10, 18))
            d = ImageDraw.Draw(canvas)
            for i, (img, side) in enumerate(zip(imgs, sides)):
                x = i * (w + gap)
                canvas.paste(img, (x, band))
                d.text((x + 10, 10), f"{side.upper()}  {manifest['variants'][side]['label']}",
                       fill=(230, 230, 240))
            canvas.save(SHOTS / f"{name}_{view}.png")
            print(f"  {name}_{view}.png", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=["export", "join"])
    ap.add_argument("--left", default="before:rmin=1.5,sigma=1.0,iters=12")
    ap.add_argument("--mid", default="")
    ap.add_argument("--right", default="after:rmin=2.5,sigma=0,iters=30")
    ap.add_argument("--cases", default=",".join(CASES))
    args = ap.parse_args()
    cases = [c for c in args.cases.split(",") if c]
    variants = {"left": parse_variant(args.left)}
    if args.mid:
        variants["mid"] = parse_variant(args.mid)
    variants["right"] = parse_variant(args.right)
    if args.stage == "export":
        export(variants, cases)
    else:
        join(cases)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""比較スクショの**重い段** —— 面ごとに最適化を回して三角形メッシュをJSONへ落とす。

**直接は呼ばない。** 呼ぶのは `web/scripts/shoot-topopt.mjs`(撮影台の入口)で、そちらが
1本のコマンドとしてこの段と描画段をまとめる。ここを分けてあるのは、最適化が Python 側
(`duocad.topopt` / `duocad.optimize_run`)にしか無いからであって、手順を増やすためではない。

    python web/scripts/shoot_topopt_export.py --out <データ置き場> \
        --model hanger_bar_attachment.json --panel "既定:" --panel "細かく:element_mm=2.5"

題材は`--model <ファイル名>` —— **利用者が実際に作った文書**(`--models-dir` 配下の
操作ログ)。条件は文書に置いてあるものをそのまま使い、製品が実際に持っている
つまみ(`element_mm`)だけを面ごとに変える。

出力は `<out>/panel-<i>.json`(頂点は**mm**)と `<out>/manifest.json`。

**同じ `--out` へ別の文書を撮ると `_data` が丸ごと上書きされる** ——
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

def parse_panel(spec: str) -> dict:
    """`"名前:element_mm=2.5"` を dict にする。

    **ラベルに `:` は使えない**(最初の `:` で切る)。値は実数。知らないキーは
    **そのまま持ち回して呼び出し側が断る** —— ここで捨てると、綴りを間違えた設定が
    黙って既定で撮られる。
    """
    label, _, rest = spec.partition(":")
    out: dict = {"label": label}
    for part in rest.split(","):
        if not part.strip():
            continue
        key, _, value = part.partition("=")
        out[key.strip()] = float(value)
    return out


def _encode(verts: np.ndarray, faces: np.ndarray) -> dict:
    return {
        "verts_b64": base64.b64encode(verts.astype(np.float32).tobytes()).decode("ascii"),
        "faces_b64": base64.b64encode(faces.astype(np.uint32).tobytes()).decode("ascii"),
        "n_verts": int(verts.shape[0]),
        "n_faces": int(faces.shape[0]),
    }


# --------------------------------------------------------------------------------------
# 利用者の文書(`--models-dir` 配下の操作ログ)
# --------------------------------------------------------------------------------------
def export_model(name: str, models_dir: Path, panels: list[dict], out: Path) -> list[dict]:
    """**製品の経路をそのまま通す**(`optimize_run.run`)。

    ここが合成題材と決定的に違うのは、**塊も条件も文書から来る**ことである ——
    「実物の部品で変な答えが出た」を撮れないなら、撮影台を本体へ移した意味が無い。
    """
    from duocad.server import Session

    known = {"label", "element_mm"}
    written: list[dict] = []
    for index, panel in enumerate(panels):
        unknown = set(panel) - known
        if unknown:
            raise SystemExit(
                f"--model で使えないつまみ: {', '.join(sorted(unknown))}"
                "(製品の経路には二値探索の内部パラメータのつまみが無い)"
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
        result = session.run_optimization(element_size_mm=panel.get("element_mm"))
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
    ap.add_argument("--model", required=True)
    ap.add_argument("--models-dir", default=None)
    ap.add_argument("--panel", action="append", default=[])
    args = ap.parse_args()

    if len(args.panel) < 2:
        raise SystemExit("--panel は2つ以上(左右に並べるための最小)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    panels = [parse_panel(spec) for spec in args.panel]

    models_dir = Path(args.models_dir) if args.models_dir else None
    if models_dir is None:
        from duocad.paths import DEFAULT_MODELS_DIR

        models_dir = DEFAULT_MODELS_DIR
    written = export_model(args.model, models_dir, panels, out)
    # **`.json` を落とす**(呼び出し側の `--skip-export` の照合と揃える)。
    subject = args.model[:-5] if args.model.lower().endswith(".json") else args.model

    manifest = {
        "subject": subject,
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

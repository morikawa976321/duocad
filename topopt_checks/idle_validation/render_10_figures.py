"""タスク10: 統合報告用の図(Pillowだけ。FEMなし)。保存済みの台帳とマスクから描く。

図1 `10_history_small.png` — 小型4条件の適応・再配置の「これまでの最良材料体積」の履歴と、同条件の製品step20/step05の最終値
図2 `10_shapes_small.png`  — 小型2条件の最終形(奥行き方向の材料の厚みの射影)。step20・適応・再配置
図3 `10_shapes_hanger.png` — hanger H1(200N・2.0mm)の製品step20と5%歩幅(射影)
実行: `.venv/Scripts/python.exe topopt_checks/idle_validation/render_10_figures.py`
出力: `docs/topopt_idle_validation/10_figures/`
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "output" / "idle_validation_20260928"
FIG = REPO / "docs" / "topopt_idle_validation" / "10_figures"
FONT = "C:/Windows/Fonts/meiryo.ttc"
INK, MUTED, GRID, BG = (30, 30, 30), (110, 110, 110), (225, 225, 225), (255, 255, 255)
COLORS = {"step20": (90, 90, 90), "step05": (160, 160, 160), "adaptive": (31, 119, 180), "reallocation": (214, 120, 20)}
LABELS = {"step20": "製品step20", "step05": "step05(5%歩幅)", "adaptive": "適応", "reallocation": "再配置"}


def font(size: int):
    return ImageFont.truetype(FONT, size)


def load_matrix() -> dict:
    d = json.loads((OUT / "05_main_matrix.json").read_text(encoding="utf-8"))
    return {c["case_id"]: c for c in d["cases"]}


def panel(draw: ImageDraw.ImageDraw, box, case: dict, title: str) -> None:
    x0, y0, x1, y1 = box
    f, fs = font(15), font(13)
    draw.text((x0, y0 - 26), title, fill=INK, font=f)
    curves = {m: [(p["evaluation_index"], p["material_mm3"]) for p in case[f"{m}_best_so_far_curve"]] for m in ("adaptive", "reallocation")}
    finals = {m: case[m]["material_mm3"] for m in ("step20", "step05")}
    sfs = {m: case[m]["safety_factor_product"] for m in ("step20", "step05", "adaptive", "reallocation")}
    xmax = max(p[0] for c in curves.values() for p in c)
    ys = [p[1] for c in curves.values() for p in c] + list(finals.values())
    ymax, ymin = max(ys) * 1.05, 0.0
    px = lambda x: x0 + (x1 - x0) * x / xmax
    py = lambda y: y1 - (y1 - y0) * (y - ymin) / (ymax - ymin)
    for i in range(5):
        yy = y0 + (y1 - y0) * i / 4
        draw.line([(x0, yy), (x1, yy)], fill=GRID, width=1)
        draw.text((x0 - 6, yy - 8), f"{ymax * (1 - i / 4) / 1000:.0f}", fill=MUTED, font=fs, anchor="ra")
    draw.text((x0 - 6, y0 - 16), "cm³", fill=MUTED, font=fs, anchor="ra")
    for t in (0, xmax / 2, xmax):
        draw.text((px(t), y1 + 4), f"{t:.0f}", fill=MUTED, font=fs, anchor="ma")
    draw.text((x1, y1 + 22), "評価回数(合格した最良形の履歴。出発点の評価1から)", fill=MUTED, font=fs, anchor="ra")
    for m, v in finals.items():
        draw.line([(x0, py(v)), (x1, py(v))], fill=COLORS[m], width=2)
    for m, pts in curves.items():
        draw.line([(px(a), py(b)) for a, b in pts], fill=COLORS[m], width=2)
    ly = y1 + 44
    for i, m in enumerate(("step20", "step05", "adaptive", "reallocation")):
        lx, lyy = x0 + (i % 2) * ((x1 - x0) / 2), ly + (i // 2) * 22
        draw.line([(lx, lyy + 8), (lx + 18, lyy + 8)], fill=COLORS[m], width=3)
        draw.text((lx + 24, lyy), f"{LABELS[m]} SF{sfs[m]:.2f}", fill=INK, font=fs)


def figure_history() -> None:
    cases = load_matrix()
    picks = [("dilution_base_w6", "dilution 300N・2.0mm(適応が軽いがSF未達)"),
             ("dilution_res3.0_w6", "dilution 3.0mm格子(適応が2.7倍重い)"),
             ("path_baseline_2.0mm", "path 3000N・2.0mm(適応が軽いがSF未達)"),
             ("path_res1.5_w6", "path 1.5mm格子(適応が+83%)")]
    w, h = 1500, 860
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.text((24, 12), "小型題材: 探索中の最良材料体積の履歴(縦軸 cm³、凡例のSFは最終形の製品安全率。目標4.8)", fill=INK, font=font(17))
    for i, (cid, title) in enumerate(picks):
        col, row = i % 2, i // 2
        x0, y0 = 70 + col * 730, 80 + row * 390
        panel(d, (x0, y0, x0 + 640, y0 + 220), cases[cid], title)
    FIG.mkdir(parents=True, exist_ok=True)
    img.save(FIG / "10_history_small.png")


def projection(mask: np.ndarray, axis: int) -> np.ndarray:
    depth = mask.sum(axis=axis).astype(float)
    return depth / max(mask.shape[axis], 1)


def shade(proj: np.ndarray, scale: int) -> Image.Image:
    a = np.clip(proj / max(proj.max(), 1e-9), 0, 1)
    rgb = np.stack([255 - 205 * a, 255 - 175 * a, 255 - 120 * a], axis=-1)
    rgb[proj <= 0] = 255
    return Image.fromarray(rgb.astype(np.uint8)).resize((proj.shape[1] * scale, proj.shape[0] * scale), Image.NEAREST)


def load_mask(rel: str) -> np.ndarray:
    with np.load(OUT / rel) as z:
        return np.asarray(z["mask"], dtype=bool)


def figure_shapes(name: str, title: str, rows: list[tuple[str, list[tuple[str, str]]]], axes: tuple[int, ...], scale: int) -> None:
    f, fs = font(16), font(13)
    tiles = []
    for label, items in rows:
        for cap, rel in items:
            m = load_mask(rel)
            tiles.append((label, cap, m, [shade(projection(m, ax), scale) for ax in axes]))
    cw = max(sum(i.width for i in t[3]) + 8 * len(axes) for t in tiles) + 16
    ch = max(i.height for t in tiles for i in t[3]) + 56
    ncol = len(rows[0][1])
    nrow = len(rows)
    img = Image.new("RGB", (ncol * cw + 20, nrow * ch + 56), BG)
    d = ImageDraw.Draw(img)
    d.text((14, 10), title, fill=INK, font=f)
    for k, (label, cap, m, ims) in enumerate(tiles):
        r, c = divmod(k, ncol)
        x, y = 14 + c * cw, 48 + r * ch
        d.text((x, y), f"{label} / {cap}  材料{int(m.sum()):,}セル", fill=INK, font=fs)
        xx = x
        for im in ims:
            img.paste(im, (xx, y + 24))
            xx += im.width + 8
    FIG.mkdir(parents=True, exist_ok=True)
    img.save(FIG / name)


def main() -> int:
    figure_history()
    figure_shapes("10_shapes_small.png", "小型題材の最終形(奥行き方向の材料の厚みの射影。濃いほど厚い。左=軸0方向、右=軸1方向の射影)",
                  [("dilution 300N・2.0mm", [("step20", "05_masks/dilution_base_w6_step20_best_ever.npz"),
                                              ("適応", "05_masks/dilution_base_w6_adaptive_best_ever.npz"),
                                              ("再配置", "05_masks/dilution_base_w6_reallocation_best_ever.npz")]),
                   ("path 3000N・2.0mm", [("step20", "05_masks/path_baseline_2.0mm_step20_best_ever.npz"),
                                           ("適応", "05_masks/path_baseline_2.0mm_adaptive_best_ever.npz"),
                                           ("再配置", "05_masks/path_baseline_2.0mm_reallocation_best_ever.npz")])],
                  axes=(0, 1), scale=5)
    figure_shapes("10_shapes_hanger.png", "hanger H1(200N・2.0mm)の最終形(射影。左=軸0方向、右=軸1方向)",
                  [("hanger", [("step20(139,230 mm³)", "06_masks/hanger_h1_baseline_step20_best_ever.npz"),
                               ("step05(159,327 mm³)", "06_masks/hanger_h1_baseline_step05_best_ever.npz")])],
                  axes=(0, 1), scale=2)
    print("wrote", FIG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

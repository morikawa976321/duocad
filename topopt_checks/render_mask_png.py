"""メッシュを正射影・奥から順に塗って描く(Pillow だけ)。色は三角形ごとの値(0〜1)を turbo 風の色に。

    render(v, f, val, path, views)   v: (n,3) f: (m,3) val: (m,) 0..1
"""
import numpy as np
from PIL import Image, ImageDraw


def colormap(t):
    t = np.clip(t, 0, 1)
    # 青→水→緑→黄→赤
    stops = np.array([[0.19, 0.07, 0.23], [0.12, 0.45, 0.95], [0.1, 0.85, 0.75], [0.55, 0.95, 0.25],
                      [0.98, 0.75, 0.15], [0.85, 0.2, 0.05], [0.48, 0.02, 0.01]])
    x = t * (len(stops) - 1)
    i = np.minimum(x.astype(int), len(stops) - 2)
    w = (x - i)[:, None]
    return stops[i] * (1 - w) + stops[i + 1] * w


def rot(el, az):
    el, az = np.radians(el), np.radians(az)
    rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, np.cos(el), -np.sin(el)], [0, np.sin(el), np.cos(el)]])
    return rx @ rz


def render_view(v, f, rgb, el, az, size):
    # カメラ系: x 右, y 上, z 手前。el=0,az=0 で -y 方向から見る(正面)
    base = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float)  # world→camera(正面)
    R = rot(el, 0)[[0, 1, 2]] @ base @ rot(0, az)
    p = (v - v.mean(0)) @ R.T
    tri = p[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    shade = 0.35 + 0.65 * np.abs(n[:, 2])
    front = n[:, 2] > 0
    span = np.ptp(p[:, :2], axis=0).max()
    s = (size - 20) / span
    xy = (p[:, :2] - p[:, :2].min(0)) * s + 10
    xy[:, 1] = size - xy[:, 1]
    order = np.argsort(tri[:, :, 2].mean(1))
    img = Image.new('RGB', (size, size), (255, 255, 255))
    d = ImageDraw.Draw(img)
    col = (rgb * shade[:, None] * 255).astype(int)
    for t in order:
        if not front[t]:
            continue
        a, b, c = f[t]
        d.polygon([tuple(xy[a]), tuple(xy[b]), tuple(xy[c])], fill=tuple(col[t]))
    return img


def render(v, f, val, path, views=((20, -60), (0, 0), (90, 0), (0, 90)), size=700):
    rgb = colormap(val)
    imgs = [render_view(v, f, rgb, el, az, size) for el, az in views]
    w = Image.new('RGB', (size * 2, size * 2), (255, 255, 255))
    for i, im in enumerate(imgs):
        w.paste(im, ((i % 2) * size, (i // 2) * size))
    w.save(path)


if __name__ == "__main__":
    # python topopt_checks/render_mask_png.py <mask の npz(キー mask)> <出力 png>
    # 製品の build_surface_from_mask(平滑化後)を描く。venv に matplotlib が無いので Pillow だけで描く。
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from duocad.topopt.mesh import build_surface_from_mask
    surface = build_surface_from_mask(np.load(sys.argv[1])["mask"])
    render(surface.verts_smooth, surface.faces, np.full(len(surface.faces), 0.55), sys.argv[2],
           views=((20, -60), (0, 90)), size=600)

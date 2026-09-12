---
name: duocad-venv-is-uv-managed-without-pip
description: 事実(2026-08-29実測) — duocadの.venvにpipは入っておらずuv管理。numpy 2.4.6とscipy 1.17.1はbuild123d/OCP経由で既に入っている
created: 2026-08-29
updated: 2026-08-29
---

# duocadの.venvはuv管理でpipが無い

`C:\Users\tm976\mf\duocad\.venv\Scripts\python.exe -m pip ...` は **`No module named pip`** で落ちる。パッケージ操作は `uv pip ...`（uv 0.11.18が導入済み）で行う。依存の解決だけ見たいときは:

```
uv pip install --dry-run --python ./.venv/Scripts/python.exe <package>
```

同時に実測した在庫（`pyproject.toml` の宣言は build123d / cadquery-ocp-novtk / pytest / websockets の4件だけだが、推移依存で入っているもの）:

- **numpy 2.4.6・scipy 1.17.1 は既に在る**（build123d/OCP経由）。数値計算を足すのに新規依存は要らない。
- 入っていないもの: scikit-image・trimesh・pyvista・vtk・gmsh・matplotlib・numba・cupy・torch・pyamg・scikit-sparse・meshio。

Python本体は 3.11.15（`pyproject.toml` が `>=3.11,<3.12` で固定）。

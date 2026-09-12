---
name: kernel-bounding-box-and-voxelize-bbox-differ-by-setgap-order
description: OCCTのBnd_Boxは SetGap(0.0) を Add_s の前に呼ぶか後に呼ぶかで値が 1e-7mm 変わる — duocadの kernel.bounding_box(前)と topopt/voxelize(後)が別物なのはこのため
created: 2026-09-05
updated: 2026-09-05
---

# `Bnd_Box` は `SetGap(0.0)` を `Add_s` の前後どちらで呼ぶかで値が変わる

`BRepBndLib.Add_s(shape, box, useTriangulation)` は**形状の公差ぶんの隙間を box に設定する**。
したがって:

- **`SetGap(0.0)` → `Add_s`**(duocad `kernel.bounding_box`): Add が隙間を上書きするので
  効かない。docstring も「`SetGap(0.0)` を入れても内部許容差ぶん(実測 1e-7mm)膨らませた値を
  返すので、**厳密な寸法の根拠には使わない**」と明記している。
- **`Add_s` → `SetGap(0.0)`**(duocad `topopt/voxelize` の内部ヘルパ): 隙間が実際に 0 になる。

**この 1e-7mm が要素サイズに乗ると、辺長が要素サイズの整数倍の箱で体積が厳密一致しなくなる。**
topopt の受入検査 `check_voxelize.py` [7] がその厳密一致を assert しているので、
**ボクセル化の bbox を `kernel.bounding_box` へ寄せると落ちる**(2026-09-05の移設で確認)。

「同じことをしている2つの実装を1つにまとめる」と見えるが、**まとめると数値が動く**。
統合するなら `kernel` 側に「隙間を落とした厳密なbbox」を別の口として足すこと。

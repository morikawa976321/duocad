---
name: no-oss-mesh-to-editable-brep
description: 事実(2026-08-29調査) — 有機メッシュから編集可能なNURBS B-repへの汎用変換はOSSに存在しない。取りうる道は「商用ツール」「メッシュのまま扱う」「参照にして手で作り直す」の3つだけ
created: 2026-08-29
updated: 2026-08-29
---

# OSSにメッシュ→編集可能B-repの道は無い

トポロジー最適化やスキャンが吐く**有機的な三角メッシュ**を、フィレット等が効く**編集可能なNURBS B-rep**へ自動変換する汎用ツールは、2026-08時点のOSSに存在しない。調査で確認できた選択肢は3つだけ:

1. **商用ツール** — Rhino 8 の `QuadRemesh`→`ToSubD`→`ToNURBS`(事実上の業界標準、買い切り約$995)、Fusion 360のMesh→T-spline変換(Autodesk独自・非公開)、Geomagic Design X等のリバースエンジニアリング専用ソフト。
2. **B-rep化を諦める** — nTopが採る戦略。インプリシット/フィールド表現のまま解析・製造まで通す。nTop自身が最適化の生出力を "best viewed as an inspirational representation" と表現し、技術者は普通そこから手で作り直す、と明言している。
3. **参照ジオメトリとして表示し、人が手で再モデリング** — 実務での標準パターン。

OCCTでできるのは `BRepBuilderAPI_Sewing`+`MakeSolid` による**faceted B-rep**化（三角形1枚=Face 1枚のまま）で、STEPに書き出せはするが面数が三角形数と等しくなり、後段のB-rep編集は事実上不可能。`ShapeUpgrade_UnifySameDomain` も、真に同一曲面へ乗る三角形が稀な有機形状ではほとんど効かない。

`geomdl`(NURBS-Python)は曲面フィッティングを持つが**構造化されたUVグリッド点群が前提**で、非構造三角メッシュには直接使えない（先にquadrangulation/パラメータ化という別の難問が要る）。

含意: [[duocad-topopt-scope-cut-to-simp-core]] のようにトポロジー最適化を自作CADへ載せる場合、**出力はメッシュ止まりと最初から決めてよい**。3Dプリント(STL/3MF)へ直行するか、参照として置いて手でモデリングし直すのが現実解。

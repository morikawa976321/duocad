---
name: occt-angular-deflection-params-are-radians
description: 事実(2026-08-30実測、duocad kernel.py調査) — OCCTのBRepMesh_IncrementalMesh/GCPnts_TangentialDeflectionの角度偏差パラメータはラジアンで、度ではない
created: 2026-08-30
updated: 2026-08-30
---

OCCTの`BRepMesh_IncrementalMesh`と`GCPnts_TangentialDeflection`が受け取る角度偏差(angular
deflection)パラメータは**ラジアン**であり、度ではない。`IMeshTools_Parameters().Angle`の既定値が
`0.5`であること、`duocad/kernel.py`が自前でBRepMesh_IncrementalMeshを呼ぶ箇所(引数無しの
tessellate相当)で明示的に`0.5`を渡していることの両方で確認した(0.5ラジアン≈28.6度はOCCTの
デフォルトの妥当な粗さと符合、0.5度では細かすぎて説明がつかない)。

`duocad/kernel.py`の`DEFAULT_ANGULAR_DEFLECTION = 0.35`もラジアンであり(≈20度)、コメントに
単位が書かれていなかったため誤って「度」と読まれかけた(タスク`duocad-edge-picking-deflection-fix`
の指示文に「0.35度」という前提が書かれていたが、実機確認の結果これは誤りだった)。以後この値を
使う実装(GCPnts_TangentialDeflection等)では単位変換をせずラジアンのまま渡してよい。

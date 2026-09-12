---
name: ocp-vscode-selection-channel-discovery
description: 運用知見 — ocp_vscodeでクリック選択をPythonへ取り出す実際の経路(2026-08-07に実機検証で確定)。standaloneでは listener() は使えず status() ポーリングが正。SELECTが送るキーは selectedShapeIDs ではなく selected で、面/エッジの種別が落ちる
created: 2026-07-20
updated: 2026-08-07
---

# ocp_vscode: クリック選択をPythonへ取り出す経路（実機検証済み）

[[cad-migration-deixis-layer]]（CAD deixisレイヤー方針）の土台として使えるかの技術検証。
**2026-07-20はソース読解のみだった。2026-08-07に ocp-vscode 4.0.1 / build123d 0.11.1 で実機検証し、
下記のとおり内容を全面的に書き換えた**（旧版の手順は動かない。詳細な実測値は検証フォルダの `FINDINGS.md`）。

## 正しい経路（実測で動いたもの）

- **standalone ビューア（`python -m ocp_vscode`）では、公開API `ocp_vscode.status()` をポーリングする**。
  戻り値の dict に `selected`（選択された添字のリスト）と `activeTool` が入っている。
  `show(obj, analysis_tool=AnalysisTool.SELECT)` で選択モードは有効化できる。
- **添字 → 形状オブジェクトの解決は `select_face(obj, i)` でそのままできる**。
  ビューア側の添字空間と `ocp_selectors` の添字空間が同一であることは実測でPASS
  （同じ `ocp_tessellate.ocp_utils.get_faces` を使っており、WebSocketのserialize往復でも順序が保存される）。
- **実測値**: 添字→build123dオブジェクトの解決 **0.1〜0.6ms**。`status()` の往復 **中央値39ms**
  （毎回WebSocketを張り直す実装のため）。クリックからPythonに出るまで、0.4秒間隔のポーリングで最大0.5秒弱。
- **standalone は選択添字をクリップボードにも自動コピーする**。つまり
  `select_faces(part, [ここに貼る])` は**自作コード0行で今日から成立している**。最小の出発点。

## 旧版の誤り（同じ罠を踏まないための記録）

- **`selectedShapeIDs` を subscribe する、は誤り**。あのキーは Distance/Properties ツール専用。
  `AnalysisTool.SELECT` が送るのは **`selected`** で、中身は `["1","2"]` のような**裸の添字**。
- **`listener(callback)` は standalone では機能しない。しかも表示を壊す**。
  `standalone.py` は WebSocket の `L:` を「ブラウザが viewer client として登録した」と解釈するため、
  `listener()` が接続すると **`javascript_client` の枠をブラウザから奪う**。更新は中継されず無反応になる。
  `listener()` が想定しているのは **VS Code拡張版**のビューア（拡張版での挙動は未検証）。
- `AnalysisTool.SELECT` は「定義だけでハンドラ未実装」ではない。**ビューア側には実装があり実際に動く**。
  未実装なのは配布版 `backend.py` の側だけ。

## 設計に効く欠陥: 種別が落ちる

ビューアは `_getIndex("faces_6")` → `"6"` と**トポロジ種別を捨ててから**送る。
`faces_6` と `edges_6` がどちらも `"6"` になり、**面かエッジかを復元できない**（実測で確認）。
deixisレイヤーの仕事は「ユーザーが指したもの」を正確に受け取ることなので、
**自作差分は「添字→セレクタ式の逆合成」の手前に、この種別を回復する層を必要とする**。
回避策は3つ: ①ビューア側にパッチ ②面だけに限定して運用 ③選択直後の `lastObject.info.path` を別経路で拾う。

## 添字は実際に壊れる（トポロジカルネーミング問題）

同一の面を追跡して形状変更をかけた実測: 穴の径変更・穴の追加・ポケット追加・面取りでは添字は動かなかったが、
**縦エッジへのフィレットで添字が 5→9 にずれた**。詳細は [[build123d-face-index-breaks-on-fillet]]。
**壊れ方が散発的**なので、生の添字を保存すると「たまに違う面を指す」という最悪の壊れ方をする。

関連: Fusion360離脱の理由6点は [[cad-migration-deixis-layer]] に記載

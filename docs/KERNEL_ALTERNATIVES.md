# 幾何カーネルの代替の調査（2026-11-14）

OCCT（OCP）から別のカーネル・別の言語へ移る価値があるかを調べた記録。**決定（本人、2026-11-14）: 当面は Python + OCCT で続ける。**
カーネルを OCCT にした理由は [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md)（由来マップが取れる唯一の経路）。以下は、その後に見つけた候補の評価と、再検討するときの見方。
調査は AI（調査エージェント）が GitHub・crates.io・PyPI と実機で行った。**確認済み**は親のセッションが一次情報で見直したもの。

## 結論

- OCCT の代わりになるカーネルは、2026-11-14 時点で見つからなかった。候補が共通して欠くのは、**由来マップ（`Modified`/`Generated`/`History` 相当）**と **STEP の入出力**、そして duocad が使う操作（シェル・面オフセット・面削除・はみ出し吸収のフィレット）のどれか。
- 別言語への移植で縮むのは OCCT 以外の時間だけ。測れた経路での上限は約 1.3〜1.75 倍（下記）。数万行の再実装とバインディングの自作に見合わない。

## truck（ricosjp/truck、純 Rust）

| duocad が使う機能 | truck |
|---|---|
| ブーリアン | 部分（面が横断的に交わる場合のみ。接する面は未対応。**確認済み**: `truck-shapeops/src/lib.rs` の記述） |
| フィレット | 部分（両端の頂点がそれぞれ3面に隣接する単一辺のみ。**確認済み**: 同上）。複数辺は monstertruck（フォーク）が追加 |
| 面取り・シェル・面オフセット・面削除・ロフト・ねじ | 未対応（monstertruck は面取りに対応。シェルは未確認） |
| 押し出し・回転・スイープ、NURBS、テセレーション | 対応（テセレーションの品質は未確認） |
| STEP 入出力 | 部分（Fusion・Onshape の出力で読み込み失敗の報告あり） |
| 由来マップ | 未確認 |
| Python からの利用 | なし（pyo3 のバインディングが見当たらない） |

最新リリースは 2024-09。master は 2026-11 まで動くが、実質的な修正は少なく、主にメンテナ1名。ブーリアンの panic・ハングの issue が未解決（#114、#135）。

## USD BRep（NVIDIA-Omniverse/usd-brep 1.0.1、SMLib カーネル、Apache-2.0）

2026-10-08 公開、リリース2回、コミット4件、外部の貢献は受け付けていない。Python は CPython 3.12 の wheel のみ（Windows・Linux。duocad の 3.14 では入らない）。単一スレッド。
実機の比較（Python 3.12 の新規 venv ↔ `.venv` の OCP。各1回計測、揺らぎは未確認）:

| 試験 | USD BRep | OCCT |
|---|---|---|
| 箱×円柱のブーリアン、接触、24 穴 | 成功・体積一致（24 穴 53 ms） | 成功（逐次 76 ms、一括 Splitter 23 ms） |
| 板に 576 穴を順次カット | 5.5 秒 | 29.5 秒 |
| 同じ板の穴縁のフィレット | 63 秒 | 2.3 秒 |
| ヘリックスパイプ | 60 ms | 2.8 秒（build123d） |
| テセレーション（576 穴の板） | 5.7 秒 | 3.0 秒 |
| はみ出す半径のフィレット（円柱縁 r6） | **成功と答えるが `manifold=False`・体積 0**（**確認済み**） | 失敗を返す |
| フィレット付き箱のシェル | 正しい体積 | 素の呼び出しは別物の体積を無言で返す（duocad は検査で防いでいる） |

- **STEP の入出力が無い**（**確認済み**: README とソースに該当が無く、`SmSTEPSurface` は曲面の種類の名前）。入出力は USD・SMLib 独自形式・OCCT の `.brep` のみで、`.brep` からの変換器はソースからのビルドが要る。
- 履歴の公式 API が無い。面オフセットの API が無い。ねじ専用の API が無い（ヘリックス+スイープでは動いた）。
- 判定: 押し出し・回転・ドラフトは使えるが、フィレット（無言の不正結果）・STEP・履歴・面オフセット・Python 3.14 で置き換えにならない。補助（専用ワーカーでの大量ブーリアン、第二カーネルでの検算）も、形状の受け渡しが壁で費用対効果が低い。

## 全体の地図（調査エージェントの報告。出典は各社・各リポジトリ）

- 商用 CAD の大半は、Parasolid（SolidWorks・NX・Solid Edge・Onshape・Shapr3D・Plasticity）、ShapeManager（Fusion・Inventor）、CGM（CATIA）、Granite（Creo）、ACIS（BricsCAD）、C3D（KOMPAS-3D）のクローズドなカーネル。FreeCAD・KiCad は OCCT。
- OSS で実用になる B-rep カーネルは OCCT のみ（C++）。2026-10 に USD BRep が加わったが上記のとおり。
- Rust: truck、monstertruck、新興の純 Rust 勢（arris・vcad など）は自己申告の段階。fornjot は開発終了。Go: B-rep カーネルも OCCT のバインディングも見つからなかった。他の言語（Haskell・Swift・C#・JS/TS）は OCCT のバインディング。
- build123d 相当（CAD-as-code）は、Rust の cadrum（OCCT を静的リンク。**確認済み**: 73★・MIT・2026-11-07 更新）と opencascade-rs が近い。土台はどちらも OCCT。

## Rust 等への置き換えの見積もり

OCCT は C++ で、言語を替えても OCCT の計算は速くならない。簡易プロファイル（`tools/bench_all_ops.py --profile` の途中までの結果。合成部品・カメラ配信の三角形化経路のみ）では、全体 0.81 秒のうち OCCT 本体 57%、Python から C++ への細かい呼び出し（約5.3万回）14%、Python・numpy 28%。置き換えで縮むのは OCCT 以外の約43%で、上限は約1.75倍、現実には1.3〜1.5倍と見積もった。フィレット・ブーリアンなど他の経路は未測定。
規模: `duocad/` は 132 ファイル・約52,000 行、OCCT の API は 154 種を直接使う（`duocad/` に build123d の import は無い。2026-11-14 に外した）。

## 再検討するときの見方（AI の整理）

- truck / monstertruck: シェル・面オフセット・由来追跡・複数辺フィレットと、STEP 読み込みの堅牢性が揃ったとき。
- USD BRep: STEP の入出力、履歴 API、フィレットの失敗を失敗として返す挙動、Python 3.14 への対応が揃ったとき。
- 速さが目的なら、移植より先に [PERFORMANCE.md](PERFORMANCE.md) の7章に残る案（面分割を子プロセスへ出す、境界の細かい呼び出しの一括化）を検討する。

## 確認できていないこと

STEP モデルの USD BRep での読み込み（API が無く不可）、Linux・macOS、ねじの本格形状、truck の由来追跡 API とブーリアンの実際の堅牢性、全操作の OCCT 比率（`tools/bench_all_ops.py --profile` が Python 3.14 で `ValueError: Another profiling tool is already active` で止まるため、全体の内訳は未取得）。

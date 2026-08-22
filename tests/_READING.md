# テストの地図（一時・2026-08-22）

**捨ててよい。** 契約の正は [`README.md`](README.md)（約3000行）。
この文書は「何を見ているか」だけ。関数名はファイルを開けば読めるので、ここには書かない。

収集時点: **pytest 614件**（`llama-server` が居なければ 1 skip）。フロントの単体テストは無い。

---

## 30秒

テストは3系統。役割が違う。

| 系統 | 何か | 見るもの | 要るもの |
|---|---|---|---|
| **pytest** `tests/test_*.py` | 614 | コア・サーバ・MCPの契約 | Pythonだけ |
| **smoke** `web/scripts/smoke*.mjs` | 4本 | 本物のクリックが画面に届くか | ブラウザ |
| **acceptance** `web/scripts/acceptance.mjs` | 1本 | 19操作を人間とAIの両方で動かす | ブラウザ＋MCP |

語彙はこれだけ覚えれば足りる。

- **verb** — 押し出し・フィレットなどの操作。ログに残る。
- **選択** — 人とAIが共有する「今選んでいる面・辺・領域」。コアにある。ログには載せない。
- **述語** — AIが「上面」「Zに平行な辺」のように条件で選ぶ。JSON。DSL文字列は使わない。
- **領域** — スケッチの閉じた面。押し出す対象。
- **提案 / 確定** — AIの提案はそのまま verb に渡せない。人が触るか `commit` してから。

例外2つ（コードを直すな）:

1. `test_incremental_update.py::test_extruding_one_part_of_the_pc_case_stays_under_1000ms` — マシンが混むと赤。この1本だけなら負荷。
2. `test_agent_llama_server.py` — 8080にllama-serverが無ければ skip。

---

## 疑問 → ファイル

ここが本体。上から探す。

| 知りたいこと | ファイル | 件 |
|---|---|---|
| 単位・Body・active | `test_document.py` | 12 |
| undo / redo / JSON保存 | `test_oplog.py` | 8 |
| STEP/STLの入出力 | `test_io_step.py` | 11 |
| STEPをソリッドごとに分解 | `test_import_split.py` | 25 |
| OCPはkernel、build123dはqueryだけ | `test_boundaries.py` | 5 |
| 面押し出し・編集用複製・三角形化 | `test_extrude_face.py` | 21 |
| 線・円・矩形 | `test_sketch.py` | 12 |
| 円弧 | `test_arc.py` | 15 |
| 閉じた領域の検出 | `test_region.py` | 6 |
| 領域の押し出し（結合/切り取り） | `test_extrude.py` | 13 |
| 名前つき寸法 | `test_parameters.py` | 11 |
| 矩形反復 | `test_pattern.py` | 14 |
| 円形反復 | `test_pattern_circular.py` | 26 |
| フィレット | `test_fillet.py` | 17 |
| 面取り | `test_chamfer.py` | 8 |
| 隠すと選べない | `test_visibility.py` | 15 |
| 差分送信・性能 | `test_incremental_update.py` | 19 |
| 選択集合そのもの（企画の核） | `test_selection.py` | 44 |
| 述語で選ぶ | `test_predicates.py` | 33 |
| 形が変わったら選択を空にする | `test_server_selection_reset.py` | 15 |
| プレビューは残さない | `test_preview.py` | 11 |
| ブラウザからスケッチ | `test_sketch_ui.py` | 11 |
| ブラウザからフィーチャ | `test_feature_ui.py` | 22 |
| 開く / 保存 / 書き出し | `test_files_ui.py` | 14 |
| UIのコマンド名がサーバに実在するか | `test_ui_command_wiring.py` | 12 |
| 図形種別の3表が一致するか | `test_entity_kind_tables.py` | 3 |
| MCPの土管（JSON-RPC・イベント） | `test_mcp.py` | 29 |
| 全verbをMCPツールとして出す | `test_mcp_verbs.py` | 53 |
| 選択と述語をMCPへ出す | `test_mcp_selection.py` | 41 |
| アプリ内AIのtool-useループ | `test_agent_loop.py` | 27 |
| AIパネルのWebSocket | `test_agent_ws.py` | 16 |
| 人とAIが同時に触る | `test_agent_concurrency.py` | 12 |
| 実llama-server | `test_agent_llama_server.py` | 1 |
| WebSocketのOrigin | `test_ws_origin.py` | 13 |
| 契約は緑なのに壊れていた経路 | `test_m2_regressions.py` / `test_m3_regressions.py` | 9 / 10 |

ヘルパ: `models.py`（テストではない）。実物STEPのパスと実測値（棚=17ソリッド、PCケース=27ソリッド/2835面）を1箇所に置いている。

---

## 15分で全体を掴むならこの順

1. `test_document.py` + `test_oplog.py` — 文書と履歴。他は全部ここへ載る。
2. `test_selection.py` + `test_predicates.py` — 人とAIが同じ選択を触る、という企画の核。
3. `test_sketch.py` → `test_region.py` → `test_extrude.py` — 空からソリッドが生まれる道。
4. `test_mcp_verbs.py` — AIから見た道具一式（29ツール。名前＝ログのverb名）。
5. 残りは上の表で必要なときだけ。

---

## ファイルごとの中身（1〜2行）

### 骨格

**`test_document.py`**
単位は `"mm"` 固定。Bodyは `solid`（編集可）か `reference`（読み取り専用）。追加したBodyがactiveになる。

**`test_oplog.py`**
`append` は記録だけ（副作用は呼び出し側）。`replay` はログを増やさない。`redo` が公開APIを呼び直すと二重記録になるので禁止。古いJSONの `selection` キーは無視して開ける。

**`test_io_step.py`**
棚STEPを読んで面の内訳が実測と一致。無い/壊れたファイルは文書を触らない。STEP往復で体積と面数が残る。STLは偏差を細かくするとファイルが大きくなる。

**`test_import_split.py`**
複合STEPはソリッドごとにBodyへ分解（PCケース27）。`import_step` は参照、`import_step_editable` は編集可能なBody。どちらも1操作。undoで全部消える。

**`test_boundaries.py`**
一番大事な安全網。OCPをimportしてよいのは `kernel.py` だけ、build123dは `query.py` だけ。サブパッケージに逃げても検出する。検査関数自体も既知の違反ソースで検査する。

### 形を作る

**`test_sketch.py`**
基準平面は XY/XZ/YZ。平面の面にも貼れる。曲面には貼れない。線・円・矩形は名前つき寸法になる。undo可。

**`test_arc.py`**
円弧は線と閉じて領域になる。押すと部分円筒。角度は反時計回り、0度越え可。壊れた半径・同じ始終角は入らない。

**`test_region.py`**
円1つ→領域1。線で割ると増える。列挙順は安定。空や未知のスケッチは失敗。

**`test_extrude.py`**
領域×距離＝体積。`join` は既存へ融合、無ければ新規。`cut` は穴。参照Bodyは拒否。板＋穴は平面＋全周円筒だけ。replayで同じ形。

**`test_extrude_face.py`**
既存の平面を押す／削る。参照は触らない。連鎖は `after_face_index`。曲面・距離0・参照は拒否。`tessellate` は面ごとの三角形でJSONに載る。

**`test_parameters.py`**
図形は数値を持たず名前（`d1`, `d2`…）を持つ。`set_parameter` で形が変わる。領域を壊す編集は拒否。保存→再生で残る。

**`test_pattern.py`**
矩形反復は1操作のまま（実体へ展開しない）。`nx×ny` 個の領域。間隔や個数を変えると穴が追随。押し出したあとに個数を減らしすぎると拒否。

**`test_pattern_circular.py`**
円周上に複製。360÷個数。`rotate_copies` で向きを回すか据え置くか。中心に乗った図形は拒否。個数・角度の後編集も検査する。

**`test_fillet.py`**
箱の辺→円筒＋球＋平面。円い辺→トーラス。半径リストは辺数と一致。失敗は何も残さない。
落とし穴: 半径10.5はOCCTが「できた」と言うが不正。duocadは受け取らない（`test_radius_10_5_...`）。球は種別 `other`。

**`test_chamfer.py`**
直線辺→平面、円い辺→円錐。実物の脚（平面3＋円筒1＋円錐1）にどれだけ近いかも見る。失敗は残さない。

**`test_visibility.py`**
隠したBodyは選択も押し出しもできない。redoも同じ。選択可能なBodyは見えるものだけ、文書順。

**`test_incremental_update.py`**
ブラウザへは変わったBodyだけ送る。隠したBodyは三角形化しない。再表示でも測り直さない。差分は全stateより小さい。
落とし穴: 1000msの1本は負荷で振れる。直すな。

### 選択（企画の核）

**`test_selection.py`**（44・最大クラスのひとつ）
選択はコアの共有物。面・辺・頂点・領域。同じ参照は二重にならない。複数Bodyにまたがれる。隠したBodyは選べない。
覚えること:
- 出所は集合単位（`proposed` / `user`）。人が触ること自体が承認。
- **提案のまま verb には渡せない**。
- 選択はログに載らないが、イベントは1回出る。

**`test_predicates.py`**
`faces where normal·Z > 0.9` のような構造化条件。0件は成功（空で置き換える）。隠したBodyは走査しない。不正な述語は選択を触らない。添字は決め打ちせず、その場の法線・長さから導く。

**`test_server_selection_reset.py`**
形を変える13 verb（押し出し・フィレット・図形追加・パラメータ・undo/redo）が成功したら選択は空。`set_visible` と選択操作そのものは触らない。undo/redoは何を戻しても空にする。「手前を隠して奥を選ぶ」では飛ばない。

### サーバがブラウザに出す口

ここはコアverbの再実装ではなく、「ブラウザからその口が叩けるか」。

**`test_preview.py`**
プレビューは確定と同じ形を返すが、文書・ログ・イベント・キャッシュを汚さない。失敗も残さない。選択の編集もここ。

**`test_sketch_ui.py`**
基準平面と平面の面にスケッチ。4種を描ける。undoは1図形ずつ。線で割った領域を共有選択で選べる。領域ペイロードはスケッチが変わるまでキャッシュ。

**`test_feature_ui.py`**
辺の折れ線の添字＝フィレットが取る添字。ブラウザから押し出し・フィレット・面取り・反復。失敗しても選択は残る。UIとAIが同じverbで同じ形。プレビューはBodyを増やさない。

**`test_files_ui.py`**
保存はverbではない（ログをファイルに書くだけ）。開くはログ再生。失敗したopenは今の文書を残す。ブラウザからのSTEP読みは最初から編集可能。パラメータと履歴位置が配信される。

**`test_ui_command_wiring.py`**
静的検査。UIが投げるコマンドIDがサーバの表に実在する。WebSocketコマンドは「対象集合」を引数に取らない（選択は共有物）。AIコマンドはverbではない。

**`test_entity_kind_tables.py`**
`sketch.py` / `kernel.py` / `web/src/entityKinds.ts` の3表のキー集合が一致。新種別を1表にだけ足しても、これまでは止まらなかった。

**`test_ws_origin.py`**
自分が出すページ（8766とdevの5173、localhostと127.0.0.1）だけ接続可。Origin無しは意図的に許可（MCP等）。

### AI（MCPとアプリ内）

**`test_mcp.py`**
JSON-RPCの土管。未知メソッド・不正引数はプロトコルエラー。変更はイベント1回。全WebSocketクライアントへ届く。stdioは行区切りJSON。bboxは三角形化に依存しない。

**`test_mcp_verbs.py`**（53・最大）
29ツールちょうど。名前＝ログのverb。形は選択から、数値・パスだけが引数。結果は文ではなく `changed_bodies` 等。失敗は6語彙の構造化エラー（例外テキストを投げない）。失敗したverbは文書を触らない。stdioへゴミを出さない（exportがチャンネルを壊さない）。

**`test_mcp_selection.py`**
提案は明示参照か述語のどちらか一方。選択変更はログに載らない。0件は成功。不正な述語は直し方が返る。提案のまま fillet 等はできない（ゲートはコア側。MCP層の二重検査ではない）。`measure` は提案でも測れる。交互操作の一巡がMCPだけで閉じる。

**`test_agent_loop.py`**
UI無しのtool-use。AnthropicとOpenAI互換の2アダプタ。ツール記述子とsystem promptはMCPサーバと同じ文面。壊れたJSONは推測しない。失敗してもループは続く。上限・キャンセル・会話継続。APIキーはenv→ローカルファイル、reprに出ない、gitignored。

**`test_agent_ws.py`**
パネルからの `ai_status` / `ai_configure` / `ai_send` / `ai_cancel`。送信は完了前に返る（ストリーム）。実行中の二重送信は拒否。Anthropicに攻撃者のbase_urlを渡さない。最後のクライアント切断で実行中を止める。

**`test_agent_concurrency.py`**
人が途中で形や選択を変えたら、AIの古いツール呼び出しは静かに別物へ効かない。楽観ロックは「ログの本数＋選択の指紋」。同じ種別の辺の入れ替えも検知。1応答に複数ツールがあるとき、その間も見る。種別不一致は「何が確定しているか」を名指しする。

**`test_agent_llama_server.py`**
8080が生きていれば、ローカルモデルが本当に文書ツールを呼ぶ。居なければskip。

### 契約は通っても壊れていた経路

契約テストを1行も変えずに穴が残った、という再発防止。**新しい振る舞いを足す場所ではない。**

**`test_m2_regressions.py`**
面に貼ったスケッチが領域検出で落ちていた。別スケッチの領域を混ぜると体積0を黙って作った。NaNが正常な領域まで消し、JSONを壊した。参照がactiveのとき最初の `join` が板を作れなかった。

**`test_m3_regressions.py`**
`set_parameter` の作り直しでactiveが消えた。失敗すると空の文書が残った。円が弾く値を後から書けた。他人のログをreplayすると例外なしで空になった。

---

## pytest が見ていないもの

ここを緑にしても保証されない。実機か目視。

| 見ていない | 代わりに見ている場所 |
|---|---|
| `add_line` のゴムひも（確定前の追従線） | 目視。acceptanceは「線が1本増えた」まで |
| Escでプレビュー中断 | `smoke.mjs` / `smoke-features.mjs`（acceptanceには無い） |
| 19操作の「人間が満足に使える」全体 | `acceptance.mjs`（記録は `ACCEPTANCE.md`） |
| AIが操作すると画面が変わる（実プロセス） | `smoke-mcp.mjs` |
| パーツ一覧27・奥の面のピック | `smoke.mjs` |
| 空から板→穴→フィレット→面取り→反復 | `smoke-features.mjs` |
| 穴径1箇所で全部追随・保存して開き直す | `smoke-files.mjs` |

smoke / acceptance はサーバを自分で立てて止める。既に立てているものへ繋ぐときだけ `--url`。

---

## 走らせ方

```powershell
# 契約（614）。サーバもブラウザも不要
cd C:\Users\tm976\mf\duocad; .\.venv\Scripts\python.exe -m pytest -q

# 1ファイル
.\.venv\Scripts\python.exe -m pytest tests/test_selection.py -q

# 実ブラウザ
cd C:\Users\tm976\mf\duocad\web
node scripts/smoke.mjs
node scripts/smoke-features.mjs
node scripts/smoke-files.mjs
node scripts/smoke-mcp.mjs

# 19操作 × 人間/AI
cd C:\Users\tm976\mf\duocad; node web\scripts\acceptance.mjs
```

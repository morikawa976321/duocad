# duocad

**Fusion360と同等以上にモダンでシンプルなUIに、自分が実際に使う手動操作と強力なAI操作を載せ、
両者が同じ選択集合を交互に触れるようにしたモデリングツール。**
差別化はUIでも機能数でもなく **交互操作**（AIが集合を提案 → 人が過不足を直す → AIが操作を適用）にある。

**状態:** 24操作。うち23操作が人間（UI）とAI（MCP）の両側から使える（`set_parameter` だけは2026-08-30に人間側の口を捨て、AI専用にした）。このREADMEは開発を再開するときの入口である。

| | |
|---|---|
| ブランチ / コミット | `main`（最新は `git log -1`） |
| テスト | **809件**（うち2本は実機が揃うときだけ走る＝ふだんはskip。**赤は1本も無い**） |
| 実行環境 | Python 3.11（`.venv`）／ Node（`web/node_modules` 導入済み）／ `web/dist` ビルド済み |
| 単位 | mm 固定 |

### 文書の地図（このREADMEは入口。詳細は下記が正）

| 文書 | 何が書いてあるか | 読むとき |
|---|---|---|
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | **設計の現行の正**。何を作るか・到達表・残タスク | 仕様を確かめるとき |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | **なぜこう作るか**（§2 分業の原則が企画の主張） | 判断の背景を辿るとき |
| [MCP.md](MCP.md) | **MCPツール一覧の正**（36個）と登録手順 | AI側から触るとき |
| [tests/README.md](tests/README.md) | テストが定義するAPI契約 | コアの挙動を疑うとき |
| [docs/TOPOPT.md](docs/TOPOPT.md) | **形状最適化の実測の記録**(費用の表・余白比率・案5の比較・応力・面アンカー・変異検査) | 数字の根拠を辿るとき |

---

## 0. 設計原則

UIとAI操作の両方に共通する設計原則。verbを足す・直すときはここへ立ち返る（本人決定。
背景は engine `duocad-ui-interaction-principles` / `duocad-errors-shown-via-toast` /
`duocad-sketch-start-is-two-steps-by-pointing`）。

1. **viewport上で実物をクリックして選択できるものは、別途ボタンを用意しない。**
   実例はスケッチの貼る先クリック（`CommandHost.svelte`）——基準平面(XY/XZ/YZ)もボタンではなく
   viewport上のグリッドを直接クリックして指す。
2. **すべての操作を verb→target→how の順序に統一する。**
   コマンドを起動し(verb)、対象を指し(target。クリックまたは共有選択集合から)、
   どうするかを打つ(how。数値・選択肢・パス)。全コマンドで同じ型（`CommandHost.svelte`）。
   **対象を指すID・点の座標・貼る先の平面名は、文字列や列挙に見えても target 側であり
   how ではない**（`MCP.md`「AIはどこまで自分でやるか」と揃える。「名前はすべて対象」
   という言い方はしない — 新規ボディへの命名のような**省略できる付帯入力**は target でも
   how でもない第三分類である。**現行のverbに実例は無い**。無い例を作文せず、規則だけ残す）。
   **適用範囲はコマンド行を通るverbに限る**。値の打ち直し
   (`set_parameter`)・可視性の切替(`set_visible`)・選択集合の編集は直接操作であり、
   この型の対象外である。
3. **省けるクリックは徹底的に省く。** Enterが要るのは次のいずれかに当てはまる操作だけで、
   どちらでもなければ最後のクリックで即適用される（実装は `web/src/commands/command.ts`
   の `needsEnter`、表は `tests/test_ui_command_wiring.py` の `ENTER_REQUIRED`）:
   - (1) **対象を複数選べる**（辺を何本でも・図形を何個でも）
   - (2) **howの入力がある**（数値・選択肢・パス）

   現行で即適用になるのは **`add_line`・`sketch_create`** の2つ。
4. **エラーはすべてトーストで見せる**（`web/src/Toast.svelte`）。**いったんエラーのみ**を
   寄せてあり、情報（読み込んだ部品数・書き出したパス等）は`note()`が内部状態として
   保持するだけで画面には出さない（`window.__duocad.messages` で読める）。
5. **コメントは、コードに現れない制約だけを書く。** 経緯はgitが、決定の理由はengineが持つ。
   書いてよいのは**外部の罠・実測事実・層をまたぐ契約・意図的な変則**の4種だけである。
   「以前は〜だった」「〜から変えた」の語り、次の行が何をするかの言い直し、レビュアーへ向けた
   設計エッセイ、複数ファイルへ同じ理屈を書く重複、テストが既に固定している事柄の再説明は
   書かない（tests の docstring は「落ちたとき何を意味するか」の1〜2行だけ）。

AI側の対応する規則は [MCP.md](MCP.md)「AIはどこまで自分でやるか」が正。

---

## 1. 起動（開発サーバー）

前提はどちらも導入済みなので、**通常は起動コマンド1本だけでよい**。作り直しが要るときだけ:

```powershell
cd C:\Users\tm976\mf\duocad\web; npm install; npm run build
```

> **`web/dist` を作り直さないとUIの変更は画面に出ない。** Pythonサーバが配信するのは
> `web/src` ではなく `web/dist`（ビルド成果物）。`web/src` を触ったら `npm run build`。

**立て方は1つ。** MCPとWebSocketと静的配信を1プロセスで兼ねるので、手で触るだけの日も
AIに頼む日も同じもので立てる。**先にアプリを立てて、Claude Codeが後から繋ぎに来る**（1.2）。

**AIから文書を触る道は2本ある**。どちらもこの1つの立て方の上に乗り、同じ `Session` を叩く
対等なクライアントなので**同時に使ってよい**:

- **アプリ内AIパネル**（画面右サイドバー）—— ローカルの llama-server（キー不要）。
  APIキーでのClaude利用はできない（Claudeを使う道は下のMCP経路）
- **Claude Code**（MCPの30ツール）—— **サブスク認証のまま使えるのはこちらだけ**。繋ぎ方は1.2

### 1.1 立てる

```powershell
cd C:\Users\tm976\mf\duocad; .\.venv\Scripts\python.exe -m duocad.mcp_server --open
```

`--open` でブラウザが開く（省略時は開かない）。**ターミナルのstdinが開いている限り生きる**（`Ctrl+C` で終了）。
立つと stderr にこう出る:

```
duocad MCP server: stdio / WebSocket ws://127.0.0.1:8765
  ブラウザで開く: http://127.0.0.1:8766/
  MCP(HTTP): http://127.0.0.1:8766/mcp
  bodies=0 (参照ボディを除く)
```

### 1.2 Claude Codeを繋ぐ（登録は要らない）

**`.mcp.json` がリポジトリ直下にあるので、このディレクトリでClaude Codeを起動すれば繋がる**
（起動時に**cwdの `.mcp.json` だけ**を読む。親ディレクトリは遡らない）。初回だけ「このプロジェクトの
MCPサーバを承認するか」と訊かれ、承認は `~/.claude.json` の `enabledMcpjsonServers` に残る
（やり直すなら `claude mcp reset-project-choices`）。

**一度だけ必要なのは、古いstdio登録を消すことだけ**:

```powershell
claude mcp remove duocad -s local
```

スコープの優先順位は **local > project > user** で、**エントリ全体が入れ替わる**（フィールドは
マージされない）。古い登録はlocalスコープなので、消すまで `.mcp.json` を**黙って覆い隠す**。

- **duocadが立っていなくてもClaude Codeは起動する**（そのサーバが `✘ Failed to connect` になるだけ）。
  繋がったかは `claude mcp list`（接続まで見る）、セッション内なら `/mcp`
- **HTTPのサーバはClaude Codeが自動で繋ぎ直す**（初回失敗で最大3回、セッション中の切断で最大5回・
  指数バックオフ）。**duocadを立て直してもClaude Code側は放っておいてよい** —— stdioには無かった性質
- 起動時の待ちは既定30秒（`MCP_TIMEOUT` にミリ秒を入れて変更する）
- **トークンもヘッダも要らない。** `/mcp` は `Origin` を持つリクエスト（＝ブラウザから来たもの）と
  知らない `Host` を落とすので、URLだけで足りる。理由は `WebHandler._mcp_forbidden` のdocstring
- **stdioの口も残してある** —— Claude Desktopのようにstdioしか話せないクライアント用（[MCP.md](MCP.md)）

duocad以外のディレクトリからも使いたい日だけ、手でuserスコープへ登録する:

```powershell
claude mcp add --scope user --transport http duocad http://127.0.0.1:8766/mcp
```

**既定にはしない** —— 30ツールが無関係なプロジェクトにも出てくるため。

### 1.3 frontendを直しながら見る（Vite dev server）

```powershell
cd C:\Users\tm976\mf\duocad; .\.venv\Scripts\python.exe -m duocad.mcp_server   # コア側(WS 8765)
cd C:\Users\tm976\mf\duocad\web; npm run dev                                              # 画面(5173)
```

`http://127.0.0.1:5173/` を開く。devサーバは `/duocad-config.json` を返さないので、
画面は既定の `ws://127.0.0.1:8765` へ落ちて繋がる（Pythonサーバは立てておくこと）。
このときだけは `web/dist` の作り直しが要らない（Viteが `web/src` を直に配る）。
型検査は `npm run check`（`tsc --noEmit` ＋ `svelte-check`）。**`.svelte` の中も見る**。

### ポートまとめ

| ポート | 何 | 誰が使う |
|---|---|---|
| 8765 | WebSocket（コマンド下り／イベント上り） | `duocad.mcp_server` |
| 8766 | 静的配信（**ブラウザで開くのはこちら**）＋ `/mcp`（**Claude Codeが繋ぐのはこちら**） | 同上 |
| 5173 | Vite dev server | `npm run dev` |
| 8775/8776・8785/8786・8795/8796 | `smoke` / `smoke-features` / `smoke-files` | **各スクリプトが自分で立てる** |
| 8805/8806・8815/8816 | `smoke-toast` / `smoke-regions` | 同上 |
| 8865/8866・8885/8886 | `smoke-mcp` / `acceptance` | 同上 |
| 8975/8976・8985/8986 | `smoke-move` / `smoke-mesh-body` | 同上 |

---

## 2. UIチートシート

操作手順・キー割当・ボタン文言は各コンポーネントのコメントが正
（`CommandHost.svelte`・`commands/*.ts`・`FilesPanel.svelte`・`catalog.py` など）。
ここに書くのは、コードを読むだけでは分からないことだけ。

- **右サイドバー（`.sidebar`）の幅は常に300px固定**。動くとビューポートの寸法＝カメラの
  アスペクトが変わり、実機検査の座標決め打ちが黙って別の場所を掴む
  （[[viewport-resize-silently-changes-what-a-click-grabs]]）。
- 通常モードで右サイドバーに `AiPanel` だけが残り空きの帯になるのは**意図した見た目**。
- 設定ダイアログは**非モーダル**（`show()`）で、**開いている間もビューポートは触れる**
  （2026-09-04に `showModal()` から切り替え）。FOVは見ながら決める設定なので、背後を
  inert にすると「見る」だけが残って「回して確かめる」が死ぬ。`showModal()` がただで
  付けていた Esc と重なり順は自前で持ち直してある（`SettingsDialog.svelte`）。
- **Enterが要らないコマンドは2つ（線分・スケッチ）**。
- undo/redoは**キーボードのみ**（Ctrl+Z / Ctrl+Y）。
- **保存できる先は「いま編集しているファイル」1つだけ**（2026-08-30）。上書きの口は
  ヘッダの保存ボタン（`#file-save`）と **Ctrl+S** で、行き先を選ばせない。ファイルパネルの
  名前欄は「**新しい名前**で保存」専用で、既存の名前を打つとサーバが拒否する
  （`duocad/session.py` の `save_document`。**現在のファイルと同じ名前でも拒否する** ——
  例外条項を残すと、後からUIを足したときに黙って穴が開く）。一覧の行にあった「上書き」
  ボタンは撤去した：A を編集中に B の行を押せば B が今の文書で潰れ、操作ログが唯一の正で
  ある以上それは復旧できなかった。**いま編集しているファイルはサーバが持ち**
  （`state.current_name`）、画面はその写しを出す＝リロードしても名前が残る。
- `#save-state`（保存済み/未保存）の判定は**内容の比較ではなく操作ログの履歴位置**による。
  undoで同じ形へ戻しても「未保存」のまま。可視性の切り替えはファイルに載らないので動かさない。
- **選択パネルは無い**。選択を直す手段は3Dクリックに一本化してある。
- **保存ファイルは自己完結している**。読み込んだSTEPは本文がそのまま操作ログへ
  取り込まれるので、元のSTEPを移動・改名・削除しても、別PCへ持っていっても開ける
  （Fusionの「挿入」と同じで、元STEPを後から更新しても追随はしない）。代償はファイルサイズで、
  4.6MBのSTEPを読むと `model.json` は約4.8MBになる（開くのに約1.6秒）。
- **形を変える操作（押し出し・丸め・面取り・反復・図形追加・パラメータ変更）の確定後、
  undo/redoの後は選択が常に空になる**。添字は形状変更で壊れるので、後継を
  当てにいくのではなく選び直させる。可視性の切替（手前を隠す）では選択は飛ばない。

---

## 3. 自動検査の走らせ方

### pytest（コア・MCPの契約 854件）

```powershell
cd C:\Users\tm976\mf\duocad; .\.venv\Scripts\python.exe -m pytest -q
```

サーバもブラウザも要らない。実STEPを読む重いテストは含まない。

### 形状最適化の受入検査(11本・502件。通しで約200秒)

**pytest には入れていない。** 通しで約200秒あり、`tests/` へ入れると `pytest -q` が毎回
それだけ重くなる。マーカーで既定から外す案も採らない —— duocad に CI が無いので、既定から
外した瞬間に誰も回さなくなる(`pytest -q` が緑で返るため、無人の runner は「緑だ」と
報告して先へ進む)。**強制力は、タスクに書く「受入検査がすべて緑」の一文と runner が担う。**

```powershell
cd C:\Users\tm976\mf\duocad

# SIMP側の数値の正しさ。約6秒
& .\.venv\Scripts\python.exe topopt_checks\check_simp.py

# メッシュ化側の正しさ。SIMPを1回回すので約13〜30秒(機械の状態による)
& .\.venv\Scripts\python.exe topopt_checks\check_mesh.py

# ボクセル化側の正しさ。SIMPを回さないので約8秒
& .\.venv\Scripts\python.exe topopt_checks\check_voxelize.py

# 面アンカー側の正しさ。端から端まで1回通すので約37〜49秒(うち約27秒が検査[5]の円筒面)
& .\.venv\Scripts\python.exe topopt_checks\check_anchor.py

# 塊(設計空間)。分離 + 案1 + 連結性ガード + 案5 + 4題材の通し + 既定の導出 + 空の条件面。約19秒
& .\.venv\Scripts\python.exe topopt_checks\check_design_space.py

# 体積目標を絶対量(mm^3)で指定する口。プリセットを再現するので約16秒
& .\.venv\Scripts\python.exe topopt_checks\check_absolute_volume.py

# von Mises応力・実単位換算・代表応力・最小安全率。約8秒
& .\.venv\Scripts\python.exe topopt_checks\check_stress.py

# 幾何マルチグリッド前処理(階層・対称正定値性・検出力・格子非依存性・答えの一致)。約21秒
& .\.venv\Scripts\python.exe topopt_checks\check_multigrid.py

# CPU並列化(行ブロック並列matvecの正しさ・退避経路・スレッド数を振ってもビット一致)。約15秒
& .\.venv\Scripts\python.exe topopt_checks\check_parallel.py

# 安全率の外側ループ(配線・単調性のスポット確認・入口・達成不能の例外・収束・引数検証)。約39〜48秒
& .\.venv\Scripts\python.exe topopt_checks\check_safety_loop.py

# 密度フィルタ半径をmmで指定する口(排他・下限・境界・既定不変・単位の等価性・検出力・配線)。約10秒
& .\.venv\Scripts\python.exe topopt_checks\check_filter_radius.py
```

**どれも最後の行が `OK:` で、終了コードが 0 なら合格。** 11本すべてで合計 **502件**
(24 / 15 / 48 / 59 / 124 / 44 / 40 / 46 / 41 / 30 / 31)。**項目数を減らさないこと。**
秒数は熱状態で 1.2〜1.7倍振れる(engine `benchmark-seconds-swing-with-thermal-state`)ので、
所要は目安である。何を守っている検査かは [docs/TOPOPT.md](docs/TOPOPT.md)
「受入検査が見ているもの」に書いてある。

### 実ブラウザのsmoke（本物のクリックとキー入力）

**サーバを立てる手順は要らない**。**11本とも自分で `duocad.mcp_server` を立てて、
終わったら止める**ので、コマンドは1本ずつ叩くだけでよい。**毎回まっさらな文書で始まる**。

ブラウザは**Chromeを先に見る**（無ければEdge。`DUOCAD_BROWSER` で明示すればそれが最優先）。

```powershell
cd C:\Users\tm976\mf\duocad\web

# ① PCケース（ブラウザの一覧・可視性・奥の面のピック）※スクリプトが自分でSTEPを読み込む
node scripts/smoke.mjs

# ② 空から手だけで形を作る（スケッチ→押し出し→穴→フィレット→面取り→反復）
node scripts/smoke-features.mjs

# ③ パネルとファイル操作（パラメータ追随・保存/開く・STEP/STL・**即適用の3条件**）
node scripts/smoke-files.mjs

# ④ AIが操作すると人間の画面が変わる（**AIの選択変更では即適用が発火しない**ことも見る）
node scripts/smoke-mcp.mjs

# ⑤ エラーの見せ方(トーストの積層・数え上げ・クリック素通し・close・自動消滅)
node scripts/smoke-toast.mjs

# ⑥ 通常モードで領域を指せる(要求中だけ・全スケッチ・面より優先・修飾キー・一巡して形が変わる・消えた領域は押し出せない)
node scripts/smoke-regions.mjs

# ⑦ 選択パネルを消した後の代替(頂点マーカーのCtrl+クリック・提案のまま人が実行できる)
node scripts/smoke-vertex.mjs

# ⑧ SVG読み込み(スケッチへ入る→パス→Enter・開いているスケッチへ足す・新しいスケッチは
#    できない・スケッチモードのまま押し出せる・曲線の側面が割れない・Ctrl+Z 1回)
node scripts/smoke-svg.mjs

# ⑨ ホバーの予告とクリックの結果が一致する(面・外へ出たら消える・選択済みにも明度・辺が先)
node scripts/smoke-hover.mjs

# ⑩ 直接選択とmove(右ドラッグ回転・中ドラッグパン・左ドラッグのウィンドウ選択・
#    Shift/Ctrlのトグル・点/辺の移動と複製・ボディの移動と複製・
#    モード操作のツールバー移設・右サイドバーの開閉)
node scripts/smoke-move.mjs

# ⑪ 三角形メッシュのボディ(部品一覧に出る・viewportが三角形を持つ・画面に投影できる・
#    編集は断られる・表示の切り替え・混在のままSTLへ書ける・消せる)
node scripts/smoke-mesh-body.mjs
```

既に立てているサーバへ繋ぎたいときだけ `--url http://127.0.0.1:8766/` を渡す
（その場合スクリプトはサーバを立てず、止めもしない）。

### 一括受入チェック（20操作 × 人間/AI ＋ 交互操作）

```powershell
cd C:\Users\tm976\mf\duocad; node web\scripts\acceptance.mjs
```

MCPとWebSocketを1プロセスで持つサーバを自分で起動し、**人間側はブラウザの本物のクリックとキー、
AI側は本物のJSON-RPC**で20操作を叩く（`set_parameter` の人間側だけは口が無いので空欄）。結果はJSONでstdout末尾に出る。数分。
**形状最適化の4操作(合計24)はまだ`acceptance.mjs`に無い**（`web/scripts/smoke-optimize-mode.mjs` が個別の実機smokeを持つ。到達表は §0 参照）。

**❌ が1つでもあれば終了コード1で終わる。** ただし**途中では止めない** ——
全セクションを最後まで走らせて RESULT_JSON を出し切ってから、`failed[]` を見て終了コードだけを決める。

---

## 4. アーキテクチャ

```
┌── AI（MCPクライアント）──┐        ┌── 人間（アプリ層 / ブラウザ）──┐
│  述語で引数を作る          │        │  ポインティングで引数を作る      │
└───────────┬──────────────┘        └──────────────┬─────────────────┘
            │ stdio(JSON-RPC)                        │ WebSocket
            │                                        │ コマンド↓ ／ イベント↑
            └────────────────┬───────────────────────┘
                             ▼
        ┌──────────── コア（Python・唯一の真実源）────────────┐
        │  文書（ボディ・スケッチ）／ 選択集合 ／ 操作ログ ／ undo │
        │  ── verb層（純関数） f(文書, 選択, 引数) → 新しい文書 ──  │
        │      問い合わせ : build123d ラッパ（差し替え可能）         │
        │      操作      : OCP（OCCT）＋ 由来マップ回収            │
        └────────────────────────────────────────────────────┘
```

### 外してはいけない3つ

1. **AIの経路はアプリ層を通らない。** コアはUIが無くても機能的に完全であること。
   アプリ層にしか無い機能を作った瞬間、AIから使えなくなる。**層分けの唯一かつ最大の存在理由。**
2. **コマンドは下り、イベントは上り。** AIが選択や可視性を変えるとコアがイベントを発火し、
   ブラウザが描き直す。**この上り方向が無いと交互操作が成立しない。**
3. **アプリ層が落ちて失われてよいのは、途中まで入力していたコマンドだけ。**
   文書・選択・操作ログ・undo履歴はすべてコア側にある。

### 中核の4つの決めごと

- **操作ログが唯一の正で、形状はそこから導出されるキャッシュ。** 保存形式はJSON（SQLiteは使わない）。
  開くときはログから再生する。undo・再生・AIの履歴読み・git差分が全部これに乗る。
- **選択集合はアプリ層ではなくコアにある。** 永続／双方向（AIが書ける）／種別を保持／差分編集可能／
  **提案（`proposed`）と確定（`user`）を区別**。ここをUIの状態にすると交互操作が原理的に不可能になる。
  **verbが食う面・辺・領域は、MCPからもWebSocketからも引数で渡さない**——
  どちらもこの集合から取るので、「対象集合の指定方法」は1つしかない。
- **verbのメタ情報は1箇所にしか書かない。** 名前・引数・カテゴリ・プレビュー可否・
  MCPのスキーマは `duocad/catalog.py` の `COMMANDS` が唯一の宣言で、WebSocketの許可リストも
  MCPのツール定義も導出である。**verbを1つ足すときメタ情報を書くのは宣言1エントリだけ**。
  **ただし実行の薄いラッパ2本
  （`Session` と `DocumentTools`）は依然verbごとに要る** —— 手順の正は `catalog.py`
  冒頭docstringの5ステップ。ここを1本化するには汎用ディスパッチャが要る（未着手）。frontendだけは別に持つ ——
  TS側が持つのは**引数の集め方**（どのクリックを待つか・入力欄のid）で、宣言には無い情報だから。
  名前と分類が一致することは `tests/test_ui_command_wiring.py` が機械で見る。
- **verb ＝ ①undo1回で消える単位 ②操作ログ1行 ③MCP1呼び出し が一致する単位。**
  UIの操作手順（面を選ぶ→距離→Enter）は**1つのverbを組み立てる漏斗**であって、3つのverbではない。

### ボディの種別 —— B-rep と 三角形メッシュ（2026-09-05）

`Body.kind` は2つある。**形の持ち方は種別ごとに1本だけ**で、混ぜたBodyは作れない
（`Body.__post_init__` が弾く）——「`body.shape is None`」で分岐している既存の経路が
「どちらの意味か」を判定できなくなるため。

| kind | 形 | できること |
|---|---|---|
| `"solid"` | `Body.shape`（`TopoDS_Shape`） | 全部 |
| `"mesh"` | `Body.mesh`（`Mesh` = 頂点＋三角形。不変） | **表示 / STL書き出し / 削除 / 表示の切り替えだけ** |

**メッシュボディに編集verbを効かせない**（2026-09-04本人決定。理由の本文は
`duocad/meshbody.py` のモジュールdocstring）:

1. marching cubes の出力は**デモの粗い解像度でも6,000三角形**、実用解像度なら数万。
   三角形1枚を1面とするシェルとしてOCCTへ入れると**面数が数万**になる。
2. **`kernel.face_count` は2,835面で既に律速**（2026-09-04実測。ドラッグ一括選択1回で51.1ms、
   `TopExp.MapShapes_s` を参照1件ごとにやり直す）。数万面では操作のたびに秒単位で待つ。
3. **三角形の辺にフィレットを掛けるのは幾何的に無意味**。まともなB-repにするには
   面の再構成（平面・曲面フィッティング）が要り、それは別の仕事。

断りは1箇所（`meshbody.reject`）に文言を持ち、**必ず「次に何ができるか」を書く**。
断りが入る門は2つだけ: `verbs/_common.editable_body`（面・辺を取るverb全部）と
`verbs/extrude._resolve_target`（領域の押し出しだけ専用の解決関数を持つ）。

決めたこと3つ:

- **メッシュボディの扱いは「名指ししたら断る / 導出した集合からは黙って外す」。**
  この1本の線で全部の門が決まる —— `select_face`/`edge`/`vertex`・`predicates` の `body` 明示・
  `optimize.face_fingerprint`・`sketch` の貼る先は**名指し**なので断り、
  `predicates` の `body` 省略（可視Body全部）と `export_stl` の対象集合は**導出**なので、
  前者は黙って外し後者は含める。判断の根拠は「呼び出し側が何を主張したか」である ——
  名指しは「このボディには面がある」という誤った主張なので断る。導出された集合から外すのは
  不可視Bodyを外しているのと同じで、主張と矛盾しない。**ここで断ると、最適化結果のメッシュが
  文書に1つあるだけで述語による選択が丸ごと使えなくなる。**
- **選択の粒度はボディ単位だけ。** メッシュには面の指紋（選択の安定性の仕組み）が無いので、
  `select_face` / `select_edge` / `select_vertex` はメッシュボディを断る（`kind="body"` を使う）。
  **その結果、メッシュボディを含むドラッグ一括選択は丸ごと落ちる** —— 範囲外の添字を渡した
  ときと同じ挙動で、わざとそろえてある（黙って読み飛ばすと「囲んだのに入っていないものがある」
  が見えなくなる）。
- **STLは混在した文書でも1本に両方書く。** メッシュの三角形は**テッセレーションを通さず
  そのまま**、B-repはその場で `kernel.tessellate` して同じファイルへ。ただし
  **メッシュが1つも無い文書は従来どおり `kernel.write_stl`（OCCT）を通る** ——
  B-repだけの書き出しを1ビットも変えないため。**STEPはメッシュを含む文書を断る**
  （境界表現の形式に三角形を書く先が無い。ファイルも作らない）。
- **undo / redo に載る。** メッシュボディの追加は操作ログ1行（verb `add_mesh_body`）で、
  三角形は `args` にそのまま入る。**元になったB-repを args に持たない** —— 持つと、元の
  ボディをその後に編集した文書を再生したときに別の形のメッシュが復元される。代償として
  **数万三角形のメッシュを足したモデルのJSONは数MBになる**。

`add_mesh_body` は**WebSocketには開けていない**（`catalog` の `ws="none"`）。画面から三角形を
手で打つ操作は無く、入口はMCPと、形状最適化の結果を入れるコード側だけである。

### 前提にしないこと

- **複数のブラウザ・複数の人が同じ文書を同時に編集すること。**
  WebSocketは複数接続へ配信する作りだが、想定するのは「1人が1つのブラウザで開いている」
  ことだけで、同時編集の経路に `Session` 全体を守る排他は無い。形状が壊れることはない
  （verbの適用は同期でawaitを挟まない）が、操作ログや選択状態が意図と違う順序で反映され
  うる。**この前提を変えるなら `duocad/net/ws.py` の `Server` に排他を入れるのが先。**

### モジュールとimport境界

**`duocad/` 配下で `OCP` を import してよいのは `kernel.py` だけ、`build123d` は `query.py` だけ。**
`tests/test_boundaries.py` がソースを再帰的に静的走査して機械的に検査する（動的importも検出）。
**verbがカーネルを直接叩き始めるのを防ぐ唯一の安全網。**

| モジュール | 役割 | OCP | build123d |
|---|---|---|---|
| `duocad/document.py` | `Document` / `Body` のデータモデル | ✗ | ✗ |
| `duocad/oplog.py` | `Operation` / `OpLog`（undo・再生・永続化。**verb名の分岐を書かない**＝レジストリ） | ✗ | ✗ |
| `duocad/sketch.py` | `Sketch` / `Entity` / `Pattern` とスケッチのverb | ✗ | ✗ |
| `duocad/region.py` | 閉領域の検出（`BRepAlgoAPI_Splitter`）。領域は**描くものではなく計算されるもの** | ✗ | ✗ |
| `duocad/parameters.py` | 名前つきの寸法値・`set_parameter`・ログからの再生 | ✗ | ✗ |
| `duocad/selection.py` | 選択集合（人のクリック側。可視性を含む「何が選べるか」） | ✗ | ✗ |
| `duocad/predicates.py` | **述語による選択（AI側）**。構造化JSONでDSL文字列ではない | ✗ | ✗ |
| `duocad/speculative.py` | プレビュー（確定前にverbを投機的に走らせて捨てる） | ✗ | ✗ |
| `duocad/verbs/*.py` | verb層（1 verb 1モジュール・純関数） | ✗ | ✗ |
| `duocad/catalog.py` | **verbの単一宣言**（名前・引数・カテゴリ・プレビュー可否・MCPスキーマ）。下の2つが持つ表はここからの導出 | ✗ | ✗ |
| `duocad/svg_import.py` | SVG → スケッチの図形（**パーサはbuild123dの推移依存で既に入っている** svgelements） | ✗ | ✗ |
| `duocad/query.py` | 問い合わせ層のインタフェース | ✗ | **○ここだけ** |
| `duocad/kernel.py` | OCCTを直接叩く層（1637行。テセレーションもここ） | **○ここだけ** | ✗ |
| `duocad/session.py` | `Session`（1つの文書を持つサーバ側の状態。verbの呼び出し・選択・保存/開く。**配りものは持たず `view` へ委譲**） | ✗ | ✗ |
| `duocad/view.py` | `DocumentView`（**配りもの**＝状態配信・版・テセレーション/辺/頂点/体積/スケッチのキャッシュ。文書を変えない） | ✗ | ✗ |
| `duocad/paths.py` | `REPO_ROOT` と、そこから出る既定の置き場（`web/dist` / `models`） | ✗ | ✗ |
| `duocad/net/ws.py` | `Server`（WebSocketの配信と `_dispatch`）＋ソケットから踏めるコマンドの表 | ✗ | ✗ |
| `duocad/net/http.py` | `web/dist` の静的配信と `/mcp`（MCPのHTTPトランスポート） | ✗ | ✗ |
| `duocad/net/tools_on_loop.py` | `ToolsOnLoop`（別スレッドからの道具呼び出しをイベントループへ限定する覆い） | ✗ | ✗ |
| `duocad/server.py` | **入口**（上の6つの再輸出。`mcp_server.py` とテストはここから名前を取る） | ✗ | ✗ |
| `duocad/mcp_server.py` | MCPサーバ。**アプリ層ではなくコアのもう一人のクライアント** | ✗ | ✗ |
| `duocad/agent/` | **アプリ内AIエージェント**（プロバイダのアダプタ＋tool-useループ。UIは持たない） | ✗ | ✗ |

**`duocad/net/` → `duocad/session.py` → `duocad/view.py` の一方向**（`session.py` から `net/` を参照せず、`view.py` から `session.py` を参照しない）。`DocumentView` が配る封筒のうち**持ち主が `Session` の値**（`can_undo` / `applied_count` / 「いまプレビュー中か」）は、`Session._view_inputs()` が引数で渡す（`view` 側に旗を複製しない）。唯一の逆向きは `Server._ensure_ai_loop` が `build_provider` を**入口（`duocad.server`）越しに引く**ところで、これはプロバイダの差し替え点を `duocad.server.build_provider` に固定するため（`tests/test_agent_ws.py` がそこを monkeypatch する。直接importすると差し替えが黙って効かなくなる）。呼び出し時まで遅らせてあるのでimport時の循環にはならない。

### frontend（`web/`）

**Svelte 5 + TypeScript + Vite。SvelteKitは使わない**（2026年の破壊的変更はほぼ全てSvelteKit側で、
使わなければ年1回ではなく5年周期の側に乗る）。CSSはバニラ（CSS変数）。

**守るべき唯一のルール: canvasはフレームワークの外に置く。** three.jsが所有するコンテナを渡し、
Svelteは二度と中を触らない。また `viewport.ts` を狭いインタフェースにして
**three.jsの型を外へ漏らさない**（最もchurnの激しい依存を1ファイルに閉じる）。

| ファイル | 役割 |
|---|---|
| `web/src/App.svelte` | 画面全体の組み立て・選択の規則・キー処理（1021行） |
| `web/src/viewport.ts` | three.js の囲い（load / onPick / highlight / project …） |
| `web/src/session.ts` | WebSocketクライアント。**WSのURLはサーバが `/duocad-config.json` で教える**（ポートを推測しない） |
| `web/src/commands/CommandHost.svelte` | **コマンド層の骨格**（起動→引数収集→プレビュー→Enter/Esc、**および即適用の発火**）。verbごとに作り直さない |
| `web/src/commands/{command,sketch,features,files}.ts` | 各コマンドの引数の集め方の定義だけ（**Enter要否はここから導出**＝`needsEnter`） |
| `web/src/{Browser,Files,Sketch,Parameters}Panel.svelte` | 各パネル（コアが持つものを描き直すだけ）。**選択パネルは無い**（選択を直すのは3Dクリックだけ） |
| `web/src/Toast.svelte` | **エラーの札**。`showToast(message)` の1本だけを公開し、出すのは `App.svelte` の `fail()` から |
| `web/src/AiPanel.svelte` | **AIパネル**。プロバイダ選択・対話・ツール呼び出しの進捗・中断。文書は描かない。**`#ai-collapse`(`BrowserPanel.svelte`と同じ形)で畳める** |
| `web/src/SettingsDialog.svelte` | **設定ダイアログ**。native `<dialog>` + `showModal()`。**一度決めたら触らない設定だけ**（いまは画角と正投影）。ビューポートを覆わず暗くもしない |
| `web/src/viewSettings.ts` | **画面設定のlocalStorage**。入れてよいのはクライアント側だけの設定で、**文書の状態は入れない**。読み書きとも例外を投げない |

### AI側（MCP・36ツール）

**ツールの一覧は [MCP.md](MCP.md) が正**（ここに写すと、増減のたびに2箇所を直すことになる）。
機械の正は `tests/test_mcp_verbs.py` の `EXPECTED_TOOLS` で、**顔ぶれを一致で固定**している。

設計として押さえるのはこの4点:

- **面・辺・領域は引数で受けない。確定した選択から取る**（数値・名前・パスだけが引数）。
  提案状態の選択はそのままverbに渡せず、先に `commit_selection` が要る。**WebSocketのコマンドも
  同じ形**。`sketch_create` も MCPと同じく `base_plane` だけを取り、省略時は確定選択の面
  （ちょうど1枚）に貼る
- **ツール名 == WebSocketのコマンド名 == UIのコマンドID == 操作ログのverb名**。
  AIは `describe_document` のログを読んで次の一手を決めるので、綴りが違うと対応表を
  推測させることになる。**1つのツールを引数で複数のverbに打ち分けない**のはこの規律の帰結
- 述語は構造化JSON: `{"kind":"edge","where":[{"op":"parallel_to","axis":"Z"},{"op":"length_gt","value":20}]}`
  （`where` はAND。**語彙は `describe_predicates` が自己記述する**ので、ここには写さない）
- 名前の一致は両方向のテストが固定する
  （`test_tool_names_match_the_verb_names_in_the_operation_log` と
  `tests/test_ui_command_wiring.py`）

公式SDKは入れず、JSON-RPC 2.0 を素で実装している（依存を増やしていない）。**トランスポートは
stdioとHTTP（`/mcp`）の2本**で、`McpServer.handle()` はどちらも知らない —— 1メッセージの辞書を
受け取って返すだけなので、口が増えても中身は増えない（契約は `tests/test_mcp_http.py`）。

### AI側（アプリ内エージェント・`duocad/agent/`）

**AIから文書を触る道は2本ある**。`duocad/agent/` は**先にアプリが立っていて、アプリの中から
AIを選んで繋ぐ**（今あるのは llama-server の1つ）。**MCP経路は残す** —— Claude Codeから
触る道とアプリ内の道は共存する。どちらも「先にアプリが立っている」という同じ向きに揃っている。

- **道具の定義を第二の場所に書かない** —— `DocumentTools.descriptors()` を渡し、`.call()` で実行し、
  失敗は `.error_envelope()` の6語彙。MCP経路と顔ぶれ・挙動・失敗の形が同一である
- **システムプロンプトも二重に持たない** —— `catalog.agent_instructions()` の1本をMCPの
  `instructions` とエージェントの両方が読む（ツール数も宣言から数える）
- **暴走は必ず打ち切る** —— `max_turns` / `max_tokens` / 1リクエストのタイムアウト / 外から立てる中断フラグ。
  打ち切った理由は構造化して返す（矛盾した指示を渡すと**思考**が膨らみ、1リクエストが600秒返らなかった実測がある）
- **同時編集は楽観ロックで守る** —— モデルへの1往復を送る前に文書の版
  （`DocumentTools.current_seq()`）を控え、応答が返ったとき版がずれていたら、そのツール呼び出しは
  実行せず「文書が変わったので選び直せ」と返す。形状を変えるverbの確定後は確定選択が全リセットされる
  （`Session._reset_selection_if_needed`）ので大半は元から `selection_empty` で止まっていたが、
  `set_parameter` のように選択を経由しないverbはこの網に掛からず、楽観ロックがその穴を埋める
  （`fillet`/`chamfer`/`extrude` は対象ボディの可視性も実行時に見直す）。
  **版は「ログの本数・選択の指紋・可視性の指紋」の3つ組** —— 可視性は操作ログに
  載らないので、指紋を持たないと「AIが考えている間に人がボディを隠した」が版に出ない
- **鍵を持つ層ではない。** `build_provider` はプロバイダへ
  鍵を渡さないので、`base_url` をどこへ向けても飛ぶのはキーの無いリクエストだけになる。
  **鍵を要るプロバイダを足すなら、`Server._ai_configure` に `base_url` の拒否を書き戻すこと**
- 依存は増やしていない（公式SDKを入れず `urllib` で素のHTTP）

契約は `tests/test_agent_loop.py`（偽プロバイダで決定的に検査）。実機は
`tests/test_agent_llama_server.py` の1本だけで、**8080にllama-serverが居なければskipする**。

**WebSocketへの配線。** `duocad/net/ws.py` が `ai_get_status` / `ai_configure` /
`ai_send` / `ai_cancel` の4つを持ち、画面右サイドバーの `web/src/AiPanel.svelte` から叩く。

- **`ai_*` はverbではない**。文書を直接は変えない(操作ログに載らずundoの単位でもない)ので
  `duocad/catalog.py` の `COMMANDS`(「MCPツール名==WSコマンド名==UIコマンドID==verb名」の
  一致規律)には入れない。対応するMCPツールも作らない(AIがAIを起動する口は要らない)。
  除外は `tests/test_ui_command_wiring.py::test_ai_commands_are_not_verbs` と
  `tests/test_mcp_verbs.py::test_ai_commands_have_no_mcp_tool` が固定する
- **`ai_send` はモデルへのHTTPリクエスト(数十秒かかりうる)を待たずに返る** ——
  `AgentLoop.run()` を `run_in_executor` で実行器スレッドへ逃がし、進捗
  (`ai_tool_call`)と最終応答(`ai_result`)は別イベントとして後から届く
  (`Server._ai_start` のdocstring)
- **`Session` に触るのは依然イベントループのスレッドだけ** —— 実行器スレッドから
  `DocumentTools` を直接叩くと、WebSocket/MCPと同時に2スレッドが文書を書き換えうる。
  `Server._AiToolsOnLoop` が `.call()` / `.error_envelope()` / `.current_seq()` を1回ずつ
  `asyncio.run_coroutine_threadsafe` でイベントループへ投げ返し、この不変条件を保つ
  (`mcp_server.py` 冒頭の「既知の割り切り」を作り直さずに済む形)
- **AIがツールを呼んで文書を変えると、通常のverb配信に乗って開いている画面が変わる**
  (`applied` は付かない。MCPが変えたときと同じ扱い)
- 中断は `ai_cancel`。走っているHTTPリクエスト自体は切れないので、反映は次の
  チェックポイント(モデルへ投げる前・ツール1件ごと)まで遅れうる —— 「どこまでやったか」は
  `ai_result.tool_calls` に残る

契約は `tests/test_agent_ws.py`(偽プロバイダで `duocad.server.build_provider` を差し替え、
実キー・実HTTPなしで決定的に検査)。

---

## 5. リポジトリの地図

```
duocad/
  .mcp.json              Claude Code向けのMCP登録（projectスコープ。これがあるので登録作業が要らない）
  README.md              ← このファイル（入口）
  MCP.md                 MCPサーバの仕様と登録手順
  docs/                  IMPLEMENTATION_PLAN(現行の正) / REQUIREMENTS
  duocad/                コア（Python）。verbs/ に1 verb 1モジュール
                         session.py=1つの文書を持つサーバ側の状態／view.py=その配りもの
                         （状態配信・版・キャッシュ。session.py が1つ持つ）
                         net/ はアプリ層との接点（ws.py=WebSocket／http.py=静的配信と/mcp／
                         tools_on_loop.py）。server.py はその入口で、再輸出と起動の番人だけ
                         agent/ はアプリ内AIエージェント（プロバイダ＋ループ・UIなし）
  tests/                 854件。README.md はモジュール単位のAPI契約リファレンス
  web/                   frontend（Svelte 5 + Vite）
    src/                 画面
    scripts/             実ブラウザの検査（smoke*.mjs / acceptance.mjs）。共有部品は
                         server.mjs（サーバの起動・停止）／browser.mjs（ブラウザ選択・起動）／
                         mcp-client.mjs（MCPクライアント）／args.mjs／models.mjs（STEPのパス）
    dist/                ビルド成果物（**Pythonサーバが配信するのはこちら**・git管理外）
  .venv/                 Python 3.11（git管理外）
  pyproject.toml         依存はバージョン固定（ローカル専用なので上げなければ壊れない）
```

サンプルのSTEPは `C:\Users\tm976\mf\3d-models\` にある
（`mATX_PC_case_20251122\PC_case_20251122.step`（27部品）／同 `PC_case_feet.step`（5面）／
`stackable_plate\plate_1by1_24.step`（74面）／`shelf_attachment_GD.step`（56面））。
**読み取り専用で扱うこと。**

---

## 6. 既知の落とし穴

### 運用の注意

- **duocadのサーバを2つ同時に立てない**（8765/8766がぶつかり、後から立った方がWebSocketのbindで落ちる）。
  **Claude CodeがMCPをHTTPで繋ぐ限りはこれを起こさない**（クライアントはduocadを起動しない）
  —— ただし**stdioの登録が残っているとまだ起きる**（§1.2）。
- **`/mcp`（8766）はブラウザから叩けない。** `Origin` を持つリクエストと、
  `127.0.0.1:<port>` / `localhost:<port>` 以外の `Host` を403で落とす。**認証トークンは無い** ——
  このPCでコードを実行できる相手はトークンを書いたファイルも読めるので、守れる相手が増えない
  （下のWebSocketのOrigin検査と同じ線引き）。契約は `tests/test_mcp_http.py`。
- **WebSocket(8765)はOriginを検査する。** 繋げるのはduocad自身が配るページだけ
  ——静的配信（`--http-port`、既定8766）と Vite dev server（5173）を、`127.0.0.1` / `localhost`
  の両方の綴りで許可する。**`Origin` ヘッダを送らないクライアント（`smoke*.mjs` などの素のWS）は
  今までどおり繋がる**（意図した許可。理由は `tests/test_ws_origin.py` 冒頭）。
  WebSocketは同一オリジンポリシーの対象外なので、検査が無いと**duocadを立てたまま別のサイトを
  開いているだけで**、そのページが文書を読み・壊し・`export_step` で任意のパスへ書き・`ai_chat` で
  課金を消費できた。`--http-port` を既定から変えると許可リストもそれに追随する。
- **`web/dist` を作り直さないとUIの変更が画面に出ない**（`npm run build`）。
  `dist` が無くても `mcp_server` は**静的配信だけ諦めて起動する**ので、
  「立ったのに画面が出ない」という形で出る（理由はstderrに1行出ている）。
- **文書は接続より長生きする。** ブラウザを閉じても消えないので、まっさらから試すときはサーバを立て直す。
- **読み込んだSTEPはそのまま編集できる。** 参照ボディ（原本）は隠されていて
  画面からは指せない —— 触れるのは編集用ボディの方である。
- **duocadには改名のverbが無い**（どの種別のボディにも。2026-09-05時点）。ボディの名前は
  作るときに決まる（`add_mesh_body(name=...)` / STEP読み込みは `<ファイル名>_N`）。

### 実測で判明している技術的な地雷（コード側は対処済み。数値を疑うとき用）

- **OCCTのフィレットは例外を投げずに失敗する**（20mm立方体・全辺で r=10.0 は失敗、**r=10.5 は成功**、r=30.0 は失敗。
  **半径に対して単調ですらない**）→ verbのラッパで毎回 `IsDone()` を確認する。
- **面の添字は形状変更で壊れる**（同じ面がフィレットで i=5→9。ただし穴の径変更等では動かない＝**散発的に静かに間違う**）
  → 生の添字を保存・再利用しない。由来スロットを day1 から用意してある。
- **STEP往復の体積は bit-exact ではない**（相対 約8e-16 の1ULP級）→ 比較は `math.isclose(rel_tol=1e-6)`。
- **STLの偏差は絶対値（`isRelative=False`）で渡す** → 相対にすると偏差を1/20にしてもメッシュが1バイトも変わらない。
- **STEP読み込みは `ReadFile()` の戻り値を検査してから `TransferRoots()`** → 怠るとPython例外ではなく
  **プロセスごと落ちる**（0xC0000005）。
- **OCCTのSTEP書き出しはC++からstdoutへ統計を吐く** → MCPのJSON-RPCへ割り込んで接続が死ぬので、
  起動時に fd 1 を fd 2 へ振り替えてある（`_claim_stdout`）。

### 現行verbの制約

- **面取りは距離1つ（＝45度）しか取れない。** PCケースの脚の円錐は半径方向4mm・軸方向22mmの
  非対称な面取りなので、**面の種類は作れても寸法までは再現できない**。
- 拘束ソルバは持たない（各図形が自分の数値を持つパラメトリック・プリミティブで足りている）。
  次に足すなら `python-solvespace`（実測済み・GPLv3+）。

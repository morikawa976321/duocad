# duocad — 開発環境と運用のメモ

**読むとき**: 環境（venv・行末・git）でつまずいたとき、コメントや文書を減量するとき、デッドコードを洗い出すとき、タスクを切るとき。

## 環境

- **数値計算（numpy / scipy）は `.venv` にしか無い。** 既定の `python`（3.14）には無く、matplotlib はどの venv にも無い
  （図が要るなら標準ライブラリだけで PNG/SVG を書く。マスクの画像化は Pillow の `topopt_checks/render_mask_png.py`）。実行は `.\.venv\Scripts\python.exe`。
  逆にシステム側にだけある依存もある（2026-10-06: manifold3d がシステムの 3.14 にだけ入っていた）ので、素の `python` で
  import できても製品・テストが venv で動く保証にならない。
- **venv の python は1本がプロセス2つに見える。** uv が作った `.venv\Scripts\python.exe` はランチャで、実体
  `%APPDATA%\uv\python\cpython-3.14-*\python.exe` を子として起こすので、プロセス一覧に同じコマンドラインが2つ、作成時刻が
  同じ秒で出る（`sys.executable` を呼ぶ子プロセスも2つずつ）。二重起動を疑うときは親子関係（ppid）と作成時刻を見る
  （2026-10-07 に本人の検証を二重起動と誤認して片方を止め、検証全体が止まった）。
- **Git Bash の `python` は日本語入りのファイルを cp932 で読む。** `-X utf8` か `encoding="utf-8"`。
- **OCCT の重い処理を大きい形で総当たりするときは `tools/capped_run.py --gb 3 -- <コマンド>` で走らせる。** 上限を超えた
  割り当ては失敗して子だけが終わる。2026-10-22 にフィレットの吸収を PC ケースの部品で総当たりしたら python が仮想メモリを
  84GB 使い、PC がブルースクリーンで落ちた(イベントログの Resource-Exhaustion-Detector に PID と使用量が残る)。
  venv の python はランチャなので、走り出してからジョブに入れると実体がジョブの外に出うる(このツールは停止状態で作ってから入れる)。
- **形状探索の FEM を並べると OOM になる。** RAM 31.7 GB で普段 10〜15 GB は他のアプリが使う。hanger の1条件のピークは 2.388 mm で 7.5 GB〜1.592 mm で 24.2 GB。
  2本以上のピークが重なると落ちる（2026-09-26 に PC が落ちた）。
- **PowerShell の `Invoke-CimMethod -ClassName Win32_Process -MethodName Create` で起動した独立プロセスは、Claude のアプリを閉じても続く**（親が `WmiPrvSE` になる）。
- **`.venv` は `uv sync --frozen` で `uv.lock` から作り直せる。** 依存は `uv add` で入れる。`uv pip install` で手入れした
  ものは作り直すと消え、収集エラー（`No module named ...`）で初めて気づく。Python の版が変わったときは venv ごと
  作り直すしかない（2026-10-10 の 3.11→3.14 は `uv sync --frozen` で作り直した）。`pyvenv.cfg` まで消えて作り直せない
  ときは、uv の python（`%APPDATA%\uv\python\cpython-<requires-python の版>-*`）を `home` にして書き戻す。
- **同じリポジトリ内でも行末はファイルごとに違う。** `core.autocrlf=true` で blob は LF だが、作業ツリーはツールが直接書いた
  ファイルが LF、checkout を経たファイルが CRLF。`git diff` も `git status` も差分を出さない。踏んだ形: 複数行の完全一致置換が
  0件で落ちる／変異検査で置換が0件のまま「変異を入れても緑」＝「テストが噛んでいない」という**逆の結論**が出る／
  `git checkout -- <path>` が1ファイルだけ CRLF で書き戻して `line == '}'` の完全一致が `'}\r'` で外れる。
  守り方: 置換は**必ず件数を assert**／変異チェックで緑が出たらテストを疑う前に `git diff` で変異が入ったかを見る／検出は
  `python -c "d=open(p,'rb').read(); print(d.count(b'\r\n'), d.count(b'\n'))"`（Git Bash の `grep -c $'\r$'` は当てにならない）／
  `newline="\n"` で保存すると作業ツリーだけ LF になり並行セッションの差分表示が汚れる。
- **ソースに生の NUL バイトを書くと git がバイナリ扱いにする**（`git diff` / `blame` が効かず `core.autocrlf` の正規化も止まる）。
  Unicode エスケープにする。見つけ方は `git ls-files --eol` に `i/-text` が出るテキストファイル、`git diff --stat` と
  `--ignore-all-space --stat` が桁違い。
- **Git Bash から Windows の python に `/c/Users/...` のパスを渡すと、`sys.path.insert(0, '/c/...')` は黙って無効になる。**
  editable install が本体チェックアウトを指すので、旧コードの `git archive` と比べたつもりが本体どうしの比較になる。
  `cygpath -m` で `C:/...` にして渡し、`module.__file__` で読み込み元を確かめる（`PYTHONPATH` も editable install に負ける）。
  別セッションが同じチェックアウトの製品コードを書き換えている最中に測ると、その未コミット変更を読む。測定の前後で
  `git status --short -- duocad` を見て、固定したいときは `git archive` の展開を `sys.path.insert(0, ...)` で import する。
- **GitHub リモートは private で公開しない**（[DESIGN_DECISIONS.md](DESIGN_DECISIONS.md)）。

## 機械的な一括編集の道具

- **行番号指定でコメント/docstring を一括置換する道具には4つ要る**: (1) 行末を保存する（バイト列で `split(b"\n")` して `\r` を
  行の一部として運ぶ）(2) docstring の閉じ引用符を範囲に含める (3) 書き込み**前**に `ast.parse` (4) 置換先の1行目（できれば
  最終行も）を照合する（1行ずれても構文が通り、同じコメントが2行重複する）。
- **Python の関数を機械的に消すときは正規表現ではなく `ast`。** 複数行デコレータ（`@pytest.mark.parametrize(` … `)`）で
  `)` の手前で止まり、残ったデコレータが次の関数に付く（構文は通る）。`decorator_list` の最小 `lineno` から `end_lineno` まで。
- **既存呼び出しを新ラッパーへ一括置換すると、ラッパー内の元関数呼び出しまで置換されて無限相互再帰になる**（件数を assert
  していても巻き込んだ分で数が合う）。非同期だと例外もタイムアウトも出ず低 CPU で回り続ける。置換後にラッパー本体を目で読む。
- **引数を1つ減らすリファクタは、その引数をキーにしていたキャッシュ・重複送信よけ・デバウンス判定を黙って無効化する**
  （フィレットの引数から辺の集合を落としたら、プレビュー再送判定が `JSON.stringify([verb, args])` を印にしていたため辺を
  足しても印が変わらなくなった）。消す前に (1) キャッシュキー (2) 重複送信よけ (3) 再計算トリガ への使用を grep し、
  代わりに新しい真実源の指紋を印へ足す（`CommandHost` の `selectionPrint`）。
- **クラスを割って可変リストを共有させるとき、`x, self.x = self.x, []` の束ね直しが共有を静かに切る**
  （`Session.take_warnings()`。中身を `clear()` する）。「共有が続いていること」を見る検査を足す。
- **plan-then-write は同じオブジェクトへの複数の計画を併合しないと最後だけが勝つ**（`move_sketch_entities` で線分の両端を
  選ぶと片方しか動かなかった）。計画の単位は書き込みの単位。「2つ動かして両方動く」テストが要る。
- **「既定を置き換える引数」に「既定へ追加したいもの」を相乗りさせない**（`fixed_dofs` に内部計算の拘束を代入したら既定の
  拘束がまるごと消えた。`extra_fixed_dofs` として別の引数にする）。

## コメントと文書の減量

規律は [../CLAUDE.md](../CLAUDE.md)。判定基準:

- **日付＋実測が同じ括弧に同居するとき、日付だけを消し数値は残す。** 逆に数値ごと消すのも誤り。発見者・発見日の枕詞も、
  後ろの再現手順だけ残せば自立する。
- **経緯のパラグラフを削る前に、中に埋まっている参照（`[[…]]` / `engine \`…\``）を grep する。** context-engine のノートは
  各リポジトリの文書へ移設済みで、コード中の `[[…]]` 参照の行き先は `docs/` の文書群。
- **「複数ファイルへ同じ理屈を書く重複」は同一ファイル内でも起きる。** 宣言部に全文を残し、他は参照に圧縮する。
  1,500行超のファイルでは減量の完了条件に「同一ファイル内の重複説明が無いこと」を含める。
- **減量は流入を変えなければ戻る**（実測: 減量5コミットで 32.9%→29.3%、13日後に 31.1%）。ファイルサイズも片道
  （`viewport.ts` 388→5,155行、`App.svelte` 950→2,461、`kernel.py` 298→2,052）。肥大の主因は無人実行 taskbox の積み重ね
  （各 runner が経緯・正当化を書き残す癖）。**AI にコードを書かせるならコメント規律は最初からリポジトリ規約（CLAUDE.md）に
  入れておく。**
- **文書は「行」でなく「節」の単位で増える**（`docs/TOPOPT.md` は −173行の9時間後に機能タスク1本が +397行）。反復する節
  （実測値・基準線・案N）は表に畳み「新見出しを立てず表の末尾に行を足す」と契約に書く。
- **正を宣言してもコピーは腐る。** 重複を見つけたら正を選ぶだけでなく**コピーを消して参照に置き換える**。
- **文書に写さない型**（書き直すと次の同期漏れの口が増えるので、消して「正は○○」に置き換える）: (1) 件数（同じ文の中に列挙が
  並記されて検算できるものは残してよい）(2) 定数値・列挙の転記（定数の写しでない**実測**は残す）(3) 実装の現状を数え上げた否定
  （「改名の verb が無い」は黙って嘘になる。「そうしないと決めた」という決定の否定〈「拘束ソルバは持たない」〉は残す）
  (4) 実行出力の貼り付け (5) 外部ファイルの属性（サンプル STEP の「27部品」。ディレクトリを指すだけに縮める）。
- **文書をコードと機械照合する対象は3条件すべてを満たすものだけ**（`tests/test_doc_code_agreement.py`）: (i) 読み手がその文書を
  信じて行動する (ii) コード側に機械で1点に引ける正がある (iii) 食い違っても何も落ちない。(ii)を欠く（smoke 一覧・モジュール表・
  `Entity.params` のキー）ものと(iii)を欠く（コード側が既にテストで固定）ものは意図して押さえていない。期待値を検査側へ書き写して
  緑にしない（正はコード）。
- **文書の見出しは「動かせない面」**。参照は行番号でなく見出し名で書き、見出しを改名・統合・分割するときは
  `grep -rn "<見出し文字列>" .`（docs・`duocad/` のコメント・`topopt_checks/`・README）で指す参照を洗って同じ変更で直す。
  コメントの引用句が見出しでなく本文の太字に一致して成立していることもあるので、ダングリング判定の前に太字も検索する。
- **一括掃除の削減率を次回に当てない**（初回の 14.7% を掛けた見積もり −3,000行に対し実績 −232行）。**行数を成果の指標に
  しない。**
- 率・行数の門は作らない（本人 2026-09-05「価値あるコメントが増える類のタスクもある」）。門を作ると実測値の1行メモが経緯の
  語りより先に消える。`tools/measure_bloat.py` は報告専用。

## デッドコードの洗い出し（`tools/dead_code/`）

vulture 等は入れていない。標準ライブラリの `ast` と TypeScript の AST で作った**候補の生成器**を置いてある（結果は `output/dead_code/`、git 管理外）:

```powershell
.\.venv\Scripts\python.exe tools\dead_code\sweep.py     # Python: 名前ベースの到達性（固定点）・未使用 import/引数/ローカル・孤立モジュール
node tools\dead_code\ts_sweep.mjs                        # web/src・web/scripts・tests/*.mjs の宣言とメンバの到達性
.\.venv\Scripts\python.exe tools\dead_code\dangling.py  # 存在しない名前・モジュールへの import
npm run check --prefix web                               # tsc と svelte-check（noUnusedLocals）
```

エージェントに精読と反証をさせるなら、`areas.py` で領域ごとの brief を作り、`workflow.js` を Workflow ツールへ渡す（`args` の形は脚本の冒頭）。

- **候補は確定ではない。** 2026-10-18 の初回は機械候補の約7割が誤検出だった。型: 同名の別実体への参照（`weights`・`render`・`flat`）／`@entry`・`register_verb`・`getattr(self, ツール名)`・catalog の文字列がメソッド名・dataclass の `asdict` と trace の `_encode` で動的に生きる／呼び元が1か所でも、その呼び元が生きている private ヘルパ／基底クラスのフック（`WebHandler.log_message`）やコールバック型に合わせた未使用引数／Svelte のテンプレートからの参照。
- **消す前に独立に反証させる。** 「意図的な残置」は docs やコメントに書かれていることが多い（旧 SIMP 引数の拒否は `docs/TOPOPT.md` が契約、`server.py` の再輸出はコメントが「名前を減らさない」と明言）。動的参照の視点と履歴・意図の視点の2つを通ったものだけを確定にした。
- **`window.__duocad` のキー（`web/src/testHooks.svelte.ts`）は、`web/scripts/*.mjs` と `tests/` が読むものだけを置く**（2026-10-18 本人決定: 読み手の無いキーは docs が名指ししていても消す。足すときは読む本と一緒に足す）。ピックを見る `project*`・`hoveredRef`・`pickKinds`・`edges` と、待ち受けを見る `wanted*`・`wantsBasePlanes` は `web/scripts/smoke-pick-everything.mjs` ほかが読む。
- **線引き**: 本番・テスト・検査スクリプトのどこからも参照されないものを消す。テストだけ／検査スクリプトだけが使うもの、docs が名指しするもの、保存形式・WebSocket の payload・trace の記録に載るフィールド、既定値のまま誰も変えない引数は、初回は本人判断として残したが、2026-10-18 に本人が「迷ったらシンプルな方で削除」と決めたので、呼び出しを AST で全数確かめたうえで消した（直すテストは直す）。いま残っている「消せるが消していない」ものは、テストが別の値を渡している引数と、型検査の都合で渡している引数（`restartServer` の `fail`: `tsc` が `.svelte` の名前つき export を読めない）。

## 測定記録（`output/`）の書き方

**リポジトリ直下の `output/` は git 管理外**（`.gitignore` の `/output/`。`duocad/topopt/output/` の検査成果物は追跡する）。
文書は結論・表・再現コマンドを持ち、生データは持たない —— 生データは再実行で作り直す。**文書に貼る図は `docs/images/` へ置く**
（`output/` に置くと文書だけ残って図が消える）。

`topopt_checks/` の記録は **`_record.py` の `dumps` / `write` で書く**（`json.dumps(..., indent=N)` を直接使わない）。
600文字に収まる辞書・配列を1行にまとめるので、試行1件が1行になり差分が試行単位で読める。`indent=2` だと追跡中の測定記録22件が
174,841 行、コンパクト形で 13,343 行（−92.4%）で、記録の 98〜99% は `search.diagnostics.reduction.steps` と `search.history` の
2配列。

- **同じ結果しか持たない実行を別ファイルでコミットしない**（`patience-16` と `patience-unlimited` は秒と記録したパラメータ
  以外まったく同一。`B-*` と `B-*-recheck` も同様）。
- 外せる重複（外せばコンパクト形で −37%）: `steps[].step_ratio`（= `gap_step_ratio × multiplier`）、
  `safety_factor_before` / `material_mm3_before`（直前の採用 step の値）、常に長さ1で誰も読まない `trials`、
  `steps[].failed` と同じ情報の `history[].checks`。

## タスクの切り方（サブエージェントに渡すとき）

- **verb を1つ足したら宣言（`Command.returns` まで）を書いて `python tools/generate_protocol_ts.py` を走らせる。** 画面の
  `Applied` はその生成物なので、バックエンドだけのタスクでも `tests/test_protocol.py` は自分で緑にできる。`state_meta` にキーを
  足すときだけ `web/src/session.ts` の `ServerState` も直す。**宣言・型・種別の表＝バックエンド／引数の集め方・描画＝フロント**で
  切る。`commands/command.ts` の `SelectableKind` / `KIND_COUNTERS` はコアの `SELECTABLE_KINDS` の写しで、足し忘れると件数表示が
  `undefined` になる（`ref as ShapeRef` のキャストで書かれているため `tsc` では出ない）。
- **`ws="none"`（MCP 専用）の verb は画面の `Applied` に載らない。** ブラウザに開けていないのに操作ログに載る verb
  （`add_mesh_body` / `mirror_sketch_entities`）は全ブラウザへ状態配信されるが、型が無くても検査は緑。足すとき `Applied` を省略
  してよいかは都度判断し、省略するなら理由を宣言に書く。
- **文書の置き場は「誰が直せるか」で決める。** AI の作業ディレクトリの外にある文書は実装が進むたびに古くなる。コードと同じ
  リポジトリなら同じコミットで直せる。日付入りのファイル名は継続改訂する文書に向かない。
- **「直した」と「記録を直した」は別の作業**で後者は落ちやすい。同じコミットで問題を直しているとき、直前に自分が書いた
  「未決」が視界から外れる。
- **MCP サーバは catalog を変えたら立て直す**（`delete` 追加直後に `unknown command`）。
- **状態変更の配信を「直後の別コマンドの配信」に相乗りさせない**（`duocad/net/ws.py` の自動 commit）。配信しない種類のコマンド
  （`preview`）が同じ入口を通った瞬間に破れ、例外は出ない。検査は**2接続**で書く（1接続だと要求元には結果が返るので緑に見える）。
  自動で起こした変更に人の操作 ID を載せない。

## ワークツリー（既定では使わない）

**本人指示: ワークツリーは作らない。** 作業は本体チェックアウトの develop で行う（並行セッションのワークツリーが増えると変更が
develop に集まらず、ジャンクション経由で本体の `.venv` を消す事故も起きた）。以下は、本人が明示的に「ワークツリーで」と言った
ときのための落とし穴。ワークツリーのブランチは develop から切り、develop へ戻す。

- **セッションが動いているワークツリーは消せない。** Windows ではそのセッションのシェルがディレクトリを掴んでいて
  `git worktree remove` が `Permission denied` になる。**そのセッションを閉じてから** `git worktree prune` と空ディレクトリの
  削除で片付ける。
- **ジャンクションを張る。** 新しいワークツリーには gitignore 済みの `.venv` と `web/node_modules` が無く、`npm run check` は
  依存が解決できず、`web/scripts/server.mjs` は `REPO_ROOT/.venv/Scripts/python.exe` を決め打ちなので「venv の python が無い」で
  smoke が1本も走らない:
  ```powershell
  New-Item -ItemType Junction -Path <worktree>\.venv -Target C:\Users\tm976\mf\duocad\.venv
  New-Item -ItemType Junction -Path <worktree>\web\node_modules -Target C:\Users\tm976\mf\duocad\web\node_modules
  ```
  管理者権限不要。git は無視するので `git status` に出ない。smoke が配る画面は `web/dist` なので、ワークツリーで先に
  `npm run build` を回す。pytest は本体の `.venv\Scripts\python.exe -m pytest` をワークツリーの cwd で呼べば足りる。
- **ワークツリーを消す前にジャンクションを先に外す。** ジャンクション入りのワークツリーを削除した処理がジャンクションを辿り、
  **本体の `.venv` の中身を丸ごと消した**（`uv sync --frozen` で作り直した）。外すのは PowerShell の
  `[System.IO.Directory]::Delete('<worktree>\.venv', $false)`（本体の `.venv` が残ることを実測済み）。再帰削除
  （`Remove-Item -Recurse`、エクスプローラーでの削除）をジャンクション入りのワークツリーへ向けない。
- **Codex アプリのワークツリー（`%USERPROFILE%\.codex\worktrees\<名前>\duocad`）の前後でも本体の環境が消えた。**
  2026-10-24、Codex が上の2本のジャンクションを張って #229 を作業した間に、本体の `.venv` が `Lib` と `pyvenv.cfg` を失い（00:35）、
  そのワークツリーが消えた時刻（00:50）に `web/node_modules` が空になった。経路は未確定。Codex に duocad を触らせた後は、
  本体の `.venv\pyvenv.cfg` と `web\node_modules\typescript` があるかを先に見る。戻し方は
  `uv venv --allow-existing --python 3.14 .venv` → `uv sync --frozen --extra gpu`（この機械は GPU の部品を入れている。
  `--extra gpu` を落とすと cupy が消える）と、`web` での `npm ci`。
- **素の python スクリプトは本体を import する。** `.venv` は `_editable_impl_duocad.pth` で本体の checkout を sys.path に入れて
  いるため、ワークツリーで `python script.py` するとワークツリーではなく本体の `duocad` を読む。ワークツリーのコードを確実に
  読ませる経路は `pytest`（`pythonpath = ["."]`）、`topopt_checks/*.py`（`_bootstrap`）、`python -m duocad...` / `python -c`
  （cwd が先頭）の3つ。それ以外は `PYTHONPATH=<worktree>` を明示するか `sys.path.insert(0, ...)`。
- **Browser pane の `preview_start` はワークツリーの `.claude/launch.json` を見ない**（本体の `web/src` を配る）。ワークツリーの
  画面は Python サーバを立てて `http://127.0.0.1:8766/`（`web/dist`）を開くか、`web/scripts/server.mjs` の `startServer`
  （`REPO_ROOT` をスクリプト位置から解く）をワークツリーから `file:///` で import した使い捨ての puppeteer スクリプトで撮る。
  `web/node_modules` はワークツリーごとに `npm ci` でもよい。

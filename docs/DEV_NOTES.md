# duocad — 開発環境と運用のメモ

**読むとき**: 環境（venv・行末・git）でつまずいたとき、コメントや文書を減量するとき、タスクを切るとき。

## 環境

- **数値計算（numpy 2.4.6 / scipy 1.17.1）は `.venv` にしか無い。** 既定の `python`（3.14）には無く、matplotlib は
  どの venv にも無い（図が要るなら標準ライブラリだけで PNG/SVG を書く）。実行は `.\.venv\Scripts\python.exe`。
- **`.venv` は `uv sync --frozen` で `uv.lock` から作り直せる**（2026-09-18 実測）。依存は `uv add` で `pyproject.toml` と
  `uv.lock` に入れる。`uv pip install` で手入れしたものは作り直すと消え、そのときは収集エラー（`No module named ...`）で初めて気づく。
- **同じリポジトリ内でも行末はファイルごとに違う。** `core.autocrlf=true` で git の blob は LF だが、作業ツリーは
  ツールが直接書いたファイル（`web/src/viewport.ts` 等）が LF、checkout を経たファイル（`App.svelte` / `viewCube.ts` 等）が
  CRLF。`git diff` も `git status` も差分を出さないので目視では分からない。踏んだ形:
  複数行の完全一致置換が0件で落ちる／変異検査で置換が0件のまま「変異を入れても緑」＝「テストが噛んでいない」という
  **逆の結論**が出る／`git checkout -- <path>` が1ファイルだけ CRLF で書き戻して `line == '}'` の完全一致が `'}\r'` で外れる。
  守り方: 置換は**必ず件数を assert**（`assert s.count(old) == 1`）／変異チェックで緑が出たらテストを疑う前に
  `git diff` で変異が入ったかを見る／検出は `python -c "d=open(p,'rb').read(); print(d.count(b'\r\n'), d.count(b'\n'))"`
  （Git Bash の `grep -c $'\r$'` は当てにならない）／`newline="\n"` で保存すると作業ツリーだけ LF になり並行セッションの
  差分表示が汚れる。
- **ソースに生の NUL バイトを書くと git がバイナリ扱いにする**（`viewport.ts` に面の強調キーの区切りとして2個入っていた。
  実行は正常）。`git diff` / `blame` が効かず、`core.autocrlf` の正規化も止まる。最も churn の激しいファイルで5日以上
  差分が読めない状態だった。Unicode エスケープへ置き換える。見つけ方は `git ls-files --eol` に `i/-text` が出るテキストファイル、
  `git diff --stat` と `--ignore-all-space --stat` が桁違い。
- **GitHub リモートは private で公開しない**（[DESIGN_DECISIONS.md](DESIGN_DECISIONS.md)）。

## 機械的な一括編集の道具

- **行番号指定でコメント/docstring を一括置換する道具には4つ要る**（83ファイル分で全部踏んだ）: (1) 行末を保存する
  （バイト列で `split(b"\n")` して `\r` を行の一部として運ぶ。往復するだけで全行が差分になる）(2) docstring の閉じ引用符を
  範囲に含める（1行足りないと `unterminated triple-quoted string`。3回踏んだ）(3) 書き込み**前**に `ast.parse`
  (4) 置換先の1行目（できれば最終行も）を照合する（1行ずれても構文が通り、同じコメントが2行重複する。実際に2箇所作った）。
- **Python の関数を機械的に消すときは正規表現ではなく `ast`。** 「`def` 行から次の `def` まで」＋「直前が `@` の間だけ遡る」
  は複数行デコレータ（`@pytest.mark.parametrize(` … `)`）で `)` の手前で止まり、残ったデコレータが次の関数に付く
  （構文は通るので気づけない）。`decorator_list` の最小 `lineno` から `end_lineno` まで。壊れた3ファイルだけでなく
  同じ手口で触った14ファイル全部を戻してやり直した。
- **既存呼び出しを新ラッパーへ一括置換すると、ラッパー内の元関数呼び出しまで置換されて無限相互再帰になる**
  （`dragFovTo` を包む `setFovTo` で 7＋1＝8 件。件数を数えて assert していたのに数が合ったのは巻き込んだから）。
  非同期だと例外もタイムアウトも出ず低い CPU で回り続ける（約30分溶かした）。置換後にラッパーの本体を目で読む。
  無時間で止まったら相互再帰を先に疑う。
- **引数を1つ減らすリファクタは、その引数をキーにしていたキャッシュ・重複送信よけ・デバウンス判定を黙って無効化する。**
  フィレットの引数から辺の集合を落としたら、プレビュー再送判定が `JSON.stringify([verb, args])` を印にしていたため
  辺を足しても印が変わらなくなった。消す前にその名前が (1) キャッシュキー (2) 重複送信よけ (3) 再計算トリガ に
  使われていないか grep する。代わりに新しい真実源の指紋（選択の参照キー）を印へ足す（`CommandHost` の `selectionPrint`）。
- **クラスを割って可変リストを共有させるとき、`x, self.x = self.x, []` の束ね直しが共有を静かに切る**
  （`Session.take_warnings()`。中身を `clear()` する）。分割と同時に「共有が続いていること」を見る検査を足す。
- **plan-then-write は同じオブジェクトへの複数の計画を併合しないと最後だけが勝つ**（`move_sketch_entities` で線分の
  両端を選ぶと片方しか動かなかった。計画の単位は入力の単位ではなく書き込みの単位）。「2つ動かして両方動く」テストが要る ——
  1つだけ動かすテストは何本あっても通り、片方が偶然同じ値になる配置も素通りする。
- **「既定を置き換える引数」に「既定へ追加したいもの」を相乗りさせない**（`fixed_dofs` に内部で計算した拘束を代入したら、
  `void_mask` だけ渡した呼び出しで既定の拘束がまるごと消えた。`extra_fixed_dofs` として別の引数にする）。

## コメントと文書の減量

規律は [../CLAUDE.md](../CLAUDE.md)。減量の判定基準（2026-09-06 のコメント減量タスク群で確立）:

- **日付＋実測が同じ括弧に同居するとき、日付だけを消し数値は残す。** 「実測だから残す」を理由に日付まで保護しない。
  逆に「日付があるから経緯」と数値ごと消すのも誤り。発見者・発見日の枕詞も、後ろの再現手順だけ残せば自立する。
- **経緯のパラグラフを削る前に、中に埋まっている参照（`[[…]]` / `engine \`…\``）を grep する。** パラグラフごと
  「経緯だから消す」と判断すると巻き添えで消える（実際に `smoke-files.mjs` で消しかけて復元した）。
  ※ 2026-09-16 に context-engine のノートは各リポジトリの文書へ移設されたので、コード中の `[[…]]` 参照の行き先は
  `docs/` のこの文書群になる。
- **「複数ファイルへ同じ理屈を書く重複」は同一ファイル内でも起きる。** `viewport.ts` で同じ罠の説明が4箇所に全文重複していた。
  宣言部に全文を残し、他は「詳細はそちら参照」に圧縮する。1,500行超のファイルでは減量の完了条件に
  「同一ファイル内の重複説明が無いこと」を含める。
- **減量は流入を変えなければ戻る**（実測）: 2026-08-23 の減量5コミットで 32.9%→29.3%（3.6pt）、13日後に 31.1%
  （1.8pt 戻り）。追加された 42,431 非空行のコメント密度は減量前と同じだった。ファイルサイズも片道
  （`viewport.ts` 388→5,155行、`App.svelte` 950→2,461、`kernel.py` 298→2,052。一度も減っていない）。
  肥大の主因は無人 drain の積み重ね（各 runner が経緯・正当化を書き残す癖。16タスク分蓄積）。
  **無人実行でコードを書かせるなら、コメント規律は最初からリポジトリ規約（CLAUDE.md）に入れておかないと単調増加する。**
- **文書は「行」でなく「節」の単位で増える。** `docs/TOPOPT.md` は削減タスクで −173行の9時間後に機能タスク1本が
  +397行（機能タスク1本＝日付付きの新規節1つ）。反復する節（実測値・基準線・案N）は表に畳み「新見出しを立てず表の末尾に行を足す」
  と契約に書く。
- **正を宣言してもコピーは腐る。** README が正を宣言しながら再掲していた4組のうち2組がコピー側だけ、あるいは連鎖ごと
  誤っていた。重複を見つけたら正を選ぶだけでなく**コピーを消して参照に置き換える**。
- **一括掃除の削減率を次回に当てない。** 初回（規律が無かった時代の在庫）の 14.7% を掛けた見積もり −3,000行に対し
  実績 −232行（13倍の過大）。規律がある状態で書かれたコードは最初から経緯の密度が低い。**行数を成果の指標にしていたら
  「失敗」と誤判定していた**（実質の完了条件は全部満たされていた）。
- 率・行数の門は作らない（本人 2026-09-05「価値あるコメントが増える類のタスクもある」）。門を作ると実測値の1行メモが
  経緯の語りより先に消える。`tools/measure_bloat.py` は報告専用。

## タスクの切り方（無人 runner に渡すとき）

- **verb を1つ足すと `web/src/session.ts` の型宣言（`Applied` union と `ServerState`）も直さないと
  `tests/test_applied_type_sync.py` が赤になる。** 「画面には手を付けない」というバックエンドのタスクは自分では緑に
  できない（4 verb と `state_meta` の新キーで7本赤）。**宣言・型・種別の表＝バックエンド／引数の集め方・描画＝フロント**で切る。
  `tests/test_ui_command_wiring.py` の `ENTER_REQUIRED` は逆に画面側（`CommandDef` からの導出）。
  `commands/command.ts` の `SelectableKind` / `KIND_COUNTERS` はコアの `SELECTABLE_KINDS` の写しで、足し忘れると
  件数表示が `undefined` になる（`tsc` では出ない。`ref as ShapeRef` のキャストで書かれているため）。
- **`ws="none"`（MCP 専用）の verb は `test_applied_type_sync.py` の対象外**（対象条件は `ws != "none" and changes_document`）。
  `add_mesh_body` / `mirror_sketch_entities` のようにブラウザに開けていないのに操作ログに載る verb は、MCP 経由で呼ばれても
  全ブラウザへ状態配信されるが、`Applied` に型が無くても検査は緑。MCP 専用 verb を足すとき `Applied` を省略してよいかは
  この抜け穴を踏まえて都度判断し、省略するなら理由を宣言に書く。
- **文書の置き場は「誰が直せるか」で決める。** 設計文書がリポジトリの外（`reports/`）にあった間は、無人 drain の
  `target_dir` 外なので実装が進むたび文書だけが古くなった。コードと同じリポジトリなら同じコミットで直せる。
  日付入りのファイル名は継続改訂する文書に向かない。
- **「直した」と「記録を直した」は別の作業**で後者は落ちやすい。とくに同じコミットの中で問題を直しているとき、
  直前に自分が書いた「未決」が視界から外れる。
- **MCP サーバは catalog を変えたら立て直す**（`delete` 追加直後に `unknown command`）。
- **状態変更の配信を「直後の別コマンドの配信」に相乗りさせない**（`duocad/net/ws.py` の自動 commit）。配信しない種類の
  コマンド（`preview`）が同じ入口を通った瞬間に破れ、例外は出ない。検査は**2接続**で書く（1接続だと要求元には結果が返るので
  緑に見える）。自動で起こした変更に人の操作 ID を載せない。

## ワークツリーでの作業（2026-09-16実測、issue #9）

issue の実装は **作業ブランチを汚さないようにワークツリー上で行う**（本人指示 2026-09-16、issue #6 断面表示の依頼時）。issue ごとのワークツリー（`.claude/worktrees/issue-9-shell` / `issue-10-measure` 等）を並べて並行して進めるのが作法。ワークツリーのブランチは **develop から切り、develop へ戻す**（main は動作確認済みの完成品で、develop → main は PR だけ。2026-09-18 本人指示、`CLAUDE.md`「ブランチ」）。以下の「本体」はリポジトリ直下のチェックアウト（develop）を指す。

**ジャンクションを張る。** `git worktree add` 直後のワークツリーには gitignore 済みの `.venv` と `web/node_modules` が無く、`npm run check` は依存が解決できず、`web/scripts/server.mjs`（smoke の起点）は「venv の python が無い」で止まる。本体のそれらへジャンクションを張る:

```powershell
New-Item -ItemType Junction -Path <worktree>\.venv -Target C:\Users\tm976\mf\duocad\.venv
New-Item -ItemType Junction -Path <worktree>\web\node_modules -Target C:\Users\tm976\mf\duocad\web\node_modules
```

- ジャンクションは gitignore 対象なので `git status` には出ない。
- pytest は本体の `.venv\Scripts\python.exe -m pytest` をワークツリーの cwd で呼べば足りる（`duocad.__file__` はワークツリー側を指す）。

**ワークツリーを消す前に、ジャンクションを先に外す。** 2026-09-18、`.venv` へのジャンクションを張ったままのワークツリーを削除した処理がジャンクションを辿り、**本体の `.venv` の中身を丸ごと消した**（`uv sync --frozen` で作り直した）。リンクだけを外すのは PowerShell の `[System.IO.Directory]::Delete('<worktree>\.venv', $false)`（同日、本体の `.venv` が残ることを実測）。外してから `git worktree remove` する。再帰削除（`Remove-Item -Recurse`、エクスプローラーでのフォルダ削除など）をジャンクション入りのワークツリーへ向けない。
- smoke が配る画面は `web/dist` なので、ワークツリーで先に `npm run build` を回す。

**素の python スクリプトは本体を import する。** `.venv` は `_editable_impl_duocad.pth` で本体の checkout を sys.path に入れているため、ワークツリーで scratch のスクリプトを `python script.py` で走らせるとワークツリーではなく本体の `duocad` を import する（ワークツリーで足した属性が「無い」と言われ、原因が分かるまで1往復溶かした）。ワークツリーのコードを確実に読ませる経路は `pytest`（pyproject の `pythonpath = ["."]`）、`topopt_checks/*.py`（`_bootstrap` がリポジトリ直下を `sys.path[0]` へ）、`python -m duocad...` / `python -c`（cwd が先頭に入る）の3つ。それ以外は `PYTHONPATH=<worktree>` を明示するか `sys.path.insert(0, ...)` を書く。

**Browser pane の `preview_start` はワークツリーの `.claude/launch.json` を見ない**（起動元リポジトリ直下の launch.json を読み、本体の `web/src` を配る）。回避: `web/scripts/server.mjs` の `startServer` は `REPO_ROOT` をスクリプト位置から解くので、ワークツリーのそれを `file:///` で import した使い捨ての puppeteer スクリプトを scratchpad に置き、MCP（HTTP `/mcp`）で素材を作って UI 操作とスクリーンショットを撮る。Browser pane 経由の確認は諦める。

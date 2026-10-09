# 文書・コメント・テストの保守の契約

文書の腐り方への対処、コメント減量で消してはいけないもの、文書とコードの一致検査が固定している事柄の契約。
**いまの挙動の正はコードとテスト。** ここは、ソースから読めない不変条件・依存禁止・罠を、元の作業タスクの契約から
分野別に蒸留したもの。コメントの4種の基準そのものは [../../CLAUDE.md](../../CLAUDE.md)、減量の判定基準の一部は
[../DEV_NOTES.md](../DEV_NOTES.md)「コメントと文書の減量」が持つので、ここでは重複させず、それらに無いものだけを書く。

出典は項目末尾の括弧 `(タスクID, 日付)`。タスクIDは元の作業タスクの識別子。

## 1. 文書に写してはいけないものの型(手動同期の口を増やさない)

文書の記述が腐る型は5つに分類できる。**古い記述を直すときは、数字や列挙を新しく書き換えるのではなく、
型に応じて消して「正は○○」に置き換える**(書き直すと次の同期漏れの口が増えるだけ)。

- **型1 件数**: 数字を書かない。**線引きは「同じ文の中に列挙が並記されているか」**。列挙が並記されていて数字がその場で
  検算できる(「条件を置く4 verb(a / b / c / d)」)なら残してよい。列挙なしで数だけ書いてあるものが型1の本体
  (「中核の4つの決めごと」のように項目を1つ参照へ置くと数が変わる見出しも対象)。
  `(20260906-0100-duocad-fix-stale-docs, 2026-09-06)`
- **型2 列挙・定数値の転記**: 消して「正は○○」の参照へ。ただし**実測は定数の写しではない**ので残す
  (「`MAX_ELEMENTS` = 120,000」は記号名だけにし、直後の「12万要素で約0.55GB」の実測は残した)。
  `(20260906-0100-duocad-fix-stale-docs, 2026-09-06)`
- **型3 否定の主張**: 判定線は「**実装の現状を数え上げた否定は消す。『そうしないと決めた』という決定そのものの否定は残す**」。
  残す例: 「拘束ソルバは持たない」「選択パネルは無い」「SvelteKitは使わない」。消す例: 「改名のverbが無い」
  「複製する手段が無い」「スケッチ図形を消す・動かすverbは無い」「頂点を引数に取るverbは無い」。
  現状の否定は実装が進むと黙って嘘になる —— 実際に、削った否定のうち複数が既に偽だった
  (`move_body` に `copy=true` と `center_vertex` が実在した。後に `duocad/verbs/rename.py` も入り「改名verbが無い」も偽になった)。
  肯定形に直すなら「何が担当か」(例: 「消す・動かすのは `delete` と `move_sketch_entities`」)にする。
  `(20260906-0100-duocad-fix-stale-docs, 20260906-0250-duocad-tests-readme-resync, 2026-09-06)`
- **型4 実行出力の転記**: 起動時 stderr の貼り付けなどは消し、変わらない部分(「立った時点でURLとポートがstderrに出る」)だけ文にする。
  URLを本文へ書き写すと同じ型を再生産する。 `(20260906-0100-duocad-fix-stale-docs, 2026-09-06)`
- **型5 外部ファイルへの参照とその属性**: サンプルSTEPの「27部品」「74面」のような属性は手動同期になる。ディレクトリを指すだけに縮める
  (名指しした1ファイルは既に実在しなかった)。 `(20260906-0100-duocad-fix-stale-docs, 2026-09-06)`
- **消しきれずに残る列挙がある**。README が「文書の地図」で正と宣言している表(MCP.md「公開ツール」、失敗語彙の列挙、到達表、
  smokeの抜粋など)は消せない。それは手動同期のまま残るので、**機械で1点に引ける正があり、静かに壊れ、読み手が他に引けない
  ものだけ**を検査で押さえる(3章)。残りは意図して押さえていない。
  `(20260906-0100-duocad-fix-stale-docs, 20260906-0250-duocad-tests-readme-resync, 20260906-0900-duocad-lock-doc-code-agreement, 2026-09-06)`

## 2. 「正は○○」参照と見出し名

- **参照は行番号ではなく見出し名で書く**(行番号は削除で必ずずれる)。**裏返しの義務**: 名指しされた見出しを改名・統合・分割する
  タスクは、その見出しを指す全参照(docs・`duocad/` のコメント・`topopt_checks/*.py` のコメント・README)を
  `grep -rn "<見出し文字列>" .` で洗って同じ変更で直す。見出しを畳むと名指しの相互参照が黙って死ぬ。
  文書の見出しは「動かせない面」になる。 `(20260906-0100-duocad-fix-stale-docs, 20260906-0800-duocad-restructure-topopt-md, 2026-09-06)`
- 動かせない見出しの例: README §4 `## 4. アーキテクチャ`(MCP.md が「README §4」を名指しする。**節の中身を減らすのは良いが見出しは消せない**)、
  `docs/TOPOPT.md`「許容応力の導出」(README が指す)、INITIAL_PLAN の §2「verbの粒度」「外してはいけない3つ」(旧名 IMPLEMENTATION_PLAN)。
  `(20260906-0100-duocad-fix-stale-docs, 2026-09-06)`
- **引用は見出しだけではない**。コード側コメントの引用句が、見出しではなく本文の太字フレーズや別節の自己参照に一致して
  成立していることがある。ダングリングと判定する前に `###`/`##` だけでなく本文の太字も検索する。
  逆に、コード側コメントの引用句が文書のどこにも一致しない例も実在した。コードを触れないタスクでは、文書側の既存見出しに
  引用句を埋め込む形で解消した(見出し名の書き換えではなく句の追加)。
  `(20260906-0800-duocad-restructure-topopt-md, 2026-09-06)`
- `docs/TOPOPT.md` を再構成するときの具体的な面は4章。

## 3. 文書とコードの一致検査(`tests/test_doc_code_agreement.py`)

文書に残した列挙のうち、機械で1点に引けるものの最後の受け皿。**落ちたときの意味は「コードを変えたのに、それを写した文書の行が古い」**。
`(20260906-0900-duocad-lock-doc-code-agreement, 2026-09-06)`

- **掛かり先は見出し名と本文の一意な文字列**。改名・統合するなら同じコミットで検査側の定数も直す。改名すると
  「…に見出し '…' が無い」で**落ちる**(黙って緑にはならない)。現行の掛かり先:
  - `MCP.md` 見出し `## 公開ツール` の表 ↔ `catalog.tool_names()` 由来の `tools/list`
  - `MCP.md` 見出し `## 実装のメモ` + 本文の語 `構造化封筒` の直後の（…）↔ `error_envelope` の `code` の語彙
  - `docs/TOPOPT.md` 見出し `## 現在の正` の表(2列目の**太字の数値**と3列目の `` `module.SYMBOL` ``)↔ `duocad.topopt.<module>` の定数の実値
  - `README.md` 見出し `### 形状最適化の受入検査` のコードブロック ↔ `topopt_checks/check_*.py` の実ファイル
  - `tests/mcp_support.py` の `ERROR_CODES` ↔ 同じ語彙(`call_error` が未知コードで落ちないための突き合わせ)
- 「現在の正」表の読み方: 3列目は `design_space.COARSE_ELEMENTS_LONG_AXIS` / `COARSE_MAX_ITER` のように**2つ目以降のモジュール名を省いてよい**
  (行の中で直前のモジュールを引き継ぐ)。数で書いていない行(スレッド数=「総コアの半分」)は値の比較から外れるが記号の実在は見る。
- **押さえる基準は3条件すべて**: (i) 読み手がその文書を信じて行動する(他に引ける場所が無い、または文書が「正」と宣言されている)、
  (ii) コード側に**機械で1点に引ける正**がある、(iii) 食い違っても**何も落ちない**(静かに間違う)。
  (ii)を欠くもの(smoke一覧・モジュール表・リポジトリの地図・擬似コード・正が `_validate_*` と `add_*` に散る `Entity.params` のキーそのもの)と、
  (iii)を欠くもの(コード側が既にテストで固定されている: `PREVIEWABLE_VERBS`・`Command.category`・`ai_*` の口・ビューキューブ26領域など)は
  意図して押さえていない。**「この検査があるから文書はもう腐らない」に依存しない。**
- **不変条件: 期待値を検査側へ書き写して緑にしない。** 正はコード側。検査の中に「7語彙」のような数を定数で置かず、すべてコードから導出する。
  落ちたときに直すのは文書の行。
- **検査を足すときの2点(文字列を文書全体から `in` で探すだけの検査の穴を塞ぐため)**:
  (1) **走査が効いていることを先に見る**(0件なら常に緑になる。見出しや表の形が変わったら「読めなかった」で落とす)
  (2) **集合を両方向で比べる**(足りない側と余計な側の両方)。
  `in` で探すだけの検査は文書側の古い行を検出できず、節が消えても同じ文字列が別の場所にあれば緑のままになる。
- **`README.md`「形状最適化の受入検査」に `topopt_checks/check_*.py` を1本残らず並べる。** この群は pytest に入っておらず、
  READMEのブロックを写して回すのが唯一の強制力(CIが無い)。**新しい `check_*.py` を足したらREADMEにも足す**(足さないとこの検査が落ちる)。
- **`MCP.md`「公開ツール」表は `mcp=False` の verb を載せない**。
- 文書を読む検査を足すなら必ず踏む穴: 節の切り出しを素の行頭 `#` で行うと、PowerShellコードブロックの `# 説明` 行で節が1行目で終わり0件を拾う。
  **コードフェンス(```)の内側は見出しに数えない**。失敗文は `path.name` だけだと `tests/README.md` も `README.md` も「README.md」と表示されるので
  リポジトリ相対パスで出す。 `(20260906-0900-duocad-lock-doc-code-agreement, 2026-09-06)`
- **変異検査の罠**: ファイルごとに CRLF/LF が違う(9章)ので、複数行文字列の置換は `old` が0件一致のまま
  「検査が緑」になり**変異が効いていないのに合格に見える**。変異は必ず「置換できたこと」(`count(old)==1`)を assert してから走らせる。
  `(20260906-0900-duocad-lock-doc-code-agreement, 2026-09-06)`
- `Session` は引数なしでは作れない。検査では `Session(None, 0.2)`(`step_path` と `deviation` が必須)。
  `(20260906-0900-duocad-lock-doc-code-agreement, 2026-09-06)`
- **失敗語彙の件数は書かない。** 「6語彙」と「7語彙」がリポジトリ内に混在していた(正しくは語彙の実数)。現行の検査は語彙の列挙 ↔ `error_envelope` の一致だけで件数は見ないので、件数を書くと検査されない口になる(型1)。 `(20260906-0900-duocad-lock-doc-code-agreement, 20260906-0100-duocad-fix-stale-docs, 2026-09-06)`

## 4. docs/TOPOPT.md の構成規約

- **冒頭「## 現在の正」は索引であって値の置き場ではない。値そのものはコード側が正**(手動同期しない。本人指示 2026-09-05)。
  **新しい既定値を追加・変更したら、まずこの表の行を直す** —— ここが古いと文書全体が「時系列を読まないと現在値が分からない」状態に戻る。
  この表は3章の検査が値まで見る。 `(20260906-0800-duocad-restructure-topopt-md, 20260906-0900-duocad-lock-doc-code-agreement, 2026-09-06)`
- **置き換えられた方式の節は削除せず、節の先頭に「置き換えられた」旨の1段落**を足す。
  中身(実測・根拠・留保)は削らない。 `(20260906-0800-duocad-restructure-topopt-md, 2026-09-06)`
- 性能の基準線の節には、構造的な文書(題材の定義表・検査一覧・未解決の設計判断)が過去の測定値と同居することがある。**「時系列の実測値は全部1箇所にある」前提で自動処理を書かない。** 文書全体から文字列を探す検査は、この表を動かすと落ちる。 (20260906-0800-duocad-restructure-topopt-md, 2026-09-06)
- 行数が変わらないことを失敗の根拠にしない(価値は「現在値が時系列を読まずに引けること」)。節の位置にも依存しない。見出し文字列で探す。
  `(20260906-0800-duocad-restructure-topopt-md, 2026-09-06)`
- `docs/TOPOPT.md` は巨大(当時299KB)で全文読みはガードに止められる。見出しを `awk` / grep で1行だけ取って確かめる。
  `(20260906-0100-duocad-fix-stale-docs, 2026-09-06)`

## 5. コメント減量で消してはいけないもの

**4種(外部の罠・実測事実・層をまたぐ契約・意図的な変則)の基準は CLAUDE.md が持つ。** 以下は、実際に「消えかけた/消さずに残した」具体例と判断線。

- **`Command` / `Param` の `note=` / `description=` はコメントに見えるがコード**(`duocad/catalog.py`)。関数呼び出し引数の文字列リテラルで、
  docstring でも `#` でもない。経緯の語りが混じっていても**機械的に対象外**にする。実際に1度これで壊した
  (`check_code_unchanged.py` が exit 1)。**将来似た形の宣言テーブルを持つファイルも同じ**。削ってよいのは `#`/`#:` の実コメントと
  docstring(関数・クラス・モジュール先頭の文字列)だけ。迷ったら1ファイルごとに `check_code_unchanged.py` を走らせる(まとめて編集すると原因の切り分けが遅れる)。
  `(20260906-0300-duocad-trim-comments-core, 2026-09-06)`
- **経緯と実測が同じ括弧・同じ注記にあるときは、経緯を削り実測を残す**(判断は1文の中で両立させる)。例: `kernel.py` の
  `tessellate_edges`(ライブラリ名の変遷を削り、なぜ現行方式が要るかの実測を残す)、`topopt/mesh.py` の `DEFAULT_ITERS` 注記
  (「30→12→30に戻した」の往復を削り、その値が正しい理由の実測 0.765^30≈0.03%・4.8%→15.2% は1文字も削らない)、
  `topopt/voxelize.py`(発見の経緯を削り、
  再現手順の具体値 `a=b=(0,0,0), c=(5,4,4), p=(10,4,-8)` は残す)。判断基準は「**数値の再現に日付の文脈が要るか**」。
  `web/src/viewport.ts` の実測数値(0.6%/55%ずれ、7e-14でカメラが動いた実例、93.6px→143.7px)も日付だけ外した。
  `(20260906-0300-duocad-trim-comments-core, 20260906-0400-duocad-trim-comments-subpackages, 20260906-0500-duocad-trim-comments-web-src, 2026-09-06)`
- **削ってよいと自信を持てた4種**: (1) 日付+決定の刻印だけの注記(`(2026-08-23)` `(2026-09-04本人決定)`)、(2) 「以前は〜だった」の過去形叙述、
  (3) 削除済みの実験場・旧コードへの言及とコミットハッシュ(値の由来は残す)、
  (4) 同一ファイル内の重複した理由付け(`selection.py` の `ref_key`/`same_ref` と `modify_selection` の「集合引きでO(n)にした」。実測込みの方だけ残し片方は結論だけに)。
  `(20260906-0300-duocad-trim-comments-core, 2026-09-06)`
- **日付なしの `(本人決定)` `(本人裁定)` タグも削る。ただし機械的な全削除ではない**: 情報が「誰が決めたか」だけなら削り、
  「わざとこうしてある」という将来への警告として要るなら残す(例: `(交差判定はしない。本人明言。向きも見ない)`)。
  `(20260906-0500-duocad-trim-comments-web-src, 2026-09-06)`
- **同一ファイル内の重複**(同じ罠の全文が `sketchGridIsFacePlane` 宣言・`basePlaneForFrame`・`desiredGridPlanes`・`gridTiers()` の4箇所に)は
  宣言部に全文を残し他は「詳細はそちら参照」に圧縮する。罠の中身は「`sketchGridFrame` のframe一致だけでは基準平面か任意面かを判別できない —
  実ジオメトリの面でも数値的に基準平面と一致することがある」。 `(20260906-0500-duocad-trim-comments-web-src, 2026-09-06)`
- **経緯のパラグラフの中に `engine \`…\`` / `[[…]]` の参照が埋まっていることがある**。経緯として削る前にこの参照の有無を確認し、
  実在するなら参照だけ残して周辺の日付・経緯だけ削る。
  なお参照先ノートは各リポジトリの docs に移設済み([../DEV_NOTES.md](../DEV_NOTES.md)参照)。
  `(20260906-0700-duocad-trim-comments-scripts-and-checks, 2026-09-06)`
- **残した代表例(消すと戻せない側)**:
  外部の罠 —— `mcp_server.py` の `_claim_stdout`(OCCTのC++が標準出力へ直接統計を吐くので fd 1 を fd 2 へ dup2)、
  `session.py` の `_resolve_document_name`(Windowsのドライブ相対パス・NTFS代替データストリーム・予約デバイス名・末尾ドット/空白によるパス脱出の実測列挙)、
  `net/http.py` の `_mcp_forbidden`(DNSリバインディング対策・プリフライト不要リクエストの悪用経路)、
  `agent/providers.py` の `_call`(`URLError` にタイムアウトが包まれてくる urllib の実装依存)、
  `topopt/anchor.py` のモジュールdocstring(なぜボクセル側から境界を取り直してはいけないか: 階段状境界の偽の応力集中)。
  実測事実 —— `selection.py` の「PCケース(27部品・2835面)のピック失敗率63%」、`resolution.py`・`kernel.py` の実測テーブル、
  `topopt/voxelize.py` の `closest_triangle`(退化三角形の反例数値)。
  層をまたぐ契約 —— `resolution.py` のPython/TS二重実装、`optimize.py` の `FINGERPRINT_RTOL`/`ATOL`(再生の決定性を前提にした許容差)、
  `session.py`/`view.py` の分担(`Session` が持つ値と `DocumentView` が持つ値)、`net/ws.py` のモジュールdocstring(プロトコルの上り下り・`state`/`delta` の分担)。
  意図的な変則 —— `meshbody.py` の「メッシュをB-repに直さない理由」3点(marching cubesの面数爆発・`face_count` のO(n)再走査・フィレットの意味喪失)、
  `verbs/move_body.py` の「既知の穴」(`copy=True` で回転対称な角度を指すと重なった複製ができるが、健全な判定コストの方が高いのでわざと通す)、
  `verbs/extrude.py` の `_resolve_target`(いま到達しない分岐だが将来の verb 追加に備えて残してある防御的コード)。
  `(20260906-0300-duocad-trim-comments-core, 20260906-0400-duocad-trim-comments-subpackages, 20260906-0600-duocad-trim-comments-tests, 2026-09-06)`
- **`selection.py` / `session.py` / `kernel.py` は層をまたぐ契約が最も密なので、意図的に薄くしか削っていない。** 「まだコメント率が高い」を理由に
  この3ファイルへ追加の削除を掛けない(濃い箇所ほど契約が多い)。`net/ws.py` の51%も同じで、削ったのは16箇所以上の日付注記だけ。
  `(20260906-0300-duocad-trim-comments-core, 20260906-0400-duocad-trim-comments-subpackages, 2026-09-06)`
- **`topopt_checks/` の日付注記は web/scripts のUI変更履歴とは性質が違う。** 「既定値がいつ変わったか」の較正根拠(しきい値を取り直した日付)で、`docs/TOPOPT.md` の失効した数字の追跡に使われている可能性がある。**削る前に、その日付が `docs/TOPOPT.md` から参照されていないか確認する**。 `(20260906-0700-duocad-trim-comments-scripts-and-checks, 2026-09-06)`
- **`tools/measure_bloat.py` と `tools/check_code_unchanged.py` のモジュールdocstringは `--help` に出るので触らない**
  (「これは門ではない」等の自己言及も層をまたぐ契約寄りで消しすぎない)。触ってよいのは `--help` に出ない関数docstring内の日付だけ。
  `(20260906-0700-duocad-trim-comments-scripts-and-checks, 20260906-0600-duocad-trim-comments-tests, 2026-09-06)`
- **テストのモジュールdocstringは契約の出所**。丸ごと消すと出所の追跡が `git log` だけになる。減らすなら関数内コメント側から。
  各テストの docstring/コメントが持つのは「なぜその検査がその形なのか」(実測の数字・過去に空振り緑だった穴・ハーネス固有の後始末・偽プロバイダの作り)で、
  契約文書(旧 `tests/README.md`)に写せない/写してはいけない。逆に **「文書に同じことが書いてある」を削除理由にしない**(書いている対象が違う)。
  `(20260906-0250-duocad-tests-readme-resync, 20260906-0600-duocad-trim-comments-tests, 2026-09-06)`

## 6. 減量の運用の判断

- **行数・コメント率は目標にしない。** 減量の結果は「種別を守った結果この程度」であり、「もっと削れるはず」の追加削除の根拠にしない。
  `tools/measure_bloat.py` は報告専用(門にしない。[../DEV_NOTES.md](../DEV_NOTES.md))。
  `(20260906-0300-duocad-trim-comments-core, 20260906-0400-duocad-trim-comments-subpackages, 20260906-0500-duocad-trim-comments-web-src, 20260906-0600-duocad-trim-comments-tests, 20260906-0700-duocad-trim-comments-scripts-and-checks, 2026-09-06)`
- **「触っていないファイルがある」は「見ていない」ではない。** 密度が高いのが実測事実・層をまたぐ契約なら規律準拠として無編集になる
  (`topopt/` の8ファイルには日付注記が最初から無かった。同じ範囲を後続が再確認する必要はない)。
  `(20260906-0400-duocad-trim-comments-subpackages, 20260906-0500-duocad-trim-comments-web-src, 2026-09-06)`
- **同一ファイルを続けて編集するときは、直前の変更が意図どおり反映されたかを都度 grep で確認する。** `Edit` は `old_string`/`new_string` を取り違えても
  エラーを出さず、別の場所へ意図しない行を複製することがある(`net/ws.py` で1行複製した)。
  `(20260906-0400-duocad-trim-comments-subpackages, 2026-09-06)`
- **並列委譲した担当ファイルに親が手動で触らない**(同じ2ファイルを両方が編集し「modified since read」で衝突した)。待つか、確実に担当外のファイルだけにする。
  `(20260906-0600-duocad-trim-comments-tests, 2026-09-06)`

## 7. 「コードを変えていない」の機械的な証明

- **Python(`duocad/` `tests/` `topopt_checks/` `tools/`)**: `python tools/check_code_unchanged.py <git ref>`。docstring だけを剥がして AST を比べ、
  差が出なければ動作コードは1文字も変わっていない。**`#` コメントは AST に残らず、`note=`/`description=` の文字列リテラルは残る**(5章)。
  `TARGETS = ("duocad", "tests", "topopt_checks", "tools")` で、**`web/scripts/`(`.mjs` と `shoot_topopt_export.py`)は対象外**。将来 `web/scripts` に
  Python の検査を足すなら TARGETS 追加を検討する。 `(20260906-0300-duocad-trim-comments-core, 20260906-0700-duocad-trim-comments-scripts-and-checks, 2026-09-06)`
- **TS / Svelte(`web/src/`)には使えない。** `npm run build` の成果物(`dist/assets/*.js` `*.css` `dist/index.html` とフォント)の SHA-256 が着手前と一致することで代替する
  (`*.js.map` だけ別ハッシュになるのが期待される署名。バンドラは本番ビルドでコメントを落とすので、取り違えて消せば JS のハッシュが動く)。
  あわせて `npm run check` が `0 ERRORS 0 WARNINGS`。 `(20260906-0500-duocad-trim-comments-web-src, 2026-09-06)`
- **`.mjs`(`tests/eval_*.mjs`・`web/scripts/`)は機械検証が無い。** `git diff` を全部読む + `node --check`(全ファイル)+ 実機 smoke を実行して緑を確認する。
  `shoot_topopt_export.py` は `py_compile`。 `(20260906-0600-duocad-trim-comments-tests, 20260906-0700-duocad-trim-comments-scripts-and-checks, 2026-09-06)`
- 減量の完了後に回す検査: `pytest`、`topopt_checks/` の検査群(コメントだけの変更なら数値は不変)、smoke(`web/` から `node scripts/smoke*.mjs` で自己完結。サーバを別途立てなくてよい)。
  `(20260906-0400-duocad-trim-comments-subpackages, 20260906-0500-duocad-trim-comments-web-src, 2026-09-06)`

## 8. 契約を書く場所の役割分担

- **契約文書の役割は「実装が守っている契約は何か」だけ**。テストの目録でも検査の作り方でもなく、個々のテスト関数は列挙しない(それが手動同期になる)。選択リセット verb の顔ぶれ(`resets_selection=True` の全量。`duocad/catalog.py` が正)などは文書に写さず参照だけにする。旧 `tests/README.md`(1,199行)は廃止し、契約はコードのそばへ移した。 `(20260906-0250-duocad-tests-readme-resync, 2026-09-06)`
- **契約を書き出すときは、当該テストを全文読んでから書く。ファイル名からの推測をしない。** 特に `tests/test_ts_evaluators.py` は `tests/eval_*.mjs` を node で走らせて終了コードを見るだけで、**契約の実体は `eval_*.mjs` の assert にある**。 `(20260906-0250-duocad-tests-readme-resync, 2026-09-06)`
- **`ai_*` の実体は `duocad/net/ws.py`、`duocad/server.py` は再輸出**。テストは `duocad.server.Server` を import するので、`server.py` を読みに行くと `_ai_configure` が無い。
  `(20260906-0250-duocad-tests-readme-resync, 2026-09-06)`
- **コード側コメントが指す文書の節が実在しない**例が、文書再同期の前から存在した。
  コードコメントに文書の節名を書くときは、その節が実在することを grep で確かめる。 `(20260906-0250-duocad-tests-readme-resync, 2026-09-06)`
- 契約の追加先に迷ったら、文書の目録(旧 `tests/README.md` 型)を増やすのではなく、特定の行に紐づく制約はコメント、複数ファイルにまたがる判断・手順・比較表は docs/(CLAUDE.md の方針)。

## 9. 作業環境の罠(文書・コメント・検査の保守で踏んだもの)

- **素の `python` は別 venv を拾う**(このマシンでは hermes-agent の venv が優先)。`ModuleNotFoundError: No module named 'OCP'` で**48件の収集エラー**になるが、
  `python -m pytest -q | tail` だと終了コードが0に見えて緑と誤読する。**必ず `./.venv/Scripts/python.exe -m pytest -q`**(件数を読む)。バージョン表示が偶然一致するため気づきにくい。
  `(20260906-0250-duocad-tests-readme-resync, 20260906-0600-duocad-trim-comments-tests, 20260906-0700-duocad-trim-comments-scripts-and-checks, 20260906-0900-duocad-lock-doc-code-agreement, 2026-09-06)`
- **`tools/check_code_unchanged.py` / `measure_bloat.py` の日本語出力は Windows 既定の cp932 で化ける。** `PYTHONIOENCODING=utf-8` を付け、終了コードだけで判断しない。
  `(20260906-0500-duocad-trim-comments-web-src, 20260906-0600-duocad-trim-comments-tests, 20260906-0700-duocad-trim-comments-scripts-and-checks, 2026-09-06)`
- **改行コードがファイルによって混在する**(`.gitattributes` は無く、作業ツリーは autocrlf で CRLF のファイル — `README.md` `MCP.md` `duocad/sketch.py` `tests/test_*.py` の多く — と LF のもの — `duocad/kernel.py` — が並ぶ)。`file <path>` で確かめ、Python で読み書きするときは `newline=''` で素通しする。bash の `sed`/`grep -c` は CR を見せず、heredoc で追記するとその部分だけ LF になる。行番号での切り出しも Python で。`git diff`/`git add` の "LF will be replaced by CRLF" 警告は正規化されるので無害(差分は意図した行だけ)。詳細な形は [../DEV_NOTES.md](../DEV_NOTES.md)。
  `(20260906-0100-duocad-fix-stale-docs, 20260906-0250-duocad-tests-readme-resync, 20260906-0600-duocad-trim-comments-tests, 20260906-0900-duocad-lock-doc-code-agreement, 2026-09-06)`
- **タスク本文や申し送りの行番号は着手時点で既にずれている。** 位置は見出し名と本文の一意な文字列で特定する。
  `(20260906-0100-duocad-fix-stale-docs, 20260906-0250-duocad-tests-readme-resync, 2026-09-06)`
- 文書を一括編集するときの設計の注意(行番号指定の置換に4つ要る、関数削除は regex でなく `ast` 等)は [../DEV_NOTES.md](../DEV_NOTES.md)「機械的な一括編集の道具」。

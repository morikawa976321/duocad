export const meta = {
  name: 'dead-code-sweep',
  description: 'duocad のデッドコードを、機械走査の候補の確認+領域別の精読で洗い出し、独立した2視点の反証で絞る(読み取り専用)',
  phases: [
    { title: 'Sweep', detail: '22領域それぞれで候補を確認し、機械走査が拾えないものを精読で探す' },
    { title: 'Refute', detail: '各領域の「消せる」判定を、動的参照の視点と履歴・意図の視点で反証する' },
    { title: 'Critic', detail: '網羅漏れを指摘させる' },
    { title: 'Fill gaps', detail: '指摘された漏れを同じ手順で補う' },
  ],
}

// Workflow ツールで走らせる脚本(読み取り専用)。先に sweep.py → ts_sweep.mjs → dangling.py → areas.py を回して brief を作る。
//   args = { repo: <リポジトリの絶対パス>, scratch: <一時フォルダ>, briefDir: <briefs の絶対パス + 区切り文字>,
//            areas: <areas.json の [{key, title}, ...]> }
// 領域ごとの精読 → 2視点(動的参照／履歴・意図)の反証 → 網羅漏れの批評と補完。結果は confirmed / contested / review に分かれて返る。
const REPO = args.repo
const SCRATCH = args.scratch
const BRIEF_DIR = args.briefDir

const KINDS = ['function', 'method', 'class', 'const', 'field', 'param', 'import', 'local', 'branch', 'flag', 'compat',
  'commented_out', 'duplicate', 'script', 'export', 'interface_member', 'css', 'dependency', 'test_helper', 'fixture', 'other']

const FINDING = {
  type: 'object',
  properties: {
    file: { type: 'string', description: 'リポジトリ直下からの相対パス' },
    line: { type: 'integer' },
    end_line: { type: 'integer' },
    symbol: { type: 'string', description: '名前(メソッドは Class.method)。分岐や旗なら短い説明' },
    kind: { type: 'string', enum: KINDS },
    verdict: { type: 'string', enum: ['dead', 'test_only', 'script_only', 'unsure'] },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    source: { type: 'string', enum: ['mechanical', 'reading'] },
    evidence: { type: 'string', description: '何をどの範囲で探して参照ゼロだったか、または何が分かったか。300字以内' },
    note: { type: 'string', description: '消すときの注意(連鎖して死ぬもの・同時に消すテストなど)。無ければ空' },
  },
  required: ['file', 'line', 'symbol', 'kind', 'verdict', 'confidence', 'source', 'evidence'],
}

const AREA_RESULT = {
  type: 'object',
  properties: {
    findings: { type: 'array', items: FINDING },
    mechanical_checked: { type: 'integer', description: '確認した機械候補の件数' },
    false_positives: { type: 'integer', description: '機械候補のうち、動的呼び出し・契約・名前衝突などで実際は生きていた/残すべき件数' },
    false_positive_patterns: { type: 'array', items: { type: 'string' }, description: '機械走査が誤判定した型の代表例(各1行)' },
    files_read: { type: 'array', items: { type: 'string' }, description: '実際に中身を読んだファイル' },
    coverage_note: { type: 'string', description: '読み切れていない範囲・時間配分で落とした範囲があれば正直に' },
  },
  required: ['findings', 'mechanical_checked', 'false_positives', 'false_positive_patterns', 'files_read', 'coverage_note'],
}

const VERDICTS = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          index: { type: 'integer' },
          refuted: { type: 'boolean' },
          reason: { type: 'string', description: '反証の根拠(見つけた参照の場所:行)、または反証に失敗した探索の範囲。1〜2文' },
        },
        required: ['index', 'refuted', 'reason'],
      },
    },
  },
  required: ['verdicts'],
}

const GAPS = {
  type: 'object',
  properties: {
    gaps: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          key: { type: 'string' },
          title: { type: 'string' },
          files: { type: 'array', items: { type: 'string' } },
          focus: { type: 'string' },
          reason: { type: 'string' },
        },
        required: ['key', 'title', 'files', 'focus', 'reason'],
      },
    },
    overall_note: { type: 'string' },
  },
  required: ['gaps', 'overall_note'],
}

const COMMON = [
  '## 前提(厳守)',
  '- これは**読み取り専用**の調査。リポジトリのファイルを変更・コミット・stash しない。サーバ(ポート 8765/8766/8775/8776 を含む)を立てない。pytest・npm run・node でのビルド/テストを走らせない。',
  '- 使ってよいもの: Read / Grep / Glob、git の読み取り系(git grep・git log -S・git log -G・git show・git blame)、' + REPO + '\\.venv\\Scripts\\python.exe での ast 解析(読むだけ)。一時ファイルは ' + SCRATCH + ' の下にだけ作る。',
  '- 作業ディレクトリは ' + REPO + ' 。%USERPROFILE%\\mf\\duocad-stable と duocad-data は触らない(今回は不要)。',
  '',
  '## 「デッドコード」の定義(この調査での分類)',
  '- dead: 本番コード(duocad/・web/src/)・スクリプト(tools/・topopt_checks/・web/scripts/)・テスト(tests/)のどこからも到達しない関数/メソッド/クラス/定数/フィールド/import/引数/ローカル変数。',
  '- 到達しても意味が無いもの: 常に真偽が決まる分岐、誰も切り替えない環境変数・設定の旗、対応する古い入力がもう存在しない互換・移行コード、コメントアウトされたコード、同じことをする重複実装の死んだ方。',
  '- test_only: テストからしか参照されない本番コード(消すならテストごと消す必要がある)。',
  '- script_only: topopt_checks/・tools/・web/scripts/ からしか参照されない本番コード。',
  '- 孤立スクリプト(kind=script): docs・README・別スクリプト・テストのどこからも言及されず、出力も他の入力に使われない単発スクリプト。ただし docs/ が「結論と再現コマンド」として挙げているものは生きている扱い。',
  '- 判定は非対称にする: 消して困る側が大きい。**動的に生きている疑いが少しでも残るなら dead にしない**(unsure にする)。',
  '',
  '## このリポジトリで「参照が見えなくても生きている」もの(誤検出の主因)',
  '- catalog.COMMANDS(duocad/catalog.py)が verb の唯一の宣言。WebSocket の許可リスト・MCP のツール定義・web/src/generated/protocol.ts(生成物)がここから導出される。',
  '- verb の入口は @entry("verb名")(duocad/entry.py)で登録され Session.apply が名前で引く。Session.<verb> は session.py の末尾で setattr される。oplog のレジストリは register_verb() で埋まる。',
  '- McpServer はツール名と同じ綴りのメソッドを getattr(self, name) で引く(mcp_server.py)。ws.py は getattr(self.session, command) や FILE_COMMANDS のメソッド名文字列で呼ぶ。WebHandler.do_* は http.server が名前で呼ぶ。',
  '- 文字列でメソッド/属性を引く箇所: catalog の preview_session 等、getattr(setup, role)、dataclass の asdict / __dataclass_fields__ / trace.py の _encode(getattr(...))。',
  '- duocad/__init__.py・duocad/verbs/__init__.py・duocad/server.py の noqa: F401 の import は登録/再輸出のために意図的に残してある(消さない)。server.py の再輸出は tests と mcp_server が名前を取る。',
  '- Python と TypeScript は JSON の形で結ばれる: Python の dict のキー・dataclass のフィールドを TS が読む。snake_case のキーが TS では同じ綴りで出る。',
  '- Svelte のテンプレート内の参照({foo}・onclick={foo}・bind:・class: )は ts の識別子検索では見えない。window.__duocad(testHooks.svelte.ts)は web/scripts/smoke*.mjs と tests/ が使う。tests/eval_*.mjs は TS を直接 import する。',
  '- pytest の fixture は引数名と文字列(getfixturevalue・usefixtures)で解決される。',
  '- MCP.md が「ツール一覧の正」で、tests/test_doc_code_agreement.py が表と実装の一致を機械で見る。README/docs が手順として挙げるコマンドは生きている扱い。',
  '- 経緯・設計判断は docs/(DESIGN_DECISIONS.md・DEV_NOTES.md・STATUS.md・UI.md・TOPOPT*.md ほか)にある。コードの意図を疑うときはまずそこ。',
  '',
  '## 機械走査の候補ファイルの読み方(JSON の mechanical キー)',
  '- py_defs: 名前ベースの到達性で本番から参照の無い関数/メソッド/クラス/定数/フィールド。category = DEAD(どこからも参照なし) / TEST_ONLY / SCRIPT_ONLY(tools・topopt_checks・web/scripts のみ) / STR_ONLY(文字列や他言語のテキストにだけ名前がある=動的呼び出しの疑い)。live/scripts/tests/strs/texts/docs は参照の数、sites/test_sites/str_sites は参照元の例。固定点反復で「死んだ関数からしか呼ばれない関数」も含めてある。',
  '- py_weak_refs: 参照が1〜2か所しか無いメソッド/同名定義が複数ある関数。同名の別クラスのメソッドへの参照を数えているだけかもしれない(名前衝突)。sites の参照が本当にこの定義に束縛されているかを確かめる。',
  '- py_unused_imports / py_unused_locals / py_commented_code: pyflakes 相当とコメントアウトの推定。py_unused_params: 本体で使われない引数(優先度が低い。呼び出し元を確認し、契約=宣言と同じ綴りの引数名・コールバック・プロトコル適合なら残す)。',
  '- py_orphan_modules: import も文字列参照も無いモジュール。doc_refs は docs の言及数、has_main は __main__ を持つか。',
  '- ts_defs: web/src・web/scripts・tests/*.mjs のトップレベル宣言とメンバ(interface・class・オブジェクトリテラル)で、名前ベースで参照の無いもの。kind=member/objmember は interface のフィールドやハンドルのメソッド。category は DEAD/TEST_ONLY/SCRIPT_ONLY。',
  '- ts_export_only_used_locally: export されているがファイル内でしか使われない(export が不要なだけで死んではいない。低優先。findings に入れるなら kind=export で verdict=unsure か省く)。',
  '- ts_iface_members_low_use: interface メンバで、全体の出現が2回以下。ts_test_only_exports / css_unused / tests_unused_fixtures / dangling_imports も同様。',
  '- 機械走査は名前ベースなので、見逃し(同名衝突で参照ありに見える)と誤検出(動的呼び出し)の両方がある。候補にない死んだコードを探すのも仕事。',
].join('\n')

function sweepPrompt(area) {
  const input = area.files
    ? '担当ファイル: ' + area.files.join(', ') + '\n重点: ' + area.focus + '\n(機械走査の候補ファイルは無い。自分で Grep して調べる。)'
    : '機械走査の候補と担当ファイル一覧は ' + BRIEF_DIR + area.key + '.json にある。**最初にこれを Read せよ**(files=担当ファイルと行数 / hint=この領域の動的呼び出しの癖 / mechanical=機械走査の候補)。'
  return [
    'あなたは duocad リポジトリのデッドコード調査担当。担当領域「' + area.title + '」(キー ' + area.key + ')を調べて、デッドコードを洗い出して報告する。',
    '',
    COMMON,
    '',
    '## 入力',
    input,
    '',
    '## やること',
    '1. 機械候補を**1件ずつ**確認する。定義を読み、全リポジトリ(duocad/・web/・tests/・tools/・topopt_checks/・docs/・*.md)を Grep して参照を確かめる。別名 import・getattr・文字列・TS 側・テンプレート・docs・テストを見落とさない。名前が同じだけの別実体への参照を、この定義への参照と取り違えない。',
    '   時間配分の優先順: py_defs / ts_defs → py_unused_imports・py_unused_locals・py_commented_code・py_orphan_modules・dangling_imports・tests_unused_fixtures → py_weak_refs・ts_iface_members_low_use → py_unused_params。',
    '2. **候補に無いものを精読で探す**(機械走査が拾えない分):',
    '   a. 担当ファイルの def/class/関数/メソッドの一覧を Grep で出し、公開に見えるものは呼び元が本当に実在するか確かめる(特に「呼び元が1つだけ」のものは、その呼び元自体が死んでいないか)。',
    '   b. 定数・環境変数・設定で決まる分岐、誰も立てない旗、常に同じ値を渡される引数、片方の枝が到達しない if。',
    '   c. 互換・移行・レガシー系: Grep で 旧|互換|後方|廃止|レガシー|deprecated|legacy|obsolete|unused|未使用|使われ|不要|暫定|一時|TODO|FIXME|HACK|workaround|以前は|昔 を当て、その周辺が今も必要か(対応する古い入力・古い保存形式がまだ存在するか)を確かめる。',
    '   d. コメントアウトされたコード、同じことを2通りで実装している片方、try/except で握りつぶしているが到達しない例外処理。',
    '   e. 大きいファイルは全行を読む必要は無いが、構造を掴んでから疑わしい塊を読む。読んだファイルは files_read に挙げ、読み切れなかった範囲は coverage_note に正直に書く。',
    '3. 「いつから死んだか」が1行で分かるなら evidence に入れる(git log -S "名前" --oneline -- ファイル で、最後に呼び元が消えたコミット)。',
    '4. dead と判断する条件: 上の探索で参照が1つも無い(自分自身の本体内を除く)こと、かつ動的呼び出しの経路に乗っていないこと。テストだけが参照するなら test_only、スクリプトだけなら script_only。迷うなら unsure(confidence=low も可)。',
    '',
    '## 出力(スキーマに従う)',
    '- findings には verdict が dead / test_only / script_only / unsure のものだけを入れる。動的に生きていた・契約として残すべきもの・名前衝突だったものは入れず、false_positives に件数、false_positive_patterns に型の代表例を1行ずつ。',
    '- 同種の細かいもの(未使用 import が20件など)は、1件ずつ findings に入れてよい(後で集計する)。evidence は300字以内。',
    '- mechanical_checked は確認した機械候補の件数。確認し切れなかった分があれば coverage_note に書く。',
    '- 消した結果ほかが連鎖して死ぬ場合は note に書く。',
  ].join('\n')
}

const LENS = {
  dynamic: [
    '## あなたの視点: 動的参照・登録・契約',
    '次のどれかで実は生きていないかを、Grep と読解で**積極的に探す**:',
    '- getattr / setattr / 文字列キー / デコレータ登録(@entry・register_verb など) / catalog 宣言経由 / dict のディスパッチ表。',
    '- Python⇄TypeScript の JSON の形(キーの綴りは同じか、camelCase⇄snake_case の変換が挟まるか)、web/src/generated/protocol.ts、Svelte のテンプレート、window.__duocad、tests/eval_*.mjs による TS の直 import、smoke*.mjs。',
    '- __all__・再輸出・TYPE_CHECKING・文字列の型注釈・pytest fixture の引数名。',
    '- **名前衝突**: 参照だと思っていたものが別クラスの同名メソッドへの参照だった、逆に、本当の呼び元が別の綴りや別名 import で書かれていた。',
    '「別の綴り」でも Grep を当てる(大文字小文字・snake/camel・接頭辞)。',
  ].join('\n'),
  intent: [
    '## あなたの視点: 履歴・意図・文書上の契約',
    '次のどれかで残すべきでないかを、git と文書で**積極的に探す**:',
    '- git log -S / -G と git show で追加の経緯と直近の変更理由。**直近(数日以内)の追加で、まだ配線の途中**の可能性が無いか(git log --since="14 days ago")。',
    '- docs/(DESIGN_DECISIONS.md・DEV_NOTES.md・STATUS.md・UI.md・TOPOPT*.md・THREAD_SIM.md ほか)・README.md・MCP.md・CLAUDE.md・コード内コメントが、それを「意図的に残す」「再開時に使う」「公開面として固定」「テストが固定」と書いていないか。docs の手順・再現コマンドが参照していないか。',
    '- 消すと tests が落ちるか(test_only なら、そのテストが固定している契約は何か。その契約自体が今も要るか)。tests/test_doc_code_agreement.py・test_boundaries.py・test_protocol.py のような「一致」を見る検査に引っかからないか。',
    '- スクリプトなら、docs が結論と再現コマンドを持つ実験記録か、後続のスクリプトの入力を作っているか。',
  ].join('\n'),
}

function refutePrompt(area, part, lens) {
  const items = part.map((f) => ({
    index: f.index, file: f.file, line: f.line, end_line: f.end_line, symbol: f.symbol, kind: f.kind,
    verdict: f.verdict, confidence: f.confidence, evidence: f.evidence, note: f.note,
  }))
  return [
    'あなたは duocad リポジトリのデッドコード判定の**反証担当**。別の調査員が領域「' + area.title + '」(' + area.key + ')で「これはデッドコードで消せる」と判定した項目を、独立に疑って覆そうとする。',
    '',
    COMMON,
    '',
    LENS[lens],
    '',
    '## 判定すべき項目(JSON)',
    JSON.stringify(items, null, 1),
    '',
    '## やること',
    '- 各項目について、まず定義を読み、上の視点で反証を試みる。調査員の evidence を鵜呑みにしない(再度 Grep する)。',
    '- **refuted の既定は true**: 「消して安全」と positive に示せない限り refuted=true。反証できなかったこと(refuted=false)は、あなたの視点で探せる限り探して何も出なかったことを意味する。',
    '- verdict が test_only の項目は「テスト以外から参照が無い」ことが主張。非テストの参照があれば refuted=true。script_only も同様(本番からの参照があれば refuted=true)。',
    '- reason は、見つけた参照の場所(ファイル:行)か、反証に失敗した探索の範囲(何を Grep したか)を1〜2文で。',
    '- 全項目について index を付けて verdicts を返す。',
  ].join('\n')
}

const chunk = (arr, n) => {
  const out = []
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n))
  return out
}
const REMOVABLE = ['dead', 'test_only', 'script_only']

async function sweepStage(area, phaseName) {
  return await agent(sweepPrompt(area), { label: 'sweep:' + area.key, phase: phaseName, schema: AREA_RESULT, model: 'sonnet' })
}

async function refuteStage(res, area, phaseName) {
  if (!res) return null
  const findings = (res.findings || []).map((f, i) => Object.assign({}, f, { index: i }))
  const testable = findings.filter((f) => REMOVABLE.includes(f.verdict) && f.confidence !== 'low')
  const votes = {}
  if (testable.length) {
    const thunks = []
    for (const part of chunk(testable, 25)) {
      for (const lens of ['dynamic', 'intent']) {
        thunks.push(async () => {
          const r = await agent(refutePrompt(area, part, lens), {
            label: 'refute:' + lens + ':' + area.key, phase: phaseName, schema: VERDICTS, model: 'sonnet',
          })
          return { lens, r }
        })
      }
    }
    const done = await parallel(thunks)
    for (const d of done.filter(Boolean)) {
      for (const v of (d.r && d.r.verdicts) || []) {
        if (!votes[v.index]) votes[v.index] = []
        votes[v.index].push({ lens: d.lens, refuted: v.refuted, reason: v.reason })
      }
    }
  }
  log(area.key + ': 候補 ' + findings.length + ' 件 / 反証にかけた ' + testable.length + ' 件')
  return { area: area.key, title: area.title, res, findings, testable: testable.map((f) => f.index), votes }
}

function classify(items) {
  const confirmed = [], contested = [], unverified = [], review = []
  for (const it of items.filter(Boolean)) {
    for (const f of it.findings) {
      const base = Object.assign({}, f, { area: it.area })
      if (!it.testable.includes(f.index)) { review.push(base); continue }
      const vs = it.votes[f.index] || []
      if (vs.length === 0) unverified.push(base)
      else if (vs.some((v) => v.refuted)) contested.push(Object.assign({}, base, { votes: vs }))
      else confirmed.push(Object.assign({}, base, { votes: vs, full_vote: vs.length >= 2 }))
    }
  }
  return { confirmed, contested, unverified, review }
}

// ------------------------------------------------------------------ 本体
const AREAS = args.areas
log('領域 ' + AREAS.length + ' 件を精読+反証にかける')

phase('Sweep')
const first = await pipeline(
  AREAS,
  (area) => sweepStage(area, 'Sweep'),
  (res, area) => refuteStage(res, area, 'Refute'),
)
const firstOk = first.filter(Boolean)
log('第1巡 完了: ' + firstOk.length + '/' + AREAS.length + ' 領域')
const failed = AREAS.filter((a) => !firstOk.some((x) => x.area === a.key)).map((a) => a.key)
if (failed.length) log('結果が返らなかった領域: ' + failed.join(', '))

phase('Critic')
const coverage = firstOk.map((it) => ({
  key: it.area, title: it.title,
  files_read: (it.res.files_read || []).length,
  coverage_note: it.res.coverage_note,
  mechanical_checked: it.res.mechanical_checked,
  false_positives: it.res.false_positives,
  fp_patterns: it.res.false_positive_patterns,
  findings: it.findings.length,
}))
const critique = await agent([
  'あなたは duocad のデッドコード調査の**網羅性の批評担当**。22 領域の調査員が報告した範囲と、機械走査の限界を踏まえて、見落としていそうな範囲を指摘する。',
  '',
  COMMON,
  '',
  '## 領域ごとの報告(調査員の自己申告)',
  JSON.stringify(coverage, null, 1),
  '',
  '## 領域の定義',
  '領域ごとの担当ファイル一覧は ' + BRIEF_DIR + '<key>.json の files にある(必要なものだけ Read する)。',
  '',
  '## やること',
  '1. coverage_note と files_read から、**読み切れていない大きいファイル**や、調査員が「時間配分で落とした」と書いた範囲を拾う。',
  '2. 領域の割当てから漏れているもの(リポジトリ直下の他のコード・設定・.claude/ の定義、web/public、docs/ が参照する存在しないスクリプト名、pyproject/package.json の依存など)を git ls-files で確かめる。',
  '3. 機械走査と調査員が共通して見落としやすい型(同名衝突で参照ありに見えるメソッド、カタログ宣言の死んだ項目=どの面からも使われない verb/パラメータ/機能旗、環境変数の読み取りで誰も設定しないもの)について、**あなた自身がいくつか Grep で当たりをつけ**、実在するなら gap の focus にその具体例を書く。',
  '4. 補完が要る範囲を gaps に最大3件返す(key は gap1 など、files はリポジトリ相対パスの配列で実在するもの、focus は何をどう調べるか)。補完が要らなければ空配列。',
  '- overall_note に、調査全体の弱点(何を保証できていないか)を率直に書く。',
].join('\n'), { label: 'critic', phase: 'Critic', schema: GAPS, model: 'sonnet' })

let second = []
const gaps = ((critique && critique.gaps) || []).slice(0, 3)
if (gaps.length) {
  phase('Fill gaps')
  log('補完 ' + gaps.length + ' 件: ' + gaps.map((g) => g.key + '(' + g.files.length + 'ファイル)').join(', '))
  second = await pipeline(
    gaps,
    (g) => sweepStage(g, 'Fill gaps'),
    (res, g) => refuteStage(res, g, 'Fill gaps'),
  )
}

const all = classify(firstOk.concat(second.filter(Boolean)))
const stats = {
  areas: AREAS.length,
  areas_returned: firstOk.length,
  areas_failed: failed,
  mechanical_checked: firstOk.reduce((s, x) => s + (x.res.mechanical_checked || 0), 0),
  false_positives: firstOk.reduce((s, x) => s + (x.res.false_positives || 0), 0),
  confirmed: all.confirmed.length,
  contested: all.contested.length,
  unverified: all.unverified.length,
  review: all.review.length,
}
log('確定 ' + stats.confirmed + ' / 反証された ' + stats.contested + ' / 反証が返らず ' + stats.unverified + ' / 要判断 ' + stats.review)

return {
  stats,
  fp_patterns: firstOk.map((x) => ({ area: x.area, patterns: x.res.false_positive_patterns })).filter((x) => x.patterns && x.patterns.length),
  coverage_notes: coverage.map((c) => ({ key: c.key, files_read: c.files_read, note: c.coverage_note })),
  critic: critique,
  confirmed: all.confirmed,
  contested: all.contested.map((f) => ({
    area: f.area, file: f.file, line: f.line, symbol: f.symbol, kind: f.kind, verdict: f.verdict,
    refutations: f.votes.filter((v) => v.refuted).map((v) => v.lens + ': ' + v.reason),
  })),
  unverified: all.unverified,
  review: all.review,
}
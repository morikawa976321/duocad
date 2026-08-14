/**
 * ファイル操作のコマンド(タスクE・2026-08-14) — **作ったものが手元に残る場所**。
 *
 * 保存・開く・書き出し・参照部品の取り込みは、それまで**MCPからしか呼べなかった**
 * (人はブラウザで作れても、作ったものを保存できなかった)。ここが埋めるのはその片道で、
 * コアには何も足していない —— `duocad/server.py` の `FILE_COMMANDS` に口を開け、
 * ここに引数の集め方を書くだけである(C・Dと同じ形)。
 *
 * ## ファイル操作も「コマンド」として載せる
 *
 * タスクEの制約が「Bの骨格を使う(ファイル操作もコマンドとして載せる)」。ボタンと
 * ダイアログを別に作らないので、**Escで中断できる・引数が揃うまで確定しない・
 * 何を待っているか1行で出る**が、押し出しやフィレットと同じ仕組みでそのまま効く。
 * 骨格へ足したのは**文字列(text)引数**だけで、これは「パスを打つ」ための3種類目の
 * 打ち込み(数値でも選択肢でもない)である。
 *
 * ## 保存形式は操作ログのJSON(§3.1)
 *
 * **これが正**で、形状は導出物である。したがって「開く」は再生であり、
 * **開くたびに再生経路が検査される**ことになる。gitに載り、AIが読め、1モデル1ファイル。
 *
 * ## パスの解釈はサーバに任せる
 *
 * 相対パスはサーバの作業ディレクトリ基準で解決され、**実際に書いた絶対パスが応答で返る**。
 * ブラウザ側で正規化すると、解釈が2箇所に分かれて「保存したと言われたファイルが
 * 見つからない」が起きる。
 */
import type { CommandDef } from './command'
import type { SelectionRef, ShapeRef } from '../session'

/** 空のパスだけ弾く。**それ以上の判定はサーバに任せる**(上のdocstring)。 */
function pathLike(label: string): (value: string) => string | null {
  return (value) => (value.trim() === '' ? `${label}を打つ` : null)
}

export const SAVE_DOCUMENT: CommandDef = {
  id: 'save_document',
  label: '保存',
  buttonId: 'file-save',
  preview: 'none',
  args: [
    {
      kind: 'text',
      name: 'path',
      label: '保存先',
      inputId: 'file-path',
      placeholder: 'model.json',
      reject: pathLike('保存先'),
    },
  ],
  build(values) {
    const path = values.path
    if (typeof path !== 'string' || path.trim() === '') return { ok: false, reason: '保存先が空' }
    return { ok: true, args: { path: path.trim() } }
  },
}

export const OPEN_DOCUMENT: CommandDef = {
  id: 'open_document',
  label: '開く',
  buttonId: 'file-open',
  preview: 'none',
  args: [
    {
      kind: 'text',
      name: 'path',
      label: '開く',
      inputId: 'file-path',
      placeholder: 'model.json',
      reject: pathLike('開くファイル'),
    },
  ],
  build(values) {
    const path = values.path
    if (typeof path !== 'string' || path.trim() === '') return { ok: false, reason: '開く先が空' }
    return { ok: true, args: { path: path.trim() } }
  },
}

/**
 * STEPの読み込み。**読んだ時点で編集できる**(2026-08-15)。ソリッドごとに編集用ボディが
 * でき、そのまま面をクリックして押し出せる(原本の参照ボディは隠したまま文書に残る)。
 *
 * 以前は参照ボディ(`kind="reference"`)だけが増え、編集するには `make_editable_copy` を
 * 1ボディずつ打つ必要があった —— PCケース27ソリッドなら27回。**起動時の `--step` でしか
 * 編集可能な状態が作れない**というこの非対称のせいで起動オプションが消せなかったので、
 * 起動時の手順をそのままverb1本(`import_step_editable`)にしてここへ載せた。
 */
export const IMPORT_STEP: CommandDef = {
  id: 'import_step',
  label: 'STEP読み込み',
  buttonId: 'file-import-step',
  preview: 'none',
  args: [
    {
      kind: 'text',
      name: 'path',
      label: 'STEP',
      inputId: 'file-path',
      placeholder: 'part.step',
      reject: pathLike('STEPのパス'),
    },
  ],
  build(values) {
    const path = values.path
    if (typeof path !== 'string' || path.trim() === '') return { ok: false, reason: 'パスが空' }
    return { ok: true, args: { path: path.trim() } }
  },
}

export const EXPORT_STEP: CommandDef = {
  id: 'export_step',
  label: 'STEP書き出し',
  buttonId: 'file-export-step',
  preview: 'none',
  args: [
    {
      kind: 'text',
      name: 'path',
      label: '書き出し先',
      inputId: 'file-path',
      placeholder: 'out.step',
      reject: pathLike('書き出し先'),
    },
  ],
  build(values) {
    const path = values.path
    if (typeof path !== 'string' || path.trim() === '') return { ok: false, reason: '書き出し先が空' }
    return { ok: true, args: { path: path.trim() } }
  },
}

/**
 * STL書き出し。**偏差を必ず打たせる**(既定で埋めない)。
 *
 * 実測: 0.2mm→30.7KB / 0.05mm→47.2KB / 0.01mm→170.6KB(§7)。用途で桁が変わる値なので、
 * 黙って既定値で出すと「粗いまま3Dプリンタへ送った」が起きる。
 */
export const EXPORT_STL: CommandDef = {
  id: 'export_stl',
  label: 'STL書き出し',
  buttonId: 'file-export-stl',
  preview: 'none',
  args: [
    {
      kind: 'text',
      name: 'path',
      label: '書き出し先',
      inputId: 'file-path',
      placeholder: 'out.stl',
      reject: pathLike('書き出し先'),
    },
    {
      kind: 'number',
      name: 'deviation',
      label: '偏差',
      unit: 'mm',
      inputId: 'file-deviation',
      placeholder: 'mm',
      reject: (value) => (value > 0 ? null : '偏差は0より大きい値'),
    },
  ],
  build(values) {
    const path = values.path
    const deviation = values.deviation
    if (typeof path !== 'string' || path.trim() === '') return { ok: false, reason: '書き出し先が空' }
    if (typeof deviation !== 'number') return { ok: false, reason: '偏差が数値でない' }
    return { ok: true, args: { path: path.trim(), deviation } }
  },
}

/**
 * 参照ボディを編集できるようにする。**面をクリックして選ぶ**のが引数の集め方。
 *
 * 「どのボディか」を名前で打たせないのは、画面に見えているものを指せるなら
 * それが一番短い経路だからである(引数収集はまず共有選択集合を見る、というBの規律)。
 *
 * **2026-08-15以降、UIから指せるのは編集用ボディだけになった**。STEP読み込みが編集用ボディ
 * まで作るようになり、参照ボディは隠されてパーツ一覧にも出ない(一覧は `kind === "solid"`
 * だけを並べる)ため、UIから参照を指す経路そのものが無い。**このコマンドはボディの複製**
 * として残っている(選んだ面を持つボディの形をもう1つ作る)——
 * 参照ボディを起点に使うのはMCP側(`import_step` を `editable=false` で呼んだ後)である。
 */
export const MAKE_EDITABLE_COPY: CommandDef = {
  id: 'make_editable_copy',
  label: '編集可能にする',
  buttonId: 'file-make-editable',
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'source',
      label: '元にする面',
      shapeKind: 'face',
      min: 1,
      max: 1,
    },
  ],
  build(values) {
    const faces = (values.source ?? []) as SelectionRef[]
    const face = faces[0] as ShapeRef | undefined
    if (!face) return { ok: false, reason: '面が選ばれていない' }
    return { ok: true, args: { source_body_id: face.body } }
  },
}

/** ファイル操作のツールバー(ソリッドを触るモードに並ぶ)。 */
export const FILE_COMMANDS: CommandDef[] = [
  SAVE_DOCUMENT,
  OPEN_DOCUMENT,
  IMPORT_STEP,
  EXPORT_STEP,
  EXPORT_STL,
  MAKE_EDITABLE_COPY,
]

/** Eが足したverb(`App.svelte` が送信の分岐に使う)。 */
export function isFileVerb(verb: string): boolean {
  return FILE_COMMANDS.some((command) => command.id === verb)
}

/**
 * そのverbは**文書を変えるか**(＝応答が状態配信で返るか)。
 *
 * 保存と書き出しは文書を変えないので、応答は `{"type":"file"}` で要求した接続にだけ返る。
 * 送った側が「応答待ち」を解く条件が違うので、ここで見分けられるようにしてある。
 */
export function changesDocument(verb: string): boolean {
  return verb === OPEN_DOCUMENT.id || verb === IMPORT_STEP.id || verb === MAKE_EDITABLE_COPY.id
}

/**
 * ファイル操作のコマンド。コアには何も足していない —— `duocad/server.py` の `FILE_COMMANDS`
 * に口を開け、ここに引数の集め方を書くだけである(スケッチ・フィーチャと同じ形)。
 *
 * **保存/開くだけは `CommandHost` に無い**。`duocad/models/` 配下の**ファイル名の一覧から
 * 選ぶ**形なので、`FilesPanel.svelte`(一覧・クリックで開く・上書き・新規保存)が担当し、
 * `session.runVerb(...)` を直接呼ぶ(`SketchPanel.svelte` の `onSetParameter` と同じ形)。
 *
 * **保存形式は操作ログのJSON**で、形状は導出物である。したがって「開く」は再生であり、
 * **開くたびに再生経路が検査される**ことになる。
 *
 * **パスの解釈はサーバに任せる**(書き出し・読み込みの経路)。相対パスはサーバの作業
 * ディレクトリ基準で解決され、**実際に書いた絶対パスが応答で返る**。ブラウザ側で正規化すると、
 * 解釈が2箇所に分かれて「保存したと言われたファイルが見つからない」が起きる。**保存/開く
 * (`FilesPanel.svelte`)はこの経路を使わない** —— 受け取るのはファイル名だけで、`models_dir`
 * 配下に閉じる解決はサーバ側の `Session._resolve_document_name` が行う。
 */
import { isPlaneArgValue } from './command'
import type { CommandDef } from './command'

/** 空のパスだけ弾く。**それ以上の判定はサーバに任せる**(上のdocstring)。 */
function pathLike(label: string): (value: string) => string | null {
  return (value) => (value.trim() === '' ? `${label}を打つ` : null)
}

/**
 * STEPの読み込み。**読んだ時点で編集できる**。ソリッドごとに編集用ボディができ、そのまま面を
 * クリックして押し出せる(原本の参照ボディは隠したまま文書に残る)。
 *
 * **STEP読み込みのコマンドはこれ1つだけ**(2026-08-27)。参照ボディだけを足す読み込みと、
 * それを1ボディずつ編集可能にする複製のコマンドは消えた —— 27部品なら27手かかる形であり、
 * 読み込みが1操作(ログ1行・undo1回)であるという規律とも噛み合わなかった。
 */
export const IMPORT_STEP: CommandDef = {
  // **id == サーバの表のコマンド名 == ログのverb名**。
  id: 'import_step',
  category: 'file',
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

/**
 * SVGを**1枚のスケッチ**として読み込む。外で描いた輪郭(ロゴ・図面・アウトライン化した文字)を
 * 持ち込んで押し出すための入口で、読んだ時点で領域として使える。
 *
 * **引数の順は verb → target → how**(UI設計指針)。先に貼る先をクリックで指し、次にパスを打つ。
 * 貼る先は `sketch_create` と同じ `base_plane` 引数で、基準平面ならその名前を送り、**面なら
 * 何も送らない**(サーバがコアの確定選択から取る)。
 *
 * **倍率(`scale`)はここに出していない** —— 単位の合わないSVGを直すための逃げ道であって、
 * 普段は要らない。常に出すと「毎回1.0と打つ欄」が1つ増える(`catalog` の `ws=false`)。
 */
export const IMPORT_SVG: CommandDef = {
  // **id == サーバのコマンド名 == ログのverb名**。
  id: 'import_svg',
  category: 'file',
  label: 'SVG読み込み',
  buttonId: 'file-import-svg',
  preview: 'none',
  args: [
    { kind: 'plane', name: 'target', label: 'スケッチを貼る先' },
    {
      kind: 'text',
      name: 'path',
      label: 'SVG',
      inputId: 'file-svg-path',
      placeholder: 'logo.svg',
      reject: pathLike('SVGのパス'),
    },
  ],
  build(values) {
    const plane = values.target
    if (!isPlaneArgValue(plane)) return { ok: false, reason: '貼る先が決まっていない' }
    const path = values.path
    if (typeof path !== 'string' || path.trim() === '') return { ok: false, reason: 'パスが空' }
    // **面は送らない**(サーバがコアの確定選択から取る)。基準平面だけが引数になる。
    return {
      ok: true,
      args:
        plane === 'face'
          ? { path: path.trim() }
          : { path: path.trim(), base_plane: plane },
    }
  },
}

export const EXPORT_STEP: CommandDef = {
  id: 'export_step',
  category: 'file',
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
  category: 'file',
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
 * ファイル操作のツールバー(ソリッドを触るモードに並ぶ)。
 *
 * **保存/開くはここに無い**(上のモジュールdocstring参照)。`FilesPanel.svelte` が
 * `session.runVerb('save_document' | 'open_document' | 'list_documents', ...)` を
 * 直接呼ぶので、`CommandHost` の引数収集(ここでの `CommandDef` 宣言)を経由しない。
 */
export const FILE_COMMANDS: CommandDef[] = [
  IMPORT_STEP,
  IMPORT_SVG,
  EXPORT_STEP,
  EXPORT_STL,
]

/**
 * そのverbは**文書を変えるか**(＝応答が状態配信で返るか)。
 *
 * 保存と書き出しは文書を変えないので、応答は `{"type":"file"}` で要求した接続にだけ返る。
 * 送った側が「応答待ち」を解く条件が違うので、ここで見分けられるようにしてある。
 * `open_document` はこの一覧を経由しなくなったが、**verb名としては変わっていない**ので判定は
 * そのまま残す。
 */
export function changesDocument(verb: string): boolean {
  return verb === 'open_document' || verb === IMPORT_STEP.id || verb === IMPORT_SVG.id
}

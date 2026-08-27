/**
 * ファイル操作のコマンド。コアには何も足していない —— `duocad/server.py` の `FILE_COMMANDS`
 * に口を開け、ここに引数の集め方を書くだけである(スケッチ・フィーチャと同じ形)。
 *
 * **保存/開く/STEP読み込み/STEP書き出し/STL書き出しは `CommandHost` に無い**。
 * `FilesPanel.svelte`(左サイドバー)が一覧・クリックで開く・上書き・新規保存に続けて
 * この3つも担当し、`session.runVerb(...)` を直接呼ぶ(`SketchPanel.svelte` の
 * `onSetParameter` と同じ形。2026-08-27にツールバーの`CommandHost`から移設した)。
 * **`FILE_COMMANDS`(下)に残るのは `IMPORT_SVG` だけ** —— スケッチへ貼る先を選ぶ操作なので
 * 引数収集の状態機械(`CommandHost`)を経由する理由がある。
 *
 * **保存形式は操作ログのJSON**で、形状は導出物である。したがって「開く」は再生であり、
 * **開くたびに再生経路が検査される**ことになる。
 *
 * **パスの解釈はサーバに任せる**(書き出し・読み込みの経路)。相対パスはサーバの作業
 * ディレクトリ基準で解決され、**実際に書いた絶対パスが応答で返る**。ブラウザ側で正規化すると、
 * 解釈が2箇所に分かれて「保存したと言われたファイルが見つからない」が起きる。**保存/開く/
 * STEP・STLの入出力(`FilesPanel.svelte`)はこの経路を使わない** —— 受け取るのは
 * ファイル名・パスの文字列だけで、解決はサーバ側(`Session._resolve_document_name` /
 * 各verbの実装)が行う。
 */
import { isPlaneArgValue } from './command'
import type { CommandDef } from './command'

/** 空のパスだけ弾く。**それ以上の判定はサーバに任せる**(上のdocstring)。 */
function pathLike(label: string): (value: string) => string | null {
  return (value) => (value.trim() === '' ? `${label}を打つ` : null)
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

/**
 * ファイル操作のツールバー(ソリッドを触るモードに並ぶ)。
 *
 * **保存/開く/STEP読み込み/STEP書き出し/STL書き出しはここに無い**(上のモジュール
 * docstring参照)。`FilesPanel.svelte` が
 * `session.runVerb('save_document' | 'open_document' | 'new_document' | 'list_documents' |
 * 'import_step' | 'export_step' | 'export_stl', ...)` を直接呼ぶので、`CommandHost` の
 * 引数収集(ここでの `CommandDef` 宣言)を経由しない。
 */
export const FILE_COMMANDS: CommandDef[] = [
  IMPORT_SVG,
]

/**
 * そのverbは**文書を変えるか**(＝応答が状態配信で返るか)。
 *
 * 保存と書き出しは文書を変えないので、応答は `{"type":"file"}` で要求した接続にだけ返る。
 * 送った側が「応答待ち」を解く条件が違うので、ここで見分けられるようにしてある。
 * `open_document` / `import_step` はこの一覧(`FILE_COMMANDS`)を経由しなくなったが、
 * **verb名としては変わっていない**ので判定はそのまま残す(`'import_step'` は文字どおりの
 * verb名 —— かつては `IMPORT_STEP.id` を参照していたが、そのCommandDefは
 * `FilesPanel.svelte` への移設で不要になり削除した)。
 */
export function changesDocument(verb: string): boolean {
  return verb === 'open_document' || verb === 'import_step' || verb === IMPORT_SVG.id
}

export const DROP_GUIDE = 'STEP(.step・.stp)は通常モード、SVG(.svg)はスケッチを開いて、1ファイルずつ落とす'
export const DROP_FILES_ONLY = '落とせるのは STEP(.step・.stp)か SVG(.svg)の1ファイルだけ(フォルダは取り込めない)'

export type DroppedFile = { name: string; directory?: boolean }
export type DropState = {
  connected: boolean
  documentOpen: boolean
  mode: string
  waiting: boolean
  commandActive: boolean
}
export type DropDecision = { verb: 'import_step' | 'import_svg' } | { message: string }

export function decideDroppedFiles(files: readonly DroppedFile[], state: DropState): DropDecision {
  if (files.length !== 1 || files[0].directory || !/\.(step|stp|svg)$/i.test(files[0].name)) {
    return { message: DROP_FILES_ONLY }
  }
  if (!state.connected) return { message: 'サーバに繋がっていないのでファイルを取り込めない' }
  if (!state.documentOpen) return { message: '文書を開いていないのでファイルを取り込めない' }
  if (state.waiting) return { message: '処理中なのでファイルを取り込めない(終わってから落とす)' }
  if (state.mode !== 'idle' && state.mode !== 'sketch' && state.mode !== 'sketch-pending') {
    return { message: 'このモードではファイルを取り込めない(通常モードに戻ってから落とす)' }
  }
  const svg = /\.svg$/i.test(files[0].name)
  if (svg && state.mode !== 'sketch') return { message: 'SVG はスケッチの中でしか取り込めない(スケッチを開いてから落とす)' }
  if (!svg && state.mode !== 'idle') return { message: 'STEP はスケッチの中では取り込めない(スケッチを閉じてから落とす)' }
  if (state.commandActive) return { message: 'コマンドの途中なのでファイルを取り込めない(確定か中断をしてから落とす)' }
  return { verb: svg ? 'import_svg' : 'import_step' }
}

export function attachFileDrop(target: Window, options: {
  show: (visible: boolean) => void
  drop: (files: File[], descriptions: DroppedFile[]) => void
}): () => void {
  let depth = 0
  const isFiles = (event: DragEvent) => event.dataTransfer?.types.includes('Files') === true
  const hide = () => { depth = 0; options.show(false) }
  const enter = (event: DragEvent) => {
    if (!isFiles(event)) return
    event.preventDefault()
    depth += 1
    options.show(true)
  }
  const leave = () => {
    if (depth === 0) return
    depth -= 1
    if (depth === 0) options.show(false)
  }
  const over = (event: DragEvent) => {
    if (!isFiles(event)) return
    event.preventDefault()
    if (event.dataTransfer !== null) event.dataTransfer.dropEffect = 'copy'
  }
  const drop = (event: DragEvent) => {
    if (!isFiles(event)) return
    event.preventDefault()
    event.stopPropagation()
    hide()
    const transfer = event.dataTransfer!
    const files = Array.from(transfer.files)
    const items = Array.from(transfer.items).filter((item) => item.kind === 'file')
    const descriptions = items.length === 0 ? files : items.map((item) => ({
      name: item.getAsFile()?.name ?? '',
      directory: item.webkitGetAsEntry()?.isDirectory ?? item.getAsFile() === null,
    }))
    options.drop(files, descriptions)
  }
  const key = (event: KeyboardEvent) => {
    if (depth === 0 || event.key !== 'Escape' || event.isComposing || event.keyCode === 229) return
    hide()
    event.preventDefault()
    event.stopImmediatePropagation()
  }
  target.addEventListener('dragenter', enter, true)
  target.addEventListener('dragleave', leave, true)
  target.addEventListener('dragover', over, true)
  target.addEventListener('drop', drop, true)
  target.addEventListener('keydown', key, true)
  target.addEventListener('blur', hide)
  return () => {
    target.removeEventListener('dragenter', enter, true)
    target.removeEventListener('dragleave', leave, true)
    target.removeEventListener('dragover', over, true)
    target.removeEventListener('drop', drop, true)
    target.removeEventListener('keydown', key, true)
    target.removeEventListener('blur', hide)
    hide()
  }
}

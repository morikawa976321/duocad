// web/src, web/scripts, tests/*.mjs のトップレベル宣言とメンバ(interface・class・オブジェクトリテラル)の到達性(名前ベース+固定点)。
// 使い方: node tools/dead_code/ts_sweep.mjs [出力JSON]     (既定は output/dead_code/ts_candidates.json)
// tsc / svelte-check の noUnusedLocals が拾わない「死んだ宣言からしか呼ばれない宣言」と「誰も読まない interface のフィールド」を出す。
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')
const outPath = process.argv[2] ?? path.join(root, 'output', 'dead_code', 'ts_candidates.json')
const require = createRequire(path.join(root, 'web', 'package.json'))
const ts = require('typescript')

const files = execFileSync('git', ['ls-files', '-z'], { cwd: root }).toString('utf8').split('\0').filter(Boolean)
const targets = files.filter(
  (f) =>
    (f.startsWith('web/src/') && /\.(ts|svelte)$/.test(f)) ||
    (f.startsWith('web/scripts/') && f.endsWith('.mjs')) ||
    (f.startsWith('tests/') && f.endsWith('.mjs')),
)

const zoneOf = (f) =>
  f.startsWith('web/src/') ? 'web' : f.startsWith('web/scripts/') ? 'webscripts' : f.startsWith('tests/') ? 'tests' : 'other'

// ---- ファイルを「ソース断片」に分ける(.svelte は script ブロックとテンプレート)
const pieces = [] // {file, kind:'ts'|'template', text, lineOffset}
for (const f of targets) {
  const text = fs.readFileSync(path.join(root, f), 'utf8')
  if (f.endsWith('.svelte')) {
    const re = /<script([^>]*)>([\s\S]*?)<\/script>/g
    let m
    let rest = text
    while ((m = re.exec(text))) {
      const before = text.slice(0, m.index + m[0].indexOf('>') + 1)
      pieces.push({ file: f, kind: 'ts', text: m[2], lineOffset: before.split('\n').length - 1 })
      rest = rest.replace(m[0], m[0].replace(/[^\n]/g, ' '))
    }
    rest = rest.replace(/<style[\s\S]*?<\/style>/g, (s) => s.replace(/[^\n]/g, ' '))
    pieces.push({ file: f, kind: 'template', text: rest, lineOffset: 0 })
  } else {
    pieces.push({ file: f, kind: 'ts', text, lineOffset: 0 })
  }
}

const decls = [] // {id,file,name,kind,line,end,exported,nameNode}
const refs = new Map() // name -> [{file,line,zone}]
const addRef = (name, file, line) => {
  if (!refs.has(name)) refs.set(name, [])
  refs.get(name).push({ file, line, zone: zoneOf(file) })
}

for (const piece of pieces) {
  if (piece.kind === 'template') {
    const lines = piece.text.split('\n')
    lines.forEach((ln, i) => {
      for (const w of ln.match(/[A-Za-z_$][\w$]*/g) ?? []) addRef(w, piece.file, i + 1)
    })
    continue
  }
  const sf = ts.createSourceFile(piece.file, piece.text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS)
  const lineOf = (pos) => sf.getLineAndCharacterOfPosition(pos).line + 1 + piece.lineOffset
  const declNameNodes = new Set()

  const addDecl = (nameNode, node, kind, exported, container) => {
    if (!nameNode) return
    declNameNodes.add(nameNode)
    decls.push({
      id: decls.length,
      file: piece.file,
      name: nameNode.text ?? nameNode.getText(sf),
      kind,
      line: lineOf(node.getStart(sf)),
      end: lineOf(node.getEnd()),
      exported,
      container,
    })
  }
  const hasExport = (node) => !!node.modifiers?.some((m) => m.kind === ts.SyntaxKind.ExportKeyword)

  const memberKinds = (parentName, members, exported) => {
    for (const mem of members) {
      if (
        ts.isMethodDeclaration(mem) ||
        ts.isPropertyDeclaration(mem) ||
        ts.isGetAccessor(mem) ||
        ts.isSetAccessor(mem) ||
        ts.isMethodSignature(mem) ||
        ts.isPropertySignature(mem)
      ) {
        if (mem.name && (ts.isIdentifier(mem.name) || ts.isStringLiteral(mem.name)))
          addDecl(mem.name, mem, 'member', exported, parentName)
      }
    }
  }
  const objectLiteralMembers = (parentName, obj) => {
    for (const mem of obj.properties) {
      if (ts.isMethodDeclaration(mem) || ts.isPropertyAssignment(mem) || ts.isGetAccessor(mem)) {
        if (mem.name && ts.isIdentifier(mem.name)) addDecl(mem.name, mem, 'objmember', false, parentName)
      } else if (ts.isShorthandPropertyAssignment(mem)) {
        // `{ foo }` は foo の参照。メンバ宣言としては数えない(参照として下で数える)。
      }
    }
  }

  const visitTop = (stmt) => {
    const exported = hasExport(stmt)
    if (ts.isFunctionDeclaration(stmt) && stmt.name) addDecl(stmt.name, stmt, 'function', exported)
    else if (ts.isClassDeclaration(stmt) && stmt.name) {
      addDecl(stmt.name, stmt, 'class', exported)
      memberKinds(stmt.name.text, stmt.members, exported)
    } else if (ts.isInterfaceDeclaration(stmt)) {
      addDecl(stmt.name, stmt, 'interface', exported)
      memberKinds(stmt.name.text, stmt.members, exported)
    } else if (ts.isTypeAliasDeclaration(stmt)) addDecl(stmt.name, stmt, 'type', exported)
    else if (ts.isEnumDeclaration(stmt)) addDecl(stmt.name, stmt, 'enum', exported)
    else if (ts.isVariableStatement(stmt)) {
      for (const d of stmt.declarationList.declarations) {
        if (ts.isIdentifier(d.name)) {
          addDecl(d.name, stmt, 'const', exported)
          if (d.initializer && ts.isObjectLiteralExpression(d.initializer)) objectLiteralMembers(d.name.text, d.initializer)
        }
      }
    }
  }
  for (const stmt of sf.statements) visitTop(stmt)

  // 関数が return するオブジェクトリテラルのメンバ(viewport.ts のようなハンドル)
  const walkReturns = (node, fnName) => {
    if (ts.isFunctionDeclaration(node) && node.name && node.parent === sf) fnName = node.name.text
    if (ts.isReturnStatement(node) && node.expression && ts.isObjectLiteralExpression(node.expression) && fnName)
      objectLiteralMembers(fnName + '()', node.expression)
    ts.forEachChild(node, (c) => walkReturns(c, fnName))
  }
  walkReturns(sf, null)

  // 参照
  const visit = (node) => {
    if (ts.isIdentifier(node) && !declNameNodes.has(node)) {
      // import 指定子の名前は参照ではない(使われているかは別途)
      const p = node.parent
      const isImportSpec = p && (ts.isImportSpecifier(p) || ts.isImportClause(p) || ts.isNamespaceImport(p))
      if (!isImportSpec) addRef(node.text, piece.file, lineOf(node.getStart(sf)))
    } else if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      if (/^[A-Za-z_$][\w$]*$/.test(node.text)) addRef(node.text, piece.file, lineOf(node.getStart(sf)))
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
}

// ---- 固定点反復
const byFile = new Map()
for (const d of decls) {
  if (!byFile.has(d.file)) byFile.set(d.file, [])
  byFile.get(d.file).push(d)
}
const nonlive = new Set()
const inNonlive = (file, line) => (byFile.get(file) ?? []).some((d) => nonlive.has(d.id) && d.line <= line && line <= d.end)

const evaluate = (d) => {
  let live = 0,
    scripts = 0,
    tests = 0,
    local = 0
  const sites = []
  for (const r of refs.get(d.name) ?? []) {
    if (r.file === d.file && d.line <= r.line && r.line <= d.end) continue
    if (inNonlive(r.file, r.line)) continue
    if (r.file === d.file) {
      local++
      sites.push(`${r.file}:${r.line}`)
    } else if (r.zone === 'web' || r.zone === zoneOf(d.file)) {
      live++
      sites.push(`${r.file}:${r.line}`)
    } else if (r.zone === 'tests') tests++
    else scripts++
  }
  return { live, scripts, tests, local, sites: sites.slice(0, 3) }
}

const SKIP = new Set(['default', 'constructor', 'toString', 'dispose', 'then', 'render'])
const cand = decls.filter((d) => !SKIP.has(d.name))
let changed = true
let rounds = 0
while (changed) {
  changed = false
  rounds++
  for (const d of cand) {
    if (nonlive.has(d.id)) continue
    const e = evaluate(d)
    // 同じファイル内の参照は(宣言が nonlive でない限り)生きているとみなす → 内部呼び出しで生きる
    if (e.live === 0 && e.local === 0) {
      nonlive.add(d.id)
      changed = true
    }
  }
}

const results = []
for (const d of cand) {
  if (!nonlive.has(d.id)) continue
  const e = evaluate(d)
  const category = e.scripts ? 'SCRIPT_ONLY' : e.tests ? 'TEST_ONLY' : 'DEAD'
  results.push({ file: d.file, name: d.name, kind: d.kind, container: d.container ?? null, line: d.line, end: d.end, exported: d.exported, category, scripts: e.scripts, tests: e.tests })
}

// export されているが、ファイル外から参照が無く、ファイル内だけで使われているもの(= export が不要)
const localOnly = []
for (const d of cand) {
  if (!d.exported || nonlive.has(d.id)) continue
  const e = evaluate(d)
  if (e.live === 0 && e.scripts === 0 && e.tests === 0 && e.local > 0) localOnly.push({ file: d.file, name: d.name, kind: d.kind, line: d.line })
}

fs.mkdirSync(path.dirname(outPath), { recursive: true })
fs.writeFileSync(outPath, JSON.stringify({ rounds, total_decls: decls.length, results, localOnly }, null, 1))
const count = {}
for (const r of results) count[`${r.category}:${r.kind}`] = (count[`${r.category}:${r.kind}`] ?? 0) + 1
console.log('decls', decls.length, 'rounds', rounds, 'results', results.length, 'localOnly', localOnly.length)
console.log(count)

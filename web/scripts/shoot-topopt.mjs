/**
 * 形状最適化の比較スクリーンショットの撮影台。
 *
 *   web> node scripts/shoot-topopt.mjs --model hanger_bar_attachment.json \
 *          --panel "既定:" --panel "細かく:element_mm=2.5" --views iso,side
 *
 * 最適化(面ごと)・描画・ラベル焼きまで1本のコマンドで終わる。
 *
 * ## 引数
 *
 *   --model <name>     利用者の文書(`models/` 配下の操作ログ。条件は文書のものを使う)
 *   --models-dir <dir> `--model` の置き場(既定はリポジトリ直下の `models/`)
 *   --panel "<ラベル>:<つまみ>"   2つ以上。左から順に並ぶ
 *                      つまみ: element_mm
 *   --views iso,side,threeq,zoom   撮る向き(既定は4つ全部)
 *   --dir x,y,z        `--views custom` と併せて任意の向き(`--zoom` で距離を詰める)
 *   --out <dir>        出力先(既定 `screenshots/topopt-compare`)
 *   --skip-export      `<out>/_data` にある前回の最適化結果を使い回す(向きだけ撮り直す)
 *   --width / --height 1面の画素(既定 900 x 700)
 *
 * ## 守っていること
 *
 * - カメラ・光源・背景・画角は面をまたいで同一(1枚のcanvasに `setScissor` で並べて描く)。
 * - 距離は全部の面の頂点から1つだけ、その視線方向での実際の投影から決める
 *   (外接球だと細長い題材が画面の3分の1にしか映らない)。
 * - サーバは自分で立てて止める。ポートは 0 でOSに空きを選ばせる。
 * - 三角形が1枚もGPUへ行っていない面があったら落とす(真っ黒の合格を防ぐ)。
 * - 新しいパッケージは入れない: three.js と puppeteer-core は `web/node_modules` から借り、
 *   ラベルはブラウザのDOMで焼く。
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { parseArgs } from './args.mjs'
import { findBrowser, launchBrowser } from './browser.mjs'
import { distanceFor } from './framing.mjs'
import { PYTHON, REPO_ROOT } from './server.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))

const argv = process.argv.slice(2)
const options = parseArgs(argv)
/** `--panel` は繰り返せる(`parseArgs` は最後の1つしか残さないので自分で拾う)。 */
const panels = argv.flatMap((item, i) => (item === '--panel' ? [argv[i + 1]] : []))

if (options.help || !options.model) {
  console.log(readFileSync(new URL(import.meta.url)).toString().split('*/')[0])
  process.exit(options.help ? 0 : 1)
}
if (panels.length < 2) {
  console.error('--panel を2つ以上渡すこと(左右に並べるための最小)')
  process.exit(1)
}

const WIDTH = Number.parseInt(options.width ?? '900', 10)
const HEIGHT = Number.parseInt(options.height ?? '700', 10)
const BAND = 34
const GAP = 6
const FOV = 50
const BG = '#0a0a12'
const MESH_COLOR = 0x6fa8dc

const VIEWS = {
  iso: { dir: [-0.75, 0.5, 0.6], zoom: 1.0 },
  side: { dir: [-0.06, -1.0, 0.08], zoom: 1.0 },
  threeq: { dir: [0.62, 0.66, 0.42], zoom: 1.0 },
  zoom: { dir: [-0.75, 0.5, 0.6], zoom: 0.62 },
}
if (options.dir) {
  VIEWS.custom = {
    dir: options.dir.split(',').map((v) => Number.parseFloat(v)),
    zoom: Number.parseFloat(options.zoom ?? '1'),
  }
  const { dir } = VIEWS.custom
  if (dir.length !== 3 || !dir.every(Number.isFinite) || Math.hypot(...dir) === 0) {
    console.error(`--dir は零でない x,y,z の3つの数で渡すこと(いま: ${options.dir})`)
    process.exit(1)
  }
}
const wantViews = (options.views ?? (options.dir ? 'custom' : 'iso,side,threeq,zoom'))
  .split(',')
  .map((name) => name.trim())
  .filter(Boolean)
for (const name of wantViews) {
  if (!(name in VIEWS)) {
    console.error(`知らない向き ${name}(あるのは ${Object.keys(VIEWS).join(', ')})`)
    process.exit(1)
  }
}

const subject = options.model.replace(/\.json$/i, '')
const outDir = resolve(REPO_ROOT, options.out ?? join('screenshots', 'topopt-compare'))
const dataDir = join(outDir, '_data')
mkdirSync(dataDir, { recursive: true })

async function runExport() {
  const args = [
    join(HERE, 'shoot_topopt_export.py'),
    '--out',
    dataDir,
    '--model',
    options.model,
    ...(options['models-dir'] ? ['--models-dir', options['models-dir']] : []),
    ...panels.flatMap((spec) => ['--panel', spec]),
  ]
  console.log(`[1/2] 最適化 ${panels.length}面(重い。題材と設定によっては数分〜十数分)`)
  await new Promise((done, failed) => {
    const child = spawn(PYTHON, args, {
      cwd: REPO_ROOT,
      stdio: ['ignore', 'inherit', 'inherit'],
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    })
    child.on('exit', (code) =>
      code === 0 ? done() : failed(new Error(`最適化の段が失敗した(code ${code})`)),
    )
  })
}

if (options['skip-export']) {
  if (!existsSync(join(dataDir, 'manifest.json'))) {
    console.error(`--skip-export だが前回の結果が無い: ${dataDir}`)
    process.exit(1)
  }
  console.log(`[1/2] 最適化は飛ばす(--skip-export。${dataDir} を使い回す)`)
} else {
  await runExport()
}

const manifest = JSON.parse(readFileSync(join(dataDir, 'manifest.json'), 'utf8'))
const PANEL_COUNT = manifest.panels.length
// `_data` は出力先ごとに1つ。別の題材のデータを `--skip-export` で使い回すと、ラベルと絵の題材が食い違う。
if (options['skip-export'] && manifest.subject !== subject) {
  console.error(
    `--skip-export だが ${dataDir} に入っているのは ${manifest.subject} の結果` +
      `(いま指定したのは ${subject})。--out を分けるか、--skip-export を外すこと`,
  )
  process.exit(1)
}

function vertsOf(index) {
  const payload = JSON.parse(readFileSync(join(dataDir, `panel-${index}.json`), 'utf8'))
  const buffer = Buffer.from(payload.verts_b64, 'base64')
  return new Float32Array(
    buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength),
  )
}

const allVerts = manifest.panels.map((_, index) => vertsOf(index))

/** 全部の面の外接箱の中心。**同じ点を見る**ので比較の土俵は崩れない。 */
function commonCentre() {
  const lo = [Infinity, Infinity, Infinity]
  const hi = [-Infinity, -Infinity, -Infinity]
  for (const verts of allVerts) {
    for (let i = 0; i < verts.length; i += 3) {
      for (let a = 0; a < 3; a += 1) {
        lo[a] = Math.min(lo[a], verts[i + a])
        hi[a] = Math.max(hi[a], verts[i + a])
      }
    }
  }
  return [0, 1, 2].map((a) => (lo[a] + hi[a]) / 2)
}

const CANVAS_W = WIDTH * PANEL_COUNT + GAP * (PANEL_COUNT - 1)

const PAGE = `<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{margin:0;padding:0;background:${BG};overflow:hidden}
  #band{display:flex;gap:${GAP}px;height:${BAND}px;width:${CANVAS_W}px}
  #band div{width:${WIDTH}px;color:#e6e6f0;font:15px/34px system-ui,sans-serif;
            padding-left:10px;box-sizing:border-box;white-space:nowrap;overflow:hidden}
  #band b{color:#9fb4d8;margin-right:8px}
</style>
<script type="importmap">{"imports":{"three":"/vendor/three.module.js"}}</script>
</head><body>
<div id="band"></div>
<script type="module">
import * as THREE from 'three';
const PANELS = ${PANEL_COUNT}, W = ${WIDTH}, H = ${HEIGHT}, GAP = ${GAP};
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(1);
renderer.setSize(${CANVAS_W}, H, false);
renderer.setScissorTest(true);
renderer.autoClear = false;
// **面と面のすきまも同じ色で塗る。** clear() はシザー矩形の中だけを既定色で塗るので、
// ここを揃えないと 6px の隙間だけ真っ黒になる(BG と揃える)。
renderer.setClearColor(new THREE.Color('${BG}'), 1);
document.body.appendChild(renderer.domElement);

// **光源は全部の面で同一。** 1つの scene を使い回すので、面ごとに別の値を持てない。
const scene = new THREE.Scene();
scene.background = new THREE.Color('${BG}');
scene.add(new THREE.HemisphereLight(0xffffff, 0x333344, 0.65));
const dirLight = new THREE.DirectionalLight(0xffffff, 0.85);
dirLight.position.set(2, 3, 1);
scene.add(dirLight);
// **カメラ追従の弱い正面光。** 4方向から撮るので、背面側の視点(side)がこれ無しでは
// 真っ黒になる。全部の面で同一。
const headLight = new THREE.DirectionalLight(0xffffff, 0.45);
scene.add(headLight);

const camera = new THREE.PerspectiveCamera(${FOV}, W / H, 0.1, 50000);
camera.up.set(0, 0, 1);

function b64(text) {
  const bin = atob(text);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

const meshes = [];
window.__load = async () => {
  const band = document.getElementById('band');
  for (let i = 0; i < PANELS; i += 1) {
    const payload = await (await fetch('/data/panel-' + i + '.json')).json();
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position',
      new THREE.BufferAttribute(new Float32Array(b64(payload.verts_b64)), 3));
    geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(b64(payload.faces_b64)), 1));
    geometry.computeVertexNormals();
    const mesh = new THREE.Mesh(geometry, new THREE.MeshLambertMaterial({ color: ${MESH_COLOR} }));
    mesh.visible = false;
    scene.add(mesh);
    meshes.push(mesh);
    // **位置の名前を頭に付ける**(3面までは LEFT / MID / RIGHT、それ以上は番号)。
    // ラベルだけだと「どっちが後か」を画像だけ見て言えない。
    const names = PANELS === 2 ? ['LEFT', 'RIGHT']
      : PANELS === 3 ? ['LEFT', 'MID', 'RIGHT']
      : Array.from({ length: PANELS }, (_, k) => '#' + (k + 1));
    const label = document.createElement('div');
    label.innerHTML = '<b></b>';
    label.querySelector('b').textContent = names[i];
    label.appendChild(document.createTextNode(payload.label));
    band.appendChild(label);
  }
  return meshes.length;
};

window.__render = (dir, dist, centre) => {
  const c = new THREE.Vector3(...centre);
  camera.position.copy(new THREE.Vector3(...dir).normalize().multiplyScalar(dist).add(c));
  camera.near = Math.max(dist / 500, 0.01);
  camera.far = dist * 20;
  camera.lookAt(c);
  headLight.position.copy(camera.position);
  camera.updateProjectionMatrix();
  renderer.setScissor(0, 0, ${CANVAS_W}, H);
  renderer.clear();
  // **面ごとの数を返す。** 合算すると「1面だけ真っ黒」が他の面に隠れて合格する。
  const triangles = [];
  for (let i = 0; i < PANELS; i += 1) {
    const x = i * (W + GAP);
    renderer.setViewport(x, 0, W, H);
    renderer.setScissor(x, 0, W, H);
    for (let j = 0; j < PANELS; j += 1) meshes[j].visible = j === i;
    renderer.info.reset();
    renderer.render(scene, camera);
    triangles.push(renderer.info.render.triangles);
  }
  for (const mesh of meshes) mesh.visible = false;
  return triangles;
};
window.__ready = true;
</script></body></html>`

/** three.js とデータを配る最小のHTTPサーバ。**ポートは0**(OSに空きを選ばせる)。 */
const THREE_DIR = join(REPO_ROOT, 'web', 'node_modules', 'three', 'build')
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json' }
const server = createServer((request, response) => {
  const url = new URL(request.url, 'http://127.0.0.1')
  if (url.pathname === '/page') {
    response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
    response.end(PAGE)
    return
  }
  // **favicon は 204 で返す。** 404 にするとブラウザのコンソールにエラーが1件出て、
  // 「ページ側でエラー」の網に毎回かかる(絵とは無関係の失敗で撮影が止まる)。
  if (url.pathname === '/favicon.ico') {
    response.writeHead(204)
    response.end()
    return
  }
  const rel = decodeURIComponent(url.pathname).replace(/^\/+/, '')
  const file = rel.startsWith('vendor/')
    ? join(THREE_DIR, rel.slice('vendor/'.length))
    : join(dataDir, rel.replace(/^data\//, ''))
  if (!existsSync(file)) {
    response.writeHead(404)
    response.end('no')
    return
  }
  response.writeHead(200, {
    'content-type': MIME[file.slice(file.lastIndexOf('.'))] ?? 'application/octet-stream',
  })
  response.end(readFileSync(file))
})
await new Promise((done) => server.listen(0, '127.0.0.1', done))
const PORT = server.address().port

console.log(`[2/2] 描画(${PANEL_COUNT}面 x ${wantViews.length}方向 / 1面 ${WIDTH}x${HEIGHT})`)
const browser = await launchBrowser(findBrowser())
const written = []
try {
  const page = await browser.newPage()
  await page.setViewport({ width: CANVAS_W, height: HEIGHT + BAND })
  // **ページ側の例外は握り潰さない。** ハンドラの中で throw しても呼び出し元へは
  // 伝わらない(未処理のPromise拒否になるだけ)ので、溜めて描画の後で見る。
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(String(error)))
  page.on('console', (message) => {
    if (message.type() === 'error') pageErrors.push(message.text())
  })
  await page.goto(`http://127.0.0.1:${PORT}/page`, { waitUntil: 'load' })
  await page.waitForFunction('window.__ready === true', { timeout: 30000 })
  const loaded = await page.evaluate(() => window.__load())
  if (loaded !== PANEL_COUNT) throw new Error(`面が ${loaded} 枚しか読めていない`)

  const centre = commonCentre()
  for (const name of wantViews) {
    const view = VIEWS[name]
    const dist = distanceFor(view.dir, centre, allVerts, FOV, WIDTH / HEIGHT) * view.zoom
    const triangles = await page.evaluate(
      (d, r, c) => window.__render(d, r, c),
      view.dir,
      dist,
      centre,
    )
    // **真っ黒の合格を防ぐ。** **面ごとに**最低1枚は三角形がGPUへ行っていること
    // (合算で見ると、1面だけ真っ黒でも他の面に隠れて合格する)。
    const empty = triangles.flatMap((count, i) => (count > 0 ? [] : [i]))
    if (empty.length > 0) {
      throw new Error(
        `${name}: 面 ${empty.join(',')} に三角形が1つもGPUへ行っていない` +
          `(面ごとの三角形数 ${triangles.join(' / ')})`,
      )
    }
    if (pageErrors.length > 0) throw new Error(`ページ側でエラー: ${pageErrors[0]}`)
    const png = await page.screenshot({ type: 'png' })
    const path = join(outDir, `${subject}_${name}.png`)
    writeFileSync(path, png)
    written.push(path)
    console.log(`  ${path}  (三角形 ${triangles.join(' / ')} / 距離 ${dist.toFixed(1)}mm)`)
  }
} finally {
  await browser.close()
  server.close()
}

console.log(`\nshoot-topopt: ${written.length} 枚`)

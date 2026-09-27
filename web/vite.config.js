import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'fs'
import path from 'path'
import { localePreload } from './scripts/locale-preload.ts'

// Shared by the entry and the lazy vendors — see codeSplitting below.
const EAGER_SHARED = new Set(['clsx', 'use-sync-external-store'])
const REACT = new Set(['react', 'react-dom', 'react-router', ...EAGER_SHARED])
const MARKDOWN = new Set([
  'react-markdown', 'remark-gfm', 'remark-math', 'remark-cjk-friendly',
  'rehype-katex', 'rehype-raw', 'katex',
])
const CHARTS = new Set(['recharts', 'lightweight-charts'])

/**
 * The npm package a module id belongs to, or null for app source.
 * @param {string} id
 */
function packageOf(id) {
  const tail = id.split(/[\\/]node_modules[\\/]/)
  if (tail.length < 2) return null
  const [scope, name] = tail[tail.length - 1].split(/[\\/]/)
  return scope.startsWith('@') ? `${scope}/${name}` : scope
}

/**
 * @param {string} name
 * @param {number} priority
 * @param {(pkg: string) => boolean} matches
 */
const vendorGroup = (name, priority, matches) => ({
  name,
  priority,
  test: (/** @type {string} */ id) => {
    const pkg = packageOf(id)
    return pkg !== null && matches(pkg)
  },
})

// Emits dist/version.json holding this build's entry chunk filename — the identity
// the running app polls to notice it is a build the server no longer serves.
//
// Content-derived on purpose: the value is the entry's content hash, so a rebuild
// that changes nothing produces the same id and raises no spurious "new version"
// prompt. A timestamp or git sha would fire on every rebuild.
//
// generateBundle, not writeBundle: the hashed fileName is final by this hook, and
// emitFile puts the result through the bundler's own output pipeline. Selecting by
// `isEntry` and not by name is the load-bearing part — codeSplitting below also emits
// vendor-* chunks, and `index` is a name a chunking change could quietly move.
/** @returns {import('vite').Plugin} */
function emitVersionManifest() {
  return {
    name: 'la-version-manifest',
    apply: 'build',
    generateBundle(_options, bundle) {
      const entries = Object.values(bundle).filter((c) => c.type === 'chunk' && c.isEntry)
      // Zero means the selector went stale; more than one means a second entry
      // appeared and "the build" is no longer a single identity. Either way the
      // manifest would be wrong, and a wrong build id is worse than none: the
      // client would prompt for a reload that changes nothing.
      if (entries.length !== 1) {
        this.error(
          `version.json needs exactly 1 entry chunk, found ${entries.length}` +
            `${entries.length ? `: ${entries.map((c) => c.fileName).join(', ')}` : ''}`,
        )
      }
      this.emitFile({
        type: 'asset',
        fileName: 'version.json',
        source: `${JSON.stringify({ build: entries[0].fileName.split('/').pop() })}\n`,
      })
    },
  }
}

// Serves pdf.js's image decoders (JBIG2 and CCITT since pdf.js 6, JPEG 2000) at
// assets/pdfjs-wasm/<version>/, the `wasmUrl` PdfViewer passes. pdf.js fetches
// them by bare filename from one directory, so they cannot take hashed names;
// the version in the path keeps a long-cached copy from pairing a new worker
// with old decoders. Without them, those images are silently left blank.
/** @returns {import('vite').Plugin} */
function pdfjsWasm() {
  const pkg = path.resolve(import.meta.dirname, 'node_modules/pdfjs-dist')
  const dir = path.join(pkg, 'wasm')
  const { version } = JSON.parse(fs.readFileSync(path.join(pkg, 'package.json'), 'utf8'))
  const route = `assets/pdfjs-wasm/${version}/`
  /** @type {Record<string, string>} */
  const types = { '.wasm': 'application/wasm', '.js': 'text/javascript' }
  return {
    name: 'la-pdfjs-wasm',
    configureServer(server) {
      server.middlewares.use(`/${route}`, (req, res, next) => {
        if (!req.url) return next()
        const file = path.join(dir, path.basename(req.url.split('?')[0]))
        if (!fs.existsSync(file)) return next()
        res.setHeader('Content-Type', types[path.extname(file)] ?? 'application/octet-stream')
        fs.createReadStream(file).pipe(res)
      })
    },
    generateBundle() {
      for (const name of fs.readdirSync(dir)) {
        this.emitFile({ type: 'asset', fileName: route + name, source: fs.readFileSync(path.join(dir, name)) })
      }
    },
  }
}

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // Load VITE_-prefixed vars from .env files (.env, .env.local, …) so they can
  // be seeded on disk instead of passed inline. loadEnv also merges matching
  // process.env entries, so `VITE_FOO=bar pnpm dev` keeps working too.
  const env = loadEnv(mode, process.cwd())
  const backendTarget = env.VITE_PROXY_BACKEND || 'http://localhost:8000'
  // Only honor a well-formed port; a non-numeric VITE_HMR_CLIENT_PORT would
  // otherwise yield NaN and silently break the HMR socket.
  const hmrClientPort = Number(env.VITE_HMR_CLIENT_PORT)
  const hasHmrClientPort = Number.isFinite(hmrClientPort) && hmrClientPort > 0
  // Extra Host headers the dev server accepts, for tunnels (ngrok, Cloudflare)
  // that front it under their own hostname. Comma-separated — kept in .env so a
  // tunnel host never needs a local edit to this file.
  const allowedHosts = (env.VITE_DEV_ALLOWED_HOSTS || '')
    .split(',')
    .map((host) => host.trim())
    .filter(Boolean)

  return {
    base: env.VITE_CDN_BASE || '/',
    plugins: [react(), emitVersionManifest(), pdfjsWasm(), localePreload(path.resolve(import.meta.dirname, 'src/locales'))],
    resolve: {
      alias: [
        { find: '@', replacement: path.resolve(import.meta.dirname, './src') },
        // Fixtures a unit test shares with the Playwright specs. Kept in step
        // with vitest.config.ts; nothing in the app graph imports it.
        { find: '@e2e', replacement: path.resolve(import.meta.dirname, './e2e') },
        // pdf.js 6's default build calls Map#getOrInsertComputed and Math.sumPrecise
        // unguarded (Chrome 145, Safari 26.2, Firefox 144). Its legacy build carries
        // the polyfills. The regex matches only react-pdf's bare import; the viewer
        // takes the matching legacy worker by path.
        { find: /^pdfjs-dist$/, replacement: 'pdfjs-dist/legacy/build/pdf.mjs' },
      ],
    },
    build: {
      rolldownOptions: {
        // Explicit single entry: dev-only harness pages (e.g. intro-preview.html)
        // must never ship in the production build, even if a future Vite version
        // or multi-page config change starts picking up root .html files.
        input: path.resolve(import.meta.dirname, 'index.html'),
        output: {
          // Vendors get a pinned chunk so app deploys don't re-invalidate them.
          //
          // The priorities are load-bearing. A group also captures every
          // dependency of what it matches, so recharts would pull React,
          // `clsx` and `use-sync-external-store` into vendor-charts, and the
          // entry would then have to preload that whole chunk to reach them.
          // That is how 170 kB of charts sat on the critical path for five
          // months. The eager groups claim first so the lazy vendors stay lazy.
          // Enforced by scripts/check-critical-path.mjs.
          //
          // The `$initial` group is everything the entry reaches statically.
          // Without it Rolldown splits the eager code shared with lazy routes
          // into ~100 small common chunks, each one more request on first load.
          // It ranks above the lazy vendors so sharing a dependency with them
          // can never make them eager.
          codeSplitting: {
            groups: [
              vendorGroup('vendor-react', 50, (pkg) => REACT.has(pkg)),
              vendorGroup('vendor-motion', 40, (pkg) => pkg === 'framer-motion'),
              vendorGroup('vendor-dnd', 30, (pkg) => pkg.startsWith('@dnd-kit/')),
              { name: 'index', priority: 25, tags: ['$initial'] },
              vendorGroup('vendor-markdown', 20, (pkg) => MARKDOWN.has(pkg)),
              vendorGroup('vendor-charts', 10, (pkg) => CHARTS.has(pkg)),
            ],
          },
        },
      },
    },
    server: {
      host: '127.0.0.1',
      // Unset leaves Vite's default host checking in place.
      allowedHosts: allowedHosts.length ? allowedHosts : undefined,
      watch: {
        // The docker-compose pnpm store volume sits inside the project root and
        // holds ~40k files; watching it costs one inotify watch per file, enough
        // to exhaust a Linux host's per-user limit.
        ignored: ['**/.pnpm-store/**'],
      },
      // When served behind the nginx dev proxy (oss.localhost etc.), the HMR
      // WebSocket must dial the proxy port, not the Vite port. Seed
      // VITE_HMR_CLIENT_PORT (e.g. =80) in .env.local, or pass it inline.
      // Unset leaves Vite's default HMR behavior untouched (dev-only; ignored
      // by `vite build`).
      //
      // `path` moves the HMR socket off "/" so it doesn't collide with the
      // proxy's `location = /` session redirect (/ → /home|/app), which would
      // 302 the upgrade and surface as "WebSocket closed without opened". The
      // non-root path falls through nginx's `location /` to this Vite server.
      hmr: hasHmrClientPort
        ? { clientPort: hmrClientPort, path: '/vite-hmr' }
        : undefined,
      proxy: {
        '/api/v1': {
          target: backendTarget,
          changeOrigin: true,
        },
        '/ws/v1': {
          target: backendTarget.replace(/^http/, 'ws'),
          ws: true,
        },
      },
      cors: true,
    },
  }
})

import { defineConfig, loadEnv } from 'vite'
import { reactPlugins } from './scripts/react-plugins.ts'
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

// Keeps the entry chunk out of every lazy import's preload list. The `$initial`
// group below lives in the entry, so most route chunks import from it and Vite
// lists it among their deps. Its preload helper skips a dep that already has a
// <link>, but the entry arrived by <script>, so the helper appends a
// modulepreload for a module that is already running. Firefox 146 fires `error`
// on that link, and index.html's stale-build listener reads it as a dead build.
/** @returns {import('vite').Plugin} */
function skipEntryPreload() {
  /** @type {Set<string>} */
  const entries = new Set()
  return {
    name: 'la-skip-entry-preload',
    apply: 'build',
    config: () => ({
      build: {
        modulePreload: {
          resolveDependencies: (_file, deps) => deps.filter((dep) => !entries.has(dep)),
        },
      },
    }),
    // Ahead of vite:build-import-analysis, whose generateBundle is what calls
    // resolveDependencies.
    generateBundle: {
      order: 'pre',
      handler(_options, bundle) {
        entries.clear()
        for (const chunk of Object.values(bundle)) {
          if (chunk.type === 'chunk' && chunk.isEntry) entries.add(chunk.fileName)
        }
      },
    },
  }
}

// The pdfjs-dist directories pdf.js fetches from at runtime: image decoders
// (JBIG2, CCITT, JPEG 2000, ICC color), the predefined CMaps a non-embedded CJK
// font is encoded with, the fonts it substitutes for non-embedded Symbol and
// ZapfDingbats, and the CMYK output profile. Without them those glyphs, images
// and colors are silently dropped or degraded.
const PDFJS_DATA = ['wasm', 'cmaps', 'standard_fonts', 'iccs']

// Serves PDFJS_DATA at assets/pdfjs/<version>/<dir>/, the URLs PdfViewer passes.
// pdf.js fetches by bare filename, so the files cannot take hashed names; the
// version in the path keeps a long-cached copy from pairing a new worker with
// old data. Emitted as loose assets, nothing imports them into a chunk.
/** @returns {import('vite').Plugin} */
function pdfjsData() {
  const pkg = path.resolve(import.meta.dirname, 'node_modules/pdfjs-dist')
  const { version } = JSON.parse(fs.readFileSync(path.join(pkg, 'package.json'), 'utf8'))
  const route = `assets/pdfjs/${version}/`
  /** @type {Record<string, string>} */
  const types = { '.wasm': 'application/wasm', '.js': 'text/javascript' }
  return {
    name: 'la-pdfjs-data',
    configureServer(server) {
      // Plugin middleware runs before Vite strips the base, so a non-root base
      // stays on the request URL and the mount has to carry it.
      server.middlewares.use(`${server.config.base}${route}`, (req, res, next) => {
        const [dir = '', name = '', ...rest] = (req.url ?? '').split('?')[0].split('/').filter(Boolean)
        // A backslash separates paths on Windows, so a name holding one would
        // walk out of the directory.
        if (!PDFJS_DATA.includes(dir) || rest.length || path.basename(name) !== name) return next()
        const file = path.join(pkg, dir, name)
        if (!fs.statSync(file, { throwIfNoEntry: false })?.isFile()) return next()
        res.setHeader('Content-Type', types[path.extname(file)] ?? 'application/octet-stream')
        fs.createReadStream(file).pipe(res)
      })
    },
    generateBundle() {
      for (const dir of PDFJS_DATA) {
        for (const name of fs.readdirSync(path.join(pkg, dir))) {
          const source = fs.readFileSync(path.join(pkg, dir, name))
          this.emitFile({ type: 'asset', fileName: `${route}${dir}/${name}`, source })
        }
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
    plugins: [...reactPlugins(), emitVersionManifest(), skipEntryPreload(), pdfjsData(), localePreload(path.resolve(import.meta.dirname, 'src/locales'))],
    resolve: {
      // `@/` and the tests' `@e2e/` come from the tsconfig projects' `paths`,
      // each file resolving through the project that owns it.
      tsconfigPaths: true,
      alias: [
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
      // `cors` stays at Vite's default, which answers only loopback origins,
      // `*.localhost` included (the dev proxy's worktree hosts). `true` let any
      // page open in the developer's browser read this server's responses.
    },
  }
})

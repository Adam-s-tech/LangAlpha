// Starts the active locale's catalog download from index.html, alongside the
// entry instead of after it.
//
// Each locale is its own chunk (src/i18n.ts) and nothing renders until the
// active one is in, so left to the entry the request would wait a full
// waterfall step. The build writes the hashed names into an inline ES5 script
// that picks the locale as detectLocale() does and appends a preload. It
// replaces index.html's `<!-- locale-preload -->` marker: after the stale-build
// script, whose listener must exist before this preload can fail (a 404 then
// reloads like any dead asset), and before any stylesheet, which would hold an
// inline script until it loaded.

import fs from 'node:fs';
import path from 'node:path';
import type { Plugin } from 'vite';

/** detectLocale()'s last resort. */
export const DEFAULT_LOCALE = 'en-US';

const MARKER = /<!--\s*locale-preload\b[^>]*-->/;

/**
 * The locales that have a catalog, in the order the resolver tries a
 * language-only match. detectLocale() tries SUPPORTED_LOCALES order instead;
 * the two agree while no two locales share a language, and
 * lib/__tests__/localePreload.test.ts fails the day they would not.
 */
export function catalogLocales(dir: string): string[] {
  return fs
    .readdirSync(dir)
    .filter((f) => f.endsWith('.json'))
    .map((f) => path.basename(f, '.json'))
    .sort();
}

/**
 * The inline resolver. `urls` maps each locale to its chunk, in
 * catalogLocales() order. lib/__tests__/localePreload.test.ts runs it against
 * detectLocale().
 */
export function localePreloadScript(urls: Record<string, string>): string {
  const locales = Object.keys(urls);
  return (
    '(function(){' +
    `var u=${JSON.stringify(urls)},l=${JSON.stringify(locales)},p=${JSON.stringify(DEFAULT_LOCALE)},c=null,b,m,i,k;` +
    'function has(v){for(var j=0;j<l.length;j++)if(l[j]===v)return true;return false}' +
    'm=document.cookie.match(/(?:^|;\\s*)locale=([^;]+)/);' +
    'if(m){try{c=decodeURIComponent(m[1])}catch(e){}}' +
    'if(has(c))p=c;else{b=navigator.language||"";' +
    'if(has(b))p=b;else for(i=0;i<l.length;i++)if(l[i].indexOf(b.split("-")[0]+"-")===0){p=l[i];break}}' +
    // Vite's own preload shape, fallback included: rel="preload" where
    // modulepreload is unsupported, as="script" either way. Both are shapes
    // the recovery listener classifies as a build asset.
    'k=document.createElement("link");' +
    'try{k.rel=k.relList.supports("modulepreload")?"modulepreload":"preload"}catch(e){k.rel="preload"}' +
    'k.as="script";k.crossOrigin="";k.href=u[p];document.head.appendChild(k)' +
    '})();'
  );
}

/** Build-only: the dev server has no hashed chunks to name. */
export function localePreload(localesDir: string): Plugin {
  let base = '/';
  return {
    name: 'la-locale-preload',
    apply: 'build',
    configResolved(config) {
      base = config.base;
    },
    transformIndexHtml: {
      order: 'post',
      handler(html, ctx) {
        const dir = fs.realpathSync(localesDir);
        const found = new Map<string, string>();
        for (const out of Object.values(ctx.bundle ?? {})) {
          const id = out.type === 'chunk' ? out.facadeModuleId : null;
          if (id && path.dirname(id) === dir && id.endsWith('.json')) {
            found.set(path.basename(id, '.json'), base + out.fileName);
          }
        }
        // A catalog without a chunk of its own was imported statically
        // somewhere, which puts it back in the entry.
        const locales = catalogLocales(dir);
        const missing = locales.filter((l) => !found.has(l));
        if (missing.length) {
          throw new Error(`locale-preload: no chunk of its own for ${missing.join(', ')}; is it imported statically?`);
        }
        if (!found.has(DEFAULT_LOCALE)) {
          throw new Error(`locale-preload: no catalog for the default locale ${DEFAULT_LOCALE}`);
        }
        if (!MARKER.test(html)) {
          throw new Error('locale-preload: index.html has no <!-- locale-preload --> marker');
        }
        const urls = Object.fromEntries(locales.map((l) => [l, found.get(l)!]));
        return html.replace(MARKER, () => `<script>${localePreloadScript(urls)}</script>`);
      },
    },
  };
}

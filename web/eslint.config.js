import { resolve } from 'node:path'
import { includeIgnoreFile } from 'eslint/config'
import js from '@eslint/js'
import globals from 'globals'
import eslintReact from '@eslint-react/eslint-plugin'
import reactHooks from 'eslint-plugin-react-hooks'
import { reactRefresh } from 'eslint-plugin-react-refresh'
import tseslint from '@typescript-eslint/eslint-plugin'
import tsParser from '@typescript-eslint/parser'

// The checks eslint-plugin-react's recommended preset ran, under their
// @eslint-react names. That preset also caught JSX undefined names, duplicate
// props, string refs, isMounted and a missing render return, which tsc already
// rejects, and unescaped entities, which has no counterpart. @eslint-react's
// own presets add ~290 findings of new policy (index keys, ref naming) plus
// copies of the react-hooks rules, so they stay off until chosen on purpose.
const reactRules = Object.fromEntries([
  'no-missing-key',
  'no-missing-component-display-name',
  'no-direct-mutation-state',
  'no-component-will-mount',
  'no-component-will-receive-props',
  'no-component-will-update',
  'jsx-no-children-prop',
  'jsx-no-comment-textnodes',
  'dom-no-dangerously-set-innerhtml-with-children',
  'dom-no-find-dom-node',
  'dom-no-hydrate',
  'dom-no-render',
  'dom-no-render-return-value',
  'dom-no-unknown-property',
  'dom-no-unsafe-target-blank',
].map((rule) => [`@eslint-react/${rule}`, 'error']))

// react-hooks 6+ puts the React Compiler's checks in its recommended preset
// at error. Existing code predates them, so they report as warnings until the
// code is brought in line; the two rules v5 shipped keep their levels.
const HOOKS_V5_RULES = new Set(['react-hooks/rules-of-hooks', 'react-hooks/exhaustive-deps'])
const hooksCompilerRulesAsWarnings = Object.fromEntries(
  Object.keys(reactHooks.configs.flat.recommended.rules)
    .filter((rule) => !HOOKS_V5_RULES.has(rule))
    .map((rule) => [rule, 'warn']),
)

// Export shapes @vitejs/plugin-react can hot-swap in place, the same pair
// react-refresh's own vite preset allows.
const refreshExportOptions = { allowConstantExport: true, allowCompoundComponents: true }

export default [
  // Skip what git ignores. Flat config never reads .gitignore itself, so build
  // output or local tooling in a checkout would fail lint where a fresh clone
  // passes.
  ...includeIgnoreFile(
    [resolve(import.meta.dirname, '../.gitignore'), resolve(import.meta.dirname, '.gitignore')],
    { gitignoreResolution: true },
  ),

  js.configs.recommended,
  {
    // ESLint 10 added these to recommended and existing code violates them.
    rules: {
      'no-useless-assignment': 'warn',
      'preserve-caught-error': 'warn',
    },
  },

  {
    files: ['**/*.{js,jsx,ts,tsx}'],
    plugins: { '@eslint-react': eslintReact },
    rules: reactRules,
  },

  reactHooks.configs.flat.recommended,
  { rules: hooksCompilerRulesAsWarnings },

  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: {
        ...globals.browser,
        process: 'readonly',
      },
    },
    plugins: { 'react-refresh': reactRefresh.plugin },
    rules: {
      'no-unused-vars': ['warn', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_|^React$',
        caughtErrorsIgnorePattern: '^_',
        destructuredArrayIgnorePattern: '^_',
      }],
      'react-refresh/only-export-components': ['warn', refreshExportOptions],
    },
  },

  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      parser: tsParser,
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
      globals: {
        ...globals.browser,
        process: 'readonly',
      },
    },
    plugins: {
      '@typescript-eslint': tseslint,
      'react-refresh': reactRefresh.plugin,
    },
    rules: {
      'no-undef': 'off',
      'no-redeclare': 'off',
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': ['warn', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_|^React$',
        caughtErrorsIgnorePattern: '^_',
        destructuredArrayIgnorePattern: '^_',
      }],
      // Under verbatimModuleSyntax `import { type A } from 'x'` still loads
      // 'x' for its side effects, and tsc accepts it. `import type` does not.
      '@typescript-eslint/no-import-type-side-effects': 'error',
      'react-refresh/only-export-components': ['warn', refreshExportOptions],
    },
  },

  // lib/framer is the only module that may import framer-motion: it arms the
  // hidden-tab rule, and ES module order only guarantees that runs first for
  // code that imports framer through it.
  //
  // zod core sits in the entry chunk, because the entry validates with
  // zod/mini, so the core code any lazy schema reaches is paid on first load.
  // Classic zod reaches nearly all of it (its methods do not tree-shake) and
  // adds its own API and English locale in a chunk of their own.
  {
    files: ['src/**/*.{js,jsx,ts,tsx}'],
    ignores: ['src/lib/framer.ts'],
    rules: {
      'no-restricted-imports': ['error', {
        paths: [{
          name: 'framer-motion',
          message: 'Import from @/lib/framer, which sets up framer before any animation exists.',
        }],
        // zod/v4 and zod/v3 are classic zod under another name.
        patterns: [{
          regex: '^zod(/v3|/v4)?$',
          message: 'Import zod/mini. Classic zod puts most of zod core on the entry chunk.',
        }],
      }],
    },
  },

  {
    files: ['vite.config.js', 'scripts/**/*.mjs'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.node },
    },
  },
]

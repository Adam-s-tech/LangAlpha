import js from '@eslint/js'
import globals from 'globals'
import reactPlugin from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import { reactRefresh } from 'eslint-plugin-react-refresh'
import tseslint from '@typescript-eslint/eslint-plugin'
import tsParser from '@typescript-eslint/parser'
import { version as reactVersion } from 'react'

// eslint-plugin-react 7.37 resolves version 'detect' through
// context.getFilename(), which ESLint 10 removed, so detection throws on the
// first file (jsx-eslint/eslint-plugin-react#3977). Reading the installed
// version here gives the plugin the same answer without that call.
const reactSettings = { react: { version: reactVersion } }

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
  { ignores: ['dist/**', 'dist-perf/**', 'public/mockServiceWorker.js'] },

  js.configs.recommended,
  {
    // ESLint 10 added these to recommended and existing code violates them.
    rules: {
      'no-useless-assignment': 'warn',
      'preserve-caught-error': 'warn',
    },
  },

  reactPlugin.configs.flat.recommended,
  reactPlugin.configs.flat['jsx-runtime'],

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
    settings: reactSettings,
    plugins: { 'react-refresh': reactRefresh.plugin },
    rules: {
      'react/prop-types': 'off',
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
    settings: reactSettings,
    plugins: {
      '@typescript-eslint': tseslint,
      'react-refresh': reactRefresh.plugin,
    },
    rules: {
      'react/prop-types': 'off',
      'no-undef': 'off',
      'no-redeclare': 'off',
      'no-unused-vars': 'off',
      '@typescript-eslint/no-unused-vars': ['warn', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_|^React$',
        caughtErrorsIgnorePattern: '^_',
        destructuredArrayIgnorePattern: '^_',
      }],
      'react-refresh/only-export-components': ['warn', refreshExportOptions],
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

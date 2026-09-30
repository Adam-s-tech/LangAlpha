import { renderToStaticMarkup } from 'react-dom/server';
import PrismLight from 'react-syntax-highlighter/dist/esm/prism-light';
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash';
import css from 'react-syntax-highlighter/dist/esm/languages/prism/css';
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json';
import markdown from 'react-syntax-highlighter/dist/esm/languages/prism/markdown';
import markup from 'react-syntax-highlighter/dist/esm/languages/prism/markup';
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python';
import rust from 'react-syntax-highlighter/dist/esm/languages/prism/rust';
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql';
import tsx from 'react-syntax-highlighter/dist/esm/languages/prism/tsx';
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml';
import { highlightRenderer, oneDark, oneLight } from '../SyntaxHighlighter';

// The renderer replaces the library's for speed only: every call site's markup
// must stay byte for byte what the library draws, in both themes and in every
// prop shape the app uses. A library upgrade that changes how it builds
// elements fails here instead of shifting colors in the transcript.
const languages = { bash, css, json, markdown, markup, python, rust, sql, tsx, yaml };
for (const [name, grammar] of Object.entries(languages)) PrismLight.registerLanguage(name, grammar);

const samples: Array<[string, string]> = [
  ['python', 'import pandas as pd\n\n# revenue by quarter\ndef bridge(df, *, base="Q1"):\n    """Margin\n    bridge."""\n    return df.pivot_table(index=[\'q\'], values=f"{base}_rev") @ 2\n'],
  ['tsx', 'const Row = ({ label }: { label: string }) => (\n  <li className="row" data-x={`a${label}`}>{/* note */}{label}</li>\n);\nexport default Row;'],
  ['css', '@media (min-width: 40rem) {\n  .card > a[href^="https://"]::after { color: #fff !important; background: url("x.png"); }\n}'],
  ['markup', '<!doctype html>\n<div class="a" onclick="go()"><script>let x = 1;</script><style>p{color:red}</style>&amp;</div>'],
  ['json', '{\n  "ticker": "NVDA",\n  "eps": [1.2, -0.4e3, null, true]\n}'],
  ['bash', 'for f in *.csv; do\n  echo "$f" | grep -v "^#" > "${f%.csv}.txt" # strip\ndone'],
  ['sql', "SELECT q, SUM(rev) AS total -- by quarter\nFROM filings WHERE ticker = 'NVDA'\nGROUP BY q;"],
  ['yaml', 'model:\n  name: "x" # comment\n  tiers: [a, b]\n  multi: |\n    line one\n    line two'],
  ['markdown', '# Title\n\n- **bold** and `code`\n\n```py\nprint(1)\n```\n[link](https://example.com)'],
  ['rust', 'fn main() {\n    let v: Vec<u8> = vec![1, 2];\n    println!("{:?}", v); /* multi\n    line */\n}'],
  ['text', 'no grammar\n  just lines\n'],
  ['python', ''],
];

type Shape = { wrapLines?: boolean; [prop: string]: unknown };

const shapes: Array<[string, Shape]> = [
  ['chat', { wrapLongLines: true, customStyle: { margin: 0, padding: '1rem' }, codeTagProps: { style: { backgroundColor: 'transparent' } } }],
  ['file viewer', { showLineNumbers: true, wrapLines: true }],
  ['numbered long lines', { showLineNumbers: true, wrapLongLines: true }],
  ['bare', {}],
];

function draw(language: string, code: string, style: typeof oneDark, props: Shape, ours: boolean): string {
  return renderToStaticMarkup(
    <PrismLight language={language} style={style} {...props} {...(ours ? { renderer: highlightRenderer, wrapLines: props.wrapLines ?? false } : {})}>
      {code}
    </PrismLight>,
  );
}

describe('highlightRenderer', () => {
  for (const [themeName, theme] of [['dark', oneDark], ['light', oneLight]] as const) {
    for (const [shapeName, props] of shapes) {
      it(`draws what the library draws: ${themeName}, ${shapeName}`, () => {
        for (const [language, code] of samples) {
          const library = draw(language, code, theme, props, false);
          expect(draw(language, code, theme, props, true)).toBe(library);
          // Second pass reads every token's class string and style from the cache.
          expect(draw(language, code, theme, props, true)).toBe(library);
        }
      });
    }
  }

  it('shares one style object between tokens with the same classes', () => {
    const rows = [{ type: 'element' as const, tagName: 'span' as const, properties: { className: [] }, children: [
      { type: 'element' as const, tagName: 'span' as const, properties: { className: ['token', 'keyword'] }, children: [{ type: 'text' as const, value: 'def' }] },
      { type: 'element' as const, tagName: 'span' as const, properties: { className: ['token', 'keyword'] }, children: [{ type: 'text' as const, value: 'return' }] },
    ] }];
    const [line] = highlightRenderer({ rows, stylesheet: oneDark, useInlineStyles: true }) as Array<{ props: { children: Array<{ props: { style: object } }> } }>;
    const [a, b] = line.props.children;
    expect(a.props.style).toBe(b.props.style);
  });
});

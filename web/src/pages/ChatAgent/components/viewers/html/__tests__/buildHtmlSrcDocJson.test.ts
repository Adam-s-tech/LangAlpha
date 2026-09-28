import { runInNewContext } from 'node:vm';
import { describe, expect, it } from 'vitest';

import { buildHtmlSrcDoc, type HtmlSrcDocVariant } from '../buildHtmlSrcDoc';

function parseWidgetData(
  variant: HtmlSrcDocVariant,
  payload: string,
  reviver?: (key: string, value: unknown) => unknown,
) {
  const html = buildHtmlSrcDoc(variant, {
    html: '<div>probe</div>',
    data: { 'data.json': payload },
  });
  const scripts = Array.from(html.matchAll(/<script>([\s\S]*?)<\/script>/g), (m) => m[1]);
  // Execute the generated early script and data injection in an isolated realm,
  // as the iframe does, without replacing the test runner's JSON.parse.
  const encoded = runInNewContext(
    `${scripts[0]}\n${scripts[1]}\n`
      + 'JSON.stringify(JSON.parse(window.__WIDGET_DATA__["data.json"], reviver));',
    {
      window: { addEventListener() {} },
      document: { addEventListener() {} },
      reviver,
    },
  );
  return JSON.parse(encoded);
}

describe.each(['widget-inline', 'widget-fullscreen'] as const)('%s JSON data', (variant) => {
  it.each([
    {
      name: 'quoted values',
      payload: '{"label":"Infinity Growth","literal":"NaN","negative":"-Infinity"}',
      expected: { label: 'Infinity Growth', literal: 'NaN', negative: '-Infinity' },
    },
    {
      name: 'quoted keys',
      payload: '{"NaN":1,"Infinity":2,"-Infinity":3}',
      expected: { NaN: 1, Infinity: 2, '-Infinity': 3 },
    },
    {
      name: 'escaped quotes and backslashes',
      payload: JSON.stringify({ label: 'say "NaN" and "Infinity"', path: 'C:\\Infinity' }),
      expected: { label: 'say "NaN" and "Infinity"', path: 'C:\\Infinity' },
    },
    {
      name: 'normal nested JSON',
      payload: '{"nested":{"label":"Revenue","values":[1.2300,2e+02]}}',
      expected: { nested: { label: 'Revenue', values: [1.23, 200] } },
    },
    {
      name: 'nonstandard numeric constants',
      payload: '[NaN, Infinity, -Infinity]',
      expected: [null, null, null],
    },
    {
      name: 'mixed nested data',
      payload: '{"value":NaN,"nested":[Infinity,-Infinity,{"label":"NaN"}]}',
      expected: { value: null, nested: [null, null, { label: 'NaN' }] },
    },
  ])('preserves $name', ({ payload, expected }) => {
    expect(parseWidgetData(variant, payload)).toEqual(expected);
  });

  it('preserves the native reviver behavior', () => {
    expect(parseWidgetData(variant, '{"n":2,"label":"Infinity"}',
      (key, value) => key === 'n' ? Number(value) * 3 : value,
    )).toEqual({ n: 6, label: 'Infinity' });
  });

  it('still rejects otherwise invalid JSON', () => {
    expect(() => parseWidgetData(variant, '{"value":}')).toThrow();
  });
});

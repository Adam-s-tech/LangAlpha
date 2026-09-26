/**
 * Math has to survive the whole pipeline: remark-math finds it, rehype-katex
 * renders it, and rehype-sanitize runs last, so a KaTeX markup change the
 * schema does not allow strips the output without any error.
 */
import { describe, it, expect, vi } from 'vitest';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

vi.mock('@/contexts/ThemeContext', () => ({
  useTheme: () => ({ theme: 'dark', setTheme: () => {} }),
}));

import Markdown from '../Markdown';

function render(content: string): HTMLElement {
  const host = document.createElement('div');
  host.innerHTML = renderToStaticMarkup(<Markdown variant="chat" content={content} />);
  return host;
}

describe('Markdown math', () => {
  it('renders inline and display math with the MathML tree intact', () => {
    const host = render('Area is $x^2$.\n\n$$\n\\int_0^1 x\\,dx\n$$\n');

    expect(host.querySelectorAll('.katex')).toHaveLength(2);
    expect(host.querySelectorAll('.katex-display')).toHaveLength(1);
    const tex = [...host.querySelectorAll('annotation[encoding="application/x-tex"]')].map((a) => a.textContent);
    expect(tex).toEqual(['x^2', '\\int_0^1 x\\,dx']);
  });

  it('reads \\[...\\] and \\(...\\) as math', () => {
    const host = render('Inline \\(a+b\\) and\n\n\\[E = mc^2\\]\n');

    const tex = [...host.querySelectorAll('annotation[encoding="application/x-tex"]')].map((a) => a.textContent);
    expect(tex).toEqual(['a+b', 'E = mc^2']);
  });

  it('adds no empty numbered row for a trailing \\\\ in align', () => {
    const host = render('$$\n\\begin{align}\na &= b \\\\\nc &= d \\\\\n\\end{align}\n$$\n');

    expect(host.querySelectorAll('.katex-mathml mtr')).toHaveLength(2);
  });

  it('keeps a lone currency dollar as text', () => {
    const host = render('It costs $100 today.');

    expect(host.querySelector('.katex')).toBeNull();
    expect(host.textContent).toContain('$100');
  });
});

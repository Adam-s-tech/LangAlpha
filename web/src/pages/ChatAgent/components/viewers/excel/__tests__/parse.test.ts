import { beforeAll, describe, expect, it } from 'vitest';
import ExcelJS from 'exceljs';
import JSZip from 'jszip';
import { parseWorkbook, type SheetData } from '../parse';
import { openpyxlWorkbook } from './openpyxlFixture';

/**
 * A real ExcelJS round-trip, because every bug this locks was invisible to a
 * hand-built fixture: what `[object Object]` used to be is a formula the writer
 * left uncalculated, and openpyxl writes every formula that way.
 */
async function buildWorkbook(): Promise<ArrayBuffer> {
  const wb = new ExcelJS.Workbook();
  const ws = wb.addWorksheet('Model');
  wb.addWorksheet('Inputs');

  ws.getCell('A1').value = 'DCF model';
  ws.mergeCells('A1:C1');

  ws.getCell('A4').value = 'Units (M)';
  ws.getCell('B4').value = 12.4;
  ws.getCell('B4').numFmt = '#,##0.0';
  ws.getCell('C4').value = 0.1234;
  ws.getCell('C4').numFmt = '0.0%';

  // Calculated: a cached result rode along with the formula.
  ws.getCell('B5').value = { formula: 'B4*2', result: 24.8 };
  ws.getCell('B5').numFmt = '#,##0.0';

  // Uncalculated: a formula and nothing else.
  ws.getCell('B6').value = { formula: 'B5*Inputs!C3' } as ExcelJS.CellFormulaValue;

  ws.getCell('B7').value = { formula: 'B5/0', result: { error: '#DIV/0!' } };

  ws.getCell('B8').value = new Date(Date.UTC(2023, 2, 15));
  ws.getCell('B8').numFmt = 'yyyy-mm-dd';

  const out = await wb.xlsx.writeBuffer();
  return out as ArrayBuffer;
}

describe('parseWorkbook', () => {
  let model: SheetData;

  beforeAll(async () => {
    const sheets = await parseWorkbook(await buildWorkbook(), 'en-US');
    model = sheets[0];
    expect(sheets.map((s) => s.name)).toEqual(['Model', 'Inputs']);
  });

  const at = (ref: string) => {
    const [, col, row] = /^([A-Z]+)(\d+)$/.exec(ref)!;
    const c = col.split('').reduce((n, ch) => n * 26 + (ch.charCodeAt(0) - 64), 0);
    return model.rows[Number(row) - 1][c - 1];
  };

  it('shows a formula with no cached value as its formula, flagged', () => {
    expect(at('B6').calculated).toBe(false);
    expect(at('B6').formula).toBe('B5*Inputs!C3');
    expect(at('B6').text).toBe('=B5*Inputs!C3');
    expect(at('B6').text).not.toContain('[object Object]');
    expect(model.uncalculated).toBe(1);
  });

  it('shows the cached result of a formula that has one', () => {
    expect(at('B5').calculated).toBe(true);
    expect(at('B5').formula).toBe('B4*2');
    expect(at('B5').text).toBe('24.8');
  });

  it('shows a cached error as the error', () => {
    expect(at('B7').error).toBe('#DIV/0!');
    expect(at('B7').text).toBe('#DIV/0!');
  });

  it('applies the number format the cell carries', () => {
    expect(at('B4').text).toBe('12.4');
    expect(at('C4').text).toBe('12.3%');
    expect(at('B8').text).toBe('2023-03-15');
  });

  it('spans a merge from its top-left and marks what it covers', () => {
    expect(at('A1').colSpan).toBe(3);
    expect(at('A1').text).toBe('DCF model');
    expect(at('B1').master).toEqual({ row: 1, col: 1 });
    expect(at('C1').master).toEqual({ row: 1, col: 1 });
  });

  it('keeps row 1 a data row, addressed from 1', () => {
    expect(model.rows[3][0].text).toBe('Units (M)');
    expect(model.totalRows).toBe(8);
  });
});

/** The same workbook as Excel writes it: `xdr:`-prefixed drawings, part names relative to their owner. */
async function asExcelWrites(buffer: ArrayBuffer): Promise<ArrayBuffer> {
  const zip = await JSZip.loadAsync(buffer);
  for (const path of Object.keys(zip.files)) {
    let xml: string;
    if (/^xl\/drawings\/drawing\d+\.xml$/.test(path)) {
      xml = (await zip.file(path)!.async('string'))
        .replace('xmlns="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"', 'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"')
        .replace(/<(\/?)(\w+)(?=[\s/>])/g, '<$1xdr:$2');
    } else if (path.startsWith('xl/') && path.endsWith('.rels')) {
      xml = (await zip.file(path)!.async('string'))
        .replaceAll('Target="/xl/', path.startsWith('xl/_rels/') ? 'Target="' : 'Target="../');
    } else continue;
    zip.file(path, xml);
  }
  return zip.generateAsync({ type: 'arraybuffer' });
}

const drawingsOf = (sheets: SheetData[]) => sheets.map(({ name, charts, pictures }) => ({ name, charts, pictures }));

describe('parseWorkbook on a workbook with charts and pictures', () => {
  it('reads the cells of an openpyxl workbook whose sheets carry drawings', async () => {
    // Every chart or picture used to fail the whole file: ExcelJS reads only
    // the `xdr:` spelling, so the drawing parsed to nothing and the load threw.
    const sheets = await parseWorkbook(openpyxlWorkbook(), 'en-US');
    expect(sheets.map((s) => s.name)).toEqual(['Model', 'Charts', 'Notes', 'Inputs']);
    const [model, , notes] = sheets;
    expect(model.rows[0].map((c) => c.text)).toEqual(['Year', 'Revenue']);
    expect(model.rows[3][1].text).toBe('150');
    expect(model.rows[4][1].text).toBe('=SUM(B2:B4)');
    expect(notes.rows[0][0].text).toBe('Synthetic data');
  });

  it.each([
    ['openpyxl spells it, the namespace as the default', async () => openpyxlWorkbook()],
    ['Excel spells it, with the xdr: prefix', async () => asExcelWrites(openpyxlWorkbook())],
  ])('counts each sheet\'s charts and pictures as %s', async (_, build) => {
    const sheets = await parseWorkbook(await build(), 'en-US');
    expect(drawingsOf(sheets)).toEqual([
      { name: 'Model', charts: 1, pictures: 1 },
      { name: 'Charts', charts: 2, pictures: 0 },
      { name: 'Notes', charts: 0, pictures: 1 },
      { name: 'Inputs', charts: 0, pictures: 0 },
    ]);
    expect(sheets[0].rows[0][0].text).toBe('Year');
  });

  it('counts none for a drawing it cannot read, and still opens the sheet', async () => {
    const zip = await JSZip.loadAsync(openpyxlWorkbook());
    zip.file('xl/drawings/drawing1.xml', '<wsDr');
    const sheets = await parseWorkbook(await zip.generateAsync({ type: 'arraybuffer' }), 'en-US');
    expect(drawingsOf(sheets)[0]).toEqual({ name: 'Model', charts: 0, pictures: 0 });
    expect(drawingsOf(sheets)[1]).toEqual({ name: 'Charts', charts: 2, pictures: 0 });
    expect(sheets[0].rows[0][0].text).toBe('Year');
  });

  it('counts nothing on a workbook with no drawings', async () => {
    const sheets = await parseWorkbook(await buildWorkbook(), 'en-US');
    expect(drawingsOf(sheets)).toEqual([
      { name: 'Model', charts: 0, pictures: 0 },
      { name: 'Inputs', charts: 0, pictures: 0 },
    ]);
  });
});

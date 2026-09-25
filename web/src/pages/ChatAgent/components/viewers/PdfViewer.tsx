import React, { useState, useEffect, useMemo } from 'react';
import { useStableHandler } from '@/hooks/useStableHandler';
import { Document, Page, pdfjs } from 'react-pdf';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import 'react-pdf/dist/Page/TextLayer.css';
import './PdfViewer.css';

// Vite-native ?url import resolves correctly in both dev and build. Legacy, to
// match the main-thread build vite.config.js aliases react-pdf to.
import pdfjsWorkerUrl from 'pdfjs-dist/legacy/build/pdf.worker.min.mjs?url';
pdfjs.GlobalWorkerOptions.workerSrc = pdfjsWorkerUrl;

// Image decoders the build emits beside the worker (vite.config.js `pdfjsWasm`).
// Module-level so react-pdf sees one options object and never reloads over it.
const DOCUMENT_OPTIONS = {
  wasmUrl: `${import.meta.env.BASE_URL}assets/pdfjs-wasm/${pdfjs.version}/`,
};

const ZOOM_STEPS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0];
const DEFAULT_ZOOM_INDEX = 2; // 1.0

interface PdfViewerProps {
  data: ArrayBuffer | Uint8Array;
  /** Page a reference pointed at; `focusSeq` changes on each visit so a repeat click returns to it. */
  focusPage?: number | null;
  focusSeq?: number | null;
  onPageCount?: (count: number) => void;
}

export default function PdfViewer({ data, focusPage = null, focusSeq = null, onPageCount }: PdfViewerProps) {
  const [numPages, setNumPages] = useState<number | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [zoomIndex, setZoomIndex] = useState(DEFAULT_ZOOM_INDEX);
  const [error, setError] = useState<Error | null>(null);

  const scale = ZOOM_STEPS[zoomIndex];

  const onDocumentLoadSuccess = useStableHandler(({ numPages: n }: { numPages: number }) => {
    setNumPages(n);
    setPageNumber(focusPage && focusPage <= n ? focusPage : 1);
    onPageCount?.(n);
  });

  useEffect(() => {
    if (focusPage && numPages && focusPage <= numPages) setPageNumber(focusPage);
  }, [focusSeq]); // eslint-disable-line react-hooks/exhaustive-deps

  const goToPrev = () => setPageNumber((p) => Math.max(1, p - 1));
  const goToNext = () => setPageNumber((p) => Math.min(numPages || 1, p + 1));
  const zoomIn = () => setZoomIndex((i) => Math.min(ZOOM_STEPS.length - 1, i + 1));
  const zoomOut = () => setZoomIndex((i) => Math.max(0, i - 1));

  // A new `file` object is a new document to react-pdf, so a render that rebuilt
  // it would reload the PDF and reset the page.
  const fileData = useMemo(() => {
    if (!data) return null;
    const bytes = data instanceof ArrayBuffer ? new Uint8Array(data) : data;
    return { data: bytes };
  }, [data]);

  // Bubble errors to the error boundary via render-phase throw
  if (error) throw error;

  return (
    <div className="pdf-viewer">
      {/* Controls */}
      <div className="pdf-controls">
        <div className="pdf-nav">
          <button onClick={goToPrev} disabled={pageNumber <= 1} className="pdf-btn">
            Prev
          </button>
          <span className="pdf-page-info">
            {pageNumber} / {numPages ?? '...'}
          </span>
          <button onClick={goToNext} disabled={pageNumber >= (numPages || 1)} className="pdf-btn">
            Next
          </button>
        </div>
        <div className="pdf-zoom">
          <button onClick={zoomOut} disabled={zoomIndex <= 0} className="pdf-btn">
            −
          </button>
          <span className="pdf-zoom-info">{Math.round(scale * 100)}%</span>
          <button onClick={zoomIn} disabled={zoomIndex >= ZOOM_STEPS.length - 1} className="pdf-btn">
            +
          </button>
        </div>
      </div>

      {/* Document */}
      <div className="pdf-document-wrapper">
        {/* react-pdf defaults to Suspense, which would discard `fileData` with this
            never-committed component on every retry and reload forever. Effect
            mode keeps the loading props and the error throw above; Page inherits it. */}
        <Document
          suspense={false}
          file={fileData}
          options={DOCUMENT_OPTIONS}
          onLoadSuccess={onDocumentLoadSuccess}
          onLoadError={(err: Error) => setError(err)}
          loading={
            <div className="pdf-loading">Loading PDF...</div>
          }
        >
          <Page
            pageNumber={pageNumber}
            scale={scale}
            loading={<div className="pdf-loading">Rendering page...</div>}
          />
        </Document>
      </div>
    </div>
  );
}

import { useEffect, useRef, useState } from 'react';
import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentLoadingTask,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { api, errorMessage } from '../../lib/api';
import type { EvidenceDocument, RegionCitation } from '../../lib/apiSchemas';
import { Button } from '@/components/ui/button';
import { NativeSelect } from '@/components/ui/native-select';

GlobalWorkerOptions.workerSrc = workerUrl;

type SourceAccess =
  | { returnId: string; revision: string; loadOriginal?: never }
  | { loadOriginal: (signal?: AbortSignal) => Promise<Response>; returnId?: never; revision?: never };

type Props = SourceAccess & {
  document: EvidenceDocument;
  citation: RegionCitation['evidence'] | null;
  citationLabel?: string;
  page: number;
  onChoosePage: (page: number) => void;
};

export default function DocumentViewer({
  returnId, document: source, revision, loadOriginal, citation, citationLabel, page, onChoosePage,
}: Props) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [width, setWidth] = useState(600);
  const [rendered, setRendered] = useState('');
  const [pageSize, setPageSize] = useState({ width: 0, height: 0 });

  const viewportRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [downloading, setDownloading] = useState(false);

  const renderKey = `${source.id}:${source.versionId}:${page}:${width}:${zoom}:${retry}`;

  useEffect(() => {
    const target = viewportRef.current;
    if (!target) return;

    const observer = new ResizeObserver(entries => {
      const measured = entries[0]?.contentRect.width;
      if (measured) setWidth(Math.max(220, Math.floor(measured - 48)));
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    let loadingTask: PDFDocumentLoadingTask | undefined;

    setPdf(null);
    setError('');
    setRendered('');

    async function loadPdf() {
      try {
        // Fetch bytes ourselves so the PDF request uses the same authenticated API.
        const response = loadOriginal
          ? await loadOriginal(controller.signal)
          : await api.original(returnId, revision, source.id, source.versionId, controller.signal);
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (cancelled) return;

        loadingTask = getDocument({ data: bytes, useSystemFonts: true });
        const document = await loadingTask.promise;
        if (!cancelled) setPdf(document);
      } catch (reason) {
        if (!cancelled) setError(errorMessage(reason));
      }
    }

    void loadPdf();
    return () => {
      cancelled = true;
      controller.abort();
      if (loadingTask) void loadingTask.destroy().catch(() => { });
    };
  }, [returnId, source.id, source.versionId, revision, loadOriginal, retry]);

  useEffect(() => {
    if (!pdf || !canvasRef.current) return;
    let cancelled = false;
    let renderTask: RenderTask | undefined;
    const canvas = canvasRef.current;

    setRendered('');
    setError('');

    async function renderPage() {
      try {
        const pdfPage = await pdf!.getPage(page);
        if (cancelled) return;

        const natural = pdfPage.getViewport({ scale: 1 });
        const viewport = pdfPage.getViewport({ scale: (width / natural.width) * zoom });
        const density = Math.min(window.devicePixelRatio || 1, 2);
        canvas.width = Math.floor(viewport.width * density);
        canvas.height = Math.floor(viewport.height * density);
        const context = canvas.getContext('2d');
        if (!context) throw new Error('This browser cannot display the PDF canvas.');

        setPageSize({ width: viewport.width, height: viewport.height });
        renderTask = pdfPage.render({
          canvas,
          canvasContext: context,
          viewport,
          transform: density === 1 ? undefined : [density, 0, 0, density, 0, 0],
        });
        await renderTask.promise;
        if (!cancelled) setRendered(renderKey);
      } catch (reason) {
        if (!cancelled) setError(errorMessage(reason));
      }
    }

    void renderPage();
    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [pdf, page, width, zoom, renderKey]);

  useEffect(() => {
    if (rendered !== renderKey) return;
    const viewport = viewportRef.current;
    if (!viewport) return;

    if (citation && citation.page === page) {
      const centerX = (citation.rect[0] + citation.rect[2] / 2) * pageSize.width;
      const centerY = (citation.rect[1] + citation.rect[3] / 2) * pageSize.height;
      viewport.scrollTo({
        top: Math.max(0, centerY - viewport.clientHeight / 2 + 24),
        left: Math.max(0, centerX - viewport.clientWidth / 2 + 24),
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches
          ? 'instant'
          : 'smooth',
      });
    } else {
      viewport.scrollTo({ top: 0, left: 0 });
    }
  }, [rendered, renderKey, citation, page, pageSize]);

  async function downloadOriginal() {
    setDownloading(true);

    try {
      const response = loadOriginal
        ? await loadOriginal()
        : await api.original(returnId, revision, source.id, source.versionId);
      const url = URL.createObjectURL(await response.blob());

      const anchor = window.document.createElement('a');
      anchor.href = url;
      anchor.download = source.title.toLowerCase().endsWith('.pdf')
        ? source.title
        : `${source.title}.pdf`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setDownloading(false);
    }
  }

  const ready = rendered === renderKey && !!pdf;
  const count = pdf?.numPages ?? source.pageCount ?? 1;
  const pageDescription =
    citation?.page === page
      ? `Highlighted evidence: ${citation.quote}`
      : 'No evidence highlighted on this page.';

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2.5 border-b border-[#dde5d5] bg-[#fafcf6] px-[15px] py-[9px]" aria-label="PDF page and zoom controls">
        <div className="flex items-center gap-[5px]">
          <Button
            variant="ghost" size="icon" className="size-7 text-sm text-[#798d68]"
            aria-label="Previous PDF page"
            disabled={page <= 1 || !pdf}
            onClick={() => onChoosePage(page - 1)}
          >
            ←
          </Button>
          <label className="flex items-center gap-[5px] whitespace-nowrap text-[9px] font-normal text-[#8c9a7e]">
            Page
            <NativeSelect className="h-7 w-auto px-[5px] py-[3px] text-[10px] text-[#708960]"
              aria-label="PDF page"
              value={page}
              disabled={!pdf}
              onChange={event => onChoosePage(Number(event.target.value))}
            >
              {Array.from({ length: count }, (_, index) => (
                <option key={index} value={index + 1}>
                  {index + 1}
                </option>
              ))}
            </NativeSelect>
            of {count}
          </label>
          <Button
            variant="ghost" size="icon" className="size-7 text-sm text-[#798d68]"
            aria-label="Next PDF page"
            disabled={page >= count || !pdf}
            onClick={() => onChoosePage(page + 1)}
          >
            →
          </Button>
        </div>
        <div className="flex items-center gap-[5px]">
          <Button
            variant="ghost" size="icon" className="size-7 text-sm text-[#798d68]"
            aria-label="Zoom out"
            disabled={zoom <= 0.75}
            onClick={() => setZoom(value => Math.max(0.75, value - 0.25))}
          >
            −
          </Button>
          <Button variant="outline" size="sm" className="h-7 px-1.5 text-[9px] text-[#7b9069]" onClick={() => setZoom(1)} title="Reset to fit width">
            {Math.round(zoom * 100)}%
          </Button>
          <Button
            variant="ghost" size="icon" className="size-7 text-sm text-[#798d68]"
            aria-label="Zoom in"
            disabled={zoom >= 2}
            onClick={() => setZoom(value => Math.min(2, value + 0.25))}
          >
            +
          </Button>
        </div>
      </div>
      <div className="border-b border-border bg-card px-4 py-2">
        <Button variant="outline" size="sm" disabled={downloading} onClick={() => void downloadOriginal()}>
          {downloading ? 'Downloading…' : 'Download original PDF'}
        </Button>
      </div>
      <div className="relative h-[clamp(240px,47vh,660px)] flex-none overflow-auto overscroll-contain bg-[#e9ede4] p-6 [scrollbar-color:#becbb5_#e9ede4] [scrollbar-width:thin]" ref={viewportRef} aria-busy={!ready && !error}>
        {error && (
          <div className="absolute inset-x-6 top-10 z-10 p-[25px] text-center text-xs text-[#7d8d70]" role="alert">
            <strong>Unable to display this source</strong>
            <p className="my-2.5">{error}</p>
            <Button variant="outline" size="sm" onClick={() => setRetry(value => value + 1)}>Retry PDF</Button>
          </div>
        )}
        {!ready && !error && (
          <div className="absolute inset-x-6 top-10 z-10 p-[25px] text-center text-xs text-[#7d8d70]" role="status">
            Rendering local PDF…
          </div>
        )}
        <div
          className={`pdf-paper relative mx-auto shrink-0 bg-white shadow-[0_2px_9px_#2b3e211d] ${ready && !error ? 'visible' : 'invisible'}`}
          style={{
            width: pageSize.width || width,
            height: pageSize.height || width * 1.3,
          }}
        >
          <canvas
            className="block"
            ref={canvasRef}
            style={{ width: pageSize.width, height: pageSize.height }}
            role="img"
            aria-label={`${source.title}, page ${page}. ${pageDescription}`}
          />
          {citation?.page === page && (
            <div
              className="pointer-events-none absolute rounded-[2px] border-2 border-[#c4a630] bg-[#efda4654] shadow-[0_0_0_3px_#efda461a] mix-blend-multiply"
              aria-hidden="true"
              style={{
                left: `${citation.rect[0] * 100}%`,
                top: `${citation.rect[1] * 100}%`,
                width: `${citation.rect[2] * 100}%`,
                height: `${citation.rect[3] * 100}%`,
              }}
            />
          )}
        </div>
      </div>
      {citation && (
        <div className="flex items-start gap-[9px] border-t border-[#e1e7d9] bg-[#fbfcf7] px-[21px] py-4">
          <span className="mt-1 inline-block size-[7px] shrink-0 rounded-[2px] bg-[#cbba44]" />
          <div>
            <strong className="text-[10px] font-semibold text-[#82936f]">
              {citationLabel} · page {citation.page}
            </strong>
            <blockquote className="mt-[5px] text-[11px] leading-[1.7] text-[#667b54] [overflow-wrap:anywhere]">“{citation.quote}”</blockquote>
            {citation.page !== page && (
              <Button variant="link" className="mt-2 h-auto whitespace-normal p-0 text-[10px]" onClick={() => onChoosePage(citation.page)}>
                Return to highlighted evidence
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

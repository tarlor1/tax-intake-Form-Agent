import { useEffect, useRef, useState } from 'react';
import { api, errorMessage } from '../../lib/api';
import type { EvidenceDocument, LineCitation } from '../../lib/apiSchemas';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';

type SourceAccess =
  | { returnId: string; revision: string; loadOriginal?: never }
  | { loadOriginal: (signal?: AbortSignal) => Promise<Response>; returnId?: never; revision?: never };

type Props = SourceAccess & {
  document: EvidenceDocument;
  citation: LineCitation['evidence'] | null;
  citationLabel?: string;
};

export default function TextDocumentViewer({ returnId, document: source, citation, citationLabel, revision, loadOriginal }: Props) {
  const [lines, setLines] = useState<string[] | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const viewport = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    setLines(null);

    const response = loadOriginal
      ? loadOriginal(controller.signal)
      : api.original(returnId, revision, source.id, source.versionId, controller.signal);
    void response
      .then(response => response.text())
      .then(text => {
        if (controller.signal.aborted) return;

        const parsed = text.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/);
        if (parsed[parsed.length - 1] === '') parsed.pop();
        setLines(parsed);
      })
      .catch(reason => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      });
    return () => controller.abort();
  }, [returnId, source.id, source.versionId, revision, loadOriginal, retry]);

  useEffect(() => {
    if (!lines || !citation?.lineStart) return;

    const container = viewport.current;
    const target = container?.querySelector<HTMLElement>(`[data-line="${citation.lineStart}"]`);
    if (container && target)
      container.scrollTo({
        top: target.offsetTop - container.offsetTop - container.clientHeight / 3,
      });
  }, [lines, citation]);
  return (
    <div className="flex min-w-0 flex-col">
      {error && (
        <Alert variant="destructive" role="alert">
          {error}
          <Button variant="outline" size="sm" onClick={() => setRetry(value => value + 1)}>Retry TXT</Button>
        </Alert>
      )}
      {!lines && !error && <p role="status">Loading original text…</p>}
      <div
        className="text-evidence-viewport relative max-h-[48vh] min-h-[220px] overflow-auto overscroll-contain bg-white py-3 font-mono text-xs leading-[1.8] focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring"
        ref={viewport}
        tabIndex={0}
        role="region"
        aria-label={`${source.title}, selectable original text with line numbers`}
      >
        {lines?.map((line, index) => {
          const number = index + 1;
          const cited =
            !!citation?.lineStart &&
            number >= citation.lineStart &&
            number <= (citation.lineEnd ?? citation.lineStart);
          return (
            <div
              className={`text-evidence-line grid grid-cols-[48px_minmax(0,1fr)] whitespace-pre-wrap pr-3.5 [overflow-wrap:anywhere] ${cited ? 'is-cited bg-[#fff2b7]' : ''}`}
              key={index}
              data-line={number}
            >
              <span className="select-none pr-3 text-right text-[#8b978e]" aria-hidden="true">
                {number}
              </span>
              <span>{line || '\u00a0'}</span>
            </div>
          );
        })}
      </div>
      {citation && (
        <div className="flex items-start gap-[9px] border-t border-[#e1e7d9] bg-[#fbfcf7] px-[21px] py-4">
          <div>
            <strong className="text-[10px] font-semibold text-[#82936f]">
              {citationLabel} ·{' '}
              {citation.lineStart
                ? `lines ${citation.lineStart}–${citation.lineEnd ?? citation.lineStart}`
                : 'text source'}
            </strong>
            <blockquote className="mt-[5px] text-[11px] leading-[1.7] text-[#667b54] [overflow-wrap:anywhere]">“{citation.quote}”</blockquote>
          </div>
        </div>
      )}
    </div>
  );
}

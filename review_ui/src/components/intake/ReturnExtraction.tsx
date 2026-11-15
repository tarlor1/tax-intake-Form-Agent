import { Card } from '@/components/ui/card';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { useEffect, useRef, useState } from 'react';
import { Link } from '@tanstack/react-router';
import { api, errorMessage, type InboxDocument, type IntakeState, type ReturnDetail } from '../../lib/api';
import { AIConsent } from './AIConsent';

interface Props {
  detail: ReturnDetail;
  disabled: boolean;
  onUpdated: (detail: ReturnDetail) => void;
}

export default function ReturnExtraction({ detail, disabled, onUpdated }: Props) {
  const [config, setConfig] = useState<IntakeState | null>(null);
  const [documents, setDocuments] = useState<InboxDocument[]>([]);
  const [approved, setApproved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const tracked = useRef(new Set<string>());
  const fingerprint = useRef('');
  const onUpdatedRef = useRef(onUpdated);
  onUpdatedRef.current = onUpdated;

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;

    async function poll() {
      try {
        const state = await api.intake(controller.signal);
        if (controller.signal.aborted) return;

        setConfig(state);
        const related = state.documents.filter(
          document =>
            tracked.current.has(document.id) ||
            document.groups.some(group => group.returnId === detail.id),
        );
        setDocuments(related);

        const next = related
          .map(document => `${document.id}:${document.status}:${document.updatedAt}`)
          .sort()
          .join('|');
        if (fingerprint.current !== next) {
          const hadPreviousState = fingerprint.current !== '';
          fingerprint.current = next;
          if (hadPreviousState || related.some(document => document.status === 'completed')) {
            const saved = await api.detail(detail.id, controller.signal);
            if (!controller.signal.aborted) onUpdatedRef.current(saved);
          }
        }
      } catch (reason) {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      }

      if (!controller.signal.aborted) timer = window.setTimeout(() => void poll(), 1800);
    }

    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [detail.id]);

  async function extract() {
    if (busy) return;

    setBusy(true);
    setError('');

    try {
      const pending = await api.extract(detail.id, detail.revision, approved);
      tracked.current = new Set(pending.map(document => document.id));
      setDocuments(pending);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card role="region" className="return-extraction mt-[22px] overflow-hidden rounded-lg border border-border bg-card p-[22px] max-[720px]:p-4" aria-labelledby="extraction-heading">
      <div className="mb-[18px] flex items-center justify-between gap-3 max-[720px]:flex-col max-[720px]:items-start [&_h2]:text-base [&_h2]:font-semibold">
        <div>
          <p className="mb-1.5 text-[10px] font-bold tracking-[1.45px] text-muted-foreground">SOURCE EXTRACTION</p>
          <h2 id="extraction-heading">Extract documents</h2>
        </div>
        <Button

          disabled={
            disabled ||
            busy ||
            !config?.configured ||
            !approved ||
            !detail.documents.length ||
            documents.some(document => document.status === 'processing')
          }
          onClick={() => void extract()}
        >
          {busy ? 'Scheduling…' : 'Extract documents'}
        </Button>
      </div>
      {config && (
        <AIConsent
          configured={config.configured}
          model={config.model}
          approved={approved}
          onChange={setApproved}
        />
      )}
      {!config && !error && <p role="status">Loading extraction configuration…</p>}
      {error && (
        <Alert variant="destructive" className="rounded-md border border-[#edccbd] bg-[#fff0eb] px-4 py-[13px] text-xs leading-relaxed text-[#93472f]" role="alert">
          {error}
        </Alert>
      )}
      <ul className="extraction-statuses list-disc pl-5 text-xs [&_li]:mt-2" aria-live="polite">
        {documents.map(document => (
          <li key={document.id}>
            <strong>{document.filename}</strong> ·{' '}
            {
              {
                uploaded: 'Saved locally · extraction needed',
                processing: 'Extracting…',
                needs_assignment: 'Assignment needed in the document inbox',
                completed: 'Extracted',
                failed: 'Extraction failed · retry with Extract documents',
              }[document.status]
            }
            {document.error && <Alert variant="destructive" className="rounded-md border border-[#edccbd] bg-[#fff0eb] px-4 py-[13px] text-xs leading-relaxed text-[#93472f]">{document.error}</Alert>}
          </li>
        ))}
      </ul>
      {documents.some(document => document.status === 'needs_assignment') && (
        <Link className="text-primary underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-ring" to="/clients" search={{}}>
          Open document inbox to confirm ambiguous groups
        </Link>
      )}
    </Card>
  );
}

import { useCallback, useEffect, useState } from 'react';
import { Link } from '@tanstack/react-router';
import { ApiError, api, errorMessage, type InboxEvidenceFile } from '@/lib/api';
import type { EvidenceDocument, LineCitation, RegionCitation } from '@/lib/apiSchemas';
import {
  inboxFileIsPending, INBOX_REFRESH_MS, PRACTICE_APPROVAL_CHANNEL, practiceDestination,
  type PracticeFieldSelection
} from '@/lib/practiceDraft';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import DocumentViewer from '../evidence/DocumentViewer';
import TextDocumentViewer from '../evidence/TextDocumentViewer';
import PracticeStatus from './PracticeStatus';

interface Props {
  batchId: string;
  file: InboxEvidenceFile;
  practiceSelection?: PracticeFieldSelection;
  practiceError?: string;
  openPractice: (file: InboxEvidenceFile) => void;
}

export default function InboxEvidence({ batchId, file: initialFile, practiceSelection, practiceError, openPractice }: Props) {
  const [file, setFile] = useState(initialFile);
  const [approving, setApproving] = useState(false);
  const [approvalError, setApprovalError] = useState('');
  const [refreshError, setRefreshError] = useState('');
  const { slot, uploadId, versionId, practice } = file;
  const pending = inboxFileIsPending(file);
  const destination = practiceDestination(practice);
  const nativeSelection = practiceSelection?.href === destination?.url.href ? practiceSelection : undefined;
  const nativeField = nativeSelection?.field;
  const factIndex = nativeField && practice && Object.hasOwn(practice.bindings, nativeField)
    ? practice.bindings[nativeField] : null;
  const facts = file.analysis?.facts;
  const boundFact = factIndex !== null && facts?.[factIndex] ? factIndex : null;
  const [manual, setManual] = useState<{
    selection?: PracticeFieldSelection;
    factIndex: number | null;
    page: number | null;
  }>({ factIndex: 0, page: null });
  const currentManual = manual.selection === nativeSelection ? manual : null;
  const selected = currentManual ? currentManual.factIndex : nativeSelection ? boundFact : 0;
  const approvalScope = JSON.stringify([batchId, slot, versionId]);

  useEffect(() => {
    if (approving || (!pending && practice?.status !== 'waiting')) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const approvals = pending ? null : new BroadcastChannel(PRACTICE_APPROVAL_CHANNEL);
    if (approvals) {
      approvals.onmessage = ({ data }) => {
        if (data !== approvalScope) return;
        approvals.close();
        void refresh();
      };
    } else {
      timer = setTimeout(refresh, INBOX_REFRESH_MS);
    }

    async function refresh() {
      try {
        const updated = await api.inboxEvidence(batchId, slot, controller.signal);
        if (controller.signal.aborted) return;
        if (updated.uploadId !== uploadId || updated.versionId !== versionId) {
          setRefreshError('This evidence no longer identifies the original upload. Reopen it from the inbox.');
          return;
        }
        setFile(updated);
        setRefreshError('');
        if (!inboxFileIsPending(updated)) return;
      } catch (cause) {
        if (controller.signal.aborted) return;
        setRefreshError(errorMessage(cause));
        if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) return;
      }
      timer = setTimeout(refresh, INBOX_REFRESH_MS);
    }
    return () => {
      clearTimeout(timer);
      approvals?.close();
      controller.abort();
    };
  }, [approvalScope, approving, batchId, pending, practice?.status, slot, uploadId, versionId]);

  async function approvePractice() {
    setApproving(true);
    setApprovalError('');
    try {
      const updated = await api.approveInboxPractice(batchId, slot);
      if (updated.uploadId !== uploadId || updated.versionId !== versionId || !updated.practice) {
        throw new Error('The preparation response does not identify this uploaded document.');
      }
      setFile(updated);
      setRefreshError('');
      const approvals = new BroadcastChannel(PRACTICE_APPROVAL_CHANNEL);
      approvals.postMessage(approvalScope);
      approvals.close();
    } catch (cause) {
      setApprovalError(errorMessage(cause));
    } finally {
      setApproving(false);
    }
  }
  const loadOriginal = useCallback((signal?: AbortSignal) =>
    api.inboxOriginal(batchId, file.slot, signal), [batchId, file.slot]);
  const source: EvidenceDocument = {
    id: file.uploadId,
    versionId: file.versionId,
    title: file.filename,
    mediaType: file.mediaType,
    pageCount: null,
  };
  const original = { documentId: source.id, versionId: source.versionId };
  const fact = selected === null ? undefined : facts?.[selected];
  const page = currentManual?.page ?? fact?.page ?? 1;
  const pdfCitation: RegionCitation['evidence'] | null = fact?.page && fact.rect
    ? { precision: 'region', original, page: fact.page, rect: fact.rect, quote: fact.quote }
    : null;
  const textCitation: LineCitation['evidence'] | null = fact && fact.page === null
    ? { precision: 'lines', original, lineStart: fact.line, lineEnd: fact.line, quote: fact.quote }
    : null;

  return (
    <main id="main-content" className="mx-auto w-full max-w-6xl space-y-4 px-5 py-8">
      <header>
        <Link to="/clients" search={{}} className="mb-2 inline-block text-sm underline">Back to inbox</Link>
        <p className="text-sm text-muted-foreground">Document inbox evidence</p>
        <h1 className="break-words text-2xl font-semibold">{source.title}</h1>
        {file.analysis?.message && <p className="mt-1 text-sm text-muted-foreground">{file.analysis.message}</p>}
        {practice && (
          <div className="mt-3 space-y-2">
            <PracticeStatus practice={practice} />
            {practice.status === 'waiting' && (
              <p className="text-sm text-muted-foreground">
                Approve only public samples. Only guarded preparation choices are sent to Gemini.
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" size="sm" disabled={!destination} onClick={() => openPractice(file)}>
                Open practice draft
              </Button>
              {practice.status === 'waiting' && (
                <Button type="button" size="sm" disabled={approving} onClick={() => void approvePractice()}>
                  {approving ? 'Approving…' : 'Approve Gemini and prepare'}
                </Button>
              )}
            </div>
          </div>
        )}
        {(practiceError || approvalError || refreshError) && (
          <Alert className="mt-2" variant="destructive" role="alert">{practiceError || approvalError || refreshError}</Alert>
        )}
      </header>
      <div className="grid items-start gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <section className="min-w-0 space-y-2 rounded-lg border border-border p-3" aria-label="Extracted facts">
          <h2 className="font-semibold">Extracted facts</h2>
          {selected === null && <p className="text-sm text-muted-foreground">No linked citation for the active practice field.</p>}
          {facts?.length ? (
            <ul className="max-h-[55vh] space-y-1 overflow-auto p-1">
              {facts.map((item, index) => (
                <li key={index}>
                  <Button type="button" variant={selected === index ? 'secondary' : 'ghost'}
                    className="h-auto w-full flex-col items-start gap-0 whitespace-normal break-words py-2 text-left"
                    aria-pressed={selected === index}
                    onClick={() => setManual({ selection: nativeSelection, factIndex: index, page: null })}>
                    <span className="w-full font-semibold">{item.label}</span>
                    <span className="w-full text-muted-foreground">{item.value}</span>
                  </Button>
                </li>
              ))}
            </ul>
          ) : <p className="text-sm text-muted-foreground">No extracted facts are available. The original is shown for review.</p>}
        </section>
        <section className="min-w-0 overflow-hidden rounded-lg border border-border" aria-label="Original document">
          {source.mediaType === 'text/plain' ? (
            <TextDocumentViewer document={source} loadOriginal={loadOriginal} citation={textCitation} citationLabel={fact?.label} />
          ) : (
            <>
              {fact && !pdfCitation && (
                <div className="space-y-1 border-b border-border p-3 text-sm">
                  <p className="text-muted-foreground">{fact.page === null ? 'Page not verified.' : `Page ${fact.page} · No exact highlight available.`}</p>
                  {fact.quote && <blockquote className="whitespace-pre-wrap break-words">{fact.quote}</blockquote>}
                </div>
              )}
              <DocumentViewer document={source} loadOriginal={loadOriginal} citation={pdfCitation}
                citationLabel={fact?.label} page={page}
                onChoosePage={page => setManual({ selection: nativeSelection, factIndex: selected, page })} />
            </>
          )}
        </section>
      </div>
    </main>
  );
}

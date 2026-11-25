import { useEffect, useRef, useState } from 'react';
import { createFileRoute, notFound } from '@tanstack/react-router';
import { api } from '../lib/api';
import { queueSearchSchema, type BatchFile } from '../lib/apiSchemas';
import { practiceDestination, type PracticeDestination, type PracticeFieldSelection } from '../lib/practiceDraft';
import ClientQueue from '../components/clients/ClientQueue';
import InboxEvidence from '../components/intake/InboxEvidence';

const slotScope = (batchId: string, file: BatchFile) =>
  JSON.stringify([batchId, file.slot, file.uploadId, file.versionId]);

interface PracticeWindow {
  scope: string;
  target: Window;
  destination: PracticeDestination;
  field?: string | null;
}

interface PracticeNotice {
  scope: string;
  selection?: PracticeFieldSelection;
  error?: string;
}

export const Route = createFileRoute('/clients')({
  validateSearch: search => {
    const parsed = queueSearchSchema.safeParse(search);
    if (!parsed.success) throw notFound();
    return parsed.data;
  },
  loaderDeps: ({ search: { inboxBatch, inboxSlot } }) => ({ inboxBatch, inboxSlot }),
  loader: async ({ deps: { inboxBatch, inboxSlot }, abortController }) => {
    const { signal } = abortController;
    if (inboxBatch !== undefined && inboxSlot !== undefined) {
      const file = await api.inboxEvidence(inboxBatch, inboxSlot, signal);
      return { kind: 'evidence' as const, batchId: inboxBatch, file };
    }
    return { kind: 'queue' as const, returns: await api.returns(signal) };
  },
  staleTime: 0,
  component: ClientsPage,
});

function ClientsPage() {
  const data = Route.useLoaderData();
  const navigate = Route.useNavigate();
  const opened = useRef<PracticeWindow | null>(null);
  const [notice, setNotice] = useState<PracticeNotice | null>(null);

  useEffect(() => {
    function follow(event: MessageEvent) {
      const pair = opened.current;
      if (!pair || event.source !== pair.target || event.origin !== pair.destination.url.origin) return;
      const message = event.data;
      if (!message || message.type !== 'practice-field' || typeof message.field !== 'string' ||
        typeof message.draft !== 'string' || typeof message.section !== 'string') return;
      const field = message.draft === pair.destination.draft && message.section === pair.destination.section &&
        message.pathname === pair.destination.url.pathname
        ? message.field : null;
      if (field === pair.field) return;
      pair.field = field;
      setNotice({ scope: pair.scope, selection: { href: pair.destination.url.href, field } });
    }
    window.addEventListener('message', follow);
    return () => window.removeEventListener('message', follow);
  }, []);

  function openDraft(batchId: string, file: BatchFile) {
    const destination = practiceDestination(file.practice);
    if (!destination || !file.uploadId || !file.versionId) return;
    const scope = slotScope(batchId, file);
    const pair = opened.current;
    if (pair?.scope === scope && pair.destination.url.href === destination.url.href && !pair.target.closed) {
      pair.target.focus();
      return;
    }
    // The trusted practice target needs its opener to send passive field notifications.
    const target = window.open(destination.url.href, `practice-${crypto.randomUUID()}|${window.location.origin}`, 'popup');
    opened.current = target ? { scope, target, destination } : null;
    setNotice(target ? null : { scope, error: 'The practice window was blocked. Allow pop-ups and try again.' });
  }

  if (data.kind === 'queue') {
    return <ClientQueue returns={data.returns} openPractice={(batchId, file) => {
      // Preserve this window as the popup's opener as the inbox becomes the evidence view.
      openDraft(batchId, file);
      void navigate({ search: { inboxBatch: batchId, inboxSlot: file.slot } });
    }} />;
  }
  const scope = slotScope(data.batchId, data.file);
  const currentNotice = notice?.scope === scope ? notice : null;
  return <InboxEvidence key={scope} batchId={data.batchId} file={data.file}
    practiceSelection={currentNotice?.selection} practiceError={currentNotice?.error}
    openPractice={file => openDraft(data.batchId, file)} />;
}

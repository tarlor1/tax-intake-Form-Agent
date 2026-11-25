import { useEffect, useRef, useState } from 'react';
import { Link } from '@tanstack/react-router';
import { Alert } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { NativeSelect } from '@/components/ui/native-select';
import { ApiError, api, errorMessage, streamReviewSelections, type Job, type ReviewIdentity } from '@/lib/api';
import DocumentInbox from '../intake/DocumentInbox';
import { useDocumentInbox } from '@/hooks/useDocumentInbox';
import type { EvidenceCitation, EvidenceDetail, EvidenceDocument, EvidenceField, EvidenceSearch, ReviewSelection } from '@/lib/apiSchemas';
import DocumentViewer from '../evidence/DocumentViewer';
import TextDocumentViewer from '../evidence/TextDocumentViewer';

interface Props {
  detail: EvidenceDetail;
  search: EvidenceSearch;
  jobs: Job[];
  refresh: () => Promise<void>;
  navigateView: (search: EvidenceSearch, replace?: boolean) => Promise<void>;
  previous: () => void;
  next: () => void;
  canGoBack: boolean;
  canGoForward: boolean;
}

const selectionFields = ['revision', 'field', 'document', 'version', 'citation', 'page'] as const;
const keyOf = (view: EvidenceSearch) => JSON.stringify(selectionFields.map(key => view[key] ?? null));
const sourceKey = (doc: EvidenceDocument) => `${doc.id}/${doc.versionId}`;
const label = (field: EvidenceField) => `${field.recordLabel} · ${field.label}`;
const fields = (field?: EvidenceField) => field?.candidates.flatMap(candidate => candidate.citations) ?? [];

function defaultView(detail: EvidenceDetail, field?: EvidenceField): EvidenceSearch {
  if (!field) return { revision: detail.inputRevision };
  const source = fields(field).find(candidate => candidate.id === field.usedCitationId);
  if (source) return citationView(detail, field, source);
  return { revision: detail.inputRevision, field: field.id };
}

function citationView(detail: EvidenceDetail, field: EvidenceField, citation: EvidenceCitation): EvidenceSearch {
  const { evidence } = citation;
  return {
    revision: detail.inputRevision, field: field.id, citation: citation.id,
    document: evidence.original.documentId, version: evidence.original.versionId,
    page: 'page' in evidence ? evidence.page : undefined,
  };
}

function resolveView(detail: EvidenceDetail, view: EvidenceSearch) {
  const field = detail.fields.find(candidate => candidate.id === view.field);
  const citation = fields(field).find(candidate => candidate.id === view.citation);
  const document = detail.documents.find(doc => doc.id === view.document && doc.versionId === view.version);
  const invalid = !!(
    (view.revision && view.revision !== detail.inputRevision) ||
    (view.field && !field) || ((view.document || view.version) && !document) ||
    (view.citation && (!citation || !document || citation.evidence.original.documentId !== document.id ||
      citation.evidence.original.versionId !== document.versionId)) ||
    (view.page !== undefined && (!document?.pageCount || view.page > document.pageCount))
  );
  return { field, citation, document, invalid };
}

export default function EvidenceWorkspace({
  detail, search, jobs, refresh, navigateView, previous, next, canGoBack, canGoForward,
}: Props) {
  const [session, setSession] = useState<ReviewIdentity | null>(null);
  const [activeSelection, setActiveSelection] = useState<ReviewSelection | null>(null);
  const [automaticView, setAutomaticView] = useState<string | null>(null);
  const [streamConnected, setStreamConnected] = useState(false);
  const [followingEnded, setFollowingEnded] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const inbox = useDocumentInbox(() => { void refresh(); }, detail.id);
  const current = useRef({ detail, search, navigateView });
  current.current = { detail, search, navigateView };
  const nativeDetail = useRef<EvidenceDetail | null>(null);
  const targetView = useRef(keyOf(search));
  const lastNative = useRef<ReviewSelection | null>(null);
  const navigation = useRef<Promise<void>>(Promise.resolve());
  const sessionGeneration = useRef(0);

  useEffect(() => {
    if (session?.inputRevision === detail.inputRevision) nativeDetail.current = detail;
  }, [detail, session]);

  useEffect(() => {
    targetView.current = keyOf(search);
  }, [search]);

  function go(nextView: EvidenceSearch, automatic = false, generation?: number) {
    const key = keyOf(nextView);
    if (!automatic) setAutomaticView(null);
    if (targetView.current === key) return;
    targetView.current = key;
    navigation.current = navigation.current.catch(() => { }).then(async () => {
      if (generation !== undefined && generation !== sessionGeneration.current) return;
      const route = current.current;
      const replace = selectionFields.every(name => route.search[name] === undefined);
      if (keyOf(route.search) !== key) await route.navigateView(nextView, replace);
      if (automatic) setAutomaticView(key);
    }).catch(cause => setError(errorMessage(cause)));
  }

  const navigateRef = useRef(go);
  navigateRef.current = go;

  useEffect(() => {
    if (!session || followingEnded) return;
    const abort = new AbortController();
    const generation = ++sessionGeneration.current;
    let timer: number | undefined;
    let failureDelay = 250;
    lastNative.current = null;

    function disconnect() {
      setStreamConnected(false);
      setActiveSelection(null);
    }

    async function connect(scope: ReviewIdentity) {
      try {
        await streamReviewSelections(scope, abort.signal, event => {
          if (abort.signal.aborted || generation !== sessionGeneration.current) return;
          const before = lastNative.current;
          if (before && event.sequence < before.sequence) return;
          setStreamConnected(true);
          setActiveSelection(event);
          if (before && event.sequence === before.sequence) return;
          lastNative.current = event;
          failureDelay = 250;
          if (!event.bindingId || before?.bindingId === event.bindingId) return;
          const active = nativeDetail.current;
          if (!active || active.id !== event.returnId || active.inputRevision !== event.inputRevision) return;
          const field = active.fields.find(item => item.id === event.bindingId);
          if (field) navigateRef.current(defaultView(active, field), true, generation);
          else setActiveSelection({ ...event, status: 'unmapped', bindingId: null });
        });
      } catch (cause) {
        if (abort.signal.aborted) return;
        if (cause instanceof ApiError && [401, 403, 404, 409, 410].includes(cause.status)) {
          disconnect();
          setFollowingEnded(true);
          setMessage(cause.status === 409
            ? 'Evidence is already following this review in another window. You can browse sources here manually.'
            : 'Native evidence following ended. Close the native return, then release the reservation.');
          return;
        }
      }
      if (!abort.signal.aborted) {
        disconnect();
        timer = window.setTimeout(() => void connect(scope), failureDelay);
        failureDelay = Math.min(failureDelay * 2, 4000);
      }
    }

    void connect(session);
    return () => {
      ++sessionGeneration.current;
      abort.abort();
      clearTimeout(timer);
    };
  }, [session, followingEnded]);

  async function command(action: () => Promise<void>) {
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  function openReview() {
    void command(async () => {
      const result = await api.openReview(detail.id, detail.inputRevision);
      if (result.returnId !== detail.id || (result.status !== 'recovery_required' && result.inputRevision !== detail.inputRevision)) {
        throw new Error('The reservation does not match this return revision.');
      }
      if (result.status === 'queued') {
        setMessage('Review requested. The current writer must finish and release the native return.');
      } else if (result.reviewSessionId) {
        nativeDetail.current = detail;
        setFollowingEnded(result.status === 'recovery_required');
        setSession({ reviewSessionId: result.reviewSessionId, returnId: result.returnId, inputRevision: result.inputRevision });
        setMessage(result.status === 'recovery_required'
          ? 'The evidence session expired. Close the native return, then release the reservation.'
          : 'Use the separate Sigma window to check and make edits.');
      }
    });
  }

  const view = resolveView(detail, search);
  const { field, citation, document } = view;
  const suppressAutomatic = automaticView === keyOf(search) && (
    !streamConnected || activeSelection?.status !== 'selected' || activeSelection.bindingId !== field?.id
  );
  const anchored = suppressAutomatic ? undefined : citation;
  const active = activeSelection?.bindingId && nativeDetail.current?.fields.find(item => item.id === activeSelection.bindingId);
  const canMark = streamConnected && !followingEnded && session?.inputRevision === detail.inputRevision &&
    (!search.revision || search.revision === detail.inputRevision);
  const currentPage = search.page ?? (anchored && 'page' in anchored.evidence ? anchored.evidence.page : 1);
  const archive = !detail.reviewAvailable && detail.revisions.every(revision => revision.label === 'Archived intake');

  return (
    <main id="main-content" className="mx-auto w-full max-w-6xl space-y-4 px-5 py-8">
      <Link to="/clients" className="text-sm underline">Back to returns</Link>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{detail.clientName} · {detail.taxYear}</h1>
          <p className="text-sm text-muted-foreground">{archive ? 'Archived intake' : 'Draft evidence review'}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {session ? (
            <>
              <Button disabled={busy || !canMark} onClick={() => void command(async () => {
                await api.reviewDecision(session, detail.decisionVersion, detail.fieldVersion, { kind: 'mark_reviewed' });
                setMessage('Marked reviewed for this input revision. Nothing was filed.');
                await refresh();
              })}>Mark reviewed</Button>
              <Button variant="outline" disabled={busy} onClick={() => void command(async () => {
                await api.closeReview(session);
                setSession(null);
                setActiveSelection(null);
                setStreamConnected(false);
                setMessage('Native review released.');
              })}>Release review</Button>
            </>
          ) : (
            <Button disabled={busy || !detail.reviewAvailable || detail.inputRevision !== detail.latestRevision} onClick={openReview}>Open review</Button>
          )}
        </div>
      </header>
      {(message || detail.reviewMessage) && <p role="status" className="text-sm text-muted-foreground">{message || detail.reviewMessage}</p>}
      {error && <Alert variant="destructive" role="alert">{error}</Alert>}
      <div className="flex flex-wrap gap-2" aria-label="Evidence history">
        <Button variant="outline" size="sm" disabled={!canGoBack} onClick={() => { setAutomaticView(null); previous(); }}>Previous evidence</Button>
        <Button variant="outline" size="sm" disabled={!canGoForward} onClick={() => { setAutomaticView(null); next(); }}>Next evidence</Button>
      </div>
      <div className="grid gap-2 border-y border-border py-3 text-sm md:grid-cols-2" aria-live="polite">
        <p><strong>Viewing:</strong> {view.invalid ? 'Unavailable evidence' : field ? label(field) : document?.title ?? 'No field selected'}</p>
        <p><strong>Active in Sigma:</strong> {active ? label(active) : session ? 'No linked citation · choose evidence manually' : 'No active review'}</p>
      </div>
      <section className="flex flex-wrap items-center justify-between gap-3 text-sm" aria-label="Preparation status">
        <div>
          {jobs.length ? jobs.map(job => (
            <div key={job.id}>
              <strong>{job.inputRevision === detail.inputRevision ? 'This revision' : 'Another revision'}</strong>
              {' · '}{job.status.replaceAll('_', ' ')}
              {job.issues.length > 0 && <p className="text-muted-foreground">{job.issues.join(' · ')}</p>}
            </div>
          )) : <p className="text-muted-foreground">No preparation job for this return.</p>}
        </div>
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void command(refresh)}>Refresh status</Button>
      </section>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="grid gap-1 text-sm font-medium">Input revision
          <NativeSelect value={detail.inputRevision} onChange={event => go({ revision: event.target.value })}>
            {detail.revisions.map(revision => <option key={revision.id} value={revision.id}>{revision.label}</option>)}
          </NativeSelect>
        </label>
        <label className="grid gap-1 text-sm font-medium">Field evidence
          <NativeSelect value={field?.id ?? ''} onChange={event => go(defaultView(detail, detail.fields.find(item => item.id === event.target.value)))}>
            <option value="">Choose a field</option>
            {detail.fields.map(item => <option key={item.id} value={item.id}>{label(item)}</option>)}
          </NativeSelect>
        </label>
        <label className="grid gap-1 text-sm font-medium">Original document
          <NativeSelect value={document ? sourceKey(document) : ''} onChange={event => {
            const selected = detail.documents.find(doc => sourceKey(doc) === event.target.value);
            go({
              revision: detail.inputRevision, field: field?.id, document: selected?.id, version: selected?.versionId,
              page: selected?.pageCount ? 1 : undefined
            });
          }}>
            <option value="">Choose an original</option>
            {detail.documents.map(doc => <option key={sourceKey(doc)} value={sourceKey(doc)}>{doc.title}</option>)}
          </NativeSelect>
        </label>
      </div>
      {view.invalid ? (
        <Alert variant="destructive" role="alert">This evidence location is unavailable for the selected return revision.</Alert>
      ) : (
        <>
          {document && !suppressAutomatic ? (
            <section className="min-w-0 overflow-hidden rounded-lg border border-border" aria-label="Source document">
              <div className="border-b border-border p-3">
                <h2 className="font-semibold">{document.title}</h2>
                {anchored?.evidence.precision === 'document' && <p className="text-sm text-muted-foreground">Page not verified.</p>}
                {anchored?.evidence.precision === 'page' && <p className="text-sm text-muted-foreground">Page {anchored.evidence.page} verified; there is no exact highlight.</p>}
              </div>
              {document.mediaType === 'text/plain' ? (
                <TextDocumentViewer key={`${detail.inputRevision}:${sourceKey(document)}`} returnId={detail.id}
                  document={document} revision={detail.inputRevision}
                  citation={anchored?.evidence.precision === 'lines' ? anchored.evidence : null}
                  citationLabel={anchored?.label} />
              ) : (
                <DocumentViewer key={`${detail.inputRevision}:${sourceKey(document)}`} returnId={detail.id}
                  document={document} revision={detail.inputRevision}
                  citation={anchored?.evidence.precision === 'region' ? anchored.evidence : null}
                  citationLabel={anchored?.label} page={currentPage}
                  onChoosePage={page => go({ ...search, revision: detail.inputRevision, page })} />
              )}
            </section>
          ) : <p className="text-sm text-muted-foreground">No linked citation is selected. Choose a field or an original.</p>}
          {field && <FieldSources field={field} select={source => go(citationView(detail, field, source))} />}
        </>
      )}
      {!archive && <DocumentInbox inbox={inbox} />}
    </main>
  );
}

function FieldSources({ field, select }: { field: EvidenceField; select: (source: EvidenceCitation) => void }) {
  return (
    <section className="space-y-2 rounded-lg border border-border p-4" aria-label="Field provenance">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-semibold">{label(field)}</h2>
        {field.provenance === 'preparer' && <Badge variant="secondary">Entered by preparer</Badge>}
        <Badge variant="secondary">{field.provenance === 'archived' ? 'Archived value'
          : field.status === 'needs_client_input' ? 'Needs client input'
          : field.status === 'conflict' ? 'Conflict'
          : field.status === 'verified' && field.savedValue !== null ? 'Verified after reopening'
          : field.provenance === 'source' ? 'Source evidence only' : 'Native value unverified'}</Badge>
      </div>
      {field.status === 'conflict' ? (
        <dl className="grid gap-2 text-sm sm:grid-cols-3">
          <div><dt>Previous source</dt><dd>{field.priorSourceValue ?? 'Unknown'}</dd></div>
          <div><dt>Saved native value</dt><dd>{field.savedValue ?? 'Unknown'}</dd></div>
          <div><dt>New proposal</dt><dd>{field.proposedValue ?? 'Unavailable'}</dd></div>
        </dl>
      ) : field.status === 'verified' && field.savedValue !== null ? (
        <p className="text-sm">Reopened native value: {field.savedValue || 'Blank'}</p>
      ) : field.value !== null && <p className="text-sm">{field.value || 'Blank'}</p>}
      {field.derivation && <p className="text-sm text-muted-foreground">{field.derivation}</p>}
      {field.candidates.map(candidate => (
        <div key={candidate.id} className="flex flex-wrap items-center gap-2 border-t border-border pt-2 text-sm">
          <span>{candidate.label}{candidate.value !== null ? `: ${candidate.value}` : ''}</span>
          {candidate.citations.map(source => (
            <Button key={source.id} variant="outline" size="sm" onClick={() => select(source)}>
              {source.label}{source.id === field.usedCitationId
                ? field.savedValue !== null && field.status === 'verified' ? ' · Used' : ' · Selected source'
                : ''}
            </Button>
          ))}
        </div>
      ))}
    </section>
  );
}

import { useCallback, useEffect, useRef, useState } from 'react';
import { getRouteApi, Link } from '@tanstack/react-router';
import {
  api,
  errorMessage,
  fieldValue,
  type BrowserState,
  type Citation,
  type Field,
  type Job,
  type ReturnDetail,
  type ReturnSummary,
} from '../../lib/api';
import BrowserSurface from '../automation/BrowserSurface';
import JobPanel from '../automation/JobPanel';
import { Progress } from '@/components/ui/progress';
import SourcePanel from '../evidence/SourcePanel';
import { useReturnJobs } from '../../hooks/useReturnJobs';
import WorkspaceTools from './WorkspaceTools';
import ReturnExtraction from '../intake/ReturnExtraction';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import FieldEditors from './FieldEditors';
import FieldEvidence from './FieldEvidence';
import BrowserSubmit from './BrowserSubmit';
import { canonicalFieldValue, useFieldDrafts } from './useFieldDrafts';

const workspaceRoute = getRouteApi('/clients_/$clientId/returns/$taxYear');
interface Props {
  initialDetail: ReturnDetail;
  years: ReturnSummary[];
}
interface SourceChoice {
  fieldId: string;
  documentId: string;
  citationId: string | null;
}

interface BrowserActionResult {
  saved: ReturnDetail;
  state: BrowserState;
}

export default function ReviewWorkspace({ initialDetail, years }: Props) {
  const params = workspaceRoute.useParams();
  const search = workspaceRoute.useSearch();
  const navigate = workspaceRoute.useNavigate();

  const [detail, setDetail] = useState(initialDetail);
  const [observation, setObservation] = useState<BrowserState | null>(null);
  const [sourceChoice, setSourceChoice] = useState<SourceChoice | null>(null);
  const initialFieldId = useRef(initialDetail.activeFieldId);
  const drafts = useFieldDrafts(detail.fields);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(0);
  const [error, setError] = useState('');
  const [browserError, setBrowserError] = useState('');
  const [notice, setNotice] = useState('');

  const busyRef = useRef(false);
  const queue = useRef<Promise<void>>(Promise.resolve());
  const commandEpoch = useRef(0);
  const lifetime = useRef<AbortController | null>(null);
  const observationRef = useRef<BrowserState | null>(null);

  const observe = useCallback((state: BrowserState) => {
    const previous = observationRef.current;
    if (
      previous?.jobId === state.jobId &&
      previous?.observedAt &&
      state.observedAt &&
      state.observedAt < previous.observedAt
    )
      return;
    observationRef.current = state;
    setObservation(state);
  }, []);
  const jobs = useReturnJobs(detail.id);

  const orderedJobs = [...jobs.jobs].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  const live = jobs.active ?? orderedJobs.find(job => job.browserAvailable);
  const monitored = live ?? orderedJobs.find(job => job.target === 'intake-form');
  const liveRef = useRef(live);
  liveRef.current = live;

  const sourceWritesDisabled =
    busy || !jobs.ready || !!jobs.active || jobs.busy || !!live?.browserAvailable || pending > 0;
  const interactive =
    !!live?.browserAvailable && live.target === 'intake-form' && !jobs.active && !jobs.busy;

  const desiredFieldId = search.field ?? initialFieldId.current;
  const field = [...detail.fields, ...detail.legacyFields].find(item => item.id === desiredFieldId);
  const legacy = !!field && !detail.fields.some(item => item.id === field.id);

  const evidence = new Map<string, Citation>();
  for (const item of field?.citations ?? []) evidence.set(item.id, item);
  for (const candidate of field?.candidates ?? [])
    for (const item of candidate.citations) evidence.set(item.id, item);

  const choice = sourceChoice?.fieldId === field?.id ? sourceChoice : null;
  const selectedCandidate = field?.candidates.find(item => item.id === field.selectedCandidateId);
  const defaultCandidate = selectedCandidate ?? field?.candidates[0];
  const citation = choice
    ? (evidence.get(choice.citationId ?? '') ?? null)
    : (field?.citations[0] ?? defaultCandidate?.citations[0] ?? null);
  const selectedDocument =
    detail.documents.find(
      item => item.id === (choice?.documentId ?? citation?.documentId ?? defaultCandidate?.documentId),
    ) ?? detail.documents[0];

  const currentObservation = observation?.jobId === monitored?.id ? observation : null;
  const submitted = currentObservation?.submitted ?? monitored?.submitted ?? false;
  const intakeId = currentObservation?.intakeId ?? monitored?.intakeId;
  const issues = [
    ...new Set([...(monitored?.issues ?? []), ...(currentObservation?.issues ?? [])]),
  ];
  const populatedFields = detail.fields.filter(item => item.value !== null && item.value !== '').length;

  const apply = useCallback(
    (updated: ReturnDetail) => {
      if (
        updated.id !== initialDetail.id ||
        updated.clientId !== params.clientId ||
        String(updated.taxYear) !== params.taxYear
      )
        throw new Error(
          'The server returned a different client/year. No workspace state was applied.',
        );
      setDetail(current => (updated.revision >= current.revision ? updated : current));
      return updated;
    },
    [initialDetail.id, params.clientId, params.taxYear],
  );

  useEffect(() => {
    apply(initialDetail);
  }, [initialDetail, apply]);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => {
      controller.abort();
      commandEpoch.current += 1;
    };
  }, [detail.id]);

  useEffect(() => {
    if (!monitored || monitored.target !== 'intake-form') return;
    const controller = new AbortController();
    let timer: number | undefined;

    async function poll() {
      try {
        const [state, saved] = await Promise.all([
          api.browserState(detail.id, controller.signal),
          api.detail(detail.id, controller.signal),
        ]);
        if (controller.signal.aborted) return;
        observe(state);
        apply(saved);
        setBrowserError('');
      } catch (reason) {
        if (!controller.signal.aborted) setBrowserError(errorMessage(reason));
      }

      if (!controller.signal.aborted && (monitored!.browserAvailable || !!jobs.active))
        timer = window.setTimeout(() => void poll(), 700);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [detail.id, monitored?.id, monitored?.browserAvailable, !!jobs.active, apply, observe]);

  async function refresh() {
    setError('');
    try {
      apply(await api.detail(detail.id));
      await jobs.refresh();
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }

  async function awaitAction(job: Job, signal: AbortSignal) {
    jobs.accept(job);
    const deadline = Date.now() + 30000;

    while (job.actionPending && !signal.aborted) {
      if (Date.now() > deadline)
        throw new Error(
          'Browser action is still pending. Its outcome is not yet known; inspect the real browser before trying again.',
        );
      await new Promise(resolve => window.setTimeout(resolve, 100));
      if (signal.aborted) return;
      const current = (await api.jobs(detail.id, signal)).find(item => item.id === job.id);
      if (!current) throw new Error('The browser session is no longer available.');
      job = current;
      jobs.accept(job);
    }

    if (signal.aborted) return;
    if (job.actionError) throw new Error(job.actionError);
    if (!job.browserAvailable)
      throw new Error('The browser closed before the action could be confirmed.');

    const [saved, state] = await Promise.all([
      api.detail(detail.id, signal),
      api.browserState(detail.id, signal),
    ]);
    if (signal.aborted) return;
    apply(saved);
    observe(state);
    return { saved, state };
  }

  function enqueue(
    operation: (job: Job, signal: AbortSignal) => Promise<void>,
    onFailure?: (message: string) => void,
  ) {
    const epoch = commandEpoch.current;
    const signal = lifetime.current?.signal;
    if (!signal || signal.aborted) return;

    setPending(value => value + 1);
    setError('');
    setNotice('');

    queue.current = queue.current
      .then(async () => {
        if (epoch !== commandEpoch.current || signal.aborted) return;
        const job = liveRef.current;
        if (!job?.browserAvailable || job.target !== 'intake-form')
          throw new Error('The actual intake browser is not available.');
        await operation(job, signal);
      })
      .catch(reason => {
        if (signal.aborted) return;

        commandEpoch.current += 1;
        onFailure?.(errorMessage(reason));
        setError(`${errorMessage(reason)} Remaining queued browser actions were not sent.`);
      })
      .finally(() => {
        if (!signal.aborted) setPending(value => Math.max(0, value - 1));
      });
  }

  function focusField(fieldId: string) {
    setSourceChoice(null);
    if (fieldId !== desiredFieldId)
      void navigate({ search: { field: fieldId }, replace: true, resetScroll: false });
  }

  function viewCitation(item: Citation) {
    if (field)
      setSourceChoice({ fieldId: field.id, documentId: item.documentId, citationId: item.id });
  }

  function selectDocument(documentId: string) {
    if (field) setSourceChoice({ fieldId: field.id, documentId, citationId: null });
  }

  function confirmField(item: Field, expected: string, result: BrowserActionResult | undefined, version: number) {
    if (!result) return;
    const observed = result.state.values[item.id];
    if (observed === undefined || canonicalFieldValue(item, observed) !== expected)
      throw new Error('The browser readback did not match the requested value. The field is not confirmed.');
    const saved = result.saved.fields.find(field => field.id === item.id);
    if (!saved || canonicalFieldValue(item, saved.value) !== expected)
      throw new Error('The browser value was not recorded successfully. Reload the observed state before trying again.');
    drafts.confirm(item, saved.value ?? '', version);
  }

  function applyField(item: Field) {
    if (!interactive || pending || live?.actionPending || submitted) return;
    const snapshot = drafts.begin(item);

    enqueue(async (job, signal) => {
      const value = canonicalFieldValue(item, snapshot.value);
      const saved = apply(await api.detail(detail.id, signal));
      const result = await awaitAction(await api.applyField(detail.id, job.id, {
        expectedRevision: saved.revision,
        fieldId: item.id,
        value,
        reason: snapshot.reason,
      }), signal);
      if (!signal.aborted) confirmField(item, value, result, snapshot.version);
    }, message => drafts.fail(item, message));
  }

  function chooseSource(item: Field, candidateId: string) {
    if (!interactive || pending || live?.actionPending || legacy || submitted) return;
    const candidate = item.candidates.find(value => value.id === candidateId);
    if (!candidate) return;
    const snapshot = drafts.begin(item);

    enqueue(async (job, signal) => {
      const saved = apply(await api.detail(detail.id, signal));
      const result = await awaitAction(await api.chooseSource(detail.id, job.id, {
        expectedRevision: saved.revision,
        fieldId: item.id,
        candidateId,
        reason: snapshot.reason,
      }), signal);
      if (!signal.aborted)
        confirmField(item, canonicalFieldValue(item, candidate.value), result, snapshot.version);
    }, message => drafts.fail(item, message));
  }

  function submitBrowser() {
    if (!interactive || pending || live?.actionPending || drafts.unsavedCount || submitted) return;
    enqueue(async (job, signal) => {
      const saved = apply(await api.detail(detail.id, signal));
      const result = await awaitAction(await api.submitBrowser(detail.id, job.id, {
        expectedRevision: saved.revision,
      }), signal);
      if (result && !result.state.submitted)
        throw new Error('The original form did not confirm submission. Review the browser issues before trying again.');
    });
  }

  async function upload(file: File) {
    if (sourceWritesDisabled || busyRef.current) return false;

    busyRef.current = true;
    setBusy(true);
    setError('');

    try {
      apply(await api.upload(detail.id, detail.revision, file));
      setNotice('Document uploaded. Extract documents to identify its intake values.');
      return true;
    } catch (reason) {
      setError(errorMessage(reason));
      return false;
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  async function startJob() {
    if (sourceWritesDisabled || live || busyRef.current) return;

    busyRef.current = true;
    setBusy(true);
    setError('');

    try {
      const saved = apply(await api.detail(detail.id));
      await jobs.start(saved.revision);
      setNotice('Opening and filling the intake form.');
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto max-w-[1800px] px-3.5 pb-[30px] pt-[18px] min-[661px]:px-[3%] min-[661px]:pb-[42px] min-[661px]:pt-[22px]" id="main-content">
      <div className="mb-[21px] flex flex-wrap items-center gap-[7px] text-[10px] text-[#819083] min-[661px]:mb-[25px] min-[661px]:gap-3 min-[661px]:text-[11px]">
        <Link className="rounded-sm text-primary hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" to="/clients" search={{}}>
          ← Document inbox / clients
        </Link>
        <span>/</span>
        <span className="max-[660px]:max-w-[42%] max-[660px]:truncate">
          {detail.clientName} · {detail.taxYear}
        </span>
        <span className="ml-auto text-[9px] text-[#667a6c] min-[661px]:text-[10px]" role="status">
          Local evidence · revision {detail.revision}
        </span>
      </div>
      <div className="flex flex-col items-stretch justify-between gap-[22px] px-[5px] min-[661px]:flex-row min-[661px]:items-end min-[661px]:gap-[30px] min-[661px]:px-0">
        <div>
          <p className="mb-[7px] text-[9px] font-bold tracking-[1.45px] text-muted-foreground min-[661px]:text-[10px]">
            {detail.returnType.toUpperCase()} CLIENT · TAX YEAR {detail.taxYear}
          </p>
          <h1 className="text-[28px] font-semibold leading-[1.16] tracking-[-1px] min-[661px]:text-[29px]">
            {detail.clientName} <span className="ml-2 inline-block rounded-md border border-[#d5e1ce] bg-[#eaf1e5] px-2 py-1 align-middle text-[17px] tracking-[-.3px] text-[#637d52]">{detail.taxYear}</span>
          </h1>
          <p className="mt-[9px] text-[10px] text-muted-foreground min-[661px]:text-[11px]">Original client intake form · TaxSlayer not connected</p>
          <nav className="mt-3.5 flex flex-wrap gap-2" aria-label="Client tax years">
            {years.map(year => (
              <Link
                className="rounded-[5px] border border-border px-2.5 py-1 text-primary no-underline hover:bg-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring aria-[current=page]:border-[#92ac9a] aria-[current=page]:bg-[#edf4ef] aria-[current=page]:font-semibold"
                key={year.id}
                to="/clients/$clientId/returns/$taxYear"
                params={{ clientId: year.clientId, taxYear: String(year.taxYear) }}
                search={{}}
                aria-current={year.taxYear === detail.taxYear ? 'page' : undefined}
              >
                {year.taxYear}
              </Link>
            ))}
          </nav>
        </div>
        <div className="w-full min-[661px]:w-[280px]">
          <div className="mb-[9px] flex items-baseline justify-between gap-2.5 text-[11px] text-muted-foreground">
            <span>Fields populated</span>
            <strong className="text-xs text-foreground">
              {populatedFields}{' '}
              <span className="font-normal text-muted-foreground">/ {detail.fields.length}</span>
            </strong>
          </div>
          <Progress
            className="h-[5px]"
            value={populatedFields}
            max={detail.fields.length || 1}
            aria-label={`${populatedFields} of ${detail.fields.length} fields populated`}
          />
          <div className="mt-2.5 flex gap-5 text-[10px] text-[#7d897e] min-[661px]:gap-[18px]">
            <span>{detail.fields.filter(item => item.conflict).length} conflicts</span>
            <span>{detail.documents.length} documents</span>
          </div>
          <p className="mt-2.5 text-[10px] leading-[1.7] text-[#88937f]">Locally recorded facts · not a submission</p>
        </div>
      </div>
      <div className="mb-4 mt-[18px] min-h-[22px] px-[5px] has-[[role=alert]]:sticky has-[[role=alert]]:top-3 has-[[role=alert]]:z-10 min-[661px]:mt-[19px] min-[661px]:px-0" aria-live="polite">
        {error ? (
          <Alert variant="destructive" role="alert">
            {error}
            <Button variant="link" className="h-auto p-0 max-[660px]:mt-2 max-[660px]:block" disabled={busy} onClick={() => void refresh()}>
              Reload observed state
            </Button>
          </Alert>
        ) : (
          notice && <p className="flex items-start gap-[7px] text-[10px] text-[#77867a] min-[661px]:items-center min-[661px]:text-[11px]">{notice}</p>
        )}
      </div>
      <JobPanel state={jobs} disabled={sourceWritesDisabled} onStart={() => void startJob()} />
      {monitored && (
        <div
          className={submitted && intakeId ? 'mt-[18px] rounded-lg border border-[#9bb99c] bg-[#eef6ed] p-4' : 'mt-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-[#f7faf6] px-4 py-3.5'}
          role="status"
        >
          {submitted && intakeId ? (
            <>
              Submitted · Intake ID <strong>{intakeId}</strong>
            </>
          ) : (
            'Not submitted · Review the editable fields and submit explicitly.'
          )}
        </div>
      )}
      {browserError && (
        <Alert className="mt-3" variant="destructive" role="alert">
          Browser observation: {browserError}
        </Alert>
      )}
      {monitored?.actionError && (
        <Alert className="mt-3" variant="destructive" role="alert">
          Browser action: {monitored.actionError}
        </Alert>
      )}
      {monitored?.error && (
        <Alert className="mt-3" variant="destructive" role="alert">
          {monitored.error}
        </Alert>
      )}
      {!!issues.length && (
        <details className="mt-5 rounded-lg border border-border bg-card p-4" open>
          <summary className="cursor-pointer rounded-sm focus-visible:outline-2 focus-visible:outline-ring">Required information / browser issues ({issues.length})</summary>
          <ul className="my-3 list-disc pl-6 text-xs">
            {issues.map(issue => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </details>
      )}
      <div className="mt-5 grid items-start gap-4 min-[1100px]:grid-cols-[minmax(340px,1fr)_minmax(0,1.25fr)_minmax(300px,1fr)]">
        <div className="w-full min-w-0">
          <FieldEditors
            fields={detail.fields}
            selectedFieldId={field?.id}
            disabled={!interactive || pending > 0 || !!live?.actionPending || submitted}
            getDraft={drafts.get}
            onEdit={drafts.edit}
            onApply={applyField}
            onDiscard={drafts.discard}
            onSelect={focusField}
          />
        </div>
        <div className="w-full min-w-0 min-[1100px]:sticky min-[1100px]:top-4">
          {monitored ? (
            <BrowserSurface
              job={monitored}
              pending={pending > 0}
            />
          ) : (
            <section className="overflow-hidden rounded-lg border border-border bg-[#f7faf6]">
              <h2 className="m-[18px] text-lg font-semibold">Actual intake form</h2>
              <div className="grid min-h-[180px] place-items-center p-6 text-muted-foreground">
                Open and fill the form above to see the actual controlled browser here. The
                editable fields stay local until you apply them.
              </div>
            </section>
          )}
          <BrowserSubmit
            disabled={!interactive || pending > 0 || !!live?.actionPending}
            submitted={submitted}
            unsavedCount={drafts.unsavedCount}
            onSubmit={submitBrowser}
          />
        </div>
        <div className="w-full min-w-0 overflow-auto overscroll-contain rounded-lg border border-border [scrollbar-width:thin] min-[1100px]:sticky min-[1100px]:top-4 min-[1100px]:max-h-[calc(100vh-32px)]">
          <FieldEvidence
            fields={detail.fields}
            legacyFields={detail.legacyFields}
            documents={detail.documents}
            field={field}
            legacy={legacy}
            disabled={!interactive || pending > 0 || !!live?.actionPending || submitted}
            onSelect={focusField}
            onChooseSource={chooseSource}
            onViewCitation={viewCitation}
            onSelectDocument={selectDocument}
          />
          <SourcePanel
            returnId={detail.id}
            documents={detail.documents}
            selectedDocument={selectedDocument}
            citation={citation}
            field={field}
            onSelectDocument={selectDocument}
          />
        </div>
      </div>
      {!!detail.legacyFields.length && (
        <details className="mt-5 rounded-lg border border-border bg-card p-4">
          <summary className="cursor-pointer rounded-sm focus-visible:outline-2 focus-visible:outline-ring">
            Preserved historical worksheet facts ({detail.legacyFields.length}) · read only
          </summary>
          <dl>
            {detail.legacyFields.map(item => (
              <div className="pt-3" key={item.id}>
                <dt>
                  <Button variant="link" className="h-auto whitespace-normal p-0 text-left text-xs" onClick={() => focusField(item.id)}>
                    {item.label} · view evidence
                  </Button>
                </dt>
                <dd className="mt-1.5 [overflow-wrap:anywhere]">{fieldValue(item)}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
      <WorkspaceTools
        detail={detail}
        disabled={sourceWritesDisabled}
        onViewDocument={documentId => {
          selectDocument(documentId);
          document.getElementById('source-panel-heading')?.scrollIntoView({ block: 'nearest' });
        }}
        onUpload={upload}
      />
      <ReturnExtraction detail={detail} disabled={sourceWritesDisabled} onUpdated={apply} />
    </main>
  );
}

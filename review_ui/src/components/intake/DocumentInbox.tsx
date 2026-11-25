import { useState } from 'react';
import { Link } from '@tanstack/react-router';
import type { DocumentInboxState } from '@/hooks/useDocumentInbox';
import type { Batch, BatchFile } from '@/lib/apiSchemas';
import { errorMessage } from '@/lib/api';
import { practiceDestination } from '@/lib/practiceDraft';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { NativeSelect } from '@/components/ui/native-select';
import ClientPicker, { hasAssignment, type ClientSelection } from './ClientPicker';
import PracticeStatus from './PracticeStatus';

const analysisLabels = {
  pending: 'Local analysis pending',
  filed: 'Categorized',
  needs_review: 'Needs review',
  failed: 'Local analysis failed',
} as const;

function UploadAnalysis({ analysis }: { analysis: NonNullable<BatchFile['analysis']> }) {
  return (
    <div className="space-y-1">
      <p>
        <span className="font-medium">{analysisLabels[analysis.status]}</span>
        {analysis.category && <> · Category: {analysis.category}</>}
      </p>
      {analysis.message && <p className="text-muted-foreground">{analysis.message}</p>}
      {analysis.forms.length > 0 && <p>Forms reported or mentioned: {analysis.forms.join(', ')}</p>}
      {analysis.facts.length > 0 && (
        <details>
          <summary className="cursor-pointer">Source facts ({analysis.facts.length})</summary>
          <ul className="mt-1 space-y-2 pl-4">
            {analysis.facts.map((fact, index) => (
              <li key={index}>
                <p><strong>{fact.label}:</strong> <span className="whitespace-pre-wrap">{fact.value}</span></p>
                <p className="text-muted-foreground">
                  {fact.page === null ? `Line ${fact.line}` : `Page ${fact.page}, line ${fact.line}`}
                </p>
                <blockquote className="whitespace-pre-wrap border-l border-border pl-2">{fact.quote}</blockquote>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function isSaved(file: BatchFile) {
  return file.state === 'uploaded' && file.analysis?.status !== 'pending';
}

function selectFiles(batches: DocumentInboxState['batches'], keep: (file: BatchFile) => boolean) {
  return batches.flatMap(batch => {
    const files = batch.files.filter(keep);
    return files.length ? [{ ...batch, files }] : [];
  });
}

export default function DocumentInbox({ inbox }: { inbox: DocumentInboxState }) {
  const [selection, setSelection] = useState<ClientSelection>({ clientId: '', year: '' });
  const [name, setName] = useState('');
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState('');
  const chooseClient = !!inbox.settings?.requiresClient && !inbox.returnScoped;
  const uploadEnabled = !!inbox.settings?.enabled && (!chooseClient || !!inbox.assignment);
  const batches = chooseClient
    ? selectFiles(inbox.batches, file => file.state !== 'excluded' && !isSaved(file))
    : inbox.batches;

  function select(next: ClientSelection) {
    setSelection(next);
    inbox.setAssignment(hasAssignment(next));
  }

  async function createClient() {
    const clientName = name.trim();
    if (creating || !clientName) return;
    setCreating(true);
    setCreateError('');
    try {
      const client = await inbox.createClient(clientName);
      select({ clientId: client.clientId, year: selection.year });
      setName('');
    } catch (cause) {
      setCreateError(errorMessage(cause));
    } finally {
      setCreating(false);
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-border bg-card p-4" aria-label="Document inbox">
      <h2 className="text-lg font-semibold">Document inbox</h2>
      {inbox.returnScoped && (
        <p className="text-sm text-muted-foreground">
          Only committed files verified for this return appear here. Manage pending files in the general document inbox.
        </p>
      )}
      {chooseClient && (
        <>
          {inbox.settings?.canCreateClients && (
            <form className="flex flex-wrap items-end gap-2" onSubmit={event => {
              event.preventDefault();
              void createClient();
            }}>
              <label className="grid gap-1 text-sm">
                New client name
                <Input value={name} maxLength={160} onChange={event => setName(event.target.value)} />
              </label>
              <Button type="submit" variant="outline" disabled={creating || !name.trim()}>Add client</Button>
            </form>
          )}
          {createError && <Alert variant="destructive" role="alert">{createError}</Alert>}
          <fieldset disabled={creating}>
            <legend className="mb-2 text-sm font-medium">Assign this upload</legend>
            <ClientPicker clients={inbox.clients} selection={selection} onChange={select} />
          </fieldset>
          <p className="text-sm text-muted-foreground">Choose a client and tax year before dropping files. Use a separate drop for each client and year.</p>
        </>
      )}
      {inbox.settings?.approvalLabel && (
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={inbox.approved} disabled={!uploadEnabled}
            onChange={event => inbox.setApproved(event.target.checked)} />
          <span>{inbox.settings.approvalLabel}</span>
        </label>
      )}
      <div {...inbox.getRootProps({
        className: `space-y-2 rounded-md border border-dashed p-5 text-sm ${inbox.isDragActive ? 'bg-accent' : ''}`,
      })}>
        <input {...inbox.getInputProps({ 'aria-label': 'Select PDF or TXT files' })} />
        <p>Drop selectable-text PDF or UTF-8 TXT files here. Each valid upload can proceed separately.</p>
        <Button type="button" onClick={inbox.open} disabled={!uploadEnabled}>Select files</Button>
      </div>
      {inbox.settings?.message && <p className="text-sm text-muted-foreground" role="status">{inbox.settings.message}</p>}
      {inbox.notice && <p className="text-sm text-muted-foreground" role="status">{inbox.notice}</p>}
      {inbox.returnScoped && (
        <Button type="button" variant="outline" size="sm" onClick={() => void inbox.refreshBatches()}>
          Refresh verified uploads
        </Button>
      )}
      {inbox.error && <Alert variant="destructive" role="alert">{inbox.error}</Alert>}
      {inbox.notDeclared.filter(drop => inbox.errors[drop.batchId]).map(drop => (
        <Alert key={drop.batchId} variant="destructive">
          {inbox.errors[drop.batchId]}{' '}
          <Button type="button" size="sm" variant="outline" onClick={() => void inbox.declare(drop)}>
            Retry creating upload
          </Button>
        </Alert>
      ))}
      <UploadList inbox={inbox} batches={batches} />
    </section>
  );
}

export function SavedDocumentWorkspaces({ inbox, query, year, openPractice }: {
  inbox: DocumentInboxState;
  query: string;
  year?: number;
  openPractice: (batchId: string, file: BatchFile) => void;
}) {
  const [category, setCategory] = useState('');
  if (!inbox.settings?.requiresClient) return null;

  const saved = selectFiles(inbox.batches, isSaved);
  const names = new Map(inbox.clients.map(client => [client.clientId, client.name]));
  const categories = [...new Set(saved.flatMap(batch => batch.files.flatMap(
    file => file.analysis?.category ? [file.analysis.category] : [],
  )))].sort((a, b) => a.localeCompare(b));
  const selectedCategory = categories.includes(category) ? category : '';
  const groups = new Map<string, { title: string; taxYear: number; batches: Map<string, Batch> }>();
  const unassigned: Batch[] = [];

  for (const batch of saved) {
    for (const file of batch.files) {
      const assignment = file.assignment;
      const title = assignment ? names.get(assignment.clientId) ?? assignment.clientId : '';
      if (year !== undefined && assignment?.taxYear !== year) continue;
      if (selectedCategory && file.analysis?.category !== selectedCategory) continue;
      if (query && ![title, assignment?.clientId ?? '', file.filename, file.analysis?.category ?? '',
        ...(file.analysis?.forms ?? [])].some(value => value.toLocaleLowerCase().includes(query))) continue;

      if (!assignment) {
        unassigned.push({ ...batch, files: [file] });
        continue;
      }
      const { clientId, taxYear } = assignment;
      const key = `${clientId}/${taxYear}`;
      let group = groups.get(key);
      if (!group) {
        group = { title, taxYear, batches: new Map() };
        groups.set(key, group);
      }
      const groupedBatch = group.batches.get(batch.batchId);
      if (groupedBatch) groupedBatch.files.push(file);
      else group.batches.set(batch.batchId, { ...batch, files: [file] });
    }
  }
  const workspaces = [...groups].sort(([, a], [, b]) => a.title.localeCompare(b.title) || b.taxYear - a.taxYear);

  return (
    <section className="mb-5 space-y-3" aria-label="Saved document workspaces">
      <h3 className="font-semibold">Saved documents</h3>
      {categories.length > 0 && (
        <label className="flex flex-wrap items-center gap-2 text-sm">
          Category
          <NativeSelect aria-label="Document category" value={selectedCategory} onChange={event => setCategory(event.target.value)}>
            <option value="">All categories</option>
            {categories.map(value => <option key={value} value={value}>{value}</option>)}
          </NativeSelect>
        </label>
      )}
      {workspaces.map(([key, workspace]) => (
        <section key={key} className="space-y-2 rounded-md border border-border p-3">
          <h4 className="font-medium">{workspace.title} · {workspace.taxYear}</h4>
          <UploadList inbox={inbox} batches={[...workspace.batches.values()]} openPractice={openPractice} saved />
        </section>
      ))}
      {unassigned.length > 0 && (
        <section className="space-y-2" aria-label="Unassigned documents">
          <h4 className="font-medium">Unassigned documents</h4>
          <p className="text-sm text-muted-foreground">
            These files were uploaded before client assignment. Assign each file without uploading it again.
          </p>
          <UploadList inbox={inbox} batches={unassigned} openPractice={openPractice} saved />
        </section>
      )}
      {!workspaces.length && !unassigned.length && (
        <p className="text-sm text-muted-foreground">
          {saved.length ? 'No saved documents match these filters.' : 'No saved documents yet.'}
        </p>
      )}
    </section>
  );
}

function EarlierUploadAssignment({ inbox, batchId, slot }: {
  inbox: DocumentInboxState;
  batchId: string;
  slot: string;
}) {
  const [selection, setSelection] = useState<ClientSelection>({ clientId: '', year: '' });
  const assignment = hasAssignment(selection);
  return (
    <form className="flex flex-wrap items-end gap-2" onSubmit={event => {
      event.preventDefault();
      if (assignment) void inbox.assignSlot(batchId, slot, assignment);
    }}>
      <ClientPicker clients={inbox.clients} selection={selection} onChange={setSelection} />
      <Button type="submit" size="sm" variant="outline"
        disabled={!assignment || inbox.busy.has(inbox.slotKey(batchId, slot))}>Assign document</Button>
    </form>
  );
}

function UploadList({ inbox, batches, openPractice, saved = false }: {
  inbox: DocumentInboxState;
  batches: DocumentInboxState['batches'];
  openPractice?: (batchId: string, file: BatchFile) => void;
  saved?: boolean;
}) {
  if (!batches.length) return null;
  return (
    <ul className="space-y-3" aria-live="polite">
      {batches.map(drop => (
        <li key={`${drop.batchId}/${drop.files[0]?.slot}`} className="rounded-md border border-border p-3">
          {!saved && (
            <p className="text-sm font-medium">
              {inbox.returnScoped ? 'Verified for this return' : drop.state === 'incomplete' ? 'Files still pending' : drop.state === 'excluded' ? 'No files uploaded' : 'Upload complete'}
            </p>
          )}
          <ul className="mt-2 space-y-2">
            {drop.files.map(slot => {
              const key = inbox.slotKey(drop.batchId, slot.slot);
              const busy = inbox.busy.has(key);
              const error = inbox.errors[key];
              const needsAssignment = !!inbox.settings?.requiresClient && !inbox.returnScoped && !slot.assignment;
              const client = !saved && slot.assignment && inbox.clients.find(item => item.clientId === slot.assignment?.clientId);
              return (
                <li key={key} className="space-y-1 break-words text-sm">
                  <strong>{slot.filename}</strong>
                  {' · '}{slot.state === 'uploaded' ? 'Committed' : busy ? 'Uploading…' : slot.state === 'excluded' ? 'Excluded' : 'Pending'}
                  {!saved && client && slot.assignment && <span> · {client.name} · {slot.assignment.taxYear}</span>}
                  {needsAssignment && <EarlierUploadAssignment inbox={inbox} batchId={drop.batchId} slot={slot.slot} />}
                  {slot.state === 'uploaded' && slot.analysis && <UploadAnalysis analysis={slot.analysis} />}
                  {slot.state === 'uploaded' && slot.practice && <PracticeStatus practice={slot.practice} />}
                  {error && <p className="text-destructive" role="alert">{error}</p>}
                  {slot.state === 'uploaded' && !inbox.returnScoped && inbox.settings?.canDownloadOriginals && (
                    <div className="flex flex-wrap gap-2">
                      <Button asChild variant="outline" size="sm">
                        <Link to="/clients" search={{ inboxBatch: drop.batchId, inboxSlot: slot.slot }}>
                          Open evidence
                        </Link>
                      </Button>
                      {openPractice && slot.practice && (
                        <Button type="button" variant="outline" size="sm"
                          disabled={!practiceDestination(slot.practice)} onClick={() => openPractice(drop.batchId, slot)}>
                          Open practice draft
                        </Button>
                      )}
                      <Button type="button" variant="outline" size="sm" disabled={busy}
                        onClick={() => void inbox.downloadSlot(drop.batchId, slot)}>
                        Download original
                      </Button>
                    </div>
                  )}
                  {slot.state === 'pending' && !busy && !inbox.returnScoped && (
                    <div className="flex items-center gap-2">
                      {!needsAssignment && (inbox.hasFile(drop.batchId, slot.slot) ? (
                        <Button type="button" variant="outline" size="sm" onClick={() => void inbox.retrySlot(drop.batchId, slot)}>
                          Retry upload
                        </Button>
                      ) : (
                        <label>Reselect file
                          <input type="file" accept={slot.mediaType} onChange={event => {
                            const file = event.target.files?.[0];
                            if (file) void inbox.retrySlot(drop.batchId, slot, file);
                          }} />
                        </label>
                      ))}
                      <Button type="button" variant="ghost" size="sm" onClick={() => void inbox.excludeSlot(drop.batchId, slot.slot)}>
                        Exclude file
                      </Button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </li>
      ))}
    </ul>
  );
}

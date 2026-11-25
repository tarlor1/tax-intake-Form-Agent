import { useEffect, useRef, useState } from 'react';
import { useDropzone, type DropzoneState } from 'react-dropzone';
import { api, ApiError, errorMessage } from '@/lib/api';
import type { Batch, BatchFile, ClientAssignment, IntakeClient, IntakeSettings, MediaType } from '@/lib/apiSchemas';
import { inboxFileIsPending, INBOX_REFRESH_MS, PRACTICE_APPROVAL_CHANNEL } from '@/lib/practiceDraft';

interface PendingDrop {
  batchId: string;
  files: { slot: string; filename: string; mediaType: MediaType }[];
  approvedForAI: boolean;
  returnHint?: string;
  assignment?: ClientAssignment;
}

export interface DocumentInboxState extends Pick<DropzoneState, 'getRootProps' | 'getInputProps' | 'open' | 'isDragActive'> {
  settings: IntakeSettings | null;
  batches: Batch[];
  clients: IntakeClient[];
  assignment: ClientAssignment | undefined;
  setAssignment: (assignment: ClientAssignment | undefined) => void;
  createClient: (name: string) => Promise<IntakeClient>;
  assignSlot: (batchId: string, slot: string, assignment: ClientAssignment) => Promise<void>;
  returnScoped: boolean;
  notice: string;
  refreshBatches: () => Promise<void>;
  approved: boolean;
  setApproved: (approved: boolean) => void;
  error: string;
  errors: Record<string, string>;
  busy: ReadonlySet<string>;
  notDeclared: PendingDrop[];
  declare: (drop: PendingDrop) => Promise<void>;
  retrySlot: (batchId: string, slot: BatchFile, file?: File) => Promise<void>;
  slotKey: (batchId: string, slot: string) => string;
  hasFile: (batchId: string, slot: string) => boolean;
  excludeSlot: (batchId: string, slot: string) => Promise<void>;
  downloadSlot: (batchId: string, slot: BatchFile) => Promise<void>;
}

const MAX_FILE_BYTES = 10 * 1024 * 1024;
const MAX_CONCURRENT_UPLOADS = 4;
let uploadsInFlight = 0;
const waitingUploads: Array<() => void> = [];

async function withUploadSlot<T>(upload: () => Promise<T>): Promise<T> {
  if (uploadsInFlight >= MAX_CONCURRENT_UPLOADS) {
    // The resolver is the FIFO wakeup callback; the UI targets ES2022.
    await new Promise<void>(resolve => waitingUploads.push(resolve));
  } else {
    uploadsInFlight += 1;
  }
  try {
    return await upload();
  } finally {
    const next = waitingUploads.shift();
    if (next) next();
    else uploadsInFlight -= 1;
  }
}

const retainedKey = (batch: string, slot: string) => `${batch}/${slot}`;

const hasPendingFiles = (batches: Batch[]) => batches.some(batch =>
  batch.files.some(file => file.state === 'uploaded' && inboxFileIsPending(file)),
);

export function useDocumentInbox(onChanged: () => void, returnId?: string): DocumentInboxState {
  const [settings, setSettings] = useState<IntakeSettings | null>(null);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [clients, setClients] = useState<IntakeClient[]>([]);
  const [assignment, setAssignment] = useState<ClientAssignment>();
  const [approved, setApproved] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [notDeclared, setNotDeclared] = useState<PendingDrop[]>([]);
  const files = useRef(new Map<string, File>());
  const running = useRef(new Set<string>());
  const listGeneration = useRef(0);
  const listScope = useRef<{ returnId: string | undefined; controller: AbortController } | null>(null);
  const changed = useRef(onChanged);
  changed.current = onChanged;

  useEffect(() => {
    const controller = new AbortController();
    listScope.current = { returnId, controller };
    setBatches([]);
    const generation = ++listGeneration.current;
    void Promise.all([api.intakeSettings(controller.signal), api.batches(controller.signal, returnId)]).then(
      async ([config, stored]) => {
        const directory = config.requiresClient && !returnId ? await api.intakeClients(controller.signal) : [];
        if (controller.signal.aborted) return;
        setClients(directory);
        setSettings(config);
        if (listGeneration.current === generation) setBatches(stored);
      },
    ).catch(cause => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    const approvals = new BroadcastChannel(PRACTICE_APPROVAL_CHANNEL);
    approvals.onmessage = () => { void refreshBatches(); };
    return () => {
      controller.abort();
      approvals.close();
      listScope.current = null;
    };
  }, [returnId]);

  const refreshPending = hasPendingFiles(batches);
  useEffect(() => {
    if (!refreshPending) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      if (controller.signal.aborted) return;
      if (!running.current.size) {
        const generation = ++listGeneration.current;
        try {
          const stored = await api.batches(controller.signal, returnId);
          if (!controller.signal.aborted && generation === listGeneration.current && !running.current.size) {
            setBatches(stored);
            if (!hasPendingFiles(stored)) return;
          }
        } catch (cause) {
          if (controller.signal.aborted) return;
          if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) {
            setError(errorMessage(cause));
            return;
          }
        }
      }
      if (!controller.signal.aborted) timer = setTimeout(refresh, INBOX_REFRESH_MS);
    }
    timer = setTimeout(refresh, INBOX_REFRESH_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [refreshPending, returnId]);

  function update(result: Batch) {
    // Stage acknowledgements aren't verified return projections.
    const scope = listScope.current;
    if (returnId !== undefined || !scope || scope.returnId !== undefined) return;
    listGeneration.current += 1;
    setBatches(current => {
      const before = current.find(batch => batch.batchId === result.batchId);
      const prior = new Map(before?.files.map(file => [file.slot, file]));
      const merged = {
        ...result,
        state: before && before.state !== 'incomplete' ? before.state : result.state,
        files: result.files.map(incoming => {
          const previous = prior.get(incoming.slot);
          const file = previous?.assignment && !incoming.assignment
            ? { ...incoming, assignment: previous.assignment } : incoming;
          if (!previous) return file;
          if (file.state === 'pending' && previous.state !== 'pending') return previous;
          // Concurrent upload acknowledgements can predate a terminal analysis result.
          if (previous.analysis && (!file.analysis ||
            (file.analysis.status === 'pending' && previous.analysis.status !== 'pending'))) {
            return { ...previous, assignment: file.assignment };
          }
          const practice = previous.practice;
          if (practice && (!file.practice || (file.practice.status === 'waiting' && practice.status !== 'waiting') ||
            (!inboxFileIsPending(previous) && practice.status !== 'waiting' && inboxFileIsPending(file)))) {
            return { ...file, practice };
          }
          return file;
        }),
      };
      return [merged, ...current.filter(batch => batch.batchId !== result.batchId)];
    });
  }

  async function refreshBatches() {
    const scope = listScope.current;
    if (!scope || scope.returnId !== returnId) return;
    const { signal } = scope.controller;
    const generation = ++listGeneration.current;
    try {
      const stored = await api.batches(signal, returnId);
      if (!signal.aborted && listGeneration.current === generation) setBatches(stored);
    } catch (cause) {
      if (!signal.aborted && listGeneration.current === generation) setError(errorMessage(cause));
    }
  }

  async function perform(key: string, action: () => Promise<void>) {
    if (running.current.has(key)) return;
    running.current.add(key);
    setBusy(new Set(running.current));
    setErrors(current => ({ ...current, [key]: '' }));
    try {
      await action();
      return true;
    } catch (cause) {
      setErrors(current => ({ ...current, [key]: errorMessage(cause) }));
      return false;
    } finally {
      running.current.delete(key);
      setBusy(new Set(running.current));
    }
  }

  async function retrySlot(batchId: string, slot: Pick<BatchFile, 'slot' | 'mediaType' | 'filename'>, chosen?: File) {
    const key = retainedKey(batchId, slot.slot);
    if (chosen) files.current.set(key, chosen);
    const file = files.current.get(key);
    if (!file) return;
    return perform(key, async () => {
      if (file.name !== slot.filename || !file.size || file.size > MAX_FILE_BYTES) {
        throw new Error('Select the declared nonempty file (up to 10 MiB), or leave this slot out.');
      }
      const result = await withUploadSlot(() => api.uploadSlot(batchId, slot.slot, slot.mediaType, file));
      update(result);
      files.current.delete(key);
    });
  }

  async function declare(drop: PendingDrop) {
    await perform(drop.batchId, async () => {
      const result = await api.declareDrop(drop, returnId);
      update(result);
      setNotDeclared(current => current.filter(item => item.batchId !== drop.batchId));
      const pending = result.files.filter(slot => slot.state === 'pending');
      const outcomes = await Promise.all(pending.map(slot => retrySlot(result.batchId, slot)));
      if (returnId) {
        setNotice(outcomes.includes(false)
          ? 'Some files are pending. Retry or exclude them in the general document inbox.'
          : 'Files appear here only after the firm verifies that they belong to this return.');
        await refreshBatches();
      }
      if (outcomes.includes(true)) changed.current();
    });
  }

  function uploadDrop(selected: File[]) {
    if (!settings?.enabled || !selected.length) return;
    if (selected.length > 100) {
      setError('Choose no more than 100 files in one drop.');
      return;
    }
    if (settings.requiresClient && !returnId && !assignment) {
      setError('Choose a client and tax year before uploading.');
      return;
    }
    const batchId = crypto.randomUUID();
    const drop: PendingDrop = {
      batchId, approvedForAI: approved && !!settings.approvalLabel, returnHint: returnId,
      assignment: returnId ? undefined : assignment,
      files: selected.map(file => {
        const slot = crypto.randomUUID();
        files.current.set(retainedKey(batchId, slot), file);
        return { slot, filename: file.name, mediaType: file.name.toLowerCase().endsWith('.txt') ? 'text/plain' : 'application/pdf' };
      }),
    };
    setApproved(false);
    setNotDeclared(current => [drop, ...current]);
    void declare(drop);
  }

  const { getRootProps, getInputProps, open, isDragActive } = useDropzone({
    accept: { 'application/pdf': ['.pdf'], 'text/plain': ['.txt'] },
    minSize: 1,
    maxSize: MAX_FILE_BYTES,
    multiple: true,
    disabled: !settings?.enabled || (settings.requiresClient && !returnId && !assignment),
    noClick: true,
    noKeyboard: true,
    onDrop: (selected, rejected) => {
      setError(rejected.map(({ file }) => `${file.name}: choose a nonempty PDF or UTF-8 TXT up to 10 MiB.`).join(' '));
      uploadDrop(selected);
    },
  });

  return {
    settings, batches, approved, setApproved, error, errors, busy, notDeclared,
    returnScoped: returnId !== undefined, notice, refreshBatches,
    clients, assignment, setAssignment,
    createClient: async name => {
      const client = await api.createClient(name);
      setClients(current => [...current, client].sort((a, b) => a.name.localeCompare(b.name)));
      return client;
    },
    assignSlot: async (batch, slot, selected) => {
      await perform(retainedKey(batch, slot), async () => {
        update(await api.assignSlot(batch, slot, selected));
      });
    },
    getRootProps, getInputProps, open, isDragActive, declare,
    retrySlot: async (batch, slot, file) => {
      if (!returnId && await retrySlot(batch, slot, file)) changed.current();
    },
    slotKey: retainedKey,
    hasFile: (batch: string, slot: string) => files.current.has(retainedKey(batch, slot)),
    downloadSlot: async (batch, slot) => {
      await perform(retainedKey(batch, slot.slot), async () => {
        const response = await api.inboxOriginal(batch, slot.slot);
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement('a');
        link.href = url;
        link.download = slot.filename;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
    },
    excludeSlot: async (batch, slot) => {
      if (returnId) return;
      await perform(retainedKey(batch, slot), async () => {
        update(await api.excludeSlot(batch, slot));
        files.current.delete(retainedKey(batch, slot));
        changed.current();
      });
    },
  };
}

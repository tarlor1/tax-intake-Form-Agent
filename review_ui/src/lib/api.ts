import { z } from 'zod';
import { expireSession, isSessionExpired, SESSION_EXPIRED_MESSAGE } from './session';
import {
  batchSchema,
  batchFileSchema,
  evidenceDetailSchema,
  reviewDecisionSchema,
  reviewOpenedSchema,
  reviewSelectionSchema,
  intakeClientSchema,
  intakeSettingsSchema,
  jobSchema,
  summarySchema,
} from './apiSchemas';
import type { BatchFile, ClientAssignment, MediaType, ReviewSelection } from './apiSchemas';
export type { Job, ReturnSummary } from './apiSchemas';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

function token(): string {
  const value = document.querySelector<HTMLMetaElement>('meta[name="review-token"]')?.content;
  if (!value || value === '__REVIEW_TOKEN__') {
    throw new Error(
      'Local frontend authentication is missing. Rebuild the frontend, restart python review.py, and open the address printed by the launcher.',
    );
  }
  return value;
}

const errorSchema = z.object({
  detail: z
    .union([
      z.string(),
      z.array(
        z.object({
          loc: z.array(z.union([z.string(), z.number()])).optional(),
          msg: z.string().optional(),
        }),
      ),
    ])
    .optional(),
});

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  if (isSessionExpired()) {
    throw new ApiError(401, SESSION_EXPIRED_MESSAGE);
  }

  const headers = new Headers(init.headers);
  headers.set('X-Review-Token', token());
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(`/api/review${path}`, {
    ...init,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
    mode: 'same-origin',
  });
  if (response.status === 401) {
    expireSession();
    throw new ApiError(401, SESSION_EXPIRED_MESSAGE);
  }

  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const payload = errorSchema.safeParse(await response.json());
      if (payload.success && typeof payload.data.detail === 'string') message = payload.data.detail;
      else if (payload.success && Array.isArray(payload.data.detail)) {
        message = payload.data.detail
          .map(
            item =>
              `${item.loc?.filter(part => part !== 'body').join('.') || 'Request'}: ${item.msg || 'Invalid value'}`,
          )
          .join('; ');
      }
    } catch {
      /* A non-JSON error retains its HTTP status. */
    }
    throw new ApiError(response.status, message);
  }
  return response;
}

async function decode<T>(response: Response, schema: z.ZodType<T>): Promise<T> {
  const parsed = schema.safeParse(await response.json());
  if (!parsed.success)
    throw new Error(
      `The server returned invalid review data: ${parsed.error.issues[0]?.message ?? 'Unknown schema error'}`,
    );
  return parsed.data;
}

const returnPath = (id: string) => `/returns/${encodeURIComponent(id)}`;

export interface ReviewIdentity {
  reviewSessionId: string;
  returnId: string;
  inputRevision: string;
}

export async function streamReviewSelections(
  scope: ReviewIdentity,
  signal: AbortSignal,
  publish: (selection: ReviewSelection) => void,
) {
  const response = await request(`${returnPath(scope.returnId)}/review/events`, {
    signal,
    headers: { 'X-Review-Session': scope.reviewSessionId, Accept: 'text/event-stream' },
  });
  if (!response.body || !response.headers.get('Content-Type')?.startsWith('text/event-stream')) {
    throw new Error('The local review stream is unavailable.');
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  try {
    for (; ;) {
      const { done, value } = await reader.read();
      if (done || signal.aborted) return;
      buffer += value;
      let boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const match = /^event: selection\ndata: (.+)$/.exec(frame);
        if (!match || frame.length > 2048) throw new Error('Invalid native selection event.');
        const selection = reviewSelectionSchema.parse(JSON.parse(match[1]));
        if (
          selection.reviewSessionId !== scope.reviewSessionId ||
          selection.returnId !== scope.returnId ||
          selection.inputRevision !== scope.inputRevision
        ) throw new Error('Native selection is outside this review reservation.');
        if (!signal.aborted) publish(selection);
        boundary = buffer.indexOf('\n\n');
      }
      if (buffer.length > 2048) throw new Error('Invalid native selection event.');
    }
  } finally {
    await reader.cancel().catch(() => { });
    reader.releaseLock();
  }
}

interface DropManifest {
  batchId: string;
  files: { slot: string; filename: string; mediaType: MediaType }[];
  approvedForAI: boolean;
  returnHint?: string;
  assignment?: ClientAssignment;
}

const batchesPath = (returnId?: string) => `${returnId ? returnPath(returnId) : ''}/intake/batches`;

function batchFilePath(batchId: string, slot: string) {
  return `/intake/batches/${encodeURIComponent(batchId)}/files/${encodeURIComponent(slot)}`;
}

export type InboxEvidenceFile = BatchFile & { uploadId: string; versionId: string };

async function decodeInboxEvidence(response: Response, slot: string): Promise<InboxEvidenceFile> {
  const file = await decode(response, batchFileSchema);
  if (file.slot !== slot || file.state !== 'uploaded' || !file.uploadId || !file.versionId) {
    throw new Error('The evidence does not identify the requested committed file.');
  }
  return { ...file, uploadId: file.uploadId, versionId: file.versionId };
}

export const api = {
  async returns(signal?: AbortSignal) {
    return decode(await request('/returns', { signal }), z.array(summarySchema));
  },
  async evidence(id: string, revision?: string, signal?: AbortSignal) {
    const query = revision === undefined ? '' : `?${new URLSearchParams({ revision })}`;
    const detail = await decode(await request(`${returnPath(id)}/evidence${query}`, { signal }), evidenceDetailSchema);
    if (detail.id !== id || (revision !== undefined && detail.inputRevision !== revision)) {
      throw new Error('The evidence revision is outside this return.');
    }
    return detail;
  },
  async original(id: string, revision: string, doc: string, version: string, signal?: AbortSignal) {
    const path = `/review/originals/${encodeURIComponent(doc)}/${encodeURIComponent(version)}`;
    return request(`${returnPath(id)}${path}?${new URLSearchParams({ revision })}`, { signal });
  },
  async openReview(id: string, inputRevision: string) {
    return decode(await request(`${returnPath(id)}/review/open`, {
      method: 'POST', body: JSON.stringify({ inputRevision }),
    }), reviewOpenedSchema);
  },
  async closeReview(scope: ReviewIdentity) {
    await request(`${returnPath(scope.returnId)}/review/close`, {
      method: 'POST', body: JSON.stringify({ reviewSessionId: scope.reviewSessionId }),
    });
  },
  async reviewDecision(
    scope: ReviewIdentity, expectedDecisionVersion: number, expectedFieldVersion: number,
    decision: { kind: 'mark_reviewed' } | { kind: 'choose_source'; bindingId: string; candidateId: string },
  ) {
    return decode(await request(`${returnPath(scope.returnId)}/review/decisions`, {
      method: 'POST',
      body: JSON.stringify({
        ...decision, inputRevision: scope.inputRevision,
        reviewSessionId: scope.reviewSessionId, expectedDecisionVersion, expectedFieldVersion,
      }),
    }), reviewDecisionSchema);
  },
  async jobs(id: string, signal?: AbortSignal) {
    const jobs = await decode(
      await request(`${returnPath(id)}/jobs`, { signal }),
      z.array(jobSchema),
    );
    if (jobs.some(job => job.returnId !== id))
      throw new Error('A job belongs to a different return.');
    return jobs;
  },
  async intakeSettings(signal?: AbortSignal) {
    return decode(await request('/intake', { signal }), intakeSettingsSchema);
  },
  async intakeClients(signal?: AbortSignal) {
    return decode(await request('/intake/clients', { signal }), z.array(intakeClientSchema));
  },
  async createClient(name: string) {
    return decode(await request('/intake/clients', {
      method: 'POST', body: JSON.stringify({ name }),
    }), intakeClientSchema);
  },
  async assignSlot(batchId: string, slot: string, assignment: ClientAssignment) {
    return decode(await request(`${batchFilePath(batchId, slot)}/assignment`, {
      method: 'PUT', body: JSON.stringify(assignment),
    }), batchSchema);
  },
  async batches(signal?: AbortSignal, returnId?: string) {
    return decode(await request(batchesPath(returnId), { signal }), z.array(batchSchema));
  },
  async declareDrop(manifest: DropManifest, returnId?: string) {
    return decode(await request(batchesPath(returnId), {
      method: 'POST', body: JSON.stringify(manifest),
    }), batchSchema);
  },
  async uploadSlot(batchId: string, slot: string, mediaType: MediaType, file: File) {
    return decode(await request(batchFilePath(batchId, slot), {
      method: 'PUT', headers: { 'Content-Type': mediaType }, body: file,
    }), batchSchema);
  },
  async inboxEvidence(batchId: string, slot: string, signal?: AbortSignal): Promise<InboxEvidenceFile> {
    return decodeInboxEvidence(await request(`${batchFilePath(batchId, slot)}/evidence`, { signal }), slot);
  },
  async approveInboxPractice(batchId: string, slot: string) {
    return decodeInboxEvidence(await request(`${batchFilePath(batchId, slot)}/practice`, { method: 'POST' }), slot);
  },
  async inboxOriginal(batchId: string, slot: string, signal?: AbortSignal) {
    return request(`${batchFilePath(batchId, slot)}/original`, { signal });
  },
  async excludeSlot(batchId: string, slot: string) {
    return decode(await request(`${batchFilePath(batchId, slot)}/exclude`, { method: 'POST' }), batchSchema);
  },
};

export const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : 'An unexpected error occurred.';

export function dateLabel(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

import { z } from 'zod';
import { expireSession, isSessionExpired, SESSION_EXPIRED_MESSAGE } from './session';
import {
  browserStateSchema,
  detailSchema,
  inboxDocumentSchema,
  intakeSchema,
  jobSchema,
  summarySchema,
} from './apiSchemas';
import type { Field } from './apiSchemas';
export type {
  ReturnSummary,
  SourceDocument,
  Citation,
  Candidate,
  Field,
  ReviewEvent,
  ReturnDetail,
  Job,
  BrowserState,
  InboxDocument,
  InboxGroup,
  IntakeState,
} from './apiSchemas';

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

export interface RevisionCommand {
  expectedRevision: number;
}
export interface SelectCommand extends RevisionCommand {
  fieldId: string;
}
export interface ResolveCommand extends SelectCommand {
  candidateId: string;
  reason?: string;
}
export interface ApplyFieldCommand extends SelectCommand {
  value: string;
  reason?: string;
}
export type BrowserAction =
  | { kind: 'click'; x: number; y: number }
  | { kind: 'wheel'; x: number; y: number; deltaY: number }
  | { kind: 'key'; key: string }
  | { kind: 'text'; text: string }
  | { kind: 'focus'; fieldId: string };

export const api = {
  async returns(signal?: AbortSignal) {
    return decode(await request('/returns', { signal }), z.array(summarySchema));
  },
  async detail(id: string, signal?: AbortSignal) {
    const detail = await decode(await request(returnPath(id), { signal }), detailSchema);
    if (detail.id !== id) throw new Error('The server returned a different return identity.');
    return detail;
  },
  async browserState(id: string, signal?: AbortSignal) {
    return decode(await request(`${returnPath(id)}/browser`, { signal }), browserStateSchema);
  },
  async interact(id: string, jobId: string, action: BrowserAction & { reason?: string }) {
    return decode(
      await request(`${returnPath(id)}/jobs/${encodeURIComponent(jobId)}/interact`, {
        method: 'POST',
        body: JSON.stringify(action),
      }),
      jobSchema,
    );
  },
  async chooseSource(id: string, jobId: string, body: ResolveCommand) {
    return decode(
      await request(`${returnPath(id)}/jobs/${encodeURIComponent(jobId)}/source`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),
      jobSchema,
    );
  },
  async applyField(id: string, jobId: string, body: ApplyFieldCommand) {
    return decode(
      await request(`${returnPath(id)}/jobs/${encodeURIComponent(jobId)}/field`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),
      jobSchema,
    );
  },
  async submitBrowser(id: string, jobId: string, body: RevisionCommand) {
    return decode(
      await request(`${returnPath(id)}/jobs/${encodeURIComponent(jobId)}/submit`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),
      jobSchema,
    );
  },
  async upload(id: string, expectedRevision: number, file: File) {
    return decode(
      await request(`${returnPath(id)}/documents`, {
        method: 'POST',
        headers: {
          'Content-Type': file.name.toLowerCase().endsWith('.txt')
            ? 'text/plain'
            : 'application/pdf',
          'X-Document-Name': encodeURIComponent(file.name),
          'X-Expected-Revision': String(expectedRevision),
        },
        body: file,
      }),
      detailSchema,
    );
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
  async startJob(id: string, body: RevisionCommand) {
    return decode(
      await request(`${returnPath(id)}/jobs`, { method: 'POST', body: JSON.stringify(body) }),
      jobSchema,
    );
  },
  async cancelJob(id: string, jobId: string) {
    return decode(
      await request(`${returnPath(id)}/jobs/${encodeURIComponent(jobId)}/cancel`, {
        method: 'POST',
        body: '{}',
      }),
      jobSchema,
    );
  },
  async pdf(returnId: string, documentId: string, signal: AbortSignal): Promise<Uint8Array> {
    const response = await request(
      `${returnPath(returnId)}/documents/${encodeURIComponent(documentId)}/pdf`,
      { signal },
    );
    return new Uint8Array(await response.arrayBuffer());
  },
  async text(returnId: string, documentId: string, signal: AbortSignal) {
    return (
      await request(`${returnPath(returnId)}/documents/${encodeURIComponent(documentId)}/text`, {
        signal,
      })
    ).text();
  },
  async intake(signal?: AbortSignal) {
    return decode(await request('/intake', { signal }), intakeSchema);
  },
  async uploadInbox(file: File) {
    return decode(
      await request('/intake/documents', {
        method: 'POST',
        headers: {
          'Content-Type': file.name.toLowerCase().endsWith('.txt')
            ? 'text/plain'
            : 'application/pdf',
          'X-Document-Name': encodeURIComponent(file.name),
        },
        body: file,
      }),
      inboxDocumentSchema,
    );
  },
  async processDocuments(documentIds: string[], approvedForAI: boolean) {
    return decode(
      await request('/intake/process', {
        method: 'POST',
        body: JSON.stringify({ documentIds, approvedForAI }),
      }),
      z.array(inboxDocumentSchema),
    );
  },
  async extract(id: string, expectedRevision: number, approvedForAI: boolean) {
    return decode(
      await request(`${returnPath(id)}/extract`, {
        method: 'POST',
        body: JSON.stringify({ expectedRevision, approvedForAI }),
      }),
      z.array(inboxDocumentSchema),
    );
  },
  async assign(
    documentId: string,
    body: {
      groupId: string;
      returnId?: string;
      clientName?: string;
      taxYear?: number;
      returnType?: 'individual' | 'business';
    },
  ) {
    return decode(
      await request(`/intake/documents/${encodeURIComponent(documentId)}/assign`, {
        method: 'POST',
        body: JSON.stringify(body),
      }),
      inboxDocumentSchema,
    );
  },
  async browserFrame(returnId: string, jobId: string, signal: AbortSignal): Promise<Blob | null> {
    const response = await request(
      `${returnPath(returnId)}/jobs/${encodeURIComponent(jobId)}/frame`,
      { signal },
    );
    return response.status === 204 ? null : response.blob();
  },
};

export const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : 'An unexpected error occurred.';

export function fieldValue(field: Pick<Field, 'kind' | 'value'>): string {
  if (field.value === null || field.value === '') return 'Missing';
  if (field.kind === 'money') {
    const amount = Number(field.value);
    if (Number.isFinite(amount))
      return amount.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
  }
  if (field.kind === 'multi') {
    try {
      const values: unknown = JSON.parse(field.value);
      if (Array.isArray(values) && values.every(value => typeof value === 'string'))
        return values.join(', ') || 'None selected';
    } catch {
      /* Historical values remain visible verbatim. */
    }
  }
  return field.value;
}

export function dateLabel(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

import { z } from 'zod';

// Disable Zod's eval-based capability probe and JIT under the browser's strict CSP.
z.config({ jitless: true });

const identifier = z.string().min(1).max(150);
const count = z.number().int().nonnegative();
export const rectangleSchema = z
  .tuple([
    z.number().min(0).max(1),
    z.number().min(0).max(1),
    z.number().positive().max(1),
    z.number().positive().max(1),
  ])
  .refine(
    ([left, top, width, height]) => left + width <= 1.000001 && top + height <= 1.000001,
    'Region must fit inside the page.',
  );

const originalReferenceSchema = z.strictObject({ documentId: identifier, versionId: identifier });
const evidencePage = z.number().int().positive();
const literalQuote = z.string().min(1).max(20_000);

export const taxEvidenceSchema = z.discriminatedUnion('precision', [
  z.strictObject({
    precision: z.literal('region'),
    original: originalReferenceSchema,
    page: evidencePage,
    rect: rectangleSchema,
    quote: literalQuote,
  }),
  z.strictObject({
    precision: z.literal('lines'),
    original: originalReferenceSchema,
    lineStart: evidencePage,
    lineEnd: evidencePage,
    quote: literalQuote,
  }).refine(({ lineStart, lineEnd }) => lineStart <= lineEnd, 'Source lines must be ordered.'),
  z.strictObject({
    precision: z.literal('page'),
    original: originalReferenceSchema,
    page: evidencePage,
  }),
  z.strictObject({
    precision: z.literal('document'),
    original: originalReferenceSchema,
  }),
]);

const evidenceDocumentSchema = z.object({
  id: identifier,
  versionId: identifier,
  title: z.string(),
  mediaType: z.enum(['application/pdf', 'text/plain']),
  pageCount: z.number().int().positive().nullable(),
});
const evidenceCitationSchema = z.object({ id: identifier, label: z.string(), evidence: taxEvidenceSchema });
const evidenceCandidateSchema = z.object({
  id: identifier,
  label: z.string(),
  value: z.string().nullable(),
  citations: z.array(evidenceCitationSchema),
});
const evidenceFieldSchema = z.object({
  id: identifier,
  label: z.string(),
  recordLabel: z.string(),
  value: z.string().nullable(),
  priorSourceValue: z.string().nullable(),
  proposedValue: z.string().nullable(),
  savedValue: z.string().nullable(),
  provenance: z.enum(['source', 'preparer', 'archived', 'none']),
  status: z.enum(['verified', 'unverified', 'conflict', 'needs_client_input']),
  usedCitationId: identifier.nullable(),
  candidates: z.array(evidenceCandidateSchema),
  derivation: z.string().nullable(),
});
export const evidenceDetailSchema = z.object({
  id: identifier,
  clientId: identifier,
  clientName: z.string(),
  taxYear: z.number().int().min(1900).max(2200),
  returnType: z.enum(['individual', 'business', '1120']),
  inputRevision: identifier,
  latestRevision: identifier,
  revisions: z.array(z.object({ id: identifier, label: z.string() })),
  documents: z.array(evidenceDocumentSchema),
  fields: z.array(evidenceFieldSchema),
  reviewAvailable: z.boolean(),
  reviewMessage: z.string().nullable(),
  decisionVersion: count,
  fieldVersion: count,
});
export const reviewOpenedSchema = z.strictObject({
  status: z.enum(['open', 'queued', 'recovery_required']),
  reviewSessionId: identifier.nullable(),
  returnId: identifier,
  inputRevision: identifier,
}).refine(value => (value.status !== 'queued') === (value.reviewSessionId !== null));
export const reviewSelectionSchema = z.strictObject({
  reviewSessionId: identifier,
  returnId: identifier,
  inputRevision: identifier,
  sequence: count,
  bindingId: identifier.nullable(),
  status: z.enum(['selected', 'unmapped', 'detached']),
}).refine(value => (value.status === 'selected') === (value.bindingId !== null));
export const reviewDecisionSchema = z.strictObject({ decisionVersion: count });

export type EvidenceDetail = z.infer<typeof evidenceDetailSchema>;
export type EvidenceField = z.infer<typeof evidenceFieldSchema>;
export type EvidenceCitation = z.infer<typeof evidenceCitationSchema>;
export type EvidenceDocument = z.infer<typeof evidenceDocumentSchema>;
export type ReviewSelection = z.infer<typeof reviewSelectionSchema>;
export type EvidenceSearch = z.infer<typeof returnSearchSchema>;

const identity = {
  id: identifier,
  clientId: identifier,
  clientName: z.string(),
  taxYear: z.number().int().min(1900).max(2200),
  returnType: z.enum(['individual', 'business', '1120']),
  destination: z.enum(['Archived intake', 'Sigma Web', 'Sigma Desktop']),
};
export const summarySchema = z.object({
  ...identity,
  totalFields: count,
  acceptedFields: count,
  conflictFields: count,
  missingFields: count,
  updatedAt: z.string(),
  reviewRequired: z.boolean().default(false),
});
export const jobSchema = z.object({
  id: identifier,
  returnId: identifier,
  inputRevision: identifier,
  status: z.enum(['queued', 'preparing', 'verifying', 'ready_for_review', 'needs_attention', 'reconciling']),
  issues: z.array(z.string()),
});
export const returnSearchSchema = z.strictObject({
  revision: identifier.optional(),
  field: identifier.optional(),
  document: identifier.optional(),
  version: identifier.optional(),
  citation: identifier.optional(),
  page: z.number().int().positive().optional(),
});
export const queueSearchSchema = z
  .object({
    q: z.string().optional(),
    year: z.number().int().min(1900).max(2200).optional(),
    inboxBatch: identifier.optional(),
    inboxSlot: identifier.optional(),
  })
  .strict()
  .refine(search => (search.inboxBatch === undefined) === (search.inboxSlot === undefined));

const mediaType = z.enum(['application/pdf', 'text/plain']);
const inboxAnalysis = z.object({
  status: z.enum(['pending', 'filed', 'needs_review', 'failed']),
  category: z.string().nullable(),
  forms: z.array(z.string()),
  facts: z.array(z.object({
    label: z.string(),
    value: z.string(),
    page: z.number().int().positive().nullable(),
    line: z.number().int().positive(),
    quote: z.string(),
    rect: rectangleSchema.nullish(),
  })),
  message: z.string().nullable(),
});
const practiceDraftSchema = z.object({
  status: z.enum(['waiting', 'queued', 'preparing', 'ready', 'needs_review', 'failed']),
  gated: z.string().nullish(),
  message: z.string().nullish(),
  url: z.string().nullable(),
  bindings: z.record(z.string(), count),
});

const taxYear = z.number().int().min(1900).max(9999);
const clientAssignmentSchema = z.object({ clientId: identifier, taxYear });
export const intakeClientSchema = z.object({
  clientId: identifier,
  name: z.string(),
  taxYears: z.array(taxYear).nullable(),
});

export const batchFileSchema = z.object({
  slot: identifier,
  filename: z.string(),
  mediaType,
  state: z.enum(['pending', 'uploaded', 'excluded']),
  uploadId: identifier.nullable(),
  versionId: identifier.nullable(),
  analysis: inboxAnalysis.nullish(),
  practice: practiceDraftSchema.nullish(),
  assignment: clientAssignmentSchema.nullish(),
});
export const batchSchema = z.object({
  batchId: identifier,
  state: z.enum(['incomplete', 'sealed', 'excluded']),
  approvedForAI: z.boolean(),
  files: z.array(batchFileSchema),
});
export const intakeSettingsSchema = z.object({
  enabled: z.boolean(),
  approvalLabel: z.string().nullable(),
  message: z.string().nullable(),
  canDownloadOriginals: z.boolean().default(false),
  requiresClient: z.boolean().default(false),
  canCreateClients: z.boolean().default(false),
});

export type ReturnSummary = z.infer<typeof summarySchema>;
export type Job = z.infer<typeof jobSchema>;
export type IntakeSettings = z.infer<typeof intakeSettingsSchema>;
export type IntakeClient = z.infer<typeof intakeClientSchema>;
export type ClientAssignment = z.infer<typeof clientAssignmentSchema>;
export type Batch = z.infer<typeof batchSchema>;
export type BatchFile = z.infer<typeof batchFileSchema>;
export type PracticeDraft = z.infer<typeof practiceDraftSchema>;
export type MediaType = z.infer<typeof mediaType>;
export type RegionCitation = EvidenceCitation & { evidence: Extract<EvidenceCitation['evidence'], { precision: 'region' }> };
export type LineCitation = EvidenceCitation & { evidence: Extract<EvidenceCitation['evidence'], { precision: 'lines' }> };

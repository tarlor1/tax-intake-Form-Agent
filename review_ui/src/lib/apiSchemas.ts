import { z } from 'zod';

// Disable Zod's eval-based capability probe and JIT under the browser's strict CSP.
z.config({ jitless: true });

const identifier = z.string().min(1).max(150);
const count = z.number().int().nonnegative();
const revision = z.number().int().positive();
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

export const documentSchema = z.object({
  id: identifier,
  title: z.string(),
  pageCount: z.number().int().positive(),
  version: revision,
  mediaType: z.enum(['application/pdf', 'text/plain']).optional(),
  extractionStatus: z.string().optional(),
});
export const citationSchema = z.object({
  id: identifier,
  documentId: identifier,
  page: z.number().int().positive(),
  rect: rectangleSchema,
  quote: z.string(),
  label: z.string(),
  lineStart: z.number().int().positive().optional(),
  lineEnd: z.number().int().positive().optional(),
});
export const candidateSchema = z.object({
  id: identifier,
  value: z.string(),
  citations: z.array(citationSchema),
  provenance: z.enum(['human', 'gemini']).optional(),
  documentId: identifier.optional(),
  reason: z.string().optional(),
  documentVersion: revision.optional(),
});
export const fieldSchema = z.object({
  id: identifier,
  label: z.string(),
  section: z.string(),
  kind: z.enum(['money', 'text', 'date', 'integer', 'select', 'multi']),
  value: z.string().nullable(),
  revision,
  status: z.enum(['needs_review', 'accepted', 'blocked']),
  conflict: z.boolean(),
  selectedCandidateId: identifier.nullable(),
  candidates: z.array(candidateSchema),
  citations: z.array(citationSchema),
  dependencies: z.array(identifier),
  derivation: z.string().nullable(),
  correctionReason: z.string().nullable(),
  valueOrigin: z.enum(['ai', 'source', 'preparer', 'workspace', 'calculated']).nullish(),
  required: z.boolean().default(false),
  options: z.array(z.object({ value: z.string(), label: z.string() })).default([]),
  controlType: z.string().optional(),
  min: z.union([z.number(), z.iso.date()]).optional(),
  max: z.union([z.number(), z.iso.date()]).optional(),
});
export const eventSchema = z.object({
  id: identifier,
  createdAt: z.string(),
  kind: z.string(),
  fieldId: identifier.nullable(),
  message: z.string(),
});
const identity = {
  id: identifier,
  clientId: identifier,
  clientName: z.string(),
  taxYear: z.number().int().min(1900).max(2200),
  returnType: z.enum(['individual', 'business']),
  destination: z.enum(['Client intake', 'ProWeb', 'Pro Desktop']),
};
export const summarySchema = z.object({
  ...identity,
  totalFields: count,
  acceptedFields: count,
  conflictFields: count,
  missingFields: count,
  updatedAt: z.string(),
});
export const detailSchema = z
  .object({
    ...identity,
    revision,
    activeFieldId: z.string(),
    fields: z.array(fieldSchema),
    legacyFields: z.array(fieldSchema).default([]),
    intakeContractVersion: z.union([z.string(), z.number()]).optional(),
    documents: z.array(documentSchema),
    events: z.array(eventSchema),
  })
  .superRefine((detail, context) => {
    const fieldIds = new Set(detail.fields.map(field => field.id));
    const documentIds = new Set(detail.documents.map(document => document.id));
    if (fieldIds.size !== detail.fields.length || documentIds.size !== detail.documents.length) {
      context.addIssue({ code: 'custom', message: 'Duplicate field or document identity.' });
    }
    if (detail.fields.length && !fieldIds.has(detail.activeFieldId)) {
      context.addIssue({
        code: 'custom',
        message: 'The saved active field is not part of this return.',
      });
    }
    for (const field of detail.fields) {
      for (const citation of [
        ...field.citations,
        ...field.candidates.flatMap(candidate => candidate.citations),
      ]) {
        const source = detail.documents.find(document => document.id === citation.documentId);
        if (!source || citation.page > source.pageCount) {
          context.addIssue({
            code: 'custom',
            message: 'A citation points outside this return’s documents.',
          });
        }
      }
    }
  });
export const jobSchema = z.object({
  id: identifier,
  returnId: identifier,
  status: z.enum([
    'queued',
    'running',
    'cancelling',
    'cancelled',
    'ready_for_review',
    'failed',
    'interrupted',
  ]),
  stage: z.string(),
  completedFields: count,
  totalFields: count,
  createdAt: z.string(),
  updatedAt: z.string(),
  error: z.string().nullable(),
  issues: z.array(z.string()),
  sourceRevision: revision,
  target: z.enum(['intake-form', 'legacy-generated']).default('legacy-generated'),
  browserAvailable: z.boolean().default(false),
  focusedFieldId: z.string().nullable().default(null),
  submitted: z.boolean().default(false),
  intakeId: z.string().nullable().default(null),
  observedAt: z.string().nullable().default(null),
  actionPending: z.boolean().default(false),
  actionError: z.string().nullable().default(null),
});
export const browserStateSchema = z.object({
  jobId: identifier.nullable(),
  values: z.record(z.string(), z.union([z.string(), z.array(z.string())])),
  observedAt: z.string().nullable(),
  submitted: z.boolean(),
  intakeId: z.string().nullable(),
  browserAvailable: z.boolean(),
  focusedFieldId: z.string().nullable(),
  issues: z.array(z.string()),
});
export const returnSearchSchema = z.object({ field: identifier.optional() }).strict();
export const queueSearchSchema = z
  .object({
    q: z.string().optional(),
    year: z.number().int().min(1900).max(2200).optional(),
  })
  .strict();

export const inboxGroupSchema = z.object({
  id: identifier,
  clientName: z.string().nullable(),
  taxYear: z.number().int().min(1900).max(2200).nullable(),
  returnType: z.enum(['individual', 'business']).nullable(),
  status: z.enum(['assigned', 'needs_assignment']),
  returnId: identifier.nullable(),
  clientId: identifier.nullable(),
  issues: z.array(z.string()),
});
export const inboxDocumentSchema = z.object({
  id: identifier,
  filename: z.string(),
  mediaType: z.enum(['application/pdf', 'text/plain']),
  status: z.enum(['uploaded', 'processing', 'needs_assignment', 'completed', 'failed']),
  error: z.string().nullable(),
  groups: z.array(inboxGroupSchema),
  createdAt: z.string(),
  updatedAt: z.string(),
});
export const intakeSchema = z.object({
  configured: z.boolean(),
  model: z.string(),
  documents: z.array(inboxDocumentSchema),
});
export type InboxGroup = z.infer<typeof inboxGroupSchema>;
export type InboxDocument = z.infer<typeof inboxDocumentSchema>;
export type IntakeState = z.infer<typeof intakeSchema>;

export type ReturnSummary = z.infer<typeof summarySchema>;
export type SourceDocument = z.infer<typeof documentSchema>;
export type Citation = z.infer<typeof citationSchema>;
export type Candidate = z.infer<typeof candidateSchema>;
export type Field = z.infer<typeof fieldSchema>;
export type ReviewEvent = z.infer<typeof eventSchema>;
export type ReturnDetail = z.infer<typeof detailSchema>;
export type Job = z.infer<typeof jobSchema>;
export type BrowserState = z.infer<typeof browserStateSchema>;

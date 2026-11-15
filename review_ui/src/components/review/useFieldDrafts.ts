import { useRef, useState } from 'react';
import type { Field } from '../../lib/api';

export type DraftStatus = 'recorded' | 'unsaved' | 'applying' | 'confirmed' | 'failed';

export interface FieldDraft {
  value: string;
  reason: string;
  version: number;
  status: DraftStatus;
  error: string;
}

export function canonicalFieldValue(field: Field, value: string | string[] | null): string {
  if (field.kind === 'multi') {
    const selected: unknown = Array.isArray(value) ? value : value ? JSON.parse(value) : [];
    if (!Array.isArray(selected) || !selected.every(item => typeof item === 'string')) {
      throw new Error(`Invalid multiple-choice value for ${field.label}.`);
    }
    if (selected.some(value => !field.options.some(option => option.value === value))) {
      throw new Error(`Choose allowed options for ${field.label}.`);
    }

    const orderedValues = field.options
      .filter(option => selected.includes(option.value))
      .map(option => option.value);
    return JSON.stringify(orderedValues);
  }

  const text = (Array.isArray(value) ? value.join(', ') : value ?? '').trim();
  return field.kind === 'integer' && /^\d+$/.test(text) ? String(Number(text)) : text;
}

function recordedDraft(field: Field): FieldDraft {
  return { value: field.value ?? '', reason: '', version: 0, status: 'recorded', error: '' };
}

export function useFieldDrafts(fields: Field[]) {
  const [drafts, setDrafts] = useState<Record<string, FieldDraft>>({});
  const current = useRef(drafts);
  current.current = drafts;

  function get(field: Field): FieldDraft {
    const draft = current.current[field.id];
    // Confirmed values may change in a later browser action. Only local edits are sticky.
    if (
      (draft?.status === 'confirmed' || draft?.status === 'recorded')
      && draft.value !== (field.value ?? '')
    ) {
      return { ...recordedDraft(field), reason: draft.reason, version: draft.version };
    }
    return draft ?? recordedDraft(field);
  }

  function update(field: Field, transform: (draft: FieldDraft) => FieldDraft) {
    const next = { ...current.current, [field.id]: transform(get(field)) };
    current.current = next;
    setDrafts(next);
  }

  function edit(field: Field, changes: Partial<Pick<FieldDraft, 'value' | 'reason'>>) {
    update(field, draft => ({
      ...draft,
      ...changes,
      version: draft.version + 1,
      status: draft.status === 'applying' ? 'applying' : 'unsaved',
      error: '',
    }));
  }

  function begin(field: Field) {
    const snapshot = get(field);
    update(field, draft => ({ ...draft, status: 'applying', error: '' }));
    return snapshot;
  }

  function confirm(field: Field, value: string, version: number) {
    update(field, draft => {
      if (draft.version !== version) {
        return { ...draft, status: 'unsaved', error: '' };
      }

      return { ...draft, value, status: 'confirmed', error: '' };
    });
  }

  function fail(field: Field, error: string) {
    update(field, draft => ({ ...draft, status: 'failed', error }));
  }

  function discard(field: Field) {
    update(field, draft => ({ ...recordedDraft(field), version: draft.version + 1 }));
  }

  const unsavedCount = fields.filter(field => {
    const status = get(field).status;
    return status === 'unsaved' || status === 'failed' || status === 'applying';
  }).length;

  return { get, edit, begin, confirm, fail, discard, unsavedCount };
}

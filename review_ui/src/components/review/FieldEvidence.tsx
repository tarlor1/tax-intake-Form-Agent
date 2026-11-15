import { fieldValue, type Citation, type Field, type SourceDocument } from '../../lib/api';
import { Button } from '@/components/ui/button';
import { NativeSelect } from '@/components/ui/native-select';

function originLabel(field: Field) {
  if (field.valueOrigin === 'ai') return 'AI-extracted draft · pending review';
  if (field.valueOrigin === 'workspace') return 'Workspace identity · not source evidence';
  if (field.valueOrigin === 'preparer') return 'Preparer entered in browser';
  if (field.valueOrigin === 'calculated') return 'Historical calculation';
  return field.citations.length ? 'Source backed' : 'No source evidence';
}

interface Props {
  fields: Field[];
  legacyFields: Field[];
  documents: SourceDocument[];
  field?: Field;
  legacy: boolean;
  disabled: boolean;
  onSelect: (fieldId: string) => void;
  onChooseSource: (field: Field, candidateId: string) => void;
  onViewCitation: (citation: Citation) => void;
  onSelectDocument: (documentId: string) => void;
}

export default function FieldEvidence({
  fields,
  legacyFields,
  documents,
  field,
  legacy,
  disabled,
  onSelect,
  onChooseSource,
  onViewCitation,
  onSelectDocument,
}: Props) {
  function citationButton(source: Citation) {
    return (
      <Button
        variant="link"
        className="mt-2 block h-auto w-full whitespace-normal p-0 text-left text-xs leading-relaxed [overflow-wrap:anywhere]"
        key={source.id}
        onClick={() => onViewCitation(source)}
      >
        {documents.find(document => document.id === source.documentId)?.title ?? 'Source document'} ·{' '}
        {source.lineStart ? `lines ${source.lineStart}–${source.lineEnd ?? source.lineStart}` : `page ${source.page}`}
        <q className="block font-normal text-muted-foreground">{source.quote}</q>
      </Button>
    );
  }

  return (
    <section className="field-evidence border-b border-border bg-card p-[18px]" aria-label="Field navigation and provenance">
      <label className="block text-xs font-semibold" htmlFor="field-navigation">Field evidence</label>
      <NativeSelect
        className="mt-2"
        id="field-navigation"
        value={field?.id ?? ''}
        onChange={event => onSelect(event.target.value)}
      >
        <option value="" disabled>Choose a field to inspect</option>
        {[...new Set(fields.map(item => item.section))].map(section => (
          <optgroup label={section} key={section}>
            {fields.filter(item => item.section === section).map(item => (
              <option key={item.id} value={item.id}>
                {item.label}{item.required ? ' *' : ''}
              </option>
            ))}
          </optgroup>
        ))}
        {!!legacyFields.length && (
          <optgroup label="Historical worksheet facts · read only">
            {legacyFields.map(item => (
              <option key={item.id} value={item.id}>{item.label}</option>
            ))}
          </optgroup>
        )}
      </NativeSelect>
      <p className="mt-2.5 text-xs leading-relaxed text-muted-foreground">Choose evidence deliberately. Browser polling keeps your selection and local edits in place.</p>

      {field && (
        <>
          <h3 className="mt-5 text-[15px] font-semibold">
            {field.label}{legacy ? ' · historical fact' : ''}
          </h3>
          <p className="observed-fact mt-2.5 [overflow-wrap:anywhere]">
            <strong>{fieldValue(field)}</strong>
          </p>
          <p className="mt-2.5 text-xs leading-relaxed text-muted-foreground">
            Locally recorded · {originLabel(field)}{field.correctionReason ? ` · ${field.correctionReason}` : ''}
          </p>
          {legacy && (
            <p className="mt-3 rounded-md border border-border bg-muted p-3 text-xs leading-relaxed">
              Preserved from the old worksheet. Read-only historical evidence; not an input in the original form.
            </p>
          )}
          {field.conflict && (
            <p className="mt-2 text-xs text-[#8d561c]">
              Conflicting values. Review the candidates before using a value.
            </p>
          )}

          {!!field.candidates.length && (
            <ul className="mt-3.5 grid list-none gap-2 p-0" aria-label={`Value candidates for ${field.label}`}>
              {field.candidates.map(candidate => (
                <li key={candidate.id} className={`border-t pt-2.5 ${field.selectedCandidateId === candidate.id ? 'border-[#9bbda4]' : 'border-border'}`}>
                  <div className="flex items-center justify-between gap-3">
                    <strong className="text-[13px] [overflow-wrap:anywhere]">
                      {fieldValue({ kind: field.kind, value: candidate.value })}
                    </strong>
                    {!legacy && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="shrink-0"
                        disabled={disabled}
                        onClick={() => onChooseSource(field, candidate.id)}
                      >
                        Use in form
                      </Button>
                    )}
                  </div>
                  <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                    {candidate.provenance === 'gemini'
                      ? 'AI-extracted draft · pending review'
                      : candidate.citations.length
                        ? 'Source-cited value'
                        : candidate.provenance === 'human'
                          ? 'Manually entered value'
                          : 'Recorded value'}
                  </p>
                  {candidate.documentId && !candidate.citations.length && (
                    <Button
                      variant="link"
                      className="mt-2 block h-auto w-full whitespace-normal p-0 text-left text-xs leading-relaxed"
                      onClick={() => {
                        if (candidate.documentId) onSelectDocument(candidate.documentId);
                      }}
                    >
                      Open original document · {documents.find(document => document.id === candidate.documentId)?.title ?? 'Source document'}
                    </Button>
                  )}
                  {field.selectedCandidateId === candidate.id && (
                    <p className="mt-2 text-xs text-muted-foreground">
                      Recorded selection · verify actual form
                    </p>
                  )}
                  {candidate.citations.map(citationButton)}
                  {candidate.reason && (
                    <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                      {candidate.reason}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
          {!field.candidates.length && field.citations.map(citationButton)}
        </>
      )}
    </section>
  );
}

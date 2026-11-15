import type { Field } from '../../lib/api';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { NativeSelect } from '@/components/ui/native-select';
import { Textarea } from '@/components/ui/textarea';
import type { FieldDraft } from './useFieldDrafts';

interface ControlProps {
  field: Field;
  value: string;
  onChange: (value: string) => void;
}

function FieldControl({ field, value, onChange }: ControlProps) {
  const id = `field-${field.id}`;
  const describedBy = `${id}-status`;

  if (field.kind === 'multi') {
    let selected: string[] = [];
    try {
      const parsed: unknown = value ? JSON.parse(value) : [];
      if (Array.isArray(parsed) && parsed.every(item => typeof item === 'string')) {
        selected = parsed;
      }
    } catch {
      // A historical malformed value remains visible below until the reviewer replaces it.
    }

    return (
      <fieldset aria-describedby={describedBy} className="grid gap-2.5 rounded-md border border-input p-3">
        <legend className="sr-only">{field.label}</legend>
        {field.options.map(option => (
          <label className="flex items-center gap-2 text-sm" key={option.value}>
            <Checkbox
              id={`${id}-${option.value}`}
              checked={selected.includes(option.value)}
              onCheckedChange={checked => {
                const values = new Set(selected);
                if (checked === true) {
                  values.add(option.value);
                } else {
                  values.delete(option.value);
                }

                const orderedValues = field.options
                  .filter(item => values.has(item.value))
                  .map(item => item.value);
                onChange(JSON.stringify(orderedValues));
              }}
            />
            {option.label}
          </label>
        ))}
        {!!value && !selected.length && value !== '[]' && (
          <p className="text-xs text-destructive">Recorded value: {value}. Choose options to replace it.</p>
        )}
      </fieldset>
    );
  }

  if (field.kind === 'select') {
    return (
      <NativeSelect
        id={id}
        value={value}
        onChange={event => onChange(event.target.value)}
        aria-describedby={describedBy}
        aria-required={field.required}
      >
        <option value="">Choose / clear</option>
        {!!value && !field.options.some(option => option.value === value) && (
          <option value={value}>{value} (recorded)</option>
        )}
        {field.options.map(option => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </NativeSelect>
    );
  }

  if (field.controlType === 'textarea') {
    return (
      <Textarea
        id={id}
        rows={3}
        value={value}
        maxLength={10000}
        aria-describedby={describedBy}
        aria-required={field.required}
        onChange={event => onChange(event.target.value)}
      />
    );
  }

  let type = field.controlType === 'email' || field.controlType === 'tel'
    ? field.controlType
    : 'text';
  if (field.kind === 'date') {
    type = 'date';
  } else if (field.kind === 'integer' || field.kind === 'money') {
    type = 'number';
  }

  return (
    <Input
      id={id}
      type={type}
      min={field.min}
      max={field.max}
      step={field.kind === 'integer' ? 1 : field.kind === 'money' ? '0.01' : undefined}
      value={value}
      maxLength={10000}
      aria-describedby={describedBy}
      aria-required={field.required}
      onChange={event => onChange(event.target.value)}
    />
  );
}

const statusLabels = {
  recorded: 'Recorded value · not browser-confirmed here',
  unsaved: 'Unsaved local changes',
  applying: 'Applying · waiting for browser readback',
  confirmed: 'Confirmed from browser',
  failed: 'Apply failed · local value preserved',
};

interface Props {
  fields: Field[];
  selectedFieldId?: string;
  disabled: boolean;
  getDraft: (field: Field) => FieldDraft;
  onEdit: (field: Field, changes: Partial<Pick<FieldDraft, 'value' | 'reason'>>) => void;
  onApply: (field: Field) => void;
  onDiscard: (field: Field) => void;
  onSelect: (fieldId: string) => void;
}

function FieldEditor({
  field,
  draft,
  selected,
  disabled,
  onEdit,
  onApply,
  onDiscard,
  onSelect,
}: {
  field: Field;
  draft: FieldDraft;
  selected: boolean;
  disabled: boolean;
  onEdit: Props['onEdit'];
  onApply: Props['onApply'];
  onDiscard: Props['onDiscard'];
  onSelect: Props['onSelect'];
}) {
  return (
    <div
      role="group"
      aria-label={field.label}
      className={`min-w-0 rounded-lg border p-3.5 ${selected ? 'border-primary bg-accent/30' : 'border-border bg-card'}`}
      onFocusCapture={() => {
        if (!selected) onSelect(field.id);
      }}
    >
      <div className="mb-2 flex items-start justify-between gap-3">
        <label className="text-sm font-semibold" htmlFor={field.kind === 'multi' ? undefined : `field-${field.id}`}>
          {field.label}{field.required ? ' *' : ''}
        </label>
        <Button
          variant="link"
          size="sm"
          className="h-auto shrink-0 p-0 text-xs"
          onClick={() => onSelect(field.id)}
          aria-pressed={selected}
        >
          Evidence{field.conflict ? ' · conflict' : ''}
        </Button>
      </div>
      <FieldControl field={field} value={draft.value} onChange={value => onEdit(field, { value })} />
      <p
        id={`field-${field.id}-status`}
        className={`mt-2 text-xs ${draft.status === 'failed' ? 'text-destructive' : 'text-muted-foreground'}`}
        role="status"
      >
        {statusLabels[draft.status]}
      </p>
      {draft.error && (
        <p className="mt-1 text-xs text-destructive" role="alert">
          {draft.error}
        </p>
      )}

      <details className="mt-2 text-xs">
        <summary className="cursor-pointer text-muted-foreground">
          Reason / provenance note (optional){draft.reason ? ' · added' : ''}
        </summary>
        <label className="sr-only" htmlFor={`reason-${field.id}`}>
          Reason for {field.label}
        </label>
        <Textarea
          id={`reason-${field.id}`}
          className="mt-2"
          rows={2}
          maxLength={2000}
          value={draft.reason}
          onChange={event => onEdit(field, { reason: event.target.value })}
        />
      </details>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          aria-label={`Apply ${field.label}`}
          disabled={disabled || draft.status === 'applying'}
          onClick={() => onApply(field)}
        >
          {draft.status === 'applying' ? 'Applying…' : 'Apply to form'}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={draft.status === 'applying'}
          onClick={() => onEdit(field, { value: '' })}
        >
          Clear
        </Button>
        {(draft.status === 'unsaved' || draft.status === 'failed') && (
          <Button variant="ghost" size="sm" onClick={() => onDiscard(field)}>
            Discard local changes
          </Button>
        )}
      </div>
    </div>
  );
}

export default function FieldEditors({
  fields,
  selectedFieldId,
  disabled,
  getDraft,
  onEdit,
  onApply,
  onDiscard,
  onSelect,
}: Props) {
  const sections = [...new Set(fields.map(field => field.section))];

  return (
    <section className="min-w-0" aria-labelledby="editable-fields-heading">
      <h2 id="editable-fields-heading" className="text-lg font-semibold">Editable form fields</h2>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        All current form fields are below. Type locally, review the evidence, then apply each whole field to the actual browser. Polling preserves your unapplied edits. * Required in the original form.
      </p>
      {disabled && (
        <p className="mt-2 text-xs text-muted-foreground">
          You can edit now. Applying is available when the intake browser is ready and no action is pending.
        </p>
      )}

      {sections.map(section => (
        <section key={section} className="mt-5" aria-label={section}>
          <h3 className="mb-3 border-b border-border pb-2 text-sm font-semibold">{section}</h3>
          <div className="grid gap-3 min-[1400px]:grid-cols-2">
            {fields.filter(field => field.section === section).map(field => (
              <FieldEditor
                key={field.id}
                field={field}
                draft={getDraft(field)}
                selected={selectedFieldId === field.id}
                disabled={disabled}
                onEdit={onEdit}
                onApply={onApply}
                onDiscard={onDiscard}
                onSelect={onSelect}
              />
            ))}
          </div>
        </section>
      ))}
    </section>
  );
}

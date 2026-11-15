import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';

interface Props {
  disabled: boolean;
  submitted: boolean;
  unsavedCount: number;
  onSubmit: () => void;
}

export default function BrowserSubmit({ disabled, submitted, unsavedCount, onSubmit }: Props) {
  const [confirmed, setConfirmed] = useState(false);

  if (submitted) return null;

  return (
    <section className="mt-4 rounded-lg border border-border bg-card p-4" aria-labelledby="submit-browser-heading">
      <h2 id="submit-browser-heading" className="text-sm font-semibold">Submit the original intake form</h2>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        Review the fields and view-only browser preview first. This clicks the original form’s Submit button in the actual browser. It does not submit to TaxSlayer.
      </p>
      {unsavedCount > 0 && (
        <p className="mt-2 text-xs text-muted-foreground">
          Apply or discard {unsavedCount} local field change{unsavedCount === 1 ? '' : 's'} before submitting.
        </p>
      )}
      <label className="mt-3 flex items-start gap-2 text-xs leading-relaxed">
        <Checkbox
          className="mt-0.5"
          checked={confirmed}
          disabled={disabled || unsavedCount > 0}
          onCheckedChange={value => setConfirmed(value === true)}
        />
        I have reviewed the original intake and want to submit it now.
      </label>
      <Button
        className="mt-3"
        disabled={disabled || unsavedCount > 0 || !confirmed}
        onClick={() => {
          setConfirmed(false);
          onSubmit();
        }}
      >
        Submit original form
      </Button>
    </section>
  );
}

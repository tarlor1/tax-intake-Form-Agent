import { useId } from 'react';
import { Alert } from '@/components/ui/alert';
import { Checkbox } from '@/components/ui/checkbox';

interface AIConsentProps {
  configured: boolean;
  model: string;
  approved: boolean;
  onChange: (approved: boolean) => void;
}

export function AIConsent({ configured, model, approved, onChange }: AIConsentProps) {
  const consentId = useId();

  return (
    <div className="my-4 space-y-3">
      {!configured && (
        <Alert variant="warning">
          Extraction is not configured. Set GEMINI_API_KEY in the project-root .env file, then
          restart the server. Uploaded documents remain saved locally.
        </Alert>
      )}
      <div className="flex items-start gap-3">
        <Checkbox
          id={consentId}
          checked={approved}
          onCheckedChange={checked => onChange(checked === true)}
          className="mt-0.5 shrink-0"
        />
        <label htmlFor={consentId} className="cursor-pointer text-xs leading-relaxed">
          I approve sending only these synthetic or redacted documents to external Google Gemini
          {model ? ` (${model})` : ''} for extraction. Do not send live taxpayer data.
        </label>
      </div>
    </div>
  );
}

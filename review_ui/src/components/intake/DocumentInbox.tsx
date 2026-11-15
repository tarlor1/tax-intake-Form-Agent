import type { ReturnSummary } from '@/lib/api';
import type { DocumentInboxState } from '@/hooks/useDocumentInbox';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { AIConsent } from './AIConsent';
import { DocumentUpload } from './DocumentUpload';
import { InboxDocumentCard } from './InboxDocumentCard';

interface DocumentInboxProps {
  returns: ReturnSummary[];
  inbox: DocumentInboxState;
}

export default function DocumentInbox({ returns, inbox }: DocumentInboxProps) {
  const extractionDisabled =
    !inbox.state?.configured || !inbox.approved || inbox.processing || inbox.uploading;

  return (
    <DocumentUpload dropzone={inbox} disabled={inbox.uploading} progress={inbox.uploadProgress}>
      {inbox.state && (
        <AIConsent
          configured={inbox.state.configured}
          model={inbox.state.model}
          approved={inbox.approved}
          onChange={inbox.setApproved}
        />
      )}
      <p id="extraction-readiness" className="my-3 text-sm text-muted-foreground" role="status">
        {inbox.extractionReadiness}
      </p>
      {(inbox.processing || inbox.extracting.length > 0) && (
        <div className="my-4 space-y-3" role="status" aria-live="polite">
          <p className="text-sm text-muted-foreground">
            {inbox.processing ? 'Starting extraction…' : `Documents processing: ${inbox.extracting.length}`}
          </p>
          <Progress aria-label="Extracting uploaded documents" value={null} />
        </div>
      )}
      <div className="mt-4 flex flex-wrap justify-end gap-2">
        <Button
          type="button"
          aria-describedby="extraction-readiness"
          disabled={extractionDisabled || !inbox.pending.length}
          onClick={() => void inbox.process(inbox.pending)}
        >
          {inbox.processing ? 'Scheduling extraction…' : 'Extract uploaded documents'}
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={inbox.uploading || inbox.processing}
          onClick={() => void inbox.refresh()}
        >
          Refresh inbox
        </Button>
      </div>
      {inbox.error && <Alert className="mt-4" variant="destructive">{inbox.error}</Alert>}
      <ul className="mt-5 grid list-none gap-3 p-0" aria-live="polite">
        {inbox.localFiles.map(entry => (
          <li key={entry.id} className="space-y-3 rounded-lg border p-4 wrap-anywhere">
            <div className="flex flex-wrap justify-between gap-3">
              <strong>{entry.file.name}</strong>
              <span className="text-xs text-muted-foreground">{entry.status}</span>
            </div>
            {entry.error && (
              <>
                <Alert variant="destructive">{entry.error}</Alert>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={inbox.uploading}
                  onClick={() => inbox.retryUpload(entry.id)}
                >
                  Retry upload
                </Button>
              </>
            )}
          </li>
        ))}
        {inbox.state?.documents.map(document => (
          <InboxDocumentCard
            key={document.id}
            document={document}
            returns={returns}
            extractionDisabled={extractionDisabled}
            onExtract={inbox.process}
            onAssigned={inbox.updateDocument}
          />
        ))}
      </ul>
    </DocumentUpload>
  );
}

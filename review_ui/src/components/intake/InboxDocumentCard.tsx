import { Link } from '@tanstack/react-router';
import type { InboxDocument, ReturnSummary } from '@/lib/api';
import { Alert } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Assignment } from './Assignment';

interface InboxDocumentCardProps {
  document: InboxDocument;
  returns: ReturnSummary[];
  extractionDisabled: boolean;
  onExtract: (ids: string[]) => Promise<void>;
  onAssigned: (document: InboxDocument) => void;
}

const documentStatuses = {
  uploaded: 'Saved locally · extraction needed',
  processing: 'Extracting…',
  needs_assignment: 'Assignment needed',
  completed: 'Extracted',
  failed: 'Extraction failed',
};

export function InboxDocumentCard({
  document,
  returns,
  extractionDisabled,
  onExtract,
  onAssigned,
}: InboxDocumentCardProps) {
  return (
    <li className="space-y-3 rounded-lg border p-4 wrap-anywhere">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <strong>{document.filename}</strong>
        <Badge variant={document.status === 'failed' ? 'destructive' : 'secondary'}>
          {documentStatuses[document.status]}
        </Badge>
      </div>
      {document.error && <Alert variant="destructive">{document.error}</Alert>}
      {['uploaded', 'failed'].includes(document.status) && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={extractionDisabled}
          aria-describedby="extraction-readiness"
          onClick={() => void onExtract([document.id])}
        >
          {document.status === 'failed' ? 'Retry extraction' : 'Extract this file'}
        </Button>
      )}
      {document.groups.map(group => group.status === 'needs_assignment' ? (
        <Assignment
          key={group.id}
          documentId={document.id}
          group={group}
          returns={returns}
          onAssigned={onAssigned}
        />
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-3 pt-3 text-xs" key={group.id}>
          <span>{group.clientName ?? 'Assigned client'} · {group.taxYear ?? 'Year not identified'}</span>
          {group.clientId && group.taxYear && group.returnId && (
            <Button asChild variant="link" size="sm">
              <Link
                to="/clients/$clientId/returns/$taxYear"
                params={{ clientId: group.clientId, taxYear: String(group.taxYear) }}
                search={{}}
              >
                Review fields ↗
              </Link>
            </Button>
          )}
          {group.issues.length > 0 && (
            <ul className="w-full list-disc space-y-1 pl-5 text-amber-800">
              {group.issues.map((issue, index) => <li key={index}>{issue}</li>)}
            </ul>
          )}
        </div>
      ))}
    </li>
  );
}

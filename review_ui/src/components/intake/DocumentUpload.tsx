import type { ReactNode } from 'react';
import type { DropzoneState } from 'react-dropzone';
import type { UploadProgress } from '@/hooks/useDocumentInbox';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';

interface DocumentUploadProps {
  dropzone: Pick<DropzoneState, 'getInputProps' | 'open' | 'isDragActive'>;
  disabled: boolean;
  progress: UploadProgress | null;
  children: ReactNode;
}

export function DocumentUpload({ dropzone, disabled, progress, children }: DocumentUploadProps) {
  return (
    <section
      className="document-inbox mb-8 rounded-xl border bg-card p-4 text-card-foreground sm:p-6"
      aria-labelledby="inbox-heading"
    >
      <header className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h2 id="inbox-heading" className="text-lg font-semibold">Document inbox</h2>
        <span className="text-xs text-muted-foreground">PDF + TXT · multiple clients</span>
      </header>
      <div className={cn(
        'document-dropzone rounded-lg border-2 border-dashed border-primary/30 bg-muted/40 p-6 transition-colors',
        dropzone.isDragActive && 'border-primary bg-accent',
      )}>
        <strong>Drop PDF or TXT files anywhere on this Clients page</strong>
        <p className="mt-2 mb-4 text-xs leading-relaxed text-muted-foreground">
          No client name or tax year needed yet. Use selectable-text PDFs or UTF-8 text; scans and
          photos need OCR first.
        </p>
        <Button type="button" variant="outline" onClick={dropzone.open} disabled={disabled}>
          Select files
        </Button>
        <input {...dropzone.getInputProps({ 'aria-label': 'Select PDF or TXT documents' })} />
        {progress && (
          <div className="mt-4 space-y-3" role="status" aria-live="polite">
            <p className="text-sm text-muted-foreground">
              Saving and checking {progress.currentFile} · file {progress.completed + 1} of {progress.total}
            </p>
            <Progress
              aria-label="Uploading and validating documents"
              max={progress.total}
              value={progress.completed || null}
            />
          </div>
        )}
      </div>
      {children}
    </section>
  );
}

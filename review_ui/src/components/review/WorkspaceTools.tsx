import { Alert } from '@/components/ui/alert';
import { Input } from '@/components/ui/input';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { useRef, useState, type FormEvent } from 'react';
import { dateLabel, type ReturnDetail } from '../../lib/api';

interface Props {
  detail: ReturnDetail;
  disabled: boolean;
  onViewDocument: (documentId: string) => void;
  onUpload: (file: File) => Promise<boolean>;
}

export default function WorkspaceTools({ detail, disabled, onViewDocument, onUpload }: Props) {
  const [showSources, setShowSources] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [uploadError, setUploadError] = useState('');

  const fileInput = useRef<HTMLInputElement>(null);

  const history = [...detail.events].sort((a, b) => b.createdAt.localeCompare(a.createdAt));

  async function upload(event: FormEvent) {
    event.preventDefault();
    if (!file) return;

    if (!/\.(pdf|txt)$/i.test(file.name) || file.size === 0 || file.size > 10 * 1024 * 1024) {
      setUploadError('Choose a nonempty PDF or UTF-8 TXT file no larger than 10 MiB.');
      return;
    }

    setUploadError('');
    if (await onUpload(file)) {
      setFile(null);
      if (fileInput.current) fileInput.current.value = '';
    }
  }

  return (
    <div className="workspace-tools mt-[22px] grid items-start gap-5 min-[661px]:grid-cols-2 max-[980px]:gap-3.5 max-[660px]:mt-[17px] max-[660px]:gap-3">
      <Card className="tool-section overflow-hidden rounded-lg">
        <Button variant="outline"
          className="disclosure-button h-auto w-full justify-between rounded-none border-0 bg-card px-[22px] py-[18px] text-left whitespace-normal max-[660px]:p-[17px] [&_strong]:block [&_strong]:text-xs [&_small]:mt-1 [&_small]:block [&_small]:text-[10px] [&_small]:font-normal [&_small]:text-muted-foreground [&>span:last-child]:text-lg [&>span:last-child]:text-muted-foreground"
          aria-expanded={showSources}
          aria-controls="source-library"
          onClick={() => setShowSources(value => !value)}
        >
          <span>
            <strong>Source library</strong>
            <small>{detail.documents.length} local documents · add PDF or TXT</small>
          </span>
          <span aria-hidden="true">{showSources ? '−' : '+'}</span>
        </Button>
        {showSources && (
          <div id="source-library" className="tool-content border-t border-border px-[22px] pb-[22px] max-[660px]:px-[17px] max-[660px]:pb-[17px]">
            <ul className="document-list m-0 list-none p-0 [&>li]:flex [&>li]:items-center [&>li]:gap-[11px] [&>li]:border-b [&>li]:border-border [&>li]:py-3.5 [&_strong]:text-[11px] [&_small]:mt-[3px] [&_small]:block [&_small]:text-[9px] [&_small]:text-muted-foreground [&_button]:ml-auto [&_button]:shrink-0 [&_button]:text-[10px]">
              {detail.documents.map(document => (
                <li key={document.id}>
                  <span aria-hidden="true">▤</span>
                  <div>
                    <strong>{document.title}</strong>
                    <small>
                      {document.mediaType === 'text/plain'
                        ? 'TXT'
                        : `${document.pageCount} page${document.pageCount === 1 ? '' : 's'}`}{' '}
                      · version {document.version}
                    </small>
                  </div>
                  <Button variant="link" className="h-auto p-0 text-xs" onClick={() => onViewDocument(document.id)}>
                    View source ↗
                  </Button>
                </li>
              ))}
            </ul>
            {detail.documents.length === 0 && (
              <p className="text-muted-foreground">No source documents yet. Add PDF or TXT documents below.</p>
            )}
            <form className="upload-source mt-5 rounded-md border border-border bg-[#f2f7f3] p-[18px] [&_h3]:text-sm [&_h3]:font-semibold [&>p]:my-3 [&>p]:text-[11px] [&>p]:text-muted-foreground [&_label]:mb-[7px] [&_label]:block [&_label]:text-xs [&_label]:font-semibold [&_button]:mt-3" onSubmit={event => void upload(event)}>
              <h3>Add a source document</h3>
              <p>
                Selectable-text PDF or UTF-8 TXT, maximum 10 MiB. After upload, use Extract
                documents with your explicit approval. Scans and photos need OCR first.
              </p>
              <label htmlFor="source-upload">
                PDF or TXT for {detail.clientName}, tax year {detail.taxYear}
              </label>
              <Input
                ref={fileInput}
                id="source-upload"
                type="file"
                accept=".pdf,.txt,application/pdf,text/plain"
                disabled={disabled}
                onChange={event => {
                  setFile(event.target.files?.[0] ?? null);
                  setUploadError('');
                }}
              />
              {uploadError && (
                <Alert variant="destructive" className="rounded-md border border-[#edccbd] bg-[#fff0eb] px-4 py-[13px] text-xs leading-relaxed text-[#93472f]" role="alert">
                  {uploadError}
                </Alert>
              )}
              <Button type="submit" disabled={disabled || !file}>
                Upload document
              </Button>
            </form>
          </div>
        )}
      </Card>
      <Card className="tool-section overflow-hidden rounded-lg">
        <Button variant="outline"
          className="disclosure-button h-auto w-full justify-between rounded-none border-0 bg-card px-[22px] py-[18px] text-left whitespace-normal max-[660px]:p-[17px] [&_strong]:block [&_strong]:text-xs [&_small]:mt-1 [&_small]:block [&_small]:text-[10px] [&_small]:font-normal [&_small]:text-muted-foreground [&>span:last-child]:text-lg [&>span:last-child]:text-muted-foreground"
          aria-expanded={showHistory}
          aria-controls="review-history"
          onClick={() => setShowHistory(value => !value)}
        >
          <span>
            <strong>Decision history</strong>
            <small>{detail.events.length} saved events · retained across sessions</small>
          </span>
          <span aria-hidden="true">{showHistory ? '−' : '+'}</span>
        </Button>
        {showHistory && (
          <div id="review-history" className="tool-content border-t border-border px-[22px] pb-[22px] max-[660px]:px-[17px] max-[660px]:pb-[17px]">
            <ol className="history-list mt-[18px] max-h-[420px] list-none overflow-y-auto p-0 [&>li]:relative [&>li]:flex [&>li]:gap-3 [&>li]:pb-[18px] [&_p]:text-[11px] [&_p]:text-[#6f8161] [&_p]:[overflow-wrap:anywhere] [&_small]:mt-1 [&_small]:flex [&_small]:gap-1.5 [&_small]:text-[9px] [&_small]:text-muted-foreground">
              {history.map(event => (
                <li key={event.id}>
                  <span className="history-dot mt-1.5 size-1.5 shrink-0 rounded-full bg-[#abc39a]" />
                  <div>
                    <p>{event.message}</p>
                    <small>
                      <time dateTime={event.createdAt}>{dateLabel(event.createdAt)}</time>
                      <span>·</span>
                      {event.kind.replaceAll('_', ' ')}
                    </small>
                  </div>
                </li>
              ))}
            </ol>
            {history.length === 0 && (
              <p className="text-muted-foreground">No review decisions have been recorded yet.</p>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}

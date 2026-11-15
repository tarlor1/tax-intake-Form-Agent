import type { Citation, Field, SourceDocument } from '../../lib/api';
import DocumentViewer from './DocumentViewer';
import TextDocumentViewer from './TextDocumentViewer';
import { Badge } from '@/components/ui/badge';
import { NativeSelect } from '@/components/ui/native-select';

interface Props {
  returnId: string;
  documents: SourceDocument[];
  selectedDocument: SourceDocument | undefined;
  citation: Citation | null;
  field: Field | undefined;
  onSelectDocument: (documentId: string) => void;
}

export default function SourcePanel({
  returnId,
  documents,
  selectedDocument,
  citation,
  field,
  onSelectDocument,
}: Props) {
  return (
    <section className="flex min-w-0 flex-col bg-[#fcfdfa]" aria-labelledby="source-panel-heading">
      <div className="flex items-center justify-between gap-2.5 px-[17px] pb-[15px] pt-5 min-[661px]:pt-[22px] min-[1191px]:px-[22px]">
        <div>
          <p className="text-[8px] font-bold tracking-[1.25px] text-muted-foreground">SOURCE DOCUMENTS</p>
          <h2 className="mt-1 text-base font-semibold tracking-[-.3px]" id="source-panel-heading">Source document</h2>
        </div>
        <Badge variant="secondary" className="whitespace-nowrap text-[9px]">
          {selectedDocument?.mediaType === 'text/plain' ? 'Local TXT' : 'Local PDF'}
        </Badge>
      </div>
      <div className="px-[17px] pb-[17px] min-[661px]:pb-3.5 min-[1191px]:px-[22px]">
        <label className="mb-1.5 block text-[9px] font-semibold text-[#88977c]" htmlFor="source-document">Viewing source</label>
        <NativeSelect
          id="source-document"
          value={selectedDocument?.id ?? ''}
          onChange={event => onSelectDocument(event.target.value)}
        >
          {documents.map(document => (
            <option value={document.id} key={document.id}>
              {document.title}
            </option>
          ))}
        </NativeSelect>
      </div>
      <div className="flex min-h-[43px] items-center gap-[7px] border-y border-[#e0e8d6] bg-[#f0f5e9] px-[17px] py-3 text-[10px] text-[#7b8e69] min-[1191px]:px-[22px]">
        <span className="inline-block size-[7px] shrink-0 rounded-[2px] bg-[#cbba44]" />
        <span>
          {field ? (
            <>
              {citation ? 'Evidence for ' : 'Document context for '}<strong className="font-semibold text-[#5f7a4c]">{field.label}</strong>
              {citation ? '' : ' · No citation available for this field in the selected document.'}
            </>
          ) : (
            'Browsing document · no field selected'
          )}
        </span>
      </div>
      {selectedDocument?.mediaType === 'text/plain' ? (
        <TextDocumentViewer
          key={`${returnId}:${selectedDocument.id}:${selectedDocument.version}`}
          returnId={returnId}
          document={selectedDocument}
          citation={citation?.documentId === selectedDocument.id ? citation : null}
        />
      ) : selectedDocument ? (
        <DocumentViewer
          key={`${returnId}:${selectedDocument.id}:${selectedDocument.version}`}
          returnId={returnId}
          document={selectedDocument}
          citation={citation?.documentId === selectedDocument.id ? citation : null}
        />
      ) : (
        <div className="rounded-lg border border-border bg-card px-6 py-[50px] text-muted-foreground">No source documents are available.</div>
      )}
    </section>
  );
}

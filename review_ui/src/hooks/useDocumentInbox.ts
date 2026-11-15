import { useCallback, useEffect, useRef, useState } from 'react';
import { useDropzone, type DropzoneState } from 'react-dropzone';
import { api, errorMessage, type InboxDocument, type IntakeState } from '@/lib/api';

export interface UploadProgress {
  completed: number;
  total: number;
  currentFile: string;
}

export interface LocalUpload {
  id: number;
  file: File;
  status: string;
  error: string;
}

export interface DocumentInboxState extends Pick<
  DropzoneState, 'getRootProps' | 'getInputProps' | 'open' | 'isDragActive'
> {
  state: IntakeState | null;
  approved: boolean;
  setApproved: (approved: boolean) => void;
  uploading: boolean;
  processing: boolean;
  error: string;
  localFiles: LocalUpload[];
  uploadProgress: UploadProgress | null;
  retryUpload: (id: number) => void;
  refresh: () => Promise<void>;
  updateDocument: (document: InboxDocument) => void;
  process: (ids: string[]) => Promise<void>;
  pending: string[];
  extracting: InboxDocument[];
  extractionReadiness: string;
}

export function useDocumentInbox(onChanged: () => void): DocumentInboxState {
  const [state, setState] = useState<IntakeState | null>(null);
  const [approved, setApproved] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [error, setError] = useState('');
  const [localFiles, setLocalFiles] = useState<LocalUpload[]>([]);
  const [uploadProgress, setUploadProgress] = useState<UploadProgress | null>(null);

  const uploadingRef = useRef(false);
  const nextId = useRef(0);
  const fingerprint = useRef('');
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const result = await api.intake(signal);
      if (signal?.aborted) return;

      setState(result);
      const next = result.documents
        .flatMap(document =>
          document.groups
            .filter(group => group.returnId)
            .map(group => `${group.returnId}:${document.updatedAt}`),
        )
        .sort()
        .join('|');
      if (fingerprint.current !== next) {
        fingerprint.current = next;
        onChangedRef.current();
      }
    } catch (reason) {
      if (!signal?.aborted) setError(errorMessage(reason));
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;

    async function poll() {
      await load(controller.signal);
      if (!controller.signal.aborted) {
        timer = window.setTimeout(() => void poll(), 1600);
      }
    }

    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [load]);

  function updateDocument(document: InboxDocument) {
    setState(current =>
      current
        ? {
          ...current,
          documents: [document, ...current.documents.filter(item => item.id !== document.id)],
        }
        : current,
    );
    onChangedRef.current();
  }

  async function uploadFiles(files: File[]) {
    if (uploadingRef.current) {
      setError('An upload is already in progress. Wait for it to finish before adding more files.');
      return;
    }
    if (!files.length) {
      setError('No readable files were dropped. Use Select files to choose PDF or TXT documents.');
      return;
    }

    const entries = files.map(file => ({
      id: ++nextId.current,
      file,
      status: 'Waiting to upload',
      error: '',
    }));

    uploadingRef.current = true;
    setUploading(true);
    setError('');
    setLocalFiles(current => [...current, ...entries]);

    try {
      for (const [index, entry] of entries.entries()) {
        setUploadProgress({
          completed: index,
          total: entries.length,
          currentFile: entry.file.name,
        });
        const update = (status: string, message = '') =>
          setLocalFiles(current =>
            current.map(item => item.id === entry.id ? { ...item, status, error: message } : item),
          );

        if (
          !/\.(pdf|txt)$/i.test(entry.file.name) ||
          entry.file.size === 0 ||
          entry.file.size > 10 * 1024 * 1024
        ) {
          update('Upload failed', 'Choose a nonempty PDF or UTF-8 TXT file no larger than 10 MiB.');
          continue;
        }

        update('Uploading and checking readable text…');
        try {
          const document = await api.uploadInbox(entry.file);
          updateDocument(document);
          setLocalFiles(current => current.filter(item => item.id !== entry.id));
        } catch (reason) {
          update('Upload failed', errorMessage(reason));
        }
      }

      await load();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      uploadingRef.current = false;
      setUploading(false);
      setUploadProgress(null);
    }
  }

  const { getRootProps, getInputProps, open, isDragActive } = useDropzone({
    accept: {
      'application/pdf': ['.pdf'],
      'text/plain': ['.txt'],
    },
    minSize: 1,
    maxSize: 10 * 1024 * 1024,
    multiple: true,
    disabled: uploading,
    noClick: true,
    noKeyboard: true,
    preventDropOnDocument: true,
    onDrop: (files, rejectedFiles) => {
      if (rejectedFiles.length) {
        setLocalFiles(current => [
          ...current,
          ...rejectedFiles.map(({ file, errors }) => ({
            id: ++nextId.current,
            file,
            status: 'Upload failed',
            error: errors.map(error => error.message).join(' '),
          })),
        ]);
      }

      if (files.length) {
        void uploadFiles(files);
      }
    },
  });

  async function process(ids: string[]) {
    if (processing) return;
    if (!ids.length || !state?.configured || !approved) {
      setError(
        !ids.length
          ? 'No documents have been saved yet. Upload a PDF or TXT file first.'
          : !state?.configured
            ? 'Configure GEMINI_API_KEY and restart the backend before extracting.'
            : 'Check the approval box above before sending documents to Gemini.',
      );
      return;
    }

    setProcessing(true);
    setError('');

    try {
      const documents = await api.processDocuments(ids, approved);
      for (const document of documents) {
        updateDocument(document);
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setProcessing(false);
    }
  }

  const pending =
    state?.documents
      .filter(document => ['uploaded', 'failed'].includes(document.status))
      .map(document => document.id) ?? [];
  const extracting = state?.documents.filter(document => document.status === 'processing') ?? [];
  let extractionReadiness: string;
  if (uploading) {
    extractionReadiness = 'Wait for the files to finish uploading and checking.';
  } else if (processing) {
    extractionReadiness = 'Starting extraction. Please wait…';
  } else if (!state) {
    extractionReadiness = 'Loading extraction settings…';
  } else if (!pending.length) {
    extractionReadiness = extracting.length
      ? 'Extraction is running. Results will appear below.'
      : 'No files are waiting for extraction. Drop PDF or TXT files into the document inbox.';
  } else if (!state.configured) {
    extractionReadiness = 'Files are saved. Set GEMINI_API_KEY in .env and restart the backend to extract them.';
  } else if (!approved) {
    extractionReadiness = `Files saved: ${pending.length}. Check the approval box above to enable extraction.`;
  } else {
    extractionReadiness = `Files ready: ${pending.length}. Click Extract uploaded documents to identify clients and values.`;
  }

  function retryUpload(id: number) {
    const entry = localFiles.find(file => file.id === id);
    if (!entry || uploadingRef.current) return;

    setLocalFiles(current => current.filter(file => file.id !== id));
    void uploadFiles([entry.file]);
  }

  async function refresh() {
    setError('');
    await load();
  }

  return {
    state,
    approved,
    setApproved,
    uploading,
    processing,
    error,
    localFiles,
    uploadProgress,
    getRootProps,
    getInputProps,
    open,
    isDragActive,
    retryUpload,
    refresh,
    updateDocument,
    process,
    pending,
    extracting,
    extractionReadiness,
  };
}

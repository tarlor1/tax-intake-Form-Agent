import type { BatchFile, PracticeDraft } from './apiSchemas';

export const INBOX_REFRESH_MS = 1000;
export const PRACTICE_APPROVAL_CHANNEL = 'review-inbox-practice-approved';

export interface PracticeFieldSelection {
  href: string;
  field: string | null;
}

export interface PracticeDestination {
  url: URL;
  draft: string;
  section: string;
}

export function inboxFileIsPending(file: Pick<BatchFile, 'analysis' | 'practice'>) {
  const practice = file.practice;
  return file.analysis?.status === 'pending' || practice?.status === 'queued' ||
    practice?.status === 'preparing' || (practice?.status === 'waiting' && !practice.gated);
}

export function practiceDestination(practice: PracticeDraft | null | undefined): PracticeDestination | null {
  if (!practice?.url || (practice.status !== 'ready' && practice.status !== 'needs_review')) return null;
  try {
    const url = new URL(practice.url);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password ||
      url.origin === window.location.origin) return null;
    const path = /^\/drafts\/([0-9a-f]{32})\/(w2|1099-k|1099-nec)(?:\/([0-9a-f]{32}))?\/?$/.exec(url.pathname);
    if (!path || (path[2] === 'w2') !== Boolean(path[3])) return null;
    return { url, draft: path[1], section: path[2] };
  } catch {
    return null;
  }
}

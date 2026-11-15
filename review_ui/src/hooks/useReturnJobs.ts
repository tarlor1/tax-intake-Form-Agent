import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorMessage, type Job } from '../lib/api';

export function isActiveJob(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running' || job.status === 'cancelling';
}

export interface ReturnJobs {
  jobs: Job[];
  ready: boolean;
  busy: boolean;
  error: string;
  active: Job | undefined;
  refresh: (signal?: AbortSignal) => Promise<void>;
  start: (expectedRevision: number) => Promise<void>;
  cancel: (jobId: string) => Promise<void>;
  accept: (job: Job) => void;
}

export function useReturnJobs(returnId: string): ReturnJobs {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const mutationEpoch = useRef(0);

  const accept = useCallback(
    (job: Job) => {
      if (job.returnId !== returnId)
        throw new Error('Browser action returned a different return identity.');
      mutationEpoch.current += 1;
      setJobs(current => [job, ...current.filter(item => item.id !== job.id)]);
      setReady(true);
    },
    [returnId],
  );
  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      const epoch = mutationEpoch.current;
      try {
        const saved = await api.jobs(returnId, signal);
        if (signal?.aborted || epoch !== mutationEpoch.current) return;
        setJobs(saved);
        setReady(true);
        setError('');
      } catch (reason) {
        if (signal?.aborted || epoch !== mutationEpoch.current) return;
        setReady(false);
        setError(errorMessage(reason));
      }
    },
    [returnId],
  );

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    async function poll() {
      await refresh(controller.signal);
      if (!controller.signal.aborted) timer = window.setTimeout(() => void poll(), 1500);
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [refresh]);

  async function start(expectedRevision: number) {
    mutationEpoch.current += 1;
    setBusy(true);
    setError('');
    try {
      const job = await api.startJob(returnId, { expectedRevision });
      mutationEpoch.current += 1;
      setJobs(current => [job, ...current.filter(item => item.id !== job.id)]);
      setReady(true);
    } catch (reason) {
      setError(errorMessage(reason));
      await refresh();
      throw reason;
    } finally {
      setBusy(false);
    }
  }

  async function cancel(jobId: string) {
    mutationEpoch.current += 1;
    setBusy(true);
    setError('');
    try {
      const job = await api.cancelJob(returnId, jobId);
      mutationEpoch.current += 1;
      setJobs(current => current.map(item => (item.id === job.id ? job : item)));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return {
    jobs,
    ready,
    busy,
    error,
    refresh,
    start,
    cancel,
    accept,
    active: jobs.find(isActiveJob),
  };
}

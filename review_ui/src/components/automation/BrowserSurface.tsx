import { useEffect, useRef, useState } from 'react';
import { api, errorMessage, type Job } from '../../lib/api';
import { isActiveJob } from '../../hooks/useReturnJobs';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { NativeSelect } from '@/components/ui/native-select';

interface Props {
  job: Job;
  pending: boolean;
}

interface BrowserFrame {
  url: string;
  width: number;
  height: number;
}

export default function BrowserSurface({ job, pending }: Props) {
  const [frame, setFrame] = useState<BrowserFrame | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [zoom, setZoom] = useState('fit');

  const active = useRef(false);

  active.current = isActiveJob(job) || job.browserAvailable;

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let currentUrl = '';

    setFrame(null);
    setError('');

    async function poll() {
      try {
        const blob = await api.browserFrame(job.returnId, job.id, controller.signal);
        if (controller.signal.aborted) return;
        if (blob) {
          const next = URL.createObjectURL(blob);
          try {
            const decoded = new Image();
            decoded.src = next;
            await decoded.decode();
            if (controller.signal.aborted) {
              URL.revokeObjectURL(next);
              return;
            }
            const previous = currentUrl;
            currentUrl = next;
            setFrame({ url: next, width: decoded.naturalWidth, height: decoded.naturalHeight });
            if (previous) URL.revokeObjectURL(previous);
          } catch (reason) {
            URL.revokeObjectURL(next);
            throw reason;
          }
        }
        setError('');
      } catch (reason) {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      }

      if (!controller.signal.aborted && active.current)
        timer = window.setTimeout(() => void poll(), 1000);
    }

    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [job.id, job.returnId, retry]);

  return (
    <section className="overflow-hidden rounded-lg border border-border bg-[#f7faf6]" aria-labelledby="browser-heading">
      <div className="flex flex-wrap items-center justify-between gap-3 px-[18px] pb-2 pt-4 text-xs">
        <h2 className="text-lg font-semibold" id="browser-heading">Actual intake form</h2>
        <span>
          {job.completedFields} / {job.totalFields} fields filled
        </span>
      </div>
      <p className="px-[18px] pb-3 text-xs text-muted-foreground" role="status">
        {job.stage}
        {pending || job.actionPending ? ' · Applying field changes…' : ''}
      </p>
      <p id="browser-instructions" className="px-[18px] pb-3 text-xs text-muted-foreground">
        View-only actual browser image. Scroll here to inspect the full page; use the editable
        fields beside it to make changes and the separate submit control when ready.
        {' '}
        {job.browserAvailable
          ? 'The form is available for changes.'
          : isActiveJob(job)
            ? 'The browser is preparing the form.'
            : 'The browser is unavailable; this is its last retained image.'}
      </p>
      <div className="flex items-center gap-2 px-[18px] pb-3">
        <label htmlFor="browser-preview-zoom" className="shrink-0 text-xs font-medium">
          Preview zoom
        </label>
        <NativeSelect
          id="browser-preview-zoom"
          className="w-auto"
          value={zoom}
          onChange={event => setZoom(event.target.value)}
        >
          <option value="fit">Fit width</option>
          <option value="50">50%</option>
          <option value="75">75%</option>
          <option value="100">100%</option>
          <option value="125">125%</option>
        </NativeSelect>
      </div>
      <div
        id="actual-browser-viewport"
        className="relative max-h-[min(75vh,900px)] overflow-auto overscroll-contain outline-none focus-visible:outline-[3px] focus-visible:outline-offset-[-3px] focus-visible:outline-primary"
        tabIndex={0}
        role="region"
        aria-label="View-only external intake browser preview"
        aria-describedby="browser-instructions"
      >
        {frame ? (
          <img
            className={`block h-auto max-w-none select-none ${zoom === 'fit' ? 'w-full' : ''}`}
            style={zoom === 'fit' ? undefined : { width: frame.width * Number(zoom) / 100 }}
            src={frame.url}
            width={frame.width}
            height={frame.height}
            draggable={false}
            alt={`Actual external intake browser: ${job.stage}`}
          />
        ) : (
          <div className="grid min-h-[180px] place-items-center p-6 text-muted-foreground">
            {isActiveJob(job) || job.browserAvailable
              ? 'Waiting for the browser’s first frame…'
              : 'No browser frame is retained for this job.'}
          </div>
        )}
      </div>
      {error && (
        <Alert className="m-3.5 w-auto" variant="destructive" role="alert">
          Browser image: {error}
          <Button className="ml-3" variant="outline" size="sm" onClick={() => setRetry(value => value + 1)}>Retry image</Button>
        </Alert>
      )}
    </section>
  );
}

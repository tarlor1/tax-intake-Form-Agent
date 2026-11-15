import { Card } from '@/components/ui/card';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { dateLabel } from '../../lib/api';
import type { ReturnJobs } from '../../hooks/useReturnJobs';

interface Props {
  state: ReturnJobs;
  disabled: boolean;
  onStart: () => void;
}

export default function JobPanel({ state, disabled, onStart }: Props) {
  const ordered = [...state.jobs].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  const live = state.active ?? ordered.find(job => job.browserAvailable);

  return (
    <Card role="region" className="automation-panel mt-6 rounded-[10px] border border-border bg-card p-6 max-[660px]:p-[18px] [&>details]:mt-4 [&_summary]:cursor-pointer [&_summary]:rounded-sm [&_summary]:focus-visible:outline-2 [&_summary]:focus-visible:outline-ring" aria-labelledby="automation-heading">
      <div className="mb-[18px] flex items-center justify-between gap-3 max-[720px]:flex-col max-[720px]:items-start [&_h2]:text-base [&_h2]:font-semibold">
        <div>
          <p className="mb-1.5 text-[10px] font-bold tracking-[1.45px] text-muted-foreground">CONTROLLED EXTERNAL BROWSER</p>
          <h2 id="automation-heading">Open and fill intake form</h2>
        </div>
        <Button disabled={disabled || !state.ready || !!live} onClick={onStart}>
          {state.busy ? 'Requesting…' : 'Open and fill form'}
        </Button>
      </div>
      <p className="mt-2.5 text-[10px] leading-relaxed text-muted-foreground">
        Fills the original intake form, then hands the browser to you for review and submission.
      </p>
      {!state.ready && !state.error && <p role="status">Loading browser session state…</p>}
      {state.error && (
        <Alert variant="destructive" className="rounded-md border border-[#edccbd] bg-[#fff0eb] px-4 py-[13px] text-xs leading-relaxed text-[#93472f]" role="alert">
          <p>{state.error}</p>
          <Button variant="outline" onClick={() => void state.refresh()}>Reload job state</Button>
        </Alert>
      )}
      {live && (
        <div className="browser-status mt-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-[#f7faf6] px-4 py-3.5" role="status">
          {live.stage}
          <Button variant="outline"
            disabled={state.busy || live.status === 'cancelling'}
            onClick={() => {
              if (
                window.confirm(
                  live.browserAvailable
                    ? 'Close this browser? Unsubmitted form work exists only in this browser. Locally observed values are retained, but are not an external save.'
                    : 'Cancel this browser job?',
                )
              )
                void state.cancel(live.id);
            }}
          >
            {live.status === 'cancelling'
              ? 'Closing browser…'
              : live.browserAvailable
                ? 'Close browser'
                : 'Cancel job'}
          </Button>
        </div>
      )}
      {!!ordered.length && (
        <details>
          <summary>Browser history ({ordered.length})</summary>
          <ol className="job-list mt-5 grid max-h-[460px] list-none gap-3 overflow-auto p-0 [&>li]:rounded-md [&>li]:border [&>li]:border-border [&>li]:bg-[#fafcf9] [&>li]:p-4 [&_p]:text-xs [&_small]:mt-1.5 [&_small]:block [&_small]:text-[11px] [&_small]:text-muted-foreground [&_small]:[overflow-wrap:anywhere] [&_details]:mt-3 [&_details]:text-xs [&_ul]:list-disc [&_ul]:pl-5 [&_ul_li]:mt-1.5">
            {ordered.map(job => (
              <li key={job.id}>
                <div className="job-heading mb-[5px] flex justify-between gap-5 max-[660px]:flex-col max-[660px]:gap-1 [&_strong]:capitalize [&_span]:text-xs">
                  <strong>
                    {job.target === 'legacy-generated' ? 'Historical generated worksheet · ' : ''}
                    {job.status.replaceAll('_', ' ')}
                  </strong>
                  <span>
                    {job.completedFields} / {job.totalFields} fields{' '}
                    {job.target === 'legacy-generated' ? 'recorded historically' : 'filled'}
                  </span>
                </div>
                <p>{job.stage}</p>
                <small>{dateLabel(job.updatedAt)}</small>
                {job.target === 'intake-form' && (
                  <p>
                    {job.submitted && job.intakeId
                      ? `Actual form confirmation · intake ${job.intakeId}`
                      : 'No confirmed external submission'}{' '}
                    · {job.browserAvailable ? 'Browser available' : 'Browser unavailable'}
                  </p>
                )}
                {job.error && (
                  <Alert variant="destructive" className="rounded-md border border-[#edccbd] bg-[#fff0eb] px-4 py-[13px] text-xs leading-relaxed text-[#93472f]" role="alert">
                    {job.error}
                  </Alert>
                )}
                {job.actionError && (
                  <Alert variant="destructive" className="rounded-md border border-[#edccbd] bg-[#fff0eb] px-4 py-[13px] text-xs leading-relaxed text-[#93472f]" role="alert">
                    Browser action: {job.actionError}
                  </Alert>
                )}
                {!!job.issues.length && (
                  <details>
                    <summary>
                      {job.issues.length} preparation issue{job.issues.length === 1 ? '' : 's'}
                    </summary>
                    <ul>
                      {job.issues.map((issue, index) => (
                        <li key={`${index}:${issue}`}>{issue}</li>
                      ))}
                    </ul>
                  </details>
                )}
              </li>
            ))}
          </ol>
        </details>
      )}
    </Card>
  );
}

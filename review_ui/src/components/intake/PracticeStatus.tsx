import type { PracticeDraft } from '@/lib/apiSchemas';

const statusLabels = {
  waiting: 'Practice draft: waiting',
  queued: 'Practice draft: queued',
  preparing: 'Practice draft: preparing',
  ready: 'Practice draft: ready',
  needs_review: 'Practice draft: needs review',
  failed: 'Practice draft: failed',
} satisfies Record<PracticeDraft['status'], string>;

export default function PracticeStatus({ practice }: { practice: PracticeDraft }) {
  return (
    <div className="space-y-1 text-sm" role="status">
      <p className="font-medium">{statusLabels[practice.status]}</p>
      {practice.gated && <p className="text-muted-foreground">{practice.gated}</p>}
      {practice.message && practice.message !== practice.gated && (
        <p className="text-muted-foreground">{practice.message}</p>
      )}
    </div>
  );
}

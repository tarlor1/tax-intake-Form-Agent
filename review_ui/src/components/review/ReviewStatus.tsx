import { Progress as ProgressBar } from '@/components/ui/progress';

export function Progress({ filled, total }: { filled: number; total: number }) {
  return (
    <div className="progress-block mt-6">
      <div className="progress-label mb-[9px] flex items-baseline justify-between gap-2.5 text-[11px] text-muted-foreground">
        <span>Fields populated</span>
        <strong className="text-xs text-foreground">
          {filled} <span className="font-normal text-muted-foreground">/ {total}</span>
        </strong>
      </div>
      <ProgressBar
        className="h-[5px]"
        value={filled}
        max={total || 1}
        aria-label={`${filled} of ${total} fields populated`}
      />
    </div>
  );
}

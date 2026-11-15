import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Link, useRouter, type ErrorComponentProps } from '@tanstack/react-router';
import { errorMessage } from '../../lib/api';

export function RouteError({ error, reset }: ErrorComponentProps) {
  const router = useRouter();

  return (
    <main className="workspace-loading min-h-[70vh] px-[4%] py-[35px] [&>p]:mt-[30px] [&>div]:mt-[30px] [&_h1]:text-3xl [&_h1]:font-semibold [&_a]:text-primary [&_a]:underline [&_a]:underline-offset-4 [&_p]:my-3">
      <Link to="/clients" search={{}}>
        ← Client queue
      </Link>
      <Alert variant="destructive" className="rounded-md border border-[#edccbd] bg-[#fff0eb] px-4 py-[13px] text-xs leading-relaxed text-[#93472f]" role="alert">
        <h1>Unable to open this workspace</h1>
        <p>{errorMessage(error)}</p>
        <Button variant="outline"
          onClick={() => {
            void router.invalidate().then(reset);
          }}
        >
          Retry saved workspace
        </Button>
      </Alert>
    </main>
  );
}

export function RouteNotFound() {
  return (
    <main className="workspace-loading min-h-[70vh] px-[4%] py-[35px] [&>p]:mt-[30px] [&>div]:mt-[30px] [&_h1]:text-3xl [&_h1]:font-semibold [&_a]:text-primary [&_a]:underline [&_a]:underline-offset-4 [&_p]:my-3">
      <h1>Workspace, field, or filter not found</h1>
      <p>
        This URL does not identify an available workspace and field, or contains invalid queue
        filters. No other return has been opened.
      </p>
      <Link to="/clients" search={{}}>
        Choose an available client and tax year
      </Link>
    </main>
  );
}

export function RoutePending() {
  return (
    <main className="workspace-loading min-h-[70vh] px-[4%] py-[35px] [&>p]:mt-[30px] [&>div]:mt-[30px] [&_h1]:text-3xl [&_h1]:font-semibold [&_a]:text-primary [&_a]:underline [&_a]:underline-offset-4 [&_p]:my-3" role="status">
      Opening saved review…
    </main>
  );
}

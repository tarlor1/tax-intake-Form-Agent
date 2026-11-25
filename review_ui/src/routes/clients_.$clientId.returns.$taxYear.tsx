import { createFileRoute, notFound, useRouter, useRouterState } from '@tanstack/react-router';
import { z } from 'zod';
import { api } from '../lib/api';
import { returnSearchSchema } from '../lib/apiSchemas';
import EvidenceWorkspace from '../components/review/EvidenceWorkspace';

const routeParamsSchema = z.object({
  clientId: z.string().min(1).max(150),
  taxYear: z
    .string()
    .regex(/^\d{4}$/)
    .refine(year => Number(year) >= 1900 && Number(year) <= 2200),
});

export const Route = createFileRoute('/clients_/$clientId/returns/$taxYear')({
  params: {
    parse: params => {
      const parsed = routeParamsSchema.safeParse(params);
      if (!parsed.success) throw notFound();
      return parsed.data;
    },
  },
  validateSearch: search => {
    const parsed = returnSearchSchema.safeParse(search);
    if (!parsed.success) throw notFound();
    return parsed.data;
  },
  loaderDeps: ({ search }) => ({ revision: search.revision }),
  loader: async ({ params, deps, abortController }) => {
    const signal = abortController.signal;
    const summaries = await api.returns(signal);
    const matches = summaries.filter(
      entry => entry.clientId === params.clientId && String(entry.taxYear) === params.taxYear,
    );
    if (matches.length !== 1) throw notFound();

    const [{ id }] = matches;
    const [detail, jobs] = await Promise.all([api.evidence(id, deps.revision, signal), api.jobs(id, signal)]);
    if (detail.clientId !== params.clientId || String(detail.taxYear) !== params.taxYear) throw notFound();
    return { detail, jobs };
  },
  staleTime: Infinity,
  component: ReturnWorkspace,
});

function ReturnWorkspace() {
  const { detail, jobs } = Route.useLoaderData();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const router = useRouter();
  useRouterState({ select: state => state.location });
  const navigation = (window as Window & { navigation?: { canGoForward: boolean } }).navigation;

  return (
    <EvidenceWorkspace key={detail.id} detail={detail} search={search} jobs={jobs}
      refresh={async () => { await router.invalidate({ sync: true }); }}
      navigateView={async (view, replace) => { await navigate({ search: view, replace, resetScroll: false }); }}
      previous={() => router.history.back()}
      next={() => router.history.forward()}
      canGoBack={router.history.canGoBack()}
      canGoForward={navigation?.canGoForward ?? true} />
  );
}

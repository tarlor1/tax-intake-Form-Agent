import { createFileRoute, notFound } from '@tanstack/react-router';
import { z } from 'zod';
import { api } from '../lib/api';
import { returnSearchSchema } from '../lib/apiSchemas';
import ReviewWorkspace from '../components/review/ReviewWorkspace';

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
  loaderDeps: ({ search }) => ({ field: search.field }),
  loader: async ({ params, deps, abortController }) => {
    const summaries = await api.returns(abortController.signal);
    const matches = summaries.filter(
      item => item.clientId === params.clientId && String(item.taxYear) === params.taxYear,
    );
    if (matches.length !== 1) throw notFound();

    const summary = matches[0];
    const detail = await api.detail(summary.id, abortController.signal);
    if (detail.clientId !== params.clientId || String(detail.taxYear) !== params.taxYear)
      throw notFound();
    if (
      deps.field &&
      !detail.fields.some(field => field.id === deps.field) &&
      !detail.legacyFields.some(field => field.id === deps.field)
    ) {
      throw notFound();
    }

    return { detail, years: summaries.filter(item => item.clientId === params.clientId) };
  },
  staleTime: 0,
  component: ReturnWorkspace,
});

function ReturnWorkspace() {
  const { detail, years } = Route.useLoaderData();

  return <ReviewWorkspace key={detail.id} initialDetail={detail} years={years} />;
}

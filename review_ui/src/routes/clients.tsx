import { createFileRoute, notFound } from '@tanstack/react-router';
import { api } from '../lib/api';
import { queueSearchSchema } from '../lib/apiSchemas';
import ClientQueue from '../components/clients/ClientQueue';

export const Route = createFileRoute('/clients')({
  validateSearch: search => {
    const parsed = queueSearchSchema.safeParse(search);
    if (!parsed.success) throw notFound();
    return parsed.data;
  },
  loader: ({ abortController }) => api.returns(abortController.signal),
  staleTime: 0,
  component: ClientQueue,
});

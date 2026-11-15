import { Input } from '@/components/ui/input';
import { NativeSelect } from '@/components/ui/native-select';
import { Badge } from '@/components/ui/badge';
import { Card } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { getRouteApi, Link, useRouter } from '@tanstack/react-router';
import { dateLabel, type ReturnSummary } from '../../lib/api';
import DocumentInbox from '../intake/DocumentInbox';
import { Progress } from '../review/ReviewStatus';
import { useDocumentInbox } from '@/hooks/useDocumentInbox';

const queueRoute = getRouteApi('/clients');

export default function ClientQueue() {
  const returns = queueRoute.useLoaderData();
  const search = queueRoute.useSearch();
  const navigate = queueRoute.useNavigate();
  const router = useRouter();
  const inbox = useDocumentInbox(() => {
    void router.invalidate();
  });

  const query = (search.q ?? '').trim().toLocaleLowerCase();
  const years = [...new Set(returns.map(item => item.taxYear))].sort((a, b) => b - a);
  const filtered = returns.filter(
    item =>
      (!query ||
        item.clientName.toLocaleLowerCase().includes(query) ||
        item.clientId.toLocaleLowerCase().includes(query)) &&
      (search.year === undefined || item.taxYear === search.year),
  );
  const hasFilters = !!search.q || search.year !== undefined;
  const clients = new Map<string, ReturnSummary[]>();
  for (const item of filtered) {
    const clientReturns = clients.get(item.clientId) ?? [];
    clientReturns.push(item);
    clients.set(item.clientId, clientReturns);
  }
  const populated = filtered.reduce((sum, item) => sum + item.totalFields - item.missingFields, 0);
  const conflicts = filtered.reduce((sum, item) => sum + item.conflictFields, 0);

  return (
    <main
      {...inbox.getRootProps({ role: 'main' })}
      className="queue-page mx-auto min-h-[calc(100vh-185px)] max-w-[1280px] px-10 pt-[58px] pb-[76px] max-[980px]:px-7 max-[980px]:pt-[42px] max-[980px]:pb-[60px] max-[660px]:px-5 max-[660px]:pt-8 max-[660px]:pb-[45px]"
      id="main-content"
    >
      <div className="page-heading mb-6 flex flex-col gap-6 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="text-3xl font-semibold">Clients</h1>
        </div>
        <div className="queue-stats flex gap-[35px] pb-2 max-[660px]:w-full max-[660px]:justify-between max-[660px]:gap-[15px] [&>div]:flex [&>div]:flex-col [&>div]:border-l [&>div]:border-border [&>div]:pl-[25px] max-[660px]:[&>div]:pl-[15px] [&_strong]:text-[32px] [&_strong]:leading-tight [&_strong]:font-medium [&_strong]:tracking-[-1px] max-[660px]:[&_strong]:text-[27px] [&_span]:mt-[7px] [&_span]:text-[11px] [&_span]:whitespace-nowrap [&_span]:text-muted-foreground max-[660px]:[&_span]:text-[10px]">
          <div>
            <strong>{clients.size}</strong>
            <span>matching clients</span>
          </div>
          <div>
            <strong>{populated}</strong>
            <span>fields populated</span>
          </div>
          <div>
            <strong>{conflicts}</strong>
            <span>source conflicts</span>
          </div>
        </div>
      </div>

      <DocumentInbox returns={returns} inbox={inbox} />

      <section aria-labelledby="queue-title">
        <div className="mb-[18px] flex items-center justify-between gap-3 max-[720px]:flex-col max-[720px]:items-start [&_h2]:text-base [&_h2]:font-semibold">
          <h2 id="queue-title">Saved workspaces</h2>
          <span className="text-muted-foreground" role="status">
            {filtered.length} of {returns.length} saved tax-year workspaces · {clients.size}{' '}
            matching clients
          </span>
        </div>
        <div className="queue-filters mb-5 grid grid-cols-[minmax(0,1fr)_minmax(160px,220px)_auto] items-end gap-3.5 max-[660px]:grid-cols-1 [&_label]:mb-1.5 [&_label]:block [&_label]:text-xs [&_label]:font-semibold" role="search" aria-label="Filter saved clients">
          <div>
            <label htmlFor="client-search">Search clients</label>
            <Input
              id="client-search"
              type="search"
              placeholder="Client name or ID"
              value={search.q ?? ''}
              onChange={event => {
                const q = event.target.value;
                void navigate({
                  search: previous => ({ ...previous, q: q || undefined }),
                  replace: true,
                  resetScroll: false,
                });
              }}
            />
          </div>
          <div>
            <label htmlFor="year-filter">Tax year</label>
            <NativeSelect
              id="year-filter"
              value={search.year ?? ''}
              onChange={event => {
                const year = event.target.value === '' ? undefined : Number(event.target.value);
                void navigate({ search: previous => ({ ...previous, year }), resetScroll: false });
              }}
            >
              <option value="">All years</option>
              {search.year !== undefined && !years.includes(search.year) && (
                <option value={search.year}>{search.year} · no saved returns</option>
              )}
              {years.map(year => (
                <option key={year} value={year}>
                  {year}
                </option>
              ))}
            </NativeSelect>
          </div>
          <Button variant="outline"
            disabled={!hasFilters}
            onClick={() => void navigate({ search: {}, resetScroll: false })}
          >
            Clear filters
          </Button>
        </div>
        {returns.length === 0 ? (
          <div className="loading-panel rounded-lg border border-border bg-card px-6 py-[50px] text-muted-foreground [&_button]:mt-3">
            No saved client returns yet. Upload documents above to identify clients and tax years.
          </div>
        ) : (
          filtered.length === 0 && (
            <div className="loading-panel rounded-lg border border-border bg-card px-6 py-[50px] text-muted-foreground [&_button]:mt-3">
              <p>
                No client tax-year workspaces match these filters. Your saved records are unchanged.
              </p>
              <Button
                variant="link"
                className="h-auto p-0 text-xs"
                onClick={() => void navigate({ search: {}, resetScroll: false })}
              >
                Clear filters to show all saved returns
              </Button>
            </div>
          )
        )}
        <div className="client-grid grid grid-cols-2 gap-6 max-[660px]:grid-cols-1 max-[660px]:gap-[17px]">
          {[...clients.entries()].map(([clientId, clientReturns]) => {
            const client = clientReturns[0];
            const initials = client.clientName
              .split(' ')
              .map(part => part[0])
              .slice(0, 2)
              .join('');
            const orderedReturns = [...clientReturns].sort((a, b) => b.taxYear - a.taxYear);

            return (
              <Card role="article" className="client-card group p-[26px] max-[980px]:p-[22px] max-[660px]:p-[23px] [&>h3]:text-[23px] [&>h3]:font-semibold [&>h3]:tracking-[-.7px]" key={clientId}>
                <div className="client-card-top mb-[21px] flex items-center justify-between">
                  <span className="client-avatar grid size-[43px] place-items-center rounded-[10px] border border-[#d9e4d8] bg-[#edf3e9] text-sm font-semibold tracking-[.5px] text-[#506c43] group-even:border-[#dfe3ee] group-even:bg-[#edf0f6] group-even:text-[#5a667c]" aria-hidden="true">
                    {initials}
                  </span>
                  <Badge variant="secondary" className="text-[9px]">
                    {client.returnType === 'individual' ? 'Individual client' : 'Business client'}
                  </Badge>
                </div>
                <h3>{client.clientName}</h3>
                <p className="client-meta mt-1.5 flex flex-wrap gap-2 text-[11px] text-muted-foreground">
                  {clientReturns.length} tax-year workspace{clientReturns.length === 1 ? '' : 's'}
                </p>
                {orderedReturns.map(item => (
                  <section
                    className="year-return mt-6 border-t border-border pt-[19px] [&_.progress-block]:mt-[17px]"
                    key={item.id}
                    aria-label={`${item.clientName}, tax year ${item.taxYear}`}
                  >
                    <div className="year-return-heading flex items-center justify-between gap-3 [&>strong]:text-[17px] [&>strong]:tracking-[-.35px] [&>span]:text-[10px] [&>span]:text-muted-foreground">
                      <strong>Tax year {item.taxYear}</strong>
                      <span>Client intake</span>
                    </div>
                    <Progress
                      filled={item.totalFields - item.missingFields}
                      total={item.totalFields}
                    />
                    <div className="card-counts mt-[17px] flex gap-[22px] text-[11px] text-muted-foreground [&>span]:flex [&>span]:items-center [&>span]:gap-1.5">
                      <span>
                        <i className="count-dot conflict-dot inline-block size-1.5 rounded-full bg-[#c38b42]" />
                        {item.conflictFields} conflicts
                      </span>
                      <span>
                        <i className="count-dot missing-dot inline-block size-1.5 rounded-full bg-[#a5b0a7]" />
                        {item.missingFields} missing
                      </span>
                    </div>
                    <div className="card-bottom mt-4 flex items-center justify-between gap-3 border-t border-border pt-3.5 min-[661px]:max-[980px]:flex-col min-[661px]:max-[980px]:items-start [&>span]:text-[10px] [&>span]:text-muted-foreground">
                      <span>Updated {dateLabel(item.updatedAt)}</span>
                      <Button asChild>
                        <Link
                          className="button-link gap-[18px] text-[11px] min-[661px]:max-[980px]:w-full"
                          to="/clients/$clientId/returns/$taxYear"
                          params={{ clientId: item.clientId, taxYear: String(item.taxYear) }}
                          search={{}}
                          aria-label={`Review ${item.clientName}, tax year ${item.taxYear}`}
                        >
                          Review {item.taxYear} <span aria-hidden="true">↗</span>
                        </Link>
                      </Button>
                    </div>
                  </section>
                ))}
              </Card>
            );
          })}
        </div>
      </section>
    </main>
  );
}

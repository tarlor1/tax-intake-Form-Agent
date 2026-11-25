import type { ClientAssignment, IntakeClient } from '@/lib/apiSchemas';
import { Input } from '@/components/ui/input';
import { NativeSelect } from '@/components/ui/native-select';

export interface ClientSelection {
  clientId: string;
  year: string;
}

export function hasAssignment(selection: ClientSelection): ClientAssignment | undefined {
  const taxYear = Number(selection.year);
  if (selection.clientId && /^\d{4}$/.test(selection.year) && taxYear >= 1900) {
    return { clientId: selection.clientId, taxYear };
  }
}

export default function ClientPicker({ clients, selection, onChange }: {
  clients: IntakeClient[];
  selection: ClientSelection;
  onChange: (selection: ClientSelection) => void;
}) {
  const years = clients.find(client => client.clientId === selection.clientId)?.taxYears;
  const names = new Map<string, number>();
  for (const client of clients) {
    names.set(client.name, (names.get(client.name) ?? 0) + 1);
  }

  return (
    <div className="flex flex-wrap items-end gap-2">
      <label className="grid gap-1 text-sm">
        Client
        <NativeSelect value={selection.clientId} onChange={event => {
          const clientId = event.target.value;
          const available = clients.find(client => client.clientId === clientId)?.taxYears;
          const year = available && !available.includes(Number(selection.year)) ? '' : selection.year;
          onChange({ clientId, year });
        }}>
          <option value="">Choose a client</option>
          {clients.map(client => (
            <option key={client.clientId} value={client.clientId}>
              {client.name}{(names.get(client.name) ?? 0) > 1 ? ` · ${client.clientId}` : ''}
            </option>
          ))}
        </NativeSelect>
      </label>
      <label className="grid gap-1 text-sm">
        Tax year
        {years ? (
          <NativeSelect value={selection.year} onChange={event => onChange({ ...selection, year: event.target.value })}>
            <option value="">Choose a year</option>
            {years.map(year => <option key={year} value={year}>{year}</option>)}
          </NativeSelect>
        ) : (
          <Input type="text" inputMode="numeric" maxLength={4} className="w-24"
            value={selection.year} onChange={event => onChange({ ...selection, year: event.target.value })} />
        )}
      </label>
    </div>
  );
}

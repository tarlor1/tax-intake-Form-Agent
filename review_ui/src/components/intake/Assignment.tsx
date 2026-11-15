import { useState, type FormEvent } from 'react';
import { api, errorMessage, type InboxDocument, type InboxGroup, type ReturnSummary } from '@/lib/api';
import { Alert } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { NativeSelect } from '@/components/ui/native-select';

interface AssignmentProps {
    documentId: string;
    group: InboxGroup;
    returns: ReturnSummary[];
    onAssigned: (document: InboxDocument) => void;
}

export function Assignment({ documentId, group, returns, onAssigned }: AssignmentProps) {
    const [returnId, setReturnId] = useState('');
    const [name, setName] = useState(group.clientName ?? '');
    const [year, setYear] = useState(group.taxYear?.toString() ?? '');
    const [kind, setKind] = useState<'individual' | 'business'>(group.returnType ?? 'individual');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    async function assign(event: FormEvent) {
        event.preventDefault();
        if (busy) return;

        setBusy(true);
        setError('');

        try {
            const assignment = returnId
                ? { groupId: group.id, returnId }
                : {
                    groupId: group.id,
                    clientName: name.trim(),
                    taxYear: Number(year),
                    returnType: kind,
                };

            const assigned = await api.assign(documentId, assignment);
            onAssigned(assigned);
        } catch (reason) {
            setError(errorMessage(reason));
        } finally {
            setBusy(false);
        }
    }

    return (
        <form
            className="mt-4 space-y-4 rounded-lg border border-amber-200 bg-amber-50/50 p-4"
            onSubmit={event => void assign(event)}
        >
            <p className="font-semibold">Confirm this document group’s client and tax year</p>
            {group.issues.length > 0 && (
                <ul className="list-disc space-y-1 pl-5 text-xs text-amber-800">
                    {group.issues.map((issue, index) => <li key={index}>{issue}</li>)}
                </ul>
            )}
            <label className="block space-y-2 text-xs font-medium">
                <span>Assign to an existing return</span>
                <NativeSelect
                    value={returnId}
                    disabled={busy}
                    onChange={event => setReturnId(event.target.value)}
                >
                    <option value="">Create a separate client/year workspace</option>
                    {returns.map(item => (
                        <option key={item.id} value={item.id}>
                            {item.clientName} · {item.taxYear} · {item.clientId}
                        </option>
                    ))}
                </NativeSelect>
            </label>
            {!returnId && (
                <div className="grid gap-3 md:grid-cols-[1.5fr_.7fr_1fr]">
                    <label className="block space-y-2 text-xs font-medium">
                        <span>Client name</span>
                        <Input
                            value={name}
                            onChange={event => setName(event.target.value)}
                            required
                            maxLength={200}
                            disabled={busy}
                        />
                    </label>
                    <label className="block space-y-2 text-xs font-medium">
                        <span>Tax year</span>
                        <Input
                            type="number"
                            min="1900"
                            max="2200"
                            value={year}
                            onChange={event => setYear(event.target.value)}
                            required
                            disabled={busy}
                        />
                    </label>
                    <label className="block space-y-2 text-xs font-medium">
                        <span>Return type</span>
                        <NativeSelect
                            value={kind}
                            disabled={busy}
                            onChange={event => setKind(event.target.value as 'individual' | 'business')}
                        >
                            <option value="individual">Individual</option>
                            <option value="business">Business</option>
                        </NativeSelect>
                    </label>
                </div>
            )}
            <Button type="submit" disabled={busy}>
                {busy ? 'Assigning…' : 'Confirm assignment'}
            </Button>
            {error && <Alert variant="destructive">{error}</Alert>}
        </form>
    );
}

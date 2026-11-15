import { useSyncExternalStore, type ReactNode } from 'react';
import { isSessionExpired, subscribeToSession } from '@/lib/session';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export function SessionBoundary({ children }: { children: ReactNode }) {
  const expired = useSyncExternalStore(subscribeToSession, isSessionExpired);
  if (!expired) return children;

  return (
    <main className="mx-auto flex min-h-screen max-w-xl items-center px-5 py-12">
      <Card className="w-full" role="alert" aria-labelledby="session-expired-heading">
        <CardHeader>
          <CardTitle id="session-expired-heading">Session expired — reload this page</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm leading-relaxed">
          <p>
            This page is no longer authorized to use the local server. This can happen when the
            backend restarts. Uploads and document processing cannot continue from this page.
          </p>
          <p className="text-muted-foreground">
            Saved documents remain in your local workspace. Files that failed to upload must be
            selected again after reloading. Your Gemini API key does not need to change.
          </p>
          <Button type="button" onClick={() => window.location.reload()}>
            Reload workspace
          </Button>
        </CardContent>
      </Card>
    </main>
  );
}

export const SESSION_EXPIRED_MESSAGE =
  'This page is no longer authorized. Reload it to reconnect to the current server session.';

let expired = false;
const listeners = new Set<() => void>();

export function isSessionExpired() {
  return expired;
}

export function subscribeToSession(listener: () => void) {
  listeners.add(listener);

  return () => {
    listeners.delete(listener);
  };
}

export function expireSession() {
  if (expired) return;

  expired = true;
  for (const listener of listeners) {
    listener();
  }
}

import { createRouter } from '@tanstack/react-router';
import { routeTree } from './routeTree.gen';
import { RouteError, RouteNotFound, RoutePending } from './components/routing/RouteFeedback';

export const router = createRouter({
  routeTree,
  defaultPreload: false,
  defaultErrorComponent: RouteError,
  defaultNotFoundComponent: RouteNotFound,
  defaultPendingComponent: RoutePending,
});

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}

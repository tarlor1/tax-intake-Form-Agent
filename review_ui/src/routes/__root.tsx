import { createRootRoute } from '@tanstack/react-router';
import App from '../components/layout/App';
import { RouteError, RouteNotFound, RoutePending } from '../components/routing/RouteFeedback';

export const Route = createRootRoute({
  component: App,
  errorComponent: RouteError,
  notFoundComponent: RouteNotFound,
  pendingComponent: RoutePending,
});

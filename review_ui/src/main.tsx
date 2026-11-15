import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RouterProvider } from '@tanstack/react-router';
import { router } from './router';
import { SessionBoundary } from './components/routing/SessionBoundary';
import './index.css';

const root = document.getElementById('root');
if (!root) throw new Error('The review workspace root element is missing.');
createRoot(root).render(
  <StrictMode>
    <SessionBoundary>
      <RouterProvider router={router} />
    </SessionBoundary>
  </StrictMode>,
);

import * as React from 'react';
import { createRoot } from 'react-dom/client';

import '@/styles/globals.css';
import { App } from './App';

/** Mount the main page into an element; tests mount it into their own container. */
export function mountApp(element: HTMLElement) {
  const root = createRoot(element);
  root.render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
  return root;
}

const element = document.getElementById('root');
if (element) mountApp(element);

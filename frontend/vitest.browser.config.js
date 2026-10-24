import { defineConfig } from 'vitest/config';
import { playwright } from '@vitest/browser-playwright';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';
import { promises as Fs } from 'node:fs';

// fetch ttl files
const ttlFiles = {};
for await (const file of Fs.glob( '**/*.ttl' ) ) {
  const content = await Fs.readFile( file, 'utf8' );
  ttlFiles[file] = content;
  // Keep the historical Windows-style fixture keys working on Linux CI and macOS.
  ttlFiles[file.replaceAll('/', '\\')] = content;
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    projects:[

      {
        // Inherit the root plugins, aliases and provided fixtures.
        extends: true,
        test:{
          name: 'browser',
          include: [
            'test/**/*.browser.{test,spec}.{js,ts,tsx}',
          ],
          enabled: true,
          browser: {
            enabled: true,
            provider: playwright(),
            instances: [
              { browser: 'chromium' },
            ],
            headless: true,
            screenshotFailures: false,
          }
        }
      },

      {
        extends: true,
        test:{
          name: 'node',
          include: [
            'test/**/*.unit.{test,spec}.{js,ts}',
          ],
          enabled: true,
          environment: 'node',
        }
      },

    ],
    provide:{
      ttl: ttlFiles,
    }
  },
});

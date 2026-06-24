import { fileURLToPath } from 'url';
import { defineConfig } from 'vite';

export default defineConfig({
  base: './',
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      input: {
        appRemote: fileURLToPath(new URL('./remote.html', import.meta.url)),
        appMain:   fileURLToPath(new URL('./index.html', import.meta.url)),
        appLogin:  fileURLToPath(new URL('./login.html', import.meta.url)),
        appAdmin:  fileURLToPath(new URL('./admin.html', import.meta.url)),
      },
    },
  },
});

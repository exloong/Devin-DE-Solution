import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Set RELAY_API_PROXY (e.g. http://127.0.0.1:8000) to forward /api to a
// locally running Relay API during `npm run dev`.
const apiProxy = process.env.RELAY_API_PROXY;

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 4173,
    proxy: apiProxy ? { '/api': { target: apiProxy, changeOrigin: true } } : undefined,
  },
});

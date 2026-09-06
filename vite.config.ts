import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// Set RELAY_API_PROXY (e.g. http://127.0.0.1:8000) in the environment or a
// .env.local file to forward /api to a locally running Relay API during
// `npm run dev`. Production serves /api from the same origin via nginx.
export default defineConfig(({ mode }) => {
  const apiProxy = loadEnv(mode, '.', 'RELAY_')['RELAY_API_PROXY'];
  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 4173,
      proxy: apiProxy ? { '/api': { target: apiProxy, changeOrigin: true } } : undefined,
    },
  };
});

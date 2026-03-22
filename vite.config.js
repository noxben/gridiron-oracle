import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/espn-api': {
        target: 'https://fantasy.espn.com',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/espn-api/, ''),
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            const espnS2 = globalThis.__ESPN_S2__;
            const swid   = globalThis.__SWID__;
            if (espnS2 && swid) {
              proxyReq.setHeader('Cookie', `ESPN_S2=${espnS2}; SWID=${swid}`);
            }
          });
        },
      },
    },
  },
})
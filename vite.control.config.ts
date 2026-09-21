import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const runtimeProcess = (globalThis as typeof globalThis & { process?: { env?: Record<string, string | undefined> } }).process
const buildId = runtimeProcess?.env?.LIVENOTE_BUILD_ID || `dev-${new Date().toISOString()}`

// The PC-only control console does not request a microphone. Keep a plain HTTP
// entry for local desktop administration while the mobile recording entry uses
// the HTTPS basic-SSL config from vite.config.ts.
export default defineConfig({
  plugins: [vue()],
  define: {
    __LIVENOTE_BUILD_ID__: JSON.stringify(buildId),
  },
  server: {
    host: '127.0.0.1',
    port: 4173,
    hmr: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})

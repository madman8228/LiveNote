import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import basicSsl from '@vitejs/plugin-basic-ssl'

const runtimeProcess = (globalThis as typeof globalThis & { process?: { env?: Record<string, string | undefined> } }).process
const buildId = runtimeProcess?.env?.LIVENOTE_BUILD_ID || `dev-${new Date().toISOString()}`

export default defineConfig({
  plugins: [vue(), basicSsl()],
  define: {
    __LIVENOTE_BUILD_ID__: JSON.stringify(buildId),
  },
  server: {
    host: true,
    port: 5173,
    // HMR reconnects can reload the mobile page when Wi-Fi returns. That
    // would destroy MediaRecorder during a long recording, so the recording
    // test server intentionally requires manual refreshes after code edits.
    hmr: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})

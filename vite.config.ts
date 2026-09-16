import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import basicSsl from '@vitejs/plugin-basic-ssl'

export default defineConfig({
  plugins: [vue(), basicSsl()],
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

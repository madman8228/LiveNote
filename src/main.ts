import { createApp } from 'vue'
import App from './App.vue'
import './styles.css'

createApp(App).mount('#app')

// Cache the production app shell so a temporary network loss does not make
// the page itself unreachable. This does not keep a MediaRecorder alive after
// Android Chrome has actually discarded the document; the recovery flow still
// protects the chunks already written to IndexedDB.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js').catch(() => undefined)
  })
}

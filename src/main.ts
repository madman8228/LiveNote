import { createApp } from 'vue'
import App from './App.vue'
import './styles.css?control-layout-v4'

const isControlConsole = new URLSearchParams(window.location.search).get('mode') === 'control'
if (isControlConsole) {
  void import('./control/ControlConsole.vue').then(({ default: ControlConsole }) => createApp(ControlConsole).mount('#app'))
} else {
  createApp(App).mount('#app')
}

// Cache the production app shell so a temporary network loss does not make
// the page itself unreachable. This does not keep a MediaRecorder alive after
// Android Chrome has actually discarded the document; the recovery flow still
// protects the chunks already written to IndexedDB.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js').catch(() => undefined)
  })
}

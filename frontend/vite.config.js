import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Electron note:
// - Dev: Electron loads http://localhost:5173 (Vite dev server)
// - Prod: Electron loads file://.../index.html, so Vite must use relative asset paths (base: "./")
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Chat
      '/chat': 'http://localhost:8008',

      // Voice
      '/tts': 'http://localhost:8008',
      '/stt': 'http://localhost:8008',
      '/transcribe': 'http://localhost:8008',
      '/voice': 'http://localhost:8008',
      '/speak': 'http://localhost:8008',

      // Plugins / tools
      '/plugins': 'http://localhost:8008',

      // Model + memory + uploads
      '/model': 'http://localhost:8008',
      '/memory': 'http://localhost:8008',
      '/file-upload': 'http://localhost:8008',

      // WebSocket (FastAPI)
      '/ws': {
        target: 'ws://localhost:8008',
        ws: true,
      },
    }
  }
})

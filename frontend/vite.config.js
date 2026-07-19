import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Electron note:
// - Dev: Electron loads http://localhost:5173 (Vite dev server)
// - Prod: Electron loads file://.../index.html, so Vite must use relative asset paths (base: "./")
export default defineConfig({
  base: "./",
  plugins: [react()],
  // Pre-bundle the three.js stack at server start. IMPORTANT: import drei via
  // SUBPATHS only (e.g. "@react-three/drei/core/Gltf") — bundling all of drei
  // produces a 3.7 MB dev chunk, and Norton's network filter on this machine
  // resets localhost HTTP responses larger than ~2 MB (symptom:
  // ERR_CONNECTION_RESET on the chunk and a blank app).
  optimizeDeps: {
    include: [
      "three",
      "@react-three/fiber",
      "@react-three/postprocessing",
      "three/examples/jsm/utils/SkeletonUtils.js",
      "three/examples/jsm/loaders/KTX2Loader.js",
      "@react-three/drei/core/Gltf",
      "@react-three/drei/core/AdaptiveDpr",
      "@react-three/drei/core/PerspectiveCamera",
    ],
  },
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

      // API sub-routes (web search, maps)
      '/api': 'http://localhost:8008',

      // Vision
      '/vision': 'http://localhost:8008',

      // Model + memory + uploads
      '/model': 'http://localhost:8008',
      '/memory': 'http://localhost:8008',
      '/file-upload': 'http://localhost:8008',
      '/uploads': 'http://localhost:8008',

      // Status + health + tasks + developer mode
      // (without these, the Nova Core card shows offline in dev)
      '/status': 'http://localhost:8008',
      '/health': 'http://localhost:8008',
      '/tasks': 'http://localhost:8008',
      '/dev': 'http://localhost:8008',

      // WebSocket (FastAPI)
      '/ws': {
        target: 'ws://localhost:8008',
        ws: true,
      },
    }
  }
})

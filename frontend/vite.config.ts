import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_PROXY_TARGET?.trim() || 'http://127.0.0.1:8000'

  return {
    plugins: [vue()],
    server: {
      proxy: {
        // Dev only: forward the FastAPI backend (`backend/`, python scripts/run.py).
        // The frontend calls the backend's own routes under /api/v1.
        '/api': {
          target,
          changeOrigin: false,
        },
        // Maintenance work-order copy / review endpoints live under /internal/v1.
        '/internal': {
          target,
          changeOrigin: false,
        },
      },
    },
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
  }
})

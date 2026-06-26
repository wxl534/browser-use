import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发模式下把 /api 反向代理到 FastAPI 后端（uvicorn :8000）。
// 生产模式 `vite build` 产出 dist/，由 FastAPI 直接静态托管。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})

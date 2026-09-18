import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发态把后端接口与 API 文档代理到本机 FastAPI（默认 8000），
// 前端同源访问 /api/*，无需后端开 CORS。
const backend = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  css: {
    modules: {
      // 让 .nav-item 这类连字符类名同时生成 navItem 的 camelCase 导出，
      // 组件中统一以 styles.navItem 访问（保留原 kebab 键，二者皆可用）。
      localsConvention: 'camelCase',
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/docs': { target: backend, changeOrigin: true },
      '/redoc': { target: backend, changeOrigin: true },
      '/openapi.json': { target: backend, changeOrigin: true },
    },
  },
})

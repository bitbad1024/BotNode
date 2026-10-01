import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发态把后端接口与 API 文档代理到本机 FastAPI（默认 8000），
// 前端同源访问 /api/*，无需后端开 CORS。
const backend = 'http://127.0.0.1:18080'

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
    // 5173 是 Vite 的默认端口，但 Windows 会随机保留一段动态端口范围
    // （netsh interface ipv4 show excludedportrange protocol=tcp 可见），
    // 且保留段不固定、每次系统/服务重启都可能变化：之前选的 5273 后来就落进了
    // 新保留段 5241-5340，绑上去报 EACCES（权限）而不是 EADDRINUSE（被占用），
    // 只能换端口。当前保留段为 5041-5857、27339、28385、28390、50000-50059，
    // 这里取范围外的 15173；再撞上就 netsh 查一次换一个，或临时用：
    // npm run dev -- --port <空闲端口>
    port: 15173,
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/docs': { target: backend, changeOrigin: true },
      '/redoc': { target: backend, changeOrigin: true },
      '/openapi.json': { target: backend, changeOrigin: true },
    },
  },
})

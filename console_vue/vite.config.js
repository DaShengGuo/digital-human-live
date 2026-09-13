import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// base './': 构建产物由 console.py(:8030) 直接静态托管, 兼容任意挂载路径
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: { outDir: 'dist', chunkSizeWarningLimit: 1500 },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8030', '/control': 'http://127.0.0.1:8030' }
  }
})

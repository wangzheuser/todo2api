import { defineConfig, type ProxyOptions } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const backendTarget = 'http://localhost:8080'

/**
 * 创建本地后端代理，并将浏览器来源重写为后端自身来源。
 */
function createBackendProxy(): ProxyOptions {
  return {
    target: backendTarget,
    changeOrigin: true,
    configure(proxy) {
      proxy.on('proxyReq', (proxyRequest) => {
        // 本地开发由 Vite 跨端口转发，后端仍按同源请求执行 CSRF 校验。
        proxyRequest.setHeader('origin', backendTarget)
        proxyRequest.setHeader('referer', `${backendTarget}/`)
      })
    },
  }
}

export default defineConfig({
  base: '/',
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': createBackendProxy(),
      '/v1': createBackendProxy(),
    },
  },
})

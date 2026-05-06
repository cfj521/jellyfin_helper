import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'
import fs from 'fs'

/**
 * 从项目根的 config.yaml 读取 server.backend_port / server.frontend_port。
 * 用最小正则解析，避免引入 yaml 依赖。
 *
 * 优先级：环境变量 > config.yaml > 默认值
 */
function loadPorts() {
  const defaults = { backend: 8000, frontend: 5173 }
  const configPath = path.resolve(__dirname, '../../config.yaml')
  let yaml = { backend: defaults.backend, frontend: defaults.frontend }

  if (fs.existsSync(configPath)) {
    try {
      const text = fs.readFileSync(configPath, 'utf-8')
      const block = text.match(/^server:\s*\n([\s\S]*?)(?=^\S|\Z)/m)
      if (block) {
        const bm = block[1].match(/backend_port:\s*(\d+)/)
        const fm = block[1].match(/frontend_port:\s*(\d+)/)
        if (bm) yaml.backend = parseInt(bm[1], 10)
        if (fm) yaml.frontend = parseInt(fm[1], 10)
      }
    } catch (e) {
      console.warn('[vite] 读取 config.yaml 失败，使用默认端口', e.message)
    }
  }

  return {
    backend: parseInt(process.env.BACKEND_PORT, 10) || yaml.backend,
    frontend: parseInt(process.env.FRONTEND_PORT, 10) || yaml.frontend,
  }
}

const ports = loadPorts()
console.log(`[vite] frontend dev port: ${ports.frontend}, backend proxy: http://localhost:${ports.backend}`)

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src')
    }
  },
  server: {
    port: ports.frontend,
    proxy: {
      '/api': {
        target: `http://localhost:${ports.backend}`,
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets'
  }
})

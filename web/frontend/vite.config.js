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
  // 用 Sass 现代 JS API（默认还是 legacy，会持续抛 [legacy-js-api] 警告）。
  // 现代 API 在 sass 1.65+ 起可用，本项目 sass ^1.69 兼容；且无任何 @import 语法依赖，
  // 切换不会破坏现有样式（如需重启 dev 服后生效）。
  // 'modern-compiler' 需要装 sass-embedded 包性能更好；'modern' 走自带 JS API 零新依赖
  css: {
    preprocessorOptions: {
      scss: { api: 'modern' },
    },
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

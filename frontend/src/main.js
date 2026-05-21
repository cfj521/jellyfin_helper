import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'

// dayjs UTC 插件：后端 datetime 字段是 naive UTC（无 tz 后缀）
// 必须显式按 UTC 解析后再 .local() 转本机时区，否则会被当作本地时间显示，差几小时
import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'
dayjs.extend(utc)

import App from './App.vue'
import router from './router'

import './styles/main.scss'

// 全局拦截 form submit 防止页面 reload。
// 整个项目走 SPA + axios，form 元素仅作语义容器（如 el-form 渲染出来的 <form>）。
// 各处 dialog 里的 input 在用户按回车时会触发浏览器隐式 form submit
// （input + 按钮放在同一 <form> 里 → 整页跳转）—— 已是用户多次反馈的"按回车刷新整页" bug。
// 业务真需要 submit 的场景仍可在自己的 @submit.prevent="..." 回调里执行 axios，
// 这里的 capture-phase 全局拦截只是兜底，不影响这类显式回调的执行。
window.addEventListener('submit', (e) => {
  e.preventDefault()
}, true)

const app = createApp(App)

// 注册所有图标
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

app.mount('#app')

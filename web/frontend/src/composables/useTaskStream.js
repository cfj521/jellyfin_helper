/**
 * 任务进度 SSE composable，替代原来的 1Hz / 5s 轮询。
 *
 * 后端推送时机（写入 /api/tasks/{id} 时同步触发）：
 *   - create_task            → tasks:any 推 ping
 *   - update_task_progress   → task:{id} 推 snapshot
 *   - complete_task / cancel → task:{id} 推 snapshot + 'done' event
 *
 * 浏览器原生 EventSource 自带断线自动重连，前端代码反而比 setInterval 简洁。
 */
import { onUnmounted, ref, watch, isRef } from 'vue'

/**
 * 订阅单任务进度流。
 *
 * @param {Ref<number> | () => number | number} taskIdSource - 任务 id；可以是 ref、getter 或固定值
 * @returns {{ task, connected, error, disconnect }}
 *   task: Ref<TaskResponse | null>  当前 snapshot（后端每次写库后实时推过来）
 *   connected: Ref<boolean>         SSE 连接是否打开
 *   error: Ref<string | null>       最近一次错误描述
 *   disconnect: () => void          手动断开（一般不用调用，组件卸载自动清理）
 */
export function useTaskStream(taskIdSource) {
  const task = ref(null)
  const connected = ref(false)
  const error = ref(null)
  let es = null
  let manualClosed = false

  const getId = () => {
    if (typeof taskIdSource === 'function') return taskIdSource()
    if (isRef(taskIdSource)) return taskIdSource.value
    return taskIdSource
  }

  const closeEs = () => {
    if (es) {
      try { es.close() } catch {}
      es = null
    }
    connected.value = false
  }

  const disconnect = () => {
    manualClosed = true
    closeEs()
  }

  const connect = (id) => {
    if (!id) return
    manualClosed = false
    closeEs()  // 旧连接先清
    task.value = null
    error.value = null

    es = new EventSource(`/api/tasks/${id}/stream`)

    es.onopen = () => {
      connected.value = true
      error.value = null
    }

    // 默认 message 事件 = snapshot 更新
    es.onmessage = (e) => {
      try {
        task.value = JSON.parse(e.data)
      } catch (err) {
        console.warn('[useTaskStream] snapshot parse error', err)
      }
    }

    // 后端在终态主动发 'done'，前端关闭以避免无谓重连
    es.addEventListener('done', () => {
      closeEs()
    })

    es.addEventListener('error', (e) => {
      // 后端的应用层 error（如 404 task not found）
      try {
        const parsed = JSON.parse(e.data || '{}')
        error.value = parsed.error || 'unknown error'
      } catch {
        // 不是 JSON，可能是连接级错误
      }
    })

    es.onerror = () => {
      // 网络/连接错误。EventSource 会自动重连（除非我们已 close）。
      connected.value = false
      if (manualClosed) return
      // 不主动重连，交给浏览器原生重试机制
    }
  }

  // taskId 变化时重连（路由切换详情页用得到）
  watch(
    () => getId(),
    (id) => {
      if (id) connect(id)
      else closeEs()
    },
    { immediate: true },
  )

  onUnmounted(disconnect)

  return { task, connected, error, disconnect }
}


/**
 * 订阅任务列表变更流。任意 task 状态变化 → 触发 onChange 回调。
 *
 * 服务端在客户端连上时立即发一个 ping，所以 mount 后无需手动触发首次 refetch。
 *
 * 节流策略（不是 debounce！）：
 *   - 首个 ping 立即触发 onChange
 *   - throttleMs 窗口内的后续 pings 合并成一次"尾部"调用
 *   这样长任务持续推进时，UI 也能按 ~1Hz 稳定刷新，
 *   不会被"持续不断的 ping 永远拖延 debounce"挂住。
 *
 * @param {() => void} onChange
 * @param {object} opts
 * @param {number} opts.throttleMs - 默认 1000ms（最高 1Hz 刷新）
 * @returns {{ connected }}
 */
export function useTasksStream(onChange, { throttleMs = 1000 } = {}) {
  const connected = ref(false)
  let es = null
  let lastFireAt = 0
  let trailingTimer = null

  const safeCall = () => {
    try { onChange() } catch (e) { console.error('[useTasksStream] onChange threw', e) }
  }

  const fire = () => {
    const now = Date.now()
    const since = now - lastFireAt
    if (since >= throttleMs) {
      lastFireAt = now
      safeCall()
    } else if (!trailingTimer) {
      // 落在窗口内：安排一个尾部触发（窗口末尾跑一次）
      trailingTimer = setTimeout(() => {
        trailingTimer = null
        lastFireAt = Date.now()
        safeCall()
      }, throttleMs - since)
    }
    // 已经有 trailingTimer 在排队 → 后续 ping 合并掉，不开新定时器
  }

  es = new EventSource('/api/tasks/stream')
  es.onopen = () => { connected.value = true }
  es.addEventListener('ping', fire)
  es.onmessage = fire
  es.onerror = () => { connected.value = false /* 浏览器自动重连 */ }

  onUnmounted(() => {
    if (es) {
      try { es.close() } catch {}
      es = null
    }
    if (trailingTimer) clearTimeout(trailingTimer)
  })

  return { connected }
}

<template>
  <div class="page-container">
    <div class="page-header">
      <h2>媒体库总览</h2>
      <div class="header-actions">
        <el-tooltip
          v-if="systemInfo"
          placement="bottom"
          effect="dark"
        >
          <template #content>
            <div>{{ systemInfo.operating_system }}</div>
            <div v-if="systemInfo.id" class="muted">{{ systemInfo.id }}</div>
            <div v-if="jellyfinHost" class="muted">点击在新窗口打开 Jellyfin Web</div>
          </template>
          <a
            v-if="jellyfinHost"
            class="server-info link"
            :href="jellyfinHost"
            target="_blank"
            rel="noopener noreferrer"
          >
            <el-icon><Connection /></el-icon>
            Jellyfin {{ systemInfo.version }}
            <span class="server-name">· {{ systemInfo.server_name }}</span>
            <el-icon class="ext-icon"><Link /></el-icon>
          </a>
          <span v-else class="server-info">
            <el-icon><Connection /></el-icon>
            Jellyfin {{ systemInfo.version }}
            <span class="server-name">· {{ systemInfo.server_name }}</span>
          </span>
        </el-tooltip>
        <el-tooltip content="重新统计所有库（绕过 2 小时缓存）" placement="bottom">
          <el-button @click="loadAll(true)" :loading="loading">
            <el-icon><Refresh /></el-icon>
            强制刷新统计
          </el-button>
        </el-tooltip>
        <el-button type="warning" @click="refreshAll" :loading="refreshing">
          <el-icon><MagicStick /></el-icon>
          通知 Jellyfin 全局重扫
        </el-button>
      </div>
    </div>

    <el-alert v-if="loadError" type="error" :title="loadError" show-icon :closable="false" style="margin-bottom: 16px" />

    <!-- 媒体处理工具栏（作用范围：所有媒体库）-->
    <MediaToolbar v-if="libraries.length" :scope="globalScope" />

    <!-- 库列表 -->
    <el-row :gutter="16" v-loading="loading">
      <el-col v-for="lib in libraries" :key="lib.id" :xs="24" :sm="12" :md="8" :lg="6">
        <el-card
          shadow="hover"
          class="lib-card"
          :class="{ 'lib-inaccessible': !lib.all_accessible, 'has-cover': !!lib.cover_url }"
          :style="lib.cover_url ? { '--cover-url': `url(${lib.cover_url})` } : {}"
          :body-style="{ padding: 0 }"
          @click="goDetail(lib)"
        >
          <!-- 封面底图 -->
          <div v-if="lib.cover_url" class="cover-bg" />
          <div class="card-content">
            <!-- 顶部：库名 + 类型（成人库用「成人」徽章替代 电影/剧集/混合）-->
            <div class="card-header">
              <span class="lib-name">{{ lib.name }}</span>
              <el-tag v-if="lib.is_adult" type="warning" size="small" effect="dark">
                成人
              </el-tag>
              <el-tag v-else :type="typeTagType(lib.collection_type)" size="small">
                {{ typeLabel(lib.collection_type) }}
              </el-tag>
            </div>

            <!-- 中间留白让海报透出 -->
            <div class="card-spacer" />

            <!-- 底部统计行：含重扫按钮 -->
            <div v-if="loadingStats[lib.id] && !stats[lib.id]" class="lib-stats placeholder">
              <el-icon class="spin"><Loading /></el-icon>
              <span>加载统计中...</span>
            </div>
            <div v-else-if="statsError[lib.id]" class="lib-stats error">
              <el-icon><Warning /></el-icon>
              <span class="err-msg">加载失败</span>
              <el-button
                link
                type="primary"
                size="small"
                @click.stop="loadStats(lib)"
              >重试</el-button>
            </div>
            <div v-else-if="stats[lib.id]" class="lib-stats">
              <div
                v-for="m in displayMetrics(lib, stats[lib.id])"
                :key="m.label"
                class="stat-cell"
              >
                <span class="stat-label">{{ m.label }}</span>
                <span class="stat-value" :style="m.color ? { color: m.color } : null">
                  {{ formatStatValue(m) }}
                </span>
              </div>
              <el-tooltip content="重新扫描媒体库" placement="top">
                <button
                  class="rescan-btn"
                  :class="{ 'is-loading': refreshing === lib.id }"
                  @click.stop="refreshLib(lib)"
                >
                  <el-icon><Refresh /></el-icon>
                </button>
              </el-tooltip>
            </div>
            <div v-else class="lib-stats placeholder">
              <span>等待加载...</span>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-empty v-if="!loading && !libraries.length && !loadError" description="Jellyfin 没有配置任何媒体库" />

    <!-- 刷新模式对话框（单库） -->
    <RefreshLibraryDialog
      v-model="showRefreshDialog"
      :library-name="targetLibrary?.name"
      :loading="!!refreshing"
      @confirm="onRefreshConfirm"
    />
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh, MagicStick, Check, Close, Loading, DataAnalysis, Warning, Right, Connection, Link } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { jellyfinApi, configApi } from '@/api'
import RefreshLibraryDialog from '@/components/RefreshLibraryDialog.vue'
import MediaToolbar from '@/components/MediaToolbar.vue'

const router = useRouter()
const libraries = ref([])
const stats = reactive({})
const loadingStats = reactive({})
const statsError = reactive({})  // 每个库的加载错误信息
const loading = ref(false)
const loadError = ref('')
const systemInfo = ref(null)
const jellyfinHost = ref('')  // 用于 Jellyfin Web 跳转链接
const refreshing = ref(false)  // 当前刷新中的库 id（单库），或 true（全局）
const showRefreshDialog = ref(false)
const targetLibrary = ref(null)  // 弹窗目标库（null 表示全局）

const MODE_LABELS = {
  scan_changes: '扫描新的和有修改的文件',
  missing_metadata: '搜索缺少的元数据',
  replace_all: '覆盖所有元数据',
}

// 总览页 toolbar scope：作用于所有已加载的库
const globalScope = computed(() => ({
  mode: 'all',
  library_ids: libraries.value.map(l => l.id),
}))

/**
 * 加载库列表 + 各库 stats。
 * force=true 时绕过后端的 2h 缓存，强制重算 —— 用于"刷新"按钮。
 * 默认 false：首次进入页面 / 路由切回都走缓存，避免对几千个文件全盘 rglob。
 *
 * stats 是 fire-and-forget 并发跑（4-5 worker），不阻塞列表渲染。
 */
const loadAll = async (force = false) => {
  loading.value = true
  loadError.value = ''
  try {
    const res = await jellyfinApi.libraries(true)
    libraries.value = res.data.libraries || []
    if (libraries.value.length) {
      loadAllStatsParallel(libraries.value, 5, force)
    }
  } catch (e) {
    loadError.value = '无法连接 Jellyfin: ' + (e.response?.data?.detail || e.message)
  } finally {
    loading.value = false
  }
  try {
    const r = await jellyfinApi.systemInfo()
    systemInfo.value = r.data
  } catch {}
  try {
    // 拿 Jellyfin host 用于"在新窗口打开"链接
    const r2 = await configApi.getFull()
    const host = r2.data?.config?.jellyfin?.host
    if (host) jellyfinHost.value = host.replace(/\/$/, '') + '/web/'
  } catch {}
}

/**
 * 并发加载所有库的 stats，限制最大并发数。
 * 每个库的 loading / 失败状态独立，不影响其他库。
 *
 * 注意：不预过滤"路径不可访问"的库 —— 后端在路径不可达时仍会返回
 * Jellyfin 元数据统计（电影/剧集/缺海报数），文件系统统计为 0。
 * 这样用户起码能看到一半数据，而不是永远卡在"等待加载"。
 */
const loadAllStatsParallel = async (libs, concurrency = 5, force = false) => {
  const queue = [...libs]
  const worker = async () => {
    while (queue.length) {
      const lib = queue.shift()
      if (lib) await loadStats(lib, true, force)  // silent: 失败时不弹全局 toast
    }
  }
  const workerCount = Math.min(concurrency, libs.length)
  await Promise.all(Array.from({ length: workerCount }, worker))
}

const loadStats = async (lib, silent = false, force = false) => {
  loadingStats[lib.id] = true
  delete statsError[lib.id]
  // 强制刷新：清掉旧数据，让模板的 placeholder（loadingStats && !stats）自然显示
  // 转圈，跟首次进入页面的视觉一致 —— 否则模板会走 v-else-if 显示旧数字，
  // 用户看不出后端正在重算
  if (force) delete stats[lib.id]
  try {
    const res = await jellyfinApi.libraryStats(lib.id, force)
    stats[lib.id] = res.data
  } catch (e) {
    const msg = e.response?.data?.detail || e.message
    statsError[lib.id] = msg
    if (!silent) {
      ElMessage.error('统计加载失败: ' + msg)
    }
  } finally {
    loadingStats[lib.id] = false
  }
}

const refreshLib = (lib) => {
  // 打开模式选择对话框
  targetLibrary.value = lib
  showRefreshDialog.value = true
}

const refreshAll = async () => {
  // 全局重扫不支持选模式（Jellyfin /Library/Refresh 没这选项），仅做二次确认
  try {
    await ElMessageBox.confirm(
      '全局重扫会按"扫描新的和有修改的文件"模式扫描所有媒体库，可能耗时较长，确定吗？',
      '确认全局重扫',
      { type: 'warning', confirmButtonText: '开始扫描', cancelButtonText: '取消' },
    )
  } catch { return }
  refreshing.value = true
  try {
    await jellyfinApi.refreshAll()
    ElMessage.success('已通知 Jellyfin 全局重扫')
  } catch (e) {
    console.error(e)
  } finally {
    refreshing.value = false
  }
}

const onRefreshConfirm = async (mode) => {
  if (!targetLibrary.value) return
  const lib = targetLibrary.value
  refreshing.value = lib.id
  try {
    await jellyfinApi.refreshLibrary(lib.id, mode)
    ElMessage.success({
      message: `已通知 Jellyfin 重扫"${lib.name}"（${MODE_LABELS[mode]}）`,
      duration: 4000,
    })
    showRefreshDialog.value = false
  } catch (e) {
    console.error(e)
  } finally {
    refreshing.value = false
  }
}

const goDetail = (lib) => {
  router.push(`/medialibraries/${lib.id}`)
}

/**
 * 把字节自适应格式化为 KB / MB / GB / TB
 */
const formatBytes = (bytes) => {
  if (!bytes || bytes <= 0) return '0'
  const KB = 1024, MB = KB * 1024, GB = MB * 1024, TB = GB * 1024
  if (bytes >= TB) return (bytes / TB).toFixed(2) + ' TB'
  if (bytes >= GB) return (bytes / GB).toFixed(2) + ' GB'
  if (bytes >= MB) return (bytes / MB).toFixed(1) + ' MB'
  return (bytes / KB).toFixed(0) + ' KB'
}

/**
 * 总时长（秒）格式化：< 1h → Nm；< 100h → HhMm；≥ 100h → Nh；≥ 10000h → Nh(≈D 天)
 */
const formatDuration = (seconds) => {
  if (!seconds || seconds <= 0) return '0'
  const total_min = Math.floor(seconds / 60)
  if (total_min < 60) return `${total_min}m`
  const h = Math.floor(total_min / 60)
  const m = total_min % 60
  if (h < 100) return m ? `${h}h${String(m).padStart(2, '0')}m` : `${h}h`
  if (h < 10000) return `${h}h`
  const days = Math.floor(h / 24)
  return `${h}h（≈${days} 天）`
}

/**
 * 健康度统计 chip：用 items_healthy / total + 百分比展示
 * 颜色：100% 绿，>=90% 蓝，>=70% 橙，否则红
 *
 * 注：总览页不读 LibraryDetail 的 stats prefs。详情页隐藏 health 是用户在那一页的视图偏好，
 * 不应该影响"媒体库总览"这个全局视图。lib 参数保留接口但不使用。
 */
const buildHealthMetric = (jf, lib) => {
  const total = jf.total_items || 0
  if (!total) return null
  // items_healthy 字段缺失（fields 没要 health）→ 不显示，但不假装 100%
  if (jf.items_healthy == null) return null
  const healthy = jf.items_healthy
  const ratio = healthy / total
  let color = '#10b981'
  if (ratio < 0.7) color = '#ef4444'
  else if (ratio < 0.9) color = '#f59e0b'
  else if (ratio < 1) color = '#3b82f6'
  return {
    label: '健康度',
    value: `${healthy}/${total} (${(ratio * 100).toFixed(0)}%)`,
    text: true,
    color,
  }
}

/**
 * 库的"总项目数"：优先用 Jellyfin 元数据计数（更准）；
 * 没拿到 Jellyfin items 就用文件系统的视频/音频/图片总数兜底
 */
const totalItemsOf = (s) => {
  const jf = s.jellyfin || {}
  const fs = s.filesystem || {}
  if (jf.total_items) return jf.total_items
  return (fs.video_count || 0) + (fs.audio_count || 0) + (fs.image_count || 0)
}

/**
 * 所有库统一显示：项目 / 占用 / 时长 / 健康度
 * 不再按 collection_type 分支细分
 */
const displayMetrics = (lib, s) => {
  const fs = s.filesystem || {}
  const jf = s.jellyfin || {}
  const sizeBytes = fs.total_size_bytes || 0
  const runtimeSec = jf.total_runtime_seconds || 0
  const out = [
    { label: '项目', value: totalItemsOf(s) },
    { label: '占用', value: formatBytes(sizeBytes), text: true },
  ]
  if (runtimeSec > 0) {
    out.push({ label: '时长', value: formatDuration(runtimeSec), text: true })
  }
  const health = buildHealthMetric(jf, lib)
  if (health) out.push(health)
  return out
}

const formatStatValue = (m) => {
  if (m.text) return m.value
  if (typeof m.value === 'number') return m.value.toLocaleString()
  return m.value ?? '—'
}

const typeLabel = (t) => ({
  movies: '电影', tvshows: '剧集', music: '音乐', musicvideos: '音乐视频',
  homevideos: '家庭视频', boxsets: '合集', books: '图书', mixed: '混合',
}[t] || t)

const typeTagType = (t) => ({
  movies: 'success', tvshows: 'primary', music: 'warning', mixed: 'info',
}[t] || '')

onMounted(loadAll)
</script>

<style lang="scss" scoped>
.header-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.server-info {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 12px;
  margin-right: 4px;
  font-size: 12px;
  color: #475569;
  white-space: nowrap;
  cursor: help;
  text-decoration: none;

  .el-icon {
    color: #00a4dc;  // Jellyfin 品牌蓝
    font-size: 14px;
  }

  .server-name {
    color: #94a3b8;
  }

  &.link {
    cursor: pointer;
    transition: color 0.15s ease;

    &:hover {
      color: #00a4dc;
      .server-name { color: #00a4dc; }
    }

    .ext-icon {
      font-size: 11px;
      color: #94a3b8;
      margin-left: 2px;
    }
  }
}

.lib-card {
  margin-bottom: 16px;
  position: relative;
  overflow: hidden;
  border: none;
  min-height: 240px;
  display: flex;
  flex-direction: column;
  cursor: pointer;
  transition: transform 0.15s ease;

  &:hover {
    transform: translateY(-2px);
  }

  &.lib-inaccessible {
    outline: 2px solid #f56c6c;
    outline-offset: -2px;
  }

  // el-card 内部 body 必须填满整张卡片，否则下半部分没蒙版会露原始海报
  :deep(.el-card__body) {
    flex: 1;
    position: relative;
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  // 封面底图
  .cover-bg {
    position: absolute;
    inset: 0;
    background-image: var(--cover-url);
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    z-index: 0;
    pointer-events: none;
  }

  // 卡片内容：上中下三段，中间留空让海报展示
  .card-content {
    position: relative;
    z-index: 1;
    padding: 14px;
    flex: 1;
    display: flex;
    flex-direction: column;
    background: transparent;
    gap: 8px;
  }

  // 中间留白：把 header 和 bottom-row 顶到两端
  .card-spacer {
    flex: 1;
    min-height: 60px;
  }

  // 没封面的卡片：纯白底，去掉所有局部蒙版
  // 标题行：完全透明，文字加大并描白边（与深色字反色）
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 4px 6px;
    background: transparent;

    .lib-name {
      font-weight: 700;
      color: #0f172a;
      font-size: 17px;
      // text-shadow 模拟描边：4 个方向各 1px 反色（深色字 → 白边）
      text-shadow:
        -1px -1px 0 #fff,
         1px -1px 0 #fff,
        -1px  1px 0 #fff,
         1px  1px 0 #fff,
         0    -1px 0 #fff,
         0     1px 0 #fff,
        -1px  0   0 #fff,
         1px  0   0 #fff;
    }
  }

  // 统计行：完全透明，依赖文字自身的白色柔光描边保证可读
  .lib-stats {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    align-items: center;
    padding: 8px 10px;
    background: transparent;
    border-radius: 6px;
    font-size: 13px;
    line-height: 1.4;
    min-width: 0;

    &.placeholder {
      color: #475569;
      justify-content: center;
      gap: 6px;
    }
    &.error {
      color: #f56c6c;
      background: rgba(254, 240, 240, 0.92);
    }

    .stat-cell {
      display: inline-flex;
      align-items: baseline;
      gap: 4px;
      white-space: nowrap;

      // 4 方向 1px 描边：背景完全透明时让文字在任意海报背景上保持可读
      .stat-label,
      .stat-value {
        text-shadow:
          -1px 0 0 #fff,
           1px 0 0 #fff,
           0 -1px 0 #fff,
           0  1px 0 #fff;
      }
      .stat-label { color: #475569; }
      .stat-value {
        color: #0f172a;
        font-weight: 600;
      }
    }

    .err-msg { flex: 1; word-break: break-all; }
    .spin { animation: spin 1.2s linear infinite; }
  }

  // 重扫按钮：嵌在 lib-stats 内、靠右；白色前景；hover 旋转
  .rescan-btn {
    margin-left: auto;
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    border: none;
    background: transparent;
    color: #fff;
    cursor: pointer;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    transition: transform 0.4s ease, background 0.15s;

    .el-icon {
      font-size: 14px;
      color: #fff;
      // 描深色边让白色 icon 在浅海报上也清晰
      filter: drop-shadow(0 0 1px rgba(0, 0, 0, 0.6));
    }

    &:hover {
      transform: rotate(180deg);
      background: rgba(255, 255, 255, 0.2);
    }

    &.is-loading {
      animation: spin 1s linear infinite;
      pointer-events: none;
    }
  }

  // 没封面的卡片：纯白底，去掉所有局部蒙版和描边（白底上无意义）
  &:not(.has-cover) {
    .card-content { background: #fff; }
    .lib-stats { background: transparent; backdrop-filter: none; }
    .card-header .lib-name { text-shadow: none; }
    .stat-cell .stat-label,
    .stat-cell .stat-value { text-shadow: none; }
    .rescan-btn {
      color: #f59e0b;
      .el-icon { color: #f59e0b; filter: none; }
    }
  }
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>

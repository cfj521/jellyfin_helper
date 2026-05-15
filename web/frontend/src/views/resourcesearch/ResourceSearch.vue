<template>
  <div class="page-container">
    <div class="page-header">
      <h2>种子搜索 (Jackett)</h2>
      <div class="header-actions">
        <a
          v-if="jackettUrl"
          class="ext-link"
          :href="jackettUrl"
          target="_blank"
          rel="noopener noreferrer"
          title="在新窗口打开 Jackett 后台"
        >
          <el-icon><Link /></el-icon>
          打开 Jackett
        </a>
      </div>
    </div>

    <el-card shadow="never" class="form-card">
      <el-form inline @submit.prevent="search">
        <el-form-item label="关键词">
          <el-input v-model="form.query" placeholder="电影/剧集名称" clearable style="width: 560px" @keyup.enter="search" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="form.category" style="width: 130px">
            <el-option label="全部" value="all" />
            <el-option label="电影" value="movie" />
            <el-option label="剧集" value="tv" />
            <el-option label="动漫" value="anime" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="search" :loading="loading">
            <el-icon><Search /></el-icon>
            搜索
          </el-button>
        </el-form-item>
      </el-form>

      <!-- 关键字 chip -->
      <div v-if="keywords.length" class="presets-bar">
        <span class="presets-label">关键字：</span>
        <el-tag
          v-for="kw in keywords"
          :key="kw"
          :type="isKeywordActive(kw) ? 'primary' : 'info'"
          :effect="isKeywordActive(kw) ? 'dark' : 'plain'"
          class="preset-chip"
          @click="toggleKeyword(kw)"
        >
          {{ kw }}
        </el-tag>
        <el-link type="info" :underline="false" class="presets-edit" @click="$router.push('/settings')">
          编辑
        </el-link>
      </div>
    </el-card>

    <el-card shadow="never" class="results-card">
      <template #header>
        <div class="card-header">
          <span class="results-count">结果 {{ results.length }} 条</span>
          <el-pagination
            v-if="results.length > pageSize"
            v-model:current-page="currentPage"
            :page-size="pageSize"
            :page-sizes="[20, 50, 100, 200]"
            :total="results.length"
            layout="total, sizes, prev, pager, next, jumper"
            background
            small
            class="results-pagination"
            @size-change="onPageSizeChange"
          />
        </div>
      </template>

      <el-table
        :data="paginatedResults"
        v-loading="loading"
        height="100%"
        class="results-table"
      >
        <el-table-column prop="title" label="标题" min-width="300" show-overflow-tooltip />
        <el-table-column prop="indexer" label="来源" width="140" show-overflow-tooltip />
        <el-table-column label="大小" width="100" sortable :sort-method="(a, b) => a.size - b.size">
          <template #default="{ row }">{{ formatSize(row.size) }}</template>
        </el-table-column>
        <el-table-column prop="seeders" label="做种" width="80" sortable />
        <el-table-column prop="peers" label="下载" width="80" />
        <el-table-column prop="category_desc" label="分类" width="120" show-overflow-tooltip />
        <el-table-column prop="publish_date" label="发布" width="180">
          <template #default="{ row }">{{ formatDate(row.publish_date) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button
              size="small"
              type="primary"
              :disabled="!row.magnet && !row.link"
              @click="pushDownload(row)"
            >
              下载
            </el-button>
            <el-link v-if="row.details" :href="row.details" target="_blank" type="primary" style="margin-left: 8px">
              详情
            </el-link>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!loading && !results.length" description="搜索结果为空" />
    </el-card>
  </div>
</template>

<script setup>
// 给组件起 name，让 App.vue 的 <keep-alive :include="['TorrentSearch']"> 能命中
defineOptions({ name: 'TorrentSearch' })

import { ref, computed, onMounted, onActivated } from 'vue'
import { useRoute } from 'vue-router'
import { Search, Link } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { discoverApi, configApi } from '@/api'

const route = useRoute()
const form = ref({ query: '', category: 'all' })
const results = ref([])
const loading = ref(false)
const keywords = ref([])
const defaultKeywords = ref('')
const jackettUrl = ref('')  // 用于"打开 Jackett 后台"链接

const loadConfig = async () => {
  try {
    const r = await configApi.getFull()
    const j = r.data?.config?.jackett || {}
    keywords.value = j.search_keywords || []
    defaultKeywords.value = j.default_keywords || ''
    if (j.host) {
      jackettUrl.value = String(j.host).replace(/\/$/, '') + '/UI/Dashboard'
    }
  } catch {
    keywords.value = []
    defaultKeywords.value = ''
  }
}

// 判断关键字是否已在 query 中（按空格分词，忽略大小写）
const isKeywordActive = (kw) => {
  const tokens = form.value.query.trim().split(/\s+/).filter(Boolean).map(t => t.toLowerCase())
  return tokens.includes(kw.toLowerCase())
}

const toggleKeyword = (kw) => {
  const tokens = form.value.query.trim().split(/\s+/).filter(Boolean)
  const lower = kw.toLowerCase()
  const idx = tokens.findIndex(t => t.toLowerCase() === lower)
  if (idx >= 0) {
    tokens.splice(idx, 1)
  } else {
    tokens.push(kw)
  }
  form.value.query = tokens.join(' ')
}

const sortedResults = computed(() =>
  [...results.value].sort((a, b) => (b.seeders || 0) - (a.seeders || 0))
)

// 分页（前端本地分页 —— 后端一次性返回 limit 条，由用户翻页查看）
const currentPage = ref(1)
const pageSize = ref(50)

const paginatedResults = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return sortedResults.value.slice(start, start + pageSize.value)
})

const onPageSizeChange = (size) => {
  pageSize.value = size
  currentPage.value = 1
}

const search = async () => {
  if (!form.value.query.trim()) {
    ElMessage.warning('请输入搜索关键词')
    return
  }
  loading.value = true
  // 重新搜索后回到第一页
  currentPage.value = 1
  try {
    const res = await discoverApi.search({
      query: form.value.query,
      category: form.value.category,
      limit: 200,
    })
    results.value = res.data.results || []
    if (!results.value.length) {
      ElMessage.info('没有找到结果，可能需要换关键词或检查 Jackett indexer')
    }
  } catch (e) {
    console.error('搜索失败', e)
  } finally {
    loading.value = false
  }
}

// ---- 下载：把种子 push 到分析队列 ----
// 后端流程：
//   1. qB add_torrent(stop_condition='MetadataReceived') → 只下 metadata 就暂停
//   2. dispatch_map(phase='analyzing') 占位
//   3. analyzer 事件驱动 → 拿 metadata + 识别 + 算路径
//   4. 高置信 → phase='dispatch_queued' + qB resume → downloader-watcher 接管
//      低置信 → phase='analyzing' status='needs_review' + qB 保持暂停 → 用户在下载流水线页审核
//
// 用户在搜索时选的 category（非 all）当 user_hint 透传，命中后 confidence=1.0 直接自动入。
const pushDownload = async (row) => {
  try {
    await discoverApi.push({
      title: row.title,
      magnet: row.magnet || undefined,
      torrent_url: !row.magnet ? row.link : undefined,
      source: 'jackett',
      // 搜索分类不是"全部"时，作为 user_hint 让 identify 直接命中 → 高置信自动入流水线
      user_hint_media_type:
        form.value.category && form.value.category !== 'all'
          ? form.value.category
          : undefined,
    })
    ElMessage.success(`${row.title}：已加入分析队列，识别后自动入流水线`)
  } catch (e) {
    ElMessage.error('推送失败：' + (e.response?.data?.detail || e.message))
  }
}

const formatSize = (bytes) => {
  if (!bytes) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024
    i++
  }
  return `${bytes.toFixed(2)} ${units[i]}`
}

const formatDate = (s) => (s ? s.replace('T', ' ').slice(0, 16) : '-')

// 根据 route.query 决定是否触发新搜索：
//   - 有 q（从其他页面跳来 / 主动带参数刷新）→ 覆盖 form 并搜索
//   - 无 q → 不动 form（让 keep-alive 保留的旧搜索结果继续显示）
//
// 跟踪上一次处理过的 q，避免同一个 query 在 keep-alive 激活时被重复触发搜索
let lastHandledQuery = null

const applyRouteQuery = () => {
  const q = route.query.q ? String(route.query.q).trim() : ''
  const t = route.query.type ? String(route.query.type) : ''
  // 唯一性 key：q + type（区分同 q 不同 type 的情况）
  const key = q ? `${q}|${t}` : null
  if (!key || key === lastHandledQuery) return
  lastHandledQuery = key

  let finalQ = q
  if (defaultKeywords.value && !finalQ.toLowerCase().includes(defaultKeywords.value.toLowerCase())) {
    finalQ = (finalQ + ' ' + defaultKeywords.value).trim()
  }
  form.value.query = finalQ
  if (t) form.value.category = t
  search()
}

onMounted(async () => {
  // 首次挂载：先 loadConfig 再决定是否搜索 / 预填
  await loadConfig()
  if (route.query.q) {
    applyRouteQuery()
  } else if (defaultKeywords.value && !form.value.query) {
    // 直接进搜索页且没有保留状态：预填默认关键字（用户可继续输入主关键词）
    form.value.query = defaultKeywords.value
  }
})

// keep-alive 激活：从其他页面切回来时触发。
// 只有 route.query.q 跟上次处理过的不一样才会重新搜索；否则保留上次的状态。
onActivated(() => {
  if (route.query.q) applyRouteQuery()
})
</script>

<style lang="scss" scoped>
.page-header .header-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.ext-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 12px;
  font-size: 13px;
  color: #475569;
  text-decoration: none;
  border: 1px solid #e2e8f0;
  border-radius: 4px;
  background: #fff;
  transition: all 0.15s ease;

  &:hover {
    color: #00a4dc;
    border-color: #00a4dc;
  }

  .el-icon {
    font-size: 14px;
  }
}

.form-card {
  margin-bottom: 20px;
}

// 整页 flex 布局：表单卡固定高度，结果卡占满剩余
.page-container {
  display: flex;
  flex-direction: column;
  height: 100%;     // 父容器（el-main padding 后的可用高度）
  min-height: 0;    // 关键：让 flex child 可被压缩 + 内部 scroll
}

.results-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;

  // el-card 内部结构 → body 拉满 + table 内部滚动
  :deep(.el-card__body) {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    padding: 0;          // 让 table 边到边
  }
}

.results-table {
  flex: 1;
  min-height: 0;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;

  .results-count {
    color: #475569;
    font-weight: 500;
  }

  .results-pagination {
    margin-left: auto;   // 靠右对齐
  }
}

.presets-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px dashed #e2e8f0;
  margin-top: 4px;

  .presets-label {
    color: #64748b;
    font-size: 13px;
  }

  .preset-chip {
    cursor: pointer;
    user-select: none;
    transition: transform 0.15s;

    &:hover {
      transform: translateY(-1px);
    }
  }

  .presets-edit {
    margin-left: auto;
    font-size: 12px;
  }
}
</style>

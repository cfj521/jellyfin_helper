<template>
  <div class="page-container">
    <div class="page-header">
      <h2>种子搜索 (Jackett)</h2>
    </div>

    <el-card shadow="never" class="form-card">
      <el-form inline @submit.prevent="search">
        <el-form-item label="关键词">
          <el-input v-model="form.query" placeholder="电影/剧集名称" clearable style="width: 300px" @keyup.enter="search" />
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

    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>结果 ({{ results.length }} 条)</span>
        </div>
      </template>

      <el-table :data="sortedResults" v-loading="loading" max-height="600">
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
              :loading="row.pushing"
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
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { discoverApi, configApi } from '@/api'

const route = useRoute()
const form = ref({ query: '', category: 'all' })
const results = ref([])
const loading = ref(false)
const keywords = ref([])
const defaultKeywords = ref('')

const loadConfig = async () => {
  try {
    const r = await configApi.getFull()
    const j = r.data?.config?.jackett || {}
    keywords.value = j.search_keywords || []
    defaultKeywords.value = j.default_keywords || ''
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

const search = async () => {
  if (!form.value.query.trim()) {
    ElMessage.warning('请输入搜索关键词')
    return
  }
  loading.value = true
  try {
    const res = await discoverApi.search({
      query: form.value.query,
      category: form.value.category,
      limit: 100,
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

const pushDownload = async (row) => {
  try {
    await ElMessageBox.confirm(
      `推送 ${row.title} 到 qBittorrent 下载？`,
      '确认下载',
      { confirmButtonText: '确定', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  row.pushing = true
  try {
    await discoverApi.push({
      title: row.title,
      magnet: row.magnet || undefined,
      torrent_url: !row.magnet ? row.link : undefined,
      source: 'jackett',
    })
    ElMessage.success('已推送到 qBittorrent')
  } catch (e) {
    console.error('推送失败', e)
  } finally {
    row.pushing = false
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

onMounted(async () => {
  await loadConfig()
  if (route.query.q) {
    // 从其他页面跳转过来：query 中已有内容，按 default_keywords 补足
    let q = String(route.query.q).trim()
    if (defaultKeywords.value && !q.toLowerCase().includes(defaultKeywords.value.toLowerCase())) {
      q = (q + ' ' + defaultKeywords.value).trim()
    }
    form.value.query = q
    if (route.query.type) {
      form.value.category = String(route.query.type)
    }
    search()
  } else if (defaultKeywords.value) {
    // 直接进搜索页：预填默认关键字（用户可继续输入主关键词）
    form.value.query = defaultKeywords.value
  }
})
</script>

<style lang="scss" scoped>
.form-card {
  margin-bottom: 20px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
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

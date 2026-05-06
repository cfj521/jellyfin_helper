<template>
  <div>
    <StatCards :cards="cards" />

    <!-- 模式提示 -->
    <el-alert
      v-if="!r.execute && total > 0"
      type="info"
      :closable="false"
      show-icon
      title="预览模式"
      description="下方列表是按规则会重命名的字幕清单，未实际改动文件。如需执行，回到 toolbar 取消"
      style="margin-bottom: 12px"
    />

    <el-card shadow="never">
      <template #header>
        <div class="header">
          <span>字幕重命名清单</span>
          <span class="muted">
            共 {{ groupedDirs.length }} 个目录、{{ total }} 个字幕
            <template v-if="task.status === 'running'"> · 扫描中</template>
          </span>
        </div>
      </template>

      <el-empty
        v-if="!total && task.status !== 'running'"
        description="未发现需要对齐的字幕（可能所有字幕已与视频同名）"
      />
      <div v-else-if="!total" class="placeholder">
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>正在扫描，发现的待改名字幕会逐步出现...</span>
      </div>

      <el-collapse v-else v-model="expandedDirs" class="dir-list">
        <el-collapse-item
          v-for="g in groupedDirs"
          :key="g.directory"
          :name="g.directory"
        >
          <template #title>
            <div class="dir-title">
              <el-icon><Folder /></el-icon>
              <span class="dir-name">{{ g.directory }}</span>
              <el-tag size="small" type="info">{{ typeLabel(g.type) }}</el-tag>
              <el-tag size="small" :type="r.execute ? 'success' : 'warning'">
                {{ r.execute ? '已重命名' : '待重命名' }} {{ g.items.length }}
              </el-tag>
            </div>
          </template>

          <el-table :data="g.items" stripe size="small">
            <el-table-column label="原文件名" min-width="280">
              <template #default="{ row }">
                <span class="filename">{{ row.old_name }}</span>
              </template>
            </el-table-column>
            <el-table-column label="新文件名" min-width="280">
              <template #default="{ row }">
                <span class="filename arrow">→</span>
                <span class="filename">{{ row.new_name }}</span>
              </template>
            </el-table-column>
            <el-table-column label="检测语言" width="110">
              <template #default="{ row }">
                <el-tag v-if="extractLang(row.new_name)" size="small">
                  {{ langLabel(extractLang(row.new_name)) }}
                </el-tag>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column v-if="hasFailures" label="结果" width="100">
              <template #default="{ row }">
                <el-tag :type="row.success ? 'success' : 'danger'" size="small">
                  {{ row.success ? (r.execute ? '✓ 已改名' : '✓ 待执行') : '✗ 失败' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
    </el-card>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { Folder, Loading } from '@element-plus/icons-vue'
import StatCards from './StatCards.vue'

const props = defineProps({ task: { type: Object, required: true } })
const r = computed(() => props.task.result || {})
const details = computed(() => r.value.details || [])
const total = computed(() => r.value.total ?? details.value.length ?? 0)

const cards = computed(() => {
  const cardList = [
    { label: '总数', value: total.value, color: '#6366f1' },
  ]
  if (r.value.execute) {
    cardList.push(
      { label: '成功', value: r.value.success ?? 0, color: '#10b981' },
      { label: '失败', value: r.value.failed ?? 0, color: '#ef4444' },
    )
  } else {
    cardList.push(
      { label: '待重命名', value: total.value, color: '#f59e0b',
        tip: '预览模式，未实际改名' },
    )
  }
  cardList.push(
    { label: '路径数', value: r.value.paths_processed ?? 0, color: '#94a3b8',
      tip: r.value.execute ? '已执行模式' : '预览模式' },
  )
  return cardList
})

const hasFailures = computed(() => details.value.some(d => d.success === false))

// 按目录分组
const groupedDirs = computed(() => {
  const groups = new Map()
  for (const d of details.value) {
    const key = d.directory || '(未知目录)'
    if (!groups.has(key)) {
      groups.set(key, { directory: key, type: d.type || 'movie', items: [] })
    }
    groups.get(key).items.push(d)
  }
  return Array.from(groups.values())
})

// 默认展开前 3 个
const expandedDirs = ref([])
watch(groupedDirs, (newDirs) => {
  if (!expandedDirs.value.length && newDirs.length) {
    expandedDirs.value = newDirs.slice(0, 3).map(g => g.directory)
  }
}, { immediate: true })

const typeLabel = (t) => ({ movie: '电影', tv: '剧集' }[t] || t || '')

// 从 new_name 反推语言（new_name 形如 movie.chs.srt）
const extractLang = (name) => {
  if (!name) return null
  const m = name.match(/\.(chs|cht|yue|eng|jpn|kor|en|ja|ko|zh-?cn|zh-?tw)\.[^.]+$/i)
  return m ? m[1].toLowerCase() : null
}

const LANG_LABEL = {
  chs: '简中', cht: '繁中', yue: '粤语', eng: '英语', jpn: '日语', kor: '韩语',
  en: '英语', ja: '日语', ko: '韩语', 'zh-cn': '简中', 'zhcn': '简中',
  'zh-tw': '繁中', 'zhtw': '繁中',
}
const langLabel = (code) => LANG_LABEL[code?.toLowerCase()] || code || ''
</script>

<style lang="scss" scoped>
.header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.muted { color: #94a3b8; font-size: 12px; }

.placeholder {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 24px;
  color: #64748b;
  font-size: 13px;
  justify-content: center;
}

.dir-list {
  border: none;
  :deep(.el-collapse-item__header) { padding-right: 8px; }
}

.dir-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;

  .dir-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 500;
  }
}

.filename {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px;
  word-break: break-all;
}
.arrow {
  color: #10b981;
  margin-right: 6px;
  font-weight: bold;
}
</style>

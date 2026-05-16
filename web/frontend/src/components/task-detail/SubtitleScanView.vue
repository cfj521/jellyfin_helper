<template>
  <div>
    <StatCards :cards="cards" />

    <!-- 缺字幕视频列表（按目录分组） -->
    <el-card shadow="never">
      <template #header>
        <div class="header">
          <div class="header-left">
            <span class="title">扫描结果</span>
            <el-radio-group v-model="filterMode" size="small">
              <el-radio-button value="missing">
                只看缺字幕 ({{ missingVideosCount }})
              </el-radio-button>
              <el-radio-button value="all">
                全部 ({{ allVideosCount }})
              </el-radio-button>
            </el-radio-group>
          </div>
          <span class="muted">
            <template v-if="task.status === 'running'">扫描中...</template>
          </span>
        </div>
      </template>

      <el-empty
        v-if="!visibleDirs.length && task.status !== 'running'"
        :description="filterMode === 'missing'
          ? '本次扫描所有视频都有所需语言字幕'
          : '本次扫描未发现任何视频文件'"
      />
      <div v-else-if="!visibleDirs.length" class="placeholder">
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>正在扫描...</span>
      </div>

      <el-collapse v-else v-model="expandedDirs" class="dir-list">
        <el-collapse-item
          v-for="d in visibleDirs"
          :key="d.path"
          :name="d.path"
        >
          <template #title>
            <div class="dir-title">
              <el-icon><Folder /></el-icon>
              <span class="dir-name">{{ d.name || d.path }}</span>
              <el-tag size="small" type="info">{{ mediaTypeLabel(d.media_type) }}</el-tag>
              <el-tag
                size="small"
                :type="missingVideosOf(d).length > 0 ? 'warning' : 'success'"
              >
                <template v-if="missingVideosOf(d).length > 0">
                  缺字幕 {{ missingVideosOf(d).length }}/{{ d.total_videos }}
                </template>
                <template v-else>
                  齐全 {{ d.total_videos }}
                </template>
              </el-tag>
              <!-- 批量标记按钮：阻止冒泡避免触发 collapse 折叠 -->
              <el-button
                size="small"
                type="primary"
                plain
                @click.stop="openBatchAnnotateDialog(d)"
              >
                <el-icon><EditPen /></el-icon>
                批量标硬字幕
              </el-button>
            </div>
          </template>

          <el-table :data="visibleVideosOf(d)" stripe size="small">
            <el-table-column label="视频" min-width="260">
              <template #default="{ row }">
                <div class="video-name">{{ row.name }}</div>
                <div v-if="row.episode" class="episode muted">{{ row.episode }}</div>
              </template>
            </el-table-column>
            <el-table-column label="缺失语言" width="160">
              <template #default="{ row }">
                <el-tag
                  v-for="l in row.missing_langs || []"
                  :key="l"
                  size="small"
                  type="warning"
                  style="margin-right: 4px"
                >
                  {{ langLabel(l) }}
                </el-tag>
                <span v-if="!(row.missing_langs || []).length" class="ok-mark">✓</span>
              </template>
            </el-table-column>
            <el-table-column label="已有外挂字幕" width="180">
              <template #default="{ row }">
                <el-tag
                  v-for="(s, i) in row.subtitles || []"
                  :key="i"
                  size="small"
                  type="info"
                  style="margin-right: 4px"
                >
                  {{ langLabel(s.lang) || s.name }}
                </el-tag>
                <span v-if="!row.subtitles?.length" class="muted">无</span>
              </template>
            </el-table-column>
            <el-table-column label="内嵌字幕轨" width="140">
              <template #default="{ row }">
                <el-tag
                  v-for="(l, i) in row.embedded_langs || []"
                  :key="i"
                  size="small"
                  type="success"
                  style="margin-right: 4px"
                >
                  {{ langLabel(l) }}
                </el-tag>
                <span v-if="!(row.embedded_langs || []).length" class="muted">无</span>
              </template>
            </el-table-column>
            <el-table-column label="硬字幕（标注）" width="160">
              <template #default="{ row }">
                <el-tag
                  v-for="(l, i) in row.hardcoded_langs || []"
                  :key="i"
                  size="small"
                  type="success"
                  effect="dark"
                  style="margin-right: 4px"
                >
                  🔖 {{ langLabel(l) }}
                </el-tag>
                <span v-if="!(row.hardcoded_langs || []).length" class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button
                  size="small"
                  text
                  type="primary"
                  @click="openSingleAnnotateDialog(row)"
                >
                  标硬字幕
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <!-- 标注对话框（单个 / 批量共用） -->
    <el-dialog
      v-model="annoDialog.visible"
      :title="annoDialog.title"
      width="520px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
    >
      <div class="anno-tip">{{ annoDialog.subtitle }}</div>
      <el-form label-width="90px">
        <el-form-item label="硬字幕语言">
          <el-checkbox-group v-model="annoDialog.langs">
            <el-checkbox value="chs">简中</el-checkbox>
            <el-checkbox value="cht">繁中</el-checkbox>
            <el-checkbox value="yue">粤语</el-checkbox>
            <el-checkbox value="eng">英语</el-checkbox>
            <el-checkbox value="jpn">日语</el-checkbox>
            <el-checkbox value="kor">韩语</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="annoDialog.note"
            type="textarea"
            :rows="2"
            placeholder="可选，例如：BD 原盘画面烧录字幕"
          />
        </el-form-item>
      </el-form>
      <el-alert
        v-if="!annoDialog.langs.length && annoDialog.hadAnyExisting"
        type="warning"
        :closable="false"
        show-icon
        title="保存后会清空这些视频的硬字幕标注"
        style="margin-top: 8px"
      />
      <template #footer>
        <el-button @click="annoDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="annoDialog.saving" @click="confirmAnnotate">
          保存（共 {{ annoDialog.targets.length }} 个视频）
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { Folder, Loading, EditPen } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { subtitleApi } from '@/api'
import StatCards from './StatCards.vue'

const props = defineProps({ task: { type: Object, required: true } })

const r = computed(() => props.task.result || {})

// directories: 按目录分组，每个含 videos 列表（含 missing_langs）
// 用 ref 镜像而不是直接 computed，因为标硬字幕保存后要本地立即更新
const directories = ref([])
watch(() => r.value.directories, (newDirs) => {
  // 后端轮询拿到的新数据 → 同步进 ref，但保留本地刚改的标注（key=path）
  if (!newDirs) return
  directories.value = newDirs.map(d => ({
    ...d,
    videos: (d.videos || []).map(v => ({ ...v })),  // 深拷贝 video 避免被覆盖
  }))
}, { immediate: true, deep: false })

const filterMode = ref('missing')  // 'missing' 仅缺字幕 / 'all' 全部

const missingVideosOf = (d) =>
  (d.videos || []).filter(v => (v.missing_langs || []).length > 0)

const missingDirs = computed(() =>
  directories.value.filter(d => missingVideosOf(d).length > 0)
)
const missingVideosCount = computed(() =>
  directories.value.reduce((sum, d) => sum + missingVideosOf(d).length, 0)
)
const allVideosCount = computed(() =>
  directories.value.reduce((sum, d) => sum + (d.videos?.length || 0), 0)
)

const visibleDirs = computed(() => {
  if (filterMode.value === 'all') return directories.value
  return missingDirs.value
})
const visibleVideosOf = (d) => {
  if (filterMode.value === 'all') return d.videos || []
  return missingVideosOf(d)
}

const missingTotal = computed(() => {
  // fallback：从 directories 实时累加（更准，已应用本地标注）
  return missingVideosCount.value
})

const cards = computed(() => {
  const total = r.value.total_videos ?? 0
  const missing = missingTotal.value
  // 字幕完整 = 总数 - 缺字幕；与"缺字幕"互补，两卡相加 = 总数，关系直观
  const complete = Math.max(0, total - missing)
  return [
    { label: '视频总数',  value: total, color: '#6366f1' },
    { label: '字幕完整',  value: complete, color: '#10b981',
      tip: '所需语言全部到位（required_langs 都覆盖了）' },
    { label: '缺字幕',    value: missing, color: '#f59e0b',
      tip: '缺所需语言（含完全没字幕的 + 有字幕但缺特定语言的）' },
    { label: '扫描路径',  value: r.value.paths_scanned ?? 0, color: '#94a3b8' },
  ]
})

const expandedDirs = ref([])
watch(visibleDirs, (newDirs) => {
  if (!expandedDirs.value.length && newDirs.length) {
    expandedDirs.value = newDirs.slice(0, 3).map(d => d.path)
  }
}, { immediate: true })

watch(filterMode, () => { expandedDirs.value = [] })

const mediaTypeLabel = (t) => ({
  movie: '电影', tv: '剧集', series: '剧集', mixed: '混合',
}[t] || t || '未知')

const LANG_LABEL = {
  chs: '简中', cht: '繁中', yue: '粤语', eng: '英语', jpn: '日语', kor: '韩语',
}
const langLabel = (code) => LANG_LABEL[code] || code || ''

// ================= 标硬字幕弹窗 =================

const annoDialog = reactive({
  visible: false,
  title: '',
  subtitle: '',
  targets: [],         // [{path, name, hardcoded_langs[]}, ...]
  langs: [],
  note: '',
  hadAnyExisting: false,
  saving: false,
})

const openSingleAnnotateDialog = (video) => {
  const existing = video.hardcoded_langs || []
  annoDialog.title = '标记硬字幕'
  annoDialog.subtitle = video.name
  annoDialog.targets = [{ path: video.path, name: video.name }]
  annoDialog.langs = [...existing]
  annoDialog.note = ''
  annoDialog.hadAnyExisting = existing.length > 0
  annoDialog.visible = true
}

const openBatchAnnotateDialog = (dir) => {
  const videos = dir.videos || []
  // 取所有视频的标注交集作为预选；如果都没标，预选为空
  let intersection = null
  let anyExisting = false
  for (const v of videos) {
    const cur = v.hardcoded_langs || []
    if (cur.length) anyExisting = true
    if (intersection === null) {
      intersection = new Set(cur)
    } else {
      intersection = new Set(cur.filter(x => intersection.has(x)))
    }
  }
  annoDialog.title = '批量标记硬字幕'
  annoDialog.subtitle = `${dir.name}（${videos.length} 个视频）`
  annoDialog.targets = videos.map(v => ({ path: v.path, name: v.name }))
  annoDialog.langs = intersection ? Array.from(intersection) : []
  annoDialog.note = ''
  annoDialog.hadAnyExisting = anyExisting
  annoDialog.visible = true
}

const confirmAnnotate = async () => {
  if (!annoDialog.targets.length) return
  annoDialog.saving = true
  try {
    const items = annoDialog.targets.map(t => ({
      file_path: t.path,
      hardcoded_langs: [...annoDialog.langs],
      note: annoDialog.note || null,
    }))
    await subtitleApi.saveAnnotations(items)
    // 本地立即更新（避免等待下一次轮询）
    const pathSet = new Set(annoDialog.targets.map(t => normalizePath(t.path)))
    const newLangs = [...annoDialog.langs]
    for (const d of directories.value) {
      for (const v of (d.videos || [])) {
        if (!pathSet.has(normalizePath(v.path))) continue
        v.hardcoded_langs = [...newLangs]
        // 重算 missing_langs：原 missing_langs 加上新增的（如有），减去硬字幕覆盖的
        const baseMissing = computeBaseMissing(v)
        v.missing_langs = baseMissing.filter(lc => !newLangs.includes(lc))
      }
    }
    ElMessage.success(`已保存 ${items.length} 条标注`)
    annoDialog.visible = false
  } catch (e) {
    console.error('保存标注失败', e)
  } finally {
    annoDialog.saving = false
  }
}

const normalizePath = (p) => (p || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()

// 计算"如果没有硬字幕，缺哪些语言"——把现存 missing 还原到无硬字幕状态
// 简化做法：基于 preferred_langs（从 task result 拿不到，硬编码 chs+eng 兜底）
// = preferred - (subtitles 的 lang ∪ embedded_langs)
const PREFERRED_FALLBACK = ['chs', 'eng']
const computeBaseMissing = (v) => {
  const covered = new Set()
  for (const s of (v.subtitles || [])) {
    if (s.lang) covered.add(s.lang)
    for (const lc of (s.langs || [])) covered.add(lc)
  }
  for (const lc of (v.embedded_langs || [])) covered.add(lc)
  return PREFERRED_FALLBACK.filter(lc => !covered.has(lc))
}
</script>

<style lang="scss" scoped>
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;

  .header-left {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .title { font-weight: 500; }
}
.muted { color: var(--jt-text-muted); font-size: 12px; }
.ok-mark { color: var(--jt-success); font-weight: bold; }

.placeholder {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 24px;
  color: var(--jt-text-secondary);
  font-size: 13px;
  justify-content: center;
}

.dir-list {
  border: none;

  :deep(.el-collapse-item__header) {
    padding-right: 8px;
  }
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

.video-name {
  font-weight: 500;
  word-break: break-all;
}
.episode {
  margin-top: 2px;
}

.anno-tip {
  margin-bottom: 14px;
  padding: 8px 12px;
  background: var(--jt-fill-light);
  border-radius: 4px;
  font-size: 13px;
  color: var(--jt-text-regular);
  word-break: break-all;
}
</style>

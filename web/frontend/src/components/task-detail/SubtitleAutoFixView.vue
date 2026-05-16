<template>
  <div>
    <StatCards :cards="cards" />

    <!-- 两段进度 -->
    <div class="phase-row">
      <div class="phase">
        <div class="phase-icon">①</div>
        <div class="phase-body">
          <div class="phase-title">扫描</div>
          <div class="phase-detail">
            视频 {{ scan.total_videos ?? 0 }} · 完全无字幕 {{ scan.without_subtitles ?? 0 }}
            <el-tag v-if="r.reused_from_task" size="small" type="success" effect="plain" style="margin-left:6px">
              复用 #{{ r.reused_from_task }}
            </el-tag>
          </div>
        </div>
      </div>
      <el-divider direction="vertical" class="phase-divider" />
      <div class="phase">
        <div class="phase-icon">②</div>
        <div class="phase-body">
          <div class="phase-title">下载</div>
          <div class="phase-detail">
            成功 {{ download.success ?? 0 }} / 共 {{ download.total ?? 0 }}
            <el-tag v-if="r.dry_run" size="small" type="info">预览</el-tag>
          </div>
        </div>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <!-- 扫描详情：逐视频列出（缺哪些语言、已有什么） -->
      <el-tab-pane :label="`扫描详情 (${scanDetails.length})`" name="scan">
        <el-empty v-if="!scanDetails.length" description="扫描详情还在生成中…" />
        <template v-else>
          <el-table :data="pagedScan" stripe size="small">
            <el-table-column label="视频" min-width="320">
              <template #default="{ row }">
                <div class="video-cell">
                  <div class="video-name">{{ row.video_name || '—' }}</div>
                  <div class="video-path muted">{{ row.video_path }}</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="已有字幕" min-width="180">
              <template #default="{ row }">
                <el-tag
                  v-for="(s, i) in row.subtitles || []"
                  :key="`s${i}`"
                  size="small"
                  type="info"
                  effect="plain"
                  class="lang-chip"
                >
                  {{ (s.langs || []).join('+') || s.lang || '?' }} · {{ s.ext }}
                </el-tag>
                <el-tag
                  v-for="(l, i) in row.embedded_langs || []"
                  :key="`e${i}`"
                  size="small"
                  type="primary"
                  effect="plain"
                  class="lang-chip"
                  title="嵌入式字幕轨"
                >
                  内嵌·{{ langLabel(l) }}
                </el-tag>
                <span v-if="!(row.subtitles?.length || row.embedded_langs?.length)" class="muted">无</span>
              </template>
            </el-table-column>
            <el-table-column label="缺失" min-width="140">
              <template #default="{ row }">
                <el-tag
                  v-for="(l, i) in row.missing_langs || []"
                  :key="`m${i}`"
                  size="small"
                  type="warning"
                  effect="plain"
                  class="lang-chip"
                >{{ langLabel(l) }}</el-tag>
                <span v-if="!(row.missing_langs?.length)" class="muted ok-mark">✓ 完整</span>
              </template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-model:current-page="scanPage"
            v-model:page-size="pageSize"
            :total="scanDetails.length"
            :page-sizes="[20, 50, 100, 200]"
            layout="total, sizes, prev, pager, next, jumper"
            small
            background
            class="pager"
          />
        </template>
      </el-tab-pane>

      <!-- 下载详情：每条字幕的最终落盘文件名 + 源 + 语言 -->
      <el-tab-pane :label="`下载详情 (${downloadDetails.length})`" name="download">
        <el-empty v-if="!downloadDetails.length" description="尚无下载条目" />
        <template v-else>
          <el-table :data="pagedDownload" stripe size="small">
            <el-table-column label="视频" min-width="240">
              <template #default="{ row }">
                <div class="video-cell">
                  <div class="video-name">{{ row.video || basename(row.video_path) || '—' }}</div>
                  <div v-if="row.video_path" class="video-path muted">{{ row.video_path }}</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="字幕文件" min-width="240">
              <template #default="{ row }">
                <div class="sub-cell">
                  <div class="sub-name">{{ row.subtitle || '—' }}</div>
                  <div v-if="row.dry_run" class="muted">（预览，未真实落盘）</div>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="源" width="120">
              <template #default="{ row }">
                <el-tag size="small" type="info" effect="plain">{{ sourceLabel(row.source) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="语言" width="100">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ langLabel(row.language) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTagType(row.status)">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="error" label="错误信息" min-width="180" show-overflow-tooltip />
          </el-table>
          <el-pagination
            v-model:current-page="downloadPage"
            v-model:page-size="pageSize"
            :total="downloadDetails.length"
            :page-sizes="[20, 50, 100, 200]"
            layout="total, sizes, prev, pager, next, jumper"
            small
            background
            class="pager"
          />
        </template>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import StatCards from './StatCards.vue'

const props = defineProps({ task: { type: Object, required: true } })

const r = computed(() => props.task.result || {})
const scan = computed(() => r.value.scan || {})
const download = computed(() => r.value.download || {})
const scanDetails = computed(() => r.value.scan_details || [])
const downloadDetails = computed(() => r.value.download_details || [])

const cards = computed(() => [
  { label: '视频总数', value: scan.value.total_videos      ?? 0, color: '#6366f1' },
  { label: '已下载',  value: download.value.success         ?? 0, color: '#10b981',
    suffix: download.value.total ? ` / ${download.value.total}` : '' },
  { label: '完全无字幕', value: scan.value.without_subtitles ?? 0, color: '#94a3b8' },
  { label: '失败/跳过', value: (download.value.failed || 0) + (download.value.skipped || 0),
    color: '#f59e0b',
    tip: r.value.jellyfin_refreshed ? '已通知 Jellyfin 刷新' : '' },
])

// 任务运行中可能逐步从 scan 切到 download；默认按当前数据自动选 tab
const activeTab = ref('scan')
watch(downloadDetails, (v) => {
  // 一旦有下载详情进来，就切到下载 tab 让用户看到实时进展
  if (v.length && activeTab.value === 'scan') activeTab.value = 'download'
}, { immediate: true })

// 分页（两 tab 共享 pageSize，便于用户保留偏好；当前页独立）
const pageSize = ref(50)
const scanPage = ref(1)
const downloadPage = ref(1)
// 任务还在运行时数据持续追加，自动 reset 到第 1 页可能打断阅读；保持当前页即可。
// 但如果数据缩到当前页之前（如切任务），自动回首页。
watch(scanDetails,     v => { if ((scanPage.value - 1) * pageSize.value >= v.length) scanPage.value = 1 })
watch(downloadDetails, v => { if ((downloadPage.value - 1) * pageSize.value >= v.length) downloadPage.value = 1 })

const pagedScan = computed(() => {
  const s = (scanPage.value - 1) * pageSize.value
  return scanDetails.value.slice(s, s + pageSize.value)
})
const pagedDownload = computed(() => {
  const s = (downloadPage.value - 1) * pageSize.value
  return downloadDetails.value.slice(s, s + pageSize.value)
})

const basename = (p) => {
  if (!p) return ''
  return String(p).replace(/\\/g, '/').split('/').pop()
}

const SOURCE_LABEL = {
  assrt: '射手网',
  opensubtitles: 'OpenSubtitles',
  shooter: 'Shooter',
}
const sourceLabel = (s) => SOURCE_LABEL[s] || s || '—'

const LANG_LABEL = {
  chs: '简', cht: '繁', zh: '中', eng: 'EN', jpn: '日', kor: '韩',
  fre: '法', fra: '法', ger: '德', deu: '德', spa: '西', ita: '意', rus: '俄',
  und: '未知',
}
// 支持 dot-separated 复合（chs.eng → "简+EN"）
const langLabel = (code) => {
  if (!code) return '—'
  return String(code).split('.').map(p => LANG_LABEL[p.toLowerCase()] || p.toUpperCase()).join('+')
}

// 后端 status 取值（来自 tools/subtitle_downloader/main.py）：
//   success / exists / skipped / not_found / failed / archive_unhandled
const statusTagType = (s) => {
  if (s === 'success' || s === 'downloaded') return 'success'
  if (s === 'exists' || s === 'skipped') return 'info'
  if (s === 'not_found') return 'warning'
  return 'danger'
}
</script>

<style lang="scss" scoped>
.phase-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  margin-bottom: 14px;
}
.phase {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;

  .phase-icon {
    width: 28px; height: 28px;
    border-radius: 50%;
    background: #6366f1;
    color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-weight: 600;
    font-size: 13px;
    flex-shrink: 0;
  }
  .phase-title { font-weight: 600; font-size: 13px; }
  .phase-detail { font-size: 12px; color: #64748b; margin-top: 2px; }
}
.phase-divider { height: 30px; }

.video-cell, .sub-cell {
  line-height: 1.4;
}
.video-name, .sub-name { word-break: break-all; font-weight: 500; }
.video-path { font-size: 11px; margin-top: 2px; word-break: break-all; }
.muted { color: #94a3b8; font-size: 12px; }
.ok-mark { color: #10b981; }
.lang-chip {
  margin-right: 4px;
  margin-bottom: 2px;
}
.pager {
  margin-top: 12px;
  justify-content: flex-end;
  display: flex;
}
</style>

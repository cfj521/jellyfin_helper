<template>
  <div>
    <StatCards :cards="cards" />

    <!-- 三段进度（已结束的任务展示三段汇总） -->
    <div class="phase-row">
      <div class="phase">
        <div class="phase-icon">①</div>
        <div class="phase-body">
          <div class="phase-title">扫描</div>
          <div class="phase-detail">
            视频 {{ scan.total_videos ?? 0 }} · 缺字幕 {{ scan.without_subtitles ?? 0 }}
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
      <el-divider direction="vertical" class="phase-divider" />
      <div class="phase">
        <div class="phase-icon">③</div>
        <div class="phase-body">
          <div class="phase-title">对齐重命名</div>
          <div class="phase-detail">
            成功 {{ rename.success ?? 0 }} / 共 {{ rename.total ?? 0 }}
          </div>
        </div>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane :label="`下载详情 (${downloadDetails.length})`" name="download">
        <el-empty v-if="!downloadDetails.length" description="无下载条目" />
        <el-table v-else :data="downloadDetails" stripe size="small">
          <el-table-column label="视频" min-width="280">
            <template #default="{ row }">
              <span class="video-name">{{ row.video || row.path || '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="lang" label="语言" width="80" />
          <el-table-column prop="status" label="状态" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTagType(row.status)">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="错误信息" />
        </el-table>
      </el-tab-pane>

      <el-tab-pane :label="`重命名详情 (${renameDetails.length})`" name="rename">
        <el-empty v-if="!renameDetails.length" description="无重命名条目" />
        <el-table v-else :data="renameDetails" stripe size="small">
          <el-table-column label="原文件名" min-width="240">
            <template #default="{ row }">{{ row.from || row.original || '-' }}</template>
          </el-table-column>
          <el-table-column label="新文件名" min-width="240">
            <template #default="{ row }">{{ row.to || row.renamed || '-' }}</template>
          </el-table-column>
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.success ? 'success' : 'danger'">
                {{ row.success ? '成功' : '失败' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import StatCards from './StatCards.vue'

const props = defineProps({ task: { type: Object, required: true } })

const r = computed(() => props.task.result || {})
const scan = computed(() => r.value.scan || {})
const download = computed(() => r.value.download || {})
const rename = computed(() => r.value.rename || {})
const downloadDetails = computed(() => r.value.download_details || [])
const renameDetails = computed(() => r.value.rename_details || [])

const cards = computed(() => [
  { label: '视频总数', value: scan.value.total_videos      ?? 0, color: '#6366f1' },
  { label: '已下载',  value: download.value.success         ?? 0, color: '#10b981',
    suffix: download.value.total ? ` / ${download.value.total}` : '' },
  { label: '已对齐',  value: rename.value.success           ?? 0, color: '#9333ea' },
  { label: '失败/跳过', value: (download.value.failed || 0) + (download.value.skipped || 0),
    color: '#f59e0b',
    tip: r.value.jellyfin_refreshed ? '已通知 Jellyfin 刷新' : '' },
])

const activeTab = ref(downloadDetails.value.length ? 'download' : 'rename')

// 后端 status 取值（来自 tools/subtitle_downloader/main.py）：
//   success / exists / skipped / not_found / failed / archive_unhandled
// 不同语义用不同颜色，避免把"已存在/搜不到"这种业务结果误染成 danger 红
const statusTagType = (s) => {
  if (s === 'success' || s === 'downloaded') return 'success'
  if (s === 'exists' || s === 'skipped') return 'info'      // 已有字幕跳过下载，正常业务流
  if (s === 'not_found') return 'warning'                    // 搜不到，有缺憾但不算错
  return 'danger'                                            // failed / archive_unhandled / 未预期
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

.video-name { word-break: break-all; }
</style>

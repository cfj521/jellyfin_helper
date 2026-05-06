<template>
  <div class="page-container">
    <!-- 顶栏：返回 + 库名 + 操作 -->
    <div class="page-header">
      <div class="header-left">
        <el-button link @click="$router.push('/jellyfin/libraries')">
          <el-icon><ArrowLeft /></el-icon>
          返回
        </el-button>
        <h2>
          <el-icon class="lib-icon"><component :is="typeIcon" /></el-icon>
          {{ library?.name || '加载中…' }}
        </h2>
        <el-tag v-if="library" :type="collectionTypeTagType(library.collection_type)" size="small">
          {{ collectionTypeLabel(library.collection_type) }}
        </el-tag>
      </div>
      <div class="header-right">
        <el-button @click="loadAll" :loading="loading">
          <el-icon><Refresh /></el-icon>
          刷新数据
        </el-button>
        <el-button @click="showDupDialog = true" :disabled="!library?.locations.length">
          <el-icon><Search /></el-icon>
          重复检测
        </el-button>
        <el-button type="warning" @click="showRefreshDialog = true" :loading="refreshing">
          <el-icon><MagicStick /></el-icon>
          通知 Jellyfin 重扫
        </el-button>
      </div>
    </div>

    <!-- 媒体处理工具栏：作用范围 = 当前库 / 选中条目 -->
    <MediaToolbar
      v-if="library"
      :scope="toolbarScope"
      @clear-selection="clearSelection"
    />

    <!-- 顶部并列：媒体路径 + 统计 -->
    <div v-if="library" class="top-row">
      <!-- 路径列表卡片 -->
      <el-card shadow="never" class="paths-card">
        <template #header>
          <div class="card-header">
            <span>媒体路径</span>
            <el-tag size="small" type="info">{{ library.locations.length }} 个</el-tag>
          </div>
        </template>
        <div class="paths-list">
          <div v-for="(loc, idx) in library.locations_status || library.locations.map(p => ({ path: p, accessible: true }))"
               :key="idx" class="path-row">
            <el-icon :class="loc.accessible ? 'ok' : 'fail'">
              <component :is="loc.accessible ? 'Check' : 'Close'" />
            </el-icon>
            <span class="path-text">{{ loc.path || loc }}</span>
            <el-tag v-if="loc.accessible === false" type="danger" size="small">本机不可访问</el-tag>
          </div>
        </div>
      </el-card>

      <!-- 统计卡片：6 项指标 -->
      <el-card shadow="never" class="stats-card">
        <template #header>
          <div class="card-header">
            <span>
              统计
              <span v-if="stats?._cached" class="cache-hint">
                · 缓存于 {{ formatCacheAge(stats._cache_age_seconds) }}前
              </span>
            </span>
            <el-tooltip content="重新计算并刷新字幕扫描（绕过 2 小时缓存）" placement="left">
              <el-button
                v-if="stats || statsError"
                link
                size="small"
                :loading="loadingStats || subtitleStatsLoading"
                @click="forceRefreshStats"
              >
                <el-icon><Refresh /></el-icon>
                强制刷新统计
              </el-button>
            </el-tooltip>
          </div>
        </template>

        <div v-if="loadingStats && !stats" class="loading-block">
          <el-icon class="spin"><Loading /></el-icon> 加载统计中...
        </div>
        <div v-else-if="statsError" class="error-block">
          <el-alert :title="`加载失败：${statsError}`" type="error" :closable="false" show-icon />
        </div>
        <div v-else-if="stats" class="stats-grid">
          <div v-for="m in metrics" :key="m.label" class="stat-card" :class="{ warn: m.warn }">
            <span class="stat-label">{{ m.label }}</span>
            <span class="stat-value" :style="m.color ? { color: m.color } : null">
              {{ m.value }}<small v-if="m.suffix">{{ m.suffix }}</small>
            </span>
          </div>
        </div>
      </el-card>
    </div>

    <!-- 内容预览：直接展示在页面下方（替代原来的 tabs 默认页） -->
    <el-card shadow="never" class="items-card">
      <template #header>
        <div class="card-header">
          <!-- 排序栏 -->
          <div class="sort-bar">
            <span class="sort-label">排序：</span>
            <button
              v-for="opt in sortOptions"
              :key="opt.field"
              :class="['sort-chip', { active: sortField === opt.field }]"
              @click="setSort(opt.field)"
            >
              {{ opt.label }}
              <el-icon v-if="sortField === opt.field" class="sort-arrow">
                <CaretTop v-if="sortDir === 'asc'" />
                <CaretBottom v-else />
              </el-icon>
            </button>
          </div>

          <!-- 搜索框：跨整库按名称模糊搜（透传 Jellyfin SearchTerm，服务端做匹配） -->
          <el-input
            v-model="searchInput"
            placeholder="按标题搜索本库..."
            clearable
            size="small"
            style="width: 260px"
            @keyup.enter="onSearchSubmit"
            @clear="onSearchSubmit"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>

          <!-- 右侧组：分页 + Folder 开关紧贴在一起，作为整体推到行尾 -->
          <div class="header-right-group">
            <el-pagination
              v-if="itemsTotal > 0"
              v-model:current-page="page"
              v-model:page-size="pageSize"
              :total="itemsTotal"
              :page-sizes="[20, 50, 100, 200]"
              layout="total, sizes, prev, pager, next, jumper"
              background
              small
              class="header-pagination"
              @current-change="loadItems"
              @size-change="onPageSizeChange"
            />
            <!-- 忽略 Folder 开关：与 Jellyfin Web 默认行为对齐 -->
            <div class="toggle-folder">
              <span class="switch-label">忽略 Folder</span>
              <el-switch v-model="hideFolders" size="small" />
              <el-tooltip placement="top">
                <template #content>
                  Jellyfin Web 默认隐藏 type=Folder 的未识别条目。<br/>
                  打开后行为对齐 Jellyfin（仅在本工具中作为查找视图）。
                </template>
                <el-icon class="hint-icon"><InfoFilled /></el-icon>
              </el-tooltip>
            </div>
          </div>
        </div>
      </template>

      <div v-if="itemsLoading" class="loading-block">
        <el-icon class="spin"><Loading /></el-icon> 加载中...
      </div>
      <el-table
        v-else
        ref="itemsTable"
        :data="sortedItems"
        stripe
        size="small"
        max-height="700"
        row-key="id"
        lazy
        :load="loadChildren"
        :tree-props="{ children: '_children', hasChildren: 'has_children' }"
        :indent="32"
        :row-class-name="rowClassName"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="50" />
        <!-- 海报列：作为"第一个 default 列"承接 Element Plus 的 tree prefix（indent + chevron）。
             师哥示例宽度 100；考虑到 Episode 16:9 缩略图 72 + chevron 22 + max indent 64 ≈ 158，
             保险起见略放宽到 170 -->
        <el-table-column label="海报" width="170">
          <template #default="{ row }">
            <a
              v-if="row.detail_url"
              :href="row.detail_url"
              target="_blank"
              rel="noopener noreferrer"
              class="poster-thumb"
              @click.stop
            >
              <el-image
                v-if="row.poster_url"
                :src="row.poster_url"
                :alt="row.name"
                fit="cover"
                lazy
                :class="['poster-img', `poster-img--${(row.type || '').toLowerCase()}`]"
              >
                <template #error>
                  <div class="poster-placeholder">无图</div>
                </template>
              </el-image>
              <div v-else :class="['poster-placeholder', `poster-img--${(row.type || '').toLowerCase()}`]">
                无图
              </div>
            </a>
            <div v-else class="poster-thumb">
              <div :class="['poster-placeholder', `poster-img--${(row.type || '').toLowerCase()}`]">
                无图
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="标题" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            <a
              v-if="row.detail_url"
              :href="row.detail_url"
              target="_blank"
              rel="noopener noreferrer"
              :class="['item-link', `title--${(row.type || '').toLowerCase()}`]"
              @click.stop
            >{{ rowDisplayTitle(row) }}</a>
            <span v-else :class="`title--${(row.type || '').toLowerCase()}`">{{ rowDisplayTitle(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="年份" width="72">
          <template #default="{ row }">
            <span v-if="row.year">{{ row.year }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <!-- 时长：Episode 显示单集时长；Series 显示总时长（聚合后才有）；Season 显示 — -->
        <el-table-column label="时长" width="100" align="center">
          <template #default="{ row }">
            <span v-if="row.type === 'Episode' && row.runtime_min">
              {{ row.runtime_min }} 分
            </span>
            <span v-else-if="row.type === 'Series' && row.total_runtime_min">
              {{ formatTotalRuntime(row.total_runtime_min) }}
            </span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="健康" width="160">
          <template #default="{ row }">
            <div class="health-cell health-cell--problem">
              <!-- 第一行：状态点 + 错误码 / 正常 -->
              <div class="health-line-top">
                <span
                  :class="['health-dot', `level-${row.health?.level || 'ok'}`]"
                  :title="row.health?.level === 'ok' ? '健康' : ''"
                />
                <span v-if="row.health?.level === 'ok'" class="muted">正常</span>
                <el-tooltip v-else placement="right">
                  <template #content>
                    <div class="health-tooltip">
                      <div
                        v-for="(iss, idx) in row.health.issues"
                        :key="idx"
                        class="health-tooltip-line"
                      >• {{ iss.label }}</div>
                    </div>
                  </template>
                  <span class="health-codes-inline">
                    <span
                      v-for="code in summaryCodes(row.health.issues)"
                      :key="code"
                      :class="['health-code-tag', `code-${code}`]"
                    >{{ codeShortLabel(code) }}</span>
                  </span>
                </el-tooltip>
              </div>

              <!-- 第二行：操作按钮（Series/Movie 显示重新识别 + 删除；Episode 显示修缩略图 + 删除；Season 不显示）-->
              <div v-if="row.type !== 'Season'" class="health-actions">
                <button
                  v-if="row.type !== 'Episode'"
                  class="row-btn row-btn--primary"
                  @click.stop="openIdentify(row)"
                >
                  重新识别
                </button>
                <!-- Episode 专属：修缩略图（无图或者用户主动想换）-->
                <button
                  v-if="row.type === 'Episode'"
                  class="row-btn row-btn--primary"
                  :disabled="row._fixingStill"
                  @click.stop="fixSingleStill(row)"
                >
                  {{ row.has_image ? '换缩略图' : '修缩略图' }}
                </button>
                <button
                  v-if="isSampleSuspect(row)"
                  class="row-btn row-btn--danger"
                  @click.stop="openSampleDelete(row, 'sample')"
                >
                  清除 Sample
                </button>
                <button
                  v-else-if="isUnrecognized(row)"
                  class="row-btn row-btn--danger"
                  @click.stop="openSampleDelete(row, 'unrecognized')"
                >
                  删除
                </button>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="type" label="类型" width="78">
          <template #default="{ row }">
            <el-tag size="small" :type="typeTagType(row.type)">
              {{ typeLabel(row.type) }}
            </el-tag>
          </template>
        </el-table-column>
        <!-- 集数：
             Series 行：未拉聚合时显示"X 季"，聚合到位后追加"· Y 集"
             Season 行：显示"Y 集"
             Episode：— -->
        <el-table-column label="集数" width="110" align="center">
          <template #default="{ row }">
            <span v-if="row.type === 'Series' && row.child_count != null">
              {{ row.child_count }} 季<span v-if="row.episode_count != null"> · {{ row.episode_count }} 集</span>
            </span>
            <span v-else-if="row.type === 'Season' && row.child_count != null">
              {{ row.child_count }} 集
            </span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="评分" width="140">
          <template #default="{ row }">
            <div class="rating-cell">
              <span v-if="row.community_rating != null" class="rating jf-rating" title="Jellyfin 社区评分">
                <el-icon><Star /></el-icon>
                {{ row.community_rating.toFixed(1) }}
              </span>
              <!-- 多源评分仅对 Series 拉取（Episode/Season 没独立 TMDB ID 或意义不大）-->
              <RatingsBadges
                v-if="row.type === 'Series' && row.tmdb_id"
                compact
                :rating="ratingFor(row)"
              />
              <!-- 字幕覆盖（只 Series 显示，best-effort：来自最近一次 subtitle_scan）-->
              <span
                v-if="row.type === 'Series' && row.subtitle_coverage"
                :class="['subtitle-coverage-chip', subtitleCoverageClass(row.subtitle_coverage.coverage_pct)]"
                :title="`字幕覆盖：${row.subtitle_coverage.with_required} / ${row.subtitle_coverage.total_videos} 集`"
              >
                字幕 {{ row.subtitle_coverage.coverage_pct }}%
              </span>
              <span v-if="row.community_rating == null && !(row.type === 'Series' && ratingFor(row))" class="muted">—</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="演员图" width="100" align="center">
          <template #default="{ row }">
            <span
              v-if="row.actors_total"
              :class="row.actors_with_image < row.actors_total ? 'actor-incomplete' : 'actor-ok'"
            >
              {{ row.actors_with_image }} / {{ row.actors_total }}
            </span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="TMDB" width="100" align="center">
          <template #default="{ row }">
            <!-- Episode 没有独立 TMDB ID，直接 — -->
            <span v-if="row.type === 'Episode'" class="muted">—</span>
            <a
              v-else-if="row.tmdb_id"
              :href="tmdbUrl(row)"
              target="_blank"
              rel="noopener noreferrer"
              class="tmdb-link"
              @click.stop
            >
              <el-icon><Link /></el-icon>
              {{ row.tmdb_id }}
            </a>
            <el-tag v-else type="info" size="small" effect="plain">未绑定</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="文件路径" width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="path-cell">
              <div class="path-text" :title="row.path">{{ row.path || '—' }}</div>
              <button
                v-if="row.path"
                class="row-btn row-btn--ghost copy-btn"
                @click.stop="copyPath(row.path)"
              >
                复制
              </button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!itemsLoading && !items.length" description="暂无内容" />
    </el-card>

    <!-- 重复检测对话框 -->
    <el-dialog v-model="showDupDialog" title="重复检测" width="720" :close-on-click-modal="false">
      <div v-if="library?.locations.length > 1" class="dup-path-pick">
        <span class="dup-pick-label">检测路径：</span>
        <el-radio-group v-model="dupPath" size="small">
          <el-radio v-for="loc in library.locations" :key="loc" :label="loc">{{ loc }}</el-radio>
          <el-radio label="__all__">全部路径</el-radio>
        </el-radio-group>
      </div>

      <div v-if="dupResult" class="dup-result">
        <div class="dup-summary">
          <el-tag>视频总数: {{ dupResult.total_videos }}</el-tag>
          <el-tag :type="dupResult.potential_duplicates > 0 ? 'warning' : 'success'">
            潜在重复组: {{ dupResult.potential_duplicates }}
          </el-tag>
        </div>

        <el-collapse v-if="dupResult.groups?.length" class="dup-groups">
          <el-collapse-item
            v-for="(group, idx) in dupResult.groups"
            :key="idx"
            :title="`大小相似组 (${group.size_mb} MB) — ${group.files.length} 个文件`"
          >
            <div v-for="file in group.files" :key="file.path" class="file-row">
              <span class="file-name">{{ file.name }}</span>
              <span class="file-path">{{ file.path }}</span>
              <el-tag size="small">{{ formatSize(file.size) }}</el-tag>
            </div>
          </el-collapse-item>
        </el-collapse>

        <el-empty v-else description="未发现重复文件" />
      </div>

      <el-empty v-else-if="!dupLoading" description="点击「开始检测」扫描重复视频" />

      <template #footer>
        <el-button @click="showDupDialog = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="dupLoading"
          :disabled="!library?.locations.length"
          @click="findDuplicates"
        >
          {{ dupResult ? '重新检测' : '开始检测' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 刷新模式选择对话框 -->
    <RefreshLibraryDialog
      v-model="showRefreshDialog"
      :library-name="library?.name"
      :loading="refreshing"
      @confirm="onRefreshConfirm"
    />

    <!-- 重新识别（刮削元数据）对话框 -->
    <IdentifyDialog
      v-model="showIdentifyDialog"
      :item="identifyTarget"
      @applied="onIdentifyApplied"
    />

    <!-- 删除 / 清除 Sample 对话框（共用） -->
    <SampleDeleteDialog
      v-model="showSampleDeleteDialog"
      :item-id="sampleDeleteTargetId"
      :mode="sampleDeleteMode"
      @deleted="onSampleDeleted"
    />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft, Refresh, MagicStick, Loading, Check, Close, Search, Link, Star,
  VideoCamera, VideoPlay, Headset, Folder,
  CaretTop, CaretBottom, InfoFilled,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { jellyfinApi, mediaApi, taskApi, ratingsApi, metadataApi } from '@/api'
import RefreshLibraryDialog from '@/components/RefreshLibraryDialog.vue'
import MediaToolbar from '@/components/MediaToolbar.vue'
import IdentifyDialog from '@/components/IdentifyDialog.vue'
import SampleDeleteDialog from '@/components/SampleDeleteDialog.vue'
import RatingsBadges from '@/components/RatingsBadges.vue'

const route = useRoute()
const router = useRouter()

const id = computed(() => String(route.params.id))

// 库基础信息
const library = ref(null)
const loading = ref(false)

// 概览统计
const stats = ref(null)
const loadingStats = ref(false)
const statsError = ref('')

// 缺字幕统计（懒加载 + 轮询字幕扫描任务）
const subtitleStats = ref(null)        // { status, task_id, without_required, total_videos, ... }
const subtitleStatsLoading = ref(false)
let subtitlePollTimer = null

// 重复检测
const dupResult = ref(null)
const dupLoading = ref(false)
const dupPath = ref('')

// 内容（分页）
const items = ref([])
const itemsTotal = ref(0)
const itemsLoading = ref(false)
const itemsTable = ref(null)
const selectedItems = ref([])
const page = ref(1)
// 评分缓存：{`${tmdb_id}-${media_type}`: RatingResponse}
const ratingsByKey = ref({})
// 标题搜索：v-model 绑输入框，提交后写入 itemsSearch 触发 loadItems
const searchInput = ref('')
const itemsSearch = ref('')
const pageSize = ref(50)
// 忽略 Folder 开关：与 Jellyfin Web 默认行为对齐（默认关闭，即显示所有类型）
const hideFolders = ref(false)

// 经过过滤后的列表（目前只用于"忽略 Folder"，sortedItems 在此基础上排序）
const filteredItems = computed(() => {
  if (hideFolders.value) {
    return items.value.filter(it => it.type !== 'Folder')
  }
  return items.value
})

// 健康问题码 → 简短标签（用于列内紧凑展示）
const _CODE_LABELS = {
  unrecognized: '未识别',
  name_mismatch: '名称错配',
  year_mismatch: '年份错配',
  short_runtime: '时长过短',
  sample_path: 'Sample',
  empty_series: '空剧集',
  empty_season: '空季',
  nested_main_file: '主文件嵌套',
}
const codeShortLabel = (code) => _CODE_LABELS[code] || code

// 取最多两个最具代表性的 issue 码：error 优先于 warning
const summaryCodes = (issues) => {
  if (!issues?.length) return []
  const errorCodes = ['unrecognized', 'name_mismatch', 'year_mismatch']
  const errs = issues.filter(i => errorCodes.includes(i.code))
  const warns = issues.filter(i => !errorCodes.includes(i.code))
  const ordered = [...errs, ...warns]
  return ordered.slice(0, 2).map(i => i.code)
}

// 排序选项与状态
const sortOptions = [
  { field: 'name',         label: '名称' },
  { field: 'health',       label: '健康度' },
  { field: 'type',         label: '类型' },
  { field: 'year',         label: '年份' },
  { field: 'rating',       label: '评分' },
  { field: 'actors_done',  label: '演员图' },
  { field: 'tmdb_bound',   label: 'TMDB' },
]

const sortField = ref('name')
const sortDir = ref('asc') // 'asc' | 'desc'

// 切到不同字段时给个合理默认方向（排查问题/找高分时降序更顺手）
const _defaultDir = (field) =>
  ['health', 'rating', 'year'].includes(field) ? 'desc' : 'asc'

const setSort = (field) => {
  if (sortField.value === field) {
    sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortField.value = field
    sortDir.value = _defaultDir(field)
  }
}

// 单字段比较器：返回 [primary, secondary] 元组（多键稳定排序用）
const _fieldKey = (row, field) => {
  switch (field) {
    case 'name':
      return [(row.name || '').toLocaleLowerCase()]
    case 'health': {
      const lvl = { error: 3, warning: 2, ok: 1 }[row.health?.level || 'ok'] || 0
      const issueCount = row.health?.issues?.length || 0
      return [lvl, issueCount]
    }
    case 'type':
      return [row.type || '']
    case 'year':
      // null 排到最后
      return [row.year == null ? -Infinity : row.year]
    case 'rating':
      return [row.community_rating == null ? -Infinity : row.community_rating]
    case 'actors_done': {
      // 演员图完成度：用比例排序；无演员的当作 1（视为完成）
      if (!row.actors_total) return [1]
      return [row.actors_with_image / row.actors_total]
    }
    case 'tmdb_bound':
      return [row.tmdb_id ? 1 : 0]
    default:
      return [0]
  }
}

const _compare = (a, b) => {
  if (a < b) return -1
  if (a > b) return 1
  return 0
}

const sortedItems = computed(() => {
  const arr = [...filteredItems.value]
  const dir = sortDir.value === 'asc' ? 1 : -1
  arr.sort((a, b) => {
    const ka = _fieldKey(a, sortField.value)
    const kb = _fieldKey(b, sortField.value)
    for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
      const r = _compare(ka[i], kb[i])
      if (r !== 0) return r * dir
    }
    // 主键完全相同时，用名称做次级稳定排序（不受方向影响）
    return _compare(
      (a.name || '').toLocaleLowerCase(),
      (b.name || '').toLocaleLowerCase(),
    )
  })
  return arr
})


const onSelectionChange = (rows) => {
  selectedItems.value = rows
}

// 行 class：有问题的行加色调标记
const rowClassName = ({ row }) => {
  const classes = ['item-row']
  if (row.health?.level === 'error') classes.push('row-health-error')
  else if (row.health?.level === 'warning') classes.push('row-health-warning')
  // 按 row.type 加层级类，给 CSS 提供可靠的层级钩子（el-table 自带的层级类版本不一致）
  if (row.type === 'Season')  classes.push('row-level-season')
  if (row.type === 'Episode') classes.push('row-level-episode')
  return classes.join(' ')
}

// 重新识别（刮削元数据）对话框
const showIdentifyDialog = ref(false)
const identifyTarget = ref(null)

const openIdentify = (row) => {
  identifyTarget.value = row
  showIdentifyDialog.value = true
}

const onIdentifyApplied = ({ itemId }) => {
  // Apply 是异步刷新，给后端 5 秒时间然后重新拉这一页
  setTimeout(() => loadItems(), 5000)
}

// 删除条目 / 清除 Sample 对话框（共用同一个组件）
const showSampleDeleteDialog = ref(false)
const sampleDeleteTargetId = ref('')
const sampleDeleteMode = ref('sample')  // 'sample' | 'unrecognized'

// 疑似 sample —— 健康 issue 命中 sample_path 或 short_runtime
const isSampleSuspect = (row) => {
  const codes = (row.health?.issues || []).map(i => i.code)
  return codes.includes('sample_path') || codes.includes('short_runtime')
}

// 未识别 —— Folder 类型
const isUnrecognized = (row) => row.type === 'Folder'

const openSampleDelete = (row, mode = 'sample') => {
  sampleDeleteTargetId.value = row.id
  sampleDeleteMode.value = mode
  showSampleDeleteDialog.value = true
}

const onSampleDeleted = () => {
  ElMessage.success('已删除，正在重新加载列表')
  loadItems()
}

// 复制路径到剪贴板
const copyPath = async (path) => {
  if (!path) return
  try {
    await navigator.clipboard.writeText(path)
    ElMessage.success('已复制到剪贴板')
  } catch (e) {
    // 老浏览器或 http 环境 fallback
    const ta = document.createElement('textarea')
    ta.value = path
    ta.style.position = 'fixed'
    ta.style.left = '-9999px'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      ElMessage.success('已复制到剪贴板')
    } catch {
      ElMessage.error('复制失败')
    } finally {
      document.body.removeChild(ta)
    }
  }
}

const clearSelection = () => {
  if (itemsTable.value) {
    itemsTable.value.clearSelection()
  }
  selectedItems.value = []
}

const onPageSizeChange = () => {
  page.value = 1
  loadItems()
}

// TMDB 详情页链接（电影/剧集/季 URL 格式不同；Episode 在表格里已显示 —）
const tmdbUrl = (row) => {
  if (row.type === 'Series') return `https://www.themoviedb.org/tv/${row.tmdb_id}`
  if (row.type === 'Movie')  return `https://www.themoviedb.org/movie/${row.tmdb_id}`
  if (row.type === 'Season' && row.tmdb_id) {
    // Season 的 ProviderIds.Tmdb 通常是 season-id，TMDB 没有"按 season-id 直跳"页面
    // 退化为父剧 + season number；如果有更精准做法以后再改
    return `https://www.themoviedb.org/tv/${row.tmdb_id}`
  }
  return `https://www.themoviedb.org/movie/${row.tmdb_id}`
}

// 字幕覆盖率 → 颜色档：>=90% 绿，>=60% 黄，否则红
const subtitleCoverageClass = (pct) => {
  if (pct == null) return ''
  if (pct >= 90) return 'sub-cov-good'
  if (pct >= 60) return 'sub-cov-warn'
  return 'sub-cov-bad'
}

// ===== 时长格式化（聚合后总时长可能上百小时）=====

const formatTotalRuntime = (minutes) => {
  if (!minutes) return ''
  const total = Math.round(minutes)
  if (total < 60) return `${total} 分`
  const h = Math.floor(total / 60)
  const m = total % 60
  if (h < 100) return m ? `${h} 时 ${m} 分` : `${h} 时`
  // 长剧集（>= 100 小时）只显示小时，避免单元格爆字
  return `${h} 时`
}

// ===== 树形表格：标题 / 类型显示帮助函数 =====

/** 标题渲染：Episode 加 SxxExx 前缀（取自 Jellyfin 自带 IndexNumber 字段）*/
const rowDisplayTitle = (row) => {
  if (row.type === 'Episode') {
    const s = row.season_number != null ? String(row.season_number).padStart(2, '0') : '?'
    const e = row.episode_number != null ? String(row.episode_number).padStart(2, '0') : '?'
    return `S${s}E${e} · ${row.name}`
  }
  return row.name
}

/** 类型 tag 颜色 */
const typeTagType = (t) => {
  if (t === 'Movie') return 'success'
  if (t === 'Series') return 'primary'
  if (t === 'Season') return 'warning'
  if (t === 'Episode') return 'info'
  return 'info'
}

/** 类型 tag 文案 */
const typeLabel = (t) => {
  const map = { Movie: '电影', Series: '剧集', Season: '季', Episode: '集', Folder: 'Folder' }
  return map[t] || t
}

// ===== Episode 缩略图修复（单集）=====

/**
 * 用户点击 Episode 行的"修缩略图 / 换缩略图"按钮：
 *   - 创建一个 episode_still_fix_single 任务（后端触发 TMDB still 取图 + 上传 Jellyfin）
 *   - 任务完成后用 task 的 result 反馈给用户
 *   - 暂时不在本页轮询任务进度，让用户去任务页查看（或自己刷新）
 */
const fixSingleStill = async (row) => {
  row._fixingStill = true
  try {
    const res = await metadataApi.fixSingleEpisodeStill(row.id)
    ElMessage.success(`已启动缩略图修复任务 #${res.data.task_id}`)
  } catch (e) {
    // 拦截器已 toast，这里兜底
    console.warn('启动 Episode 缩略图修复失败', e)
  } finally {
    row._fixingStill = false
  }
}

// ===== 树形表格：懒加载子节点 =====

/**
 * el-table 的 lazy load 回调。每个节点点开时调用一次（结果会被表格内部缓存，
 * 后续展开/折叠不再调用）。
 *   row    - 当前父行（Series 或 Season）
 *   resolve - 给表格塞子节点数组
 */
const loadChildren = async (row, treeNode, resolve) => {
  try {
    if (row.type === 'Series') {
      const r = await jellyfinApi.seasonsOfSeries(row.id)
      resolve(r.data.items || [])
    } else if (row.type === 'Season') {
      const r = await jellyfinApi.episodesOfSeason(row.id)
      const eps = r.data.items || []
      resolve(eps)
      // Episode 也批量拉评分（仅当含 tmdb_id 的，目前 episode 多半没有）
      // 不主动拉，由用户在 Series 层级看多源评分即可
    } else {
      resolve([])
    }
  } catch (e) {
    console.error('懒加载子节点失败', e)
    resolve([])
  }
}


/**
 * MediaToolbar 的 scope：
 *   - 没有选中 → 作用于当前库
 *   - 选中条目 → 作用于这些条目（path 列表来自 jellyfin items.path）
 */
const toolbarScope = computed(() => {
  const collectionType = library.value?.collection_type
  if (selectedItems.value.length > 0) {
    return {
      mode: 'items',
      library_id: id.value,
      library_name: library.value?.name,
      collection_type: collectionType,
      item_count: selectedItems.value.length,
      // jellyfin items 接口返回的 path 是文件路径（mkv/mp4 路径或剧集系列目录路径）
      item_paths: selectedItems.value.map(it => it.path).filter(Boolean),
      // Episode 缩略图修复需要 jellyfin item id（路径派生不出来）
      episode_ids: selectedItems.value
        .filter(it => it.type === 'Episode')
        .map(it => it.id),
    }
  }
  return {
    mode: 'library',
    library_id: id.value,
    library_name: library.value?.name,
    collection_type: collectionType,
  }
})

const showDupDialog = ref(false)

/**
 * 6 项指标，按用户要求顺序：
 *   资源数量、电影/剧集数、空间占用、缺海报、缺字幕、TMDB 绑定
 *
 * 不同库类型的"资源数量"含义不同：
 *   - movies / tvshows / musicvideos / homevideos / mixed  → 视频文件
 *   - music                                                → 音频文件
 *   - photos                                               → 图片
 * 缺字幕近似为 max(0, 视频数 - 字幕文件数)；音乐/图片库不适用 → 显示 —
 */
/** 自适应单位的字节格式化 → { value, suffix } */
const formatBytesValue = (bytes) => {
  if (!bytes || bytes <= 0) return { value: '0', suffix: '' }
  const KB = 1024, MB = KB * 1024, GB = MB * 1024, TB = GB * 1024
  if (bytes >= TB) return { value: (bytes / TB).toFixed(2), suffix: ' TB' }
  if (bytes >= GB) return { value: (bytes / GB).toFixed(2), suffix: ' GB' }
  if (bytes >= MB) return { value: (bytes / MB).toFixed(1), suffix: ' MB' }
  return { value: (bytes / KB).toFixed(0), suffix: ' KB' }
}

/** 总时长（秒）格式化为 { value, suffix } */
const formatRuntime = (seconds) => {
  if (!seconds || seconds <= 0) return { value: '—', suffix: '' }
  const total_min = Math.floor(seconds / 60)
  if (total_min < 60) return { value: total_min, suffix: ' m' }
  const h = Math.floor(total_min / 60)
  const m = total_min % 60
  if (h < 100) {
    return m
      ? { value: `${h}h${String(m).padStart(2, '0')}`, suffix: 'm' }
      : { value: h, suffix: ' h' }
  }
  return { value: h, suffix: ' h' }
}

const metrics = computed(() => {
  if (!stats.value) return []
  const fs = stats.value.filesystem || {}
  const jf = stats.value.jellyfin || {}
  const t = library.value?.collection_type

  // 资源数量 + 标签
  let resourceLabel = '视频文件'
  let resourceValue = fs.video_count || 0
  if (t === 'music') {
    resourceLabel = '音频文件'
    resourceValue = fs.audio_count || 0
  } else if (t === 'photos') {
    resourceLabel = '图片'
    resourceValue = fs.image_count || 0
  } else if (t === 'mixed') {
    resourceLabel = '媒体文件'
    resourceValue = (fs.video_count || 0) + (fs.audio_count || 0) + (fs.image_count || 0)
  }

  const movieSeriesCount = (jf.movies || 0) + (jf.series || 0)

  // 字幕覆盖：从独立的字幕扫描结果拿（懒加载，期间显示"统计中"）
  // 与"总体健康度"、"TMDB 绑定"统一为"已完成/总数"正向格式，避免和"缺 X / 总"
  // 这种反向格式混淆。warn 仍以"还有缺漏"为触发条件
  // 'music' / 'photos' 库不适用
  let subtitleCoverage
  if (['music', 'photos'].includes(t)) {
    subtitleCoverage = { value: '—', loading: false }
  } else if (subtitleStats.value?.status === 'ready') {
    const total = subtitleStats.value.total_videos || 0
    const missing = subtitleStats.value.without_required || 0
    const have = Math.max(0, total - missing)
    const pct = total ? Math.round((have / total) * 100) : 0
    subtitleCoverage = {
      value: total ? `${have} / ${total} (${pct}%)` : '—',
      loading: false,
      warn: missing > 0,
    }
  } else if (subtitleStats.value?.status === 'running' || subtitleStatsLoading.value) {
    subtitleCoverage = { value: '统计中…', loading: true }
  } else {
    subtitleCoverage = { value: '—', loading: false }
  }

  // 健康度（放首位）
  const totalItems = jf.total_items || 0
  const healthy = jf.items_healthy ?? totalItems
  const ratio = totalItems ? healthy / totalItems : 1
  let healthColor = '#10b981'
  if (ratio < 0.7) healthColor = '#ef4444'
  else if (ratio < 0.9) healthColor = '#f59e0b'
  else if (ratio < 1) healthColor = '#3b82f6'

  // 占用（自适应单位）
  const sizeFmt = formatBytesValue(fs.total_size_bytes || 0)
  // 总时长
  const runtimeFmt = formatRuntime(jf.total_runtime_seconds || 0)

  return [
    {
      label: '总体健康度',
      value: totalItems
        ? `${healthy} / ${totalItems} (${(ratio * 100).toFixed(0)}%)`
        : '—',
      color: healthColor,
    },
    { label: resourceLabel, value: resourceValue },
    { label: '电影/剧集数', value: movieSeriesCount },
    { label: '空间占用', value: sizeFmt.value, suffix: sizeFmt.suffix },
    { label: '总时长', value: runtimeFmt.value, suffix: runtimeFmt.suffix },
    { label: '缺海报', value: jf.without_poster || 0, warn: (jf.without_poster || 0) > 0 },
    {
      label: '字幕覆盖',
      value: subtitleCoverage.value,
      warn: subtitleCoverage.warn,
      loading: subtitleCoverage.loading,
    },
    { label: 'TMDB 绑定', value: `${jf.with_tmdb_id || 0} / ${jf.total_items || 0}` },
  ]
})

/**
 * 字幕统计：懒加载 + 轮询。
 *  - 收到 ready：直接显示
 *  - 收到 running：保留 task_id，启动 2s 轮询直到任务终态，再读 result
 *  - force=true 时跳过近期任务复用，直接启新扫描
 */
const loadSubtitleStats = async (force = false) => {
  if (!id.value) return
  subtitleStatsLoading.value = true
  // music/photos 库没字幕概念，跳过
  const t = library.value?.collection_type
  if (['music', 'photos'].includes(t)) {
    subtitleStatsLoading.value = false
    return
  }
  try {
    const res = await jellyfinApi.librarySubtitleStats(id.value, 60, force)
    subtitleStats.value = res.data
    if (res.data.status === 'running' && res.data.task_id) {
      startSubtitlePoll(res.data.task_id)
    }
  } catch (e) {
    console.error('字幕统计加载失败', e)
  } finally {
    subtitleStatsLoading.value = false
  }
}

const startSubtitlePoll = (taskId) => {
  stopSubtitlePoll()
  subtitlePollTimer = setInterval(async () => {
    try {
      const res = await taskApi.get(taskId)
      const t = res.data
      // 把当前进度同步到 subtitleStats，让 UI 显示"统计中"
      subtitleStats.value = {
        ...subtitleStats.value,
        status: t.status === 'completed' ? 'ready' : 'running',
        progress: t.progress,
        message: t.message,
        // 完成时从 result 读最终数据
        without_required: t.result?.without_required ?? subtitleStats.value?.without_required,
        total_videos: t.result?.total_videos ?? subtitleStats.value?.total_videos,
      }
      if (['completed', 'failed', 'cancelled'].includes(t.status)) {
        stopSubtitlePoll()
      }
    } catch (e) {
      console.error('轮询字幕扫描任务失败', e)
      stopSubtitlePoll()
    }
  }, 2000)
}

const stopSubtitlePoll = () => {
  if (subtitlePollTimer) {
    clearInterval(subtitlePollTimer)
    subtitlePollTimer = null
  }
}

const refreshing = ref(false)
const showRefreshDialog = ref(false)

// 顶部库标签上用的：处理 collection_type（小写复数：movies/tvshows/...）
const collectionTypeLabel = (t) => ({
  movies: '电影', tvshows: '剧集', music: '音乐', musicvideos: '音乐视频',
  homevideos: '家庭视频', boxsets: '合集', books: '图书', mixed: '混合',
}[t] || t)

const collectionTypeTagType = (t) => ({
  movies: 'success', tvshows: 'primary', music: 'warning', mixed: 'info',
}[t] || '')

const typeIcon = computed(() => {
  const t = library.value?.collection_type
  return ({
    movies: VideoCamera,
    tvshows: VideoPlay,
    music: Headset,
    musicvideos: VideoPlay,
  })[t] || Folder
})

const loadAll = async () => {
  await loadLibrary()
  loadStats()           // 概览统计（同步阻塞）
  loadSubtitleStats()   // 缺字幕统计（懒加载，异步触发后台扫描）
}

const loadLibrary = async () => {
  loading.value = true
  try {
    const res = await jellyfinApi.libraries(true)
    library.value = (res.data.libraries || []).find(l => l.id === id.value)
    if (!library.value) {
      ElMessage.error('未找到该媒体库')
      router.push('/jellyfin/libraries')
      return
    }
    // 默认选第一个路径作为重复检测目标
    if (!dupPath.value && library.value.locations.length) {
      dupPath.value = library.value.locations[0]
    }
  } catch (e) {
    ElMessage.error('加载库信息失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}

const loadStats = async (force = false) => {
  loadingStats.value = true
  statsError.value = ''
  try {
    const res = await jellyfinApi.libraryStats(id.value, force)
    stats.value = res.data
  } catch (e) {
    statsError.value = e.response?.data?.detail || e.message
  } finally {
    loadingStats.value = false
  }
}

/**
 * "强制刷新"按钮：
 *   - 清后端 seasons/episodes 缓存（让下次展开重新拉取）
 *   - 跳过库统计 + 字幕统计的缓存重算
 *   - 重新加载顶层 Series 列表（同时把 tree-table 内部已展开的子节点也清掉）
 */
const forceRefreshStats = async () => {
  stopSubtitlePoll()
  subtitleStats.value = null
  // 1. 清后端 seasons/episodes 内存缓存（30 分钟 TTL 旁路）
  try {
    await jellyfinApi.clearChildrenCache()
  } catch (e) {
    // 缓存清理失败不影响后续刷新，仅打日志
    console.warn('清空 seasons/episodes 缓存失败', e)
  }
  // 2. 重载顶层 Series 列表 —— 替换 items 数组会让 el-table tree state 重置
  loadItems()
  // 3. 重算库 / 字幕统计
  loadStats(true)
  loadSubtitleStats(true)
}

/** 把"缓存秒数"格式化为友好文案（XX 秒前 / XX 分钟前 / XX 小时前）*/
const formatCacheAge = (seconds) => {
  if (!seconds || seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`
  return `${Math.floor(seconds / 3600)} 小时`
}

const loadItems = async () => {
  itemsLoading.value = true
  try {
    const res = await jellyfinApi.libraryItems(id.value, {
      start_index: (page.value - 1) * pageSize.value,
      limit: pageSize.value,
      search: itemsSearch.value || undefined,
    })
    items.value = res.data.items || []
    itemsTotal.value = res.data.total || 0
    fetchRatingsForItems()
    fetchSeriesAggregates()
  } catch (e) {
    ElMessage.error('加载内容失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    itemsLoading.value = false
  }
}

/**
 * 当前页所有 Series 行的聚合摘要（季数/集数/总时长/字幕覆盖）
 * 后端单条结果 1 小时缓存，所以反复进出库页很快
 */
const fetchSeriesAggregates = async () => {
  const seriesIds = items.value
    .filter(x => x.type === 'Series')
    .map(x => x.id)
    .filter(Boolean)
  if (!seriesIds.length) return
  try {
    const res = await jellyfinApi.seriesAggregates(seriesIds)
    const results = res.data?.results || {}
    // 合并：直接赋值到 items 行（Vue 3 对 ref 数组单元的属性变更默认是响应的）
    for (const row of items.value) {
      const agg = results[row.id]
      if (!agg) continue
      // 服务端的 child_count 是季数，aggregate 也是。优先用聚合的（更准）
      if (agg.season_count != null) row.child_count = agg.season_count
      row.episode_count = agg.episode_count
      row.total_runtime_min = agg.total_runtime_min
      row.subtitle_coverage = agg.subtitle_coverage
    }
  } catch (e) {
    // 聚合失败不影响主流程
    console.warn('Series aggregates 加载失败', e)
  }
}

const onSearchSubmit = () => {
  itemsSearch.value = (searchInput.value || '').trim()
  page.value = 1
  loadItems()
}

// 当前页有 tmdb_id 且为 Movie / Series 的条目，批量拉取多源评分
// （Season / Episode 的 ProviderIds 即便有 TMDB ID，也不查多源评分 —— 数据不全）
const fetchRatingsForItems = async () => {
  const payload = items.value
    .filter((x) => x.tmdb_id && (x.type === 'Movie' || x.type === 'Series'))
    .map((x) => ({
      tmdb_id: x.tmdb_id,
      media_type: x.type === 'Series' ? 'tv' : 'movie',
      title: x.name || x.title,
      year: x.year || null,
    }))
  if (!payload.length) return
  try {
    const res = await ratingsApi.batch(payload)
    const next = { ...ratingsByKey.value }
    for (const r of res.data.ratings || []) {
      next[`${r.tmdb_id}-${r.media_type}`] = r
    }
    ratingsByKey.value = next
  } catch (e) {
    console.warn('评分批量拉取失败', e)
  }
}

// 表格用：item → 评分对象（找不到返回 null）
const ratingFor = (row) => {
  const mt = (row.type || '').toLowerCase().includes('series') ? 'tv' : 'movie'
  return ratingsByKey.value[`${row.tmdb_id}-${mt}`] || null
}

const findDuplicates = async () => {
  if (!library.value?.locations.length) return

  dupLoading.value = true
  try {
    if (dupPath.value === '__all__') {
      // 多路径合并：依次检测，结果合并
      const merged = { total_videos: 0, potential_duplicates: 0, groups: [] }
      for (const loc of library.value.locations) {
        try {
          const r = await mediaApi.findDuplicates(loc)
          merged.total_videos += r.data.total_videos || 0
          merged.potential_duplicates += r.data.potential_duplicates || 0
          merged.groups.push(...(r.data.groups || []))
        } catch {}
      }
      dupResult.value = merged
    } else {
      const path = dupPath.value || library.value.locations[0]
      const res = await mediaApi.findDuplicates(path)
      dupResult.value = res.data
    }
  } catch (e) {
    ElMessage.error('检测失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    dupLoading.value = false
  }
}

const onRefreshConfirm = async (mode) => {
  refreshing.value = true
  try {
    await jellyfinApi.refreshLibrary(id.value, mode)
    ElMessage.success({
      message: `已通知 Jellyfin 刷新（模式：${MODE_LABELS[mode]}）`,
      duration: 4000,
    })
    showRefreshDialog.value = false
  } catch (e) {
    console.error(e)
  } finally {
    refreshing.value = false
  }
}

const MODE_LABELS = {
  scan_changes: '扫描新的和有修改的文件',
  missing_metadata: '搜索缺少的元数据',
  replace_all: '覆盖所有元数据',
}

const formatSize = (bytes) => {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024
    i++
  }
  return `${bytes.toFixed(1)} ${units[i]}`
}

onMounted(async () => {
  await loadAll()
  loadItems()  // 内容预览也并行
})

onUnmounted(() => {
  stopSubtitlePoll()
})
</script>

<style lang="scss" scoped>
.page-header {
  .header-left {
    display: flex;
    align-items: center;
    gap: 12px;

    h2 {
      display: flex;
      align-items: center;
      gap: 8px;

      .lib-icon {
        color: #6366f1;
      }
    }
  }

  .header-right {
    display: flex;
    gap: 8px;
  }
}

.top-row {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) 2fr;
  gap: 16px;
  margin-bottom: 16px;

  // 窄屏堆叠
  @media (max-width: 900px) {
    grid-template-columns: 1fr;
  }
}

.paths-card,
.stats-card {
  height: 100%;

  // 头部高度收紧
  :deep(.el-card__header) {
    padding: 8px 14px;
  }
  :deep(.el-card__body) {
    padding: 8px 14px;
  }
}

.cache-hint {
  margin-left: 6px;
  color: #94a3b8;
  font-size: 12px;
  font-weight: normal;
}

.paths-card {
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .paths-list {
    .path-row {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 2px 0;
      font-size: 13px;
      color: #475569;
      line-height: 1.6;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;

      .path-text {
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .ok { color: #10b981; }
      .fail { color: #ef4444; }
    }
  }
}

.stats-card {
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  // 6 项指标一行展示（卡片式）：label 上小字 / value 下大字
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 8px;

    // 中等宽度退化为 3 列两行；窄屏 2 列
    @media (max-width: 900px) {
      grid-template-columns: repeat(3, 1fr);
    }
    @media (max-width: 480px) {
      grid-template-columns: repeat(2, 1fr);
    }

    .stat-card {
      display: flex;
      flex-direction: row;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
      padding: 8px 12px;
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      transition: border-color 0.15s, transform 0.15s;

      &:hover {
        border-color: #c7d2fe;
        transform: translateY(-1px);
      }

      &.warn {
        border-color: #fca5a5;
        background: #fef2f2;

        .stat-value {
          color: #ef4444;
        }
      }

      .stat-label {
        font-size: 12px;
        color: #64748b;
        line-height: 1.2;
        white-space: nowrap;
        flex-shrink: 0;
      }

      .stat-value {
        font-size: 18px;
        font-weight: 700;
        color: #0f172a;
        line-height: 1.2;
        text-align: right;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;

        small {
          font-size: 11px;
          color: #64748b;
          margin-left: 1px;
          font-weight: 400;
        }
      }
    }
  }
}

// 快捷操作：纯按钮组（无 card 包裹），位于媒体路径上方
.quick-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.items-card {
  margin-top: 16px;
  // 关键：el-card 默认 overflow:hidden 会让内部 sticky 失效，
  // 这里允许内容溢出，使分页器能 sticky 到视口底部
  overflow: visible;

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  .poster-thumb {
    // ★ 关键：必须 inline-flex（不能 block flex），否则会把 .cell 里 Element Plus 注入的
    //   indent placeholder + chevron 挤到上一行，海报独占下一行 → 看起来"按钮在海报上方"
    //   且 Episode 完全没缩进
    display: inline-flex;
    align-items: center;
    padding: 4px 0;
    cursor: pointer;
    text-decoration: none;
    vertical-align: middle;
    transition: transform 0.15s, box-shadow 0.15s;

    color: inherit;

    &:hover {
      transform: translateY(-1px);

      :deep(.el-image) {
        box-shadow: 0 4px 10px rgba(15, 23, 42, 0.18);
      }
    }

    .poster-placeholder {
      display: flex;
      align-items: center;
      justify-content: center;
      background: #f1f5f9;
      border-radius: 3px;
      color: #94a3b8;
      font-size: 11px;
    }
  }

  // 不同层级的海报尺寸（与师哥示例一致）：show/season 56×80，episode 72×40
  .poster-img--movie,
  .poster-img--series,
  .poster-img--season,
  :deep(.poster-img--movie),
  :deep(.poster-img--series),
  :deep(.poster-img--season) {
    width: 56px;
    height: 80px;
    border-radius: 4px;
    object-fit: cover;
    flex-shrink: 0;
  }
  .poster-img--episode,
  :deep(.poster-img--episode) {
    width: 72px;
    height: 40px;
    border-radius: 4px;
    object-fit: cover;
    flex-shrink: 0;
  }

  .item-link {
    color: #0f6e56;       // 师哥示例：标题统一深绿色
    text-decoration: none;
    font-size: 14px;
    transition: color 0.15s;

    &:hover {
      color: #1d9e75;
      text-decoration: underline;
    }
  }
  // 顶层（电影 / 剧集）标题：稍大稍粗
  .title--movie,
  .title--series {
    font-size: 15px;
    font-weight: 500;
  }

  // 演员图：不完整 → 红色加粗，完整 → 正常
  .actor-incomplete {
    color: #ef4444;
    font-weight: 600;
  }
  .actor-ok {
    color: #16a34a;
    font-weight: 500;
  }
  .muted { color: #94a3b8; }

  // 评分：统一星标颜色，不分档
  .rating {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-weight: 600;
    font-size: 13px;
    color: #f59e0b;  // 金黄色（star 的语义色）

    .el-icon { font-size: 12px; }
  }

  // ============ Tree-table 展开按钮（完全复刻师哥的实现）============
  // 隐藏 Element Plus 默认箭头图标
  :deep(.el-table__expand-icon .el-icon) {
    display: none;
  }
  // 把 expand-icon 容器改造成圆形按钮（默认绿色）
  :deep(.el-table__expand-icon) {
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: #1d9e75;
    color: #fff;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    position: relative;
    transition: transform 0.25s ease, background 0.2s;
  }
  // 用 ::before 画一个白色向下箭头（▾ 的纯 CSS 版）
  :deep(.el-table__expand-icon::before) {
    content: '';
    width: 0;
    height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #fff;
    margin-top: 2px;
  }
  // 展开状态：箭头旋转 180°（变成向上）
  :deep(.el-table__expand-icon--expanded),
  :deep(.el-table__expand-icon.is-expanded) {
    transform: rotate(180deg);
  }
  :deep(.el-table__expand-icon:hover) {
    background: #0f6e56;
  }

  // ============ 左侧彩色竖条（按层级配色，::before 绝对定位画线）============
  :deep(.el-table__row) {
    position: relative;
  }
  // 第一层：剧/电影 → 深绿
  :deep(.el-table__row:not(.el-table__row--level-1):not(.el-table__row--level-2) > td:first-child)::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 3px;
    background: #1d9e75;
  }
  // 第二层：季 → 浅绿
  :deep(.el-table__row--level-1 > td:first-child)::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 3px;
    background: #97c459;
  }
  // 第三层：集 → 琥珀
  :deep(.el-table__row--level-2 > td:first-child)::before {
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 3px;
    background: #ef9f27;
  }

  // 评分单元格容器：Jellyfin 社区评分 + 多源评分 + 字幕覆盖三行
  .rating-cell {
    display: flex;
    flex-direction: column;
    gap: 4px;
    align-items: flex-start;
  }

  // 字幕覆盖 chip（仅 Series 行显示）
  .subtitle-coverage-chip {
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 8px;
    font-size: 10px;
    font-weight: 500;
    line-height: 14px;
    border: 1px solid;

    &.sub-cov-good { color: #166534; border-color: #86efac; background: #f0fdf4; }
    &.sub-cov-warn { color: #b45309; border-color: #fcd34d; background: #fffbeb; }
    &.sub-cov-bad  { color: #b91c1c; border-color: #fca5a5; background: #fef2f2; }
  }

  // TMDB 详情页链接
  .tmdb-link {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    color: #0ea5e9;
    text-decoration: none;
    font-size: 12px;
    font-family: ui-monospace, monospace;
    transition: color 0.15s;

    &:hover {
      color: #0284c7;
      text-decoration: underline;
    }

    .el-icon {
      font-size: 12px;
    }
  }

  // 忽略 Folder 开关（卡片头右侧）
  .toggle-folder {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;  // 推到最右

    .switch-label {
      font-size: 13px;
      color: #475569;
    }

    .hint-icon {
      color: #94a3b8;
      cursor: help;
      font-size: 14px;
    }
  }

  // 排序栏：chip 风格，活动项品牌色 + 方向箭头
  .sort-bar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px;
    // 不再 margin-right: auto —— toggle-folder 用 margin-left: auto 推到右

    .sort-label {
      font-size: 13px;
      color: #64748b;
      margin-right: 4px;
    }

    .sort-chip {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      padding: 4px 10px;
      font-size: 12px;
      color: #475569;
      background: transparent;
      border: 1px solid #e2e8f0;
      border-radius: 14px;
      cursor: pointer;
      transition: all 0.15s;

      &:hover {
        border-color: #c7d2fe;
        color: #4f46e5;
      }

      &.active {
        background: #6366f1;
        border-color: #6366f1;
        color: #fff;
        font-weight: 500;

        .sort-arrow {
          font-size: 11px;
        }
      }
    }
  }

  // 健康单元格：双行布局（第一行圆点+错误码，第二行刮削按钮）
  .health-cell {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .health-cell--problem {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
  }

  .health-line-top {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  // 健康列下方的操作按钮组
  .health-actions {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
  }

  // 紧凑型行内按钮 —— 用原生 <button> 自己控样式，避免 el-button 的多层 padding
  .row-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 22px;
    padding: 0 8px;
    font-size: 12px;
    line-height: 1;
    border-radius: 4px;
    border: 1px solid transparent;
    background: transparent;
    cursor: pointer;
    user-select: none;
    transition: background 0.15s, border-color 0.15s;

    &--primary {
      color: #4f46e5;
      border-color: #c7d2fe;
      background: #eef2ff;

      &:hover {
        background: #6366f1;
        border-color: #6366f1;
        color: #fff;
      }
    }

    &--danger {
      color: #b91c1c;
      border-color: #fecaca;
      background: #fef2f2;

      &:hover {
        background: #ef4444;
        border-color: #ef4444;
        color: #fff;
      }
    }

    // 中性变体（用于"复制路径"这种非破坏性操作）
    &--ghost {
      color: #475569;
      border-color: #e2e8f0;
      background: #f8fafc;

      &:hover {
        background: #475569;
        border-color: #475569;
        color: #fff;
      }
    }
  }

  // 路径单元格：路径文字 + 第二行复制按钮
  .path-cell {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;

    .path-text {
      font-family: ui-monospace, monospace;
      font-size: 12px;
      color: #475569;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .copy-btn {
      align-self: flex-start;  // 按钮不撑满，左对齐
    }
  }

  .health-codes {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 3px;

    .health-code-tag {
      padding: 1px 6px;
      font-size: 11px;
      line-height: 1.5;
      border-radius: 8px;
      background: #fef2f2;
      color: #b91c1c;
      border: 1px solid #fecaca;
      white-space: nowrap;

      // warning 类用橙色
      &.code-short_runtime,
      &.code-sample_path,
      &.code-empty_series,
      &.code-nested_main_file {
        background: #fef3c7;
        color: #b45309;
        border-color: #fde68a;
      }
    }
  }

  // 健康状态圆点
  .health-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    cursor: help;

    &.level-ok      { background: #d1fae5; border: 1px solid #6ee7b7; }
    &.level-warning { background: #f59e0b; box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.18); }
    &.level-error   { background: #ef4444; box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.20); }
  }

  // 表头右侧组：分页 + Folder 开关绑定在一起整体推到行尾
  .header-right-group {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
    flex-wrap: nowrap;  // 组内不允许换行，分页和开关永远贴一起
  }

  // 分页本身不再需要 margin-left（它的容器负责右推）
  .header-pagination {
    flex-shrink: 0;
  }
}

// 让表格行的 vertical-align 居中，让海报和文字共存时不偏上
:deep(.item-row td) {
  vertical-align: middle;
}

// 健康有问题的行：hover 时浅色高亮（仅视觉提示，不可点击）
:deep(.row-health-error:hover td) {
  background-color: #fef2f2 !important;
}
:deep(.row-health-warning:hover td) {
  background-color: #fffbeb !important;
}

// 让选择列的复选框更显眼：边框加粗 + 颜色加深 + 尺寸略大
.items-card :deep(.el-table) {
  .el-checkbox__inner {
    width: 18px;
    height: 18px;
    border-color: #94a3b8;
    border-width: 2px;

    &::after {
      // 对勾粗一些
      border-width: 2px;
      height: 9px;
      left: 5px;
    }
  }

  .el-checkbox__input.is-checked .el-checkbox__inner,
  .el-checkbox__input.is-indeterminate .el-checkbox__inner {
    background-color: #6366f1;
    border-color: #6366f1;
  }

  // 表头全选框 hover 时颜色更明显
  .el-checkbox__inner:hover {
    border-color: #6366f1;
  }
}

.loading-block, .error-block {
  padding: 40px;
  text-align: center;
  color: #94a3b8;

  .spin {
    animation: spin 1.2s linear infinite;
    margin-right: 6px;
  }
}

.dup-path-pick {
  margin-bottom: 16px;
  padding: 8px 12px;
  background: #f8fafc;
  border-radius: 4px;
}

.dup-summary {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.dup-groups {
  .file-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 4px;
    border-bottom: 1px solid #f1f5f9;

    &:last-child {
      border-bottom: none;
    }

    .file-name {
      font-weight: 500;
      min-width: 200px;
    }

    .file-path {
      flex: 1;
      color: #94a3b8;
      font-size: 12px;
      word-break: break-all;
    }
  }
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>

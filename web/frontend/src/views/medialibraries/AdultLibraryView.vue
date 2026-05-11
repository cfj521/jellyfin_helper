<template>
  <!--
    成人库详情视图。
    页面结构与普通库 LibraryDetail 保持一致：toolbar / top-row(paths+stats) / items-card。
    只是 toolbar 是 4 按钮、stats 是成人专用指标、items 表是横版海报 + 健康列等。
  -->
  <div class="adult-lib-view">
    <!-- 工具栏：与普通库 MediaToolbar 同款样式（gradient bg + 同色按钮） -->
    <div class="media-toolbar">
      <div class="toolbar-scope">
        <el-icon class="scope-icon"><Operation /></el-icon>
        <span class="scope-label">作用范围：{{ library.name }}</span>
      </div>
      <div class="toolbar-actions">
        <el-button type="primary" @click="scanNow" :loading="scanning">
          <el-icon><Search /></el-icon>
          本地库重扫
        </el-button>
        <el-button type="primary" @click="repairMetadata" :loading="repairing.meta">
          <el-icon><MagicStick /></el-icon>
          扫描并修复识别错误
        </el-button>
        <el-button type="primary" @click="repairCovers" :loading="repairing.covers">
          <el-icon><Picture /></el-icon>
          扫描并修复封面图
        </el-button>
        <el-button type="danger" @click="resetAndRescan" :loading="resettingScan">
          <el-icon><Delete /></el-icon>
          清空元数据重扫
        </el-button>
        <!-- 显示排除项开关：默认关，开后列表里包含 excluded=true 的条目（不影响 cooldown 中条目，那些一直显示）
             Jellyfin 视图下"excluded"是 AdultItem 表特有概念，禁用此开关 -->
        <div class="toolbar-switch" :class="{ disabled: filters.use_jellyfin_db }">
          <span class="switch-label">显示排除项</span>
          <el-switch
            v-model="filters.show_excluded"
            size="small"
            :disabled="filters.use_jellyfin_db"
            :title="filters.use_jellyfin_db
              ? 'Jellyfin 视图下不区分排除项；切回 AdultItem 数据源后可用'
              : (filters.show_excluded
                ? '当前显示已排除条目（用户标记为无效番号）'
                : '当前隐藏已排除条目；打开后才能看到/取消排除')"
            @change="onFilterChange"
          />
        </div>
        <!-- 数据源切换开关：靠右 -->
        <div class="toolbar-switch">
          <span class="switch-label">使用 Jellyfin 数据库</span>
          <el-switch
            v-model="filters.use_jellyfin_db"
            size="small"
            :title="filters.use_jellyfin_db
              ? '当前以 Jellyfin 视角列出库内所有视频，反查我们刮的元数据做 cross-ref'
              : '当前以 AdultItem 表为数据源（番号刮削结果）'"
            @change="onFilterChange"
          />
        </div>
      </div>
    </div>

    <!-- top-row: 媒体路径 + 统计（结构与普通库一致） -->
    <div class="top-row">
      <!-- 路径列表卡片 -->
      <el-card shadow="never" class="paths-card">
        <template #header>
          <div class="card-header">
            <span>媒体路径</span>
            <el-tag size="small" type="info">{{ library.locations.length }} 个</el-tag>
          </div>
        </template>
        <div class="paths-list">
          <div
            v-for="(loc, idx) in library.locations_status || library.locations.map(p => ({ path: p, accessible: true }))"
            :key="idx"
            class="path-row"
          >
            <el-icon :class="loc.accessible ? 'ok' : 'fail'">
              <component :is="loc.accessible ? 'Check' : 'Close'" />
            </el-icon>
            <span class="path-text">{{ loc.path || loc }}</span>
            <el-tag v-if="loc.accessible === false" type="danger" size="small">本机不可访问</el-tag>
          </div>
        </div>
      </el-card>

      <!-- 统计卡片：成人专用指标 -->
      <el-card shadow="never" class="stats-card">
        <template #header>
          <div class="card-header">
            <span>统计</span>
            <el-button text size="small" :loading="statsLoading" @click="loadStats" title="刷新统计">
              <el-icon><Refresh /></el-icon>
            </el-button>
          </div>
        </template>
        <div v-if="statsLoading && !statsData" class="loading-block">
          <el-icon class="spin"><Loading /></el-icon> 加载统计中...
        </div>
        <div v-else-if="statsData" class="stats-grid">
          <div
            v-for="m in statsMetrics"
            :key="m.label"
            class="stat-card"
            :class="{ warn: m.warn }"
          >
            <span class="stat-label">{{ m.label }}</span>
            <span class="stat-value">
              {{ m.value }}<small v-if="m.suffix">{{ m.suffix }}</small>
            </span>
          </div>
        </div>
      </el-card>
    </div>

    <!-- 列表卡片：filter 行在 header，table 在 body -->
    <el-card shadow="never" class="items-card">
      <template #header>
        <div class="card-header items-header">
          <!-- 排序栏（与普通库 LibraryDetail 风格一致） -->
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

          <div class="filter-row">
            <el-input
              v-model="filters.search"
              placeholder="搜索 番号 / 标题 / 文件名"
              clearable
              size="small"
              style="width: 240px"
              @change="onFilterChange"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>

            <el-select
              v-model="filters.actresses"
              multiple
              filterable
              collapse-tags
              collapse-tags-tooltip
              placeholder="女优筛选"
              size="small"
              style="width: 220px"
              @change="onFilterChange"
            >
              <el-option
                v-for="a in allActresses"
                :key="a.name"
                :label="actressLabel(a)"
                :value="a.name"
              />
            </el-select>

            <el-select
              v-model="filters.uncensored"
              clearable
              placeholder="码"
              size="small"
              style="width: 110px"
              @change="onFilterChange"
            >
              <el-option label="全部" :value="null" />
              <el-option label="无码" :value="true" />
              <el-option label="有码" :value="false" />
            </el-select>
          </div>

          <!-- 无限滚动模式：替代原分页器 -->
          <span v-if="itemsTotal > 0" class="items-progress">
            已加载 {{ items.length }} / 共 {{ itemsTotal }}
          </span>
          <ViewModeToggle v-model="viewMode" />
        </div>
      </template>

      <!-- 网格视图：海报卡片 grid -->
      <div v-if="viewMode === 'grid'" ref="gridViewRef" v-loading="loading" class="grid-view">
        <div
          v-for="row in displayItems"
          :key="row.id"
          class="grid-card"
          :class="{
            'grid-card--excluded': !row._skeleton && row.excluded,
            'grid-card--cooling': !row._skeleton && row.cooldown_until && new Date(row.cooldown_until).getTime() > Date.now(),
            'grid-card--skeleton': row._skeleton,
          }"
          @click="!row._skeleton && onGridCardClick(row)"
        >
          <template v-if="row._skeleton">
            <div class="grid-poster">
              <div class="sk-block sk-poster" />
            </div>
            <div class="grid-meta">
              <div class="sk-line sk-code" />
              <div class="sk-line sk-title" />
              <div class="sk-line sk-health" />
            </div>
          </template>
          <template v-else>
            <div class="grid-poster">
              <AdultPosterCell :item="row" />
              <el-tooltip
                :content="gridHealthLabel(row)"
                placement="top"
                :show-after="200"
              >
                <span class="grid-health" :class="`grid-health--${gridHealthState(row)}`" />
              </el-tooltip>
              <span
                v-if="row.is_uncensored === true"
                class="grid-cen cen-badge cen-badge--uncensored"
              >无码</span>
            </div>
            <div class="grid-meta">
              <div class="grid-code">{{ row.code || '未识别' }}</div>
              <div class="grid-title" :title="row.title || row.file_name">
                {{ row.title || row.file_name || '—' }}
              </div>
              <!-- 健康度状态行：圆点 + 文字，始终可见 -->
              <div class="grid-health-row" :class="`grid-health-row--${gridHealthState(row)}`">
                <span class="grid-health-dot" />
                <span class="grid-health-text">{{ gridHealthLabel(row) }}</span>
              </div>
            </div>
          </template>
        </div>
        <el-empty v-if="!loading && !displayItems.length" description="此库还没识别到内容" />
      </div>

      <!-- 列表视图：原 el-table（默认）；移除 max-height 让页面级滚动驱动无限滚动 -->
      <el-table
        v-else
        :data="displayItems"
        v-loading="loading"
        stripe
        size="small"
        @selection-change="onSelectionChange"
        :row-class-name="rowClassName"
      >
        <el-table-column type="selection" width="44" />

        <el-table-column label="#" width="56" align="center" class-name="col-row-index">
          <template #default="{ $index }">
            <span class="row-index">{{ $index + 1 }}</span>
          </template>
        </el-table-column>

        <el-table-column label="封面" width="180">
          <template #default="{ row }">
            <AdultPosterCell :item="row" />
          </template>
        </el-table-column>

        <el-table-column label="番号 / 标题" min-width="340">
          <template #default="{ row }">
            <TitleCell :item="row" />
          </template>
        </el-table-column>

        <el-table-column label="时长" width="90" align="center">
          <template #default="{ row }">
            <span v-if="row.runtime_min">{{ formatRuntimeMin(row.runtime_min) }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="健康" width="170">
          <template #default="{ row }">
            <HealthCell
              :item="row"
              @rescrape="onRescrapeOne"
              @delete="onDeleteOne"
              @exclude="onExcludeOne"
              @unexclude="onUnexcludeOne"
              @edit="onEditMetadata"
            />
          </template>
        </el-table-column>

        <el-table-column label="女优" width="180">
          <template #default="{ row }">
            <ActressChips
              :actors="row.actors"
              :resolved-map="actressResolvedMap"
              @open-actress="onOpenActress"
            />
          </template>
        </el-table-column>

        <el-table-column label="标签" width="160">
          <template #default="{ row }">
            <TagChips :tags="row.tags" :max="3" />
          </template>
        </el-table-column>

        <el-table-column label="有/无码" width="100" align="center">
          <template #default="{ row }">
            <el-dropdown
              trigger="click"
              size="small"
              popper-class="cen-dropdown-popper"
              @command="(v) => onSetUncensored(row, v)"
            >
              <span
                class="cen-badge cen-trigger"
                :class="{
                  'cen-badge--uncensored': row.is_uncensored === true,
                  'cen-badge--censored':   row.is_uncensored === false,
                  'cen-badge--unknown':    row.is_uncensored === null || row.is_uncensored === undefined,
                  'cen-badge--manual':     row.is_uncensored_manual,
                }"
                :title="row.is_uncensored_manual ? '手动设置（点击改）' : '自动判定（点击手动覆盖）'"
              >
                <span v-if="row.is_uncensored === true">无码</span>
                <span v-else-if="row.is_uncensored === false">有码</span>
                <span v-else>—</span>
                <span v-if="row.is_uncensored_manual" class="cen-manual-mark">M</span>
              </span>
              <template #dropdown>
                <el-dropdown-menu class="cen-dropdown">
                  <el-dropdown-item
                    :command="true"
                    class="cen-item cen-item--uncensored"
                    :class="{ 'is-current': row.is_uncensored_manual && row.is_uncensored === true }"
                  >无码</el-dropdown-item>
                  <el-dropdown-item
                    :command="false"
                    class="cen-item cen-item--censored"
                    :class="{ 'is-current': row.is_uncensored_manual && row.is_uncensored === false }"
                  >有码</el-dropdown-item>
                  <el-dropdown-item
                    divided
                    :command="null"
                    class="cen-item cen-item--auto"
                    :class="{ 'is-current': !row.is_uncensored_manual }"
                  >自动判定</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>

        <el-table-column label="发行" width="110" sortable>
          <template #default="{ row }">
            <span v-if="row.release_date">{{ row.release_date }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="元数据来源" width="120">
          <template #default="{ row }">
            <MetadataSourceLink v-if="row.source && row.code" :item="row" />
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>

        <el-table-column label="路径" width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="path-cell">
              <span class="path-text mono" :title="row.file_path">{{ row.file_path || '—' }}</span>
              <el-button
                v-if="row.file_path"
                text
                size="small"
                class="path-copy-btn"
                title="复制路径到剪贴板"
                @click.stop="copyPath(row.file_path)"
              >
                <el-icon><DocumentCopy /></el-icon>
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <!-- 无限滚动哨兵：仅作 getBoundingClientRect 的位置锚点，不显示任何文字 -->
      <div ref="sentinelRef" class="scroll-sentinel" aria-hidden="true"></div>

      <el-empty v-if="!loading && !items.length" description="此库还没识别到内容；点「本地库重扫」启动扫描" />
    </el-card>

    <ActressDrawer v-model="actressDrawer.visible" :payload="actressDrawer.payload" />

    <!-- 重新识别对话框（同普通库套路：搜索 → 看候选 → 选择应用） -->
    <AdultIdentifyDialog
      v-model="identifyDialog.visible"
      :item="identifyDialog.item"
      @applied="onIdentifyApplied"
    />

    <!-- 编辑元数据对话框 -->
    <AdultMetadataEditDialog
      v-model="editDialog.visible"
      :item="editDialog.item"
      @saved="onMetadataSaved"
    />
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import {
  Search, MagicStick, Picture, Refresh, Loading,
  Check, Close, Operation, CaretTop, CaretBottom, DocumentCopy, Delete,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { adultApi } from '@/api'
import { debugInfo } from '@/composables/useDebugInfo'
import AdultPosterCell from '@/components/adult/AdultPosterCell.vue'
import TitleCell from '@/components/adult/TitleCell.vue'
import HealthCell from '@/components/adult/HealthCell.vue'
import ViewModeToggle from '@/components/ViewModeToggle.vue'
import { useViewMode } from '@/composables/useViewMode'
import ActressChips from '@/components/adult/ActressChips.vue'
import TagChips from '@/components/adult/TagChips.vue'
import MetadataSourceLink from '@/components/adult/MetadataSourceLink.vue'
import ActressDrawer from '@/components/adult/ActressDrawer.vue'
import AdultIdentifyDialog from '@/components/adult/AdultIdentifyDialog.vue'
import AdultMetadataEditDialog from '@/components/adult/AdultMetadataEditDialog.vue'

const props = defineProps({
  library: { type: Object, required: true },
})

// ---- 列表状态 ----
const items = ref([])
const loading = ref(false)
const selected = ref([])
const allActresses = ref([])
const actressResolvedMap = ref({})

const filters = reactive({
  search: '',
  actresses: [],
  uncensored: null,
  show_excluded: false,    // 默认隐藏 excluded 条目（用户标记为无效番号的）
  use_jellyfin_db: false,  // ON = data_source=jellyfin（按 Jellyfin 视角列）
})
// 无限滚动 + wanted 累加器（对齐 Trending.vue / LibraryDetail.vue 双层模型）
//   items     = 后端数据池（一次拉 FETCH_BATCH 条，比 wanted 大）
//   wanted    = 当前展示目标数（按 stepSize 行累加），displayItems 切片
//   itemsTotal = 库内符合 filter 的总数（toolbar "已加载 X / 共 Y" 用）
const FETCH_BATCH = 30
const itemsTotal = ref(0)
const nextOffset = ref(0)
const hasMore = ref(true)
const loadingMore = ref(false)
const wanted = ref(0)
const sentinelRef = ref(null)           // 底部"加载更多/已到底"提示行
const gridViewRef = ref(null)
let reqSeq = 0
let prefetchTimer = null
// 触发判定：用 sentinel 的 boundingClientRect 判定，与 LibraryDetail 同款
let _loadMoreFiredAt = 0
const SCROLL_TRIGGER_PX = 400

const scanning = ref(false)
const repairing = reactive({ covers: false, meta: false })
const resettingScan = ref(false)

const actressDrawer = reactive({ visible: false, payload: null })
const identifyDialog = reactive({ visible: false, item: null })
const editDialog = reactive({ visible: false, item: null })

// ---- 统计 ----
const statsData = ref(null)
const statsLoading = ref(false)

const statsMetrics = computed(() => {
  const s = statsData.value
  if (!s) return []
  // 健康度：healthy / total —— "完全完整"占比（scraped + 有封面文件 + 有 NFO 文件 三件齐全）
  // 之前是 scraped/total，会忽略"已刮削但缺封面/NFO"的情况导致虚高
  const healthy = s.healthy ?? s.scraped ?? 0  // 后端老版本没 healthy 时 fallback 用 scraped
  const healthRate = s.total ? Math.round((healthy / s.total) * 100) : 0
  return [
    { label: '总数', value: s.total, suffix: '' },
    {
      label: '健康度',
      value: `${healthy} / ${s.total}`,
      suffix: ` (${healthRate}%)`,
      warn: s.total && healthy < s.total,
    },
    { label: '缺封面', value: s.missing_cover, suffix: '', warn: s.missing_cover > 0 },
    { label: '缺 NFO',  value: s.missing_nfo,   suffix: '', warn: s.missing_nfo > 0 },
    { label: '无码',    value: s.uncensored,    suffix: '' },
    { label: '已排除',  value: s.excluded ?? 0, suffix: '' },
    { label: '排除冷却中',  value: s.cooling ?? 0,  suffix: '', warn: (s.cooling ?? 0) > 0 },
    { label: '占用空间', value: formatStatsSize(s), suffix: '' },
    { label: '总时长',  value: formatStatsDuration(s.total_duration_seconds), suffix: '' },
  ]
})

// 字节 → "X.XX TB / GB / MB"，按量级自动选单位
const formatStatsSize = (s) => {
  const bytes = s?.total_size_bytes ?? 0
  if (!bytes) return '0 B'
  const TB = 1024 ** 4, GB = 1024 ** 3, MB = 1024 ** 2
  if (bytes >= TB) return `${(bytes / TB).toFixed(2)} TB`
  if (bytes >= GB) return `${(bytes / GB).toFixed(2)} GB`
  if (bytes >= MB) return `${(bytes / MB).toFixed(0)} MB`
  return `${bytes} B`
}

// 秒 → 最大粒度到 "时"：累计时长哪怕是几百小时也用"X 时 Y 分"
// （之前用"X 天 Y 时"，对成人库这种长期累积的场景一眼看不出实际有多少 h）
const formatStatsDuration = (sec) => {
  if (!sec || sec <= 0) return '0 分'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (h > 0) return `${h} 时 ${m} 分`
  return `${m} 分`
}

const loadStats = async () => {
  statsLoading.value = true
  try {
    const r = await adultApi.stats(props.library.id)
    statsData.value = r.data
  } catch (e) {
    console.warn('stats 加载失败', e)
  } finally {
    statsLoading.value = false
  }
}

// ============ 数据池 + wanted 双层加载模型 ============
// reload = 重置 + 首批；filter / library 切换 / resetAndRescan 都走这条
const reload = async () => {
  if (prefetchTimer) { clearTimeout(prefetchTimer); prefetchTimer = null }
  const seq = ++reqSeq
  loading.value = true
  items.value = []
  itemsTotal.value = 0
  nextOffset.value = 0
  hasMore.value = true
  wanted.value = initialLimit()
  selected.value = []
  try {
    const res = await adultApi.list(_buildListParams(0, FETCH_BATCH))
    if (seq !== reqSeq) return
    const newItems = res.data.items || []
    items.value = newItems
    itemsTotal.value = res.data.total || 0
    nextOffset.value = newItems.length
    hasMore.value = newItems.length >= FETCH_BATCH && nextOffset.value < itemsTotal.value
    resolveActressesForCurrentPage(newItems)
  } catch (e) {
    console.error(e)
    ElMessage.error('加载失败：' + (e.response?.data?.detail || e.message))
    hasMore.value = false
  } finally {
    if (seq === reqSeq) {
      loading.value = false
      _loadMoreFiredAt = 0
      writeDebug()
      prefetchTimer = setTimeout(() => {
        prefetchTimer = null
        prefetchIfNeeded()
      }, 1500)
    }
  }
}

// 后台预取：池子剩余不足 2 步长 → 拉下一批；自递归直到足够
const prefetchIfNeeded = async () => {
  if (loadingMore.value || !hasMore.value) return
  if (items.value.length - wanted.value >= stepSize() * 2) return
  const seq = reqSeq
  loadingMore.value = true
  try {
    const start = nextOffset.value
    const res = await adultApi.list(_buildListParams(start, FETCH_BATCH))
    if (seq !== reqSeq) return
    const newItems = res.data.items || []
    items.value = [...items.value, ...newItems]
    if (res.data.total != null) itemsTotal.value = res.data.total
    nextOffset.value = start + newItems.length
    hasMore.value = newItems.length >= FETCH_BATCH && nextOffset.value < itemsTotal.value
    resolveActressesForCurrentPage(newItems)
  } catch (e) {
    console.warn('后台预取失败:', e)
    hasMore.value = false
  } finally {
    loadingMore.value = false
    writeDebug()
  }
  if (seq === reqSeq && hasMore.value && items.value.length - wanted.value < stepSize() * 2) {
    prefetchIfNeeded()
  }
}

// loadMore：wanted += stepSize；池子告急时后台预取
const loadMore = () => {
  if (loading.value) return
  if (!hasMore.value && items.value.length <= wanted.value) return
  wanted.value += stepSize()
  prefetchIfNeeded()
}

// 共用 params 构造（filter / search / library 都汇聚到这里）
const _buildListParams = (offset, limit) => {
  const params = {
    library_id: props.library.id,
    limit,
    offset,
  }
  if (filters.use_jellyfin_db) {
    params.data_source = 'jellyfin'
  } else {
    params.show_unrecognized = true
  }
  params.show_excluded = !!filters.show_excluded
  if (filters.search) params.search = filters.search
  if (filters.actresses?.length) params.actresses = filters.actresses
  if (filters.uncensored !== null && filters.uncensored !== '') {
    params.uncensored = filters.uncensored
  }
  return params
}

// ============ 滚动触发：sentinel boundingClientRect 判定（详见 LibraryDetail 注释）============
const _maybeLoadMoreOnScroll = () => {
  if (loading.value) return
  if (!hasMore.value && items.value.length <= wanted.value) return
  if (!sentinelRef.value) return
  const rect = sentinelRef.value.getBoundingClientRect()
  const viewportBottom = window.innerHeight || document.documentElement.clientHeight
  if (rect.top - viewportBottom > SCROLL_TRIGGER_PX) return
  const now = Date.now()
  if (now - _loadMoreFiredAt < 300) return
  _loadMoreFiredAt = now
  loadMore()
}

// ============ DEBUG（共享 debugInfo / 侧边栏展示）============
const writeDebug = () => {
  debugInfo.enabled = true
  debugInfo.source = `adult-lib:${viewMode.value}`
  const cols = viewMode.value === 'grid' ? _gridColsEstimate() : 1
  const visible = sortedItems.value.length
  debugInfo.cols = cols
  debugInfo.totalRows = cols ? Math.max(1, Math.ceil(visible / cols)) : 0
  debugInfo.items = items.value.length
  debugInfo.wanted = wanted.value
}

const _gridColsEstimate = () => {
  const el = document.querySelector('.adult-lib-view .grid-view')
  if (!el) return 0
  const w = el.clientWidth || 0
  // 成人卡片宽度 ~180px + 12 gap
  return Math.max(1, Math.floor(w / 192))
}

const updateScrollRow = () => {
  const el = viewMode.value === 'grid'
    ? document.querySelector('.adult-lib-view .grid-view')
    : document.querySelector('.adult-lib-view .el-table__body')
  if (!el) {
    debugInfo.scrollRow = 0
    return
  }
  const rect = el.getBoundingClientRect()
  const offset = Math.max(0, -rect.top)
  const rowH = viewMode.value === 'grid' ? 280 : 80
  debugInfo.scrollRow = Math.floor(offset / rowH) + (offset > 0 ? 1 : 0)
  writeDebug()
}

let _scrollRaf = null
const onWindowScroll = () => {
  if (_scrollRaf) return
  _scrollRaf = requestAnimationFrame(() => {
    updateScrollRow()
    _maybeLoadMoreOnScroll()
    _scrollRaf = null
  })
}

const resolveActressesForCurrentPage = async (rows) => {
  const names = new Set()
  for (const r of rows) {
    for (const n of (r.actors || [])) {
      if (n && !actressResolvedMap.value[n]) names.add(n)
    }
  }
  if (!names.size) return
  try {
    const r = await adultApi.actressResolveBatch(Array.from(names))
    actressResolvedMap.value = { ...actressResolvedMap.value, ...(r.data?.resolved || {}) }
  } catch (e) {
    console.warn('actress resolve-batch 失败', e)
  }
}

/**
 * 女优筛选下拉数据 = 当前库 AdultItem.actors 字段中出现过的所有名字 + 作品数。
 * 不再依赖 AdultActress 表（后者要爬过 javdb 才有 resolved 数据）；
 * 即使女优库没解析过，下拉里也能看到本库出场过的所有演员。
 *
 * 数据形态：[{ name: "上原亜衣", count: 12 }, ...]，按 count 降序。
 * label 优先用 actressResolvedMap 里的中文/英文译名 + 原名 + 作品数；
 * 没解析过就只显示原名 + 作品数。
 */
const loadAllActresses = async () => {
  try {
    const r = await adultApi.listLibraryActors(props.library.id)
    allActresses.value = r.data?.actors || []
  } catch {
    allActresses.value = []
  }
  // 把演员名字交给 resolveBatch，拿到中文译名后下拉自动美化
  if (allActresses.value.length) {
    const names = allActresses.value.map(a => a.name).filter(Boolean)
    try {
      const r = await adultApi.actressResolveBatch(names)
      actressResolvedMap.value = { ...actressResolvedMap.value, ...(r.data?.resolved || {}) }
    } catch {}
  }
}

// 下拉项 label：优先 jp+zh 双语 + 作品数；未解析时显示原名 + 作品数
const actressLabel = (a) => {
  const resolved = actressResolvedMap.value?.[a.name]
  const display = resolved
    ? [resolved.jp_name, resolved.zh_name && resolved.zh_name !== resolved.jp_name ? resolved.zh_name : null]
        .filter(Boolean).join(' / ')
    : a.name
  return `${display} (${a.count})`
}

const onFilterChange = () => {
  // 无限滚动模式：reload() 自身重置游标 + items，等同于回到 page 1
  reload()
}

// ============================================================================
// 排序栏（前端排序：作用于当前页；普通库也是这个策略）
// ============================================================================
const sortOptions = [
  { field: 'code',         label: '番号' },
  { field: 'title',        label: '标题' },
  { field: 'health',       label: '健康度' },
  { field: 'release_date', label: '发行' },
]

const sortField = ref('code')
const sortDir = ref('asc')

// 切到不同字段给个合理默认方向（健康度/发行降序更顺手）
const _defaultDir = (field) =>
  ['health', 'release_date'].includes(field) ? 'desc' : 'asc'

const setSort = (field) => {
  if (sortField.value === field) {
    sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortField.value = field
    sortDir.value = _defaultDir(field)
  }
}

// 健康度排序权重：要让"红 / 灰 / 黄"排在前面便于排查；excluded / cooldown 沉底
const _healthRank = (row) => {
  if (row.excluded) return 0
  if (row.cooldown_until && new Date(row.cooldown_until).getTime() > Date.now()) return 1
  if (!row.code) return 6                                         // 红：未识别
  if (row.source === 'not_found') return 6                        // 红：刮失败
  if (!row.title || !row.source || row.source === 'pending') return 5  // 灰
  // 用后端 cover_local_ok / nfo_local_ok 权威判定（字段值 + 文件实存）
  // 旧逻辑只看字段非空 → 字段有 URL 但本地未下载成功，UI 仍显示"完整"
  const missingCover = (row.cover_local_ok === undefined)
    ? (!row.poster_path && !row.cover_url)
    : !row.cover_local_ok
  const missingNfo = (row.nfo_local_ok === undefined)
    ? !row.nfo_path
    : !row.nfo_local_ok
  if (missingCover || missingNfo) return 4                        // 黄
  return 2                                                         // 绿
}

const _fieldKey = (row, field) => {
  switch (field) {
    case 'code':         return [(row.code || '').toLowerCase()]
    case 'title':        return [(row.title || '').toLocaleLowerCase()]
    case 'health':       return [_healthRank(row)]
    case 'release_date': return [row.release_date || '']
    default:             return [0]
  }
}

const _compare = (a, b) => (a < b ? -1 : a > b ? 1 : 0)

const sortedItems = computed(() => {
  const arr = [...items.value]
  const dir = sortDir.value === 'asc' ? 1 : -1
  arr.sort((a, b) => {
    const ka = _fieldKey(a, sortField.value)
    const kb = _fieldKey(b, sortField.value)
    for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
      const r = _compare(ka[i], kb[i])
      if (r !== 0) return r * dir
    }
    // 主键相同时用 code 做次级稳定排序
    return _compare((a.code || '').toLowerCase(), (b.code || '').toLowerCase())
  })
  return arr
})

const onSelectionChange = (rows) => { selected.value = rows }

// 视图模式（list 表格 / grid 网格），用户偏好持久化在 localStorage
const viewMode = useViewMode('adult-library', 'list')

// ============ wanted / 行步长 / 视口测量（对齐 LibraryDetail.vue）============
const GRID_CARD_W = 280
const GRID_CARD_GAP = 18
const GRID_POSTER_H = 158

const cardsPerRow = () => {
  if (viewMode.value !== 'grid') return 1
  const el = gridViewRef.value
  const containerW = el ? el.clientWidth : Math.max(0, window.innerWidth - 220 - 40)
  return Math.max(1, Math.floor((containerW + GRID_CARD_GAP) / (GRID_CARD_W + GRID_CARD_GAP)))
}

const stepSize = () => {
  return viewMode.value === 'grid' ? cardsPerRow() : 10
}

const initialLimit = () => {
  const el = gridViewRef.value
  let usableH
  if (el) {
    const top = el.getBoundingClientRect().top
    usableH = Math.max(300, window.innerHeight - top)
  } else {
    usableH = Math.max(300, window.innerHeight - 240)
  }
  const rowH = viewMode.value === 'grid' ? (GRID_POSTER_H + 60) : 80
  const visibleRows = Math.max(1, Math.ceil(usableH / rowH))
  return (visibleRows + 1) * cardsPerRow()
}

// displayItems：sortedItems 切到 wanted，gap 用 grid 骨架补；list 不在表内插骨架
const displayItems = computed(() => {
  const w = wanted.value || sortedItems.value.length
  const sliced = sortedItems.value.slice(0, w)
  if (viewMode.value === 'grid') {
    const perRow = cardsPerRow()
    const fillingPool = items.value.length < w && (loadingMore.value || loading.value || hasMore.value)
    if (fillingPool) {
      const gap = Math.max(0, w - sliced.length)
      const skeletonCount = Math.min(gap, perRow * 2)
      const out = sliced.map((r) => ({ ...r, _skeleton: false }))
      for (let i = 0; i < skeletonCount; i++) {
        out.push({ id: `__sk__${i}_${Date.now()}`, _skeleton: true })
      }
      return out
    }
  }
  return sliced
})

// 网格视图圆点状态：复用 HealthCell 的判定逻辑
const gridHealthState = (row) => {
  if (row.excluded) return 'excluded'
  if (row.cooldown_until && new Date(row.cooldown_until).getTime() > Date.now()) return 'cooldown'
  if (!row.code) return 'red'
  if (row.source === 'not_found') return 'red'
  if (!row.title || !row.source || row.source === 'pending') return 'gray'
  // 用后端 cover_local_ok / nfo_local_ok 权威判定（字段值 + 文件实存）
  // 旧逻辑只看字段非空 → 字段有 URL 但本地未下载成功，UI 仍显示"完整"
  const missingCover = (row.cover_local_ok === undefined)
    ? (!row.poster_path && !row.cover_url)
    : !row.cover_local_ok
  const missingNfo = (row.nfo_local_ok === undefined)
    ? !row.nfo_path
    : !row.nfo_local_ok
  if (missingCover || missingNfo) return 'yellow'
  return 'green'
}

// 网格视图健康度文字标签（圆点旁边 / tooltip 都用这个）
const gridHealthLabel = (row) => {
  const state = gridHealthState(row)
  if (state === 'excluded') return '已排除'
  if (state === 'cooldown') {
    const expiry = row.cooldown_until ? new Date(row.cooldown_until).getTime() : 0
    const days = Math.max(0, Math.ceil((expiry - Date.now()) / 86400000))
    return `冷却中 ${days}天`
  }
  if (state === 'red') return row.code ? '刮削失败' : '未识别'
  if (state === 'gray') return '未刮削'
  if (state === 'yellow') {
    // 区分到底缺啥
    const missingCover = (row.cover_local_ok === undefined)
      ? (!row.poster_path && !row.cover_url)
      : !row.cover_local_ok
    const missingNfo = (row.nfo_local_ok === undefined)
      ? !row.nfo_path
      : !row.nfo_local_ok
    if (missingCover && missingNfo) return '缺封面 + NFO'
    if (missingCover) return '缺封面'
    if (missingNfo) return '缺 NFO'
    return '部分缺失'
  }
  return '完整'
}

// 网格卡片单击:未识别 → 打开"重新识别"对话框;已识别 → 仍走重新识别(用户后续可以替换为详情页跳转)
const onGridCardClick = (row) => {
  onRescrapeOne(row)
}

// 打开"编辑元数据"对话框
const onEditMetadata = (item) => {
  if (!item?.id) return
  editDialog.item = item
  editDialog.visible = true
}

// 保存后把返回的新行 merge 进当前 items（_to_dict full=True 返回完整字段）
const onMetadataSaved = ({ itemId, item: updated }) => {
  if (!itemId || !updated) return
  const idx = items.value.findIndex(i => i.id === itemId)
  // 改后变成 excluded（用户点了"清除元数据并排除"）→ 当前列表（默认隐藏 excluded）应该移除该行
  // 直接 reload 比手动 splice 简单可靠：连带 stats 一起更新，分页/排序也保持一致
  if (updated.excluded && !filters.show_excluded) {
    reload()
    return
  }
  if (idx >= 0) {
    items.value[idx] = { ...items.value[idx], ...updated }
  }
  loadStats()
}

// 手动设置 / 清除"有码-无码"标志（value: true=无码 / false=有码 / null=恢复自动）
// 后端返回轻量 patch（仅 is_uncensored / is_uncensored_manual / updated_at），
// 前端按字段 merge —— 不触发 jellyfin path index 重建，响应快得多
const onSetUncensored = async (row, value) => {
  if (!row?.id) return
  // 点的就是当前已选项 → 啥也不干（避免无意义请求）
  // value=true/false 是手动设值；value=null 是清回自动
  const alreadyManualMatch = row.is_uncensored_manual && row.is_uncensored === value
  const alreadyAutoMatch   = value === null && !row.is_uncensored_manual
  if (alreadyManualMatch || alreadyAutoMatch) return

  try {
    const r = await adultApi.setUncensored(row.id, value)
    const patch = r.data || {}
    const idx = items.value.findIndex(i => i.id === row.id)
    if (idx >= 0) {
      items.value[idx] = { ...items.value[idx], ...patch }
    }
    const label = value === true ? '无码' : value === false ? '有码' : '自动判定'
    ElMessage.success(`已标记为「${label}」`)
    loadStats()
  } catch (e) {
    ElMessage.error('修改失败：' + (e.response?.data?.detail || e.message))
  }
}

// 单作品时长格式化：< 60 分 → "XX 分"；否则 "X 时 Y 分"
const formatRuntimeMin = (minutes) => {
  if (!minutes) return ''
  const total = Math.round(minutes)
  if (total < 60) return `${total} 分`
  const h = Math.floor(total / 60)
  const m = total % 60
  return m ? `${h} 时 ${m} 分` : `${h} 时`
}

// 复制路径到剪贴板（带 fallback：clipboard API 在 http 或老浏览器可能不可用）
const copyPath = async (path) => {
  if (!path) return
  try {
    await navigator.clipboard.writeText(path)
    ElMessage.success('已复制到剪贴板')
  } catch {
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

const rowClassName = ({ row }) => {
  // Jellyfin 视角下：未识别为番号是正常的（普通电影/剧集），不再整行染色
  // AdultItem 视角下：状态信息已在 HealthCell 体现（红色圆点+"未识别"/"刮削失败"），无需整行染色
  // 仅冷却中保留淡色提示（区别于失败 vs 已排除）
  if (row.excluded) return 'row-excluded'
  if (row.cooldown_until) {
    const expiry = new Date(row.cooldown_until).getTime()
    if (!Number.isNaN(expiry) && expiry > Date.now()) return 'row-cooling'
  }
  return ''
}

// ---- 3 按钮：每个先弹确认 → 用户点确定再调后端 ----

const confirmAction = async (title, html) => {
  try {
    await ElMessageBox.confirm(html, title, {
      type: 'warning',
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      dangerouslyUseHTMLString: true,
    })
    return true
  } catch {
    return false
  }
}

// 用作每个 confirm 顶部的"作用范围"摘要（与普通库 MediaToolbar 风格一致）
const scopeHtml = () =>
  `<div style="padding: 6px 10px; background: #f1f5f9; border-radius: 4px; font-size: 13px; color: #475569; margin-bottom: 10px;">作用范围：${props.library.name}</div>`

const scanNow = async () => {
  if (!await confirmAction(
    '本地库重扫',
    scopeHtml() + '将扫描磁盘上该库的所有视频文件，识别 番号 并入库（不会刮削元数据）。'
  )) return
  scanning.value = true
  try {
    await adultApi.watcherRunNow(props.library.id)
    ElMessage.success('已启动本地库扫描')
    setTimeout(() => { reload(); loadStats() }, 1500)
  } finally {
    scanning.value = false
  }
}

const repairCovers = async () => {
  if (!await confirmAction(
    '扫描并修复封面图',
    scopeHtml() + '将扫描已识别但缺封面的条目，从对应数据源重新下载封面图。<br>已有图的条目会被跳过。'
  )) return
  repairing.covers = true
  try {
    const r = await adultApi.repairCovers(props.library.id)
    if (r.data.count === 0) {
      ElMessage.info('没有需要修复的封面')
    } else {
      ElMessage.success(`已启动封面修复任务（共 ${r.data.count} 条）`)
    }
  } finally {
    repairing.covers = false
  }
}

const repairMetadata = async () => {
  if (!await confirmAction(
    '扫描并修复识别错误',
    scopeHtml() + '将扫描所有未刮 / 刮失败的条目，重新调用刮削源识别元数据并写回（含封面 + NFO）。'
  )) return
  repairing.meta = true
  try {
    const r = await adultApi.repairMetadata(props.library.id)
    if (r.data.count === 0) {
      ElMessage.info('没有需要修复的条目')
    } else {
      ElMessage.success(`已启动识别修复任务（共 ${r.data.count} 条）`)
    }
  } finally {
    repairing.meta = false
  }
}

// 清空所有 AdultItem 元数据（仅当前库范围）→ 重扫入库 → 同任务衔接刮削
const resetAndRescan = async () => {
  try {
    await ElMessageBox.confirm(
      `<div style="line-height:1.6">
        <p>将<b>清空当前库</b>所有 AdultItem 的识别 + 刮削数据：</p>
        <ul style="margin:8px 0;padding-left:20px;color:#475569;font-size:13px">
          <li>番号、标题、女优、标签、厂商、导演 等元数据</li>
          <li>本地封面 / NFO 路径记录（DB 字段；磁盘文件不动）</li>
          <li>排除 / 冷却 / 失败计数 / 手动覆盖等状态</li>
        </ul>
        <p style="color:#dc2626;margin:6px 0">视频文件、海报图片、NFO <b>文件不会被删除</b>，仅清 DB 记录</p>
        <p>清空后会立即触发一个任务：先扫描入库识别番号，扫完<b>同一任务内</b>衔接刮削。</p>
        <p style="color:#7f1d1d;margin-top:6px">⚠️ NFO / 海报内容会被新数据覆盖</p>
      </div>`,
      '清空元数据重扫',
      {
        confirmButtonText: '确定清空 + 重扫',
        confirmButtonClass: 'el-button--danger',
        cancelButtonText: '取消',
        type: 'warning',
        dangerouslyUseHTMLString: true,
      },
    )
  } catch {
    return
  }
  resettingScan.value = true
  try {
    const r = await adultApi.resetAndRescan(props.library.id)
    ElMessage.success(r.data?.message || '已启动')
    // 立即清空当前页表格让用户视觉感知"已清空"
    items.value = []
    itemsTotal.value = 0
    nextOffset.value = 0
    hasMore.value = true
    loadStats()
  } catch (e) {
    ElMessage.error('启动失败：' + (e.response?.data?.detail || e.message))
  } finally {
    resettingScan.value = false
  }
}

// ---- 单行 ----
// 单行"重新识别" → 弹搜索对话框（与普通库 IdentifyDialog 同套路）
const onRescrapeOne = (item) => {
  if (!item?.id) return
  identifyDialog.item = item
  identifyDialog.visible = true
}

// 用户在对话框里选完候选并 apply 后，patch 表里那一行
const onIdentifyApplied = ({ itemId, row }) => {
  const idx = items.value.findIndex(i => i.id === itemId)
  if (idx >= 0 && row) items.value[idx] = row
  loadStats()
}

const _patchRow = (itemId, newRow) => {
  const idx = items.value.findIndex(i => i.id === itemId)
  if (idx >= 0 && newRow) items.value[idx] = newRow
}

const onExcludeOne = async (item) => {
  if (!item?.id) return
  const label = item.code
    || item.file_name
    || (item.file_path || '').split(/[\\/]/).pop()
    || ''
  try {
    await ElMessageBox.confirm(
      `排除后自动流程不再处理此条。\n\n${label}`,
      '排除条目',
      { type: 'warning', confirmButtonText: '排除', cancelButtonText: '取消' }
    )
  } catch { return }
  try {
    const r = await adultApi.setExcluded(item.id, true)
    _patchRow(item.id, r.data)
    ElMessage.success(`已排除「${item.code || item.file_name}」`)
    loadStats()
  } catch (e) {
    ElMessage.error('排除失败：' + (e.response?.data?.detail || e.message))
  }
}

/**
 * "取消排除 / 立即重试"统一入口（HealthCell 两种状态都 emit 这个事件）：
 *
 *   - 用户主动 excluded：仅清状态，不主动刮削（用户标记了"不是有效番号"，无需立即抓取）
 *   - 自动 cooling：清状态 + 立即同步 rescrape 一次（用户期望"现在就重试"）
 *
 * cooling 项 rescrape 失败时引导用户走"重新识别"对话框（那是低门槛单源遍历，
 * 能展示部分命中的源给用户挑）。
 */
const onUnexcludeOne = async (item) => {
  if (!item?.id) return
  const isCooling = item.cooldown_until
    && new Date(item.cooldown_until).getTime() > Date.now()
    && !item.excluded

  // 第一步：清状态（cooldown_until / excluded / scrape_attempts）
  let cleared
  try {
    const r = await adultApi.setExcluded(item.id, false)
    _patchRow(item.id, r.data)
    cleared = r.data
  } catch (e) {
    ElMessage.error('清除状态失败：' + (e.response?.data?.detail || e.message))
    return
  }

  // 用户主动 excluded：到此为止，等下次自动流程
  if (!isCooling) {
    ElMessage.success(`已取消排除「${item.code || item.file_name}」`)
    loadStats()
    return
  }

  // cooling 状态：必须有 code 才能 rescrape
  if (!cleared?.code) {
    ElMessage.info('已清除冷却，但条目未识别番号 — 请用「重新识别」手动指定')
    loadStats()
    return
  }

  // 立即同步刮削
  ElMessage.info(`已清除冷却，正在重新刮削「${cleared.code}」...`)
  try {
    const r2 = await adultApi.rescrapeItem(item.id)
    _patchRow(item.id, r2.data)
    ElMessage.success(`刮削成功「${r2.data?.title || cleared.code}」`)
  } catch (e2) {
    const detail = e2.response?.data?.detail || e2.message
    ElMessage.error({
      message: `立即重试失败：${detail}\n建议改用「重新识别」对话框（按番号搜，门槛更低）`,
      duration: 6000,
    })
  }
  loadStats()
}

const onDeleteOne = async (item) => {
  // 只展示文件 basename（去掉前面冗长的目录路径），路径拼字符比较糟心
  const fileName = (item.file_path || '').split(/[\\/]/).pop() || item.file_name || item.code || ''
  try {
    await ElMessageBox.confirm(
      `仅删数据库记录，不删文件。\n\n${fileName}`,
      '删除条目',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch { return }
  try {
    await adultApi.remove(item.id, { deleteFiles: false, deleteInJellyfin: false })
    ElMessage.success('已删除')
    reload(); loadStats()
  } catch (e) {
    ElMessage.error('删除失败：' + (e.response?.data?.detail || e.message))
  }
}

const onOpenActress = (payload) => {
  actressDrawer.payload = payload
  actressDrawer.visible = true
}

// ---- 生命周期 ----
watch(() => props.library?.id, async () => {
  selected.value = []
  await reload()
  loadStats()
})

// items 数量 / wanted / 视图模式变化 → 刷新 debug 面板
watch([() => items.value.length, wanted, viewMode], () => writeDebug())

onMounted(async () => {
  loadAllActresses()
  if (props.library?.id) {
    await reload()
    loadStats()
  }
  window.addEventListener('scroll', onWindowScroll, { passive: true, capture: true })
  writeDebug()
  updateScrollRow()
})

onUnmounted(() => {
  if (prefetchTimer) { clearTimeout(prefetchTimer); prefetchTimer = null }
  window.removeEventListener('scroll', onWindowScroll, { capture: true })
  if (_scrollRaf) cancelAnimationFrame(_scrollRaf)
  debugInfo.enabled = false
})
</script>

<style lang="scss" scoped>
.adult-lib-view {
  // 本视图嵌在 LibraryDetail 的 .page-container（flex column）里，跟 page-header 并列。
  // 用 flex: 1 + min-height: 0 吃 header 之外的剩余空间；
  // 之前用 height: 100% 是相对父容器全高（含 header），会把总内容撑出视口 → 外层出滚动条。
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;

  // ---- toolbar（与普通库 MediaToolbar 视觉一致） ----
  .media-toolbar {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 14px;
    background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    margin-bottom: 16px;

    .toolbar-scope {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: #475569;

      .scope-icon { color: #6366f1; }
      .scope-label { font-weight: 500; }
    }

    .toolbar-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }


    // 开关组：第一个开关 margin-left:auto 把整组推到 actions 行最右；
    // 后续开关紧贴前一个（gap:16px 隔开两组开关），避免每个都被 auto 撑开。
    .toolbar-switch {
      display: flex;
      align-items: center;
      gap: 8px;

      &:first-of-type {
        margin-left: auto;
      }
      & + .toolbar-switch {
        margin-left: 16px;
      }

      .switch-label {
        font-size: 13px;
        color: #475569;
        white-space: nowrap;
      }

      // 禁用态：label 也变浅色，光标 not-allowed
      &.disabled {
        opacity: 0.5;
        cursor: not-allowed;
        .switch-label { color: #94a3b8; }
      }
    }
  }

  // ---- top-row（与 LibraryDetail 视觉一致）----
  .top-row {
    display: grid;
    grid-template-columns: minmax(280px, 1fr) 2fr;
    gap: 16px;
    margin-bottom: 16px;

    @media (max-width: 900px) {
      grid-template-columns: 1fr;
    }
  }

  .paths-card,
  .stats-card {
    height: 100%;
    :deep(.el-card__header) { padding: 8px 14px; }
    :deep(.el-card__body)   { padding: 8px 14px; }

    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
  }

  .paths-card .paths-list .path-row {
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

  // ---- stats-grid ----
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 8px;

    @media (max-width: 1100px) { grid-template-columns: repeat(3, 1fr); }
    @media (max-width: 480px)  { grid-template-columns: repeat(2, 1fr); }

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
        .stat-value { color: #ef4444; }
      }
      .stat-label {
        font-size: 12px;
        color: #64748b;
        line-height: 1.2;
        white-space: nowrap;
        flex-shrink: 0;
      }
      .stat-value {
        font-size: 17px;
        font-weight: 700;
        color: #0f172a;
        line-height: 1.2;
        text-align: right;
        white-space: nowrap;
        small { font-size: 11px; color: #94a3b8; font-weight: 500; }
      }
    }
  }

  .loading-block {
    padding: 14px;
    text-align: center;
    color: #94a3b8;
    font-size: 13px;
    .spin { animation: spin 1.5s linear infinite; color: #6366f1; margin-right: 6px; }
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  // ---- items-card ----
  .items-card {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;

    :deep(.el-card__header) { padding: 10px 14px; }
    :deep(.el-card__body) {
      padding: 0;
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      overflow: auto;
    }

    .items-header {
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }
    .filter-row {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      flex: 1;
    }
  }

  // 排序栏 chip 风格（与 LibraryDetail 同款）；
  // items-header 用 flex-wrap 横排，sort-bar 在最左、filter-row 紧随其后、分页推到右
  .sort-bar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px;
    margin-right: 4px;       // 与 filter-row 间隔；window 窄时 wrap 仍正常

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

  // ---- 网格视图（跟普通库 LibraryDetail 对齐：固定卡片宽 + 写死海报高，
  // 不依赖 aspect-ratio + 1fr 那一套，避免高度随分页器/容器变） ----
  $grid-card-w: 280px;
  $grid-poster-h: 158px;

  .grid-view {
    display: grid;
    grid-template-columns: repeat(auto-fill, $grid-card-w);
    grid-auto-rows: max-content;  // 行高跟随内容，不被 flex 父级拉伸
    justify-content: start;       // 卡片左对齐，剩余空间留白
    align-content: start;
    gap: 18px;
    padding: 18px;
    min-height: 200px;
  }

  .grid-card {
    width: $grid-card-w;
    display: flex;
    flex-direction: column;
    background: #fff;
    border-radius: 8px;
    overflow: hidden;
    cursor: pointer;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
    border: 1px solid #f1f5f9;

    &:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 14px rgba(15, 23, 42, 0.08);
    }

    &--excluded { opacity: 0.55; }
    &--cooling  { background: #faf5ff; }

    // 骨架卡片：wanted 推进但池子还没补到时撑出占位行（Trending 同款 shimmer）
    &.grid-card--skeleton {
      pointer-events: none;
      cursor: default;
      &:hover { transform: none; box-shadow: none; }

      .sk-block, .sk-line {
        background: linear-gradient(90deg, #eef2f6 0%, #f7f9fb 50%, #eef2f6 100%);
        background-size: 800px 100%;
        animation: shimmer 1.4s linear infinite;
        border-radius: 3px;
      }
      .sk-poster {
        width: 100%;
        height: $grid-poster-h;
        border-radius: 0;
      }
      .grid-meta {
        padding: 8px 10px;
        .sk-code   { height: 14px; width: 42%; margin-bottom: 6px; }
        .sk-title  { height: 16px; width: 78%; margin-bottom: 6px; }
        .sk-health { height: 12px; width: 56%; }
      }
    }
  }

  @keyframes shimmer {
    0%   { background-position: -800px 0; }
    100% { background-position:  800px 0; }
  }

  .grid-poster {
    position: relative;
    width: 100%;
    height: $grid-poster-h;       // 固定高，不依赖 aspect-ratio
    flex: 0 0 $grid-poster-h;     // 不被 flex column 父级压缩 / 拉伸
    background: #f1f5f9;

    :deep(.adult-poster) {
      width: 100%;
      height: 100%;
      border-radius: 0;
    }
    // 浏览器缩放用高质量插值；对原图本身分辨率不够的情况也会更好看一点
    :deep(img) {
      image-rendering: -webkit-optimize-contrast;
      image-rendering: high-quality;
    }
  }

  .grid-health {
    position: absolute;
    top: 6px;
    left: 6px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.85);
    z-index: 2;

    &--green    { background: #16a34a; }
    &--yellow   { background: #f59e0b; }
    &--gray     { background: #94a3b8; }
    &--red      { background: #ef4444; }
    &--cooldown { background: #8b5cf6; }
    &--excluded { background: #1e293b; }
  }

  .grid-cen {
    position: absolute;
    top: 6px;
    right: 6px;
    z-index: 2;
  }

  .grid-meta {
    padding: 10px 12px 12px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
  }

  .grid-code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px;
    font-weight: 600;
    color: #4f46e5;
  }

  .grid-title {
    font-size: 14px;
    color: #1e293b;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  // 网格卡片健康度状态行（meta 区底部，始终可见）
  .grid-health-row {
    display: flex;
    align-items: center;
    gap: 5px;
    margin-top: 2px;
    font-size: 11px;

    .grid-health-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      flex-shrink: 0;
    }

    &--green    { color: #16a34a; .grid-health-dot { background: #16a34a; } }
    &--yellow   { color: #d97706; .grid-health-dot { background: #f59e0b; } }
    &--gray     { color: #64748b; .grid-health-dot { background: #94a3b8; } }
    &--red      { color: #dc2626; .grid-health-dot { background: #ef4444; } font-weight: 500; }
    &--cooldown { color: #7c3aed; .grid-health-dot { background: #8b5cf6; } font-weight: 500; }
    &--excluded { color: #475569; .grid-health-dot { background: #1e293b; } font-weight: 500; }
  }

  // 有/无码 badge：粉色（无码）/ 绿色（有码）/ 灰色（未识别）
  .cen-badge {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    padding: 2px 10px;
    font-size: 12px;
    font-weight: 500;
    border-radius: 10px;
    line-height: 1.5;
    user-select: none;

    &--uncensored {
      background: #fbcfe8;       // pink-200
      color: #db2777;            // pink-600
      border: 1px solid #f9a8d4;
    }
    &--censored {
      background: #dcfce7;       // green-100
      color: #15803d;            // green-700
      border: 1px solid #bbf7d0;
    }
    &--unknown {
      background: #f1f5f9;
      color: #94a3b8;
      border: 1px dashed #cbd5e1;
    }
  }
  // 触发器：跟普通 badge 视觉一致 + cursor 提示可点
  .cen-trigger {
    cursor: pointer;
    transition: filter 0.12s ease;
    &:hover { filter: brightness(0.96); }
  }
  // 手动覆盖标记：badge 末尾的 "M" 小角标
  .cen-manual-mark {
    display: inline-block;
    margin-left: 2px;
    padding: 0 4px;
    font-size: 10px;
    font-weight: 600;
    background: rgba(0, 0, 0, 0.12);
    color: inherit;
    border-radius: 4px;
    line-height: 1.3;
  }

  // 路径列：文字 + 行末复制按钮
  .path-cell {
    display: flex;
    align-items: center;
    gap: 4px;
    min-width: 0;

    .path-text {
      flex: 1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .path-copy-btn {
      flex-shrink: 0;
      padding: 0 4px;
      height: 22px;
      color: #94a3b8;

      &:hover {
        color: #4f46e5;
      }
      .el-icon {
        font-size: 14px;
      }
    }
  }

  .muted { color: #94a3b8; }
  // 与普通库 LibraryDetail 同款：等宽字体路径展示
  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    color: #475569;
  }

  :deep(.el-table .row-excluded) {
    --el-table-tr-bg-color: rgba(226, 232, 240, 0.5);
  }
  :deep(.el-table .row-cooling) {
    --el-table-tr-bg-color: rgba(237, 233, 254, 0.4);
  }

  // 表格选择 checkbox 加深 —— 默认 #dcdfe6 太浅，在白底/浅灰底/浅紫底上几乎看不见
  :deep(.el-table) {
    .el-checkbox__inner {
      width: 16px;
      height: 16px;
      border-color: #94a3b8;       // slate-400，明显高于默认
      background: #fff;
    }
    .el-checkbox__input:hover .el-checkbox__inner,
    .el-checkbox__inner:hover {
      border-color: #475569;       // hover 更深
    }
    // 选中后保持品牌色
    .el-checkbox__input.is-checked .el-checkbox__inner {
      background-color: var(--el-color-primary);
      border-color: var(--el-color-primary);
    }
    // indeterminate（半选）也明显
    .el-checkbox__input.is-indeterminate .el-checkbox__inner {
      background-color: var(--el-color-primary);
      border-color: var(--el-color-primary);
    }
  }

  // 无限滚动进度文字（替代原 el-pagination）
  .items-progress {
    color: #6b7280;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    flex-shrink: 0;
  }

  // 无限滚动哨兵：闲置时极薄，仅在显示提示文字时撑开
  // 无限滚动哨兵：仅作位置锚点，不可见
  .scroll-sentinel {
    height: 1px;
  }

  // 行号列：弱化展示
  .row-index {
    color: #94a3b8;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }
}
</style>

<!--
  非 scoped style：el-dropdown 的 popper 是 teleport 到 body 的，
  scoped 样式作用不到。专门给 cen-dropdown-popper 类做风格化。
-->
<style lang="scss">
.cen-dropdown-popper {
  .cen-item {
    font-weight: 400;

    // 当前项加粗，其它项普通粗细
    &.is-current { font-weight: 700; }

    // 仅文字颜色，hover 时换成对应色的浅色背景；hover 时文字颜色保持不变
    // （Element Plus 默认 hover 会把字色改成主品牌色，得显式锁住）
    &--uncensored {
      color: #db2777;
      &:not(.is-disabled):hover, &:not(.is-disabled):focus {
        background: #fce7f3;
        color: #db2777;
      }
    }
    &--censored {
      color: #15803d;
      &:not(.is-disabled):hover, &:not(.is-disabled):focus {
        background: #dcfce7;
        color: #15803d;
      }
    }
    &--auto {
      color: #475569;
      &:not(.is-disabled):hover, &:not(.is-disabled):focus {
        background: #f1f5f9;
        color: #475569;
      }
    }
  }
}
</style>

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
        <!-- 「仅看健康有问题」chip + 显示排除项 / 使用 Jellyfin 数据库 两个开关都在下方 items-header 行 -->
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

    <!-- 排序/筛选栏：独立 sticky div，钉在视口顶（不在 el-card header 里，避免 el-card overflow 限制 sticky 滚动容器）-->
    <div class="items-sort-bar">
      <div class="card-header items-header">
        <!-- 排序栏 + 派生 filter chip（与普通库 LibraryDetail 风格一致） -->
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
            <!-- filter chip：跟 sort chip 视觉一致；激活时琥珀色 + ✓ 区分语义 -->
            <span class="filter-divider" aria-hidden="true">·</span>
            <button
              :class="['sort-chip', 'filter-chip', { active: filters.has_health_issue, disabled: filters.use_jellyfin_db }]"
              :disabled="filters.use_jellyfin_db"
              :title="filters.use_jellyfin_db
                ? 'Jellyfin 视图下用此条件不准；切回 AdultItem 数据源后可用'
                : (filters.has_health_issue
                  ? '点击取消「健康度」过滤'
                  : '只看未识别 / 未刮削 / 封面或 NFO 本地缺失的条目')"
              @click="filters.has_health_issue = !filters.has_health_issue"
            >
              <el-icon v-if="filters.has_health_issue" class="filter-check"><Check /></el-icon>
              健康度
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

        <!-- 右侧开关组：margin-left:auto 推到行尾，跟在 filter-row 之后；从顶栏迁过来 -->
        <div class="header-switches">
          <!-- 显示排除项：Jellyfin 视图下"excluded"是 AdultItem 表特有概念，禁用此开关 -->
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

        <ViewModeToggle v-model="viewMode" />
      </div>
    </div>

    <!-- 列表卡片：仅 body（grid/table），header 已抽到上方 .items-sort-bar -->
    <el-card shadow="never" class="items-card">
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
              <!-- code 行右侧并入健康度（圆点 + 文字），跟普通库的"code/title + 健康"右对齐风格保持一致 -->
              <div class="grid-code-row">
                <div class="grid-code">{{ row.code || '未识别' }}</div>
                <div class="grid-health-row" :class="`grid-health-row--${gridHealthState(row)}`">
                  <span class="grid-health-dot" />
                  <span class="grid-health-text">{{ gridHealthLabel(row) }}</span>
                </div>
              </div>
              <div class="grid-title" :title="row.title || row.file_name">
                {{ row.title || row.file_name || '—' }}
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
        <el-table-column type="selection" width="40" class-name="adult-selection-col" />

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

    <!-- 通用确认对话框（样式跟 MediaToolbar 一致；支持 dry-run）-->
    <el-dialog
      v-model="confirmDialog.visible"
      :title="confirmDialog.action?.label || '确认'"
      width="520px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      append-to-body
    >
      <div class="confirm-scope">作用范围：{{ library.name }}</div>
      <div class="confirm-text" v-html="confirmDialog.action?.text || ''" />

      <!-- 只有当 action 支持 dry-run AND 用户在「基础配置」开了"显示测试模式"开关才显示 -->
      <div
        v-if="confirmDialog.action?.supportsDryRun && showDryRunInToolbar"
        class="dry-run-row"
      >
        <el-checkbox v-model="confirmDialog.dryRun" size="large">
          <span class="dry-run-label">测试模式</span>
        </el-checkbox>
        <div class="dry-run-hint">
          {{ confirmDialog.dryRun
            ? (confirmDialog.action.dryRunHint || '仅扫描预览，不实际执行')
            : '勾选后仅统计影响范围，不真删/不真下载，确认后再正式跑' }}
        </div>
      </div>

      <template #footer>
        <el-button @click="confirmDialog.visible = false">取消</el-button>
        <el-button
          :type="confirmBtnType"
          :class="confirmBtnClass"
          @click="onConfirmOk"
        >
          {{ confirmBtnText }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import {
  Search, MagicStick, Picture, Refresh, Loading,
  Check, Close, Operation, CaretTop, CaretBottom, DocumentCopy, Delete,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { adultApi, configApi } from '@/api'
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
  has_health_issue: false, // 派生字段过滤：只看不"完全完整"的条目（未识别/未刮削/cover或nfo缺）
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
// 首批渲染后直接调 prefetchIfNeeded（async 不阻塞），不再用 setTimeout 延迟
// 触发判定：用 sentinel 的 boundingClientRect 判定，与 LibraryDetail 同款
// 渲染锁（busy flag）替代时间节流，避免快滚时容器到底 + DOM 还没更新造成的"必须往上再向下"卡顿
let _loadMoreBusy = false
const SCROLL_TRIGGER_PX = 600

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
      _loadMoreBusy = false
      writeDebug()
      // 首批渲染后立刻预取（async 不阻塞），不再延迟
      prefetchIfNeeded(/* force */ true)
    }
  }
}

// 后台预取：池子剩余不足 4 步长 → 拉下一批；自递归直到足够（库视图卡片矮，4 步长才够缓冲）
// force=true：跳过 buffer 阈值（首次 1.5s 延迟预取专用，保证池子一定能长起来）
const prefetchIfNeeded = async (force = false) => {
  if (loadingMore.value || !hasMore.value) return
  if (!force && items.value.length - wanted.value >= stepSize() * 4) return
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
  if (seq === reqSeq && hasMore.value && items.value.length - wanted.value < stepSize() * 4) {
    prefetchIfNeeded()
  }
}

// 详见 LibraryDetail 同名块：buffer 改成 visible 行（一个视口）让滚动跟手
const loadMore = (presetState = null) => {
  if (loading.value) return
  if (!hasMore.value && items.value.length <= wanted.value) return
  const { scrolled, visible } = presetState || _currentScrollState()
  const target = Math.max(1, scrolled + visible * 2) * cardsPerRow()
  if (target > wanted.value) {
    wanted.value = target
  }
  prefetchIfNeeded()
}

const _currentScrollState = () => {
  // 滚动容器现在是 .app-main（页面级滚动），跟 LibraryDetail 对齐
  const scroller = document.querySelector('.app-main')
  if (!scroller) return { scrolled: 0, visible: 0 }
  const scrollerRect = scroller.getBoundingClientRect()
  let viewportTop = scrollerRect.top

  // sticky .items-sort-bar 钉在视口顶时遮挡内容，把它的可见高度从 viewportTop 减下来
  // （rect.top ≈ scrollerRect.top 才算"当前真的钉住了"，避免未 stuck 时误补偿）
  const sortBar = document.querySelector('.adult-lib-view .items-sort-bar')
  if (sortBar) {
    const r = sortBar.getBoundingClientRect()
    if (Math.abs(r.top - scrollerRect.top) < 2) {
      viewportTop += Math.max(0, r.bottom - scrollerRect.top)
    }
  }

  if (viewMode.value === 'list') {
    const header = document.querySelector('.adult-lib-view .el-table__header-wrapper')
    if (header) viewportTop += header.offsetHeight
  }
  const viewportBottom = scrollerRect.bottom

  let scrolled = 0
  let visible = 0
  if (viewMode.value === 'list') {
    const bodyEl = document.querySelector('.adult-lib-view .el-table__body')
    const rows = bodyEl ? bodyEl.querySelectorAll('tr') : []
    let sawVisible = false
    for (const r of rows) {
      const rect = r.getBoundingClientRect()
      if (rect.bottom <= viewportTop) {
        if (!sawVisible) scrolled++
      } else if (rect.top < viewportBottom) {
        sawVisible = true
        visible++
      } else {
        break
      }
    }
  } else {
    const gridEl = document.querySelector('.adult-lib-view .grid-view')
    const cards = gridEl ? gridEl.querySelectorAll('.grid-card') : []
    if (cards.length) {
      let firstColLeft = null
      for (const c of cards) {
        const r = c.getBoundingClientRect()
        if (r.height > 0) { firstColLeft = r.left; break }
      }
      for (const c of cards) {
        const rect = c.getBoundingClientRect()
        if (rect.height < 1) continue
        if (firstColLeft !== null && Math.abs(rect.left - firstColLeft) > 5) continue
        if (rect.bottom <= viewportTop) scrolled++
        else if (rect.top < viewportBottom) visible++
        else break
      }
    }
  }
  return { scrolled, visible }
}

const _currentScrollRow = () => {
  const s = _currentScrollState()
  return s.scrolled + s.visible
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
  // 排序下推到后端 SQL ORDER BY，sortedItems 不再前端排
  params.sort_by = sortField.value
  params.sort_order = sortDir.value
  // 派生字段过滤 has_health_issue 同样下推（后端用 OR 条件 SQL 表达）
  if (filters.has_health_issue) {
    params.has_health_issue = true
  }
  return params
}

// ============ 滚动触发：sentinel boundingClientRect 判定（详见 LibraryDetail 注释）============
// presetState 由 onWindowScroll 那一帧测好的 { scrolled, visible } 传进来
const _maybeLoadMoreOnScroll = (presetState) => {
  if (_loadMoreBusy) return
  if (loading.value) return
  if (!hasMore.value && items.value.length <= wanted.value) return
  if (!sentinelRef.value) return
  const rect = sentinelRef.value.getBoundingClientRect()
  const viewportBottom = window.innerHeight || document.documentElement.clientHeight
  if (rect.top - viewportBottom > SCROLL_TRIGGER_PX) return
  _loadMoreBusy = true
  loadMore(presetState)
  nextTick(() => { _loadMoreBusy = false })
}

// ============ DEBUG（共享 debugInfo / 侧边栏展示）============
const writeDebug = () => {
  debugInfo.enabled = true
  debugInfo.source = `adult-lib:${viewMode.value}`
  const cols = cardsPerRow()
  const visible = sortedItems.value.length
  debugInfo.cols = cols
  debugInfo.totalRows = cols ? Math.max(1, Math.ceil(visible / cols)) : 0
  debugInfo.items = items.value.length
  debugInfo.wanted = wanted.value
}


// 详见 LibraryDetail.vue 同名注释
const updateScrollRow = () => {
  debugInfo.scrollRow = _currentScrollRow()
  writeDebug()
}

let _scrollRaf = null
const onWindowScroll = () => {
  if (_scrollRaf) return
  _scrollRaf = requestAnimationFrame(() => {
    // 一帧一次 rect-walk（详见 LibraryDetail 同名块）
    const state = _currentScrollState()
    debugInfo.scrollRow = state.scrolled + state.visible
    writeDebug()
    _maybeLoadMoreOnScroll(state)
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
// 排序栏（全部下推到后端 SQL ORDER BY，无限滚动天然兼容）
// ============================================================================
// 之前包含派生字段「健康度」，但前端排只对加载子集生效，无限滚动模式下结果错误。
// 健康度需求改成 filter 开关「仅看健康有问题」（见 filters.has_health_issue），更直接
const sortOptions = [
  { field: 'code',         label: '番号' },
  { field: 'title',        label: '标题' },
  { field: 'release_date', label: '发行' },
]

const sortField = ref('code')
const sortDir = ref('asc')

// 发行日期降序更顺手
const _defaultDir = (field) =>
  ['release_date'].includes(field) ? 'desc' : 'asc'

const setSort = (field) => {
  if (sortField.value === field) {
    sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortField.value = field
    sortDir.value = _defaultDir(field)
  }
}

// 排序全部下推到后端 SQL，前端不再二次排（旧版 _fieldKey/_compare/_healthRank 已删）
// 派生健康信号现在通过 has_health_issue filter 表达，比"按健康度排序看 top N"更直接
const sortedItems = computed(() => items.value)

const onSelectionChange = (rows) => { selected.value = rows }

// 视图模式（list 表格 / grid 网格），用户偏好持久化在 localStorage
const viewMode = useViewMode('adult-library', 'list')

// ============ wanted / 行步长 / 视口测量（对齐 LibraryDetail.vue）============
const GRID_CARD_W = 280
const GRID_CARD_GAP = 18
const GRID_POSTER_H = 158
const GRID_VIEW_PADDING = 18

// 见 LibraryDetail.vue 同名函数的说明：clientWidth 含 grid-view 自身 padding，需扣掉再算 auto-fill
const cardsPerRow = () => {
  if (viewMode.value !== 'grid') return 1
  const el = gridViewRef.value
  const clientW = el ? el.clientWidth : Math.max(0, window.innerWidth - 220 - 40 - 40)
  const contentW = Math.max(0, clientW - 2 * GRID_VIEW_PADDING)
  return Math.max(1, Math.floor((contentW + GRID_CARD_GAP) / (GRID_CARD_W + GRID_CARD_GAP)))
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

// ---- 4 按钮：用统一 confirm dialog（样式对齐 MediaToolbar）+ dry-run 支持 ----
//
// action 定义：
//   - label/text：对话框标题 + 提示 HTML（支持 v-html）
//   - level：'warning' / 'danger' —— 影响确认按钮颜色
//   - supportsDryRun：是否显示"测试模式"勾选；不支持就不展示
//   - dryRunHint：勾选后的提示文本
//   - handler({dryRun}): 真正动作 —— 返回 Promise，外层负责 loading / message / error
const confirmDialog = reactive({
  visible: false,
  action: null,
  dryRun: false,
})

// 调试开关：从 config.debug.show_dry_run_in_toolbar 读，控制确认弹窗里 dry-run 行的显隐
// 默认 false（生产环境隐藏），用户在「基础配置 → 调试开关」打开后才会出现
const showDryRunInToolbar = ref(false)
onMounted(async () => {
  try {
    const res = await configApi.getFull()
    showDryRunInToolbar.value = !!res?.data?.config?.debug?.show_dry_run_in_toolbar
  } catch (e) {
    console.warn('[AdultLibraryView] 读取 debug 开关失败，dry-run 默认隐藏', e)
  }
})

const ADULT_ACTIONS = {
  scan_now: {
    label: '本地库重扫',
    level: 'warning',
    text: '将扫描磁盘上该库的所有视频文件，识别<b>番号</b>并入库（不会刮削元数据）。',
    supportsDryRun: false,
    handler: async () => {
      scanning.value = true
      try {
        await adultApi.watcherRunNow(props.library.id)
        ElMessage.success('已启动本地库扫描')
        setTimeout(() => { reload(); loadStats() }, 1500)
      } finally {
        scanning.value = false
      }
    },
  },
  repair_covers: {
    label: '扫描并修复封面图',
    level: 'warning',
    text: '将扫描已识别但缺封面的条目，从对应数据源重新下载封面图。<br>已有图的条目会被跳过。',
    supportsDryRun: true,
    dryRunHint: '仅统计待修复条目数量，不实际下载封面',
    handler: async ({ dryRun }) => {
      repairing.covers = true
      try {
        const r = await adultApi.repairCovers(props.library.id, { dryRun })
        const count = r.data.count
        if (count === 0) {
          ElMessage.info('没有需要修复的封面')
        } else if (dryRun) {
          ElMessage.success(`【测试】找到 ${count} 条待修复封面，未实际下载`)
        } else {
          ElMessage.success(`已启动封面修复任务（共 ${count} 条）`)
        }
      } finally {
        repairing.covers = false
      }
    },
  },
  repair_metadata: {
    label: '扫描并修复识别错误',
    level: 'warning',
    text: '将扫描所有未刮 / 刮失败的条目，重新调用刮削源识别元数据并写回（含封面 + NFO）。',
    supportsDryRun: true,
    dryRunHint: '仅统计待修复条目数量，不实际调用刮削源',
    handler: async ({ dryRun }) => {
      repairing.meta = true
      try {
        const r = await adultApi.repairMetadata(props.library.id, { dryRun })
        const count = r.data.count
        if (count === 0) {
          ElMessage.info('没有需要修复的条目')
        } else if (dryRun) {
          ElMessage.success(`【测试】找到 ${count} 条待修复元数据，未实际刮削`)
        } else {
          ElMessage.success(`已启动识别修复任务（共 ${count} 条）`)
        }
      } finally {
        repairing.meta = false
      }
    },
  },
  reset_and_rescan: {
    label: '清空元数据重扫',
    level: 'danger',
    text: `
      <p style="margin:0 0 8px">将<b>清空当前库</b>所有 AdultItem 的识别 + 刮削数据：</p>
      <ul style="margin:0 0 8px;padding-left:20px;color:var(--jt-text-regular);font-size:13px">
        <li>番号、标题、女优、标签、厂商、导演 等元数据</li>
        <li>本地封面 / NFO 路径记录（DB 字段；磁盘文件不动）</li>
        <li>排除 / 冷却 / 失败计数 / 手动覆盖等状态</li>
      </ul>
      <p style="color:#dc2626;margin:0 0 6px">视频文件、海报图片、NFO <b>文件不会被删除</b>，仅清 DB 记录</p>
      <p style="margin:0 0 4px">清空后会立即触发一个任务：先扫描入库识别番号，扫完<b>同一任务内</b>衔接刮削。</p>
      <p style="color:#7f1d1d;margin:0">⚠️ NFO / 海报内容会被新数据覆盖</p>
    `,
    supportsDryRun: true,
    dryRunHint: '仅统计会被清空的条数，不真删、不触发扫描',
    handler: async ({ dryRun }) => {
      resettingScan.value = true
      try {
        const r = await adultApi.resetAndRescan(props.library.id, { dryRun })
        ElMessage.success(r.data?.message || '已启动')
        if (!dryRun) {
          // 立即清空当前页表格让用户视觉感知"已清空"
          items.value = []
          itemsTotal.value = 0
          nextOffset.value = 0
          hasMore.value = true
        }
        loadStats()
      } catch (e) {
        ElMessage.error('启动失败：' + (e.response?.data?.detail || e.message))
      } finally {
        resettingScan.value = false
      }
    },
  },
}

const openConfirm = (key) => {
  const action = ADULT_ACTIONS[key]
  if (!action) return
  confirmDialog.action = action
  confirmDialog.dryRun = false           // 默认关
  confirmDialog.visible = true
}

const onConfirmOk = async () => {
  const action = confirmDialog.action
  const dryRun = confirmDialog.dryRun && action.supportsDryRun
  confirmDialog.visible = false
  try {
    await action.handler({ dryRun })
  } catch (e) {
    console.error('[AdultLibraryView] action failed:', e)
  }
}

// 确认按钮的颜色 / 文案 —— 跟 MediaToolbar 的逻辑一致
const confirmBtnType = computed(() => {
  if (confirmDialog.dryRun) return 'primary'
  const lvl = confirmDialog.action?.level
  if (lvl === 'danger') return 'primary'   // 用 class 染红，type 留 primary
  return 'primary'
})
const confirmBtnClass = computed(() => {
  if (confirmDialog.dryRun) return ''
  return confirmDialog.action?.level === 'danger' ? 'el-button--danger' : ''
})
const confirmBtnText = computed(() => {
  if (confirmDialog.dryRun) return '开始测试'
  return confirmDialog.action?.level === 'danger' ? '执行' : '开始'
})

// 兼容旧 @click 绑定的薄包装：保持模板里 @click="scanNow" 等不动
const scanNow = () => openConfirm('scan_now')
const repairCovers = () => openConfirm('repair_covers')
const repairMetadata = () => openConfirm('repair_metadata')
const resetAndRescan = () => openConfirm('reset_and_rescan')

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

// 排序 / 派生 filter 切换：全部下推到后端 SQL → 触发 reload 重新拉首页
// 跟普通库 LibraryDetail 同一套模型，避免无限滚动下"前端只对子集排序"的 bug
watch([sortField, sortDir, () => filters.has_health_issue], () => {
  reload()
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
  window.removeEventListener('scroll', onWindowScroll, { capture: true })
  if (_scrollRaf) cancelAnimationFrame(_scrollRaf)
  debugInfo.enabled = false
})
</script>

<style lang="scss" scoped>
.adult-lib-view {
  // 跟 LibraryDetail 对齐：页面级滚动（外层 .app-main 滚），本视图内容自然撑开高度。
  // 配合 :has(.lib-detail-root) .app-main { padding-top: 0 } 全局规则 + .items-sort-bar sticky top:0，
  // 排序栏滚到视口顶时就贴住不动。

  // ---- toolbar（与普通库 MediaToolbar 视觉一致） ----
  .media-toolbar {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 14px;
    background: linear-gradient(135deg, var(--jt-fill-light) 0%, var(--jt-divider-light) 100%);
    border: 1px solid var(--jt-card-border);
    border-radius: 8px;
    margin-bottom: 16px;

    .toolbar-scope {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--jt-text-regular);

      .scope-icon { color: var(--jt-brand); }
      .scope-label { font-weight: 500; }
    }

    .toolbar-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;

      // 干掉 Element Plus 默认的 .el-button + .el-button { margin-left: 12px }
      // 已经用 flex gap: 8px 控制间距，多余 margin 会让按钮间距叠加成 20px
      :deep(.el-button + .el-button) {
        margin-left: 0;
      }
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
        color: var(--jt-text-regular);
        white-space: nowrap;
      }

      // 禁用态：label 也变浅色，光标 not-allowed
      &.disabled {
        opacity: 0.5;
        cursor: not-allowed;
        .switch-label { color: var(--jt-text-muted); }
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
    color: var(--jt-text-regular);
    line-height: 1.6;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;

    .path-text {
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .ok { color: var(--jt-success); }
    .fail { color: var(--jt-danger); }
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
      background: var(--jt-fill-light);
      border: 1px solid var(--jt-card-border);
      border-radius: 6px;
      transition: border-color 0.15s, transform 0.15s;

      &:hover {
        border-color: var(--jt-brand-light-7);
        transform: translateY(-1px);
      }
      &.warn {
        border-color: var(--jt-danger-border);
        background: var(--jt-danger-tint);
        .stat-value { color: var(--jt-danger); }
      }
      .stat-label {
        font-size: 12px;
        color: var(--jt-text-secondary);
        line-height: 1.2;
        white-space: nowrap;
        flex-shrink: 0;
      }
      .stat-value {
        font-size: 17px;
        font-weight: 700;
        color: var(--jt-text-primary);
        line-height: 1.2;
        text-align: right;
        white-space: nowrap;
        small { font-size: 11px; color: var(--jt-text-muted); font-weight: 500; }
      }
    }
  }

  .loading-block {
    padding: 14px;
    text-align: center;
    color: var(--jt-text-muted);
    font-size: 13px;
    .spin { animation: spin 1.5s linear infinite; color: var(--jt-brand); margin-right: 6px; }
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  // ---- items-sort-bar：与 LibraryDetail 同款 sticky 顶栏 ----
  // 顶部 .app-main padding-top 被全局 :has(.lib-detail-root) 清 0，所以这里 top:0 真正贴视口顶
  .items-sort-bar {
    position: sticky;
    top: 0;
    z-index: 5;
    background: var(--jt-card-bg);
    border: 1px solid var(--el-card-border-color, var(--jt-card-border));
    border-radius: var(--el-card-border-radius, 4px) var(--el-card-border-radius, 4px) 0 0;
    padding: 12px 20px;
    margin-bottom: 0;

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

    // 从顶栏迁过来的两个开关：靠右；filter-row 用 flex:1 自然把这组推到右边
    .header-switches {
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .toolbar-switch {
      display: flex;
      align-items: center;
      gap: 8px;

      .switch-label {
        font-size: 13px;
        color: var(--jt-text-regular);
        white-space: nowrap;
      }

      // 禁用态：label 也变浅色
      &.disabled {
        opacity: 0.5;
        cursor: not-allowed;
        .switch-label { color: var(--jt-text-muted); }
      }
    }
  }

  // ---- items-card ----
  // 顶边和顶圆角去掉：让上方 sort-bar 的 border-bottom 充当两者共用分割线
  .items-card {
    border-top: none;
    border-top-left-radius: 0;
    border-top-right-radius: 0;

    :deep(.el-card__body) {
      padding: 0;
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
      color: var(--jt-text-secondary);
      margin-right: 4px;
    }

    .sort-chip {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      padding: 4px 10px;
      font-size: 12px;
      color: var(--jt-text-regular);
      background: transparent;
      border: 1px solid var(--jt-card-border);
      border-radius: 14px;
      cursor: pointer;
      transition: all 0.15s;

      &:hover {
        border-color: var(--jt-brand-light-7);
        color: var(--jt-brand-dark);
      }

      &.active {
        background: var(--jt-brand);
        border-color: var(--jt-brand);
        color: #fff;
        font-weight: 500;

        .sort-arrow {
          font-size: 11px;
        }
      }

      // filter-chip：激活态琥珀色区分语义（多选过滤 vs 单选排序）
      &.filter-chip {
        &.active {
          background: var(--jt-warning);
          border-color: var(--jt-warning);
          color: #fff;
        }
        &.disabled,
        &:disabled {
          opacity: 0.4;
          cursor: not-allowed;
          &:hover {
            border-color: var(--jt-card-border);
            color: var(--jt-text-regular);
          }
        }
        .filter-check {
          font-size: 11px;
          margin-right: 2px;
        }
      }
    }

    .filter-divider {
      color: var(--jt-card-border);
      margin: 0 2px;
      user-select: none;
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
    background: var(--jt-card-bg);
    border-radius: 8px;
    overflow: hidden;
    cursor: pointer;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
    border: 1px solid var(--jt-divider-light);
    // 跳过视口外卡片的渲染（详见 LibraryDetail 同名��注释）
    content-visibility: auto;
    contain-intrinsic-size: #{$grid-card-w} 240px;

    &:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 14px rgba(15, 23, 42, 0.08);
    }

    &--excluded { opacity: 0.55; }
    &--cooling  { background: rgba(139, 92, 246, 0.06); }

    // 骨架卡片：wanted 推进但池子还没补到时撑出占位行（Trending 同款 shimmer）
    &.grid-card--skeleton {
      pointer-events: none;
      cursor: default;
      &:hover { transform: none; box-shadow: none; }

      .sk-block, .sk-line {
        background: linear-gradient(90deg, var(--jt-skeleton-from) 0%, var(--jt-skeleton-to) 50%, var(--jt-skeleton-from) 100%);
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
    background: var(--jt-fill-light);

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

  // code 行：左边番号、右边健康度，flex 分两端
  .grid-code-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
  }

  .grid-code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px;
    font-weight: 600;
    color: var(--jt-brand-dark);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }

  .grid-title {
    font-size: 14px;
    color: var(--jt-text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  // 网格卡片健康度状态行（并入 code-row 右侧）
  .grid-health-row {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
    flex-shrink: 0;

    .grid-health-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      flex-shrink: 0;
    }

    &--green    { color: var(--jt-success); .grid-health-dot { background: var(--jt-success); } }
    &--yellow   { color: var(--jt-warning); .grid-health-dot { background: var(--jt-warning); } }
    &--gray     { color: var(--jt-text-secondary); .grid-health-dot { background: var(--jt-text-muted); } }
    &--red      { color: var(--jt-danger); .grid-health-dot { background: var(--jt-danger); } font-weight: 500; }
    &--cooldown { color: #7c3aed; .grid-health-dot { background: #8b5cf6; } font-weight: 500; }
    &--excluded { color: var(--jt-text-regular); .grid-health-dot { background: var(--jt-text-primary); } font-weight: 500; }
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
      background: var(--jt-fill-light);
      color: var(--jt-text-muted);
      border: 1px dashed var(--jt-card-border);
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
      color: var(--jt-text-muted);

      &:hover {
        color: var(--jt-brand-dark);
      }
      .el-icon {
        font-size: 14px;
      }
    }
  }

  .muted { color: var(--jt-text-muted); }
  // 与普通库 LibraryDetail 同款：等宽字体路径展示
  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    color: var(--jt-text-regular);
  }

  :deep(.el-table .row-excluded) {
    --el-table-tr-bg-color: rgba(226, 232, 240, 0.5);
  }
  :deep(.el-table .row-cooling) {
    --el-table-tr-bg-color: rgba(237, 233, 254, 0.4);
  }

  // 选择列：收紧 cell 默认 padding，让 checkbox 更靠近左边缘（跟普通库视觉接近）
  :deep(.adult-selection-col .cell) {
    padding-left: 8px;
    padding-right: 8px;
  }

  // 表格选择 checkbox 样式 —— 与普通库 LibraryDetail.vue 保持一致
  // 关键：box 18×18 + 2px 边框 → 内部 14×14 = Element Plus 默认 box 大小，
  // 钩子 ::after 完全不动，自然居中不歪
  :deep(.el-table) {
    .el-checkbox__inner {
      width: 18px;
      height: 18px;
      border-color: var(--jt-text-muted);
      border-width: 2px;
      border-radius: 3px;
      background: var(--jt-card-bg);
    }
    .el-checkbox__inner:hover {
      border-color: var(--jt-brand);
    }
    .el-checkbox__input.is-checked .el-checkbox__inner,
    .el-checkbox__input.is-indeterminate .el-checkbox__inner {
      background-color: var(--jt-brand);
      border-color: var(--jt-brand);
    }
  }

  // 无限滚动哨兵：仅作位置锚点，不可见
  .scroll-sentinel {
    height: 1px;
  }
}

// ─── 统一 confirm dialog 样式（跟 MediaToolbar 一致；这里不嵌 .adult-lib-view 是因为
//     el-dialog 默认 append-to-body 后样式作用不到 scoped 选择器内部需要的层级）
.confirm-scope {
  padding: 8px 12px;
  background: var(--jt-fill-light);
  border-radius: 4px;
  font-size: 13px;
  color: var(--jt-text-regular);
  font-weight: 500;
  margin-bottom: 12px;
}
.confirm-text {
  font-size: 13px;
  color: var(--jt-text-regular);
  line-height: 1.7;
  margin-bottom: 14px;

  :deep(p) { margin: 0 0 6px; }
  :deep(ul) { margin: 0 0 8px; padding-left: 20px; font-size: 13px; color: var(--jt-text-regular); }
  :deep(b)  { color: var(--jt-text-primary); }
}

.dry-run-row {
  margin-top: 14px;
  padding: 10px 12px;
  background: var(--jt-warning-tint);
  border: 1px dashed var(--jt-warning-border);
  border-radius: 6px;

  .dry-run-label {
    font-weight: 500;
    color: var(--jt-warning-text);
  }
  .dry-run-hint {
    margin-top: 4px;
    margin-left: 24px;
    font-size: 12px;
    color: var(--jt-warning-text);
    line-height: 1.5;
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
      color: var(--jt-text-regular);
      &:not(.is-disabled):hover, &:not(.is-disabled):focus {
        background: var(--jt-fill-light);
        color: var(--jt-text-regular);
      }
    }
  }
}
</style>

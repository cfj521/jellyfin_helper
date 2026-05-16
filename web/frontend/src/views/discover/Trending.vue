<template>
  <div class="page-container">
    <div class="page-header">
      <h2>
        热门推荐
        <span v-if="loading || loadingMore" class="loading-tip">
          <el-icon class="spin"><Loading /></el-icon>
          {{ loadingMore ? '加载更多...' : '加载中...' }}
        </span>
      </h2>
      <div class="header-actions">
        <!-- DEBUG 数据写到 useDebugInfo，由侧边栏显示 -->
        <el-button @click="reload(true)" :loading="loading" title="强制重拉（绕过缓存）">
          <el-icon><Refresh /></el-icon>
          强制刷新
        </el-button>
      </div>
    </div>

    <!-- 工具栏：source tab + 子分类筛选合并；sticky 磨砂背景 -->
    <div class="discover-toolbar">
      <el-tabs v-model="source" class="source-tabs" @tab-change="onSourceChange">
        <el-tab-pane label="Trakt" name="trakt" />
        <el-tab-pane label="TMDB" name="tmdb" />
        <el-tab-pane label="AniList" name="anilist" />
        <el-tab-pane label="豆瓣片单" name="douban" />
      </el-tabs>

      <div class="filter-area">
        <template v-if="source === 'tmdb'">
          <el-radio-group v-model="tmdbCategory" @change="reload()">
            <el-radio-button label="trending">趋势</el-radio-button>
            <el-radio-button label="movie">电影</el-radio-button>
            <el-radio-button label="tv">剧集</el-radio-button>
          </el-radio-group>
          <el-divider direction="vertical" />
          <template v-if="tmdbCategory === 'trending'">
            <el-radio-group v-model="tmdbTrendingType" size="small" @change="reload()">
              <el-radio-button label="all">全部</el-radio-button>
              <el-radio-button label="movie">电影</el-radio-button>
              <el-radio-button label="tv">剧集</el-radio-button>
            </el-radio-group>
            <el-radio-group v-model="tmdbTrendingWindow" size="small" @change="reload()">
              <el-radio-button label="day">今日</el-radio-button>
              <el-radio-button label="week">本周</el-radio-button>
            </el-radio-group>
          </template>
          <template v-else-if="tmdbCategory === 'movie'">
            <el-radio-group v-model="tmdbMovieCat" size="small" @change="reload()">
              <el-radio-button label="popular">最受欢迎</el-radio-button>
              <el-radio-button label="now_playing">正在上映</el-radio-button>
              <el-radio-button label="upcoming">即将上映</el-radio-button>
              <el-radio-button label="top_rated">高分推荐</el-radio-button>
            </el-radio-group>
          </template>
          <template v-else-if="tmdbCategory === 'tv'">
            <el-radio-group v-model="tmdbTvCat" size="small" @change="reload()">
              <el-radio-button label="popular">最受欢迎</el-radio-button>
              <el-radio-button label="airing_today">今日播出</el-radio-button>
              <el-radio-button label="on_the_air">正在播出</el-radio-button>
              <el-radio-button label="top_rated">高分推荐</el-radio-button>
            </el-radio-group>
          </template>
        </template>

        <template v-else-if="source === 'trakt'">
          <el-radio-group v-model="traktMediaType" @change="reload()">
            <el-radio-button label="movie">电影</el-radio-button>
            <el-radio-button label="tv">剧集</el-radio-button>
          </el-radio-group>
          <el-divider direction="vertical" />
          <el-radio-group v-model="traktCategory" size="small" @change="reload()">
            <el-radio-button label="trending">热门</el-radio-button>
            <el-radio-button label="anticipated">期待</el-radio-button>
            <el-radio-button label="popular">流行</el-radio-button>
            <el-radio-button label="watched_weekly">本周观看</el-radio-button>
          </el-radio-group>
        </template>

        <template v-else-if="source === 'anilist'">
          <el-radio-group v-model="anilistCategory" @change="reload()">
            <el-radio-button label="trending">实时趋势</el-radio-button>
            <el-radio-button label="popular">最受欢迎</el-radio-button>
            <el-radio-button label="top_rated">高分推荐</el-radio-button>
            <el-radio-button label="current_season">本季番剧</el-radio-button>
          </el-radio-group>
        </template>

        <template v-else-if="source === 'douban'">
          <!-- 4 个豆瓣源做成 radio button 组，跟其它 source 的 el-radio-group 视觉一致 -->
          <el-radio-group v-model="doubanDoulistId" @change="reload()">
            <el-radio-button
              v-for="d in doubanLists"
              :key="d.doulist_id"
              :label="d.doulist_id"
            >{{ d.name }}</el-radio-button>
          </el-radio-group>
        </template>
      </div>
    </div>

    <!-- 卡片网格 -->
    <el-row ref="gridRowRef" :gutter="16" v-loading="loading && !displayItems.length">
      <el-col
        v-for="item in displayItems"
        :key="item._key"
        :xs="12" :sm="8" :md="6" :lg="4" :xl="3"
      >
        <!-- 骨架卡片：DOM 结构跟真卡片节点对齐，行高一致避免参差 -->
        <el-card
          v-if="item._skeleton"
          shadow="never"
          class="poster-card poster-card--skeleton"
          body-style="padding: 0"
        >
          <div class="poster">
            <div class="sk-block sk-poster" />
          </div>
          <div class="info">
            <!-- 对应 .title -->
            <div class="sk-line sk-title" />
            <!-- 对应 .meta（左 tag 右 year） -->
            <div class="meta">
              <div class="sk-block sk-tag" />
              <div class="sk-block sk-year" />
            </div>
            <!-- 对应 .ratings-row -->
            <div class="sk-line sk-ratings" />
          </div>
        </el-card>
        <el-card
          v-else
          shadow="hover"
          class="poster-card"
          body-style="padding: 0"
          :style="{ '--card-idx': item._cardIdx }"
        >
          <div class="poster" @click="openDetail(item)">
            <el-image
              v-if="item.poster_url"
              :src="item.poster_url"
              fit="cover"
              lazy
              loading="lazy"
              decoding="async"
              referrerpolicy="no-referrer"
              style="width: 100%; aspect-ratio: 2/3"
            />
            <div v-else class="no-poster">无海报</div>
            <!-- 媒体类型徽标：左上角，纯图标 + drop-shadow，对齐 Trakt 风格（无背景） -->
            <div
              class="media-type-badge"
              :class="`mt-${item.media_type}`"
              :title="mediaTypeLabel(item.media_type)"
            >
              <!-- 电影：film 卷盘 -->
              <svg v-if="item.media_type === 'movie'" viewBox="0 0 24 24" fill="currentColor">
                <path d="M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm1 2v2h2V6H5Zm12 0v2h2V6h-2ZM5 10v2h2v-2H5Zm12 0v2h2v-2h-2ZM5 14v2h2v-2H5Zm12 0v2h2v-2h-2ZM5 18v1h2v-1H5Zm12 0v1h2v-1h-2ZM9 6v12h6V6H9Z"/>
              </svg>
              <!-- 剧集：电视机 -->
              <svg v-else-if="item.media_type === 'tv'" viewBox="0 0 24 24" fill="currentColor">
                <path d="M21 3H3a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h6v2H7v2h10v-2h-2v-2h6a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2Zm0 14H3V5h18v12Z"/>
              </svg>
              <!-- 番剧：浪花/海浪（致敬神奈川冲浪里，番剧通用视觉） -->
              <svg v-else-if="item.media_type === 'anime'" viewBox="0 0 24 24" fill="currentColor">
                <path d="M2 17.5c1.5 0 1.5-1 3-1s1.5 1 3 1 1.5-1 3-1 1.5 1 3 1 1.5-1 3-1 1.5 1 3 1 1.5-1 3-1v2c-1.5 0-1.5 1-3 1s-1.5-1-3-1-1.5 1-3 1-1.5-1-3-1-1.5 1-3 1-1.5-1-3-1-1.5 1-3 1v-2Zm0-5c1.5 0 1.5-1 3-1s1.5 1 3 1 1.5-1 3-1 1.5 1 3 1 1.5-1 3-1 1.5 1 3 1 1.5-1 3-1v2c-1.5 0-1.5 1-3 1s-1.5-1-3-1-1.5 1-3 1-1.5-1-3-1-1.5 1-3 1-1.5-1-3-1-1.5 1-3 1v-2ZM12 3a4 4 0 1 1 0 8 4 4 0 0 1 0-8Zm0 2a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z"/>
              </svg>
              <!-- 成人：禁止 18 标志（圆圈 + 18） -->
              <svg v-else-if="item.media_type === 'adult'" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm0 2a8 8 0 0 1 8 8c0 1.85-.63 3.55-1.69 4.9L7.1 5.69A7.96 7.96 0 0 1 12 4Zm0 16a8 8 0 0 1-8-8c0-1.85.63-3.55 1.69-4.9L16.9 18.31A7.96 7.96 0 0 1 12 20Z"/>
              </svg>
              <!-- 人物：人像 -->
              <svg v-else-if="item.media_type === 'person'" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10Zm0 2c-3.33 0-10 1.67-10 5v3h20v-3c0-3.33-6.67-5-10-5Z"/>
              </svg>
            </div>
            <!-- 主源评分徽章（按 source 上色，跟下方展开列表风格一致）-->
            <div
              class="source-rating-badge"
              v-if="item.rating != null"
              :class="[
                sourceBadgeInfo.cls,
                {
                  clickable: (item.tmdb_id || item.douban_id) && item.media_type !== 'person',
                  expanded: expandedRatings[item._key],
                }
              ]"
              :title="(item.tmdb_id || item.douban_id) && item.media_type !== 'person' ? '点击展开/收起多维评分' : ''"
              @click.stop="(item.tmdb_id || item.douban_id) && item.media_type !== 'person' && toggleRatings(item)"
            >
              <img v-if="sourceBadgeInfo.iconSrc" :src="sourceBadgeInfo.iconSrc" :alt="source" class="src-img" />
              <span v-else-if="sourceBadgeInfo.label" class="src">{{ sourceBadgeInfo.label }}</span>
              <span class="val">{{ item.rating.toFixed(1) }}</span>
            </div>
            <!-- 多维评分：紧跟主源徽章下方展开（竖排）；豆瓣条目通过 douban_id 桥接索引 -->
            <RatingsBadges
              v-if="expandedRatings[item._key] && (item.tmdb_id || item.douban_id) && item.media_type !== 'person'"
              compact
              direction="column"
              :rating="ratingForItem(item)"
              :exclude-source="currentExcludeSource"
              class="ratings-expanded"
              @click.stop
            />
            <div v-if="item.badge" class="src-badge">{{ item.badge }}</div>
            <!-- 简介按钮：海报右下角，纯图标 -->
            <el-button
              size="small"
              circle
              class="poster-overview-btn"
              @click.stop="toggleOverview(item)"
              title="简介"
            >
              <el-icon><Document /></el-icon>
            </el-button>
            <div
              v-if="overviewVisible[item._key]"
              class="overview-overlay"
              @click.stop
            >
              <button class="overview-close" @click.stop="hideOverview(item)" title="关闭">
                <el-icon><Close /></el-icon>
              </button>
              <div class="overview-content">
                <div class="overview-text">{{ item.overview || '暂无简介' }}</div>
                <!-- 豆瓣条目首次打开：导演行已显示，下面挂 spin 表示剧情简介正在抓取 -->
                <div v-if="loadingOverviewKeys.has(item._key)" class="overview-loading">
                  <el-icon class="spin"><Loading /></el-icon>
                  <span>加载剧情简介…</span>
                </div>
              </div>
            </div>
          </div>
          <div class="info">
            <div class="title-row">
              <div class="title clickable" :title="item.title" @click="openDetail(item)">{{ item.title }}</div>
              <el-button
                size="small"
                circle
                type="primary"
                plain
                class="title-search-btn"
                :loading="searchingKeys.has(item._key)"
                @click.stop="searchTorrents(item)"
                title="搜种子"
              >
                <el-icon><Search /></el-icon>
              </el-button>
            </div>
            <div class="meta">
              <div class="genre-tags" v-if="(item.genres || []).length">
                <el-tag
                  v-for="g in item.genres.slice(0, 2)"
                  :key="g"
                  size="small"
                  type="info"
                  effect="plain"
                  class="genre-tag"
                >{{ g }}</el-tag>
              </div>
              <span v-else class="meta-placeholder"></span>
              <span class="year">{{ item.year || '' }}</span>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 无限滚动哨兵 -->
    <div ref="sentinelRef" class="scroll-sentinel">
      <span v-if="!hasMore && displayItems.length" class="end-tip">— 已经到底了 —</span>
    </div>

    <el-empty v-if="!loading && !displayItems.length" :description="emptyHint" />
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, onActivated, onDeactivated, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Refresh, Star, Search, Document, Close, Loading } from '@element-plus/icons-vue'
import { discoverApi, ratingsApi } from '@/api'
import RatingsBadges from '@/components/RatingsBadges.vue'
import tmdbLogo from '@/assets/icons/tmdb.svg'
import doubanLogo from '@/assets/icons/douban.svg'
import { debugInfo } from '@/composables/useDebugInfo'

// 让 App.vue 的 <keep-alive :include="[...]"> 能命中：用户从详情页返回时保留 tab/筛选/滚动位置
defineOptions({ name: 'Trending' })

const router = useRouter()

// ---- tab / 子分类 ----
const source = ref('trakt')

const tmdbCategory = ref('trending')
const tmdbTrendingType = ref('all')
const tmdbTrendingWindow = ref('week')
const tmdbMovieCat = ref('popular')
const tmdbTvCat = ref('popular')

const traktMediaType = ref('movie')
const traktCategory = ref('trending')

const anilistCategory = ref('trending')

const doubanLists = ref([])
const doubanDoulistId = ref('')

// ---- 数据 + 分页状态 ----
const items = ref([])             // 后端拉来的原始数据池（已 append 多页）
const wanted = ref(0)             // 当前期望显示的条数（从视口算起的累加器）
const page = ref(1)
const hasMore = ref(true)
const loading = ref(false)        // 首屏 loading（清空 items 时用）
const loadingMore = ref(false)    // 触底追加 loading（不清 items）
const cached = ref(false)
const overviewVisible = reactive({})
const ratingsByKey = reactive({})
// 点击海报右上角 5 角星 → 切换展开"多维评分徽章"（默认折叠，省卡片空间）
const expandedRatings = reactive({})
const toggleRatings = (item) => {
  expandedRatings[item._key] = !expandedRatings[item._key]
}
// 展开列表排除当前 source 对应的徽章（主源已在五角星上展示，避免重复）
// 桥接失败时 RatingsBadges 内部走 empty 分支显示"暂无评分"占位，不会空白
const currentExcludeSource = computed(() => {
  const s = source.value
  if (s === 'tmdb') return 'tmdb'
  if (s === 'trakt') return 'trakt'
  if (s === 'douban') return 'douban'
  return ''   // anilist 等：RatingsBadges 本来没该源徽章，无需排除
})

// 当前 source 的"主评分徽章"样式：跟下方展开列表保持视觉一致
// returns { cls, label, iconSrc(可空，有 SVG 时用它代替 label 文字) }
const sourceBadgeInfo = computed(() => {
  const s = source.value
  if (s === 'tmdb')    return { cls: 'tmdb',    label: '',       iconSrc: tmdbLogo }
  if (s === 'trakt')   return { cls: 'trakt',   label: 'Trakt',  iconSrc: null }
  if (s === 'anilist') return { cls: 'anilist', label: 'AniList',iconSrc: null }
  if (s === 'douban')  return { cls: 'douban',  label: '',       iconSrc: doubanLogo }
  return { cls: 'default', label: '', iconSrc: null }
})
// "搜种子"按钮 in-flight：豆瓣分支要先 detail → titleEnByImdb，期间这张卡的按钮转 spin
// 用 Set + reactive，按 item._key 跟踪；用户狂连点也只触发一次（在 set 里的 key 不会重复)
const searchingKeys = reactive(new Set())
// "简介"按钮 in-flight：豆瓣分支首次点击要去抓 detail，期间转 spin
const loadingOverviewKeys = reactive(new Set())
// 豆瓣条目剧情简介缓存：douban_id → summary 文本（'' = 已尝试但拉空，避免反复重抓）
// 用独立 map 而不是直接 mutation `item.overview`：displayItems 每次重算会按 mapper 重建
// item 的 overview 字段，mutation 会丢；从外部 map 在 mapper 里读，重算就能跟着同步
const overviewCache = reactive({})
const sentinelRef = ref(null)
const gridRowRef = ref(null)        // <el-row> ref，用 getBoundingClientRect 实测网格起点
let observer = null
let reqSeq = 0   // 并发竞态保护：每次 reload++，回调时对比丢弃过期响应
// prefetchTimer 已废弃：首批渲染后直接调 prefetchIfNeeded（async 不阻塞 DOM）
// IntersectionObserver 经典坑：observe() 调用时会立刻 fire 一次"当前 intersection 状态"，
// 当内容不够撑满 viewport 时这次 fire 会误触发 loadMore。
// 用这个 flag 跳过每次 observe() 的首次 fire，等用户真正滚动产生的 transition 才响应。
let skipNextIntersection = false

// ---- DEBUG（事后删）：写到共享 debugInfo，由 App.vue 侧边栏读取展示 ----
const writeDebug = () => {
  const cols = cardsPerRow()
  debugInfo.enabled = true
  debugInfo.source = source.value
  debugInfo.cols = cols
  debugInfo.totalRows = cols ? Math.max(1, Math.ceil(displayItems.value.length / cols)) : 0
  debugInfo.items = items.value.length
  debugInfo.wanted = wanted.value
  // scrollRow 已在 updateScrollRow 里单独写
}

const mediaTypeLabel = (mt) => {
  if (mt === 'tv') return '剧集'
  if (mt === 'anime') return '番剧'
  if (mt === 'person') return '人物'
  if (mt === 'adult') return '成人'
  return '电影'
}

const emptyHint = computed(() => {
  switch (source.value) {
    case 'tmdb': return '暂无数据，检查 TMDB API Key 是否配置'
    case 'trakt': return 'Trakt 未配置 client_id 或拉取失败（设置 → 第三方推荐源）'
    case 'anilist': return 'AniList 拉取失败，可能是网络问题'
    case 'douban': return doubanDoulistId.value ? '此片单为空或拉取失败（豆瓣反爬触发）' : '请先选择一个片单'
    default: return '暂无数据'
  }
})

// ---- 视口估算：首屏一次拉够"可见行数 + 1 行" ----
// el-col 响应式断点 → 每行卡片数
const cardsPerRow = () => {
  const w = window.innerWidth
  if (w >= 1920) return 8        // xl=3 → 24/3
  if (w >= 1200) return 6        // lg=4
  if (w >= 992) return 4         // md=6
  if (w >= 768) return 3         // sm=8
  return 2                        // xs=12
}

// 拿到 <el-row> 真实 DOM（el-row 是 component，需要 .$el）。挂载前返回 null
const getGridEl = () => {
  const r = gridRowRef.value
  if (!r) return null
  return r.$el || (r instanceof HTMLElement ? r : null)
}

// 海报卡片估算高度（poster 2:3 + meta + actions ≈ 卡片高度），按当前卡片宽度推。
// 优先用 .el-row 实测宽度（最准）；DOM 还没挂上时退回到 viewport 估算
const cardHeightPx = () => {
  const colsPerRow = cardsPerRow()
  const gridEl = getGridEl()
  let usable
  if (gridEl) {
    usable = gridEl.getBoundingClientRect().width
  } else {
    // fallback：viewport 减 sidebar(220) - container padding(40)
    usable = window.innerWidth - 220 - 40
  }
  const cardW = (usable - 16 * (colsPerRow - 1)) / colsPerRow
  // 海报 2:3 (cardW * 1.5) + 标签/操作行 ≈ 130
  return cardW * 1.5 + 130
}

const initialLimit = () => {
  // 优先实测 .el-row top → window.innerHeight - top = 真实可用高度（自动跟着 header / tabs / filter 一起调整）
  const gridEl = getGridEl()
  let usableH
  if (gridEl) {
    const top = gridEl.getBoundingClientRect().top
    usableH = Math.max(300, window.innerHeight - top)
  } else {
    // fallback：第一次 mount 还没渲染时用估值（200px overhead）
    usableH = Math.max(300, window.innerHeight - 200)
  }
  const visibleRows = Math.max(1, Math.ceil(usableH / cardHeightPx()))
  // +1 行让用户滚一点点就触发下一页
  const rows = visibleRows + 1
  // 严格行对齐：rows × perRow（不 clamp）
  return rows * cardsPerRow()
}

// ---- 卡片归一化 + 切片到 wanted + 骨架占位 ----
// items 池可能比 wanted 大（一次 fetch 拿了 30 条但 wanted 只到 20），切片到 wanted
// items 池可能比 wanted 小（wanted 刚加了一行，下一页还在飞）→ 用骨架占位填补
// 这样"滚到底"立即看到一行灰卡片，文字/海报随网络陆续到位 —— Trakt 同款体验
//
// 同时给每张真卡片一个 _cardIdx：CSS 用它做"图片错峰淡入"动画延迟，视觉上形成"文字立即显示 → 海报一张张冒出"的递进效果
const displayItems = computed(() => {
  const sliced = items.value.slice(0, wanted.value || items.value.length)
  const real = sliced.map((it, idx) => {
    if (source.value === 'tmdb') {
      return {
        _key: `tmdb-${it.tmdb_id || idx}-${idx}`,
        tmdb_id: it.tmdb_id,
        title: it.title,
        year: (it.release_date || '').slice(0, 4) || null,
        poster_url: it.poster_url,
        rating: it.vote_average,
        overview: it.overview,
        media_type: it.media_type,
        original_title: it.original_title,
        original_language: it.original_language,
        genres: it.genres || [],
        badge: null,
      }
    }
    if (source.value === 'trakt') {
      return {
        _key: `trakt-${it.trakt_id || idx}-${idx}`,
        trakt_id: it.trakt_id,
        tmdb_id: it.tmdb_id,
        imdb_id: it.imdb_id,
        title: it.title,
        year: it.year,
        poster_url: it.poster_url,
        rating: it.rating,
        overview: it.overview,
        media_type: it.media_type,
        genres: it.genres || [],
        badge: it.watchers != null ? `${it.watchers} 关注` : (it.play_count != null ? `${it.play_count} 次播放` : null),
      }
    }
    if (source.value === 'anilist') {
      const title = it.title_english || it.title_romaji || it.title_native
      return {
        _key: `anilist-${it.anilist_id || idx}-${idx}`,
        anilist_id: it.anilist_id,
        tmdb_id: it.tmdb_id,
        title,
        year: it.season_year,
        poster_url: it.cover_image,
        rating: it.average_score != null ? it.average_score / 10 : null,
        overview: it.description || '',
        media_type: 'anime',
        original_title: it.title_native,
        genres: it.genres || [],
        badge: it.episodes ? `${it.episodes} 集` : (it.format || null),
      }
    }
    if (source.value === 'douban') {
      // 豆瓣图片走后端反代（绕过 Referer 防盗链）
      const proxiedPoster = it.poster_url
        ? `/api/img-proxy?url=${encodeURIComponent(it.poster_url)}`
        : null
      // 豆瓣 doulist 卡片只爬到导演 / 类型 / 年份；剧情简介需用户点"简介"时才去 lazy fetch
      // overview = [导演行] + (overviewCache 里的剧情简介，如已抓过)
      // 通过 mapper 注入而不是 mutation item.overview —— 后者在 displayItems 重算时会被覆盖
      const base = it.director ? `导演：${it.director}` : ''
      const cachedSummary = it.douban_id ? overviewCache[it.douban_id] : undefined
      const overview = cachedSummary
        ? (base ? `${base}\n\n${cachedSummary}` : cachedSummary)
        : base
      return {
        _key: `douban-${it.douban_id || idx}-${idx}`,
        douban_id: it.douban_id,
        title: it.title,
        year: it.year,
        poster_url: proxiedPoster,
        rating: it.rating,
        overview,
        media_type: 'movie',
        genres: it.genres || [],
        // votes_label 区分语义：评价人数 vs 想看人数（/coming 即将上映用"想看"）
        badge: it.votes ? `${it.votes} ${it.votes_label || '评价'}` : null,
      }
    }
    return { _key: `unknown-${idx}`, title: '未知', media_type: 'movie', genres: [] }
  })

  // 给每张真卡片一个连续 idx，CSS 用作错峰动画延迟（文字立即出 → 海报一张张冒出）
  const perRow = cardsPerRow()
  real.forEach((it, idx) => {
    // 限定到行内 idx（同一行的图片错峰；新行从 0 重新开始，避免延迟无限累积）
    it._cardIdx = idx % perRow
  })

  // 骨架占位：wanted > items.length 且还在加载（或还没到底）→ 拼出 (wanted - items.length) 个 skeleton
  const gap = Math.max(0, wanted.value - sliced.length)
  if (gap > 0 && (loadingMore.value || loading.value || hasMore.value)) {
    const skeletonCount = Math.min(gap, perRow * 2)  // 最多两行 skeleton，避免无限增长
    for (let i = 0; i < skeletonCount; i++) {
      real.push({ _key: `skeleton-${i}-${Date.now()}`, _skeleton: true })
    }
  }
  return real
})

const toggleOverview = (item) => {
  // 已经打开 → 直接关闭
  if (overviewVisible[item._key]) {
    overviewVisible[item._key] = false
    return
  }
  // 立即打开 overlay（导演行已经在 item.overview 里），同时后台抓剧情简介
  overviewVisible[item._key] = true

  // 豆瓣条目首次打开 → 后台拉 detail；overlay 内 spin 提示进行中
  // overviewCache[douban_id] 已存在（包括空串）→ 视为已尝试，不再重抓
  const needsFetch = (
    source.value === 'douban' &&
    item.douban_id &&
    !(item.douban_id in overviewCache)
  )
  if (!needsFetch) return

  loadingOverviewKeys.add(item._key)
  ;(async () => {
    try {
      const r = await discoverApi.doubanDetail(item.douban_id)
      // 即便 summary 为空也写 cache（'' 防止反复重抓）
      overviewCache[item.douban_id] = r?.data?.summary || ''
    } catch (e) {
      console.warn('豆瓣简介拉取失败', e)
      overviewCache[item.douban_id] = ''
    } finally {
      loadingOverviewKeys.delete(item._key)
    }
  })()
}
const hideOverview = (item) => {
  overviewVisible[item._key] = false
}

// ---- 加载 ----
// 注意：不再传 limit 给后端 —— 各 source 后端用固定 page size，
// 缓存键只看 page；前端按 wanted 切片显示。任何 viewport 都命中同一份缓存。
const buildParams = (forceRefresh, pageNum) => {
  if (source.value === 'tmdb') {
    if (tmdbCategory.value === 'trending') {
      return ['trending', {
        media_type: tmdbTrendingType.value,
        time_window: tmdbTrendingWindow.value,
        page: pageNum,
        refresh: forceRefresh,
      }]
    }
    const mt = tmdbCategory.value
    const cat = mt === 'movie' ? tmdbMovieCat.value : tmdbTvCat.value
    return ['list', { media_type: mt, category: cat, page: pageNum, refresh: forceRefresh }]
  }
  if (source.value === 'trakt') {
    // 不再传 limit：后端固定 page size，缓存只按 page 走，命中率最大化
    return ['trakt', {
      media_type: traktMediaType.value,
      category: traktCategory.value,
      page: pageNum,
      refresh: forceRefresh,
    }]
  }
  if (source.value === 'anilist') {
    return ['anilist', {
      category: anilistCategory.value,
      page: pageNum,
      refresh: forceRefresh,
    }]
  }
  if (source.value === 'douban') {
    if (!doubanDoulistId.value) return null
    return ['doubanLists', {
      doulist_id: doubanDoulistId.value,
      page: pageNum,
      refresh: forceRefresh,
    }]
  }
  return null
}

const callApi = async (kind, params) => {
  if (kind === 'trending') return discoverApi.trending(params)
  if (kind === 'list') return discoverApi.list(params)
  if (kind === 'trakt') return discoverApi.trakt(params)
  if (kind === 'anilist') return discoverApi.anilist(params)
  if (kind === 'doubanLists') return discoverApi.doubanLists(params)
}

// 重置：换 source / 子分类 / 强刷
const reload = async (forceRefresh = false) => {
  page.value = 1
  hasMore.value = true
  // wanted 重置为视口可见行数 + 1 行（首屏所需）
  wanted.value = initialLimit()
  for (const k of Object.keys(overviewVisible)) overviewVisible[k] = false
  // 切 source / 改筛选时回顶部（用户停留在第 50 条时换 tab，不该还停在第 50 条的位置）
  // 滚动容器是 .app-main，不是 window
  const scroller = document.querySelector('.app-main')
  if (scroller) scroller.scrollTop = 0
  // keep-alive 保存的 scrollTop 也要清掉，免得 onActivated 又把页面跳回去
  _savedScrollTop = 0
  await load(forceRefresh, /*append*/ false)
  // 首批渲染后立刻预取下一页（async 不阻塞 DOM 渲染），不再延迟
  prefetchIfNeeded()
  // 切到豆瓣源且仍有海报缺失 → 启动 poll 让后台 enrich 进度能反映到 UI；其它 source 自动停
  startPosterPollIfNeeded()
}

const load = async (forceRefresh = false, append = false) => {
  const seq = ++reqSeq
  if (append) loadingMore.value = true
  else { loading.value = true; items.value = [] }

  try {
    const desc = buildParams(forceRefresh, page.value)
    if (!desc) { items.value = []; hasMore.value = false; return }
    const [kind, params] = desc
    const res = await callApi(kind, params)
    if (seq !== reqSeq) return  // 期间又触发了一次新 reload，丢弃
    const data = res?.data || {}
    const newItems = data.items || []
    if (append) items.value = [...items.value, ...newItems]
    else items.value = newItems
    cached.value = !!data.cached
    // 后端给了 has_more 用它；没给的话按"返回数 < (后端 page size) 视为到底"
    if (data.has_more !== undefined) hasMore.value = !!data.has_more
    else hasMore.value = newItems.length >= (data.limit || 20) * 0.8
    fetchRatings()
  } catch (e) {
    console.error('加载失败', e)
    if (!append) items.value = []
    hasMore.value = false
  } finally {
    if (seq === reqSeq) {
      loading.value = false
      loadingMore.value = false
      writeDebug()
    }
  }
}

// 后台预取：items 池剩余 < 2 行时主动拉下一页，不阻塞 wanted 推进
// 设计目标：用户滚到末尾时，items 池里早已有数据，看不到 2~3s 的骨架等待
// 自递归：上游 page size 小 + 用户滚得快时，一次预取可能仍不够
const prefetchIfNeeded = async () => {
  if (loadingMore.value || !hasMore.value) return
  if (items.value.length - wanted.value >= cardsPerRow() * 2) return
  loadingMore.value = true
  page.value += 1
  try {
    await load(false, /*append*/ true)
  } finally {
    loadingMore.value = false  // 强制重置（load 内 seq 守卫会跳过这一步）
    writeDebug()
  }
  // 池仍不够（用户狂滚 / 一页太小）→ 继续预取
  if (items.value.length - wanted.value < cardsPerRow() * 2 && hasMore.value) {
    prefetchIfNeeded()
  }
}

// 触底：wanted 累加一行；如果 items 池告急，触发后台预取（不 await）
// 关键变化：wanted 不再被 fetch 阻塞 —— 池里有数据就立刻渲染，下一页在后台跑
// IntersectionObserver 仍然 unobserve / 重新 observe（避免 sentinel 在视口内反复 fire）
const loadMore = async () => {
  if (loading.value || !hasMore.value) return
  if (observer && sentinelRef.value) observer.unobserve(sentinelRef.value)
  try {
    // 严格"一次一行"
    wanted.value += cardsPerRow()
    // 不 await：拉取在后台进行，wanted 推进不被阻塞
    prefetchIfNeeded()
  } finally {
    writeDebug()
    await nextTick()
    // 重新观察前 skip 一次（observe 必 fire 当前状态，sentinel 还在 viewport 时会误触发）
    observeSentinel()
  }
}

// ---- 评分批量 ----
// 支持两种条目：
//   - 有 tmdb_id 的（TMDB/Trakt/AniList 等）：直接查
//   - 仅有 douban_id 的（豆瓣源条目）：后端通过 media_metadata 反查桥接到 tmdb_id
const fetchRatings = async () => {
  const payload = displayItems.value
    .filter((x) => (x.tmdb_id || x.douban_id) && x.media_type !== 'person' && x.media_type !== 'anime')
    .map((x) => ({
      tmdb_id: x.tmdb_id || null,
      douban_id: x.douban_id || null,
      media_type: x.media_type === 'tv' ? 'tv' : 'movie',
      title: x.title || x.original_title,
      year: x.year ? parseInt(x.year) : null,
    }))
  if (!payload.length) return
  try {
    const res = await ratingsApi.batch(payload)
    for (const r of res.data.ratings || []) {
      // 主索引：tmdb_id-media_type（兼容旧路径）
      if (r.tmdb_id) {
        ratingsByKey[`${r.tmdb_id}-${r.media_type}`] = r
      }
      // 桥接索引：豆瓣条目按 douban_id 直查（用 echo 的 request_douban_id）
      if (r.request_douban_id) {
        ratingsByKey[`douban-${r.request_douban_id}`] = r
      }
    }
  } catch (e) {
    console.warn('评分批量拉取失败', e)
  }
}

// 当前 item 对应的 rating 对象（两种索引方式 fallback）
// 找不到时给一个最小占位（含 request_douban_id / tmdb_id），
// 让 RatingsBadges 走 pending 分支显示"拉取中"而不是完全空白
const ratingForItem = (item) => {
  if (item.tmdb_id) {
    const mt = item.media_type === 'tv' ? 'tv' : 'movie'
    const cached = ratingsByKey[`${item.tmdb_id}-${mt}`]
    if (cached) return cached
    return { tmdb_id: item.tmdb_id, media_type: mt, mdblist_status: 'missing', douban_status: 'missing' }
  }
  if (item.douban_id) {
    const cached = ratingsByKey[`douban-${item.douban_id}`]
    if (cached) return cached
    return { request_douban_id: String(item.douban_id), media_type: 'movie', mdblist_status: 'missing', douban_status: 'missing' }
  }
  return null
}

// ---- source 切换 ----
const onSourceChange = async () => {
  for (const k of Object.keys(overviewVisible)) overviewVisible[k] = false
  if (source.value === 'douban' && !doubanLists.value.length) {
    await loadDoubanLists()
  }
  reload()
}

const loadDoubanLists = async () => {
  try {
    const r = await discoverApi.doubanLists({})
    doubanLists.value = r.data?.lists || []
    if (doubanLists.value.length && !doubanDoulistId.value) {
      doubanDoulistId.value = doubanLists.value[0].doulist_id
    }
  } catch (e) {
    console.warn('豆瓣片单白名单拉取失败', e)
  }
}

// ---- 跳转 ----
// 优先英文标题：种子站点（PT/海外公网）几乎都用英文标题归档，传中文/日文名搜不到。
// 顺序：(1) 已注入的 english_title (TMDB 详情页有) → (2) 原语言为英文时的 original_title →
//       (3) 后端 /title-en 端点（30 天缓存，TMDB translations[en]）→ (4) original_title → (5) title
const resolveEnglistTitleByImdb = async (imdbId) => {
  try {
    const r = await discoverApi.titleEnByImdb(imdbId)
    return {
      title: r?.data?.english_title || null,
      mediaType: r?.data?.media_type || null,
    }
  } catch (e) {
    console.warn('IMDb → TMDB 英文标题解析失败', e)
    return { title: null, mediaType: null }
  }
}

// 返回 { title, mediaType? }；mediaType 来自 TMDB find 的优先 movie/tv 判定（豆瓣场景下用得到）
const resolveEnglishTitle = async (item) => {
  if (item.english_title) return { title: item.english_title }
  if (item.original_language === 'en' && item.original_title) return { title: item.original_title }
  // AniList: title 多半是 romaji（拉丁字母），可直接当英文使用
  if (source.value === 'anilist') return { title: item.original_title || item.title }

  // 豆瓣分支：列表 item 上只有 douban_id，没有 imdb_id —— 拉一次详情拿 imdb_id 再走 IMDb 路径
  // douban-detail 后端缓存 30 天，命中后毫秒；首次约 1-2s（豆瓣页爬取）
  if (source.value === 'douban' && item.douban_id) {
    try {
      const det = await discoverApi.doubanDetail(item.douban_id)
      const imdb = det?.data?.imdb_id
      if (imdb) {
        const { title: en, mediaType } = await resolveEnglistTitleByImdb(imdb)
        if (en) {
          item.english_title = en  // 缓存到 item 上，避免反复触发链路
          return { title: en, mediaType }
        }
      }
    } catch (e) {
      console.warn('豆瓣 → IMDb 链路失败，退回中文标题', e)
    }
    // 没 imdb / 解析失败 → 中文标题兜底（搜出率极低，但比空 query 强）
    return { title: item.title }
  }

  if (item.tmdb_id && (item.media_type === 'movie' || item.media_type === 'tv')) {
    try {
      const r = await discoverApi.titleEn(item.media_type, item.tmdb_id)
      const en = r?.data?.english_title
      if (en) {
        // 缓存到 item 上避免重复查
        item.english_title = en
        return { title: en }
      }
    } catch (e) {
      console.warn('英文标题查询失败', e)
    }
  }
  return { title: item.original_title || item.title }
}

const searchTorrents = async (item) => {
  // 已经在转 spin 中（用户连点）→ 短路。Set.add 同 key 也是幂等的，这里早 return 避免再起一遍异步
  if (searchingKeys.has(item._key)) return
  searchingKeys.add(item._key)
  try {
    const { title: enTitle, mediaType: resolvedMt } = await resolveEnglishTitle(item)
    let q = enTitle
    // 附带年份消歧：种子搜索引擎对 "Title 2010" 这种 query 命中率明显高于裸标题
    if (item.year) q = `${q} ${item.year}`
    // 豆瓣 item 的 media_type 默认硬编码 'movie'（doulist 卡片没区分）；
    // 走 IMDb→TMDB 链路时拿到的 mediaType 更准确，优先用它
    const finalMt = resolvedMt || item.media_type
    router.push({
      path: '/resourcesearch',
      query: {
        q,
        type: finalMt === 'tv' ? 'tv' : (finalMt === 'anime' ? 'tv' : 'movie'),
      },
    })
  } finally {
    searchingKeys.delete(item._key)
  }
}

const openDetail = (item) => {
  if (item.media_type === 'person') return
  // 优先级 1：TMDB ID 走我们自己的 TMDB 详情页（结构最全）
  // 注意 AniList 番剧偶尔附带 tmdb_id（external links → TMDB 链接），但 TMDB 上番剧元数据偏 thin，
  // 番剧详情还是优先走 AniList 自己的页面。所以这里 source 判断在前。
  if (source.value === 'anilist' && item.anilist_id) {
    router.push({ name: 'DiscoverAniListDetail', params: { anilistId: item.anilist_id } })
    return
  }
  if (source.value === 'douban' && item.douban_id) {
    router.push({ name: 'DiscoverDoubanDetail', params: { doubanId: item.douban_id } })
    return
  }
  if (item.tmdb_id) {
    router.push({
      name: 'DiscoverDetail',
      params: {
        mediaType: item.media_type === 'tv' ? 'tv' : 'movie',
        tmdbId: item.tmdb_id,
      },
    })
    return
  }
  ElMessage.info('该条目无可用详情链接')
}

// ---- 无限滚动 ----
// 哨兵 = 网格之后的 div；进入视口（含 rootMargin 提前量）才触发 loadMore
// rootMargin 用固定 200px：足够给加载提前量，又不至于在内容刚好填满时误触发
const setupObserver = () => {
  if (observer) observer.disconnect()
  observer = new IntersectionObserver((entries) => {
    if (skipNextIntersection) {
      skipNextIntersection = false
      return
    }
    for (const e of entries) {
      if (e.isIntersecting) loadMore()
    }
  }, { rootMargin: '200px 0px' })
  observeSentinel()
}

const observeSentinel = () => {
  if (!observer || !sentinelRef.value) return
  // 每次 observe 前都 set skip = true，第一帧 fire 立刻被忽略；
  // 用户后续真的滚动进入 → 那一次 fire 才是真的 transition，正常处理
  skipNextIntersection = true
  observer.observe(sentinelRef.value)
}

// resize：视口变化（用户改窗宽 / 旋转屏幕）→ 重算 observer rootMargin，
// 避免老的提前量跟新的卡片高度对不上
let resizeTimer = null
const onWindowResize = () => {
  if (resizeTimer) clearTimeout(resizeTimer)
  resizeTimer = setTimeout(() => {
    setupObserver()
    writeDebug()
    updateScrollRow()
  }, 200)
}

// DEBUG: scrollRow = 已滚过的行 + 视口内可见的行
// offset 用 (scrollerRect.top - gridRect.top)：两个 rect 都是 window 坐标，
// 相减消掉滚动容器自身的 window 偏移，剩下的就是"grid 顶被推出 scroller 顶部多少 px"
// 滚动容器是 .app-main（el-main 默认 overflow:auto）
const updateScrollRow = () => {
  const gridEl = getGridEl()
  if (!gridEl) return
  const scroller = document.querySelector('.app-main') || document.scrollingElement || document.documentElement
  const viewportH = scroller ? scroller.clientHeight : window.innerHeight
  // 实测：取第二张 poster-card（首张图常在 lazy load，offsetHeight 偏小）
  const cards = gridEl.querySelectorAll('.poster-card')
  const sample = cards[1] || cards[0]
  const ch = sample ? (sample.offsetHeight + 16) : cardHeightPx()  // 16 = el-row gutter

  const scrollerRect = scroller && scroller.getBoundingClientRect
    ? scroller.getBoundingClientRect()
    : { top: 0 }
  const gridRect = gridEl.getBoundingClientRect()
  const offset = Math.max(0, scrollerRect.top - gridRect.top)

  const scrolledRows = Math.floor(offset / ch)
  // floor：只数"完整看到的行"，半行不算
  const visibleRows = Math.max(1, Math.floor(viewportH / ch))
  debugInfo.scrollRow = scrolledRows + visibleRows
  writeDebug()
}

let scrollTimer = null
const onWindowScroll = () => {
  if (scrollTimer) return
  scrollTimer = requestAnimationFrame(() => {
    updateScrollRow()
    scrollTimer = null
  })
}

// 豆瓣源海报回填 poll：后端 enrich 流程异步进行，前端不会自动感知。
// 这里在豆瓣源且仍有 item 缺海报时每 20s 静默拉一次列表，把后台已 enrich 的字段合并进来。
// 全部 item 都拿到海报后停止，避免无意义 polling。
let posterPollTimer = null
const POSTER_POLL_INTERVAL_MS = 20_000
const startPosterPollIfNeeded = () => {
  stopPosterPoll()
  if (source.value !== 'douban') return
  const incomplete = items.value.some(it => !it.poster_url)
  if (!incomplete) return
  posterPollTimer = setInterval(pollPosters, POSTER_POLL_INTERVAL_MS)
}
const stopPosterPoll = () => {
  if (posterPollTimer) {
    clearInterval(posterPollTimer)
    posterPollTimer = null
  }
}
const pollPosters = async () => {
  if (source.value !== 'douban') { stopPosterPoll(); return }
  const incomplete = items.value.some(it => !it.poster_url)
  if (!incomplete) { stopPosterPoll(); return }
  try {
    const desc = buildParams(false, 1)
    if (!desc) return
    const [kind, params] = desc
    const res = await callApi(kind, params)
    const newItems = res?.data?.items || []
    if (!newItems.length) return
    // 按 douban_id 合并：只接收"补全字段"，不动其它（避免覆盖用户已展开的 overview 等）
    const byId = new Map(newItems.map(it => [String(it.douban_id), it]))
    let updated = 0
    items.value.forEach((it, idx) => {
      const ni = byId.get(String(it.douban_id))
      if (!ni) return
      const patch = {}
      if (!it.poster_url && ni.poster_url) { patch.poster_url = ni.poster_url; updated++ }
      if (it.rating == null && ni.rating != null) patch.rating = ni.rating
      if (!it.director && ni.director) patch.director = ni.director
      if (!it.imdb_id && ni.imdb_id) patch.imdb_id = ni.imdb_id
      if (Object.keys(patch).length) {
        // Vue 3 ref 数组项整体替换更稳：保留引用还在但触发响应
        items.value[idx] = { ...it, ...patch }
      }
    })
    if (updated) console.debug(`[douban poster poll] +${updated} 张海报已回填`)
    // 全部齐了就停
    if (!items.value.some(it => !it.poster_url)) stopPosterPoll()
  } catch (e) {
    console.warn('海报回填 poll 失败', e)
  }
}

// 首次挂载只负责"拉数据"。监听 / observer / debug 切换全交给 onActivated/onDeactivated
// —— 这样组件被 keep-alive 缓存（用户从详情页返回）时也能正确地装/卸滚动监听
onMounted(async () => {
  await reload()
})

// keep-alive 不会自动恢复滚动位置：滚动容器是 .app-main（el-main），Vue Router 默认的 scrollBehavior
// 只管 window 滚动。所以这里手动记录/恢复 .app-main.scrollTop
let _savedScrollTop = 0

// 进入页面（首次挂载 + 后续 keep-alive 激活都会触发）：装监听 + 重观察 sentinel + 恢复滚动位置
const _attachRuntime = async () => {
  await nextTick()
  setupObserver()
  window.addEventListener('resize', onWindowResize)
  // 监 grid 内容滚动（多数情况主体滚动是 .app-main 内部，加 capture 兜底）
  window.addEventListener('scroll', onWindowScroll, { passive: true, capture: true })
  // 恢复 scrollTop：必须等 DOM 重新挂回 + 浏览器布局完成；用第二个 nextTick + rAF 兜底
  if (_savedScrollTop > 0) {
    await nextTick()
    requestAnimationFrame(() => {
      const scroller = document.querySelector('.app-main')
      if (scroller) scroller.scrollTop = _savedScrollTop
    })
  }
  writeDebug()
  updateScrollRow()
  // 回到页面时，如果豆瓣源还有海报缺失，恢复 poll（_detachRuntime 时停了）
  startPosterPollIfNeeded()
}

// 离开页面（被 keep-alive deactivate 或真正卸载）：拆监听 + 断 observer + 关侧边栏 debug + 记录 scrollTop
const _detachRuntime = () => {
  const scroller = document.querySelector('.app-main')
  if (scroller) _savedScrollTop = scroller.scrollTop
  if (observer) observer.disconnect()
  window.removeEventListener('resize', onWindowResize)
  window.removeEventListener('scroll', onWindowScroll, { capture: true })
  if (resizeTimer) clearTimeout(resizeTimer)
  if (scrollTimer) cancelAnimationFrame(scrollTimer)
  // 离开页面停掉海报 poll，避免后台无意义占资源
  stopPosterPoll()
  debugInfo.enabled = false
}

onActivated(_attachRuntime)
onDeactivated(_detachRuntime)
onBeforeUnmount(_detachRuntime)
</script>

<style lang="scss" scoped>
@keyframes fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.cache-tag {
  margin-left: 10px;
  vertical-align: middle;
}

// DEBUG 已迁到侧边栏（App.vue），这里不再有 .debug-tag

.loading-tip {
  margin-left: 12px;
  font-size: 13px;
  color: var(--jt-text-muted);
  font-weight: normal;
  display: inline-flex;
  align-items: center;
  gap: 4px;

  .spin {
    animation: spin 1s linear infinite;
  }
}

.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

// ============ Discover 工具栏：source tab + 子分类筛选合并；sticky 磨砂 ============
// 磨砂背景兜底：浏览器不支持 backdrop-filter 时退回到不透明白底
// 横向 bleed 出 .app-main 的 20px padding，让磨砂带跨满视口宽度
.discover-toolbar {
  position: sticky;
  top: -20px;       // 抵消 .app-main padding-top: 20px，让磨砂带贴在视口最顶
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 16px;
  margin: 0 -20px 16px;
  padding: 8px 20px 0;
  background: rgba(var(--jt-toolbar-bg-rgb, 255, 255, 255), 0.7);
  backdrop-filter: saturate(180%) blur(14px);
  -webkit-backdrop-filter: saturate(180%) blur(14px);
  border-bottom: 1px solid var(--jt-divider-light);

  .source-tabs {
    flex: 0 0 auto;
    // 把 el-tabs 自带的下边线去掉，磨砂带自己的 border-bottom 已经够了
    :deep(.el-tabs__header) { margin-bottom: 0; }
    :deep(.el-tabs__nav-wrap::after) { display: none; }
    // 空的 el-tab-pane 不渲染内容区，避免占多余高度
    :deep(.el-tabs__content) { display: none; }
  }

  .filter-area {
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    flex-wrap: wrap;
    // tab 栏底部边距对齐：el-tabs 内部按钮上下各留点呼吸；筛选区也跟齐
    padding-bottom: 8px;

    .el-divider--vertical {
      height: 1.4em;
      margin: 0 4px;
    }
  }
}

.scroll-sentinel {
  min-height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 12px 0;

  .end-tip {
    color: var(--jt-text-muted);
    font-size: 13px;
    letter-spacing: 1px;
  }
}

// 海报加载完成的渐进式淡入（el-image 内部 img 进 DOM 时触发一次 fade-in）
@keyframes poster-fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

// 骨架占位 shimmer
@keyframes shimmer {
  0%   { background-position: -800px 0; }
  100% { background-position:  800px 0; }
}

.poster-card.poster-card--skeleton {
  pointer-events: none;
  animation: fade-in 0.2s ease;

  $shimmer-bg: linear-gradient(90deg,
    var(--jt-skeleton-from) 0%,
    var(--jt-skeleton-to) 50%,
    var(--jt-skeleton-from) 100%
  );

  // 通用 shimmer 块 + 线
  .sk-block, .sk-line {
    background: $shimmer-bg;
    background-size: 800px 100%;
    animation: shimmer 1.4s linear infinite;
    border-radius: 3px;
  }

  // 海报：跟 .poster 同款 2:3
  .sk-poster {
    width: 100%;
    aspect-ratio: 2/3;
    border-radius: 0;
  }

  // info 区按真卡片节点逐行还原
  .info {
    padding: 10px;
  }

  // 对应 .title（font-size 14, line-height 1.4 ≈ 20px + margin-bottom 6）
  .sk-title {
    height: 20px;
    width: 80%;
    margin-bottom: 6px;
  }

  // 对应 .meta：el-tag size=small 实际 20px，不是 22；行高 20 + margin-bottom 8
  .meta {
    margin-bottom: 8px;
    .sk-tag {
      width: 36px;
      height: 20px;
      border-radius: 4px;
    }
    .sk-year {
      width: 32px;
      height: 14px;
    }
  }

  // 对应 .ratings-row（min-height 18 + margin-bottom 8）
  .sk-ratings {
    height: 18px;
    width: 60%;
    margin-bottom: 8px;
  }

}

.poster-card {
  margin-bottom: 16px;
  overflow: hidden;
  // 卡片整体先出（含文字内容）—— 0.2s 内完成，文字"瞬间到位"
  animation: fade-in 0.2s ease;
  // 视口外的卡片跳过渲染/图片解码；contain-intrinsic-size 给个估算（poster 2:3 + meta ~130）
  content-visibility: auto;
  contain-intrinsic-size: 240px 490px;

  .poster :deep(.el-image) {
    background: linear-gradient(135deg, var(--jt-skeleton-from) 0%, var(--jt-card-border) 100%);
  }

  // 图片真正加载完成（el-image__inner 进 DOM）时淡入；
  // animation-delay = 100ms（让文字先到）+ var(--card-idx) × 60ms（同一行错峰，"一张接一张冒出"）
  // backwards：动画开始前保持初始（透明）状态，避免出现"先闪一下原图再淡入"的怪样
  :deep(.el-image__inner) {
    animation: poster-fade-in 0.45s ease backwards;
    animation-delay: calc(100ms + var(--card-idx, 0) * 60ms);
  }

  .poster {
    position: relative;
    cursor: pointer;

    .overview-overlay {
      position: absolute;
      inset: 0;
      background: rgba(15, 23, 42, 0.92);
      color: #fff;
      display: flex;
      flex-direction: column;
      cursor: default;
      animation: fade-in 0.2s ease;

      .overview-close {
        position: absolute;
        top: 6px;
        right: 6px;
        width: 24px;
        height: 24px;
        background: rgba(255, 255, 255, 0.15);
        color: #fff;
        border: none;
        border-radius: 50%;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 14px;
        transition: background 0.15s;
        z-index: 1;

        &:hover { background: rgba(239, 68, 68, 0.85); }
      }

      .overview-content {
        flex: 1;
        overflow-y: auto;
        padding: 30px 14px 14px;
        font-size: 12px;
        line-height: 1.6;

        &::-webkit-scrollbar { width: 4px; }
        &::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.3); border-radius: 2px; }
        &::-webkit-scrollbar-track { background: transparent; }

        .overview-title {
          font-size: 13px;
          font-weight: 600;
          margin-bottom: 8px;
          padding-bottom: 6px;
          border-bottom: 1px solid rgba(255, 255, 255, 0.15);
        }
        .overview-text {
          color: rgba(255, 255, 255, 0.92);
          word-break: break-word;
          white-space: pre-wrap;
        }
        // 简介加载中：导演行下方一小段，spinner + 文字（居中）
        .overview-loading {
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 6px;
          margin-top: 10px;
          color: rgba(255, 255, 255, 0.65);
          font-size: 11px;
          .spin {
            animation: spin 1s linear infinite;
            font-size: 13px;
          }
        }
      }
    }

    .no-poster {
      width: 100%;
      aspect-ratio: 2/3;
      background: var(--jt-fill-light);
      color: var(--jt-text-muted);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 12px;
    }

    // 主源评分徽章：按 source 上色，跟 RatingsBadges compact 风格完全一致
    // padding / font-size / min-width 必须跟下方 .badge 等同，否则两列高度对不齐
    .source-rating-badge {
      position: absolute;
      top: 8px;
      right: 8px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      padding: 2px 7px;
      border-radius: 4px;
      font-size: 11px;
      line-height: 1.4;
      font-weight: 500;
      min-width: 68px;
      box-sizing: border-box;
      z-index: 3;
      transition: filter 0.15s, transform 0.15s;
      .src {
        opacity: 0.85;
        font-weight: 500;
        font-size: 11px;
      }
      .src-img {
        height: 11px;
        width: auto;
        display: block;
        flex-shrink: 0;
      }
      .val { font-weight: 700; }

      // 各 source 配色（与 RatingsBadges 同款）
      &.tmdb    { background: #0d253f; color: #fff; border: 1px solid #0d253f; }
      &.trakt   { background: #ed1c24; color: #fff; border: 1px solid #c41318; }
      &.anilist { background: #02a9ff; color: #fff; border: 1px solid #0288d1; }
      &.douban  { background: #007722; color: #fff; border: 1px solid #00691e; }
      &.default { background: rgba(0,0,0,0.7); color: #ffd700; border: 1px solid rgba(0,0,0,0.85); }

      &.clickable {
        cursor: pointer;
        &:hover { filter: brightness(1.08); transform: scale(1.04); }
      }
      // 展开时：底部圆角去掉 + 边框去掉 → 跟下方第一个徽章贴合
      &.expanded {
        border-bottom-left-radius: 0;
        border-bottom-right-radius: 0;
      }
    }
    // 展开的多维评分（竖排，紧贴 source-rating-badge 下方）
    // 关键：跟上方主源徽章用相同的 padding / font-size / min-width，避免高度差
    .ratings-expanded {
      position: absolute;
      // top 跟主源徽章贴合：top(8) + 主源徽章 height(font 11px * 1.4 + padding 2*2 = ~19px+边框 2) ≈ 29
      top: 29px;
      right: 8px;
      z-index: 2;
      padding: 0;
      background: transparent;
      backdrop-filter: none;
      max-width: calc(100% - 16px);

      // 覆盖 RatingsBadges 的 column gap 让徽章无缝串联
      :deep(.dir-column) { gap: 0; }
      :deep(.badge) {
        // 跟主源徽章一致
        border-radius: 0;
        min-width: 68px;
        padding: 2px 7px;
        font-size: 11px;
        line-height: 1.4;
      }
      :deep(.badge:last-child) {
        border-bottom-left-radius: 4px;
        border-bottom-right-radius: 4px;
      }
      :deep(.badge .src-img) {
        height: 11px;
      }
    }

    .src-badge {
      position: absolute;
      bottom: 6px;
      left: 6px;
      background: rgba(99, 102, 241, 0.92);
      color: #fff;
      padding: 2px 8px;
      border-radius: 10px;
      font-size: 11px;
      letter-spacing: 0.2px;
    }

    // ============ 媒体类型徽标（海报左上角）============
    // Trakt 风格：白色 glyph + drop-shadow，无背景；不同 media_type 用不同色调（蓝/橙/粉/红/灰）
    .media-type-badge {
      position: absolute;
      top: 6px;
      left: 6px;
      width: 22px;
      height: 22px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.85)) drop-shadow(0 0 1px rgba(0, 0, 0, 0.9));
      pointer-events: none;
      z-index: 1;

      svg {
        width: 18px;
        height: 18px;
        display: block;
      }

      // 不同类型用不同颜色 hue（白色 fallback 已设在 color）
      &.mt-movie  { color: #e0f2fe; }
      &.mt-tv     { color: #fef3c7; }
      &.mt-anime  { color: #fce7f3; }
      &.mt-adult  { color: #fecaca; }
      &.mt-person { color: #e2e8f0; }
    }

    .poster-overview-btn {
      position: absolute;
      bottom: 6px;
      right: 6px;
      z-index: 3;
      opacity: 0;
      transition: opacity 0.2s;
      background: rgba(0, 0, 0, 0.6) !important;
      border-color: rgba(255, 255, 255, 0.2) !important;
      color: #fff !important;

      &:hover {
        background: rgba(0, 0, 0, 0.8) !important;
        border-color: rgba(255, 255, 255, 0.4) !important;
      }
    }

    &:hover .poster-overview-btn {
      opacity: 1;
    }
  }

  .info {
    padding: 10px;

    .title-row {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 6px;
    }

    .title {
      font-size: 14px;
      font-weight: 500;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      flex: 1;
      min-width: 0;

      &.clickable {
        cursor: pointer;
        &:hover { color: var(--jt-brand); }
      }
    }

    .title-search-btn {
      flex-shrink: 0;
      margin-left: auto !important;
    }

    .meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      min-height: 22px;
      gap: 6px;

      .genre-tags {
        display: flex;
        gap: 4px;
        flex-wrap: nowrap;
        overflow: hidden;
        flex: 1 1 auto;
        min-width: 0;
      }
      .genre-tag {
        max-width: 100%;
        :deep(.el-tag__content) {
          overflow: hidden;
          text-overflow: ellipsis;
        }
      }
      .meta-placeholder {
        flex: 1 1 auto;
      }
      .year { color: var(--jt-text-muted); font-size: 12px; flex: 0 0 auto; }
    }

    .ratings-row {
      margin-bottom: 8px;
      min-height: 18px;
    }
  }
}
</style>

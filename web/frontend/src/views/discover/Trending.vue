<template>
  <div class="page-container">
    <div class="page-header">
      <h2>
        热门推荐
        <el-tag v-if="cached" type="info" size="small" effect="plain" class="cache-tag">
          缓存
        </el-tag>
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
          <el-select
            v-model="doubanDoulistId"
            placeholder="选择片单"
            size="small"
            style="width: 240px"
            @change="reload()"
          >
            <el-option
              v-for="d in doubanLists"
              :key="d.doulist_id"
              :label="d.name"
              :value="d.doulist_id"
            />
          </el-select>
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
            <!-- 对应 .actions-row 两个按钮 -->
            <div class="actions-row">
              <div class="sk-block sk-btn" />
              <div class="sk-block sk-btn" />
            </div>
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
            <!-- 媒体类型徽标：左上角，pill 风格 + 类型色，对齐 Trakt 的视觉惯例 -->
            <div class="media-type-pill" :class="`mt-${item.media_type}`">
              <span class="mt-icon">{{ mediaTypeIcon(item.media_type) }}</span>
              <span class="mt-label">{{ mediaTypeLabel(item.media_type) }}</span>
            </div>
            <div class="rating" v-if="item.rating != null">
              <el-icon><Star /></el-icon>
              {{ item.rating.toFixed(1) }}
            </div>
            <div v-if="item.badge" class="src-badge">{{ item.badge }}</div>
            <div
              v-if="overviewVisible[item._key]"
              class="overview-overlay"
              @click.stop
            >
              <button class="overview-close" @click.stop="hideOverview(item)" title="关闭">
                <el-icon><Close /></el-icon>
              </button>
              <div class="overview-content">
                <div class="overview-text">{{ item.overview || (item._overviewLoading ? '加载中…' : '暂无简介') }}</div>
              </div>
            </div>
          </div>
          <div class="info">
            <div class="title clickable" :title="item.title" @click="openDetail(item)">{{ item.title }}</div>
            <div class="meta">
              <!-- 风格标签：取前 2 个，太多会换行；类型徽标已在海报左上角 -->
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
            <RatingsBadges
              v-if="item.tmdb_id && item.media_type !== 'person'"
              compact
              :rating="ratingsByKey[`${item.tmdb_id}-${item.media_type === 'tv' ? 'tv' : 'movie'}`]"
              class="ratings-row"
            />
            <div class="actions-row">
              <el-button size="small" type="primary" plain class="action-btn" @click.stop="searchTorrents(item)">
                <el-icon><Search /></el-icon>
                搜种子
              </el-button>
              <el-button size="small" type="primary" plain class="action-btn" @click.stop="toggleOverview(item)">
                <el-icon><Document /></el-icon>
                简介
              </el-button>
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
import { ref, reactive, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Refresh, Star, Search, Document, Close, Loading } from '@element-plus/icons-vue'
import { discoverApi, ratingsApi } from '@/api'
import RatingsBadges from '@/components/RatingsBadges.vue'
import { debugInfo } from '@/composables/useDebugInfo'

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

// 海报左上角小徽标用：emoji 简单直观，跟 Trakt 卡片视觉一致；后续要换 SVG 在这里改即可
const mediaTypeIcon = (mt) => {
  if (mt === 'tv') return '📺'
  if (mt === 'anime') return '🎌'
  if (mt === 'person') return '👤'
  if (mt === 'adult') return '🔞'
  return '🎬'
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
      // 豆瓣 doulist 卡片只爬到导演 / 类型 / 年份；正式简介需用户点开后再 lazy fetch（toggleOverview）
      return {
        _key: `douban-${it.douban_id || idx}-${idx}`,
        douban_id: it.douban_id,
        title: it.title,
        year: it.year,
        poster_url: proxiedPoster,
        rating: it.rating,
        overview: it.director ? `导演：${it.director}` : '',
        media_type: 'movie',
        genres: it.genres || [],
        badge: it.votes ? `${it.votes} 评价` : null,
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

const toggleOverview = async (item) => {
  const willOpen = !overviewVisible[item._key]
  overviewVisible[item._key] = willOpen
  if (!willOpen) return
  // 豆瓣条目：列表页爬不到真正的剧情简介（doulist 卡片只有导演/类型/年份），
  // 用户点开时再去 /api/discover/douban-detail 拉一次（30 天缓存，命中后毫秒级）
  if (source.value === 'douban' && item.douban_id && !item._overviewFetched) {
    item._overviewLoading = true
    try {
      const r = await discoverApi.doubanDetail(item.douban_id)
      const summary = r?.data?.summary
      if (summary) {
        const head = item.overview || ''
        // 头部保留导演/类型行，再换行追加正式简介
        item.overview = head ? `${head}\n\n${summary}` : summary
      }
    } catch (e) {
      console.warn('豆瓣简介拉取失败', e)
    } finally {
      item._overviewLoading = false
      item._overviewFetched = true
    }
  }
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
  await load(forceRefresh, /*append*/ false)
  // 首批渲染后立刻预取下一页（async 不阻塞 DOM 渲染），不再延迟
  prefetchIfNeeded()
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
const fetchRatings = async () => {
  const payload = displayItems.value
    .filter((x) => x.tmdb_id && x.media_type !== 'person' && x.media_type !== 'anime')
    .map((x) => ({
      tmdb_id: x.tmdb_id,
      media_type: x.media_type === 'tv' ? 'tv' : 'movie',
      title: x.title || x.original_title,
      year: x.year ? parseInt(x.year) : null,
    }))
  if (!payload.length) return
  try {
    const res = await ratingsApi.batch(payload)
    for (const r of res.data.ratings || []) {
      ratingsByKey[`${r.tmdb_id}-${r.media_type}`] = r
    }
  } catch (e) {
    console.warn('评分批量拉取失败', e)
  }
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
const resolveEnglishTitle = async (item) => {
  if (item.english_title) return item.english_title
  if (item.original_language === 'en' && item.original_title) return item.original_title
  // AniList: title 多半是 romaji（拉丁字母），可直接当英文使用
  if (source.value === 'anilist') return item.original_title || item.title

  if (item.tmdb_id && (item.media_type === 'movie' || item.media_type === 'tv')) {
    try {
      const r = await discoverApi.titleEn(item.media_type, item.tmdb_id)
      const en = r?.data?.english_title
      if (en) {
        // 缓存到 item 上避免重复查
        item.english_title = en
        return en
      }
    } catch (e) {
      console.warn('英文标题查询失败', e)
    }
  }
  return item.original_title || item.title
}

const searchTorrents = async (item) => {
  let q = await resolveEnglishTitle(item)
  // 附带年份消歧：种子搜索引擎对 "Title 2010" 这种 query 命中率明显高于裸标题
  if (item.year) q = `${q} ${item.year}`
  router.push({
    path: '/resourcesearch',
    query: {
      q,
      type: item.media_type === 'tv' ? 'tv' : (item.media_type === 'anime' ? 'tv' : 'movie'),
    },
  })
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

onMounted(async () => {
  await reload()
  await nextTick()
  setupObserver()
  window.addEventListener('resize', onWindowResize)
  // 监 grid 内容滚动（多数情况主体滚动是 .app-main 内部，加 capture 兜底）
  window.addEventListener('scroll', onWindowScroll, { passive: true, capture: true })
  writeDebug()
  updateScrollRow()
})

onBeforeUnmount(() => {
  if (observer) observer.disconnect()
  window.removeEventListener('resize', onWindowResize)
  window.removeEventListener('scroll', onWindowScroll, { capture: true })
  if (resizeTimer) clearTimeout(resizeTimer)
  if (scrollTimer) cancelAnimationFrame(scrollTimer)
  // 离开本页时关掉侧边栏的 debug 显示
  debugInfo.enabled = false
})
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
  color: #94a3b8;
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
  background: rgba(255, 255, 255, 0.7);
  backdrop-filter: saturate(180%) blur(14px);
  -webkit-backdrop-filter: saturate(180%) blur(14px);
  border-bottom: 1px solid rgba(0, 0, 0, 0.06);

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
    color: #94a3b8;
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

  // shimmer 渐变 = 浅灰 + 反光带从左滑到右
  $shimmer-bg: linear-gradient(90deg,
    #eef2f6 0%,
    #f7f9fb 50%,
    #eef2f6 100%
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

  // 对应 .actions-row 两个 el-button size=small（实际 24px，不是 28）
  .actions-row {
    .sk-btn {
      flex: 1;
      height: 24px;
      border-radius: 4px;
    }
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

  // el-image 容器自身：在图片到达前用浅灰渐变占位，比白色更明显地表达"图片在路上"
  .poster :deep(.el-image) {
    background: linear-gradient(135deg, #eef2f6 0%, #d8dee6 100%);
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
      }
    }

    .no-poster {
      width: 100%;
      aspect-ratio: 2/3;
      background: #f5f7fa;
      color: #909399;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 12px;
    }

    .rating {
      position: absolute;
      top: 8px;
      right: 8px;
      background: rgba(0, 0, 0, 0.7);
      color: #ffd700;
      padding: 2px 6px;
      border-radius: 3px;
      font-size: 12px;
      display: flex;
      align-items: center;
      gap: 3px;
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
    // 半透明深色底 + 类型色边框，hover 不会跟海报主色冲突
    // 不同 media_type 用不同 hue 区分，跟 Trakt 一致：电影偏冷蓝，剧集橙，番剧粉，成人红
    .media-type-pill {
      position: absolute;
      top: 8px;
      left: 8px;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 3px 8px;
      border-radius: 12px;
      font-size: 11px;
      line-height: 1.2;
      font-weight: 600;
      letter-spacing: 0.3px;
      background: rgba(15, 23, 42, 0.78);
      color: #fff;
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.25);
      pointer-events: none;
      z-index: 1;

      .mt-icon { font-size: 12px; line-height: 1; }
      .mt-label { font-size: 11px; }

      // 类型色：用边框/侧边色条区分，避免主底色被海报背景吞掉
      &.mt-movie  { box-shadow: 0 2px 6px rgba(0,0,0,0.25), inset 2px 0 0 #60a5fa; }
      &.mt-tv     { box-shadow: 0 2px 6px rgba(0,0,0,0.25), inset 2px 0 0 #f59e0b; }
      &.mt-anime  { box-shadow: 0 2px 6px rgba(0,0,0,0.25), inset 2px 0 0 #ec4899; }
      &.mt-adult  { box-shadow: 0 2px 6px rgba(0,0,0,0.25), inset 2px 0 0 #ef4444; }
      &.mt-person { box-shadow: 0 2px 6px rgba(0,0,0,0.25), inset 2px 0 0 #94a3b8; }
    }
  }

  .info {
    padding: 10px;

    .title {
      font-size: 14px;
      font-weight: 500;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      margin-bottom: 6px;

      &.clickable {
        cursor: pointer;
        &:hover { color: #6366f1; }
      }
    }

    .meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
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
        // size=small 默认 22px，跟 .meta min-height 22px 对齐
        :deep(.el-tag__content) {
          overflow: hidden;
          text-overflow: ellipsis;
        }
      }
      .meta-placeholder {
        flex: 1 1 auto;
      }
      .year { color: #909399; font-size: 12px; flex: 0 0 auto; }
    }

    .ratings-row {
      margin-bottom: 8px;
      min-height: 18px;
    }

    .actions-row {
      display: flex;
      gap: 6px;

      .action-btn { margin-left: 0 !important; }
      .action-btn + .action-btn { margin-left: auto !important; }
    }
  }
}
</style>

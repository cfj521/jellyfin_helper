<template>
  <div class="page-container">
    <div class="page-header">
      <h2>
        热门推荐
        <el-tag v-if="cached" type="info" size="small" effect="plain" class="cache-tag">
          缓存
        </el-tag>
      </h2>
      <div class="header-actions">
        <el-button @click="load(true)" :loading="loading" title="强制从 TMDB 重新拉取（绕过 30 分钟缓存）">
          <el-icon><Refresh /></el-icon>
          强制刷新
        </el-button>
      </div>
    </div>

    <!-- 顶层分类 -->
    <el-card shadow="never" class="filter-card">
      <div class="filter-row">
        <el-radio-group v-model="topCategory" @change="onTopChange">
          <el-radio-button label="trending">趋势</el-radio-button>
          <el-radio-button label="movie">电影</el-radio-button>
          <el-radio-button label="tv">剧集</el-radio-button>
        </el-radio-group>

        <el-divider direction="vertical" />

        <!-- Trending 子选项 -->
        <template v-if="topCategory === 'trending'">
          <el-radio-group v-model="trendingMediaType" size="small" @change="load()">
            <el-radio-button label="all">全部</el-radio-button>
            <el-radio-button label="movie">电影</el-radio-button>
            <el-radio-button label="tv">剧集</el-radio-button>
          </el-radio-group>
          <el-radio-group v-model="trendingTimeWindow" size="small" @change="load()">
            <el-radio-button label="day">今日</el-radio-button>
            <el-radio-button label="week">本周</el-radio-button>
          </el-radio-group>
        </template>

        <!-- 电影子分类 -->
        <template v-else-if="topCategory === 'movie'">
          <el-radio-group v-model="movieCategory" size="small" @change="load()">
            <el-radio-button label="popular">最受欢迎</el-radio-button>
            <el-radio-button label="now_playing">正在上映</el-radio-button>
            <el-radio-button label="upcoming">即将上映</el-radio-button>
            <el-radio-button label="top_rated">高分推荐</el-radio-button>
          </el-radio-group>
        </template>

        <!-- 剧集子分类 -->
        <template v-else-if="topCategory === 'tv'">
          <el-radio-group v-model="tvCategory" size="small" @change="load()">
            <el-radio-button label="popular">最受欢迎</el-radio-button>
            <el-radio-button label="airing_today">今日播出</el-radio-button>
            <el-radio-button label="on_the_air">正在播出</el-radio-button>
            <el-radio-button label="top_rated">高分推荐</el-radio-button>
          </el-radio-group>
        </template>
      </div>
    </el-card>

    <el-row :gutter="16" v-loading="loading">
      <el-col v-for="item in items" :key="item.tmdb_id" :xs="12" :sm="8" :md="6" :lg="4" :xl="3">
        <el-card shadow="hover" class="poster-card" body-style="padding: 0">
          <!-- 海报点击 → 详情 -->
          <div class="poster" @click="openDetail(item)">
            <el-image
              v-if="item.poster_url"
              :src="item.poster_url"
              fit="cover"
              lazy
              style="width: 100%; aspect-ratio: 2/3"
            />
            <div v-else class="no-poster">无海报</div>
            <div class="rating">
              <el-icon><Star /></el-icon>
              {{ item.vote_average?.toFixed(1) || '-' }}
            </div>

            <!-- 简介浮层 -->
            <div
              v-if="overviewVisible[item.tmdb_id]"
              class="overview-overlay"
              @click.stop
            >
              <button class="overview-close" @click.stop="hideOverview(item)" title="关闭">
                <el-icon><Close /></el-icon>
              </button>
              <div class="overview-content">
                <div class="overview-title">{{ item.title }}</div>
                <div class="overview-text">{{ item.overview || '暂无简介' }}</div>
              </div>
            </div>
          </div>
          <div class="info">
            <div class="title clickable" :title="item.title" @click="openDetail(item)">{{ item.title }}</div>
            <div class="meta">
              <el-tag size="small">{{ item.media_type === 'tv' ? '剧集' : '电影' }}</el-tag>
              <span class="year">{{ (item.release_date || '').slice(0, 4) }}</span>
            </div>
            <RatingsBadges
              v-if="item.media_type !== 'person'"
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

    <el-empty v-if="!loading && !items.length" description="暂无数据，检查 TMDB API Key 是否配置" />
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh, Star, Search, Document, Close } from '@element-plus/icons-vue'
import { discoverApi, ratingsApi } from '@/api'
import RatingsBadges from '@/components/RatingsBadges.vue'

const router = useRouter()

const topCategory = ref('trending')
const trendingMediaType = ref('all')
const trendingTimeWindow = ref('week')
const movieCategory = ref('popular')
const tvCategory = ref('popular')

const items = ref([])
const loading = ref(false)
const cached = ref(false)
// 简介浮层显示状态：tmdb_id → bool
const overviewVisible = reactive({})
// 评分缓存：key=`${tmdb_id}-${media_type}` → RatingResponse
const ratingsByKey = reactive({})

const toggleOverview = (item) => {
  overviewVisible[item.tmdb_id] = !overviewVisible[item.tmdb_id]
}

const hideOverview = (item) => {
  overviewVisible[item.tmdb_id] = false
}

const onTopChange = () => {
  // 子分类不变，但要重新加载
  load()
}

const load = async (forceRefresh = false) => {
  loading.value = true
  // 切换分类时关闭所有打开的简介浮层
  for (const k of Object.keys(overviewVisible)) overviewVisible[k] = false
  try {
    let res
    if (topCategory.value === 'trending') {
      res = await discoverApi.trending({
        media_type: trendingMediaType.value,
        time_window: trendingTimeWindow.value,
        refresh: forceRefresh,
      })
    } else {
      const mediaType = topCategory.value
      const category = mediaType === 'movie' ? movieCategory.value : tvCategory.value
      res = await discoverApi.list({
        media_type: mediaType,
        category,
        refresh: forceRefresh,
      })
    }
    items.value = res.data.items
    cached.value = !!res.data.cached
    fetchRatings()
  } catch (e) {
    console.error('加载失败', e)
  } finally {
    loading.value = false
  }
}

// 批量拉评分。失败/为空时不打扰用户，列表 UI 直接缺位。
const fetchRatings = async () => {
  const payload = items.value
    .filter((x) => x.tmdb_id && x.media_type !== 'person')
    .map((x) => ({
      tmdb_id: x.tmdb_id,
      media_type: x.media_type === 'tv' ? 'tv' : 'movie',
      title: x.title || x.original_title,
      year: parseInt((x.release_date || x.first_air_date || '').slice(0, 4)) || null,
    }))
  if (!payload.length) return
  try {
    const res = await ratingsApi.batch(payload)
    for (const r of res.data.ratings || []) {
      ratingsByKey[`${r.tmdb_id}-${r.media_type}`] = r
    }
  } catch (e) {
    // 评分接口挂了不影响主流程，悄悄失败
    console.warn('评分批量拉取失败', e)
  }
}

// 优先用英文标题搜种子（种子站点大多用原始/英文命名，中文搜不到）
// 列表场景没有 english_title，只能用 original_title 兜底
const pickSearchName = (item) => {
  if (item.original_language === 'en' && item.original_title) return item.original_title
  return item.original_title || item.title
}

const searchTorrents = (item) => {
  router.push({
    path: '/discover/search',
    query: { q: pickSearchName(item), type: item.media_type === 'tv' ? 'tv' : 'movie' },
  })
}

const openDetail = (item) => {
  // trending 接口的 person 类型不要进入详情
  if (item.media_type === 'person') return
  router.push({
    name: 'DiscoverDetail',
    params: {
      mediaType: item.media_type === 'tv' ? 'tv' : 'movie',
      tmdbId: item.tmdb_id,
    },
  })
}

onMounted(load)
</script>

<style lang="scss" scoped>
@keyframes fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

.cache-tag {
  margin-left: 10px;
  vertical-align: middle;
}

.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

.filter-card {
  margin-bottom: 16px;
  :deep(.el-card__body) { padding: 12px 16px; }

  .filter-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
}

.poster-card {
  margin-bottom: 16px;
  overflow: hidden;

  .poster {
    position: relative;
    cursor: pointer;

    // 简介浮层：覆盖海报，半透明深色 + 滚动
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

        &:hover {
          background: rgba(239, 68, 68, 0.85);
        }
      }

      .overview-content {
        flex: 1;
        overflow-y: auto;
        padding: 30px 14px 14px;
        font-size: 12px;
        line-height: 1.6;

        // 自定义滚动条
        &::-webkit-scrollbar {
          width: 4px;
        }
        &::-webkit-scrollbar-thumb {
          background: rgba(255, 255, 255, 0.3);
          border-radius: 2px;
        }
        &::-webkit-scrollbar-track {
          background: transparent;
        }

        .overview-title {
          font-size: 13px;
          font-weight: 600;
          margin-bottom: 8px;
          color: #fff;
          padding-bottom: 6px;
          border-bottom: 1px solid rgba(255, 255, 255, 0.15);
        }

        .overview-text {
          color: rgba(255, 255, 255, 0.92);
          word-break: break-word;
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

        &:hover {
          color: #6366f1;
        }
      }
    }

    .meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;

      .year {
        color: #909399;
        font-size: 12px;
      }
    }

    .ratings-row {
      margin-bottom: 8px;
      min-height: 18px;  // 占位，避免拉取过程中布局抖动
    }

    .actions-row {
      display: flex;
      gap: 6px;

      .action-btn {
        margin-left: 0 !important;  // 覆盖 theme.scss 表格里的 +button 间距
      }

      // 第二个按钮（简介）靠右
      .action-btn + .action-btn {
        margin-left: auto !important;
      }
    }
  }
}
</style>

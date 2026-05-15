<template>
  <div class="page-container detail-page" v-loading="loading">
    <!-- 背景图英雄区 -->
    <div class="hero" :style="heroStyle">
      <div class="hero-overlay" />
      <div class="hero-content">
        <el-button class="back-btn" @click="$router.back()">
          <el-icon><ArrowLeft /></el-icon>
          返回
        </el-button>

        <div v-if="data" class="hero-grid">
          <!-- 海报 -->
          <div class="poster-col">
            <el-image
              v-if="data.poster_url"
              :src="data.poster_url"
              fit="cover"
              :preview-src-list="[data.poster_url]"
              hide-on-click-modal
              class="poster-img"
            />
            <div v-else class="poster-img no-poster">无海报</div>
          </div>

          <!-- 主信息 -->
          <div class="info-col">
            <h1 class="title">
              {{ data.title }}
              <span v-if="year" class="year">({{ year }})</span>
            </h1>
            <div v-if="data.original_title && data.original_title !== data.title" class="original-title">
              {{ data.original_title }}
            </div>
            <div v-if="data.tagline" class="tagline">"{{ data.tagline }}"</div>

            <!-- 多维评分（替换原来的单一 TMDB 5 角星）-->
            <div class="detail-ratings">
              <RatingsBadges :rating="rating" />
            </div>

            <!-- 类型 / 时长 / 状态 -->
            <div class="meta-row">
              <span v-if="data.runtime" class="meta-item">{{ data.runtime }} 分钟</span>
              <span v-else-if="data.episode_runtime?.length" class="meta-item">每集 {{ data.episode_runtime[0] }} 分钟</span>
              <el-divider direction="vertical" v-if="data.status" />
              <span v-if="data.status" class="meta-item">{{ statusLabel(data.status) }}</span>
              <el-divider direction="vertical" v-if="data.media_type === 'tv' && data.number_of_seasons" />
              <span v-if="data.media_type === 'tv' && data.number_of_seasons" class="meta-item">
                {{ data.number_of_seasons }} 季 · 共 {{ data.number_of_episodes }} 集
              </span>
            </div>

            <!-- 类型 tags -->
            <div v-if="data.genres?.length" class="genres">
              <el-tag v-for="g in data.genres" :key="g" type="info" effect="plain">{{ g }}</el-tag>
            </div>

            <!-- 简介 -->
            <p class="overview">{{ data.overview || '暂无简介' }}</p>

            <!-- 关键人物 -->
            <div class="key-people">
              <div v-if="data.directors?.length" class="kp-row">
                <span class="kp-label">{{ data.media_type === 'tv' ? '主创' : '导演' }}</span>
                <span>{{ data.directors.join(' · ') }}</span>
              </div>
              <div v-if="data.writers?.length" class="kp-row">
                <span class="kp-label">编剧</span>
                <span>{{ data.writers.join(' · ') }}</span>
              </div>
              <div v-if="data.studios?.length" class="kp-row">
                <span class="kp-label">出品</span>
                <span>{{ data.studios.join(' · ') }}</span>
              </div>
              <div v-if="data.countries?.length" class="kp-row">
                <span class="kp-label">国家</span>
                <span>{{ data.countries.join(' · ') }}</span>
              </div>
            </div>

            <!-- 操作按钮 -->
            <div class="actions">
              <el-button
                type="primary"
                size="large"
                :loading="searching"
                @click="searchTorrents"
              >
                <template #icon><el-icon><Search /></el-icon></template>
                搜种子下载
              </el-button>
              <el-link v-if="data.imdb_url" :href="data.imdb_url" target="_blank" type="warning" :underline="false">
                <el-tag type="warning" effect="plain">IMDb</el-tag>
              </el-link>
              <el-link v-if="data.homepage" :href="data.homepage" target="_blank" :underline="false">
                <el-tag effect="plain">官网</el-tag>
              </el-link>
              <el-link
                :href="`https://www.themoviedb.org/${data.media_type}/${data.tmdb_id}`"
                target="_blank"
                :underline="false"
              >
                <el-tag effect="plain">TMDB ↗</el-tag>
              </el-link>
            </div>

          </div>
        </div>
      </div>
    </div>

    <div v-if="data" class="detail-body">
      <!-- 演员表 -->
      <section v-if="data.cast?.length" class="section">
        <h3>演员</h3>
        <div class="cast-row">
          <div v-for="p in data.cast" :key="p.id" class="cast-item">
            <el-image
              v-if="p.profile_url"
              :src="p.profile_url"
              fit="cover"
              lazy
              class="cast-img"
            />
            <div v-else class="cast-img no-img">{{ (p.name || '?').charAt(0) }}</div>
            <div class="cast-name" :title="p.name">{{ p.name }}</div>
            <div class="cast-char" :title="p.character">{{ p.character }}</div>
          </div>
        </div>
      </section>

      <!-- 剧集季 -->
      <section v-if="data.media_type === 'tv' && data.seasons?.length" class="section">
        <h3>季列表</h3>
        <el-table :data="data.seasons" stripe size="small">
          <el-table-column label="" width="80">
            <template #default="{ row }">
              <el-image v-if="row.poster_url" :src="row.poster_url" fit="cover" lazy style="width: 60px; aspect-ratio: 2/3" />
            </template>
          </el-table-column>
          <el-table-column prop="name" label="季名" min-width="160" />
          <el-table-column label="集数" width="80">
            <template #default="{ row }">{{ row.episode_count }}</template>
          </el-table-column>
          <el-table-column prop="air_date" label="首播日期" width="120" />
          <el-table-column prop="overview" label="简介" show-overflow-tooltip />
        </el-table>
      </section>

      <!-- 视频 -->
      <section v-if="data.videos?.length" class="section">
        <h3>预告片</h3>
        <div class="video-row">
          <a
            v-for="v in data.videos"
            :key="v.key"
            :href="v.youtube_url"
            target="_blank"
            class="video-card"
          >
            <div class="video-thumb">
              <el-image :src="v.thumb_url" fit="cover" lazy />
              <div class="play-icon">▶</div>
            </div>
            <div class="video-name" :title="v.name">{{ v.name }}</div>
            <div class="video-type">{{ v.type }}{{ v.official ? ' · 官方' : '' }}</div>
          </a>
        </div>
      </section>

      <!-- 推荐 -->
      <section v-if="data.recommendations?.length" class="section">
        <h3>推荐</h3>
        <div class="rec-row">
          <div
            v-for="item in data.recommendations"
            :key="item.tmdb_id"
            class="rec-card"
            @click="goDetail(item)"
          >
            <el-image v-if="item.poster_url" :src="item.poster_url" fit="cover" lazy class="rec-img" />
            <div v-else class="rec-img no-img">无海报</div>
            <div class="rec-title" :title="item.title">{{ item.title }}</div>
            <div class="rec-meta">
              <el-icon><StarFilled /></el-icon>
              {{ item.vote_average?.toFixed(1) || '-' }}
              <span class="dot">·</span>
              {{ (item.release_date || '').slice(0, 4) }}
            </div>
          </div>
        </div>
      </section>

      <!-- 相似 -->
      <section v-if="data.similar?.length" class="section">
        <h3>相似</h3>
        <div class="rec-row">
          <div
            v-for="item in data.similar"
            :key="item.tmdb_id"
            class="rec-card"
            @click="goDetail(item)"
          >
            <el-image v-if="item.poster_url" :src="item.poster_url" fit="cover" lazy class="rec-img" />
            <div v-else class="rec-img no-img">无海报</div>
            <div class="rec-title" :title="item.title">{{ item.title }}</div>
            <div class="rec-meta">
              <el-icon><StarFilled /></el-icon>
              {{ item.vote_average?.toFixed(1) || '-' }}
              <span class="dot">·</span>
              {{ (item.release_date || '').slice(0, 4) }}
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Search, StarFilled } from '@element-plus/icons-vue'
import { discoverApi, ratingsApi } from '@/api'
import RatingsBadges from '@/components/RatingsBadges.vue'

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const data = ref(null)
const rating = ref(null)
let ratingPollTimer = null

const heroStyle = computed(() => {
  if (!data.value?.backdrop_url) return {}
  return {
    backgroundImage: `url(${data.value.backdrop_url})`,
  }
})

const year = computed(() => (data.value?.release_date || '').slice(0, 4))

const statusLabel = (s) => ({
  'Released': '已上映',
  'Returning Series': '更新中',
  'Ended': '已完结',
  'Canceled': '已取消',
  'In Production': '制作中',
  'Post Production': '后期制作',
  'Planned': '已立项',
}[s] || s)

const load = async () => {
  loading.value = true
  data.value = null
  rating.value = null
  try {
    const res = await discoverApi.detail(
      route.params.mediaType,
      Number(route.params.tmdbId),
    )
    data.value = res.data
    fetchRating()
  } catch (e) {
    console.error('加载详情失败', e)
  } finally {
    loading.value = false
  }
}

// 取评分：detail 页同步取 MDB List + 异步排队豆瓣，
// 豆瓣 5s/req 不会立刻有数据，所以 5s/15s 各轮询一次更新页面
// 把当前显示的本地化 title/year 传过去：MDB List 会覆盖 DB title 成英文，
// 后端豆瓣 worker 拿那个英文搜中文条目会失败，传 hint 是 P0 修复的关键
const fetchRating = async () => {
  if (!data.value) return
  const d = data.value
  const tmdbId = d.tmdb_id
  const mediaType = d.media_type === 'tv' ? 'tv' : 'movie'
  const imdbId = d.imdb_id || null
  const localTitle = d.title || null
  const yr = (d.release_date || '').slice(0, 4)
  const localYear = yr ? parseInt(yr) : null
  try {
    const res = await ratingsApi.get(tmdbId, mediaType, imdbId, localTitle, localYear)
    rating.value = res.data
  } catch (e) {
    console.warn('评分加载失败', e)
    return
  }
  // 豆瓣未 fresh → 5s 后再拉一次
  if (rating.value?.douban_status !== 'fresh') {
    if (ratingPollTimer) clearTimeout(ratingPollTimer)
    ratingPollTimer = setTimeout(async () => {
      try {
        const res = await ratingsApi.get(tmdbId, mediaType, imdbId, localTitle, localYear)
        rating.value = res.data
      } catch {}
    }, 6000)
  }
}

const searching = ref(false)
const searchTorrents = async () => {
  if (!data.value || searching.value) return
  searching.value = true
  try {
    // 种子站点大多用英文/原始语言命名，中文搜不到
    // 优先级：english_title（从 translations 抽出）→ original_title（如果原片就是英文）→ title
    const d = data.value
    const name = d.english_title
      || (d.original_language === 'en' ? d.original_title : null)
      || d.original_title
      || d.title
    // 附带年份消歧：种子站点 query "Title 2010" 命中率显著高于裸标题
    // 后端响应不直接提供 year，用 release_date / first_air_date 的前 4 位
    const yr = (d.release_date || '').slice(0, 4)
    const q = yr ? `${name} ${yr}` : name
    // await router.push 让 spin 持续到路由切换完成，视觉反馈更明确
    await router.push({
      path: '/resourcesearch',
      query: { q, type: d.media_type === 'tv' ? 'tv' : 'movie' },
    })
  } finally {
    searching.value = false
  }
}

const goDetail = (item) => {
  router.push({
    name: 'DiscoverDetail',
    params: { mediaType: item.media_type, tmdbId: item.tmdb_id },
  })
}

// 路由参数变化（点推荐里另一部）→ 重新加载
watch(() => route.fullPath, () => {
  if (route.name === 'DiscoverDetail') load()
})

onMounted(load)
onUnmounted(() => {
  if (ratingPollTimer) clearTimeout(ratingPollTimer)
})
</script>

<style lang="scss" scoped>
.detail-page {
  // 让背景延展到整个 main 区域
  margin: -20px;
  padding: 0;
}

.hero {
  position: relative;
  background-size: cover;
  background-position: center top;
  background-color: #0f172a;
  color: #fff;
  padding: 0 0 40px;
  min-height: 480px;

  .hero-overlay {
    position: absolute;
    inset: 0;
    background: linear-gradient(180deg, rgba(15, 23, 42, 0.55) 0%, rgba(15, 23, 42, 0.85) 60%, #0f172a 100%);
  }

  .hero-content {
    position: relative;
    padding: 24px 32px;

    .back-btn {
      background: rgba(255, 255, 255, 0.12);
      border-color: rgba(255, 255, 255, 0.2);
      color: #fff;
      backdrop-filter: blur(8px);

      &:hover {
        background: rgba(255, 255, 255, 0.2);
      }
    }
  }

  .hero-grid {
    display: grid;
    grid-template-columns: 280px 1fr;
    gap: 32px;
    margin-top: 24px;
    max-width: 1280px;

    @media (max-width: 768px) {
      grid-template-columns: 1fr;
    }
  }

  .poster-col {
    .poster-img {
      width: 100%;
      aspect-ratio: 2 / 3;
      border-radius: 8px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);

      &.no-poster {
        background: rgba(255, 255, 255, 0.1);
        display: flex;
        align-items: center;
        justify-content: center;
        color: rgba(255, 255, 255, 0.5);
      }
    }
  }

  .info-col {
    .title {
      margin: 0 0 4px;
      font-size: 32px;
      font-weight: 700;
      color: #fff;
      line-height: 1.2;

      .year {
        font-weight: 400;
        opacity: 0.7;
      }
    }

    .original-title {
      font-size: 16px;
      opacity: 0.7;
      margin-bottom: 8px;
    }

    .tagline {
      margin: 8px 0;
      font-style: italic;
      opacity: 0.7;
      font-size: 14px;
    }

    .meta-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 16px 0;
      font-size: 14px;
      flex-wrap: wrap;

      .meta-item {
        opacity: 0.85;
      }

      :deep(.el-divider--vertical) {
        background-color: rgba(255, 255, 255, 0.3);
      }
    }

    .detail-ratings {
      margin: 12px 0;
    }

    .genres {
      display: flex;
      gap: 6px;
      margin-bottom: 16px;
      flex-wrap: wrap;

      :deep(.el-tag) {
        background: rgba(255, 255, 255, 0.1);
        border-color: rgba(255, 255, 255, 0.25);
        color: #fff;
      }
    }

    .overview {
      margin: 16px 0;
      line-height: 1.7;
      max-width: 720px;
      opacity: 0.92;
    }

    .key-people {
      margin: 16px 0 24px;

      .kp-row {
        display: flex;
        gap: 12px;
        font-size: 14px;
        margin-bottom: 4px;

        .kp-label {
          min-width: 56px;
          opacity: 0.6;
        }
      }
    }

    .actions {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
  }
}

.detail-body {
  padding: 32px;
  background: #f1f5f9;
}

.section {
  margin-bottom: 32px;

  h3 {
    margin: 0 0 16px;
    font-size: 18px;
    font-weight: 600;
    color: #0f172a;
    padding-bottom: 8px;
    border-bottom: 2px solid #6366f1;
    display: inline-block;
  }
}

// 演员
.cast-row {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  padding-bottom: 8px;

  .cast-item {
    flex-shrink: 0;
    width: 110px;
    text-align: center;

    .cast-img {
      width: 100%;
      aspect-ratio: 2/3;
      border-radius: 6px;
      background: #e2e8f0;

      &.no-img {
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 28px;
        color: #94a3b8;
        font-weight: 600;
      }
    }

    .cast-name {
      margin-top: 6px;
      font-size: 13px;
      font-weight: 500;
      color: #0f172a;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .cast-char {
      font-size: 12px;
      color: #64748b;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
  }
}

// 视频
.video-row {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;

  .video-card {
    text-decoration: none;
    color: inherit;
    cursor: pointer;
    transition: transform 0.15s;

    &:hover {
      transform: translateY(-2px);

      .play-icon { opacity: 1; }
    }

    .video-thumb {
      position: relative;
      aspect-ratio: 16/9;
      border-radius: 6px;
      overflow: hidden;
      background: #000;

      .el-image {
        width: 100%;
        height: 100%;
      }

      .play-icon {
        position: absolute;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        width: 50px;
        height: 50px;
        background: rgba(99, 102, 241, 0.9);
        color: #fff;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 18px;
        opacity: 0.85;
        transition: opacity 0.2s;
      }
    }

    .video-name {
      margin-top: 6px;
      font-size: 13px;
      color: #0f172a;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-weight: 500;
    }

    .video-type {
      font-size: 11px;
      color: #64748b;
    }
  }
}

// 推荐 / 相似
.rec-row {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px;

  .rec-card {
    cursor: pointer;
    transition: transform 0.15s;

    &:hover {
      transform: translateY(-3px);

      .rec-img {
        box-shadow: 0 6px 16px rgba(99, 102, 241, 0.2);
      }
    }

    .rec-img {
      width: 100%;
      aspect-ratio: 2/3;
      border-radius: 6px;
      transition: box-shadow 0.2s;

      &.no-img {
        background: #e2e8f0;
        display: flex;
        align-items: center;
        justify-content: center;
        color: #94a3b8;
        font-size: 12px;
      }
    }

    .rec-title {
      margin-top: 6px;
      font-size: 13px;
      font-weight: 500;
      color: #0f172a;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .rec-meta {
      font-size: 11px;
      color: #64748b;
      display: flex;
      align-items: center;
      gap: 4px;

      .el-icon { color: #fbbf24; font-size: 12px; }
      .dot { opacity: 0.5; }
    }
  }
}
</style>

<template>
  <div class="page-container detail-page" v-loading="loading">
    <!-- 背景图英雄区（豆瓣不给 backdrop，用海报虚化兜底） -->
    <div class="hero" :style="heroStyle">
      <div class="hero-overlay" />
      <div class="hero-content">
        <el-button class="back-btn" @click="$router.back()">
          <el-icon><ArrowLeft /></el-icon>
          返回
        </el-button>

        <!-- 加载失败时显式提示 + 重试 + 直接跳豆瓣条目页（兜底入口） -->
        <div v-if="!loading && loadError" class="load-error">
          <div class="err-title">无法加载豆瓣详情</div>
          <div class="err-msg">{{ loadError }}</div>
          <div class="err-actions">
            <el-button type="primary" @click="load">重试</el-button>
            <el-link
              :href="`https://movie.douban.com/subject/${$route.params.doubanId}/`"
              target="_blank"
              :underline="false"
            >
              <el-tag effect="plain">在豆瓣打开 ↗</el-tag>
            </el-link>
          </div>
        </div>

        <div v-else-if="data" class="hero-grid">
          <!-- 海报 -->
          <div class="poster-col">
            <el-image
              v-if="posterProxied"
              :src="posterProxied"
              fit="cover"
              :preview-src-list="[posterProxied]"
              hide-on-click-modal
              referrerpolicy="no-referrer"
              class="poster-img"
            />
            <div v-else class="poster-img no-poster">无海报</div>
          </div>

          <!-- 主信息 -->
          <div class="info-col">
            <h1 class="title">
              {{ data.title || '未知' }}
              <span v-if="data.year" class="year">({{ data.year }})</span>
            </h1>

            <!-- 评分 / 类型 / 时长 -->
            <div class="meta-row">
              <span v-if="data.rating != null" class="rating-big">
                <el-icon class="star-icon"><StarFilled /></el-icon>
                <span class="score">{{ data.rating.toFixed(1) }}</span>
                <span class="vote-count">/ 10 · {{ formatVotes(data.votes) }} 评价</span>
              </span>
              <el-divider direction="vertical" v-if="data.duration" />
              <span v-if="data.duration" class="meta-item">{{ data.duration }}</span>
              <el-divider direction="vertical" v-if="data.release_date" />
              <span v-if="data.release_date" class="meta-item">{{ data.release_date }}</span>
            </div>

            <!-- 类型 -->
            <div v-if="data.genres?.length" class="genres">
              <el-tag v-for="g in data.genres" :key="g" type="info" effect="plain">{{ g }}</el-tag>
            </div>

            <!-- 简介 -->
            <p class="overview">{{ data.summary || '暂无简介' }}</p>

            <!-- 关键信息 -->
            <div class="key-people">
              <div v-if="data.director" class="kp-row">
                <span class="kp-label">导演</span>
                <span>{{ data.director }}</span>
              </div>
              <div v-if="data.cast?.length" class="kp-row">
                <span class="kp-label">主演</span>
                <span>{{ data.cast.join(' · ') }}</span>
              </div>
              <div v-if="data.countries?.length" class="kp-row">
                <span class="kp-label">国家</span>
                <span>{{ data.countries.join(' · ') }}</span>
              </div>
              <div v-if="data.languages?.length" class="kp-row">
                <span class="kp-label">语言</span>
                <span>{{ data.languages.join(' · ') }}</span>
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
              <el-link
                :href="`https://movie.douban.com/subject/${data.douban_id}/`"
                target="_blank"
                :underline="false"
              >
                <el-tag type="success" effect="plain">豆瓣 ↗</el-tag>
              </el-link>
              <el-link
                v-if="data.imdb_id"
                :href="`https://www.imdb.com/title/${data.imdb_id}/`"
                target="_blank"
                type="warning"
                :underline="false"
              >
                <el-tag type="warning" effect="plain">IMDb ↗</el-tag>
              </el-link>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Search, StarFilled } from '@element-plus/icons-vue'
import { discoverApi } from '@/api'

const route = useRoute()
const router = useRouter()

const loading = ref(false)
const data = ref(null)
const loadError = ref(null)   // 拉取失败的错误描述
const searching = ref(false)  // "搜种子下载"按钮 in-flight（titleEnByImdb 期间）

// 豆瓣图片必须走后端反代（Referer 防盗链）
const posterProxied = computed(() => {
  if (!data.value?.poster_url) return null
  return `/api/img-proxy?url=${encodeURIComponent(data.value.poster_url)}`
})

const heroStyle = computed(() => {
  if (!posterProxied.value) return {}
  return {
    backgroundImage: `url(${posterProxied.value})`,
    backgroundColor: '#0f172a',
  }
})

const formatVotes = (n) => {
  if (!n) return 0
  if (n >= 10000) return `${(n / 10000).toFixed(1)}w`
  return n
}

const load = async () => {
  loading.value = true
  data.value = null
  loadError.value = null
  try {
    const id = String(route.params.doubanId)
    const res = await discoverApi.doubanDetail(id)
    data.value = res.data
    // 后端返回 200 但正文为空（反爬被拦或条目页结构变了）→ 显式提示，避免 UI 仅渲染半截
    if (!res.data || (!res.data.title && !res.data.summary)) {
      loadError.value = '豆瓣条目页拉取为空（可能是反爬拦截或条目已下架）'
    }
  } catch (e) {
    console.error('豆瓣详情加载失败', e)
    const detail = e?.response?.data?.detail || e?.message || '未知错误'
    loadError.value = `豆瓣详情加载失败：${detail}`
  } finally {
    loading.value = false
  }
}

const searchTorrents = async () => {
  if (!data.value || searching.value) return
  searching.value = true
  try {
    const d = data.value
    // 豆瓣条目的 title 是中文，PT/海外公网种子站几乎都按英文标题归档。
    // 有 imdb_id 时走 IMDb → TMDB → 英文标题；没有就退回中文。
    let q = d.title
    let mediaType = 'movie'
    if (d.imdb_id) {
      try {
        const r = await discoverApi.titleEnByImdb(d.imdb_id)
        const en = r?.data?.english_title
        if (en) q = en
        // TMDB find 同时返回 media_type，用它判 movie/tv 比靠时长猜更准
        if (r?.data?.media_type === 'tv') mediaType = 'tv'
      } catch (e) {
        console.warn('豆瓣 → IMDb → TMDB 英文标题解析失败，退回中文标题', e)
      }
    }
    if (d.year) q = `${q} ${d.year}`
    router.push({
      path: '/resourcesearch',
      query: { q, type: mediaType },
    })
  } finally {
    searching.value = false
  }
}

watch(() => route.fullPath, () => {
  if (route.name === 'DiscoverDoubanDetail') load()
})

onMounted(load)
</script>

<style lang="scss" scoped>
@use '@/styles/discover-detail.scss' as *;
@include discover-detail-styles;

// 豆瓣详情没有 hero 以外的内容区（不像 TMDB Detail 有相似/视频/演员墙），
// hero 结束之后就是空的 .app-main 背景 → 整页深色填底，hero 渐变收到 #0f172a 后无缝衔接
.detail-page {
  background: #0f172a;
  min-height: 100vh;
}

// hero 自身去掉硬性 min-height 限制，让它跟着内容自然撑开；下方剩余空间由 .detail-page 兜底
.hero {
  min-height: auto;
}

// 加载失败兜底卡片
.load-error {
  margin-top: 32px;
  padding: 24px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.08);
  backdrop-filter: blur(8px);
  border: 1px solid rgba(255, 255, 255, 0.15);
  max-width: 640px;
  color: #fff;

  .err-title {
    font-size: 18px;
    font-weight: 600;
    margin-bottom: 6px;
  }
  .err-msg {
    font-size: 14px;
    opacity: 0.85;
    margin-bottom: 16px;
    word-break: break-word;
  }
  .err-actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }
}
</style>

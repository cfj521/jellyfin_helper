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

        <div v-if="data" class="hero-grid">
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
              <el-button type="primary" size="large" @click="searchTorrents">
                <el-icon><Search /></el-icon>
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
  try {
    const id = String(route.params.doubanId)
    const res = await discoverApi.doubanDetail(id)
    data.value = res.data
  } catch (e) {
    console.error('豆瓣详情加载失败', e)
  } finally {
    loading.value = false
  }
}

const searchTorrents = () => {
  if (!data.value) return
  // 豆瓣条目大多是中文title；如果有 imdb_id，可以单独按 IMDB 链路搜更精准
  // 简化：直接用 title + year 走资源搜索，靠引擎自身做模糊匹配
  const d = data.value
  const q = d.year ? `${d.title} ${d.year}` : d.title
  router.push({
    path: '/resourcesearch',
    query: { q, type: 'movie' },
  })
}

watch(() => route.fullPath, () => {
  if (route.name === 'DiscoverDoubanDetail') load()
})

onMounted(load)
</script>

<style lang="scss" scoped>
@use '@/styles/discover-detail.scss' as *;
@include discover-detail-styles;
</style>

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
              v-if="data.cover_image"
              :src="data.cover_image"
              fit="cover"
              :preview-src-list="[data.cover_image]"
              hide-on-click-modal
              class="poster-img"
            />
            <div v-else class="poster-img no-poster">无海报</div>
          </div>

          <!-- 主信息 -->
          <div class="info-col">
            <h1 class="title">
              {{ displayTitle }}
              <span v-if="data.season_year" class="year">({{ data.season_year }})</span>
            </h1>
            <div v-if="data.title_native && data.title_native !== displayTitle" class="original-title">
              {{ data.title_native }}
            </div>

            <!-- 评分 / 集数 / 状态 -->
            <div class="meta-row">
              <span v-if="data.average_score != null" class="rating-big">
                <el-icon class="star-icon"><StarFilled /></el-icon>
                <span class="score">{{ (data.average_score / 10).toFixed(1) }}</span>
                <span class="vote-count">/ 10 · {{ data.popularity || 0 }} 收藏</span>
              </span>
              <el-divider direction="vertical" v-if="data.episodes" />
              <span v-if="data.episodes" class="meta-item">{{ data.episodes }} 集</span>
              <el-divider direction="vertical" v-if="data.duration" />
              <span v-if="data.duration" class="meta-item">每集 {{ data.duration }} 分钟</span>
              <el-divider direction="vertical" v-if="data.format" />
              <span v-if="data.format" class="meta-item">{{ data.format }}</span>
              <el-divider direction="vertical" v-if="data.status" />
              <span v-if="data.status" class="meta-item">{{ statusLabel(data.status) }}</span>
            </div>

            <!-- 类型 + 标签 -->
            <div v-if="data.genres?.length || data.tags?.length" class="genres">
              <el-tag v-for="g in data.genres" :key="g" type="info" effect="plain">{{ g }}</el-tag>
              <el-tag v-for="t in data.tags" :key="t" effect="plain">{{ t }}</el-tag>
            </div>

            <!-- 简介 -->
            <p class="overview">{{ data.description || '暂无简介' }}</p>

            <!-- 关键信息 -->
            <div class="key-people">
              <div v-if="data.studios?.length" class="kp-row">
                <span class="kp-label">制作</span>
                <span>{{ data.studios.join(' · ') }}</span>
              </div>
              <div v-if="data.source" class="kp-row">
                <span class="kp-label">原作</span>
                <span>{{ sourceLabel(data.source) }}</span>
              </div>
              <div v-if="data.season || data.season_year" class="kp-row">
                <span class="kp-label">档期</span>
                <span>{{ seasonLabel(data.season) }} {{ data.season_year || '' }}</span>
              </div>
              <div v-if="data.start_date" class="kp-row">
                <span class="kp-label">放送</span>
                <span>{{ data.start_date }}<span v-if="data.end_date && data.end_date !== data.start_date"> ~ {{ data.end_date }}</span></span>
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
                :href="`https://anilist.co/anime/${data.anilist_id}`"
                target="_blank"
                :underline="false"
              >
                <el-tag effect="plain">AniList ↗</el-tag>
              </el-link>
              <el-link
                v-if="data.mal_id"
                :href="`https://myanimelist.net/anime/${data.mal_id}`"
                target="_blank"
                :underline="false"
              >
                <el-tag effect="plain">MAL ↗</el-tag>
              </el-link>
              <el-link
                v-if="data.tmdb_id"
                :href="`https://www.themoviedb.org/tv/${data.tmdb_id}`"
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
      <!-- 角色 + 声优 -->
      <section v-if="data.cast?.length" class="section">
        <h3>角色 / 声优</h3>
        <div class="cast-row">
          <div v-for="(p, idx) in data.cast" :key="idx" class="cast-item">
            <el-image
              v-if="p.image"
              :src="p.image"
              fit="cover"
              lazy
              class="cast-img"
            />
            <div v-else class="cast-img no-img">{{ (p.character || '?').charAt(0) }}</div>
            <div class="cast-name" :title="p.character">{{ p.character }}</div>
            <div class="cast-char" :title="p.voice_actor">CV: {{ p.voice_actor || '—' }}</div>
          </div>
        </div>
      </section>

      <!-- 预告 -->
      <section v-if="data.trailer" class="section">
        <h3>预告片</h3>
        <div class="video-row">
          <a :href="data.trailer.youtube_url" target="_blank" class="video-card">
            <div class="video-thumb">
              <el-image :src="data.trailer.thumb_url" fit="cover" lazy />
              <div class="play-icon">▶</div>
            </div>
            <div class="video-name">官方预告</div>
            <div class="video-type">YouTube</div>
          </a>
        </div>
      </section>

      <!-- 关联作品 -->
      <section v-if="data.relations?.length" class="section">
        <h3>关联作品</h3>
        <div class="rec-row">
          <div
            v-for="item in data.relations"
            :key="item.anilist_id"
            class="rec-card"
            @click="goAniList(item.anilist_id)"
          >
            <el-image v-if="item.cover_image" :src="item.cover_image" fit="cover" lazy class="rec-img" />
            <div v-else class="rec-img no-img">无海报</div>
            <div class="rec-title" :title="item.title">{{ item.title }}</div>
            <div class="rec-meta">
              <span>{{ relationLabel(item.relation) }}</span>
              <span v-if="item.format" class="dot">·</span>
              <span v-if="item.format">{{ item.format }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- 推荐 -->
      <section v-if="data.recommendations?.length" class="section">
        <h3>推荐</h3>
        <div class="rec-row">
          <div
            v-for="item in data.recommendations"
            :key="item.anilist_id"
            class="rec-card"
            @click="goAniList(item.anilist_id)"
          >
            <el-image v-if="item.cover_image" :src="item.cover_image" fit="cover" lazy class="rec-img" />
            <div v-else class="rec-img no-img">无海报</div>
            <div class="rec-title" :title="item.title">{{ item.title }}</div>
            <div class="rec-meta">
              <el-icon><StarFilled /></el-icon>
              {{ item.average_score != null ? (item.average_score / 10).toFixed(1) : '-' }}
            </div>
          </div>
        </div>
      </section>
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

// AniList banner 当 hero 背景，缺失就用封面虚化
const heroStyle = computed(() => {
  const bg = data.value?.banner_image || data.value?.cover_image
  return bg ? { backgroundImage: `url(${bg})` } : {}
})

const displayTitle = computed(() => {
  const d = data.value || {}
  return d.title_english || d.title_romaji || d.title_native || ''
})

const statusLabel = (s) => ({
  'FINISHED': '已完结',
  'RELEASING': '放送中',
  'NOT_YET_RELEASED': '未放送',
  'CANCELLED': '已取消',
  'HIATUS': '休止',
}[s] || s)

const sourceLabel = (s) => ({
  'MANGA': '漫画',
  'LIGHT_NOVEL': '轻小说',
  'NOVEL': '小说',
  'ORIGINAL': '原创',
  'VISUAL_NOVEL': 'galgame',
  'VIDEO_GAME': '游戏',
  'DOUJINSHI': '同人',
  'ANIME': '动画',
  'WEB_NOVEL': '网络小说',
  'LIVE_ACTION': '真人',
  'OTHER': '其他',
}[s] || s)

const seasonLabel = (s) => ({
  'WINTER': '冬', 'SPRING': '春', 'SUMMER': '夏', 'FALL': '秋',
}[s] || s || '')

const relationLabel = (r) => ({
  'SEQUEL': '续作',
  'PREQUEL': '前作',
  'PARENT': '本传',
  'SIDE_STORY': '外传',
  'SUMMARY': '总集篇',
  'ALTERNATIVE': '另版',
  'SPIN_OFF': '衍生',
  'ADAPTATION': '改编自',
  'CHARACTER': '角色',
  'OTHER': '其他',
}[r] || r || '')

const load = async () => {
  loading.value = true
  data.value = null
  try {
    const id = Number(route.params.anilistId)
    const res = await discoverApi.anilistDetail(id)
    data.value = res.data
  } catch (e) {
    console.error('AniList 详情加载失败', e)
  } finally {
    loading.value = false
  }
}

const searching = ref(false)
const searchTorrents = async () => {
  if (!data.value || searching.value) return
  searching.value = true
  try {
    // AniList 番剧种子优先 romaji（拉丁字母，PT 站归档标准），其次 english
    const d = data.value
    const name = d.title_romaji || d.title_english || d.title_native || ''
    const yr = d.season_year
    const q = yr ? `${name} ${yr}` : name
    // await router.push 让 spin 持续到路由切换完成，视觉反馈更明确
    await router.push({
      path: '/resourcesearch',
      query: { q, type: 'tv' },
    })
  } finally {
    searching.value = false
  }
}

const goAniList = (id) => {
  if (!id) return
  router.push({ name: 'DiscoverAniListDetail', params: { anilistId: id } })
}

watch(() => route.fullPath, () => {
  if (route.name === 'DiscoverAniListDetail') load()
})

onMounted(load)
</script>

<style lang="scss" scoped>
@use '@/styles/discover-detail.scss' as *;
@include discover-detail-styles;
</style>

<template>
  <span
    v-if="hasAny"
    class="ratings-badges"
    :class="{ compact, full: !compact, 'dir-column': direction === 'column' }"
  >
    <!-- TMDB（list 写入的 vote_average）-->
    <span v-if="show('tmdb') && rating.tmdb_rating != null" class="badge tmdb"
          :title="`TMDB${rating.tmdb_vote_count ? '（' + rating.tmdb_vote_count.toLocaleString() + ' 票）' : ''}`">
      <img :src="tmdbIcon" alt="TMDB" class="src-img" />
      <span class="val">{{ rating.tmdb_rating.toFixed(1) }}</span>
    </span>
    <!-- IMDb -->
    <span v-if="show('imdb') && rating.imdb_rating != null" class="badge imdb" :title="imdbTitle">
      <img :src="imdbIcon" alt="IMDb" class="src-img" />
      <span class="val">{{ rating.imdb_rating.toFixed(1) }}</span>
    </span>
    <!-- Rotten Tomatoes 影评人（原值是 0-100，归一到 10 分制）-->
    <span v-if="show('rt') && rating.rt_critic != null" class="badge rt" title="Rotten Tomatoes 影评人">
      <span class="src">🍅</span>
      <span class="val">{{ (rating.rt_critic / 10).toFixed(1) }}</span>
    </span>
    <!-- Rotten Tomatoes 观众（仅 full 模式）-->
    <span v-if="!compact && show('rt') && rating.rt_audience != null" class="badge rt-aud" title="RT 观众">
      <span class="src">🍿</span>
      <span class="val">{{ (rating.rt_audience / 10).toFixed(1) }}</span>
    </span>
    <!-- Metacritic（原值 0-100，归一到 10 分制）-->
    <span v-if="show('mc') && rating.metacritic != null" class="badge mc" title="Metacritic">
      <span class="src">MC</span>
      <span class="val">{{ (rating.metacritic / 10).toFixed(1) }}</span>
    </span>
    <!-- 豆瓣 -->
    <span v-if="show('douban') && rating.douban_rating != null" class="badge douban" :title="doubanTitle">
      <img :src="doubanIcon" alt="豆瓣" class="src-img" />
      <span class="val">{{ rating.douban_rating.toFixed(1) }}</span>
    </span>
    <!-- Trakt（仅 full）-->
    <span v-if="!compact && show('trakt') && rating.trakt_rating != null" class="badge trakt" title="Trakt">
      <span class="src">Trakt</span>
      <span class="val">{{ rating.trakt_rating.toFixed(1) }}</span>
    </span>
    <!-- Letterboxd（仅 full）-->
    <span v-if="!compact && show('letterboxd') && rating.letterboxd_rating != null" class="badge letterboxd" title="Letterboxd">
      <span class="src">Lb</span>
      <span class="val">{{ rating.letterboxd_rating.toFixed(1) }}</span>
    </span>
    <!-- MDB 综合分（原值 0-100，归一到 10 分制；仅 full）-->
    <span v-if="!compact && show('aggregate') && rating.aggregate_score != null" class="badge aggregate" title="MDB List 综合分">
      <span class="src">MDB</span>
      <span class="val">{{ (rating.aggregate_score / 10).toFixed(1) }}</span>
    </span>
    <!-- 拉取中提示 -->
    <span v-if="!compact && pendingHint" class="badge pending" :title="pendingHint">
      <el-icon class="loading-icon"><Loading /></el-icon>
      <span class="val">拉取中</span>
    </span>
    <span v-if="compact && pendingHint" class="pending-dot" :title="pendingHint"></span>
  </span>
  <!-- 完全没有数据时的占位 —— compact 模式也展示一个 badge，让用户知道"展开"是有效的 -->
  <span v-else class="ratings-badges empty" :class="{ compact, full: !compact }">
    <span v-if="pendingHint" class="badge pending" :title="pendingHint">
      <el-icon class="loading-icon"><Loading /></el-icon>
      <span class="val">拉取中</span>
    </span>
    <span v-else class="badge no-data" title="该条目暂无其他评分源数据">
      <span class="val">暂无评分</span>
    </span>
  </span>
</template>

<script setup>
import { computed } from 'vue'
import { Loading } from '@element-plus/icons-vue'
import imdbIcon from '@/assets/icons/imdb.svg'
import tmdbIcon from '@/assets/icons/tmdb.svg'
import doubanIcon from '@/assets/icons/douban.svg'

const props = defineProps({
  // 评分对象。null 视为还没加载
  rating: { type: Object, default: null },
  // compact 模式：精简显示，适合卡片/列表
  compact: { type: Boolean, default: false },
  // 排除的源（避免在"展开"场景里重复显示主源徽章）
  excludeSource: { type: String, default: '' },
  // 排列方向：'row'（默认横排）/ 'column'（竖排，海报右上角下方展开时用）
  direction: { type: String, default: 'row' },
})

const r = computed(() => props.rating || {})
const show = (key) => props.excludeSource !== key

const hasAny = computed(() => {
  const x = r.value
  const items = [
    ['tmdb', x.tmdb_rating],
    ['imdb', x.imdb_rating],
    ['rt', x.rt_critic],
    ['rt', x.rt_audience],
    ['mc', x.metacritic],
    ['douban', x.douban_rating],
    ['trakt', x.trakt_rating],
    ['letterboxd', x.letterboxd_rating],
    ['aggregate', x.aggregate_score],
  ]
  return items.some(([key, v]) => v != null && show(key))
})

const pendingHint = computed(() => {
  const x = r.value
  // 至少有一个 ID 入口（tmdb_id 或 request_douban_id）才显示 pending
  // 避免完全空 rating 对象误显示拉取中
  if (!x.tmdb_id && !x.request_douban_id) return null
  const parts = []
  if (x.mdblist_status && x.mdblist_status !== 'fresh') parts.push('MDB List')
  if (x.douban_status && x.douban_status !== 'fresh') parts.push('豆瓣')
  return parts.length ? `正在拉取 ${parts.join(' / ')}` : null
})

const imdbTitle = computed(() => {
  const x = r.value
  if (!x.imdb_votes) return 'IMDb'
  return `IMDb（${x.imdb_votes.toLocaleString()} 票）`
})

const doubanTitle = computed(() => {
  const x = r.value
  if (!x.douban_votes) return '豆瓣'
  return `豆瓣（${x.douban_votes.toLocaleString()} 票）`
})
</script>

<style lang="scss" scoped>
.ratings-badges {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;

  // 竖排模式：海报右上角"5 角星下方展开"场景用；徽章一行一个
  &.dir-column {
    display: flex;
    flex-direction: column;
    flex-wrap: nowrap;
    align-items: stretch;
    gap: 3px;
  }

  .badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 11px;
    line-height: 1.4;
    font-weight: 500;
    background: var(--el-fill-color-lighter, #f5f7fa);
    color: var(--el-text-color-primary, #303133);
    border: 1px solid var(--el-border-color-lighter, #ebeef5);
    min-width: 56px;
    box-sizing: border-box;

    .src {
      opacity: 0.75;
      font-weight: 400;
      font-size: 10px;
    }
    .src-img {
      height: 12px;
      width: auto;
      display: block;
      flex-shrink: 0;
    }
    .val {
      font-weight: 700;
    }

    &.tmdb { background: #0d253f; color: #fff; border-color: #0d253f; }
    &.imdb { background: #f5c518; color: #000; border-color: #d4a811; }
    &.rt { background: #fef4ec; color: #c8341c; border-color: #f4ddc7; }
    &.rt-aud { background: #fff7e6; color: #b78a00; border-color: #f5e3b6; }
    &.mc { background: #e8f4ff; color: #0067a3; border-color: #cbe6fc; }
    &.douban { background: #007722; color: #fff; border-color: #00691e; }
    &.trakt { background: #f3eaff; color: #6a4eb1; border-color: #dccdee; }
    &.letterboxd { background: #e6f6f0; color: #00674c; border-color: #b8dccc; }
    &.aggregate { background: #2c3e50; color: #fff; border-color: #1f2d3a; }
    &.pending { background: #f5f5f5; color: #909399; border-color: #e4e7ed; }
    &.no-data { background: #f5f5f5; color: #909399; border-color: #e4e7ed; }
  }

  .pending-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #c0c4cc;
    animation: pulse 1.4s ease-in-out infinite;
  }

  .loading-icon {
    animation: rotating 2s linear infinite;
  }

  &.compact .badge {
    padding: 1px 5px;
    font-size: 10px;
    min-width: 50px;

    .src-img { height: 10px; }
  }

  &.full .badge {
    padding: 3px 8px;
    font-size: 12px;
    min-width: 60px;

    .src-img { height: 14px; }
  }

  &.empty {
    .muted {
      color: var(--el-text-color-placeholder, #c0c4cc);
      font-size: 12px;
    }
  }
}

@keyframes rotating {
  to { transform: rotate(360deg); }
}

@keyframes pulse {
  0%, 100% { opacity: 0.35; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1.1); }
}
</style>

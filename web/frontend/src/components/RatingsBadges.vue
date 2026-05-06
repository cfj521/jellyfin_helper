<template>
  <span v-if="hasAny" class="ratings-badges" :class="{ compact, full: !compact }">
    <!-- IMDb -->
    <span v-if="rating.imdb_rating != null" class="badge imdb" :title="imdbTitle">
      <span class="src">IMDb</span>
      <span class="val">{{ rating.imdb_rating.toFixed(1) }}</span>
    </span>
    <!-- Rotten Tomatoes 影评人 -->
    <span v-if="rating.rt_critic != null" class="badge rt" title="Rotten Tomatoes 影评人">
      <span class="src">🍅</span>
      <span class="val">{{ rating.rt_critic }}%</span>
    </span>
    <!-- Rotten Tomatoes 观众（full 模式才展示，避免列表太挤）-->
    <span v-if="!compact && rating.rt_audience != null" class="badge rt-aud" title="RT 观众">
      <span class="src">🍿</span>
      <span class="val">{{ rating.rt_audience }}%</span>
    </span>
    <!-- Metacritic -->
    <span v-if="rating.metacritic != null" class="badge mc" title="Metacritic">
      <span class="src">MC</span>
      <span class="val">{{ rating.metacritic }}</span>
    </span>
    <!-- 豆瓣 -->
    <span v-if="rating.douban_rating != null" class="badge douban" :title="doubanTitle">
      <span class="src">豆瓣</span>
      <span class="val">{{ rating.douban_rating.toFixed(1) }}</span>
    </span>
    <!-- Trakt（仅 full）-->
    <span v-if="!compact && rating.trakt_rating != null" class="badge trakt" title="Trakt">
      <span class="src">Trakt</span>
      <span class="val">{{ rating.trakt_rating.toFixed(1) }}</span>
    </span>
    <!-- Letterboxd（仅 full）-->
    <span v-if="!compact && rating.letterboxd_rating != null" class="badge letterboxd" title="Letterboxd（5 分制）">
      <span class="src">Lb</span>
      <span class="val">{{ rating.letterboxd_rating.toFixed(1) }}</span>
    </span>
    <!-- 拉取中提示（豆瓣首次访问会延迟） -->
    <span v-if="!compact && pendingHint" class="badge pending" :title="pendingHint">
      <el-icon class="loading-icon"><Loading /></el-icon>
      <span class="val">拉取中</span>
    </span>
  </span>
  <!-- 完全没有数据时的占位 -->
  <span v-else-if="!compact" class="ratings-badges empty">
    <span v-if="pendingHint" class="badge pending" :title="pendingHint">
      <el-icon class="loading-icon"><Loading /></el-icon>
      <span class="val">{{ pendingHint }}</span>
    </span>
    <span v-else class="muted">暂无评分</span>
  </span>
</template>

<script setup>
import { computed } from 'vue'
import { Loading } from '@element-plus/icons-vue'

const props = defineProps({
  // 评分对象（来自 /api/ratings 或 batch 接口的单条 rating）。null 视为还没加载
  rating: { type: Object, default: null },
  // compact 模式：只显示 4 大主源（IMDb / RT 影评 / MC / 豆瓣），适合卡片/列表
  compact: { type: Boolean, default: false },
})

const r = computed(() => props.rating || {})

const hasAny = computed(() => {
  const x = r.value
  return [
    x.imdb_rating, x.rt_critic, x.rt_audience, x.metacritic,
    x.trakt_rating, x.letterboxd_rating, x.douban_rating,
  ].some((v) => v != null)
})

// 拉取状态提示（仅 full 模式展示）：MDB List 同步即取，豆瓣异步
const pendingHint = computed(() => {
  const x = r.value
  if (!x.tmdb_id) return null
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

  .badge {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 11px;
    line-height: 1.4;
    font-weight: 500;
    background: var(--el-fill-color-lighter, #f5f7fa);
    color: var(--el-text-color-primary, #303133);
    border: 1px solid var(--el-border-color-lighter, #ebeef5);

    .src {
      opacity: 0.65;
      font-weight: 400;
      font-size: 10px;
    }
    .val {
      font-weight: 600;
    }

    // 各家配色（淡背景）
    &.imdb { background: #f5c518; color: #000; border-color: #d4a811; }
    &.rt   { background: #fef4ec; color: #c8341c; border-color: #f4ddc7; }
    &.rt-aud { background: #fff7e6; color: #b78a00; border-color: #f5e3b6; }
    &.mc   { background: #e8f4ff; color: #0067a3; border-color: #cbe6fc; }
    &.douban { background: #ecfae0; color: #2c8c1d; border-color: #cbeab5; }
    &.trakt { background: #f3eaff; color: #6a4eb1; border-color: #dccdee; }
    &.letterboxd { background: #e6f6f0; color: #00674c; border-color: #b8dccc; }
    &.pending { background: #f5f5f5; color: #909399; border-color: #e4e7ed; }
  }

  .loading-icon {
    animation: rotating 2s linear infinite;
  }

  &.compact .badge {
    padding: 1px 5px;
    font-size: 10px;
  }

  &.full .badge {
    padding: 3px 8px;
    font-size: 12px;
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
</style>

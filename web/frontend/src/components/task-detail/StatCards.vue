<template>
  <div class="stat-cards">
    <div
      v-for="(c, idx) in cards"
      :key="idx"
      class="stat-card"
      :style="{ borderLeftColor: c.color || '#6366f1' }"
    >
      <div class="stat-label">{{ c.label }}</div>
      <div class="stat-value" :style="{ color: c.color || '#1e293b' }">
        {{ c.value }}
        <span v-if="c.suffix" class="stat-suffix">{{ c.suffix }}</span>
      </div>
      <div v-if="c.tip" class="stat-tip">{{ c.tip }}</div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  /**
   * cards: [{ label, value, color?, suffix?, tip? }, ...]
   */
  cards: { type: Array, required: true },
})
</script>

<style lang="scss" scoped>
.stat-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.stat-card {
  padding: 12px 16px;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-left: 4px solid;
  border-radius: 6px;

  .stat-label {
    font-size: 12px;
    color: #64748b;
    margin-bottom: 6px;
  }
  .stat-value {
    font-size: 24px;
    font-weight: 600;
    line-height: 1.2;
  }
  .stat-suffix {
    font-size: 13px;
    font-weight: normal;
    color: #94a3b8;
    margin-left: 4px;
  }
  .stat-tip {
    margin-top: 4px;
    font-size: 12px;
    color: #94a3b8;
  }
}
</style>

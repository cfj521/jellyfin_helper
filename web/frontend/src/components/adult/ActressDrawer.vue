<template>
  <el-drawer
    v-model="visible"
    direction="rtl"
    :size="500"
    :with-header="false"
    :close-on-click-modal="true"
    @closed="onClosed"
  >
    <div class="actress-drawer" v-loading="loading">
      <!-- 顶部：关闭 -->
      <div class="drawer-header">
        <h3>{{ headerTitle }}</h3>
        <el-button text @click="visible = false">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>

      <!-- 未解析状态：仅显示 query 名 + 提示 -->
      <div v-if="state === 'unresolved'" class="unresolved">
        <el-empty :image-size="80" description="女优库里还没这位">
          <p class="hint">
            "{{ queryName }}" 还没被解析过。
            可以去配置页打开「女优档案库」继续构建，
            或等"扫描并修复识别错误"任务跑完。
          </p>
        </el-empty>
      </div>

      <!-- 解析中 -->
      <div v-else-if="state === 'loading'" class="loading-block">
        <el-icon class="spin"><Loading /></el-icon>
        加载中…
      </div>

      <!-- 完整数据 -->
      <template v-else-if="state === 'ready' && data">
        <!-- 头部信息卡 -->
        <div class="actress-card">
          <el-image
            v-if="data.actress.avatar_url"
            :src="data.actress.avatar_url"
            fit="cover"
            class="avatar"
          >
            <template #error>
              <div class="avatar-fallback">{{ data.actress.jp_name?.[0] || '?' }}</div>
            </template>
          </el-image>
          <div v-else class="avatar avatar-fallback">{{ data.actress.jp_name?.[0] || '?' }}</div>

          <div class="info">
            <div class="primary-name">{{ data.actress.jp_name || data.actress.zh_name }}</div>
            <div v-if="data.actress.zh_name && data.actress.zh_name !== data.actress.jp_name" class="sub-name">
              {{ data.actress.zh_name }}
            </div>
            <div v-if="data.actress.en_name" class="sub-name en">{{ data.actress.en_name }}</div>
            <div class="meta">
              <span v-if="data.actress.age">{{ data.actress.age }} 岁</span>
              <span v-if="data.actress.debut_date">出道 {{ data.actress.debut_date }}</span>
            </div>
          </div>
        </div>

        <div class="aliases-row" v-if="data.actress.aliases?.length">
          <span class="row-label">别名</span>
          <el-tag
            v-for="al in data.actress.aliases"
            :key="al"
            size="small"
            type="info"
            effect="plain"
            class="alias-chip"
          >{{ al }}</el-tag>
        </div>

        <div class="links-row" v-if="data.actress.javdb_id">
          <span class="row-label">数据源</span>
          <a
            :href="`https://javdb.com/actors/${data.actress.javdb_id}`"
            target="_blank"
            rel="noopener"
            class="ext-link"
          >
            <el-icon><Link /></el-icon>
            javdb / {{ data.actress.javdb_id }}
            <el-icon class="ext-icon"><Top /></el-icon>
          </a>
        </div>

        <!-- 作品列表 -->
        <div class="works-section">
          <div class="works-header">
            出演作品
            <el-tag size="small" type="info" effect="plain">{{ data.works.length }}</el-tag>
          </div>
          <div v-if="!data.works.length" class="empty-works">
            <el-empty :image-size="60" description="本库内还未识别到她的作品" />
          </div>
          <div v-else class="works-list">
            <div
              v-for="w in data.works"
              :key="w.id"
              class="work-row"
              @click="$emit('open-work', w)"
            >
              <el-image
                v-if="w.cover_url"
                :src="`/api/adult/items/${w.id}/poster`"
                fit="cover"
                lazy
                class="work-cover"
              >
                <template #error>
                  <div class="work-cover-ph">{{ w.code }}</div>
                </template>
              </el-image>
              <div v-else class="work-cover work-cover-ph">{{ w.code }}</div>
              <div class="work-info">
                <div class="work-code">{{ w.code }}</div>
                <div class="work-title">{{ w.title || '(未刮)' }}</div>
                <div class="work-meta">
                  <span v-if="w.release_date">{{ w.release_date }}</span>
                  <span v-if="w.studio">· {{ w.studio }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- 出错 -->
      <div v-else-if="state === 'error'" class="error-block">
        <el-alert type="error" :title="errMsg" show-icon :closable="false" />
      </div>
    </div>
  </el-drawer>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { Close, Link, Top, Loading } from '@element-plus/icons-vue'
import { adultApi } from '@/api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // payload: { name, resolved: { id, jp_name, ... } | null }
  payload: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'open-work'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const queryName = computed(() => props.payload?.name || '')
const state = ref('idle')   // idle / unresolved / loading / ready / error
const data = ref(null)
const errMsg = ref('')
const loading = ref(false)

const headerTitle = computed(() => {
  if (state.value === 'unresolved') return queryName.value || '未解析'
  if (data.value) {
    return data.value.actress.zh_name || data.value.actress.jp_name || '女优'
  }
  return '女优'
})

const load = async () => {
  data.value = null
  errMsg.value = ''
  if (!props.payload) return
  if (!props.payload.resolved) {
    state.value = 'unresolved'
    return
  }
  state.value = 'loading'
  loading.value = true
  try {
    const r = await adultApi.actressWorks(props.payload.resolved.id)
    data.value = r.data
    state.value = 'ready'
  } catch (e) {
    state.value = 'error'
    errMsg.value = e.response?.data?.detail || e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

watch(() => [props.modelValue, props.payload], ([v]) => {
  if (v) load()
})

const onClosed = () => {
  state.value = 'idle'
  data.value = null
  errMsg.value = ''
}
</script>

<style lang="scss" scoped>
.actress-drawer {
  padding: 0 20px 20px;
  min-height: 200px;
}

.drawer-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid var(--jt-card-border);
  margin-bottom: 16px;

  h3 {
    margin: 0;
    font-size: 16px;
    color: var(--jt-text-primary);
  }
}

.unresolved, .loading-block, .error-block {
  padding: 30px 0;
  text-align: center;
  color: var(--jt-text-muted);

  .hint {
    font-size: 13px;
    line-height: 1.5;
    color: var(--jt-text-secondary);
    margin: 4px 0 0;
  }
}

.loading-block {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 13px;
  .spin { animation: spin 1.4s linear infinite; color: var(--jt-brand); }
}
@keyframes spin { to { transform: rotate(360deg); } }

.actress-card {
  display: flex;
  gap: 14px;
  padding: 14px;
  border: 1px solid var(--jt-card-border);
  border-radius: 8px;
  background: var(--jt-fill-light);

  .avatar {
    width: 96px;
    height: 96px;
    flex-shrink: 0;
    border-radius: 50%;
    overflow: hidden;
    background: var(--jt-card-border);
  }
  .avatar-fallback {
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, #cbd5e1 0%, #94a3b8 100%);
    color: #fff;
    font-size: 36px;
    font-weight: 600;
  }

  .info {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  .primary-name { font-size: 18px; font-weight: 600; color: var(--jt-text-primary); }
  .sub-name { font-size: 13px; color: var(--jt-text-secondary); }
  .sub-name.en { font-style: italic; }
  .meta {
    margin-top: 4px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    font-size: 12px;
    color: var(--jt-text-muted);
  }
}

.aliases-row, .links-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 10px 4px;

  .row-label {
    font-size: 12px;
    color: var(--jt-text-muted);
    margin-right: 6px;
    flex-shrink: 0;
  }
  .alias-chip { font-size: 12px; }
}

.ext-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--jt-brand);
  font-size: 13px;
  text-decoration: none;

  &:hover { text-decoration: underline; }

  .ext-icon { font-size: 10px; transform: rotate(45deg); opacity: 0.7; }
}

.works-section {
  margin-top: 16px;

  .works-header {
    font-size: 13px;
    color: var(--jt-text-regular);
    font-weight: 500;
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
    padding: 0 4px;
  }
  .works-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
}

.work-row {
  display: flex;
  gap: 10px;
  padding: 8px;
  border: 1px solid var(--jt-card-border);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;

  &:hover {
    border-color: #c7d2fe;
    box-shadow: 0 1px 3px rgba(var(--jt-brand-rgb), 0.1);
  }
}

.work-cover {
  width: 96px;
  aspect-ratio: 16 / 9;
  border-radius: 4px;
  flex-shrink: 0;
  background: var(--jt-fill-light);
}
.work-cover-ph {
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 11px;
  font-weight: 600;
  color: var(--jt-text-secondary);
  background: var(--jt-fill-light);
}

.work-info {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-width: 0;
  flex: 1;

  .work-code {
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 11px;
    color: #0369a1;
    font-weight: 600;
  }
  .work-title {
    font-size: 13px;
    color: var(--jt-text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .work-meta {
    font-size: 11px;
    color: var(--jt-text-muted);
    margin-top: 2px;
  }
}
</style>

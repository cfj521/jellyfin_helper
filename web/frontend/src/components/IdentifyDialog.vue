<template>
  <el-dialog
    v-model="visible"
    title="重新识别"
    width="780"
    :close-on-click-modal="false"
    @closed="onClosed"
  >
    <div v-if="item" class="identify-dialog">
      <!-- 当前匹配信息（用户对照看错配方向）-->
      <div class="current-info">
        <div class="info-label">当前 Jellyfin 匹配：</div>
        <div class="info-row">
          <span class="info-name">{{ item.name }}</span>
          <span v-if="item.year" class="info-year">({{ item.year }})</span>
          <a
            v-if="item.tmdb_id"
            :href="currentTmdbUrl"
            target="_blank"
            rel="noopener noreferrer"
            class="info-tmdb"
          >
            TMDB#{{ item.tmdb_id }}
          </a>
        </div>
        <div v-if="item.path" class="info-path">{{ item.path }}</div>
      </div>

      <!-- 搜索表单 -->
      <el-form :model="form" label-width="100px" size="small">
        <el-form-item label="标题">
          <el-input v-model="form.name" placeholder="电影/剧集英文或中文名" clearable />
        </el-form-item>
        <el-form-item label="年份">
          <el-input-number v-model="form.year" :min="1900" :max="2100" :controls="false" />
          <span class="hint">可选，能消歧效果好</span>
        </el-form-item>
        <el-form-item label="TMDB ID">
          <el-input v-model="form.tmdb_id" placeholder="可选，知道 ID 直接锁定（最准）" clearable />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="searching" @click="search">
            <el-icon><Search /></el-icon>
            搜索候选
          </el-button>
          <el-button v-if="results.length" link @click="results = []">清空结果</el-button>
        </el-form-item>
      </el-form>

      <!-- 候选列表 -->
      <div v-if="results.length" class="candidates">
        <div class="candidates-header">
          找到 {{ results.length }} 个候选 — 选择正确的那个：
        </div>
        <el-radio-group v-model="selectedIdx" class="candidate-list">
          <label
            v-for="(c, idx) in results"
            :key="idx"
            class="candidate-card"
            :class="{ active: selectedIdx === idx }"
          >
            <el-radio :label="idx" class="candidate-radio" />
            <el-image
              v-if="c.ImageUrl"
              :src="c.ImageUrl"
              fit="cover"
              lazy
              style="width: 60px; height: 90px; border-radius: 3px; flex-shrink: 0"
            >
              <template #error>
                <div class="poster-placeholder">无图</div>
              </template>
            </el-image>
            <div v-else class="poster-placeholder big">无图</div>
            <div class="candidate-meta">
              <div class="candidate-title">
                {{ c.Name }}
                <span v-if="c.ProductionYear" class="candidate-year">
                  ({{ c.ProductionYear }})
                </span>
              </div>
              <div class="candidate-ids">
                <a
                  v-if="c.ProviderIds?.Tmdb"
                  :href="`https://www.themoviedb.org/${tmdbType}/${c.ProviderIds.Tmdb}`"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="id-link"
                  @click.stop
                >
                  TMDB#{{ c.ProviderIds.Tmdb }}
                </a>
                <a
                  v-if="c.ProviderIds?.Imdb"
                  :href="`https://www.imdb.com/title/${c.ProviderIds.Imdb}`"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="id-link"
                  @click.stop
                >
                  IMDB {{ c.ProviderIds.Imdb }}
                </a>
                <span v-if="c.SearchProviderName" class="provider-tag">
                  {{ c.SearchProviderName }}
                </span>
              </div>
              <div v-if="c.Overview" class="candidate-overview">{{ c.Overview }}</div>
            </div>
          </label>
        </el-radio-group>

        <div class="apply-options">
          <el-checkbox v-model="replaceAllImages">同时覆盖海报/背景图</el-checkbox>
          <span class="hint">关闭则保留现有海报，仅更新文本元数据</span>
        </div>
      </div>

      <el-empty
        v-else-if="searched && !searching"
        description="无候选 — 尝试调整标题或填 TMDB ID"
        :image-size="80"
      />
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="applying"
        :disabled="selectedIdx === null"
        @click="apply"
      >
        应用所选并刷新元数据
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { jellyfinApi } from '@/api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  item: { type: Object, default: null },  // 当前选中的 row 对象
})
const emit = defineEmits(['update:modelValue', 'applied'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

// 表单：默认用当前条目的标题/年份预填
const form = ref({ name: '', year: null, tmdb_id: '' })

const results = ref([])
const selectedIdx = ref(null)
const searching = ref(false)
const applying = ref(false)
const searched = ref(false)
const replaceAllImages = ref(true)

// dialog 打开时预填默认值：
//   优先用从路径自动提取的 suggested_title / suggested_year（避免被错误的 Jellyfin 名称误导）
//   回退到 Jellyfin 的 name / year
watch(() => props.modelValue, (val) => {
  if (val && props.item) {
    form.value = {
      name: props.item.suggested_title || props.item.name || '',
      year: props.item.suggested_year || props.item.year || null,
      tmdb_id: '',  // 不预填 TMDB ID（避免锁定到错的那个）
    }
    results.value = []
    selectedIdx.value = null
    searched.value = false
  }
})

const tmdbType = computed(() =>
  props.item?.type === 'Series' ? 'tv' : 'movie',
)

const currentTmdbUrl = computed(() => {
  if (!props.item?.tmdb_id) return ''
  return `https://www.themoviedb.org/${tmdbType.value}/${props.item.tmdb_id}`
})

const search = async () => {
  if (!form.value.name && !form.value.tmdb_id) {
    ElMessage.warning('请至少填一个：标题 或 TMDB ID')
    return
  }
  searching.value = true
  searched.value = true
  selectedIdx.value = null
  try {
    // Folder 类型在后端会自动按 Movie 处理
    const itemType = props.item?.type === 'Series' ? 'Series' : 'Movie'
    const payload = {
      item_type: itemType,
      name: form.value.name || null,
      year: form.value.year || null,
      tmdb_id: form.value.tmdb_id || null,
    }
    const res = await jellyfinApi.identifySearch(props.item.id, payload)
    results.value = res.data.candidates || []
    if (results.value.length === 0) {
      ElMessage.info('未找到候选；试试改标题或填 TMDB ID')
    }
  } catch (e) {
    console.error(e)
    ElMessage.error(e.response?.data?.detail || e.message || '搜索失败')
  } finally {
    searching.value = false
  }
}

const apply = async () => {
  if (selectedIdx.value === null) return
  const candidate = results.value[selectedIdx.value]
  applying.value = true
  try {
    await jellyfinApi.identifyApply(props.item.id, {
      candidate,
      replace_all_images: replaceAllImages.value,
    })
    ElMessage.success(`已应用 "${candidate.Name}"，Jellyfin 正在异步刷新元数据`)
    emit('applied', { itemId: props.item.id, candidate })
    visible.value = false
  } catch (e) {
    console.error(e)
    ElMessage.error(e.response?.data?.detail || e.message || '应用失败')
  } finally {
    applying.value = false
  }
}

const onClosed = () => {
  // 关闭时清状态
  results.value = []
  selectedIdx.value = null
  searched.value = false
}
</script>

<style lang="scss" scoped>
.identify-dialog {
  .hint {
    color: #94a3b8;
    font-size: 12px;
    margin-left: 8px;
  }
}

.current-info {
  margin-bottom: 14px;
  padding: 10px 12px;
  background: #f8fafc;
  border-left: 3px solid #6366f1;
  border-radius: 4px;
  font-size: 13px;

  .info-label {
    color: #64748b;
    margin-bottom: 4px;
    font-size: 12px;
  }

  .info-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;

    .info-name {
      font-weight: 600;
      color: #0f172a;
    }

    .info-year {
      color: #64748b;
    }

    .info-tmdb {
      color: #0ea5e9;
      text-decoration: none;
      font-size: 12px;
      font-family: ui-monospace, monospace;

      &:hover { text-decoration: underline; }
    }
  }

  .info-path {
    color: #94a3b8;
    font-size: 11px;
    margin-top: 4px;
    word-break: break-all;
  }
}

.candidates {
  margin-top: 16px;

  .candidates-header {
    font-size: 13px;
    color: #475569;
    margin-bottom: 8px;
  }

  .candidate-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    width: 100%;
  }

  .candidate-card {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px;
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.15s;

    &:hover {
      border-color: #c7d2fe;
      background: #f5f3ff;
    }

    &.active {
      border-color: #6366f1;
      background: #eef2ff;
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15);
    }

    .candidate-radio {
      margin: 0;
      flex-shrink: 0;
    }

    .poster-placeholder {
      width: 60px;
      height: 90px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #f1f5f9;
      color: #94a3b8;
      font-size: 11px;
      border-radius: 3px;
      flex-shrink: 0;
    }

    .candidate-meta {
      flex: 1;
      min-width: 0;

      .candidate-title {
        font-size: 14px;
        font-weight: 600;
        color: #0f172a;
      }

      .candidate-year {
        color: #64748b;
        font-weight: 400;
        margin-left: 4px;
      }

      .candidate-ids {
        display: flex;
        gap: 10px;
        margin: 4px 0;
        font-size: 12px;
        flex-wrap: wrap;

        .id-link {
          color: #0ea5e9;
          text-decoration: none;
          font-family: ui-monospace, monospace;

          &:hover { text-decoration: underline; }
        }

        .provider-tag {
          color: #94a3b8;
          font-size: 11px;
        }
      }

      .candidate-overview {
        color: #64748b;
        font-size: 12px;
        line-height: 1.5;
        max-height: 4.5em;
        overflow: hidden;
        text-overflow: ellipsis;
        display: -webkit-box;
        -webkit-line-clamp: 3;
        -webkit-box-orient: vertical;
      }
    }
  }

  .apply-options {
    display: flex;
    align-items: center;
    margin-top: 12px;
    padding: 8px 0;
    border-top: 1px solid #f1f5f9;
  }
}
</style>

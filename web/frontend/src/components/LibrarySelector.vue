<template>
  <div class="library-selector">
    <el-radio-group v-model="mode" size="small" class="mode-switch" @change="onModeChange">
      <el-radio-button label="library">从 Jellyfin 库选</el-radio-button>
      <el-radio-button label="manual">手动输路径</el-radio-button>
    </el-radio-group>

    <!-- 库选择模式 -->
    <div v-if="mode === 'library'" class="library-mode">
      <el-select
        v-model="selectedLibraryId"
        :placeholder="loading ? '加载库列表...' : '选择 Jellyfin 媒体库'"
        :loading="loading"
        clearable
        filterable
        style="width: 100%"
        @change="onLibraryChange"
      >
        <el-option
          v-for="lib in filteredLibraries"
          :key="lib.id"
          :label="lib.name"
          :value="lib.id"
        >
          <div class="lib-option">
            <span class="lib-name">{{ lib.name }}</span>
            <el-tag size="small" :type="typeTagType(lib.collection_type)">
              {{ typeLabel(lib.collection_type) }}
            </el-tag>
            <el-tag v-if="!lib.all_accessible" type="danger" size="small">路径不可访问</el-tag>
          </div>
        </el-option>
      </el-select>

      <div v-if="selectedLibrary" class="lib-detail">
        <div v-if="selectedLibrary.locations.length > 1" class="multi-paths">
          <el-radio-group v-model="selectedLocationIndex" @change="onLocationChange" size="small">
            <el-radio
              v-for="(loc, idx) in selectedLibrary.locations"
              :key="idx"
              :label="idx"
              :disabled="selectedLibrary.locations_status && !selectedLibrary.locations_status[idx].accessible"
            >
              {{ loc }}
              <el-tag
                v-if="selectedLibrary.locations_status && !selectedLibrary.locations_status[idx].accessible"
                type="danger"
                size="small"
              >不可访问</el-tag>
            </el-radio>
          </el-radio-group>
          <div v-if="allowAllLocations" class="all-toggle">
            <el-checkbox v-model="useAllLocations" @change="onAllToggle">
              使用本库的全部 {{ selectedLibrary.locations.length }} 个路径
            </el-checkbox>
          </div>
        </div>
        <div v-else class="single-path">
          <el-icon><Folder /></el-icon>
          {{ selectedLibrary.locations[0] }}
          <el-tag v-if="selectedLibrary.locations_status && !selectedLibrary.locations_status[0].accessible" type="danger" size="small">
            路径不可访问
          </el-tag>
        </div>
      </div>

      <el-alert
        v-if="loadError"
        type="warning"
        :closable="false"
        show-icon
        :title="loadError"
        style="margin-top: 8px"
      />
    </div>

    <!-- 手动模式 -->
    <div v-else class="manual-mode">
      <el-input v-model="manualPath" :placeholder="placeholder" clearable @input="onManualInput">
        <template v-if="enableBrowser" #append>
          <el-button @click="$emit('open-browser')">
            <el-icon><FolderOpened /></el-icon>
            浏览
          </el-button>
        </template>
      </el-input>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { Folder, FolderOpened } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { jellyfinApi } from '@/api'

const props = defineProps({
  modelValue: { type: String, default: '' },          // 单路径模式：当前选中的单条路径
  modelPaths: { type: Array, default: () => [] },     // 多路径模式：当前选中的所有路径
  filterTypes: { type: Array, default: () => [] },    // 仅显示这些 collection_type
  placeholder: { type: String, default: '输入媒体目录路径' },
  enableBrowser: { type: Boolean, default: true },
  allowAllLocations: { type: Boolean, default: true }, // 是否允许"全选库内所有路径"
  defaultMode: { type: String, default: 'library' },   // library | manual
})

const emit = defineEmits(['update:modelValue', 'update:modelPaths', 'change', 'library-selected', 'open-browser'])

const mode = ref(props.defaultMode)
const libraries = ref([])
const loading = ref(false)
const loadError = ref('')
const selectedLibraryId = ref('')
const selectedLocationIndex = ref(0)
const useAllLocations = ref(false)
const manualPath = ref(props.modelValue || '')

const filteredLibraries = computed(() => {
  if (!props.filterTypes.length) return libraries.value
  return libraries.value.filter(l => props.filterTypes.includes(l.collection_type))
})

const selectedLibrary = computed(() =>
  libraries.value.find(l => l.id === selectedLibraryId.value)
)

const loadLibraries = async () => {
  loading.value = true
  loadError.value = ''
  try {
    const res = await jellyfinApi.libraries(true)
    libraries.value = res.data.libraries || []
    if (!libraries.value.length) {
      loadError.value = 'Jellyfin 没有配置任何媒体库'
    }
  } catch (e) {
    loadError.value = '无法连接 Jellyfin（' + (e.response?.data?.detail || e.message) + '）'
    libraries.value = []
  } finally {
    loading.value = false
  }
}

const onModeChange = () => {
  if (mode.value === 'library' && !libraries.value.length) {
    loadLibraries()
  }
  emitChange()
}

const onLibraryChange = () => {
  selectedLocationIndex.value = 0
  useAllLocations.value = false
  emit('library-selected', selectedLibrary.value)
  emitChange()
}

const onLocationChange = () => emitChange()
const onAllToggle = () => emitChange()
const onManualInput = () => emitChange()

const emitChange = () => {
  let single = ''
  let paths = []

  if (mode.value === 'library' && selectedLibrary.value) {
    if (useAllLocations.value) {
      paths = [...selectedLibrary.value.locations]
      single = paths[0] || ''
    } else {
      single = selectedLibrary.value.locations[selectedLocationIndex.value] || ''
      paths = single ? [single] : []
    }
  } else if (mode.value === 'manual') {
    single = manualPath.value || ''
    paths = single ? [single] : []
  }

  emit('update:modelValue', single)
  emit('update:modelPaths', paths)
  emit('change', { mode: mode.value, path: single, paths, library: selectedLibrary.value })
}

// 外部 v-model 改了 → 同步到 manual
watch(() => props.modelValue, (v) => {
  if (mode.value === 'manual') manualPath.value = v || ''
})

onMounted(() => {
  if (mode.value === 'library') loadLibraries()
})

// 暴露给父组件，供"刷新本库"按钮等使用
defineExpose({
  reload: loadLibraries,
  selectedLibrary,
})

const typeLabel = (t) => ({
  movies: '电影', tvshows: '剧集', music: '音乐', musicvideos: '音乐视频',
  homevideos: '家庭视频', boxsets: '合集', books: '图书', mixed: '混合',
}[t] || t)

const typeTagType = (t) => ({
  movies: 'success', tvshows: 'primary', music: 'warning', mixed: 'info',
}[t] || '')
</script>

<style lang="scss" scoped>
.library-selector {
  .mode-switch {
    margin-bottom: 8px;
  }

  .lib-detail {
    margin-top: 8px;
    padding: 8px 12px;
    background: var(--jt-fill-light);
    border-radius: 4px;
    font-size: 13px;

    .single-path {
      display: flex;
      align-items: center;
      gap: 6px;
      color: #606266;
      word-break: break-all;
    }

    .multi-paths {
      .el-radio-group {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }

      .all-toggle {
        margin-top: 8px;
        padding-top: 8px;
        border-top: 1px solid #e4e7ed;
      }
    }
  }

  .lib-option {
    display: flex;
    align-items: center;
    gap: 8px;

    .lib-name {
      flex: 1;
    }
  }
}
</style>

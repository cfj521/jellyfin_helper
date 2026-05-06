<template>
  <div class="file-browser">
    <div class="path-bar">
      <el-button :icon="Back" @click="goUp" :disabled="!currentPath" />
      <el-input v-model="currentPath" @keyup.enter="browse(currentPath)" />
      <el-button type="primary" @click="$emit('select', currentPath)">
        选择此目录
      </el-button>
    </div>

    <div class="file-list" v-loading="loading">
      <div
        v-for="item in items"
        :key="item.path"
        class="file-item"
        @click="onItemClick(item)"
        @dblclick="onItemDblClick(item)"
      >
        <el-icon class="file-icon" :color="item.type === 'directory' ? '#e6a23c' : '#909399'">
          <Folder v-if="item.type === 'directory' || item.type === 'drive'" />
          <Document v-else />
        </el-icon>
        <span class="file-name">{{ item.name }}</span>
        <span v-if="item.size" class="file-size">{{ formatSize(item.size) }}</span>
      </div>

      <el-empty v-if="!loading && !items.length" description="空目录" />
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { Back, Folder, Document } from '@element-plus/icons-vue'
import { mediaApi } from '@/api'

const emit = defineEmits(['select'])

const currentPath = ref('')
const items = ref([])
const loading = ref(false)

const browse = async (path = '') => {
  loading.value = true
  try {
    const res = await mediaApi.browse(path)
    currentPath.value = res.data.current_path || ''
    items.value = res.data.items.filter(i => i.type === 'directory' || i.type === 'drive')
  } catch (e) {
    console.error('浏览目录失败', e)
  } finally {
    loading.value = false
  }
}

const goUp = () => {
  if (!currentPath.value) return

  const parts = currentPath.value.split(/[/\\]/)
  parts.pop()

  if (parts.length === 0 || (parts.length === 1 && parts[0] === '')) {
    browse('')
  } else {
    browse(parts.join('/'))
  }
}

const onItemClick = (item) => {
  // 单击选中
}

const onItemDblClick = (item) => {
  if (item.type === 'directory' || item.type === 'drive') {
    browse(item.path)
  }
}

const formatSize = (bytes) => {
  if (!bytes) return ''
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024
    i++
  }
  return `${bytes.toFixed(1)} ${units[i]}`
}

onMounted(() => {
  browse()
})
</script>

<style lang="scss" scoped>
.file-browser {
  .path-bar {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;

    .el-input {
      flex: 1;
    }
  }

  .file-list {
    max-height: 400px;
    overflow-y: auto;
    border: 1px solid #ebeef5;
    border-radius: 4px;

    .file-item {
      display: flex;
      align-items: center;
      padding: 10px 16px;
      border-bottom: 1px solid #ebeef5;
      cursor: pointer;

      &:last-child {
        border-bottom: none;
      }

      &:hover {
        background-color: #f5f7fa;
      }

      .file-icon {
        margin-right: 10px;
        font-size: 18px;
      }

      .file-name {
        flex: 1;
      }

      .file-size {
        color: #909399;
        font-size: 13px;
      }
    }
  }
}
</style>

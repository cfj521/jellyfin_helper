<template>
  <el-dialog
    v-model="visible"
    title="添加种子"
    width="560"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    @closed="onClosed"
    :destroy-on-close="false"
  >
    <el-form :model="form" label-width="100px" @submit.prevent>
      <el-form-item label="Magnet">
        <el-input
          v-model="form.magnet"
          type="textarea"
          :rows="3"
          placeholder="magnet:?xt=urn:btih:..."
          clearable
        />
      </el-form-item>
      <el-form-item label="或种子 URL">
        <el-input v-model="form.torrent_url" placeholder=".torrent 文件 URL" clearable />
      </el-form-item>
      <el-form-item label="媒体类型">
        <el-select v-model="form.user_hint_media_type" style="width: 200px" clearable>
          <el-option label="自动识别" value="" />
          <el-option label="电影" value="movie" />
          <el-option label="剧集" value="tv" />
          <el-option label="动漫" value="anime" />
          <el-option label="成人" value="adult" />
        </el-select>
        <span class="form-hint">
          选了具体类型 → 跳过启发式判断 confidence=1.0 直接入流水线；选"自动识别"走 regex / TMDB / LLM 链
        </span>
      </el-form-item>
    </el-form>

    <p class="hint">
      提交后种子加入 qB（暂停状态），后端在 30s 内分析识别 + 算路径，
      高置信自动入流水线；低置信会自动弹出"人工审核"窗口让你修正。
    </p>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="onSubmit">
        加入分析队列
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { discoverApi } from '@/api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // 可选：从外部传入预填的 magnet / 种子 URL（搜索页 / 热门页等）
  initialMagnet: { type: String, default: '' },
  initialUrl: { type: String, default: '' },
  // 可选：预填 media_type 当 user_hint
  initialMediaType: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue', 'submitted'])

const visible = computed({
  get: () => props.modelValue,
  set: v => emit('update:modelValue', v),
})

const form = reactive({
  magnet: '',
  torrent_url: '',
  user_hint_media_type: '',
})
const submitting = ref(false)

// 打开时预填外部传入的初始值
watch(() => props.modelValue, (now, prev) => {
  if (now && !prev) {
    if (props.initialMagnet) form.magnet = props.initialMagnet
    if (props.initialUrl) form.torrent_url = props.initialUrl
    if (props.initialMediaType) form.user_hint_media_type = props.initialMediaType
  }
})

const onSubmit = async () => {
  if (!form.magnet && !form.torrent_url) {
    ElMessage.warning('请填 magnet 或种子 URL')
    return
  }
  submitting.value = true
  try {
    // 显示标题：从 magnet 抠 dn 参数，没有就用 hash 或固定文案
    const title = _extractTitle(form.magnet) || form.torrent_url || '手动添加'
    const r = await discoverApi.push({
      title,
      magnet: form.magnet || undefined,
      torrent_url: form.torrent_url || undefined,
      source: 'manual',
      user_hint_media_type: form.user_hint_media_type || undefined,
    })
    const hash = r.data?.torrent_hash || ''
    ElMessage.success('已加入分析队列，识别后自动入流水线')
    // 把 hash 透出给父组件，父组件可以等 phase_status 变成 needs_review 时自动弹审核 modal
    emit('submitted', hash)
    visible.value = false
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || e.message)
  } finally {
    submitting.value = false
  }
}

const onClosed = () => {
  form.magnet = ''
  form.torrent_url = ''
  form.user_hint_media_type = ''
}

// 从 magnet 链接里抠 display name (dn 参数) 当 title
const _extractTitle = (magnet) => {
  if (!magnet) return ''
  try {
    const m = /[&?]dn=([^&]+)/.exec(magnet)
    return m ? decodeURIComponent(m[1].replace(/\+/g, ' ')) : ''
  } catch {
    return ''
  }
}
</script>

<style lang="scss" scoped>
.hint {
  color: var(--jt-text-muted);
  font-size: 12px;
  line-height: 1.6;
  margin: 4px 0 0;
}
</style>

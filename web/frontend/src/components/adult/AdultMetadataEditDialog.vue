<template>
  <el-dialog
    v-model="visible"
    title="编辑元数据"
    width="720"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
  >
    <div v-if="form" class="meta-edit">
      <!-- 番号头部（只读） -->
      <div class="meta-head">
        <span class="meta-code">{{ item?.code || '未识别' }}</span>
        <span v-if="item?.source" class="meta-source">来源 {{ item.source }}</span>
      </div>
      <div v-if="item?.file_path" class="meta-path">{{ item.file_path }}</div>

      <el-form :model="form" label-width="90px" label-position="right" size="default">
        <!-- 封面：预览 + URL 输入 + 本地上传 + 清除 -->
        <el-form-item label="封面">
          <div class="cover-edit">
            <div class="cover-preview">
              <img
                v-if="coverPreviewSrc"
                :src="coverPreviewSrc"
                referrerpolicy="no-referrer"
                class="cover-img"
                @error="onCoverPreviewError"
              />
              <div v-else class="cover-ph">无封面</div>
            </div>
            <div class="cover-controls">
              <el-input
                v-model="form.cover_url"
                placeholder="封面图 URL（保存时下载到本地）"
                clearable
              />
              <div class="cover-actions">
                <el-upload
                  :auto-upload="false"
                  :show-file-list="false"
                  accept="image/jpeg,image/png,image/webp"
                  :on-change="onLocalCoverChange"
                >
                  <el-button size="small" :icon="Upload" :loading="uploading">
                    上传本地图
                  </el-button>
                </el-upload>
                <el-button
                  size="small"
                  type="danger"
                  text
                  :disabled="!hasAnyCover"
                  @click="clearCover"
                >清除封面</el-button>
              </div>
              <span class="form-hint">
                URL 改了 → 保存时下载替换；上传立即生效；清除会删本地文件
              </span>
            </div>
          </div>
        </el-form-item>

        <el-form-item label="标题">
          <el-input v-model="form.title" placeholder="作品标题" clearable />
        </el-form-item>

        <el-form-item label="女优">
          <el-select
            ref="actorsSelectRef"
            v-model="form.actors"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="按回车添加（多人按顺序）"
            style="width: 100%"
          />
        </el-form-item>

        <el-form-item label="标签">
          <el-select
            ref="tagsSelectRef"
            v-model="form.tags"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="按回车添加"
            style="width: 100%"
          />
        </el-form-item>

        <el-form-item label="厂商">
          <el-input v-model="form.studio" placeholder="片商 / 工作室" clearable />
        </el-form-item>

        <el-form-item label="导演">
          <el-input v-model="form.director" placeholder="导演（可选）" clearable />
        </el-form-item>

        <el-form-item label="发行日期">
          <el-date-picker
            v-model="form.release_date"
            type="date"
            placeholder="YYYY-MM-DD"
            value-format="YYYY-MM-DD"
            style="width: 220px"
          />
        </el-form-item>

        <el-form-item label="评分">
          <el-input-number
            v-model="form.rating"
            :min="0"
            :max="10"
            :step="0.1"
            :precision="1"
            placeholder="0~10"
            style="width: 160px"
          />
          <span class="form-hint">0-10 分制；留空则清除评分</span>
        </el-form-item>
      </el-form>
    </div>

    <template #footer>
      <div class="footer-row">
        <!-- 危险操作（左侧）：清除所有元数据并标记排除 -->
        <el-button
          type="danger"
          plain
          :icon="Delete"
          :loading="clearing"
          :disabled="!item?.id"
          title="清除该条目的标题/演员/标签/封面/NFO 等所有刮削数据，并标记为排除（自动流程不再尝试）"
          @click="clearAndExclude"
        >
          清除元数据并排除
        </el-button>
        <!-- 常规操作（右侧）-->
        <div class="footer-actions">
          <el-button @click="visible = false">取消</el-button>
          <el-button
            type="primary"
            :loading="saving"
            @click="save"
          >
            保存
          </el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload, Delete } from '@element-plus/icons-vue'
import { adultApi, authedImgUrl } from '@/api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  item: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'saved'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const form = ref(null)
const saving = ref(false)
const uploading = ref(false)
const clearing = ref(false)
const coverPreviewError = ref(false)
// item.id 变化导致 cache buster 变化，避免上传后浏览器还显示老缩略图
const coverCacheBust = ref(Date.now())
// el-select refs：保存前要把输入框里没按 Enter 的残留文本强制提交进 v-model，
// 否则用户填了名字直接点"保存"会丢失
const actorsSelectRef = ref(null)
const tagsSelectRef = ref(null)

const _initForm = () => {
  const it = props.item || {}
  form.value = {
    title: it.title || '',
    actors: Array.isArray(it.actors) ? [...it.actors] : [],
    tags: Array.isArray(it.tags) ? [...it.tags] : [],
    studio: it.studio || '',
    director: it.director || '',
    release_date: it.release_date || '',
    rating: it.rating ?? null,
    cover_url: it.cover_url || '',
  }
  coverPreviewError.value = false
  coverCacheBust.value = Date.now()
}

// 当前封面预览：
//   - 用户在 URL 输入框改了且非空 → 预览那个 URL
//   - 否则用本地 /api/adult/items/{id}/poster
//   - 都没有 → 占位"无封面"
const coverPreviewSrc = computed(() => {
  if (coverPreviewError.value) return ''
  const url = form.value?.cover_url
  if (url && url !== props.item?.cover_url) {
    // 输入了新 URL → 直接预览（外站图加 referrerpolicy）
    return url
  }
  if (props.item?.id && props.item?.poster_path) {
    return authedImgUrl(`/api/adult/items/${props.item.id}/poster?v=${coverCacheBust.value}`)
  }
  if (url) return url   // 没本地但有 URL，显示 URL
  return ''
})

const hasAnyCover = computed(() =>
  !!(form.value?.cover_url || props.item?.poster_path),
)

const onCoverPreviewError = () => {
  coverPreviewError.value = true
}

const onLocalCoverChange = async (uploadFile) => {
  const file = uploadFile?.raw
  if (!file || !props.item?.id) return
  uploading.value = true
  try {
    const r = await adultApi.uploadCover(props.item.id, file)
    ElMessage.success('封面已上传')
    // 立即触发预览刷新（cache bust）+ 让 dialog 关闭后父组件 patch
    coverCacheBust.value = Date.now()
    coverPreviewError.value = false
    emit('saved', { itemId: props.item.id, item: r.data })
    // 注意：不主动关 dialog —— 用户可能还想改其它字段
  } catch (e) {
    ElMessage.error('上传失败：' + (e.response?.data?.detail || e.message))
  } finally {
    uploading.value = false
  }
}

const clearCover = () => {
  form.value.cover_url = ''
  coverPreviewError.value = false
  // 实际清除在保存时由后端处理（cover_url='' → 删本地文件）
}

watch(() => props.modelValue, (v) => {
  if (v) _initForm()
})

// 把 el-select 里残留的输入文本（用户没按 Enter）强制 commit 进 v-model 数组。
// el-select multiple+allow-create 的 UX 缺陷：必须 Enter 才创建，否则保存丢字。
const _flushPendingInput = (selectRef, currentArray) => {
  if (!selectRef?.value) return currentArray
  // el-select 内部 input value 在不同版本字段名不同：states.inputValue (>=2.5) / query
  const states = selectRef.value.states
  const pending = (states?.inputValue || states?.query || selectRef.value.query || '').trim()
  if (!pending) return currentArray
  if (currentArray.includes(pending)) return currentArray
  return [...currentArray, pending]
}

// 清除所有刮削元数据 + 标记排除（删本地 poster/nfo + 清 DB 字段，保留 file_path / code）
// 使用场景：用户判定刮削结果错乱（演员混入无关人 / 标题张冠李戴等），希望"重置 +
// 清除元数据 + 排除：删本地 poster/nfo（不可恢复），保留视频文件
const clearAndExclude = async () => {
  if (!props.item?.id) return
  const codeText = props.item?.code ? `番号 ${props.item.code}` : '该条目'
  try {
    await ElMessageBox.confirm(
      `清除元数据并标为排除。\n本地 poster / nfo 一并删除（视频文件保留）。\n\n${codeText}`,
      '清除并排除',
      {
        confirmButtonText: '清除并排除',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      },
    )
  } catch {
    return  // 用户取消
  }
  clearing.value = true
  try {
    const r = await adultApi.clearAndExclude(props.item.id)
    const deletedN = (r.data?.deleted_files || []).length
    ElMessage.success(
      `已清除元数据并排除${deletedN ? `（删 ${deletedN} 个本地文件）` : ''}`
    )
    emit('saved', { itemId: props.item.id, item: r.data?.item })
    visible.value = false
  } catch (e) {
    ElMessage.error('操作失败：' + (e.response?.data?.detail || e.message))
  } finally {
    clearing.value = false
  }
}

const save = async () => {
  if (!props.item?.id) return
  saving.value = true
  try {
    // 收尾：把 actors / tags 输入框里没按 Enter 的残留 commit 进数组（防止用户填了不按 Enter 直接保存丢失）
    form.value.actors = _flushPendingInput(actorsSelectRef, form.value.actors || [])
    form.value.tags = _flushPendingInput(tagsSelectRef, form.value.tags || [])

    // 后端 PUT 接口接受 ManualUpdate；空字符串当成 "" 写入；rating null 也合法
    // cover_url：仅当用户改过才传（避免误下载已有 URL）
    const payload = {
      title: form.value.title || null,
      release_date: form.value.release_date || null,
      studio: form.value.studio || null,
      director: form.value.director || null,
      actors: form.value.actors,
      tags: form.value.tags,
      rating: form.value.rating == null ? null : Number(form.value.rating),
    }
    if (form.value.cover_url !== (props.item?.cover_url || '')) {
      payload.cover_url = form.value.cover_url || ''   // '' = 清除本地
    }
    const r = await adultApi.update(props.item.id, payload)
    ElMessage.success('元数据已保存')
    emit('saved', { itemId: props.item.id, item: r.data })
    visible.value = false
  } catch (e) {
    ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
  } finally {
    saving.value = false
  }
}
</script>

<style lang="scss" scoped>
.meta-edit {
  .meta-head {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 4px;

    .meta-code {
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-weight: 600;
      color: #0369a1;
      font-size: 14px;
    }
    .meta-source {
      font-size: 11px;
      color: var(--jt-text-muted);
      background: var(--jt-card-border);
      padding: 1px 6px;
      border-radius: 3px;
    }
  }
  .meta-path {
    color: var(--jt-text-muted);
    font-size: 11px;
    margin-bottom: 14px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    word-break: break-all;
  }
}

.form-hint {
  margin-left: 8px;
  font-size: 12px;
  color: var(--jt-text-muted);
}

// footer 左右分栏：危险按钮在左，常规操作在右
.footer-row {
  display: flex;
  align-items: center;
  width: 100%;

  .footer-actions {
    margin-left: auto;
    display: flex;
    gap: 8px;
  }
}

.cover-edit {
  display: flex;
  gap: 12px;
  width: 100%;

  .cover-preview {
    width: 160px;
    aspect-ratio: 16 / 11;
    flex-shrink: 0;
    border-radius: 4px;
    overflow: hidden;
    background: var(--jt-fill-light);
    border: 1px solid var(--jt-card-border);
    display: flex;
    align-items: center;
    justify-content: center;

    .cover-img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .cover-ph {
      color: var(--jt-text-muted);
      font-size: 12px;
    }
  }

  .cover-controls {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;

    .cover-actions {
      display: flex;
      gap: 8px;
      align-items: center;
    }
  }
}
</style>

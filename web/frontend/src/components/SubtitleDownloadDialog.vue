<template>
  <el-dialog
    v-model="visible"
    title="下载字幕"
    width="780"
    :close-on-click-modal="false"
    @closed="onClosed"
  >
    <div v-if="item" class="sub-dl-dialog">
      <!-- 当前视频信息 -->
      <div class="current-info">
        <div class="info-line">
          <strong>{{ item.name }}</strong>
          <span v-if="item.year" class="info-year">({{ item.year }})</span>
        </div>
        <div v-if="item.path" class="info-path">{{ item.path }}</div>
        <div class="info-langs">
          <span class="info-langs-label">已有字幕</span>
          <template v-if="item.subtitle_langs?.length">
            <el-tag
              v-for="l in item.subtitle_langs"
              :key="l"
              size="small"
              effect="light"
              class="have-tag"
            >{{ langLabel(l) }}</el-tag>
          </template>
          <span v-else class="muted">— 无</span>
        </div>
      </div>

      <!-- 搜索栏：搜索片名 label + 自适应 input + 短名搜索 + 多源搜索 -->
      <form class="search-bar" @submit.prevent>
        <span class="search-label">搜索片名：</span>
        <el-input
          v-model="form.query"
          placeholder="默认用 release 名（视频文件 stem）"
          clearable
          class="search-input"
          @keyup.enter.prevent="search"
        />
        <el-button
          type="primary"
          :icon="MagicStick"
          :loading="triggerSource === 'short'"
          :disabled="searching && triggerSource !== 'short'"
          title="先用提取名（去掉 release tag / 年份后的纯标题）填充搜索框，再多源搜索"
          @click="useShortName"
        >
          短名搜索
        </el-button>
        <el-button
          type="primary"
          :icon="Search"
          :loading="triggerSource === 'search'"
          :disabled="searching && triggerSource !== 'search'"
          title="把搜索框填回 release 名（视频文件 stem），再多源搜索"
          @click="useReleaseName"
        >
          搜索
        </el-button>
      </form>
      <div v-if="searchedSources.length" class="search-meta">
        <span class="src-meta-label">本次搜索源：</span>
        <el-tag
          v-for="s in searchedSources"
          :key="s"
          size="small"
          effect="plain"
          class="src-meta-tag"
        >{{ s }}</el-tag>
      </div>

      <!-- 结果列表 -->
      <div v-if="results.length" class="sub-results">
        <div class="results-header">
          找到 <b>{{ results.length }}</b> 条 — 选择下载：
        </div>
        <el-radio-group v-model="selectedIdx" class="result-list">
          <label
            v-for="(s, idx) in results"
            :key="`${s.source}-${s.id}`"
            class="result-card"
            :class="{ active: selectedIdx === idx }"
          >
            <el-radio :label="idx" class="result-radio" />
            <div class="result-meta">
              <div class="result-line">
                <el-tag size="small" :type="srcTagType(s.source)" effect="dark" class="src-tag">
                  {{ s.source }}
                </el-tag>
                <span class="result-title">{{ s.title || '(未命名)' }}</span>
                <span v-if="s.lang_codes?.length" class="result-langs">
                  <el-tag
                    v-for="lc in s.lang_codes"
                    :key="lc"
                    size="small"
                    type="success"
                    effect="plain"
                  >{{ langLabel(lc) }}</el-tag>
                </span>
              </div>
              <!-- 字幕文件名 —— 用户最关心的"对得上视频版本吗"线索 -->
              <!-- assrt 包内可能多文件（双语 / 多集），首个 + 折叠 +N -->
              <div v-if="s.filename" class="result-filename">
                📄 {{ s.filename }}
                <el-tooltip
                  v-if="s.filelist && s.filelist.length > 1"
                  :content="s.filelist.join('\n')"
                  placement="top"
                  :show-after="200"
                >
                  <span class="filelist-more">+{{ s.filelist.length - 1 }} 个文件</span>
                </el-tooltip>
              </div>
              <div class="result-extra muted">
                <!-- 匹配分（Bazarr 风格综合分，100 分制）：hash / imdb / tmdb / release_group /
                     resolution / source / year / S+E / lang —— hover 看 breakdown -->
                <el-tooltip
                  v-if="s.score_pct != null"
                  :show-after="200"
                  placement="top"
                >
                  <template #content>
                    <div class="score-tip">
                      <div class="score-tip-title">匹配度 {{ s.score_pct }}/100　·　原始分 {{ s.score }}</div>
                      <div
                        v-for="(v, k) in s.score_breakdown"
                        :key="k"
                        class="score-tip-row"
                      >
                        <span>{{ scoreLabel(k) }}</span>
                        <b>+{{ v }}</b>
                      </div>
                      <div v-if="!Object.keys(s.score_breakdown || {}).length" class="score-tip-empty">
                        无任何匹配信号
                      </div>
                    </div>
                  </template>
                  <span :class="['score-pill', scoreClass(s.score_pct)]">🎯 {{ s.score_pct }}</span>
                </el-tooltip>
                <span v-if="s.upload_time">· 📅 {{ s.upload_time }}</span>
                <span v-if="s.download_count != null">· ⬇ {{ formatCount(s.download_count) }}</span>
                <span v-if="s.subtype">· {{ s.subtype }}</span>
                <span v-if="s.release_site">· {{ s.release_site }}</span>
              </div>
            </div>
          </label>
        </el-radio-group>
      </div>

      <el-empty
        v-else-if="searched && !searching"
        :image-size="80"
        description="没找到匹配字幕 — 试试改下搜索词"
      />
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="downloading"
        :disabled="selectedIdx === null"
        @click="download"
      >
        下载选中字幕
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, MagicStick } from '@element-plus/icons-vue'
import { subtitleApi } from '@/api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  item: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'downloaded'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const form = ref({ query: '' })
const results = ref([])
const selectedIdx = ref(null)
const searching = ref(false)         // 整体搜索中（防重入用）
const triggerSource = ref(null)      // 'search' | 'short' —— 哪个按钮触发的，对应按钮显示 spinner
const downloading = ref(false)
const searched = ref(false)
const searchedSources = ref([])

const _SRC_TAG_TYPE = {
  assrt: 'warning',
  opensubtitles: 'primary',
  shooter: 'success',
}
const srcTagType = (s) => _SRC_TAG_TYPE[s] || 'info'

const _LANG_LABEL = {
  chs: '简体', cht: '繁体', eng: '英语', jpn: '日语', kor: '韩语',
  fre: '法语', ger: '德语', spa: '西语', rus: '俄语', ita: '意语',
}
const langLabel = (code) => _LANG_LABEL[code] || (code || '').toUpperCase().slice(0, 4)

// 下载量格式化：12345 → '12.3k'，1234567 → '1.2M'，避免长串数字喧宾夺主
const formatCount = (n) => {
  if (n == null) return ''
  if (n < 1000) return String(n)
  if (n < 1_000_000) return (n / 1000).toFixed(n < 10000 ? 1 : 0).replace(/\.0$/, '') + 'k'
  return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M'
}

// 综合分配色（100 分制）：≥80 绿 / ≥50 蓝 / ≥30 黄 / 否则红
const scoreClass = (pct) => {
  if (pct >= 80) return 'score-excellent'   // 含 ID + release_group + 多维匹配
  if (pct >= 50) return 'score-good'        // 含 ID 或 release_group + 分辨率
  if (pct >= 30) return 'score-fair'        // 仅有少量信号
  return 'score-poor'
}

const _SCORE_LABEL = {
  hash: '🎯 文件 hash 一致 (+100)',
  imdb_id: 'IMDB ID 匹配',
  tmdb_id: 'TMDB ID 匹配',
  release_group: '字幕组一致',
  resolution: '分辨率一致',
  source: '片源一致 (BluRay/WEB-DL)',
  year: '年份一致',
  episode: '集数 S/E 一致',
  lang: '语言命中',
}
const scoreLabel = (k) => _SCORE_LABEL[k] || k

watch(() => props.modelValue, (v) => {
  if (!v || !props.item) return
  // 默认填 release 名（视频文件 stem）—— 大多数源（assrt / opensubtitles 文本搜）
  // 拿 release 名匹配版本最准；想用干净标题搜时点"短名搜索"切到 jellyfin 元数据
  const stem = (props.item.path || '')
    .split(/[\\/]/).pop()
    .replace(/\.[a-z0-9]{2,5}$/i, '')
  if (stem) {
    form.value.query = stem
  } else {
    // 没 path 时（罕见）退回 jellyfin name+year
    const name = (props.item.name || '').trim()
    const year = props.item.year
    form.value.query = name ? (year ? `${name} ${year}` : name) : ''
  }
  results.value = []
  selectedIdx.value = null
  searched.value = false
  searchedSources.value = []
  // 不自动 search() —— 让用户主动点搜索按钮，省配额 + 给改词机会
})

// 解析 release 名（视频文件 stem，去扩展名）
const _resolveReleaseName = () => {
  return (props.item?.path || '')
    .split(/[\\/]/).pop()
    .replace(/\.[a-z0-9]{2,5}$/i, '')
}

// 解析短名（jellyfin 元数据：item.name + item.year，干净的标题）
//   { name: "12.12: The Day", year: 2023 } → "12.12: The Day 2023"
// 没 jellyfin 元数据时（罕见）退回 release 名 stem
const _resolveShortName = () => {
  const name = (props.item?.name || '').trim()
  const year = props.item?.year
  if (name) return year ? `${name} ${year}` : name
  return _resolveReleaseName()
}

// 搜索按钮 = "用 release 名搜"（先把搜索框填回 release 名，再多源搜）
// 短名搜索按钮 = "用短名（干净标题）搜"
// 两个按钮对称：各自把搜索框换成自己的预设词，再触发搜索。
// 用户在框里手动改词后按回车走 search() 默认分支，会用框内当前内容（不覆盖）。
const useReleaseName = () => {
  const stem = _resolveReleaseName()
  if (stem) form.value.query = stem
  if (form.value.query.trim().length >= 3) {
    search('search')
  } else {
    ElMessage.warning('release 名太短或为空，请直接在搜索框输入')
  }
}

const useShortName = () => {
  const short = _resolveShortName()
  form.value.query = short
  if (short && short.trim().length >= 3) {
    search('short')
  }
}

const search = async (source = 'search') => {
  const q = form.value.query.trim()
  if (q.length < 3) {
    ElMessage.warning('搜索词需要 ≥ 3 个字符')
    return
  }
  searching.value = true
  triggerSource.value = source        // 哪个按钮触发的，spinner 转哪个
  searched.value = true
  selectedIdx.value = null
  try {
    // 多源聚合搜索：assrt + opensubtitles 后端并发
    //   - OpenSubtitles 优先用 imdb_id / tmdb_id（命中精度远高于 query）
    //   - assrt 始终用 query（不支持 ID）
    const r = await subtitleApi.multiSearch({
      query: q,
      video_path: props.item.path || undefined,
      imdb_id: props.item.imdb_id || undefined,
      tmdb_id: props.item.tmdb_id || undefined,
      year: props.item.year || undefined,
      cnt: 20,
      preferred_langs: ['chs', 'cht', 'eng'],
    })
    results.value = r.data?.results || []
    searchedSources.value = r.data?.sources_used || []
    if (!results.value.length) {
      ElMessage.info('所有源都没找到匹配字幕')
    } else if (results.value.length === 1) {
      selectedIdx.value = 0
    }
  } catch (e) {
    const detail = e.response?.data?.detail || e.message
    ElMessage.error('搜索失败：' + detail)
  } finally {
    searching.value = false
    triggerSource.value = null
  }
}

// 发起一次下载请求；首次 overwrite=false，命中 exists 由 download() 弹框决定是否重试
const _doDownload = async (sub, overwrite) => {
  return await subtitleApi.multiDownload({
    source: sub.source,
    sub_id: sub.id,
    video_path: props.item.path,
    preferred_langs: ['chs', 'cht', 'eng'],
    overwrite,
  })
}

const _handleResult = (r) => {
  const status = r.data?.status
  if (status === 'success') {
    const files = (r.data.saved_files || []).map(f => f.path).join(', ')
    ElMessage.success(`字幕下载完成：${files}`)
    emit('downloaded', { itemId: props.item.id, result: r.data })
    visible.value = false
    return true
  }
  if (status === 'archive_unhandled') {
    ElMessage.warning('压缩包格式不支持自动解压；已落地原始包到视频同目录')
    return true
  }
  ElMessage.info(`完成：${status || '未知'}`)
  return true
}

const download = async () => {
  if (selectedIdx.value === null) return
  const sub = results.value[selectedIdx.value]
  if (!sub?.id) {
    ElMessage.error('字幕缺少 id')
    return
  }
  downloading.value = true
  try {
    let r = await _doDownload(sub, false)
    // 命中已存在：弹确认框让用户选覆盖 / 取消
    if (r.data?.status === 'exists') {
      const existing = (r.data.saved_files || []).map(f => f.path).join('\n') || (r.data.message || '已存在同名字幕')
      try {
        await ElMessageBox.confirm(
          `目标位置已存在字幕文件：\n${existing}\n\n是否覆盖？`,
          '字幕已存在',
          {
            confirmButtonText: '覆盖',
            cancelButtonText: '取消',
            type: 'warning',
            customClass: 'sub-overwrite-confirm',
          },
        )
      } catch {
        ElMessage.info('已取消，未覆盖')
        return
      }
      r = await _doDownload(sub, true)
    }
    _handleResult(r)
  } catch (e) {
    const detail = e.response?.data?.detail || e.message
    if (e.response?.status === 429) {
      ElMessage.error('源配额超限：' + detail)
    } else {
      ElMessage.error('下载失败：' + detail)
    }
  } finally {
    downloading.value = false
  }
}

const onClosed = () => {
  results.value = []
  selectedIdx.value = null
  searched.value = false
  searchedSources.value = []
}
</script>

<style lang="scss" scoped>
.sub-dl-dialog .current-info {
  margin-bottom: 14px;
  padding: 10px 12px;
  background: #f8fafc;
  border-left: 3px solid #6366f1;
  border-radius: 4px;
  font-size: 13px;

  .info-line { color: #0f172a; }
  .info-year { color: #94a3b8; margin-left: 6px; }
  .info-path {
    color: #94a3b8;
    font-size: 11px;
    margin-top: 4px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    word-break: break-all;
  }
  .info-langs {
    margin-top: 6px;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px;
    font-size: 12px;

    .info-langs-label { color: #94a3b8; margin-right: 4px; }
    .have-tag { font-size: 11px; }
  }
}

// 搜索栏：label + 自适应 input + 两个按钮
.search-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;

  .search-label {
    flex-shrink: 0;
    color: #475569;
    font-size: 14px;
  }
  .search-input {
    flex: 1;        // input 自适应填满中间剩余空间
    min-width: 0;
  }
  // 两个相邻按钮之间收紧 gap（抵消父级 gap=8 的一部分）
  .el-button + .el-button {
    margin-left: -4px;
  }
}
.search-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  margin-bottom: 8px;
  flex-wrap: wrap;

  .src-meta-label {
    color: #94a3b8;
  }
  .src-meta-tag {
    text-transform: lowercase;
  }
}

.sub-results {
  .results-header {
    font-size: 13px;
    color: #475569;
    margin-bottom: 8px;
  }
  .result-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    width: 100%;
  }
}

.result-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  background: #fff;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
  margin: 0;

  &:hover {
    border-color: #c7d2fe;
    background: #f5f3ff;
  }
  &.active {
    border-color: #6366f1;
    background: #eef2ff;
    box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15);
  }

  .result-radio { margin: 0; flex-shrink: 0; }
  .result-meta {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .result-line {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;

    .src-tag {
      flex-shrink: 0;
      text-transform: lowercase;
      letter-spacing: 0.3px;
    }
    .result-title {
      font-weight: 500;
      color: #0f172a;
      word-break: break-word;
    }
  }
  // 字幕真实文件名行：等宽字体，让用户能快速看出"是否对得上视频版本"
  .result-filename {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    color: #475569;
    word-break: break-all;

    .filelist-more {
      margin-left: 6px;
      padding: 0 5px;
      font-family: inherit;
      font-size: 10px;
      color: #6366f1;
      background: #eef2ff;
      border-radius: 8px;
      cursor: help;
    }
  }
  .result-extra {
    font-size: 11px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
}

.muted { color: #94a3b8; }

// Bazarr 风格综合匹配分（pill，100 分制）
.score-pill {
  display: inline-flex;
  align-items: center;
  font-weight: 600;
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 10px;
  margin-right: 2px;
  cursor: help;

  &.score-excellent {
    background: #dcfce7;
    color: #15803d;
    border: 1px solid #86efac;
  }
  &.score-good {
    background: #dbeafe;
    color: #1d4ed8;
    border: 1px solid #93c5fd;
  }
  &.score-fair {
    background: #fef3c7;
    color: #b45309;
    border: 1px solid #fcd34d;
  }
  &.score-poor {
    background: #fee2e2;
    color: #b91c1c;
    border: 1px solid #fca5a5;
  }
}

// tooltip 内容样式（不在 scoped 容器内仍可命中，因 el-tooltip 内容渲染到 body）
:global(.score-tip) {
  min-width: 180px;
  font-size: 12px;
}
:global(.score-tip-title) {
  font-weight: 600;
  border-bottom: 1px solid rgba(255,255,255,.15);
  padding-bottom: 4px;
  margin-bottom: 4px;
}
:global(.score-tip-row) {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 1px 0;
  b { color: #fde68a; }
}
:global(.score-tip-empty) {
  color: #cbd5e1;
  font-style: italic;
}
</style>

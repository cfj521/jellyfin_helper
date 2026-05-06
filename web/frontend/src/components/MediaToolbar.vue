<template>
  <div class="media-toolbar" :class="{ 'has-selection': scope.mode === 'items' }">
    <div class="toolbar-scope">
      <el-icon class="scope-icon"><Operation /></el-icon>
      <span class="scope-label">{{ scopeLabel }}</span>
      <el-button
        v-if="scope.mode === 'items'"
        link
        size="small"
        type="primary"
        @click="$emit('clear-selection')"
      >
        清除选择
      </el-button>
    </div>

    <div class="toolbar-actions">
      <el-tooltip
        v-for="btn in buttons"
        :key="btn.key"
        :content="btn.tooltip || ''"
        :disabled="!btn.tooltip"
        placement="top"
      >
        <el-button
          type="primary"
          :disabled="btn.disabled"
          :loading="loadingKey === btn.key"
          @click="onClick(btn)"
        >
          <el-icon v-if="loadingKey !== btn.key"><component :is="btn.icon" /></el-icon>
          {{ btn.label }}
        </el-button>
      </el-tooltip>

      <!-- 一键修复（靠右对齐） -->
      <el-button
        class="run-all-btn"
        type="danger"
        :loading="loadingKey === 'run-all'"
        @click="openRunAllDialog"
      >
        <el-icon v-if="loadingKey !== 'run-all'"><MagicStick /></el-icon>
        一键修复所有问题
      </el-button>
    </div>

    <!-- 通用确认对话框（替代 ElMessageBox，支持 dry-run 开关）-->
    <el-dialog
      v-model="confirmDialog.visible"
      :title="confirmDialog.btn?.label || '确认'"
      width="500px"
      :close-on-click-modal="false"
      append-to-body
    >
      <div class="confirm-scope">{{ scopeLabel }}</div>
      <div class="confirm-text" v-html="confirmDialog.btn?.confirm?.text || ''" />

      <div v-if="confirmDialog.btn?.confirm?.supportsDryRun" class="dry-run-row">
        <el-checkbox v-model="confirmDialog.dryRun" size="large">
          <span class="dry-run-label">测试模式</span>
        </el-checkbox>
        <div class="dry-run-hint">
          {{ confirmDialog.dryRun
            ? (confirmDialog.btn.confirm.dryRunHint || '仅扫描预览，不实际写入或删除')
            : '勾选后仅扫描预览，不实际写入/删除/上传，可用于先看影响面' }}
        </div>
      </div>

      <template #footer>
        <el-button @click="onConfirmCancel">取消</el-button>
        <el-button
          :type="confirmBtnType"
          :class="confirmBtnClass"
          @click="onConfirmOk"
        >
          {{ confirmBtnText }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 音轨语言选择对话框 -->
    <el-dialog
      v-model="audioDialogVisible"
      title="选择默认音轨语言"
      width="500px"
      :close-on-click-modal="false"
    >
      <div class="audio-dialog-tip">
        将把范围内所有视频的默认音轨切换为下列语言（按可用性优先匹配）：
      </div>
      <el-radio-group v-model="audioLang" class="audio-lang-group">
        <el-radio value="eng">英语 (English)</el-radio>
        <el-radio value="chs">汉语 (Mandarin / 普通话)</el-radio>
        <el-radio value="yue">汉语 (Cantonese / 粤语)</el-radio>
        <el-radio value="jpn">日语 (Japanese)</el-radio>
      </el-radio-group>

      <div class="dry-run-row">
        <el-checkbox v-model="audioDryRun" size="large">
          <span class="dry-run-label">测试模式</span>
        </el-checkbox>
        <div class="dry-run-hint">
          {{ audioDryRun
            ? '仅扫描并列出需要修改的文件，不实际写入 mkv'
            : '勾选后不实际调 mkvpropedit，仅查看候选列表' }}
        </div>
      </div>

      <template #footer>
        <el-button @click="audioDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmAudioDialog">开始</el-button>
      </template>
    </el-dialog>

    <!-- 一键修复确认对话框 -->
    <el-dialog
      v-model="runAllDialogVisible"
      title="一键修复所有问题"
      width="560px"
      :close-on-click-modal="false"
    >
      <div class="runall-scope">{{ scopeLabel }}</div>
      <div class="runall-tip">
        将按下列顺序<strong>串行</strong>执行所有步骤，每步会单独创建一个子任务，可在任务管理页查看进度。
      </div>

      <ol class="runall-steps">
        <li v-for="(s, i) in runAllSteps" :key="i" :class="{ 'is-skip': s.skip }">
          <span class="step-no">{{ i + 1 }}</span>
          <span class="step-label">{{ s.label }}</span>
          <span v-if="s.danger" class="step-tag danger">不可逆</span>
          <span v-if="s.skip" class="step-tag skip">将跳过</span>
        </li>
      </ol>

      <el-divider style="margin: 16px 0" />

      <div class="audio-section">
        <div class="audio-section-label">默认音轨语言（最后一步）</div>
        <el-radio-group v-model="runAllAudioLang" class="audio-lang-group">
          <el-radio value="">不修改音轨</el-radio>
          <el-radio value="eng">英语</el-radio>
          <el-radio value="chs">汉语（普通话）</el-radio>
          <el-radio value="yue">汉语（粤语）</el-radio>
          <el-radio value="jpn">日语</el-radio>
        </el-radio-group>
      </div>

      <div class="dry-run-row">
        <el-checkbox v-model="runAllDryRun" size="large">
          <span class="dry-run-label">测试模式（所有步骤都跑 dry-run）</span>
        </el-checkbox>
        <div class="dry-run-hint">
          {{ runAllDryRun
            ? '所有步骤仅扫描预览：不删文件 / 不移文件 / 不下载 / 不上传 / 不写 mkv'
            : '勾选后所有步骤都仅扫描预览，可用于先看影响面再正式跑' }}
        </div>
      </div>

      <el-alert
        v-if="hasDangerSteps && !runAllDryRun"
        type="warning"
        :closable="false"
        show-icon
        title="操作包含 删除 / 移动 文件等不可撤销动作"
        description="开始后无法中途取消，请确认作用范围正确。"
        style="margin-top: 12px"
      />

      <template #footer>
        <el-button @click="runAllDialogVisible = false">取消</el-button>
        <el-button
          :type="runAllDryRun ? 'primary' : 'danger'"
          :loading="loadingKey === 'run-all'"
          @click="confirmRunAll"
        >
          {{ runAllDryRun ? '开始测试' : '开始执行' }}（{{ enabledStepCount }} 步）
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  Picture, User, Document, Download, EditPen, Headset, Operation,
  Delete, Compass, Aim, MagicStick,
} from '@element-plus/icons-vue'
import {
  metadataApi, subtitleApi, audioApi, maintenanceApi,
} from '@/api'

const props = defineProps({
  /**
   * scope 描述当前作用范围：
   *  - { mode: 'all', library_ids?: [...] }：全部媒体库
   *  - { mode: 'library', library_id, library_name }：单个库
   *  - { mode: 'items', item_paths: [...], item_count, library_id?, library_name? }：选中若干媒体
   */
  scope: { type: Object, required: true },
})

defineEmits(['clear-selection'])

const router = useRouter()

const loadingKey = ref(null)

// 通用确认对话框（替代 ElMessageBox，因为要塞 dry-run 复选框）
const confirmDialog = reactive({
  visible: false,
  btn: null,
  dryRun: false,
})
let confirmResolver = null

// 音轨独立对话框
const audioDialogVisible = ref(false)
const audioLang = ref('eng')
const audioDryRun = ref(false)

// 一键修复对话框
const runAllDialogVisible = ref(false)
const runAllAudioLang = ref('eng')
const runAllDryRun = ref(false)

const scopeLabel = computed(() => {
  const s = props.scope
  if (s.mode === 'all') return '作用范围：所有媒体库'
  if (s.mode === 'items') {
    const lib = s.library_name ? `（${s.library_name}）` : ''
    return `作用范围：选中的 ${s.item_count || s.item_paths?.length || 0} 项${lib}`
  }
  if (s.mode === 'library') return `作用范围：${s.library_name || '当前库'}`
  return '作用范围：未指定'
})

/**
 * 9 个按钮统一定义。
 *  supports：声明该按钮支持哪些 scope；不在 supports 内的会灰显，
 *           除非配置了 itemFallback —— 此时 'items' 模式自动回退到所在库。
 *  handler(opts)：opts.dryRun 由确认框传入，handler 内部映射到对应后端字段。
 *  confirm：点击后弹出的确认框配置（音轨按钮无 confirm，由语言选择框承担确认）
 *    - level: 'info' | 'warning' | 'danger'，决定按钮样式
 *    - text: 确认框正文（HTML 片段）
 *    - supportsDryRun: 是否支持测试模式（不支持的按钮不显示开关）
 *    - dryRunHint: 勾选 dry-run 后展示的提示语
 */
const buttonDefs = [
  // ── 维护类（先做：规整文件 → 再做内容修复）──
  {
    key: 'cleanup-samples',
    label: '清理 Sample 内容',
    icon: Delete,
    supports: ['all', 'library', 'items'],
    confirm: {
      level: 'danger',
      text: '将扫描范围内的视频，识别并<strong>删除</strong> Sample 文件，随后<strong>删除</strong>因此变空的 release 子目录。<br/><strong>操作不可撤销</strong>。',
      supportsDryRun: true,
      dryRunHint: '仅扫描列出 Sample 文件和会变空的目录，不实际删除',
    },
    handler: ({ dryRun = false } = {}) => maintenanceApi.cleanupSamples({
      dry_run: dryRun,
      ...buildScopePayload(),
    }),
  },
  {
    key: 'normalize-paths',
    label: '扫描并规范路径',
    icon: Compass,
    supports: ['all', 'library', 'items'],
    confirm: {
      level: 'danger',
      text: '将扫描范围内嵌套在 release 子目录里的主文件，<strong>移动</strong>回 release 根目录。<br/><strong>移动后 Jellyfin 会重扫该库</strong>。',
      supportsDryRun: true,
      dryRunHint: '仅扫描列出嵌套主文件，不实际移动',
    },
    handler: ({ dryRun = false } = {}) => maintenanceApi.normalizePaths({
      dry_run: dryRun,
      ...buildScopePayload(),
    }),
  },
  {
    key: 'auto-identify',
    label: '扫描并修复识别错误',
    icon: Aim,
    supports: ['all', 'library', 'items'],
    confirm: {
      level: 'warning',
      text: '将扫描范围内未识别的条目，自动从路径提取标题+年份去 TMDB 搜索，<strong>应用第一个匹配结果</strong>到 Jellyfin。',
      supportsDryRun: true,
      dryRunHint: '仅查询 TMDB 候选并列出，不应用到 Jellyfin',
    },
    handler: ({ dryRun = false } = {}) => maintenanceApi.autoIdentify({
      dry_run: dryRun,
      ...buildScopePayload(),
    }),
  },

  // ── 元数据修复（图片）──
  {
    key: 'posters',
    label: '扫描并修复缺失海报',
    icon: Picture,
    supports: ['all', 'library', 'items'],
    confirm: {
      level: 'warning',
      text: '将扫描范围内缺少海报的影视条目，从 TMDB 下载海报并上传到 Jellyfin。',
      supportsDryRun: true,
      dryRunHint: '仅查询 TMDB 海报地址并列出，不下载也不上传',
    },
    handler: ({ dryRun = false } = {}) => metadataApi.fixPosters({
      // poster_fix 的 scan_only 等价于 dry_run
      scan_only: dryRun,
      skip_existing: true,
      ...buildScopePayload(),
    }),
  },
  {
    // Episode 缩略图 = TMDB still。仅在剧集库可见
    key: 'episode-stills',
    label: '修复缺失 Episode 缩略图',
    icon: Picture,
    supports: ['all', 'library', 'items'],
    tvOnly: true,
    confirm: {
      level: 'warning',
      text: '将扫描范围内缺缩略图的 Episode，从 TMDB 取 still 图上传到 Jellyfin。',
      supportsDryRun: true,
      dryRunHint: '仅查询 TMDB 是否有 still 图并列出，不下载也不上传',
    },
    handler: ({ dryRun = false } = {}) => {
      // items 模式优先用 episode_ids；fallback 到 library_id
      const s = props.scope
      const payload = {
        scan_only: dryRun,
        skip_existing: true,
      }
      if (s.mode === 'items' && (s.episode_ids?.length)) {
        payload.episode_ids = s.episode_ids
      } else if (s.mode === 'items' && s.library_id) {
        payload.library_id = s.library_id
      } else if (s.mode === 'library' && s.library_id) {
        payload.library_id = s.library_id
      } else if (s.mode === 'all' && s.library_ids?.length) {
        payload.library_ids = s.library_ids
      }
      return metadataApi.fixEpisodeStills(payload)
    },
  },
  {
    key: 'actors',
    label: '扫描并修复演员图片',
    icon: User,
    supports: ['all', 'library'],
    itemFallback: 'library',
    confirm: {
      level: 'warning',
      text: '将扫描范围内缺图片的演员（自动按 TMDB ID 去重），从 TMDB 下载头像上传到 Jellyfin。',
      supportsDryRun: true,
      dryRunHint: '仅查询 TMDB 演员匹配并列出，不下载也不上传',
    },
    handler: ({ dryRun = false } = {}) => metadataApi.fixActors({
      // actor_fix 的 scan_only 等价于 dry_run
      scan_only: dryRun,
      skip_existing: true,
      ...buildScopePayload({ allowItems: false }),
    }),
  },

  // ── 字幕 ──
  {
    key: 'subtitle-scan',
    label: '扫描字幕缺失',
    icon: Document,
    supports: ['all', 'library', 'items'],
    confirm: {
      level: 'info',
      text: '将扫描范围内的视频，列出缺失字幕的项（不下载、不修改文件，纯只读扫描）。',
      // 本身就是只读扫描，不需要 dry-run 选项
      supportsDryRun: false,
    },
    handler: () => subtitleApi.scan(null, {
      ...buildScopePayload(),
    }),
  },
  {
    key: 'subtitle-auto-fix',
    label: '自动下载字幕',
    icon: Download,
    supports: ['all', 'library', 'items'],
    confirm: {
      level: 'warning',
      text: '将扫描范围内的视频，对缺字幕项调用 OpenSubtitles 下载字幕到媒体同目录，并自动对齐文件名。',
      supportsDryRun: true,
      dryRunHint: '仅扫描并显示将要下载的字幕清单，不实际请求 OpenSubtitles',
    },
    handler: ({ dryRun = false } = {}) => subtitleApi.autoFix(null, {
      dry_run: dryRun,
      rename: true,
      // 字幕外挂文件 Jellyfin 是即时发现的，不需要触发整库重扫
      refresh_jellyfin: false,
      ...buildScopePayload(),
    }),
  },
  {
    key: 'subtitle-rename',
    label: '字幕文件名对齐',
    icon: EditPen,
    supports: ['all', 'library', 'items'],
    confirm: {
      level: 'warning',
      text: '将扫描范围内同目录的字幕文件，对未对齐媒体文件名的字幕进行重命名（仅改字幕名，不动视频）。',
      supportsDryRun: true,
      dryRunHint: '仅扫描并显示将要重命名的字幕清单，不实际改名',
    },
    handler: ({ dryRun = false } = {}) => subtitleApi.rename(null, {
      // rename 的 execute 与 dryRun 取反
      execute: !dryRun,
      // 字幕外挂文件 Jellyfin 是即时发现的，重命名后无需重扫
      refresh_jellyfin: false,
      ...buildScopePayload(),
    }),
  },

  // ── 音轨（最后；点击直接弹语言选择，不走通用 confirm 流程）──
  {
    key: 'audio-default',
    label: '扫描并设置默认音轨',
    icon: Headset,
    supports: ['all', 'library', 'items'],
    needsDialog: 'audio',
    // 真正的 handler 在用户选完语言后由 confirmAudioDialog 调用
    handler: ({ audioLang, dryRun = false } = {}) => audioApi.defaultTrack({
      preferred_langs: [audioLang],
      skip_single_track: true,
      // apply 与 dryRun 取反
      apply: !dryRun,
      ...buildScopePayload(),
    }),
  },
]

/**
 * 计算每个按钮在当前 scope 下的可用状态 / 提示文案 / 实际 effective scope。
 */
const buttons = computed(() => {
  const s = props.scope
  return buttonDefs
    // tvOnly: 只在剧集库（collection_type='tvshows'）显示；'mixed' 库也算
    .filter(b => !b.tvOnly || ['tvshows', 'mixed'].includes(s.collection_type))
    .map(b => {
      if (s.mode === 'items' && !b.supports.includes('items')) {
        if (b.itemFallback === 'library' && s.library_id) {
          return { ...b, _scopeOverride: 'library', tooltip: '该功能不支持单条目，将作用于所在库' }
        }
        return { ...b, disabled: true, tooltip: '该功能暂不支持选中条目' }
      }
      if (s.mode === 'all' && !b.supports.includes('all')) {
        return { ...b, disabled: true, tooltip: '请选择具体库后再操作' }
      }
      return b
    })
})

/**
 * 把 scope 转换成请求 body 字段。
 * - 'items' 模式：item_paths（+ 所属库 library_id 一并附带，便于后端反查）
 * - 'library' 模式：library_id
 * - 'all' 模式：library_ids（如果父组件提供了具体列表），否则不传 → 后端按"全部库"展开
 *
 * options.allowItems = false：演员场景，items 模式仍按 library 处理
 */
const buildScopePayload = (options = {}) => {
  const allowItems = options.allowItems !== false
  const s = props.scope

  if (s.mode === 'items') {
    if (allowItems) {
      const payload = { item_paths: s.item_paths || [] }
      if (s.library_id) payload.library_id = s.library_id
      return payload
    }
    // fallback 到 library
    if (s.library_id) return { library_id: s.library_id }
    return {}
  }
  if (s.mode === 'library') {
    return s.library_id ? { library_id: s.library_id } : {}
  }
  if (s.mode === 'all') {
    if (s.library_ids?.length) return { library_ids: s.library_ids }
    return {}  // 后端兜底为全部库
  }
  return {}
}

/**
 * 触发某个按钮：调 handler → 收 task_id → 跳 /tasks
 * opts：透传给 handler，含 { dryRun, audioLang }
 */
const triggerHandler = async (btn, opts = {}) => {
  loadingKey.value = btn.key
  try {
    const res = await btn.handler(opts)
    const taskId = res?.data?.task_id
    const modePrefix = opts.dryRun ? '【测试】' : ''
    if (taskId) {
      ElMessage.success(`${modePrefix}任务已启动 #${taskId}（${btn.label}）`)
    } else {
      ElMessage.success(`${modePrefix}任务已启动（${btn.label}）`)
    }
    router.push({ path: '/tasks' })
  } catch (e) {
    console.error(`[MediaToolbar] ${btn.key} 启动失败:`, e)
  } finally {
    loadingKey.value = null
  }
}

/**
 * 确认对话框右侧主按钮的样式（按 level + dry-run 决定）。
 * dry-run 模式一律降级为蓝色"开始测试"，避免显示红色让用户误以为危险。
 */
const confirmBtnType = computed(() => {
  if (confirmDialog.dryRun) return 'primary'
  const lvl = confirmDialog.btn?.confirm?.level
  if (lvl === 'danger') return 'warning'
  return 'primary'
})
const confirmBtnClass = computed(() => {
  if (confirmDialog.dryRun) return ''
  return confirmDialog.btn?.confirm?.level === 'danger' ? 'el-button--danger' : ''
})
const confirmBtnText = computed(() => {
  if (confirmDialog.dryRun) return '开始测试'
  return confirmDialog.btn?.confirm?.level === 'danger' ? '执行' : '开始'
})

/**
 * 弹出通用确认对话框。返回一个 Promise：
 *   resolve({ ok: true, dryRun }) 或 resolve({ ok: false })
 */
const askConfirm = (btn) => new Promise(resolve => {
  if (!btn.confirm) {
    resolve({ ok: true, dryRun: false })
    return
  }
  confirmDialog.btn = btn
  confirmDialog.dryRun = false  // 默认关
  confirmDialog.visible = true
  confirmResolver = resolve
})

const onConfirmCancel = () => {
  confirmDialog.visible = false
  confirmResolver?.({ ok: false })
  confirmResolver = null
}

const onConfirmOk = () => {
  const dryRun = confirmDialog.dryRun
  confirmDialog.visible = false
  confirmResolver?.({ ok: true, dryRun })
  confirmResolver = null
}

const onClick = async (btn) => {
  if (btn.disabled) return
  if (btn.needsDialog === 'audio') {
    audioDialogVisible.value = true
    return
  }
  const result = await askConfirm(btn)
  if (!result.ok) return
  triggerHandler(btn, { dryRun: result.dryRun })
}

const confirmAudioDialog = () => {
  audioDialogVisible.value = false
  const audioBtn = buttonDefs.find(b => b.key === 'audio-default')
  if (audioBtn) {
    triggerHandler(audioBtn, { audioLang: audioLang.value, dryRun: audioDryRun.value })
  }
}

// ────────────────── 一键修复 ──────────────────

/**
 * 当 scope.mode === 'items' 时，演员图片按钮会自动 fallback 到所属库。
 * 这里同样适用：actor_fix step 在 backend 用 library_id（而不是 item_paths）。
 * 我们标注哪些 step 在当前 scope 下会被跳过/降级。
 */
const runAllSteps = computed(() => {
  const audioOn = !!runAllAudioLang.value
  return [
    { key: 'cleanup_samples',     label: '清理 Sample 内容',          danger: true },
    { key: 'normalize_paths',     label: '扫描并规范路径',            danger: true },
    { key: 'auto_identify',       label: '扫描并修复识别错误' },
    { key: 'poster_fix',          label: '扫描并修复缺失海报' },
    { key: 'actor_fix',           label: '扫描并修复演员图片' },
    { key: 'subtitle_scan',       label: '扫描字幕缺失' },
    { key: 'subtitle_auto_fix',   label: '自动下载字幕' },
    { key: 'subtitle_rename',     label: '字幕文件名对齐' },
    { key: 'audio_default_track', label: `设置默认音轨${audioOn ? '（' + langDisplay(runAllAudioLang.value) + '）' : ''}`, skip: !audioOn },
  ]
})

const enabledStepCount = computed(() => runAllSteps.value.filter(s => !s.skip).length)
const hasDangerSteps = computed(() => runAllSteps.value.some(s => s.danger && !s.skip))

const langDisplay = (code) => ({
  eng: '英语', chs: '普通话', yue: '粤语', jpn: '日语',
}[code] || code)

const openRunAllDialog = () => {
  runAllDialogVisible.value = true
}

const confirmRunAll = async () => {
  runAllDialogVisible.value = false
  loadingKey.value = 'run-all'
  const dryRun = runAllDryRun.value
  try {
    const payload = {
      ...buildScopePayload(),
      audio_lang: runAllAudioLang.value || null,
      dry_run: dryRun,
    }
    const res = await maintenanceApi.runAll(payload)
    const taskId = res?.data?.task_id
    const modePrefix = dryRun ? '【测试】' : ''
    if (taskId) {
      ElMessage.success(`${modePrefix}一键修复已启动 #${taskId}（共 ${enabledStepCount.value} 步）`)
    }
    router.push({ path: '/tasks' })
  } catch (e) {
    console.error('[MediaToolbar] run-all 启动失败', e)
  } finally {
    loadingKey.value = null
  }
}
</script>

<style lang="scss" scoped>
.media-toolbar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 14px;
  background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  margin-bottom: 16px;

  &.has-selection {
    background: linear-gradient(135deg, #fef3c7 0%, #fef9e7 100%);
    border-color: #fcd34d;
  }

  .toolbar-scope {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: #475569;

    .scope-icon { color: #6366f1; }
    .scope-label { font-weight: 500; }
  }

  .toolbar-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  // 一键修复：自动占据剩余空间靠右
  .run-all-btn {
    margin-left: auto;
    font-weight: 600;
  }
}

.audio-dialog-tip {
  margin-bottom: 14px;
  font-size: 13px;
  color: #64748b;
  line-height: 1.5;
}

// ── 通用确认对话框 ──
.confirm-scope {
  padding: 8px 12px;
  background: #f1f5f9;
  border-radius: 4px;
  font-size: 13px;
  color: #475569;
  font-weight: 500;
  margin-bottom: 12px;
}
.confirm-text {
  font-size: 13px;
  color: #475569;
  line-height: 1.7;
  margin-bottom: 14px;
}

// dry-run 行：勾选框 + 副提示
.dry-run-row {
  margin-top: 14px;
  padding: 10px 12px;
  background: #fefce8;
  border: 1px dashed #fde047;
  border-radius: 6px;

  .dry-run-label {
    font-weight: 500;
    color: #854d0e;
  }

  .dry-run-hint {
    margin-top: 4px;
    margin-left: 24px;  // 对齐 checkbox label
    font-size: 12px;
    color: #a16207;
    line-height: 1.5;
  }
}

.audio-lang-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;

  // Element Plus 默认 radio 有自带 margin，统一用 gap 控制
  :deep(.el-radio) {
    margin-right: 0;
  }
}

// ── 一键修复对话框 ──
.runall-scope {
  padding: 8px 12px;
  background: #f1f5f9;
  border-radius: 4px;
  font-size: 13px;
  color: #475569;
  font-weight: 500;
  margin-bottom: 12px;
}

.runall-tip {
  font-size: 13px;
  color: #64748b;
  line-height: 1.6;
  margin-bottom: 14px;
}

.runall-steps {
  list-style: none;
  padding: 0;
  margin: 0;

  li {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border-bottom: 1px dashed #e2e8f0;
    font-size: 13px;

    &:last-child { border-bottom: none; }

    &.is-skip {
      opacity: 0.45;
      text-decoration: line-through;
    }

    .step-no {
      width: 22px; height: 22px;
      border-radius: 50%;
      background: #6366f1;
      color: #fff;
      font-size: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }
    .step-label { flex: 1; }
    .step-tag {
      font-size: 11px;
      padding: 1px 6px;
      border-radius: 3px;

      &.danger {
        background: #fef2f2;
        color: #dc2626;
        border: 1px solid #fecaca;
      }
      &.skip {
        background: #f1f5f9;
        color: #64748b;
      }
    }
  }
}

.audio-section {
  .audio-section-label {
    font-size: 13px;
    font-weight: 500;
    color: #475569;
    margin-bottom: 8px;
  }
}
</style>

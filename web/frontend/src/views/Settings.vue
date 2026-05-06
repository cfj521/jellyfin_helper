<template>
  <div class="page-container">
    <div class="page-header">
      <h2>
        设置
        <el-tag v-if="dirty" type="warning" size="small" effect="dark" class="dirty-tag">
          有 {{ changedSectionCount }} 项配置未保存
        </el-tag>
      </h2>
      <div class="header-actions">
        <el-button @click="discardChanges" :disabled="!dirty || saving">
          <el-icon><Refresh /></el-icon>
          放弃修改
        </el-button>
        <el-button
          type="primary"
          :disabled="!dirty"
          :loading="saving"
          @click="confirmSave"
        >
          <el-icon><Check /></el-icon>
          应用配置
        </el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="config-tabs" tab-position="left">
      <!-- ============ 基础配置（Jellyfin） ============ -->
      <el-tab-pane name="basic">
        <template #label>
          <div class="tab-label">
            <el-icon><Connection /></el-icon>
            <span>基础配置</span>
          </div>
        </template>

        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <el-icon><Connection /></el-icon>
              <span>Jellyfin 服务器</span>
            </div>
          </template>
          <el-form :model="form.jellyfin" label-width="120px">
            <el-form-item label="服务器地址">
              <el-input v-model="form.jellyfin.host" placeholder="http://192.168.1.100:8096" />
            </el-form-item>
            <el-form-item label="API Key">
              <el-input v-model="form.jellyfin.api_key" type="password" show-password />
              <div class="form-hint">
                Jellyfin 后台 → 控制台 → API 密钥 → 创建
              </div>
            </el-form-item>
          </el-form>

          <!-- 路径映射（嵌入 Jellyfin 服务器 section） -->
          <el-divider content-position="left">
            <span class="sub-section-title">
              路径映射
              <el-tag size="small" type="info" effect="plain" style="margin-left: 6px">调试用</el-tag>
            </span>
          </el-divider>

          <div class="path-mapping-block">
            <div class="path-mapping-toolbar">
              <span class="switch-label">启用路径映射</span>
              <el-switch v-model="form.path_mappings.enabled" size="small" />
              <span class="form-hint">
                Jellyfin 跑在 Linux/Docker 看到 <code>/library/videos/...</code>，
                本工具跑在 Windows 通过 SMB 挂为 <code>Z:/videos/...</code> 时启用
              </span>
            </div>

            <div v-if="form.path_mappings.rules.length === 0" class="empty-rules">
              暂无规则，点下方"添加规则"开始
            </div>

            <div
              v-for="(rule, idx) in form.path_mappings.rules"
              :key="idx"
              class="path-rule"
              :class="{ disabled: !form.path_mappings.enabled }"
            >
              <div class="rule-fields">
                <el-input
                  v-model="rule.from"
                  placeholder="Jellyfin 路径前缀，如 /library/videos"
                  size="small"
                >
                  <template #prepend>从</template>
                </el-input>
                <el-icon class="arrow-icon"><Right /></el-icon>
                <el-input
                  v-model="rule.to"
                  placeholder="本后端可访问的前缀，如 Z:/videos"
                  size="small"
                >
                  <template #prepend>到</template>
                </el-input>
              </div>
              <el-button link type="danger" size="small" @click="removePathRule(idx)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>

            <el-button size="small" @click="addPathRule" style="margin-top: 8px">
              <el-icon><Plus /></el-icon>
              添加规则
            </el-button>
          </div>
        </el-card>

        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <el-icon><Coin /></el-icon>
              <span>数据库 (PostgreSQL)</span>
            </div>
          </template>
          <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 16px">
            修改数据库连接后必须重启后端服务才能生效。如有同名环境变量（JF_DB_*），会优先于此处的设置。
          </el-alert>
          <el-form :model="form.database" label-width="120px">
            <el-form-item label="主机">
              <el-input v-model="form.database.host" />
            </el-form-item>
            <el-form-item label="端口">
              <el-input-number v-model="form.database.port" :min="1" :max="65535" />
            </el-form-item>
            <el-form-item label="数据库名">
              <el-input v-model="form.database.name" />
            </el-form-item>
            <el-form-item label="用户">
              <el-input v-model="form.database.user" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="form.database.password" type="password" show-password />
            </el-form-item>
          </el-form>
        </el-card>

      </el-tab-pane>

      <!-- ============ 第三方服务 ============ -->
      <el-tab-pane name="services">
        <template #label>
          <div class="tab-label">
            <el-icon><Link /></el-icon>
            <span>第三方服务</span>
          </div>
        </template>

        <!-- TMDB -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge tmdb">TMDB</span>
              <span>The Movie Database</span>
              <el-tag size="small" :type="form.tmdb.api_key ? 'success' : 'info'" effect="plain">
                {{ form.tmdb.api_key ? '已配置' : '未配置' }}
              </el-tag>
            </div>
          </template>
          <el-form :model="form.tmdb" label-width="120px">
            <el-form-item label="API Key">
              <el-input v-model="form.tmdb.api_key" type="password" show-password />
              <div class="form-hint">
                <a href="https://www.themoviedb.org/settings/api" target="_blank">在 TMDB 申请 API Key</a>
              </div>
            </el-form-item>
            <el-form-item label="显示语言">
              <el-select v-model="form.tmdb.language" filterable style="width: 240px">
                <el-option label="简体中文 (zh-CN)" value="zh-CN" />
                <el-option label="繁体中文 (zh-TW)" value="zh-TW" />
                <el-option label="香港中文 (zh-HK)" value="zh-HK" />
                <el-option label="English (en-US)" value="en-US" />
                <el-option label="English UK (en-GB)" value="en-GB" />
                <el-option label="日本語 (ja-JP)" value="ja-JP" />
                <el-option label="한국어 (ko-KR)" value="ko-KR" />
                <el-option label="Français (fr-FR)" value="fr-FR" />
                <el-option label="Deutsch (de-DE)" value="de-DE" />
                <el-option label="Español (es-ES)" value="es-ES" />
                <el-option label="Italiano (it-IT)" value="it-IT" />
                <el-option label="Русский (ru-RU)" value="ru-RU" />
              </el-select>
              <div class="form-hint">
                影响热门推荐 / 详情页的标题、简介、类型等本地化字段。
                修改后会清空 TMDB 列表缓存，下次访问时按新语言重新获取。
              </div>
            </el-form-item>
            <el-form-item label="请求延迟(秒)">
              <el-input-number
                v-model="form.tmdb.request_delay"
                :min="0"
                :max="300"
                :step="5"
              />
              <div class="form-hint">
                <strong>仅用于演员图 / 海报的批量修复</strong>，避免被 TMDB 风控拒绝。
                推荐 / 流行列表不受此约束。被拒绝时按 30 / 60 / 90s 累加退避，自动重试 3 次。
              </div>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- OpenSubtitles -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge opensubs">OS</span>
              <span>OpenSubtitles</span>
              <el-tag size="small" :type="form.subtitle.opensubtitles_api_key ? 'success' : 'info'" effect="plain">
                {{ form.subtitle.opensubtitles_api_key ? '已配置' : '未配置' }}
              </el-tag>
            </div>
          </template>
          <el-form :model="form.subtitle" label-width="160px">
            <el-form-item label="API Key">
              <el-input v-model="form.subtitle.opensubtitles_api_key" type="password" show-password />
              <div class="form-hint">
                <a href="https://www.opensubtitles.com/consumers" target="_blank">opensubtitles.com/consumers</a> 申请
              </div>
            </el-form-item>
            <el-form-item label="用户名">
              <el-input v-model="form.subtitle.opensubtitles_username" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="form.subtitle.opensubtitles_password" type="password" show-password />
            </el-form-item>
            <el-form-item label="期望语言">
              <el-checkbox-group v-model="form.subtitle.preferred_langs">
                <el-checkbox label="chs">简体中文</el-checkbox>
                <el-checkbox label="cht">繁体中文</el-checkbox>
                <el-checkbox label="eng">英语</el-checkbox>
                <el-checkbox label="jpn">日语</el-checkbox>
                <el-checkbox label="kor">韩语</el-checkbox>
              </el-checkbox-group>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- Jackett -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge jackett">JK</span>
              <span>Jackett 种子搜索</span>
              <el-tag size="small" :type="form.jackett.api_key ? 'success' : 'info'" effect="plain">
                {{ form.jackett.api_key ? '已配置' : '未配置' }}
              </el-tag>
            </div>
          </template>
          <el-form :model="form.jackett" label-width="120px">
            <el-form-item label="服务器地址">
              <el-input v-model="form.jackett.host" placeholder="http://localhost:9117" />
            </el-form-item>
            <el-form-item label="API Key">
              <el-input v-model="form.jackett.api_key" type="password" show-password />
            </el-form-item>

            <el-divider content-position="left">
              <span style="font-size: 13px; color: #64748b">搜索偏好</span>
            </el-divider>

            <el-form-item label="关键字 chip">
              <el-select
                v-model="form.jackett.search_keywords"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="输入关键字按回车（如 2160p、HDR、BluRay）"
                style="width: 100%"
              />
              <div class="form-hint">
                在「种子搜索」页会显示成 chip，点击 toggle 进/出搜索框（再次点击移除）
              </div>
            </el-form-item>

            <el-form-item label="默认附加">
              <el-input
                v-model="form.jackett.default_keywords"
                placeholder="如 1080p HDR（留空则不预填）"
                clearable
              />
              <div class="form-hint">
                进入「种子搜索」页时自动拼到搜索框末尾，节省每次手动选 chip
              </div>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- qBittorrent -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge qbit">qB</span>
              <span>qBittorrent 下载管理</span>
              <el-tag size="small" :type="form.qbittorrent.username ? 'success' : 'info'" effect="plain">
                {{ form.qbittorrent.username ? '已配置' : '未配置' }}
              </el-tag>
            </div>
          </template>
          <el-form :model="form.qbittorrent" label-width="120px">
            <el-form-item label="服务器地址">
              <el-input v-model="form.qbittorrent.host" placeholder="http://localhost:8080" />
            </el-form-item>
            <el-form-item label="用户名">
              <el-input v-model="form.qbittorrent.username" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="form.qbittorrent.password" type="password" show-password />
            </el-form-item>
            <el-form-item label="下载路径">
              <el-input v-model="form.qbittorrent.download_path" placeholder="/downloads" />
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>

      <!-- ============ 成人内容 ============ -->
      <el-tab-pane name="adult">
        <template #label>
          <div class="tab-label">
            <el-icon><Lock /></el-icon>
            <span>成人内容</span>
          </div>
        </template>

        <!-- 启用开关 -->
        <el-card shadow="never" class="cfg-card">
          <el-form :model="form.adult" label-width="160px">
            <el-form-item label="启用">
              <el-switch v-model="form.adult.enabled" />
              <span class="form-hint">禁用时菜单不显示、相关 API 不挂载</span>
            </el-form-item>
            <el-form-item label="刮削间隔(秒)">
              <el-input-number v-model="form.adult.scraper_delay" :min="1" :max="300" :step="5" />
              <span class="form-hint">两次刮削请求之间的间隔（默认 30 秒，避免被封）</span>
            </el-form-item>
            <el-form-item label="自动监视">
              <el-switch v-model="form.adult.auto_scrape" />
              <span class="form-hint">
                通过 Jellyfin WebSocket 实时监听库变化，新文件自动入库 + 刮削（默认关闭）
              </span>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- 数据源 -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <el-icon><Files /></el-icon>
              <span>刮削数据源</span>
              <el-tag size="small" type="info" effect="plain">{{ enabledSourceCount }} 个启用</el-tag>
            </div>
          </template>

          <p class="form-hint" style="margin-bottom: 12px">
            按顺序尝试，第一个命中即返回。可拖动排序、自定义镜像 URL、单独配代理。
          </p>

          <div class="sources-list">
            <div
              v-for="(src, idx) in form.adult.sources"
              :key="idx"
              class="source-row"
              :class="{ disabled: !src.enabled }"
            >
              <el-switch v-model="src.enabled" />
              <el-select v-model="src.name" style="width: 140px">
                <el-option
                  v-for="key in availableSources(src.name)"
                  :key="key"
                  :label="sourceLabels[key] || key"
                  :value="key"
                />
              </el-select>
              <el-input
                v-model="src.base_url"
                :placeholder="`默认 ${defaultBaseUrls[src.name] || ''}`"
                clearable
              />
              <el-input
                v-model="src.proxy"
                placeholder="此源专用代理（可选）"
                style="width: 200px"
                clearable
              />
              <el-button-group>
                <el-button size="small" :disabled="idx === 0" @click="moveSource(idx, -1)" title="上移">
                  <el-icon><ArrowUp /></el-icon>
                </el-button>
                <el-button size="small" :disabled="idx === form.adult.sources.length - 1" @click="moveSource(idx, 1)" title="下移">
                  <el-icon><ArrowDown /></el-icon>
                </el-button>
                <el-button size="small" type="danger" @click="form.adult.sources.splice(idx, 1)" title="删除">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </el-button-group>
            </div>
          </div>

          <el-button text type="primary" @click="addSource" style="margin-top: 12px">
            <el-icon><Plus /></el-icon>
            添加数据源
          </el-button>
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import {
  Refresh, Check, Connection, Coin, Link, Lock,
  Files, Plus, Delete, ArrowUp, ArrowDown, Right,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { configApi } from '@/api'

const activeTab = ref('basic')
const loading = ref(false)
const saving = ref(false)

// 脏数据相关变量（form 在下面定义，watch 注册延后到 form 定义之后）
const dirty = ref(false)
let initialSnapshot = ''
let watchStarted = false

const sourceLabels = {
  javbus: 'JavBus',
  javdb: 'JavDB',
  javlibrary: 'JavLibrary',
  avmoo: 'AvMoo',
}
const defaultBaseUrls = {
  javbus: 'https://www.javbus.com',
  javdb: 'https://javdb.com',
  javlibrary: 'https://www.javlibrary.com/cn',
  avmoo: 'https://avmoo.cyou/cn',
}
const allSourceKeys = Object.keys(sourceLabels)

const blank = () => ({
  jellyfin: { host: '', api_key: '' },
  tmdb: { api_key: '', request_delay: 30, language: 'zh-CN' },
  subtitle: {
    opensubtitles_api_key: '',
    opensubtitles_username: '',
    opensubtitles_password: '',
    preferred_langs: ['chs', 'eng'],
  },
  jackett: { host: '', api_key: '', search_keywords: [], default_keywords: '' },
  qbittorrent: { host: '', username: '', password: '', download_path: '/downloads' },
  database: { host: '', port: 5432, name: '', user: '', password: '' },
  path_mappings: { enabled: false, rules: [] },
  adult: {
    enabled: false,
    library_ids: [],
    auto_detect: true,
    auto_scrape: false,
    scraper_delay: 30,
    sources: [],
  },
})

const form = reactive(blank())

// ==== 脏数据模式 ====
// 用 JSON 快照对比检测改动；watchStarted 标志防止 loadConfig 触发误报
const computeSnapshot = () => JSON.stringify(form)

const captureSnapshot = () => {
  initialSnapshot = computeSnapshot()
  dirty.value = false
}

// 直接传 reactive 对象给 watch，Vue 会自动 deep watch
watch(form, () => {
  if (!watchStarted) return
  dirty.value = computeSnapshot() !== initialSnapshot
}, { deep: true })

const enabledSourceCount = computed(() =>
  form.adult.sources.filter(s => s.enabled).length
)

const availableSources = (current) => {
  // 已被其他行选中的不可选（除了自己当前的）
  const used = new Set(form.adult.sources.map(s => s.name).filter(n => n !== current))
  return allSourceKeys.filter(k => !used.has(k))
}

const mergeIntoForm = (cfg) => {
  for (const section of Object.keys(form)) {
    if (cfg[section]) {
      // 数组字段直接替换，dict 字段合并
      for (const key of Object.keys(cfg[section])) {
        const val = cfg[section][key]
        form[section][key] = val
      }
    }
  }
  // 兜底：数组字段
  if (!Array.isArray(form.adult.sources)) form.adult.sources = []
  if (!Array.isArray(form.adult.library_ids)) form.adult.library_ids = []
  if (!Array.isArray(form.jackett.search_keywords)) form.jackett.search_keywords = []
}

const loadConfig = async () => {
  loading.value = true
  watchStarted = false
  try {
    const res = await configApi.getFull()
    mergeIntoForm(res.data.config || {})
  } catch (e) {
    console.error('加载配置失败', e)
  } finally {
    loading.value = false
    // 等 Vue 把响应式更新跑完，再开始监听变化
    await nextTick()
    captureSnapshot()
    watchStarted = true
  }
}

const discardChanges = async () => {
  if (!dirty.value) return
  try {
    await ElMessageBox.confirm('放弃所有未保存的修改？', '确认', {
      type: 'warning',
      confirmButtonText: '放弃',
      cancelButtonText: '取消',
    })
  } catch { return }
  await loadConfig()
  ElMessage.info('已恢复到保存前的状态')
}

const moveSource = (idx, delta) => {
  const newIdx = idx + delta
  if (newIdx < 0 || newIdx >= form.adult.sources.length) return
  const arr = form.adult.sources
  ;[arr[idx], arr[newIdx]] = [arr[newIdx], arr[idx]]
}


const addSource = () => {
  const used = new Set(form.adult.sources.map(s => s.name))
  const available = allSourceKeys.find(k => !used.has(k))
  if (!available) {
    ElMessage.warning('已添加所有支持的数据源')
    return
  }
  form.adult.sources.push({ name: available, enabled: false, base_url: '', proxy: '' })
}

// 路径映射规则增删
const addPathRule = () => {
  form.path_mappings.rules.push({ from: '', to: '' })
}
const removePathRule = (idx) => {
  form.path_mappings.rules.splice(idx, 1)
}

// 判断当前修改是否包含某个 section（用 JSON 快照对比）
const sectionChanged = (sectionName) => {
  if (!initialSnapshot) return false
  try {
    const initial = JSON.parse(initialSnapshot)
    return JSON.stringify(initial[sectionName]) !== JSON.stringify(form[sectionName])
  } catch {
    return false
  }
}

// 改动 section 数量（用于"有 X 项配置未保存"显示）
const changedSectionCount = computed(() => {
  if (!dirty.value || !initialSnapshot) return 0
  try {
    const initial = JSON.parse(initialSnapshot)
    return Object.keys(form).filter(k =>
      JSON.stringify(initial[k]) !== JSON.stringify(form[k])
    ).length
  } catch {
    return 0
  }
})

const confirmSave = async () => {
  if (!dirty.value) {
    ElMessage.info('没有需要保存的修改')
    return
  }

  // 仅当改了关键连接（Jellyfin / 数据库）时才需要二次确认
  const dbChanged = sectionChanged('database')
  const jellyfinChanged = sectionChanged('jellyfin')

  if (dbChanged || jellyfinChanged) {
    const reasons = []
    if (jellyfinChanged) reasons.push('Jellyfin 连接')
    if (dbChanged) reasons.push('数据库连接（需要手动重启后端服务）')
    try {
      await ElMessageBox.confirm(
        `检测到修改了：${reasons.join('、')}。确定继续吗？`,
        '确认关键改动',
        {
          confirmButtonText: '确定',
          cancelButtonText: '取消',
          type: 'warning',
        }
      )
    } catch { return }
  }

  saving.value = true
  try {
    // 清理 sources：去掉空 base_url / proxy 字段以保持 yaml 整洁
    const cleanedAdult = {
      ...form.adult,
      sources: form.adult.sources
        .filter(s => s.name)
        .map(s => {
          const out = { name: s.name, enabled: !!s.enabled }
          if (s.base_url) out.base_url = s.base_url
          if (s.proxy) out.proxy = s.proxy
          return out
        }),
      // 保存即视为"用户已确认过"
      auto_detect: false,
    }
    // 清理 jackett.search_keywords：去空格 + 去重 + 过滤空字符串
    const cleanedJackett = {
      ...form.jackett,
      search_keywords: Array.from(new Set(
        (form.jackett.search_keywords || []).map(s => String(s).trim()).filter(Boolean)
      )),
      default_keywords: (form.jackett.default_keywords || '').trim(),
    }
    const payload = { ...form, adult: cleanedAdult, jackett: cleanedJackett }
    await configApi.saveFull(payload)
    ElMessage.success('配置已保存')
    await loadConfig()
  } catch (e) {
    console.error('保存失败', e)
  } finally {
    saving.value = false
  }
}

onMounted(() => loadConfig())

// ==== 离开页面前拦截（脏数据模式核心）====
const onBeforeUnload = (e) => {
  if (dirty.value) {
    e.preventDefault()
    e.returnValue = '你有未保存的配置修改，确定离开吗？'
    return e.returnValue
  }
}
window.addEventListener('beforeunload', onBeforeUnload)
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
})

// 切换路由时拦截（含点击侧边栏菜单等）
onBeforeRouteLeave(async () => {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm(
      '你有未保存的配置修改，确定要离开吗？',
      '未保存的修改',
      {
        type: 'warning',
        confirmButtonText: '离开（丢弃修改）',
        cancelButtonText: '留下',
      }
    )
    return true
  } catch {
    return false
  }
})
</script>

<style lang="scss" scoped>
.header-actions {
  display: flex;
  gap: 10px;
}

.dirty-tag {
  margin-left: 12px;
  vertical-align: middle;
  animation: pulse 1.6s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

.el-alert {
  margin-bottom: 20px;

  code {
    background: rgba(0, 0, 0, 0.08);
    padding: 1px 6px;
    border-radius: 3px;
  }
}

.config-tabs {
  margin-top: 20px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.04);
  min-height: 600px;

  :deep(.el-tabs__nav-wrap) {
    padding-top: 16px;
    background: #f8fafc;
    border-radius: 8px 0 0 8px;
  }

  :deep(.el-tabs__content) {
    padding: 20px;
  }

  .tab-label {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 12px;
  }
}

.cfg-card {
  margin-bottom: 16px;

  .cfg-card-head {
    display: flex;
    align-items: center;
    gap: 10px;
    font-weight: 600;
  }

  // 服务徽章
  .badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 700;
    color: #fff;
    letter-spacing: -0.5px;

    &.tmdb { background: linear-gradient(135deg, #032541 0%, #01b4e4 100%); }
    &.opensubs { background: linear-gradient(135deg, #be1622 0%, #ff6b35 100%); }
    &.jackett { background: linear-gradient(135deg, #1f7c89 0%, #25b3c4 100%); }
    &.qbit { background: linear-gradient(135deg, #2f80ed 0%, #56ccf2 100%); }
  }
}

.form-hint {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;

  a { color: #6366f1; }
}

.sub-section-title {
  font-weight: 500;
  color: #475569;
}

// 路径映射 block
.path-mapping-block {
  .path-mapping-toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
    flex-wrap: wrap;

    .switch-label {
      font-size: 13px;
      color: #475569;
      font-weight: 500;
    }

    .form-hint {
      margin-top: 0;
      flex: 1;

      code {
        background: rgba(99, 102, 241, 0.1);
        color: #4f46e5;
        padding: 1px 4px;
        border-radius: 3px;
        font-size: 11px;
      }
    }
  }

  .empty-rules {
    text-align: center;
    color: #94a3b8;
    font-size: 12px;
    padding: 16px;
    background: #f8fafc;
    border: 1px dashed #cbd5e1;
    border-radius: 4px;
  }

  .path-rule {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
    padding: 6px;
    border-radius: 4px;
    transition: opacity 0.15s;

    &.disabled {
      opacity: 0.5;
    }

    .rule-fields {
      flex: 1;
      display: flex;
      align-items: center;
      gap: 6px;

      .el-input {
        flex: 1;
      }

      .arrow-icon {
        color: #94a3b8;
        flex-shrink: 0;
      }
    }
  }
}

.sources-list {
  display: flex;
  flex-direction: column;
  gap: 8px;

  .source-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    transition: all 0.2s;

    &.disabled {
      opacity: 0.6;
      background: #f1f5f9;
    }

    .el-input {
      flex: 1;
    }
  }
}

</style>

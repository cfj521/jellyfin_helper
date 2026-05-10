import axios from 'axios'
import { ElMessage } from 'element-plus'

// 创建 axios 实例
export const api = axios.create({
  baseURL: '',
  timeout: 30000
})

// 请求拦截器
api.interceptors.request.use(
  config => {
    return config
  },
  error => {
    return Promise.reject(error)
  }
)

// 响应拦截器
api.interceptors.response.use(
  response => {
    return response
  },
  error => {
    const message = error.response?.data?.detail || error.message || '请求失败'
    ElMessage.error(message)
    return Promise.reject(error)
  }
)

// 字幕相关 API
export const subtitleApi = {
  // path 可以是 null（此时必须传 options.library_id）
  scan: (path, options = {}) =>
    api.post('/api/subtitle/scan', { path: path || null, ...options }),

  rename: (path, options = {}) =>
    api.post('/api/subtitle/rename', { path: path || null, ...options }),

  download: (reportId, options = {}) =>
    api.post('/api/subtitle/download', { report_id: reportId, ...options }),

  // 一条龙：扫描 → 下载 → 对齐 → 刷新 Jellyfin
  // path 可空（同 scan，库优先）；dry_run 默认 true（仅预览不动文件）
  autoFix: (path, options = {}) =>
    api.post('/api/subtitle/auto-fix', { path: path || null, ...options }),

  getReports: (params = {}) =>
    api.get('/api/subtitle/reports', { params }),

  getReport: (id) =>
    api.get(`/api/subtitle/reports/${id}`),

  // 硬字幕标注（VideoAnnotation）
  // 批量查：POST 是为了 paths 数组方便传
  queryAnnotations: (paths) =>
    api.post('/api/subtitle/annotations/query', { paths }),
  // 批量保存（insert or update）。items: [{file_path, hardcoded_langs[], note?}]
  // 空 hardcoded_langs + 空 note 视为删除请求
  saveAnnotations: (items) =>
    api.put('/api/subtitle/annotations', items),
  deleteAnnotation: (file_path) =>
    api.delete('/api/subtitle/annotations', { data: { file_path } }),

  // 单视频字幕下载（assrt 字幕站）
  assrtSearch: (payload) => api.post('/api/subtitle/assrt/search', payload),
  assrtDownload: (payload) => api.post('/api/subtitle/assrt/download', payload),
  assrtQuota: () => api.get('/api/subtitle/assrt/quota'),

  // 多源聚合搜索 / 下载（assrt + opensubtitles 等）
  multiSearch: (payload) => api.post('/api/subtitle/multi-search', payload),
  multiDownload: (payload) => api.post('/api/subtitle/multi-download', payload),
}

// 媒体库维护 API（批量清理 / 规范化）
export const maintenanceApi = {
  // payload: { item_paths?, library_id?, library_ids?, dry_run }
  cleanupSamples: (payload) =>
    api.post('/api/maintenance/cleanup-samples', payload),

  normalizePaths: (payload) =>
    api.post('/api/maintenance/normalize-paths', payload),

  // 扫描并修复识别错误：unrecognized 条目 → 自动提取标题年份 → TMDB 搜索 → apply 第一个
  autoIdentify: (payload) =>
    api.post('/api/maintenance/auto-identify', payload),

  // 一键修复：串行执行 toolbar 上从左到右的所有按钮流程
  // payload: { item_paths?, library_id?, library_ids?, audio_lang? }
  // audio_lang 不传 → 跳过音轨步骤
  runAll: (payload) =>
    api.post('/api/maintenance/run-all', payload),
}

// 音轨相关 API
export const audioApi = {
  // 检查 ffprobe / mkvpropedit 是否可用
  check: () => api.get('/api/audio/check'),

  // 扫描+（可选）修复默认音轨
  // payload 支持: {item_paths, library_id, library_ids, path, recursive,
  //                preferred_langs, skip_single_track, apply}
  defaultTrack: (payload) =>
    api.post('/api/audio/default-track', payload),
}

// 元数据相关 API
export const metadataApi = {
  // 演员
  getActors: (params = {}) =>
    api.get('/api/metadata/actors', { params }),

  scanActors: () =>
    api.post('/api/metadata/actors/scan'),

  fixActors: (options = {}) =>
    api.post('/api/metadata/actors/fix', options),

  fixSingleActor: (id) =>
    api.post(`/api/metadata/actors/${id}/fix`),

  // 手动上传本地图片到 Jellyfin（用于 TMDB+Wikidata 都没找到的演员）
  uploadActorImage: (id, file) => {
    const formData = new FormData()
    formData.append('image', file)
    return api.post(`/api/metadata/actors/${id}/upload-image`, formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },

  // 清理 CJK 译名 + 无 TMDB ID 的演员
  // payload: { dry_run: bool }
  deleteCjkActors: (payload = { dry_run: true }) =>
    api.post('/api/metadata/actors/delete-cjk', payload),

  // 海报
  getPosters: (params = {}) =>
    api.get('/api/metadata/posters', { params }),

  scanPosters: () =>
    api.post('/api/metadata/posters/scan'),

  fixPosters: (options = {}) =>
    api.post('/api/metadata/posters/fix', options),

  fixSinglePoster: (id) =>
    api.post(`/api/metadata/posters/${id}/fix`),

  // Episode 缩略图（TMDB still）批量 + 单集
  fixEpisodeStills: (options = {}) =>
    api.post('/api/metadata/episodes/fix-stills', options),

  fixSingleEpisodeStill: (episodeId) =>
    api.post(`/api/metadata/episodes/${episodeId}/fix-still`),
}

// 媒体库相关 API
export const mediaApi = {
  browse: (path = '') =>
    api.get('/api/media/browse', { params: { path } }),

  scan: (path, options = {}) =>
    api.post('/api/media/scan', { path, ...options }),

  findDuplicates: (path) =>
    api.get('/api/media/duplicates', { params: { path } }),

  // 基于 Jellyfin 元数据（TMDB / IMDB / 标题+年份 / 同剧同集）的语义重复检测
  findDuplicatesByMetadata: (libraryId) =>
    api.get('/api/media/duplicates-by-metadata', { params: { library_id: libraryId } }),

  analyzeStorage: (path) =>
    api.get('/api/media/storage', { params: { path } })
}

// 任务相关 API
export const taskApi = {
  list: (params = {}) =>
    api.get('/api/tasks', { params }),

  get: (id) =>
    api.get(`/api/tasks/${id}`),

  cancel: (id) =>
    api.post(`/api/tasks/${id}/cancel`),

  delete: (id) =>
    api.delete(`/api/tasks/${id}`)
}

// Jellyfin 直通 API
export const jellyfinApi = {
  libraries: (checkPaths = true) =>
    api.get('/api/medialibraries/libraries', { params: { check_paths: checkPaths } }),
  // params 支持 start_index / limit / item_type / search / years / genres
  libraryItems: (id, params = {}) =>
    api.get(`/api/medialibraries/libraries/${id}/items`, { params }),

  // 拉某库下所有 Genre 名（给"风格"过滤下拉做 options）
  libraryGenres: (id) =>
    api.get(`/api/medialibraries/libraries/${id}/genres`),

  // 批量拉 item 的字幕语言（jellyfin 列表 endpoint 不返 MediaStreams，要单独再拉）
  itemsSubtitleLangs: (ids) =>
    api.post('/api/medialibraries/items/subtitle-langs', { ids }),
  // 库统计：内存级 2h 缓存。force=true 跳过缓存重算
  // fields: 可选，逗号分隔字符串（例如 'health,poster,tmdb'）；不传 = 全部计算；空串 = 都不算
  //         后端按 fields 跳过隐藏指标的计算，不同 fields 各自缓存
  libraryStats: (id, force = false, fields = null) =>
    api.get(`/api/medialibraries/libraries/${id}/stats`, {
      params: {
        force_refresh: force,
        ...(fields !== null ? { fields } : {}),
      },
    }),
  // 懒加载缺字幕统计：复用 settings.cache_library_days 窗口内的 subtitle_scan 任务，
  // 没有就启新的；force=true 时跳过最近任务复用，直接启新扫描
  // 后端 max_age_minutes 走 config.yaml.cache.library_days * 1440，前端不再硬编码
  librarySubtitleStats: (id, force = false) =>
    api.get(`/api/medialibraries/libraries/${id}/subtitle-stats`, {
      params: { force_refresh: force },
    }),
  refreshLibrary: (id, mode = 'scan_changes') =>
    api.post(`/api/medialibraries/libraries/${id}/refresh`, null, { params: { mode } }),
  refreshAll: () =>
    api.post('/api/medialibraries/refresh-all'),

  // 检查当前配置的 API key 是否有管理员权限（/Library/Media/Updated 需要）
  checkApiKey: () => api.post('/api/medialibraries/check-api-key'),

  // 剧集 → 季 → 集 钻取（懒加载，30 分钟缓存；force=true 旁路缓存）
  seasonsOfSeries: (seriesId, force = false) =>
    api.get(`/api/medialibraries/series/${seriesId}/seasons`, { params: { force } }),
  episodesOfSeason: (seasonId, force = false) =>
    api.get(`/api/medialibraries/seasons/${seasonId}/episodes`, { params: { force } }),
  // 清空 seasons/episodes 缓存（强制刷新按钮触发）
  clearChildrenCache: () =>
    api.post('/api/medialibraries/cache/clear-children'),

  // 批量取 Series 行的聚合摘要：季数/集数/总时长/字幕覆盖
  // payload: {series_ids: [...]}
  seriesAggregates: (seriesIds) =>
    api.post('/api/medialibraries/series/aggregates', { series_ids: seriesIds }),

  // 重新识别（刮削元数据）
  // payload: { item_type, name?, year?, tmdb_id?, language? }
  identifySearch: (itemId, payload) =>
    api.post(`/api/medialibraries/items/${itemId}/identify-search`, payload),

  // payload: { candidate, replace_all_images? }
  identifyApply: (itemId, payload) =>
    api.post(`/api/medialibraries/items/${itemId}/identify-apply`, payload),

  // Sample 取证：返回 verdict + 证据明细（路径关键字 / 时长 / 兄弟文件大小对比）
  sampleEvidence: (itemId) =>
    api.get(`/api/medialibraries/items/${itemId}/sample-evidence`),

  // 删除条目（连同物理文件，用户须有 EnableContentDeletion 权限）
  deleteItem: (itemId) =>
    api.delete(`/api/medialibraries/items/${itemId}`),

  // 通过本地文件路径反查 Jellyfin Item（用于重复检测 hash 模式删除时找 jellyfin_id）
  lookupByPath: (path) =>
    api.get('/api/medialibraries/items/by-path', { params: { path } }),

  systemInfo: () =>
    api.get('/api/medialibraries/system'),
}

// 成人内容 API
export const adultApi = {
  list: (params = {}) => api.get('/api/adult/items', { params }),
  get: (id) => api.get(`/api/adult/items/${id}`),
  update: (id, payload) => api.put(`/api/adult/items/${id}`, payload),
  remove: (id, opts = {}) =>
    api.delete(`/api/adult/items/${id}`, {
      params: {
        delete_files: opts.deleteFiles || false,
        delete_in_jellyfin: opts.deleteInJellyfin || false,
      },
    }),
  scrapeOne: (code) => api.post(`/api/adult/scrape/${code}`),
  scrapeBatch: (payload) => api.post('/api/adult/scrape/batch', payload),
  regenerateNfo: (id) => api.post(`/api/adult/items/${id}/nfo`),
  // 上传本地图片作为封面（multipart/form-data）
  uploadCover: (id, file) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post(`/api/adult/items/${id}/cover-upload`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  syncFromJellyfin: (id) => api.post(`/api/adult/items/${id}/sync-from-jellyfin`),
  identify: (id, code, autoScrape = true) =>
    api.post(`/api/adult/items/${id}/identify`, { code, auto_scrape: autoScrape }),
  // 女优库 lazy 构建
  actressBuildStatus: () => api.get('/api/adult/actresses/build/status'),
  actressBuildStart: (requestDelay = 5.0) =>
    api.post('/api/adult/actresses/build/start', { request_delay: requestDelay }),
  actressBuildStop: () => api.post('/api/adult/actresses/build/stop'),
  listActresses: (params = {}) => api.get('/api/adult/actresses', { params }),
  // 当前库 AdultItem.actors 字段的所有演员名 + 作品数；用于女优筛选下拉
  listLibraryActors: (libraryId) =>
    api.get('/api/adult/library-actors', { params: libraryId ? { library_id: libraryId } : {} }),

  // 清空 + 重扫 + 刮削（同一 task 内顺序执行）
  resetAndRescan: (libraryId) =>
    api.post('/api/adult/reset-and-rescan', null, { params: { library_id: libraryId } }),

  // 库统计（成人库详情页的 stats 卡用）
  stats: (libraryId) =>
    api.get('/api/adult/stats', { params: libraryId ? { library_id: libraryId } : {} }),
  // 单条「重新识别」（同步 fastpath，无对话框直接用当前 code 重抓）
  rescrapeItem: (id) => api.post(`/api/adult/items/${id}/rescrape`),
  // 手动设置/清除有码无码标志（value: true=无码 / false=有码 / null=恢复自动判定）
  setUncensored: (id, value) =>
    api.post(`/api/adult/items/${id}/uncensored`, { value }),

  // 排除 / 取消排除 —— 排除后自动流程跳过此条，防止反复刮削被 ban
  setExcluded: (id, excluded = true) =>
    api.post(`/api/adult/items/${id}/exclude`, { excluded }),
  // 清除元数据 + 标记排除（删本地 poster/nfo + 清 DB 字段，保留 file_path / code）
  clearAndExclude: (id) =>
    api.post(`/api/adult/items/${id}/clear-and-exclude`),
  // 重新识别对话框：搜索 / 应用所选候选
  identifySearch: (id, code) =>
    api.post(`/api/adult/items/${id}/identify-search`, { code }),
  identifyApply: (id, payload) =>
    api.post(`/api/adult/items/${id}/identify-apply`, payload),
  // 批量修复
  repairCovers: (libraryId) =>
    api.post('/api/adult/repair/covers', null, { params: libraryId ? { library_id: libraryId } : {} }),
  repairMetadata: (libraryId) =>
    api.post('/api/adult/repair/metadata', null, { params: libraryId ? { library_id: libraryId } : {} }),
  // watcher 触发本地扫描
  watcherRunNow: (libraryId) =>
    api.post('/api/adult/watcher/run-now', null, { params: libraryId ? { library_id: libraryId } : {} }),

  // 女优 toolbox 用
  actressResolveBatch: (names) =>
    api.post('/api/adult/actresses/resolve-batch', { names }),
  actressWorks: (id) => api.get(`/api/adult/actresses/${id}/works`),
}

// 内容推荐与下载 API
export const discoverApi = {
  trending: (params = {}) => api.get('/api/discover/trending', { params }),
  popular: (params = {}) => api.get('/api/discover/popular', { params }),
  // 通用分类列表（电影/剧集 × Popular/NowPlaying/Upcoming/TopRated/AiringToday/OnTheAir）
  list: (params = {}) => api.get('/api/discover/list', { params }),
  detail: (mediaType, tmdbId, params = {}) =>
    api.get('/api/discover/detail', { params: { media_type: mediaType, tmdb_id: tmdbId, ...params } }),
  clearCache: () => api.post('/api/discover/cache/clear'),
  search: (payload) => api.post('/api/resourcesearch/search', payload),
  push: (payload) => api.post('/api/downloadpipeline/download', payload),
  listDownloads: (params = {}) => api.get('/api/downloadpipeline/downloads', { params }),
  pause: (hash) => api.post(`/api/downloadpipeline/downloads/${hash}/pause`),
  resume: (hash) => api.post(`/api/downloadpipeline/downloads/${hash}/resume`),
  remove: (hash, deleteFiles = false) =>
    api.delete(`/api/downloadpipeline/downloads/${hash}`, { params: { delete_files: deleteFiles } }),
  // 批量操作（Phase H）
  bulkAction: (payload) => api.post('/api/downloadpipeline/downloads/bulk', payload),
  recheck: (hash) => api.post(`/api/downloadpipeline/downloads/${hash}/recheck`),
  reannounce: (hash) => api.post(`/api/downloadpipeline/downloads/${hash}/reannounce`),
  forceStart: (hash, force = true) =>
    api.post(`/api/downloadpipeline/downloads/${hash}/force-start`, { force }),
  retryDispatchRow: (hash) => api.post(`/api/dispatch/dispatch-map/${hash}/retry`),
  // 全局传输状态 + 限速控制
  transferInfo: () => api.get('/api/downloadpipeline/transfer-info'),
  setSpeedLimit: (payload) => api.post('/api/downloadpipeline/transfer-info/speed-limit', payload),
  toggleAltSpeed: () => api.post('/api/downloadpipeline/transfer-info/toggle-alt-speed'),
}

// 评分聚合 API（IMDB / RT / Metacritic / Trakt / Letterboxd / 豆瓣）
export const ratingsApi = {
  // 单条：缓存外会同步取 MDB List + 异步排队豆瓣
  get: (tmdb_id, media_type = 'movie', imdb_id = null) =>
    api.get('/api/ratings', {
      params: { tmdb_id, media_type, imdb_id: imdb_id || undefined },
    }),

  // 批量：仅返回缓存命中部分，缺失的项后台异步拉取
  // items: [{tmdb_id, media_type, imdb_id?, title?, year?}]
  batch: (items) => api.post('/api/ratings/batch', { items }),

  // 强制刷新（绕过 TTL）
  refresh: (tmdb_id, media_type = 'movie', imdb_id = null) =>
    api.post(`/api/ratings/${tmdb_id}/refresh`, null, {
      params: { media_type, imdb_id: imdb_id || undefined },
    }),
}

// 日志查看与级别控制
export const logsApi = {
  tail: (lines = 500, level = '') =>
    api.get('/api/logs/tail', { params: { lines, level: level || undefined } }),
  getLevel: () => api.get('/api/logs/level'),
  setLevel: (level) => api.post('/api/logs/level', { level }),
  files: () => api.get('/api/logs/files'),
}

// 配置相关 API
export const configApi = {
  get: () => api.get('/api/config'),
  getFull: () => api.get('/api/config/full'),
  saveFull: (data) => api.put('/api/config/full', { data }),
  getBackups: () => api.get('/api/config/backups'),
}

// 统计相关 API
export const statsApi = {
  getOverview: () =>
    api.get('/api/stats/overview'),

  getTaskHistory: (days = 7) =>
    api.get('/api/stats/tasks/history', { params: { days } }),

  getActorStats: () =>
    api.get('/api/stats/actors/stats'),

  getPosterStats: () =>
    api.get('/api/stats/posters/stats')
}

// 下载入库流水线 API
export const dispatchApi = {
  // 配额状态
  getQuota: () => api.get('/api/dispatch/quota'),
  cleanupNow: () => api.post('/api/dispatch/quota/cleanup-now'),
  softCleanupNow: () => api.post('/api/dispatch/quota/soft-cleanup'),
  // dispatch_map 行
  listItems: (params = {}) => api.get('/api/dispatch/items', { params }),
  // 加种预览 + 确认
  preview: (payload) => api.post('/api/dispatch/preview', payload, { timeout: 90000 }),
  confirm: (payload) => api.post('/api/dispatch/confirm', payload),
  cancelPreview: (hash) => api.delete(`/api/dispatch/preview/${hash}`),
  recomputeTarget: (payload) => api.post('/api/dispatch/preview/recompute-target', payload),
  // RSS 透传
  rssFeeds: () => api.get('/api/dispatch/rss/feeds'),
  rssRules: () => api.get('/api/dispatch/rss/rules'),
  rssMatching: (ruleName) => api.get(`/api/dispatch/rss/matching/${encodeURIComponent(ruleName)}`),
  rssRefresh: (itemPath) => api.post('/api/dispatch/rss/refresh', { item_path: itemPath }),
  rssRefreshAll: () => api.post('/api/dispatch/rss/refresh-all'),
  rssAddFeed: (url, path) => api.post('/api/dispatch/rss/add', { url, path }),
  rssRemove: (itemPath) => api.post('/api/dispatch/rss/remove', { item_path: itemPath }),
  rssMarkRead: (itemPath, articleId) =>
    api.post('/api/dispatch/rss/mark-read', { item_path: itemPath, article_id: articleId }),
  rssSettingsGet: () => api.get('/api/dispatch/rss/settings'),
  rssSettingsSet: (payload) => api.post('/api/dispatch/rss/settings', payload),
  rssRuleSet: (name, def) => api.post(`/api/dispatch/rss/rules/${encodeURIComponent(name)}`, def),
  rssLlmRegex: (description, sample_titles) =>
    api.post('/api/dispatch/rss/llm-regex', { description, sample_titles }, { timeout: 60000 }),
  rssRuleRename: (name, newName) =>
    api.post(`/api/dispatch/rss/rules/${encodeURIComponent(name)}/rename`, { new_name: newName }),
  rssRuleDelete: (name) => api.delete(`/api/dispatch/rss/rules/${encodeURIComponent(name)}`),
  // 待审核（adopt 孤儿种子认领）
  listNeedsReview: () => api.get('/api/dispatch/needs-review'),
  confirmNeedsReview: (hash, payload) => api.post(`/api/dispatch/needs-review/${hash}/confirm`, payload),
  dismissNeedsReview: (hash, deleteTorrent = false, deleteFiles = false) =>
    api.post(`/api/dispatch/needs-review/${hash}/dismiss`, null, {
      params: { delete_torrent: deleteTorrent, delete_files: deleteFiles },
    }),
  restoreDismissed: (hash) => api.post(`/api/dispatch/needs-review/${hash}/restore`),
  adoptScanNow: () => api.post('/api/dispatch/adopt/scan-now'),
  retryDispatchRow: (hash) => api.post(`/api/dispatch/dispatch-map/${hash}/retry`),
}

export default api

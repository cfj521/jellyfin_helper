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
    api.get('/api/jellyfin/libraries', { params: { check_paths: checkPaths } }),
  // params 支持 start_index / limit / item_type / search
  libraryItems: (id, params = {}) =>
    api.get(`/api/jellyfin/libraries/${id}/items`, { params }),
  // 库统计：内存级 2h 缓存。force=true 跳过缓存重算
  libraryStats: (id, force = false) =>
    api.get(`/api/jellyfin/libraries/${id}/stats`, {
      params: { force_refresh: force },
    }),
  // 懒加载缺字幕统计：复用 max_age_minutes 内的 subtitle_scan 任务，没有就启新的
  // force=true 时跳过最近任务复用，直接启新扫描
  librarySubtitleStats: (id, max_age_minutes = 60, force = false) =>
    api.get(`/api/jellyfin/libraries/${id}/subtitle-stats`, {
      params: { max_age_minutes, force_refresh: force },
    }),
  refreshLibrary: (id, mode = 'scan_changes') =>
    api.post(`/api/jellyfin/libraries/${id}/refresh`, null, { params: { mode } }),
  refreshAll: () =>
    api.post('/api/jellyfin/refresh-all'),

  // 剧集 → 季 → 集 钻取（懒加载，30 分钟缓存；force=true 旁路缓存）
  seasonsOfSeries: (seriesId, force = false) =>
    api.get(`/api/jellyfin/series/${seriesId}/seasons`, { params: { force } }),
  episodesOfSeason: (seasonId, force = false) =>
    api.get(`/api/jellyfin/seasons/${seasonId}/episodes`, { params: { force } }),
  // 清空 seasons/episodes 缓存（强制刷新按钮触发）
  clearChildrenCache: () =>
    api.post('/api/jellyfin/cache/clear-children'),

  // 批量取 Series 行的聚合摘要：季数/集数/总时长/字幕覆盖
  // payload: {series_ids: [...]}
  seriesAggregates: (seriesIds) =>
    api.post('/api/jellyfin/series/aggregates', { series_ids: seriesIds }),

  // 重新识别（刮削元数据）
  // payload: { item_type, name?, year?, tmdb_id?, language? }
  identifySearch: (itemId, payload) =>
    api.post(`/api/jellyfin/items/${itemId}/identify-search`, payload),

  // payload: { candidate, replace_all_images? }
  identifyApply: (itemId, payload) =>
    api.post(`/api/jellyfin/items/${itemId}/identify-apply`, payload),

  // Sample 取证：返回 verdict + 证据明细（路径关键字 / 时长 / 兄弟文件大小对比）
  sampleEvidence: (itemId) =>
    api.get(`/api/jellyfin/items/${itemId}/sample-evidence`),

  // 删除条目（连同物理文件，用户须有 EnableContentDeletion 权限）
  deleteItem: (itemId) =>
    api.delete(`/api/jellyfin/items/${itemId}`),

  systemInfo: () =>
    api.get('/api/jellyfin/system'),
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
  scan: (path, libraryId) => api.post('/api/adult/scan', { path, library_id: libraryId }),
  scrapeOne: (code) => api.post(`/api/adult/scrape/${code}`),
  scrapeBatch: (payload) => api.post('/api/adult/scrape/batch', payload),
  regenerateNfo: (id) => api.post(`/api/adult/items/${id}/nfo`),
  syncFromJellyfin: (id) => api.post(`/api/adult/items/${id}/sync-from-jellyfin`),
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
  search: (payload) => api.post('/api/discover/search', payload),
  push: (payload) => api.post('/api/discover/download', payload),
  listDownloads: (params = {}) => api.get('/api/discover/downloads', { params }),
  pause: (hash) => api.post(`/api/discover/downloads/${hash}/pause`),
  resume: (hash) => api.post(`/api/discover/downloads/${hash}/resume`),
  remove: (hash, deleteFiles = false) =>
    api.delete(`/api/discover/downloads/${hash}`, { params: { delete_files: deleteFiles } }),
  syncCompleted: () => api.post('/api/discover/sync-completed'),
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

export default api

import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    redirect: '/jellyfin/libraries',
  },
  // 媒体库维护
  {
    path: '/maintenance/cleanup-samples',
    name: 'CleanupSamples',
    component: () => import('@/views/maintenance/CleanupSamples.vue'),
    meta: { title: '清理 Sample 内容' }
  },
  {
    path: '/maintenance/normalize-paths',
    name: 'NormalizePaths',
    component: () => import('@/views/maintenance/NormalizePaths.vue'),
    meta: { title: '扫描并规范路径' }
  },
  {
    path: '/maintenance/auto-identify',
    name: 'AutoIdentify',
    component: () => import('@/views/maintenance/AutoIdentify.vue'),
    meta: { title: '扫描并修复识别错误' }
  },
  // 演员管理（侧边栏已移除，从首页元数据卡片跳进）
  {
    path: '/metadata/actors',
    name: 'Actors',
    component: () => import('@/views/metadata/Actors.vue'),
    meta: { title: '演员管理' }
  },
  // 媒体库（Jellyfin 库总览 + 详情）
  {
    path: '/jellyfin/libraries',
    name: 'JellyfinLibraries',
    component: () => import('@/views/jellyfin/Libraries.vue'),
    meta: { title: '媒体库' }
  },
  {
    path: '/jellyfin/libraries/:id',
    name: 'JellyfinLibraryDetail',
    component: () => import('@/views/jellyfin/LibraryDetail.vue'),
    meta: { title: '库详情' }
  },
  // 内容推荐与下载
  {
    path: '/discover/trending',
    name: 'Trending',
    component: () => import('@/views/discover/Trending.vue'),
    meta: { title: '热门推荐' }
  },
  {
    path: '/discover/detail/:mediaType/:tmdbId',
    name: 'DiscoverDetail',
    component: () => import('@/views/discover/Detail.vue'),
    meta: { title: '影视详情' }
  },
  {
    path: '/discover/search',
    name: 'TorrentSearch',
    component: () => import('@/views/discover/Search.vue'),
    meta: { title: '种子搜索' }
  },
  {
    path: '/discover/downloads',
    name: 'Downloads',
    component: () => import('@/views/discover/Downloads.vue'),
    meta: { title: '下载管理' }
  },
  // 成人内容
  {
    path: '/adult/library',
    name: 'AdultLibrary',
    component: () => import('@/views/adult/Library.vue'),
    meta: { title: '番号库', adult: true }
  },
  // 任务管理
  {
    path: '/tasks',
    name: 'Tasks',
    component: () => import('@/views/Tasks.vue'),
    meta: { title: '任务管理' }
  },
  {
    path: '/tasks/:id',
    name: 'TaskDetail',
    component: () => import('@/views/TaskDetail.vue'),
    meta: { title: '任务详情' }
  },
  // 设置
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('@/views/Settings.vue'),
    meta: { title: '设置' }
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

export default router

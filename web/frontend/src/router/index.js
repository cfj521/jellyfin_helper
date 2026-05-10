import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    redirect: '/medialibraries',
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
  // 媒体库（总览 + 详情）
  {
    path: '/medialibraries',
    name: 'MediaLibraries',
    component: () => import('@/views/medialibraries/Libraries.vue'),
    meta: { title: '媒体库' }
  },
  {
    path: '/medialibraries/:id',
    name: 'MediaLibraryDetail',
    component: () => import('@/views/medialibraries/LibraryDetail.vue'),
    meta: { title: '库详情' }
  },
  // 热门推荐
  {
    path: '/discover',
    name: 'Discover',
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
    path: '/discover/anilist/:anilistId',
    name: 'DiscoverAniListDetail',
    component: () => import('@/views/discover/AniListDetail.vue'),
    meta: { title: '番剧详情' }
  },
  {
    path: '/discover/douban/:doubanId',
    name: 'DiscoverDoubanDetail',
    component: () => import('@/views/discover/DoubanDetail.vue'),
    meta: { title: '豆瓣详情' }
  },
  // 资源搜索
  {
    path: '/resourcesearch',
    name: 'ResourceSearch',
    component: () => import('@/views/resourcesearch/ResourceSearch.vue'),
    meta: { title: '资源搜索' }
  },
  // 下载流水线
  {
    path: '/downloadpipeline',
    name: 'DownloadPipeline',
    component: () => import('@/views/downloadpipeline/DownloadPipeline.vue'),
    meta: { title: '下载流水线' }
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

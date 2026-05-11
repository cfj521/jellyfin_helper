<template>
  <div class="page-container">
    <!-- 顶栏：返回 + 库名 + 操作 -->
    <div class="page-header">
      <div class="header-left">
        <el-button link @click="$router.push('/medialibraries')">
          <el-icon><ArrowLeft /></el-icon>
          返回
        </el-button>
        <h2>
          <el-icon class="lib-icon"><component :is="typeIcon" /></el-icon>
          {{ library?.name || '加载中…' }}
        </h2>
        <el-tag v-if="library" :type="collectionTypeTagType(library.collection_type)" size="small">
          {{ collectionTypeLabel(library.collection_type) }}
        </el-tag>
      </div>
      <div class="header-right">
        <el-button @click="forceRefresh" :loading="loading || loadingStats || subtitleStatsLoading">
          <el-icon><Refresh /></el-icon>
          强制刷新
        </el-button>
        <el-button @click="showDupDialog = true" :disabled="!library?.locations.length">
          <el-icon><Search /></el-icon>
          重复检测
        </el-button>
        <el-button type="warning" @click="showRefreshDialog = true" :loading="refreshing">
          <el-icon><MagicStick /></el-icon>
          通知 Jellyfin 重扫
        </el-button>
      </div>
    </div>

    <!-- 成人库：完全切到自己的视图（自带 toolbar / paths / stats / filter / table） -->
    <AdultLibraryView v-if="library?.is_adult" :library="library" />

    <!-- 普通库：以下是原有的 toolbar / paths / stats / 内容预览 -->
    <template v-if="library && !library.is_adult">

    <!-- 媒体处理工具栏：作用范围 = 当前库 / 选中条目 -->
    <MediaToolbar
      v-if="library"
      :scope="toolbarScope"
      @clear-selection="clearSelection"
    />

    <!-- 顶部并列：媒体路径 + 统计 -->
    <div v-if="library" class="top-row">
      <!-- 路径列表卡片 -->
      <el-card shadow="never" class="paths-card">
        <template #header>
          <div class="card-header">
            <span>媒体路径</span>
            <el-tag size="small" type="info">{{ library.locations.length }} 个</el-tag>
          </div>
        </template>
        <div class="paths-list">
          <div v-for="(loc, idx) in library.locations_status || library.locations.map(p => ({ path: p, accessible: true }))"
               :key="idx" class="path-row">
            <el-icon :class="loc.accessible ? 'ok' : 'fail'">
              <component :is="loc.accessible ? 'Check' : 'Close'" />
            </el-icon>
            <span class="path-text">{{ loc.path || loc }}</span>
            <el-tag v-if="loc.accessible === false" type="danger" size="small">本机不可访问</el-tag>
          </div>
        </div>
      </el-card>

      <!-- 统计卡片：基础 4 项 + 4 项可选（用户在右上齿轮里勾选，按库持久化）-->
      <el-card shadow="never" class="stats-card">
        <template #header>
          <div class="card-header">
            <span>
              统计
              <span v-if="stats?._cached" class="cache-hint">
                · 缓存于 {{ formatCacheAge(stats._cache_age_seconds) }}前
              </span>
            </span>
            <el-popover trigger="click" placement="bottom-end" :width="220">
              <template #reference>
                <el-button text size="small" title="显示项设置">
                  <el-icon><Setting /></el-icon>
                </el-button>
              </template>
              <div class="stats-toggle-list">
                <div class="stats-toggle-title">本库显示项</div>
                <el-checkbox v-model="visibleStats.health">总体健康度</el-checkbox>
                <el-checkbox v-model="visibleStats.poster">缺海报</el-checkbox>
                <el-checkbox v-model="visibleStats.subtitle">字幕覆盖</el-checkbox>
                <el-checkbox v-model="visibleStats.tmdb">TMDB 绑定</el-checkbox>
              </div>
            </el-popover>
          </div>
        </template>

        <div v-if="loadingStats && !stats" class="loading-block">
          <el-icon class="spin"><Loading /></el-icon> 加载统计中...
        </div>
        <div v-else-if="statsError" class="error-block">
          <el-alert :title="`加载失败：${statsError}`" type="error" :closable="false" show-icon />
        </div>
        <div v-else-if="stats" class="stats-grid">
          <div v-for="m in metrics" :key="m.label" class="stat-card" :class="{ warn: m.warn }">
            <span class="stat-label">{{ m.label }}</span>
            <span class="stat-value" :style="m.color ? { color: m.color } : null">
              {{ m.value }}<small v-if="m.suffix">{{ m.suffix }}</small>
            </span>
          </div>
        </div>
      </el-card>
    </div>

    <!-- 内容预览：直接展示在页面下方（替代原来的 tabs 默认页） -->
    <el-card shadow="never" class="items-card">
      <template #header>
        <div class="card-header">
          <!-- 排序栏 -->
          <div class="sort-bar">
            <span class="sort-label">排序：</span>
            <button
              v-for="opt in sortOptions"
              :key="opt.field"
              :class="['sort-chip', { active: sortField === opt.field }]"
              @click="setSort(opt.field)"
            >
              {{ opt.label }}
              <el-icon v-if="sortField === opt.field" class="sort-arrow">
                <CaretTop v-if="sortDir === 'asc'" />
                <CaretBottom v-else />
              </el-icon>
            </button>
          </div>

          <!-- 搜索框：跨整库按名称模糊搜（透传 Jellyfin SearchTerm，服务端做匹配） -->
          <el-input
            v-model="searchInput"
            placeholder="按标题搜索本库..."
            clearable
            size="small"
            style="width: 220px"
            @keyup.enter="onSearchSubmit"
            @clear="onSearchSubmit"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>

          <!-- 年份过滤：多选，change 即提交 -->
          <el-select
            v-model="searchYears"
            multiple
            collapse-tags
            collapse-tags-tooltip
            filterable
            allow-create
            placeholder="年份"
            size="small"
            style="width: 160px"
            @change="onSearchSubmit"
          >
            <el-option
              v-for="y in yearOptions"
              :key="y"
              :label="String(y)"
              :value="String(y)"
            />
          </el-select>

          <!-- 风格过滤：多选；options 来自后端拉取的库内 Genres -->
          <el-select
            v-model="searchGenres"
            multiple
            collapse-tags
            collapse-tags-tooltip
            filterable
            placeholder="风格/类型"
            size="small"
            style="width: 200px"
            @change="onSearchSubmit"
            @visible-change="onGenrePopoverOpen"
          >
            <el-option
              v-for="g in genreOptions"
              :key="g"
              :label="g"
              :value="g"
            />
          </el-select>

          <!-- 右侧组：进度统计 + Folder 开关 -->
          <div class="header-right-group">
            <!-- 无限滚动：展示"已加载 X / 共 Y"，替代原分页器 -->
            <span v-if="itemsTotal > 0" class="items-progress">
              已加载 {{ items.length }} / 共 {{ itemsTotal }}
            </span>
            <!-- 忽略 Folder 开关：与 Jellyfin Web 默认行为对齐 -->
            <div class="toggle-folder">
              <span class="switch-label">忽略 Folder</span>
              <el-switch v-model="hideFolders" size="small" />
              <el-tooltip placement="top">
                <template #content>
                  Jellyfin Web 默认隐藏 type=Folder 的未识别条目。<br/>
                  打开后行为对齐 Jellyfin（仅在本工具中作为查找视图）。
                </template>
                <el-icon class="hint-icon"><InfoFilled /></el-icon>
              </el-tooltip>
            </div>
            <ViewModeToggle v-model="viewMode" />
          </div>
        </div>
      </template>

      <div v-if="itemsLoading" class="loading-block">
        <el-icon class="spin"><Loading /></el-icon> 加载中...
      </div>

      <!-- 网格视图：电影 / 剧集卡片（只显顶层项；树状子节点在 list 模式下展开） -->
      <div v-else-if="viewMode === 'grid'" ref="gridViewRef" class="grid-view">
        <!-- 骨架卡片：wanted 推进了但数据池还没补到，先撑出占位行（Trending 同款 shimmer） -->
        <div
          v-for="row in displayItems"
          :key="row.id"
          class="grid-card"
          :class="{
            'grid-card--problem': !row._skeleton && row.health?.level === 'error',
            'grid-card--skeleton': row._skeleton,
          }"
          @click="!row._skeleton && onGridCardClick(row)"
        >
          <template v-if="row._skeleton">
            <div class="grid-poster-wrap">
              <div class="sk-block sk-poster" />
            </div>
            <div class="grid-meta">
              <div class="sk-line sk-title" />
              <div class="sk-line sk-year" />
            </div>
          </template>
          <template v-else>
            <div class="grid-poster-wrap">
              <!-- 网格视图用 16:9：优先 backdrop（Movie/Series 横版背景图）/ thumb（Episode 剧照）；
                   没有则用 Primary 海报（2:3）裁剪填充——避免空缺，但视觉上会糊一点 -->
              <el-image
                v-if="row.backdrop_url || row.poster_url"
                :src="row.backdrop_url || row.poster_url"
                :alt="row.name"
                fit="cover"
                lazy
                class="grid-poster"
              >
                <template #error>
                  <div class="grid-placeholder">{{ row.name?.slice(0, 2) || '?' }}</div>
                </template>
              </el-image>
              <div v-else class="grid-placeholder">{{ row.name?.slice(0, 2) || '?' }}</div>
              <span
                v-if="row.health?.level && row.health.level !== 'ok'"
                class="grid-health-dot"
                :class="`grid-health-dot--${row.health.level}`"
                :title="(row.health.issues || []).map(i => i.label).join('\n')"
              />
            </div>
            <div class="grid-meta">
              <div class="grid-title" :title="row.name">{{ row.name }}</div>
              <div v-if="row.year" class="grid-year">{{ row.year }}</div>
            </div>
          </template>
        </div>
        <el-empty v-if="!displayItems.length" description="此库还没有内容" />
      </div>

      <el-table
        v-else
        ref="itemsTable"
        :data="displayItems"
        stripe
        size="small"
        row-key="id"
        lazy
        :load="loadChildren"
        :tree-props="{ children: '_children', hasChildren: 'has_children' }"
        :indent="32"
        :row-class-name="rowClassName"
      >
        <!-- 行号列：仅顶层行（Series/Movie）显示序号，Season/Episode 子行留空 -->
        <el-table-column label="#" width="56" align="center" class-name="col-row-index">
          <template #default="{ row, $index }">
            <span v-if="(row.level || 0) === 0" class="row-index">{{ $index + 1 }}</span>
          </template>
        </el-table-column>

        <!--
          ============ 左侧大 cell（合并 选择/展开/海报/标题）============
          整行内容用一个 div 包起来，padding-left 按 row.level 缩进
            level 0 (Series):  padding-left = 16
            level 1 (Season):  padding-left = 16 + 32 = 48
            level 2 (Episode): padding-left = 16 + 64 = 80
          checkbox / chevron / 海报 / 标题 都在这个 div 内，整体右移
        -->
        <el-table-column min-width="500">
          <template #header>
            <div class="row-content row-content--header">
              <el-checkbox
                :model-value="allSelected"
                :indeterminate="someSelected && !allSelected"
                @change="onToggleAll"
              />
              <span class="hdr-spacer" />
              <span class="hdr-label">海报</span>
              <span class="hdr-label hdr-label--title">标题</span>
            </div>
          </template>
          <template #default="{ row }">
            <div class="row-content" :style="{ paddingLeft: `${16 + (row.level || 0) * 32}px` }">
              <!-- 选择框 -->
              <el-checkbox
                :model-value="isRowSelected(row)"
                @change="(v) => toggleRowSelection(row, v)"
                @click.stop
              />
              <!-- 展开/折叠 chevron（无子节点时占等宽空白） -->
              <button
                v-if="row.has_children"
                :class="['row-chevron', { 'row-chevron--expanded': expandedSet.has(row.id) }]"
                @click.stop="toggleRowExpand(row)"
              />
              <span v-else class="row-chevron-spacer" />
              <!-- 海报缩略图 -->
              <a
                v-if="row.detail_url"
                :href="row.detail_url"
                target="_blank"
                rel="noopener noreferrer"
                class="poster-thumb"
                @click.stop
              >
                <el-image
                  v-if="row.poster_url"
                  :src="row.poster_url"
                  :alt="row.name"
                  fit="cover"
                  lazy
                  :class="['poster-img', `poster-img--${(row.type || '').toLowerCase()}`]"
                >
                  <template #error>
                    <div class="poster-placeholder">无图</div>
                  </template>
                </el-image>
                <div v-else :class="['poster-placeholder', `poster-img--${(row.type || '').toLowerCase()}`]">
                  无图
                </div>
              </a>
              <div v-else class="poster-thumb">
                <div :class="['poster-placeholder', `poster-img--${(row.type || '').toLowerCase()}`]">
                  无图
                </div>
              </div>
              <!-- 标题 -->
              <a
                v-if="row.detail_url"
                :href="row.detail_url"
                target="_blank"
                rel="noopener noreferrer"
                :class="['item-link', `title--${(row.type || '').toLowerCase()}`]"
                @click.stop
              >{{ rowDisplayTitle(row) }}</a>
              <span v-else :class="`title--${(row.type || '').toLowerCase()}`">{{ rowDisplayTitle(row) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="年份" width="72" fixed="right">
          <template #default="{ row }">
            <span v-if="row.year">{{ row.year }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <!-- 时长：Movie / Episode 显示单作品时长；Series 显示总时长（聚合后才有）；Season 显示 — -->
        <el-table-column label="时长" width="100" align="center" fixed="right">
          <template #default="{ row }">
            <span v-if="(row.type === 'Episode' || row.type === 'Movie') && row.runtime_min">
              {{ formatRuntimeMin(row.runtime_min) }}
            </span>
            <span v-else-if="row.type === 'Series' && row.total_runtime_min">
              {{ formatTotalRuntime(row.total_runtime_min) }}
            </span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="健康" width="140" fixed="right">
          <template #default="{ row }">
            <div class="health-cell health-cell--problem">
              <!-- 第一行：状态点 + 错误码 / 正常 -->
              <div class="health-line-top">
                <span
                  :class="['health-dot', `level-${row.health?.level || 'ok'}`]"
                  :title="row.health?.level === 'ok' ? '健康' : ''"
                />
                <span v-if="row.health?.level === 'ok'" class="muted">正常</span>
                <el-tooltip v-else placement="right">
                  <template #content>
                    <div class="health-tooltip">
                      <div
                        v-for="(iss, idx) in row.health.issues"
                        :key="idx"
                        class="health-tooltip-line"
                      >• {{ iss.label }}</div>
                    </div>
                  </template>
                  <span class="health-codes-inline">
                    <span
                      v-for="code in summaryCodes(row.health.issues)"
                      :key="code"
                      :class="['health-code-tag', `code-${code}`]"
                    >{{ codeShortLabel(code) }}</span>
                  </span>
                </el-tooltip>
              </div>

              <!-- 第二行：操作按钮（Series/Movie 显示重新识别 + 删除；Episode 显示修缩略图 + 删除；Season 不显示）-->
              <div v-if="row.type !== 'Season'" class="health-actions">
                <button
                  v-if="row.type !== 'Episode'"
                  class="row-btn row-btn--primary"
                  @click.stop="openIdentify(row)"
                >
                  重新识别
                </button>
                <!-- Episode 专属：修缩略图（无图或者用户主动想换）-->
                <button
                  v-if="row.type === 'Episode'"
                  class="row-btn row-btn--primary"
                  :disabled="row._fixingStill"
                  @click.stop="fixSingleStill(row)"
                >
                  {{ row.has_image ? '换缩略图' : '修缩略图' }}
                </button>
                <button
                  v-if="isSampleSuspect(row)"
                  class="row-btn row-btn--danger"
                  @click.stop="openSampleDelete(row, 'sample')"
                >
                  清除 Sample
                </button>
                <button
                  v-else-if="isUnrecognized(row)"
                  class="row-btn row-btn--danger"
                  @click.stop="openSampleDelete(row, 'unrecognized')"
                >
                  删除
                </button>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="type" label="类型" width="70" fixed="right">
          <template #default="{ row }">
            <el-tag size="small" :type="typeTagType(row.type)">
              {{ typeLabel(row.type) }}
            </el-tag>
          </template>
        </el-table-column>
        <!-- 集数：仅对剧集 / 混合库显示（电影库无集数概念） -->
        <el-table-column
          v-if="library?.collection_type !== 'movies'"
          label="集数"
          width="100"
          align="center"
          fixed="right"
        >
          <template #default="{ row }">
            <div v-if="row.type === 'Series' && row.child_count != null" class="count-stack">
              <span>{{ row.child_count }} 季</span>
              <span v-if="row.episode_count != null" class="count-sub">{{ row.episode_count }} 集</span>
            </div>
            <span v-else-if="row.type === 'Season' && row.child_count != null">
              {{ row.child_count }} 集
            </span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <!-- 字幕语言：仅 visibleStats.subtitle 开启时显示（与 stats 卡的"字幕覆盖"同开关） -->
        <el-table-column
          v-if="visibleStats.subtitle"
          label="字幕"
          width="130"
          fixed="right"
        >
          <template #default="{ row }">
            <div class="sub-cell">
              <!-- 第一行：已有字幕语言 chip -->
              <div class="sub-lang-row">
                <el-tag
                  v-for="l in (row.subtitle_langs || []).slice(0, 3)"
                  :key="l"
                  size="small"
                  :type="subLangTagType(l)"
                  effect="light"
                  class="sub-lang-chip"
                >{{ subLangLabel(l) }}</el-tag>
                <el-tooltip
                  v-if="(row.subtitle_langs?.length || 0) > 3"
                  :content="row.subtitle_langs.slice(3).map(subLangLabel).join(' / ')"
                  placement="top"
                >
                  <span class="sub-lang-more">+{{ row.subtitle_langs.length - 3 }}</span>
                </el-tooltip>
                <span v-if="!row.subtitle_langs?.length" class="muted">—</span>
              </div>

              <!-- 第二行：下载字幕按钮（仅 Movie / Episode） -->
              <el-button
                v-if="(row.type === 'Movie' || row.type === 'Episode') && row.path"
                size="small"
                text
                type="primary"
                class="sub-dl-btn"
                @click.stop="openSubtitleDownload(row)"
              >
                <el-icon><Search /></el-icon>
                下载字幕
              </el-button>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="风格/类型" width="130" fixed="right">
          <template #default="{ row }">
            <div v-if="row.genres?.length" class="genre-cell">
              <el-tag
                v-for="g in row.genres.slice(0, 2)"
                :key="g"
                size="small"
                effect="plain"
                class="genre-chip"
              >{{ g }}</el-tag>
              <el-tooltip v-if="row.genres.length > 2" :content="row.genres.slice(2).join(' / ')" placement="top">
                <span class="genre-more">+{{ row.genres.length - 2 }}</span>
              </el-tooltip>
            </div>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="评分" width="120" fixed="right">
          <template #default="{ row }">
            <div class="rating-cell">
              <span v-if="row.community_rating != null" class="rating jf-rating" title="Jellyfin 社区评分">
                <el-icon><Star /></el-icon>
                {{ row.community_rating.toFixed(1) }}
              </span>
              <!-- 多源评分：Movie / Series 都拉（Episode/Season 没独立 TMDB ID）-->
              <RatingsBadges
                v-if="(row.type === 'Series' || row.type === 'Movie') && row.tmdb_id"
                compact
                :rating="ratingFor(row)"
              />
              <!-- 字幕覆盖（只 Series 显示，best-effort：来自最近一次 subtitle_scan）-->
              <span
                v-if="row.type === 'Series' && row.subtitle_coverage"
                :class="['subtitle-coverage-chip', subtitleCoverageClass(row.subtitle_coverage.coverage_pct)]"
                :title="`字幕覆盖：${row.subtitle_coverage.with_required} / ${row.subtitle_coverage.total_videos} 集`"
              >
                字幕 {{ row.subtitle_coverage.coverage_pct }}%
              </span>
              <span
                v-if="row.community_rating == null
                  && !((row.type === 'Series' || row.type === 'Movie') && ratingFor(row))"
                class="muted"
              >—</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="TMDB" width="90" align="center" fixed="right">
          <template #default="{ row }">
            <!-- Episode 没有独立 TMDB ID，直接 — -->
            <span v-if="row.type === 'Episode'" class="muted">—</span>
            <a
              v-else-if="row.tmdb_id"
              :href="tmdbUrl(row)"
              target="_blank"
              rel="noopener noreferrer"
              class="tmdb-link"
              @click.stop
            >
              <el-icon><Link /></el-icon>
              {{ row.tmdb_id }}
            </a>
            <el-tag v-else type="info" size="small" effect="plain">未绑定</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="文件路径" width="180" show-overflow-tooltip fixed="right">
          <template #default="{ row }">
            <div class="path-cell">
              <span class="path-text mono" :title="row.path">{{ row.path || '—' }}</span>
              <el-button
                v-if="row.path"
                text
                size="small"
                class="path-copy-btn"
                title="复制路径到剪贴板"
                @click.stop="copyPath(row.path)"
              >
                <el-icon><DocumentCopy /></el-icon>
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <!-- 无限滚动哨兵：仅作 getBoundingClientRect 的位置锚点，不显示任何文字 -->
      <div ref="sentinelRef" class="scroll-sentinel" aria-hidden="true"></div>

      <el-empty v-if="!itemsLoading && !items.length" description="暂无内容" />
    </el-card>

    </template>
    <!-- /普通库视图 -->

    <!-- 重复检测对话框 -->
    <el-dialog v-model="showDupDialog" title="重复检测" width="780" :close-on-click-modal="false" :close-on-press-escape="false">
      <!-- 检测模式 -->
      <div class="dup-mode-pick">
        <span class="dup-pick-label">检测模式：</span>
        <el-radio-group v-model="dupMode" size="small">
          <el-radio label="metadata">
            Jellyfin 元数据
            <el-tooltip placement="top">
              <template #content>
                <div>按 TMDB ID / IMDB ID / 标题+年份 / 同剧同集 分组</div>
                <div>识别"同一作品的不同清晰度版本"等真正重复</div>
                <div>不扫盘，瞬时返回</div>
              </template>
              <el-icon class="info-ic"><InfoFilled /></el-icon>
            </el-tooltip>
          </el-radio>
          <el-radio label="hash">
            文件 byte hash
            <el-tooltip placement="top" content="按 byte 大小+首尾 64KB hash 判定，识别完全相同的两个文件；扫盘较慢">
              <el-icon class="info-ic"><InfoFilled /></el-icon>
            </el-tooltip>
          </el-radio>
        </el-radio-group>
      </div>

      <div v-if="dupMode === 'hash' && library?.locations.length > 1" class="dup-path-pick">
        <span class="dup-pick-label">检测路径：</span>
        <el-radio-group v-model="dupPath" size="small">
          <el-radio v-for="loc in library.locations" :key="loc" :label="loc">{{ loc }}</el-radio>
          <el-radio label="__all__">全部路径</el-radio>
        </el-radio-group>
      </div>

      <!-- metadata 模式结果 -->
      <div v-if="dupResult && dupMode === 'metadata'" class="dup-result">
        <div class="dup-summary">
          <el-tag>电影 {{ dupResult.total_movies }} · 剧集 {{ dupResult.total_episodes }}</el-tag>
          <el-tag :type="dupResult.potential_duplicates > 0 ? 'warning' : 'success'">
            重复组: {{ dupResult.potential_duplicates }}
            <span v-if="dupResult.movie_dup_groups || dupResult.episode_dup_groups">
              （电影 {{ dupResult.movie_dup_groups }} · 剧集 {{ dupResult.episode_dup_groups }}）
            </span>
          </el-tag>
        </div>

        <el-collapse v-if="dupResult.groups?.length" class="dup-groups">
          <el-collapse-item
            v-for="(group, idx) in dupResult.groups"
            :key="dupGroupKey(group, idx)"
            :name="dupGroupKey(group, idx)"
          >
            <template #title>
              <div class="dup-group-title">
                <el-tag size="small" :type="dupTagType(group.match_type)">
                  {{ dupMatchLabel(group.match_type) }}
                </el-tag>
                <div class="dup-group-name-stack">
                  <span class="dup-group-name">{{ groupHeadline(group) }}</span>
                  <span v-if="groupSubline(group)" class="dup-group-sub">
                    {{ groupSubline(group) }}
                  </span>
                </div>
                <span class="dup-group-count">{{ group.files.length }} 个文件</span>
              </div>
            </template>

            <el-radio-group
              v-model="dupKeepMap[dupGroupKey(group, idx)]"
              class="file-list"
            >
              <label
                v-for="file in group.files"
                :key="dupFileKey(file)"
                class="file-row"
              >
                <el-radio :label="dupFileKey(file)" class="file-radio">保留</el-radio>
                <div class="file-meta">
                  <div class="file-name">
                    {{ file.name }}
                    <span v-if="file.version_label && file.version_label !== file.name" class="version-label">[{{ file.version_label }}]</span>
                  </div>
                  <div class="file-path">{{ file.path }}</div>
                </div>
                <el-tag size="small" class="file-size">{{ formatSize(file.size) }}</el-tag>
              </label>
            </el-radio-group>

            <div class="dup-group-actions">
              <el-button
                size="small"
                type="danger"
                :disabled="!canDeleteOthers(group, idx)"
                :loading="dupDeleting[dupGroupKey(group, idx)] || false"
                @click="deleteOthersInGroup(group, idx)"
              >
                <el-icon><Delete /></el-icon>
                删除其它 {{ group.files.length - 1 }} 项（保留勾选的）
              </el-button>
              <span class="dup-group-hint">
                Jellyfin DELETE 会同时移除物理文件（需 EnableContentDeletion 权限）
              </span>
            </div>
          </el-collapse-item>
        </el-collapse>

        <el-empty v-else description="未发现重复条目" />
      </div>

      <!-- hash 模式结果（同样支持删除其它，文件按 path 反查 jellyfin item） -->
      <div v-else-if="dupResult && dupMode === 'hash'" class="dup-result">
        <div class="dup-summary">
          <el-tag>视频总数: {{ dupResult.total_videos }}</el-tag>
          <el-tag :type="dupResult.potential_duplicates > 0 ? 'warning' : 'success'">
            潜在重复组: {{ dupResult.potential_duplicates }}
          </el-tag>
        </div>

        <el-collapse v-if="dupResult.groups?.length" class="dup-groups">
          <el-collapse-item
            v-for="(group, idx) in dupResult.groups"
            :key="dupGroupKey(group, idx)"
            :name="dupGroupKey(group, idx)"
          >
            <template #title>
              <div class="dup-group-title">
                <el-tag size="small" :type="dupTagType(group.match_type)">
                  {{ dupMatchLabel(group.match_type) }}
                </el-tag>
                <div class="dup-group-name-stack">
                  <span class="dup-group-name">{{ groupHeadline(group) }}</span>
                  <span v-if="groupSubline(group)" class="dup-group-sub">
                    {{ groupSubline(group) }}
                  </span>
                </div>
                <span class="dup-group-count">{{ group.files.length }} 个文件</span>
              </div>
            </template>

            <el-radio-group
              v-model="dupKeepMap[dupGroupKey(group, idx)]"
              class="file-list"
            >
              <label
                v-for="file in group.files"
                :key="dupFileKey(file)"
                class="file-row"
              >
                <el-radio :label="dupFileKey(file)" class="file-radio">保留</el-radio>
                <div class="file-meta">
                  <div class="file-name">{{ file.name }}</div>
                  <div class="file-path">{{ file.path }}</div>
                </div>
                <el-tag size="small" class="file-size">{{ formatSize(file.size) }}</el-tag>
              </label>
            </el-radio-group>

            <div class="dup-group-actions">
              <el-button
                size="small"
                type="danger"
                :disabled="!canDeleteOthers(group, idx)"
                :loading="dupDeleting[dupGroupKey(group, idx)] || false"
                @click="deleteOthersInGroup(group, idx)"
              >
                <el-icon><Delete /></el-icon>
                删除其它 {{ group.files.length - 1 }} 项（保留勾选的）
              </el-button>
              <span class="dup-group-hint">
                hash 模式：通过路径反查 Jellyfin Item 后删除（含物理文件）
              </span>
            </div>
          </el-collapse-item>
        </el-collapse>

        <el-empty v-else description="未发现重复文件" />
      </div>

      <el-empty v-else-if="!dupLoading" description="点击「开始检测」开始" />

      <template #footer>
        <el-button @click="showDupDialog = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="dupLoading"
          :disabled="!library?.locations.length && dupMode === 'hash'"
          @click="findDuplicates"
        >
          {{ dupResult ? '重新检测' : '开始检测' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 刷新模式选择对话框 -->
    <RefreshLibraryDialog
      v-model="showRefreshDialog"
      :library-name="library?.name"
      :loading="refreshing"
      @confirm="onRefreshConfirm"
    />

    <!-- 重新识别（刮削元数据）对话框 -->
    <IdentifyDialog
      v-model="showIdentifyDialog"
      :item="identifyTarget"
      @applied="onIdentifyApplied"
    />

    <!-- 删除 / 清除 Sample 对话框（共用） -->
    <SampleDeleteDialog
      v-model="showSampleDeleteDialog"
      :item-id="sampleDeleteTargetId"
      :mode="sampleDeleteMode"
      @deleted="onSampleDeleted"
    />

    <!-- 字幕下载对话框（assrt 单视频搜索 + 下载） -->
    <SubtitleDownloadDialog
      v-model="showSubDownloadDialog"
      :item="subDownloadTarget"
      @downloaded="onSubtitleDownloaded"
    />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft, Refresh, MagicStick, Loading, Check, Close, Search, Link, Star,
  VideoCamera, VideoPlay, Headset, Folder, Setting, Delete, DocumentCopy,
  CaretTop, CaretBottom, InfoFilled,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { jellyfinApi, mediaApi, taskApi, ratingsApi, metadataApi } from '@/api'
import { debugInfo } from '@/composables/useDebugInfo'
import ViewModeToggle from '@/components/ViewModeToggle.vue'
import SubtitleDownloadDialog from '@/components/SubtitleDownloadDialog.vue'
import { useViewMode } from '@/composables/useViewMode'
import RefreshLibraryDialog from '@/components/RefreshLibraryDialog.vue'
import AdultLibraryView from '@/views/medialibraries/AdultLibraryView.vue'
import MediaToolbar from '@/components/MediaToolbar.vue'
import IdentifyDialog from '@/components/IdentifyDialog.vue'
import SampleDeleteDialog from '@/components/SampleDeleteDialog.vue'
import RatingsBadges from '@/components/RatingsBadges.vue'

const route = useRoute()
const router = useRouter()

const id = computed(() => String(route.params.id))

// 库基础信息
const library = ref(null)
const loading = ref(false)

// 概览统计
const stats = ref(null)
const loadingStats = ref(false)
const statsError = ref('')

// 统计卡每个可选指标的可见性（按库持久化在 localStorage）
const visibleStats = ref({ health: true, poster: true, subtitle: true, tmdb: true })

const _statsPrefsKey = (libId) => `lib-stats-prefs:${libId}`

const loadStatsPrefs = (libId) => {
  if (!libId) return
  try {
    const raw = localStorage.getItem(_statsPrefsKey(libId))
    if (raw) {
      const saved = JSON.parse(raw)
      visibleStats.value = {
        health:   saved.health   !== false,
        poster:   saved.poster   !== false,
        subtitle: saved.subtitle !== false,
        tmdb:     saved.tmdb     !== false,
      }
    } else {
      // 没存过：恢复默认全开
      visibleStats.value = { health: true, poster: true, subtitle: true, tmdb: true }
    }
  } catch (e) {
    console.warn('读 stats 偏好失败', e)
  }
}

// visibleStats 变化时：(1) 按库持久化  (2) 启用了之前未启用的项 → 触发对应数据拉取
// 用浅 snapshot 自管 oldVal，因为 deep watch 在 reactive 上的 oldVal 可能与 newVal 同引用
let _lastVisibleStats = { health: true, poster: true, subtitle: true, tmdb: true }
watch(visibleStats, (val) => {
  if (id.value) {
    try { localStorage.setItem(_statsPrefsKey(id.value), JSON.stringify(val)) } catch {}
  }
  const newlyOn = (k) => val[k] && !_lastVisibleStats[k]
  // health / poster / tmdb 由 stats endpoint 提供：任一新启用就重拉一次（按新 fields 命中或新建缓存）
  if (newlyOn('health') || newlyOn('poster') || newlyOn('tmdb')) {
    loadStats()
  }
  // subtitle 是单独 API（首次触发会启后台扫描，比较贵；用户启用时再拉）
  if (newlyOn('subtitle')) {
    loadSubtitleStats()
  }
  _lastVisibleStats = { ...val }
}, { deep: true })

// 缺字幕统计（懒加载 + 轮询字幕扫描任务）
const subtitleStats = ref(null)        // { status, task_id, without_required, total_videos, ... }
const subtitleStatsLoading = ref(false)
let subtitlePollTimer = null

// 重复检测
const dupResult = ref(null)
const dupLoading = ref(false)
const dupPath = ref('')
// 'metadata'（推荐：基于 Jellyfin TMDB/IMDB/标题年份/同剧同集）/ 'hash'（按 byte hash 扫盘）
const dupMode = ref('metadata')

const dupMatchLabel = (mt) => ({
  tmdb: 'TMDB 同 ID',
  imdb: 'IMDB 同 ID',
  title_year: '标题+年份',
  episode: '同剧·同季·同集',
  hash: 'byte 完全相同',
  size_only: '大小相同',
}[mt] || mt)

const dupTagType = (mt) => ({
  tmdb: 'success',
  imdb: 'success',
  title_year: 'warning',
  episode: 'success',
  hash: 'success',
  size_only: 'info',
}[mt] || '')

// 内容（无限滚动 + wanted 累加器，对齐 Trending.vue 的双层模型）
// items     = 后端拉到的"数据池"（一次拿一批，比 wanted 大或一致）
// wanted    = 当前"想要展示"的条数（按行累加），displayItems 切片到 wanted
// itemsTotal = 后端汇报的总条数，工具栏 "已加载 X / 共 Y" 用
const items = ref([])
const itemsTotal = ref(0)
const wanted = ref(0)                   // 从 initialLimit() 起步，每次 loadMore 累加 stepSize() 行
const itemsLoading = ref(false)         // 首批/重置加载（清空 items 时显示骨架）
const loadingMore = ref(false)          // 后台预取加载（不清 items；wanted 不被阻塞）
const hasMore = ref(true)               // 后端还有下一批 = true
const itemsTable = ref(null)
const sentinelRef = ref(null)           // 底部"加载更多/已到底"提示行（仅视觉，不再做 IO 观察）
const gridViewRef = ref(null)           // <div.grid-view> 的 DOM ref，用于 cardsPerRow 实测
const selectedItems = ref([])
// 已展开行 id 集合（仅用于 chevron 状态显示；展开/折叠靠 el-table 内部 store 处理）
const expandedSet = ref(new Set())
// 已懒加载的子节点：{ [parentId]: childrenArray }
// el-table lazy 模式下 row._children 不可靠（取决于 store 内部），自管一份用于级联选择
const childrenMap = ref({})
// 后端单次拉取批量；wanted 推进步长见 stepSize()；预取阈值 = stepSize × 2 见 prefetchIfNeeded
const FETCH_BATCH = 30
const nextStartIndex = ref(0)           // 下一批的 start_index（offset 模型）
// reqSeq 防竞态：任何 reset / 切库 / 改 filter 都 ++；过期回调按 seq 不一致丢弃
let reqSeq = 0
let prefetchTimer = null                // 首屏后延迟启动后台预取的 timer，reset 时取消
// 触发判定：window scroll capture 抓所有滚动事件（.app-main / .el-card__body / window 都能 catch），
// 然后用 sentinel 的 getBoundingClientRect() 看它离视口底部多近 —— 这个值不在乎谁是真正的滚动容器，
// 永远反映"sentinel 当前显示在视口的哪个位置"
let _loadMoreFiredAt = 0                // 节流：300ms 内不重复触发
const SCROLL_TRIGGER_PX = 400           // sentinel 离视口底 ≤ 400px 触发 loadMore
// 评分缓存：{`${tmdb_id}-${media_type}`: RatingResponse}
const ratingsByKey = ref({})
// 标题搜索：v-model 绑输入框，提交后写入 itemsSearch 触发 loadItems
const searchInput = ref('')
const itemsSearch = ref('')
// 年份 / 风格 多选过滤
const searchYears = ref([])     // string[]，例 ['2023', '2024']
const searchGenres = ref([])    // string[]，例 ['Action', 'Comedy']
const genreOptions = ref([])    // 库内所有 genre 名，懒加载（首次打开下拉时拉一次）
const _genresLoaded = ref(false)
// 年份 options：当前年回溯到 1950（足够覆盖大部分电影/剧）
const yearOptions = computed(() => {
  const cur = new Date().getFullYear()
  const out = []
  for (let y = cur; y >= 1950; y--) out.push(String(y))
  return out
})
// 忽略 Folder 开关：与 Jellyfin Web 默认行为对齐（默认关闭，即显示所有类型）
const hideFolders = ref(false)

// 视图模式（list 表格 / grid 网格），按库类型分桶持久化在 localStorage
const viewMode = useViewMode('library-detail', 'list')

// 网格卡片单击：在新窗口打开 Jellyfin 详情页
const onGridCardClick = (row) => {
  if (row.detail_url) window.open(row.detail_url, '_blank', 'noopener,noreferrer')
}

// 经过过滤后的列表（目前只用于"忽略 Folder"，sortedItems 在此基础上排序）
const filteredItems = computed(() => {
  if (hideFolders.value) {
    return items.value.filter(it => it.type !== 'Folder')
  }
  return items.value
})

// 健康问题码 → 简短标签（用于列内紧凑展示）
const _CODE_LABELS = {
  unrecognized: '未识别',
  name_mismatch: '名称错配',
  year_mismatch: '年份错配',
  short_runtime: '时长过短',
  sample_path: 'Sample',
  empty_series: '空剧集',
  empty_season: '空季',
  nested_main_file: '主文件嵌套',
}
const codeShortLabel = (code) => _CODE_LABELS[code] || code

// 取最多两个最具代表性的 issue 码：error 优先于 warning
const summaryCodes = (issues) => {
  if (!issues?.length) return []
  const errorCodes = ['unrecognized', 'name_mismatch', 'year_mismatch']
  const errs = issues.filter(i => errorCodes.includes(i.code))
  const warns = issues.filter(i => !errorCodes.includes(i.code))
  const ordered = [...errs, ...warns]
  return ordered.slice(0, 2).map(i => i.code)
}

// 排序选项与状态
const sortOptions = [
  { field: 'name',         label: '名称' },
  { field: 'health',       label: '健康度' },
  { field: 'type',         label: '类型' },
  { field: 'year',         label: '年份' },
  { field: 'rating',       label: '评分' },
  { field: 'tmdb_bound',   label: 'TMDB' },
]

const sortField = ref('name')
const sortDir = ref('asc') // 'asc' | 'desc'

// 切到不同字段时给个合理默认方向（排查问题/找高分时降序更顺手）
const _defaultDir = (field) =>
  ['health', 'rating', 'year'].includes(field) ? 'desc' : 'asc'

const setSort = (field) => {
  if (sortField.value === field) {
    sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortField.value = field
    sortDir.value = _defaultDir(field)
  }
}

// 单字段比较器：返回 [primary, secondary] 元组（多键稳定排序用）
const _fieldKey = (row, field) => {
  switch (field) {
    case 'name':
      return [(row.name || '').toLocaleLowerCase()]
    case 'health': {
      const lvl = { error: 3, warning: 2, ok: 1 }[row.health?.level || 'ok'] || 0
      const issueCount = row.health?.issues?.length || 0
      return [lvl, issueCount]
    }
    case 'type':
      return [row.type || '']
    case 'year':
      // null 排到最后
      return [row.year == null ? -Infinity : row.year]
    case 'rating':
      return [row.community_rating == null ? -Infinity : row.community_rating]
    case 'actors_done': {
      // 演员图完成度：用比例排序；无演员的当作 1（视为完成）
      if (!row.actors_total) return [1]
      return [row.actors_with_image / row.actors_total]
    }
    case 'tmdb_bound':
      return [row.tmdb_id ? 1 : 0]
    default:
      return [0]
  }
}

const _compare = (a, b) => {
  if (a < b) return -1
  if (a > b) return 1
  return 0
}

const sortedItems = computed(() => {
  const arr = [...filteredItems.value]
  const dir = sortDir.value === 'asc' ? 1 : -1
  arr.sort((a, b) => {
    const ka = _fieldKey(a, sortField.value)
    const kb = _fieldKey(b, sortField.value)
    for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
      const r = _compare(ka[i], kb[i])
      if (r !== 0) return r * dir
    }
    // 主键完全相同时，用名称做次级稳定排序（不受方向影响）
    return _compare(
      (a.name || '').toLocaleLowerCase(),
      (b.name || '').toLocaleLowerCase(),
    )
  })
  return arr
})

// ============ wanted / 行步长 / 视口测量（对齐 Trending.vue 同款套路）============
// 网格卡片宽度（来自 CSS $grid-card-w）+ gap：cardsPerRow = floor((containerW + gap) / (cardW + gap))
const GRID_CARD_W = 280
const GRID_CARD_GAP = 18
const GRID_POSTER_H = 158

// 网格列数：list 模式恒为 1（el-table 单列），grid 用实测容器宽度
const cardsPerRow = () => {
  if (viewMode.value !== 'grid') return 1
  const el = gridViewRef.value
  // 容器没挂上时按 viewport - sidebar(220) - padding(40) 估
  const containerW = el ? el.clientWidth : Math.max(0, window.innerWidth - 220 - 40)
  return Math.max(1, Math.floor((containerW + GRID_CARD_GAP) / (GRID_CARD_W + GRID_CARD_GAP)))
}

// 每次 IntersectionObserver 触发 loadMore 时 wanted 推进的"行数"对应条数
//   grid：一行卡片 = cardsPerRow（视觉上一次冒出一行）
//   list：固定 10 行 —— 表格行高 ~80px，10 行 = ~800px 足以把 sentinel 推出视口避免反复 fire
const stepSize = () => {
  return viewMode.value === 'grid' ? cardsPerRow() : 10
}

// 首批展示条数：内容塞满滚动容器再加一行余量
const initialLimit = () => {
  // 优先用 grid/table 自己的 top 推算可用高度
  const el = gridViewRef.value || itemsTable.value?.$el
  let usableH
  if (el) {
    const top = el.getBoundingClientRect().top
    usableH = Math.max(300, window.innerHeight - top)
  } else {
    usableH = Math.max(300, window.innerHeight - 240)
  }
  const rowH = viewMode.value === 'grid' ? (GRID_POSTER_H + 60) : 80
  const visibleRows = Math.max(1, Math.ceil(usableH / rowH))
  return (visibleRows + 1) * cardsPerRow()
}

// displayItems：sortedItems 切到 wanted，gap 补 grid 骨架；list 模式不在表内插骨架（el-table tree-lazy 不兼容）
// grid skeletonCount 上限 = perRow × 2（最多两行骨架），避免无限增长
const displayItems = computed(() => {
  const w = wanted.value || sortedItems.value.length
  const sliced = sortedItems.value.slice(0, w)
  // grid 模式：池子追不上 wanted 时用骨架补到 wanted 行末尾
  if (viewMode.value === 'grid') {
    const perRow = cardsPerRow()
    // 仅在还有更多 / 正在加载 / 池子不够时才显示骨架
    const fillingPool = items.value.length < w && (loadingMore.value || itemsLoading.value || hasMore.value)
    if (fillingPool) {
      const gap = Math.max(0, w - sliced.length)
      const skeletonCount = Math.min(gap, perRow * 2)
      const out = sliced.map((r) => ({ ...r, _skeleton: false }))
      for (let i = 0; i < skeletonCount; i++) {
        out.push({ id: `__sk__${i}_${Date.now()}`, _skeleton: true })
      }
      return out
    }
  }
  return sliced
})


const onSelectionChange = (rows) => {
  selectedItems.value = rows
}

// ===== 自定义选择 + 展开（替代 el-table 默认 selection 列 / 自动 chevron）=====
// 用 row.id 维护选中集合
const isRowSelected = (row) => selectedItems.value.some(r => r.id === row.id)

/**
 * 级联选择：Series 选中 → 自动选中其下所有 Season + Episode；取消同理。
 * 子节点必须已经懒加载过（在 childrenMap 里）才能被级联。
 * 未加载的子节点：在 loadChildren 完成时按当前父节点状态自动续选（见下方）。
 */
const _walkRowAndDescendants = (row, callback) => {
  callback(row)
  const subs = childrenMap.value[row.id]
  if (Array.isArray(subs)) {
    subs.forEach(s => _walkRowAndDescendants(s, callback))
  }
}

const toggleRowSelection = (row, checked) => {
  if (checked) {
    const map = new Map(selectedItems.value.map(r => [r.id, r]))
    _walkRowAndDescendants(row, r => map.set(r.id, r))
    selectedItems.value = [...map.values()]
    // 子节点未懒加载时，后台触发递归加载并续选 —— 用户勾 Series 后所有 Season/Episode 都会被选中
    if (row.has_children && !childrenMap.value[row.id]) {
      _cascadeLoadAndSelect(row)
    } else if (Array.isArray(childrenMap.value[row.id])) {
      // 一级 children 已在 map，但 children 的 children 可能没加载（只展开了一层）
      // 对每个已加载的子节点继续递归
      childrenMap.value[row.id].forEach(c => {
        if (c.has_children && !childrenMap.value[c.id]) {
          _cascadeLoadAndSelect(c)
        }
      })
    }
  } else {
    const idsToRemove = new Set()
    _walkRowAndDescendants(row, r => idsToRemove.add(r.id))
    selectedItems.value = selectedItems.value.filter(r => !idsToRemove.has(r.id))
  }
}

/**
 * 递归懒加载 row 的所有后代并加入 selectedItems。
 * fire-and-forget：UI 已立即把父行加进 selectedItems，这里负责把后代陆续补上。
 * 触发场景：用户勾选 Series（或 Season）但还没展开 → children 未加载 → 直接递归拉取所有层级。
 */
const _cascadeLoadAndSelect = async (row) => {
  if (!row.has_children) return
  let children = childrenMap.value[row.id]
  if (!Array.isArray(children)) {
    try {
      let r
      if (row.type === 'Series') r = await jellyfinApi.seasonsOfSeries(row.id)
      else if (row.type === 'Season') r = await jellyfinApi.episodesOfSeason(row.id)
      else return  // Episode 等没有 children 的类型
      children = r?.data?.items || []
      childrenMap.value = { ...childrenMap.value, [row.id]: children }
    } catch (e) {
      console.warn('_cascadeLoadAndSelect 加载失败', row.id, e)
      return
    }
  }
  if (!children.length) return
  // 父行被选中 → 把本层 children 也加进 selectedItems
  if (isRowSelected(row)) {
    const map = new Map(selectedItems.value.map(r => [r.id, r]))
    children.forEach(c => map.set(c.id, c))
    selectedItems.value = [...map.values()]
  }
  // 递归到下一层（Series → Season → Episode）
  await Promise.all(children.map(c => _cascadeLoadAndSelect(c)))
}

// 顶部 select-all：全选只对当前可见的顶层 Series 生效（树展开后的子节点不级联）
const allSelected = computed(() =>
  items.value.length > 0 && items.value.every(r => isRowSelected(r))
)
const someSelected = computed(() =>
  items.value.some(r => isRowSelected(r))
)
const onToggleAll = (checked) => {
  if (checked) {
    // 合并去重：保留已选的子节点 + 加入所有顶层
    const map = new Map()
    selectedItems.value.forEach(r => map.set(r.id, r))
    items.value.forEach(r => map.set(r.id, r))
    selectedItems.value = [...map.values()]
  } else {
    // 仅清掉顶层；保留已选的子节点（展开后选过的）
    const topIds = new Set(items.value.map(r => r.id))
    selectedItems.value = selectedItems.value.filter(r => !topIds.has(r.id))
  }
}

// 展开/折叠：调 el-table 内部的 toggleRowExpansion，自管 expandedSet 用于 chevron 旋转
const toggleRowExpand = (row) => {
  const id = row.id
  const willExpand = !expandedSet.value.has(id)
  if (willExpand) expandedSet.value.add(id)
  else            expandedSet.value.delete(id)
  // trigger Vue reactivity for Set mutation
  expandedSet.value = new Set(expandedSet.value)
  // 调 el-table 内部展开（lazy load 也走这条）
  if (itemsTable.value) {
    itemsTable.value.toggleRowExpansion(row, willExpand)
  }
}

// 行 class：有问题的行加色调标记
const rowClassName = ({ row }) => {
  const classes = ['item-row']
  if (row.health?.level === 'error') classes.push('row-health-error')
  else if (row.health?.level === 'warning') classes.push('row-health-warning')
  // 按 row.type 加层级类，给 CSS 提供可靠的层级钩子（el-table 自带的层级类版本不一致）
  if (row.type === 'Season')  classes.push('row-level-season')
  if (row.type === 'Episode') classes.push('row-level-episode')
  return classes.join(' ')
}

// 重新识别（刮削元数据）对话框
const showIdentifyDialog = ref(false)
const identifyTarget = ref(null)

const openIdentify = (row) => {
  identifyTarget.value = row
  showIdentifyDialog.value = true
}

const onIdentifyApplied = ({ itemId }) => {
  // Apply 是异步刷新，给后端 5 秒时间然后重新拉这一页
  setTimeout(() => loadItems(), 5000)
}

// 删除条目 / 清除 Sample 对话框（共用同一个组件）
const showSampleDeleteDialog = ref(false)
const sampleDeleteTargetId = ref('')
const sampleDeleteMode = ref('sample')  // 'sample' | 'unrecognized'

// 疑似 sample —— 健康 issue 命中 sample_path 或 short_runtime
const isSampleSuspect = (row) => {
  const codes = (row.health?.issues || []).map(i => i.code)
  return codes.includes('sample_path') || codes.includes('short_runtime')
}

// 未识别 —— Folder 类型
const isUnrecognized = (row) => row.type === 'Folder'

const openSampleDelete = (row, mode = 'sample') => {
  sampleDeleteTargetId.value = row.id
  sampleDeleteMode.value = mode
  showSampleDeleteDialog.value = true
}

const onSampleDeleted = () => {
  ElMessage.success('已删除，正在重新加载列表')
  loadItems()
}

// 复制路径到剪贴板
const copyPath = async (path) => {
  if (!path) return
  try {
    await navigator.clipboard.writeText(path)
    ElMessage.success('已复制到剪贴板')
  } catch (e) {
    // 老浏览器或 http 环境 fallback
    const ta = document.createElement('textarea')
    ta.value = path
    ta.style.position = 'fixed'
    ta.style.left = '-9999px'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      ElMessage.success('已复制到剪贴板')
    } catch {
      ElMessage.error('复制失败')
    } finally {
      document.body.removeChild(ta)
    }
  }
}

const clearSelection = () => {
  if (itemsTable.value) {
    itemsTable.value.clearSelection()
  }
  selectedItems.value = []
}

// TMDB 详情页链接（电影/剧集/季 URL 格式不同；Episode 在表格里已显示 —）
const tmdbUrl = (row) => {
  if (row.type === 'Series') return `https://www.themoviedb.org/tv/${row.tmdb_id}`
  if (row.type === 'Movie')  return `https://www.themoviedb.org/movie/${row.tmdb_id}`
  if (row.type === 'Season' && row.tmdb_id) {
    // Season 的 ProviderIds.Tmdb 通常是 season-id，TMDB 没有"按 season-id 直跳"页面
    // 退化为父剧 + season number；如果有更精准做法以后再改
    return `https://www.themoviedb.org/tv/${row.tmdb_id}`
  }
  return `https://www.themoviedb.org/movie/${row.tmdb_id}`
}

// 字幕覆盖率 → 颜色档：>=90% 绿，>=60% 黄，否则红
const subtitleCoverageClass = (pct) => {
  if (pct == null) return ''
  if (pct >= 90) return 'sub-cov-good'
  if (pct >= 60) return 'sub-cov-warn'
  return 'sub-cov-bad'
}

// ===== 时长格式化（聚合后总时长可能上百小时）=====

const formatTotalRuntime = (minutes) => {
  if (!minutes) return ''
  const total = Math.round(minutes)
  if (total < 60) return `${total} 分`
  const h = Math.floor(total / 60)
  const m = total % 60
  if (h < 100) return m ? `${h} 时 ${m} 分` : `${h} 时`
  // 长剧集（>= 100 小时）只显示小时，避免单元格爆字
  return `${h} 时`
}

// 字幕语言代码 → 显示文本（短）+ tag 风格颜色
const _SUB_LANG_LABEL = {
  chs: '简', cht: '繁', eng: 'EN', jpn: '日',
  kor: '韩', fre: '法', ger: '德', spa: '西', rus: '俄', ita: '意',
  und: '未知',  // jellyfin 给 Language=und 时的友好显示
}
const _SUB_LANG_TAG_TYPE = {
  chs: 'success',     // 简体绿
  cht: 'success',
  eng: 'primary',     // 英语蓝
  jpn: 'warning',     // 日语黄
  kor: 'info',
  und: 'info',        // 未知 → 灰
}
const subLangLabel = (code) => _SUB_LANG_LABEL[code] || (code || '').toUpperCase().slice(0, 4)
const subLangTagType = (code) => _SUB_LANG_TAG_TYPE[code] || 'info'

// 字幕下载 dialog 状态
const showSubDownloadDialog = ref(false)
const subDownloadTarget = ref(null)
const openSubtitleDownload = (row) => {
  subDownloadTarget.value = row
  showSubDownloadDialog.value = true
}
const onSubtitleDownloaded = (payload) => {
  // 下载成功后刷新当前页 items（让"字幕"列重读 MediaStreams）
  // 视频文件加新字幕后 jellyfin 需要扫一遍才会更新 MediaStreams；
  // 这里只重拉前端缓存，等 jellyfin 自身扫描后下次进入页面就看到新语言
  ElMessage.info('Jellyfin 重新扫描该项后字幕会显示在列表里')
}

// 单作品时长：电影 / 单集；< 60 分钟显示 "XX 分"，否则 "X 时 Y 分"
const formatRuntimeMin = (minutes) => {
  if (!minutes) return ''
  const total = Math.round(minutes)
  if (total < 60) return `${total} 分`
  const h = Math.floor(total / 60)
  const m = total % 60
  return m ? `${h} 时 ${m} 分` : `${h} 时`
}

// ===== 树形表格：标题 / 类型显示帮助函数 =====

/** 标题渲染：Episode 加 SxxExx 前缀（取自 Jellyfin 自带 IndexNumber 字段）*/
const rowDisplayTitle = (row) => {
  if (row.type === 'Episode') {
    const s = row.season_number != null ? String(row.season_number).padStart(2, '0') : '?'
    const e = row.episode_number != null ? String(row.episode_number).padStart(2, '0') : '?'
    return `S${s}E${e} · ${row.name}`
  }
  return row.name
}

/** 类型 tag 颜色 */
const typeTagType = (t) => {
  if (t === 'Movie') return 'success'
  if (t === 'Series') return 'primary'
  if (t === 'Season') return 'warning'
  if (t === 'Episode') return 'info'
  return 'info'
}

/** 类型 tag 文案 */
const typeLabel = (t) => {
  const map = { Movie: '电影', Series: '剧集', Season: '季', Episode: '集', Folder: 'Folder' }
  return map[t] || t
}

// ===== Episode 缩略图修复（单集）=====

/**
 * 用户点击 Episode 行的"修缩略图 / 换缩略图"按钮：
 *   - 创建一个 episode_still_fix_single 任务（后端触发 TMDB still 取图 + 上传 Jellyfin）
 *   - 任务完成后用 task 的 result 反馈给用户
 *   - 暂时不在本页轮询任务进度，让用户去任务页查看（或自己刷新）
 */
const fixSingleStill = async (row) => {
  row._fixingStill = true
  try {
    const res = await metadataApi.fixSingleEpisodeStill(row.id)
    ElMessage.success(`已启动缩略图修复任务 #${res.data.task_id}`)
  } catch (e) {
    // 拦截器已 toast，这里兜底
    console.warn('启动 Episode 缩略图修复失败', e)
  } finally {
    row._fixingStill = false
  }
}

// ===== 树形表格：懒加载子节点 =====

/**
 * el-table 的 lazy load 回调。每个节点点开时调用一次（结果会被表格内部缓存，
 * 后续展开/折叠不再调用）。
 *   row    - 当前父行（Series 或 Season）
 *   resolve - 给表格塞子节点数组
 */
const loadChildren = async (row, treeNode, resolve) => {
  try {
    let children = []
    if (row.type === 'Series') {
      const r = await jellyfinApi.seasonsOfSeries(row.id)
      children = r.data.items || []
    } else if (row.type === 'Season') {
      const r = await jellyfinApi.episodesOfSeason(row.id)
      children = r.data.items || []
    }
    // 记入级联选择用的 children map
    childrenMap.value = { ...childrenMap.value, [row.id]: children }
    resolve(children)
    // 级联续选：父行已选 → 把刚加载的子节点也加入选中
    if (isRowSelected(row) && children.length) {
      const map = new Map(selectedItems.value.map(r => [r.id, r]))
      children.forEach(c => map.set(c.id, c))
      selectedItems.value = [...map.values()]
    }
  } catch (e) {
    console.error('懒加载子节点失败', e)
    resolve([])
  }
}


/**
 * MediaToolbar 的 scope：
 *   - 没有选中 → 作用于当前库
 *   - 选中条目 → 作用于这些条目（path 列表来自 jellyfin items.path）
 */
const toolbarScope = computed(() => {
  const collectionType = library.value?.collection_type
  if (selectedItems.value.length > 0) {
    return {
      mode: 'items',
      library_id: id.value,
      library_name: library.value?.name,
      collection_type: collectionType,
      item_count: selectedItems.value.length,
      // jellyfin items 接口返回的 path 是文件路径（mkv/mp4 路径或剧集系列目录路径）
      item_paths: selectedItems.value.map(it => it.path).filter(Boolean),
      // Episode 缩略图修复需要 jellyfin item id（路径派生不出来）
      episode_ids: selectedItems.value
        .filter(it => it.type === 'Episode')
        .map(it => it.id),
    }
  }
  return {
    mode: 'library',
    library_id: id.value,
    library_name: library.value?.name,
    collection_type: collectionType,
  }
})

const showDupDialog = ref(false)

/**
 * 6 项指标，按用户要求顺序：
 *   资源数量、电影/剧集数、空间占用、缺海报、缺字幕、TMDB 绑定
 *
 * 不同库类型的"资源数量"含义不同：
 *   - movies / tvshows / musicvideos / homevideos / mixed  → 视频文件
 *   - music                                                → 音频文件
 *   - photos                                               → 图片
 * 缺字幕近似为 max(0, 视频数 - 字幕文件数)；音乐/图片库不适用 → 显示 —
 */
/** 自适应单位的字节格式化 → { value, suffix } */
const formatBytesValue = (bytes) => {
  if (!bytes || bytes <= 0) return { value: '0', suffix: '' }
  const KB = 1024, MB = KB * 1024, GB = MB * 1024, TB = GB * 1024
  if (bytes >= TB) return { value: (bytes / TB).toFixed(2), suffix: ' TB' }
  if (bytes >= GB) return { value: (bytes / GB).toFixed(2), suffix: ' GB' }
  if (bytes >= MB) return { value: (bytes / MB).toFixed(1), suffix: ' MB' }
  return { value: (bytes / KB).toFixed(0), suffix: ' KB' }
}

/** 总时长（秒）格式化为 { value, suffix } */
const formatRuntime = (seconds) => {
  if (!seconds || seconds <= 0) return { value: '—', suffix: '' }
  const total_min = Math.floor(seconds / 60)
  if (total_min < 60) return { value: total_min, suffix: ' m' }
  const h = Math.floor(total_min / 60)
  const m = total_min % 60
  if (h < 100) {
    return m
      ? { value: `${h}h${String(m).padStart(2, '0')}`, suffix: 'm' }
      : { value: h, suffix: ' h' }
  }
  return { value: h, suffix: ' h' }
}

const metrics = computed(() => {
  if (!stats.value) return []
  const fs = stats.value.filesystem || {}
  const jf = stats.value.jellyfin || {}
  const t = library.value?.collection_type

  // 资源数量 + 标签
  let resourceLabel = '视频文件'
  let resourceValue = fs.video_count || 0
  if (t === 'music') {
    resourceLabel = '音频文件'
    resourceValue = fs.audio_count || 0
  } else if (t === 'photos') {
    resourceLabel = '图片'
    resourceValue = fs.image_count || 0
  } else if (t === 'mixed') {
    resourceLabel = '媒体文件'
    resourceValue = (fs.video_count || 0) + (fs.audio_count || 0) + (fs.image_count || 0)
  }

  const movieSeriesCount = (jf.movies || 0) + (jf.series || 0)

  // 字幕覆盖：从独立的字幕扫描结果拿（懒加载，期间显示"统计中"）
  // 与"总体健康度"、"TMDB 绑定"统一为"已完成/总数"正向格式，避免和"缺 X / 总"
  // 这种反向格式混淆。warn 仍以"还有缺漏"为触发条件
  // 'music' / 'photos' 库不适用
  let subtitleCoverage
  if (['music', 'photos'].includes(t)) {
    subtitleCoverage = { value: '—', loading: false }
  } else if (subtitleStats.value?.status === 'ready') {
    const total = subtitleStats.value.total_videos || 0
    const missing = subtitleStats.value.without_required || 0
    const have = Math.max(0, total - missing)
    const pct = total ? Math.round((have / total) * 100) : 0
    subtitleCoverage = {
      value: total ? `${have} / ${total} (${pct}%)` : '—',
      loading: false,
      warn: missing > 0,
    }
  } else if (subtitleStats.value?.status === 'running' || subtitleStatsLoading.value) {
    subtitleCoverage = { value: '统计中…', loading: true }
  } else {
    subtitleCoverage = { value: '—', loading: false }
  }

  // 健康度（放首位）
  const totalItems = jf.total_items || 0
  const healthy = jf.items_healthy ?? totalItems
  const ratio = totalItems ? healthy / totalItems : 1
  let healthColor = '#10b981'
  if (ratio < 0.7) healthColor = '#ef4444'
  else if (ratio < 0.9) healthColor = '#f59e0b'
  else if (ratio < 1) healthColor = '#3b82f6'

  // 占用（自适应单位）
  const sizeFmt = formatBytesValue(fs.total_size_bytes || 0)
  // 总时长
  const runtimeFmt = formatRuntime(jf.total_runtime_seconds || 0)

  // 4 项基础指标始终显示，4 项可选指标按 visibleStats 过滤
  const result = []
  if (visibleStats.value.health) {
    result.push({
      label: '总体健康度',
      value: totalItems
        ? `${healthy} / ${totalItems} (${(ratio * 100).toFixed(0)}%)`
        : '—',
      color: healthColor,
    })
  }
  result.push(
    { label: resourceLabel, value: resourceValue },
    { label: '电影/剧集数', value: movieSeriesCount },
    { label: '空间占用', value: sizeFmt.value, suffix: sizeFmt.suffix },
    { label: '总时长', value: runtimeFmt.value, suffix: runtimeFmt.suffix },
  )
  if (visibleStats.value.poster) {
    result.push({ label: '缺海报', value: jf.without_poster || 0, warn: (jf.without_poster || 0) > 0 })
  }
  if (visibleStats.value.subtitle) {
    result.push({
      label: '字幕覆盖',
      value: subtitleCoverage.value,
      warn: subtitleCoverage.warn,
      loading: subtitleCoverage.loading,
    })
  }
  // 演员图：整库聚合（之前是表格每行一格的"演员图 X/Y"，已移除；改这里看汇总）
  const aTotal = jf.actors_total || 0
  const aHave  = jf.actors_with_image || 0
  result.push({
    label: '演员图',
    value: aTotal ? `${aHave} / ${aTotal}` : '—',
    warn: aTotal > 0 && aHave < aTotal,
  })
  if (visibleStats.value.tmdb) {
    result.push({ label: 'TMDB 绑定', value: `${jf.with_tmdb_id || 0} / ${jf.total_items || 0}` })
  }
  return result
})

/**
 * 字幕统计：懒加载 + 轮询。
 *  - 收到 ready：直接显示
 *  - 收到 running：保留 task_id，启动 2s 轮询直到任务终态，再读 result
 *  - force=true 时跳过近期任务复用，直接启新扫描
 */
const loadSubtitleStats = async (force = false) => {
  if (!id.value) return
  subtitleStatsLoading.value = true
  // music/photos 库没字幕概念，跳过
  const t = library.value?.collection_type
  if (['music', 'photos'].includes(t)) {
    subtitleStatsLoading.value = false
    return
  }
  try {
    const res = await jellyfinApi.librarySubtitleStats(id.value, force)
    subtitleStats.value = res.data
    if (res.data.status === 'running' && res.data.task_id) {
      startSubtitlePoll(res.data.task_id)
    }
  } catch (e) {
    console.error('字幕统计加载失败', e)
  } finally {
    subtitleStatsLoading.value = false
  }
}

const startSubtitlePoll = (taskId) => {
  stopSubtitlePoll()
  subtitlePollTimer = setInterval(async () => {
    try {
      const res = await taskApi.get(taskId)
      const t = res.data
      // 把当前进度同步到 subtitleStats，让 UI 显示"统计中"
      subtitleStats.value = {
        ...subtitleStats.value,
        status: t.status === 'completed' ? 'ready' : 'running',
        progress: t.progress,
        message: t.message,
        // 完成时从 result 读最终数据
        without_required: t.result?.without_required ?? subtitleStats.value?.without_required,
        total_videos: t.result?.total_videos ?? subtitleStats.value?.total_videos,
      }
      if (['completed', 'failed', 'cancelled'].includes(t.status)) {
        stopSubtitlePoll()
      }
    } catch (e) {
      console.error('轮询字幕扫描任务失败', e)
      stopSubtitlePoll()
    }
  }, 2000)
}

const stopSubtitlePoll = () => {
  if (subtitlePollTimer) {
    clearInterval(subtitlePollTimer)
    subtitlePollTimer = null
  }
}

const refreshing = ref(false)
const showRefreshDialog = ref(false)

// 顶部库标签上用的：处理 collection_type（小写复数：movies/tvshows/...）
const collectionTypeLabel = (t) => ({
  movies: '电影', tvshows: '剧集', music: '音乐', musicvideos: '音乐视频',
  homevideos: '家庭视频', boxsets: '合集', books: '图书', mixed: '混合',
}[t] || t)

const collectionTypeTagType = (t) => ({
  movies: 'success', tvshows: 'primary', music: 'warning', mixed: 'info',
}[t] || '')

const typeIcon = computed(() => {
  const t = library.value?.collection_type
  return ({
    movies: VideoCamera,
    tvshows: VideoPlay,
    music: Headset,
    musicvideos: VideoPlay,
  })[t] || Folder
})

/**
 * 首次进入页面用：走默认缓存，速度快
 */
const loadAll = async () => {
  await loadLibrary()
  loadStats()           // 概览统计（同步阻塞）
  // 字幕统计会触发后台 subtitle_scan 任务，很贵 —— 用户隐藏了就别跑
  if (visibleStats.value.subtitle) {
    loadSubtitleStats()
  }
  loadItems()           // 顶层 Series / Movie 列表
}

/**
 * 页头"强制刷新"按钮：刷新本页所有数据，旁路所有缓存
 *   - 后端 seasons/episodes/aggregates 1h 缓存（清零）
 *   - 库信息 / 库统计 (force=true 跳过 2h 缓存)
 *   - 字幕扫描复用窗口 (force=true 启新扫描)
 *   - 顶层条目列表（替换 items 数组 → el-table tree state 一并重置）
 */
const forceRefresh = async () => {
  // 1. 清后端 seasons/episodes/aggregates 缓存
  try {
    await jellyfinApi.clearChildrenCache()
  } catch (e) {
    console.warn('清空 children 缓存失败', e)
  }
  // 2. 字幕扫描状态：停掉轮询 + 清掉旧值
  stopSubtitlePoll()
  subtitleStats.value = null
  // 3. 重新拉所有数据
  await loadLibrary()
  loadStats(true)
  if (visibleStats.value.subtitle) {
    loadSubtitleStats(true)
  }
  loadItems()
}

const loadLibrary = async () => {
  loading.value = true
  try {
    const res = await jellyfinApi.libraries(true)
    library.value = (res.data.libraries || []).find(l => l.id === id.value)
    if (!library.value) {
      ElMessage.error('未找到该媒体库')
      router.push('/medialibraries')
      return
    }
    // 默认选第一个路径作为重复检测目标
    if (!dupPath.value && library.value.locations.length) {
      dupPath.value = library.value.locations[0]
    }
  } catch (e) {
    ElMessage.error('加载库信息失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    loading.value = false
  }
}

/**
 * 把当前 visibleStats 中开启的可选项（health/poster/tmdb；subtitle 是单独 API）
 * 转成后端 fields 字符串。所有都隐藏 → '' （后端跳过这三项的计算）。
 */
const _enabledStatsFields = () =>
  ['health', 'poster', 'tmdb'].filter(k => visibleStats.value[k]).join(',')

const loadStats = async (force = false) => {
  loadingStats.value = true
  statsError.value = ''
  try {
    const res = await jellyfinApi.libraryStats(id.value, force, _enabledStatsFields())
    stats.value = res.data
  } catch (e) {
    statsError.value = e.response?.data?.detail || e.message
  } finally {
    loadingStats.value = false
  }
}


/** 把"缓存秒数"格式化为友好文案（XX 秒前 / XX 分钟前 / XX 小时前）*/
const formatCacheAge = (seconds) => {
  if (!seconds || seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`
  return `${Math.floor(seconds / 3600)} 小时`
}

// ============ 数据池 + wanted 双层加载模型（对齐 Trending.vue）============
// reset = 清池 + 重置 wanted = initialLimit() + 拉首批；filter 变 / 切库 / 强刷都走这条
const loadItems = async () => {
  if (prefetchTimer) { clearTimeout(prefetchTimer); prefetchTimer = null }
  const seq = ++reqSeq
  itemsLoading.value = true
  items.value = []
  itemsTotal.value = 0
  nextStartIndex.value = 0
  hasMore.value = true
  wanted.value = initialLimit()
  // 清掉已展开 / 已加载的子节点缓存（行 id 跟新批不一定一一对应）
  expandedSet.value = new Set()
  childrenMap.value = {}
  selectedItems.value = []
  try {
    const res = await jellyfinApi.libraryItems(id.value, _buildItemsParams(0, FETCH_BATCH))
    if (seq !== reqSeq) return
    const newItems = res.data.items || []
    items.value = newItems
    itemsTotal.value = res.data.total || 0
    nextStartIndex.value = newItems.length
    hasMore.value = newItems.length >= FETCH_BATCH && nextStartIndex.value < itemsTotal.value
    _fireBatchEnrichments()
  } catch (e) {
    ElMessage.error('加载内容失败: ' + (e.response?.data?.detail || e.message))
    hasMore.value = false
  } finally {
    if (seq === reqSeq) {
      itemsLoading.value = false
      _loadMoreFiredAt = 0  // 重置节流时钟
      // 首屏 1.5s 后启动后台预取（让首批先稳定渲染；用户切库会取消这个 timer）
      prefetchTimer = setTimeout(() => {
        prefetchTimer = null
        prefetchIfNeeded()
      }, 1500)
    }
  }
}

// 后台预取：数据池剩余不足 2 个 wanted 步长 → 拉下一批补给；不阻塞 wanted 推进
// 设计目标：用户滚到末行时，items 池里已经有数据；首批后立即放出，看不到骨架占位时间
// 自递归：上游一批数量小、用户滚得快 → 一次预取不够时继续预取
const prefetchIfNeeded = async () => {
  if (loadingMore.value || !hasMore.value) return
  if (items.value.length - wanted.value >= stepSize() * 2) return
  const seq = reqSeq
  loadingMore.value = true
  try {
    const start = nextStartIndex.value
    const res = await jellyfinApi.libraryItems(id.value, _buildItemsParams(start, FETCH_BATCH))
    if (seq !== reqSeq) return
    const newItems = res.data.items || []
    items.value = [...items.value, ...newItems]
    if (res.data.total != null) itemsTotal.value = res.data.total
    nextStartIndex.value = start + newItems.length
    hasMore.value = newItems.length >= FETCH_BATCH && nextStartIndex.value < itemsTotal.value
    _fireBatchEnrichments()
  } catch (e) {
    console.warn('后台预取失败:', e)
    hasMore.value = false
  } finally {
    loadingMore.value = false  // 强制重置（seq 守卫只防污染，标志位归位无论如何）
  }
  // 仍然池子不足 → 继续预取
  if (seq === reqSeq && hasMore.value && items.value.length - wanted.value < stepSize() * 2) {
    prefetchIfNeeded()
  }
}

// 触底（scroll 监听调）：wanted += stepSize；池子告急时后台预取（不 await）
const loadMore = () => {
  if (itemsLoading.value) return
  if (!hasMore.value && items.value.length <= wanted.value) return
  wanted.value += stepSize()
  prefetchIfNeeded()
}

// 公共 params 构造：filter / search 共用
const _buildItemsParams = (start, limit) => {
  const params = {
    start_index: start,
    limit,
    search: itemsSearch.value || undefined,
  }
  if (searchYears.value.length) {
    params.years = searchYears.value.join(',')
  }
  if (searchGenres.value.length) {
    params.genres = searchGenres.value.join('|')
  }
  return params
}

// 每批数据到达后触发三个补齐查询：评分 / 季聚合 / 字幕
// 这三个原来在 loadItems 末尾各调一次；现在 loadMore 也要调（只针对新加入的批）
// 简化：仍然全量调（评分 batch / 字幕 batch 都有缓存，重复 ID 不会重复打远端）
const _fireBatchEnrichments = () => {
  fetchRatingsForItems()
  fetchSeriesAggregates()
  fetchSubtitleLangsForItems()
}

// ============ 滚动触发：用 sentinel 的 boundingClientRect 判定 ============
// 不再纠结"谁是真正的滚动容器"（.app-main / el-card__body / window 都有可能），
// 用 window scroll capture 抓住所有滚动事件，然后看 sentinel 在 window 视口里的位置。
// getBoundingClientRect 返回 window 视口坐标，跟容器内部 scrollTop 无关 —— 谁滚都对。
const _maybeLoadMoreOnScroll = () => {
  if (itemsLoading.value) return
  if (!hasMore.value && items.value.length <= wanted.value) return
  if (!sentinelRef.value) return
  const rect = sentinelRef.value.getBoundingClientRect()
  // sentinel.top 距 window 视口底 ≤ SCROLL_TRIGGER_PX 时触发
  const viewportBottom = window.innerHeight || document.documentElement.clientHeight
  if (rect.top - viewportBottom > SCROLL_TRIGGER_PX) return
  // 节流：每 300ms 最多 fire 一次
  const now = Date.now()
  if (now - _loadMoreFiredAt < 300) return
  _loadMoreFiredAt = now
  loadMore()
}

// ============ DEBUG（事后删）：写共享 debugInfo，由 App.vue 侧边栏读取展示 ============
// 跟 Trending.vue 用同一套字段；source 区分页面来源
const writeDebug = () => {
  debugInfo.enabled = true
  debugInfo.source = `library:${viewMode.value}`
  // 网格视图下行/列才有意义；列表视图固定 1 列
  const cols = viewMode.value === 'grid' ? _gridColsEstimate() : 1
  const visibleCount = sortedItems.value.length
  debugInfo.cols = cols
  debugInfo.totalRows = cols ? Math.max(1, Math.ceil(visibleCount / cols)) : 0
  // items = 数据池大小（后端拉到本地的条数）
  // wanted = 当前展示目标条数（loadMore 一步加 stepSize 行；items >= wanted 时池里足够）
  debugInfo.items = items.value.length
  debugInfo.wanted = wanted.value
}

// 网格列数：网格 CSS 用 auto-fill minmax(160px,1fr)，按容器宽度估算
const _gridColsEstimate = () => {
  const container = document.querySelector('.items-card .grid-view')
  if (!container) return 0
  const w = container.clientWidth || 0
  const cardMin = 160 + 16  // minmax(160px,1fr) + gap
  return Math.max(1, Math.floor(w / cardMin))
}

// 当前视口顶部对齐的是第几行（scrollRow），随 scroll 节流更新
const updateScrollRow = () => {
  const el = viewMode.value === 'grid'
    ? document.querySelector('.items-card .grid-view')
    : document.querySelector('.items-card .el-table__body')
  if (!el) {
    debugInfo.scrollRow = 0
    return
  }
  const rect = el.getBoundingClientRect()
  const offset = Math.max(0, -rect.top)
  // grid 行高靠 grid-card 高度（估 240px 含 meta）；list 行高约 80px
  const rowH = viewMode.value === 'grid' ? 240 : 80
  debugInfo.scrollRow = Math.floor(offset / rowH) + (offset > 0 ? 1 : 0)
  writeDebug()
}

let _scrollRaf = null
const onWindowScroll = () => {
  if (_scrollRaf) return
  _scrollRaf = requestAnimationFrame(() => {
    updateScrollRow()
    _maybeLoadMoreOnScroll()
    _scrollRaf = null
  })
}

/**
 * 当前页所有 Movie / Episode 行的字幕语言批量补齐。
 * 原因：jellyfin /Items 列表接口对 Fields=MediaStreams 经常只返回精简版（不含 streams
 * 子字段），列表里的 subtitle_langs 因此始终为空。这里用 /Items?Ids=xxx 模式重拉一次
 * 拿到完整 MediaStreams 再 patch 回 row.subtitle_langs。
 * 仅对当前页的 Movie / Episode 拉（Season / Series 没字幕概念）。
 */
const fetchSubtitleLangsForItems = async () => {
  if (!visibleStats.value.subtitle) return  // 字幕列没开 → 不浪费请求
  const ids = items.value
    .filter(x => x.id && (x.type === 'Movie' || x.type === 'Episode'))
    .map(x => x.id)
  if (!ids.length) return
  try {
    const res = await jellyfinApi.itemsSubtitleLangs(ids)
    const map = res.data?.langs || {}
    for (const row of items.value) {
      if (row.id in map) {
        row.subtitle_langs = map[row.id]
      }
    }
  } catch (e) {
    console.warn('字幕语言批量拉取失败', e)
  }
}

/**
 * 当前页所有 Series 行的聚合摘要（季数/集数/总时长/字幕覆盖）
 * 后端单条结果 1 小时缓存，所以反复进出库页很快
 */
const fetchSeriesAggregates = async () => {
  const seriesIds = items.value
    .filter(x => x.type === 'Series')
    .map(x => x.id)
    .filter(Boolean)
  if (!seriesIds.length) return
  try {
    const res = await jellyfinApi.seriesAggregates(seriesIds)
    const results = res.data?.results || {}
    // 合并：直接赋值到 items 行（Vue 3 对 ref 数组单元的属性变更默认是响应的）
    for (const row of items.value) {
      const agg = results[row.id]
      if (!agg) continue
      // 服务端的 child_count 是季数，aggregate 也是。优先用聚合的（更准）
      if (agg.season_count != null) row.child_count = agg.season_count
      row.episode_count = agg.episode_count
      row.total_runtime_min = agg.total_runtime_min
      row.subtitle_coverage = agg.subtitle_coverage
    }
  } catch (e) {
    // 聚合失败不影响主流程
    console.warn('Series aggregates 加载失败', e)
  }
}

const onSearchSubmit = () => {
  itemsSearch.value = (searchInput.value || '').trim()
  // 无限滚动模式：loadItems() 内部会清空 items + 重置游标，等同于回到 page 1
  loadItems()
}

// 风格下拉首次展开时懒加载库 genres（避免每次进库页都请求）
const onGenrePopoverOpen = async (visible) => {
  if (!visible || _genresLoaded.value) return
  try {
    const res = await jellyfinApi.libraryGenres(id.value)
    genreOptions.value = res.data?.genres || []
    _genresLoaded.value = true
  } catch (e) {
    console.warn('拉取 genres 失败', e)
  }
}

// 当前页有 tmdb_id 且为 Movie / Series 的条目，批量拉取多源评分
// （Season / Episode 的 ProviderIds 即便有 TMDB ID，也不查多源评分 —— 数据不全）
const fetchRatingsForItems = async () => {
  const payload = items.value
    .filter((x) => x.tmdb_id && (x.type === 'Movie' || x.type === 'Series'))
    .map((x) => ({
      tmdb_id: x.tmdb_id,
      media_type: x.type === 'Series' ? 'tv' : 'movie',
      title: x.name || x.title,
      year: x.year || null,
    }))
  if (!payload.length) return
  try {
    const res = await ratingsApi.batch(payload)
    const next = { ...ratingsByKey.value }
    for (const r of res.data.ratings || []) {
      next[`${r.tmdb_id}-${r.media_type}`] = r
    }
    ratingsByKey.value = next
  } catch (e) {
    console.warn('评分批量拉取失败', e)
  }
}

// 表格用：item → 评分对象（找不到返回 null）
const ratingFor = (row) => {
  const mt = (row.type || '').toLowerCase().includes('series') ? 'tv' : 'movie'
  return ratingsByKey.value[`${row.tmdb_id}-${mt}`] || null
}

const findDuplicates = async () => {
  dupLoading.value = true
  try {
    if (dupMode.value === 'metadata') {
      // 基于 Jellyfin 元数据（瞬时返回，无需扫盘）
      const res = await mediaApi.findDuplicatesByMetadata(id.value)
      dupResult.value = res.data
    } else {
      // 旧 hash 模式：仍按 path 扫盘
      if (!library.value?.locations.length) return
      if (dupPath.value === '__all__') {
        const merged = { total_videos: 0, potential_duplicates: 0, groups: [] }
        for (const loc of library.value.locations) {
          try {
            const r = await mediaApi.findDuplicates(loc)
            merged.total_videos += r.data.total_videos || 0
            merged.potential_duplicates += r.data.potential_duplicates || 0
            merged.groups.push(...(r.data.groups || []))
          } catch {}
        }
        dupResult.value = merged
      } else {
        const path = dupPath.value || library.value.locations[0]
        const res = await mediaApi.findDuplicates(path)
        dupResult.value = res.data
      }
    }
  } catch (e) {
    ElMessage.error('检测失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    dupLoading.value = false
  }
}

// 切换 mode 后清空旧结果，避免显示错位
watch(dupMode, () => {
  dupResult.value = null
  dupKeepMap.value = {}
})

// 切回 dialog 关闭时清空选择
watch(showDupDialog, (v) => {
  if (!v) dupKeepMap.value = {}
})

// ============================================================================
// 重复检测 - "勾选保留 + 删除其他" 逻辑
// ============================================================================

// groupKey: 每个组的稳定标识（存 dupKeepMap 用）
// metadata 模式有 group.key（"tmdb:12345"），hash 模式没有 → 用 idx 兜底
const dupGroupKey = (group, idx) => group.key || `idx:${idx}`

// fileKey: 每个文件的稳定标识；metadata 用 jellyfin_id，hash 用 path
const dupFileKey = (file) => file.jellyfin_id || file.path

// dupKeepMap: { groupKey -> 该组保留哪个 fileKey }
// 默认进来时为空；用户首次操作前自动 init 为该组第一个（最大的，因 backend 已按 size 降序）
const dupKeepMap = ref({})
// dupDeleting: { groupKey -> 是否在删除中 }（按钮 loading）
const dupDeleting = ref({})

// 当 dupResult 变化时给每组初始化默认保留项 = 第一个文件（已按 size 降序，通常最大版本）
watch(dupResult, (val) => {
  if (!val?.groups?.length) return
  const next = {}
  for (let i = 0; i < val.groups.length; i++) {
    const g = val.groups[i]
    const key = dupGroupKey(g, i)
    // 保留旧选择，没有就用第一项
    next[key] = dupKeepMap.value[key] || (g.files[0] ? dupFileKey(g.files[0]) : null)
  }
  dupKeepMap.value = next
})

const canDeleteOthers = (group, idx) => {
  if (!group?.files || group.files.length < 2) return false
  return !!dupKeepMap.value[dupGroupKey(group, idx)]
}

// 组标题主行：取一个有识别度的文件名（默认第一个最大版本，去扩展名）
// 给整组一个"看到就知道是哪部"的代号
const groupHeadline = (group) => {
  const first = group?.files?.[0]
  if (first?.name) {
    // 去 .mkv / .mp4 等扩展名（保留中间的 dot 部分）
    return String(first.name).replace(/\.(mkv|mp4|avi|wmv|mov|flv|webm|m4v|ts|rmvb)$/i, '')
  }
  // 兜底：metadata 模式没 file.name 时退回 group.title；hash 模式退回 size
  if (group?.title) return group.title
  if (group?.size_mb) return `${group.size_mb} MB`
  return '(未命名组)'
}

// 副标题：metadata 模式显示 "标题 (年份)"；hash 模式显示 "约 N MB"
// 与 headline 信息互补，避免文件名 release tag 太杂时一眼看不出剧/年份
const groupSubline = (group) => {
  if (group?.title) {
    // metadata 模式：用 jellyfin Item.Name + year
    const tail = group.year ? ` (${group.year})` : ''
    // 如果 headline 已经包含了 title，就不再重复（节省横向空间）
    const headline = (group?.files?.[0]?.name || '').toLowerCase()
    if (headline && headline.includes(String(group.title).toLowerCase())) {
      return tail.trim() || ''
    }
    return `${group.title}${tail}`
  }
  if (group?.size_mb) return `约 ${group.size_mb} MB`
  return ''
}

// 删除一组中除"保留"之外的全部文件
// metadata 模式：直接 jellyfinApi.deleteItem(file.jellyfin_id)
// hash 模式：先 jellyfinApi.lookupByPath(file.path) 拿到 id 再删
const deleteOthersInGroup = async (group, idx) => {
  const key = dupGroupKey(group, idx)
  const keepKey = dupKeepMap.value[key]
  if (!keepKey) {
    ElMessage.warning('请先勾选要保留的文件')
    return
  }
  const toDelete = group.files.filter(f => dupFileKey(f) !== keepKey)
  if (!toDelete.length) {
    ElMessage.info('没有可删除的文件（只有 1 项）')
    return
  }

  // 让用户确认 —— 强警告：Jellyfin 的 item.Path 可能是目录而非单视频文件
  // 删除时会调 Jellyfin DELETE，失败会落 fallback；新版 fallback 已加严格目录保护，
  // 但仍要让用户最后过一眼具体路径以防误操作（曾发生 fallback rmtree 整个暂存目录的事故）
  const fileList = toDelete
    .map(f => {
      const path = f.path || '(无路径)'
      const looksLikeDir = !/\.(mkv|mp4|avi|wmv|mov|flv|webm|m4v|ts|rmvb)$/i.test(path)
      const tag = looksLikeDir
        ? '<span style="color:#dc2626;font-size:11px;font-weight:600;margin-left:6px">⚠️ 目录</span>'
        : ''
      return `<li><code style="font-size:12px;color:#475569;word-break:break-all">${path}</code>${tag}</li>`
    })
    .join('')
  try {
    await ElMessageBox.confirm(
      `<div>
        <p style="margin:0 0 8px">将删除以下 <b>${toDelete.length}</b> 个条目（含物理文件）：</p>
        <ul style="max-height:280px;overflow:auto;margin:8px 0;padding-left:20px">${fileList}</ul>
        <div style="background:#fef2f2;border-left:3px solid #dc2626;padding:8px 10px;margin-top:8px;font-size:12px;line-height:1.5">
          <div style="color:#dc2626;font-weight:600">⚠️ 含路径前请仔细核对每条 path</div>
          <div style="color:#7f1d1d;margin-top:4px">
            带"⚠️ 目录"标记的项 path 不像单视频文件（无视频扩展名），<b>表示该 jellyfin item 的 Path 指向一个目录</b>。
            后端会先尝试 Jellyfin 自带 DELETE，失败时 fallback 物理删除：仅当目录里只含本作品视频 + 附件、且不在「待整理 / 暂存」黑名单时才会 rmtree，否则只删该作品视频文件保留容器目录。
          </div>
        </div>
        <p style="color:#dc2626;margin-top:8px;font-weight:500">此操作不可撤销，请谨慎</p>
      </div>`,
      '确认删除',
      {
        confirmButtonText: `删除 ${toDelete.length} 个`,
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
        type: 'warning',
        dangerouslyUseHTMLString: true,
        customClass: 'dup-delete-confirm',
      }
    )
  } catch {
    return
  }

  dupDeleting.value = { ...dupDeleting.value, [key]: true }
  let okCount = 0
  let failCount = 0
  const errors = []

  for (const f of toDelete) {
    let jellyfinId = f.jellyfin_id
    // hash 模式没 jellyfin_id，先反查
    if (!jellyfinId && f.path) {
      try {
        const r = await jellyfinApi.lookupByPath(f.path)
        if (r.data?.found) jellyfinId = r.data.item?.id
      } catch (e) {
        // 反查失败也继续，下一步会判失败
      }
    }
    if (!jellyfinId) {
      failCount++
      errors.push(`${f.name || f.path}：未在 Jellyfin 找到对应 Item`)
      continue
    }
    try {
      await jellyfinApi.deleteItem(jellyfinId)
      okCount++
    } catch (e) {
      failCount++
      errors.push(`${f.name || f.path}：${e.response?.data?.detail || e.message}`)
    }
  }

  dupDeleting.value = { ...dupDeleting.value, [key]: false }

  if (okCount && !failCount) {
    ElMessage.success(`已删除 ${okCount} 个文件`)
  } else if (okCount && failCount) {
    ElMessage.warning(`删除 ${okCount} 个，失败 ${failCount} 个`)
    console.warn('部分删除失败：', errors)
  } else {
    ElMessage.error('删除失败：' + (errors[0] || '未知原因'))
    console.error('删除失败：', errors)
  }

  // 重新检测刷新结果
  if (okCount > 0) {
    await findDuplicates()
  }
}

const onRefreshConfirm = async (mode) => {
  refreshing.value = true
  try {
    await jellyfinApi.refreshLibrary(id.value, mode)
    ElMessage.success({
      message: `已通知 Jellyfin 刷新（模式：${MODE_LABELS[mode]}）`,
      duration: 4000,
    })
    showRefreshDialog.value = false
  } catch (e) {
    console.error(e)
  } finally {
    refreshing.value = false
  }
}

const MODE_LABELS = {
  scan_changes: '扫描新的和有修改的文件',
  missing_metadata: '搜索缺少的元数据',
  replace_all: '覆盖所有元数据',
}

const formatSize = (bytes) => {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024
    i++
  }
  return `${bytes.toFixed(1)} ${units[i]}`
}

onMounted(async () => {
  loadStatsPrefs(id.value)
  await loadAll()
  // window capture 抓所有滚动事件（含 .app-main 内部 / .el-card__body 内部）
  window.addEventListener('scroll', onWindowScroll, { passive: true, capture: true })
  writeDebug()
  updateScrollRow()
})

// 切换不同库（router 复用同组件）：节流时钟重置即可
watch(() => id.value, async (newId) => {
  loadStatsPrefs(newId)
  await nextTick()
  _loadMoreFiredAt = 0
  writeDebug()
})

// items 数量 / wanted / 视图模式变化 → 刷新 debug
watch([() => items.value.length, wanted, viewMode], () => writeDebug())

onUnmounted(() => {
  stopSubtitlePoll()
  if (prefetchTimer) { clearTimeout(prefetchTimer); prefetchTimer = null }
  window.removeEventListener('scroll', onWindowScroll, { capture: true })
  if (_scrollRaf) cancelAnimationFrame(_scrollRaf)
  // 离开本页时关掉侧边栏的 debug 显示
  debugInfo.enabled = false
})
</script>

<style lang="scss" scoped>
.page-header {
  .header-left {
    display: flex;
    align-items: center;
    gap: 12px;

    h2 {
      display: flex;
      align-items: center;
      gap: 8px;

      .lib-icon {
        color: #6366f1;
      }
    }
  }

  .header-right {
    display: flex;
    gap: 8px;
  }
}

.top-row {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) 2fr;
  gap: 16px;
  margin-bottom: 16px;

  // 窄屏堆叠
  @media (max-width: 900px) {
    grid-template-columns: 1fr;
  }
}

.paths-card,
.stats-card {
  height: 100%;

  // 头部高度收紧
  :deep(.el-card__header) {
    padding: 8px 14px;
  }
  :deep(.el-card__body) {
    padding: 8px 14px;
  }
}

.cache-hint {
  margin-left: 6px;
  color: #94a3b8;
  font-size: 12px;
  font-weight: normal;
}

.paths-card {
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .paths-list {
    .path-row {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 2px 0;
      font-size: 13px;
      color: #475569;
      line-height: 1.6;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;

      .path-text {
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .ok { color: #10b981; }
      .fail { color: #ef4444; }
    }
  }
}

.stats-card {
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  // 6 项指标一行展示（卡片式）：label 上小字 / value 下大字
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 8px;

    // 中等宽度退化为 3 列两行；窄屏 2 列
    @media (max-width: 900px) {
      grid-template-columns: repeat(3, 1fr);
    }
    @media (max-width: 480px) {
      grid-template-columns: repeat(2, 1fr);
    }

    .stat-card {
      display: flex;
      flex-direction: row;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
      padding: 8px 12px;
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 6px;
      transition: border-color 0.15s, transform 0.15s;

      &:hover {
        border-color: #c7d2fe;
        transform: translateY(-1px);
      }

      &.warn {
        border-color: #fca5a5;
        background: #fef2f2;

        .stat-value {
          color: #ef4444;
        }
      }

      .stat-label {
        font-size: 12px;
        color: #64748b;
        line-height: 1.2;
        white-space: nowrap;
        flex-shrink: 0;
      }

      .stat-value {
        font-size: 18px;
        font-weight: 700;
        color: #0f172a;
        line-height: 1.2;
        text-align: right;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;

        small {
          font-size: 11px;
          color: #64748b;
          margin-left: 1px;
          font-weight: 400;
        }
      }
    }
  }
}

// 快捷操作：纯按钮组（无 card 包裹），位于媒体路径上方
.quick-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

// 让整个页面 flex column 占满 .app-main 给的高度，items-card 自动吃剩余空间
.page-container {
  display: flex;
  flex-direction: column;
  // 占满 .app-main content 区域（parent 已经扣掉 header + padding）
  height: 100%;
}

.items-card {
  margin-top: 0;
  flex: 1;
  // 关键：min-height: 0 让 flex 子元素能正确收缩；不然内容会撑高 page-container 出页面滚动条
  min-height: 0;
  display: flex;
  flex-direction: column;

  // 内部 body 占满 card 剩余高度，内容溢出在 body 内部滚动（不是页面整体）
  :deep(.el-card__body) {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: auto;
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  .poster-thumb {
    // ★ 关键：必须 inline-flex（不能 block flex），否则会把 .cell 里 Element Plus 注入的
    //   indent placeholder + chevron 挤到上一行，海报独占下一行 → 看起来"按钮在海报上方"
    //   且 Episode 完全没缩进
    display: inline-flex;
    align-items: center;
    padding: 4px 0;
    cursor: pointer;
    text-decoration: none;
    vertical-align: middle;
    transition: transform 0.15s, box-shadow 0.15s;

    color: inherit;

    &:hover {
      transform: translateY(-1px);

      :deep(.el-image) {
        box-shadow: 0 4px 10px rgba(15, 23, 42, 0.18);
      }
    }

    .poster-placeholder {
      display: flex;
      align-items: center;
      justify-content: center;
      background: #f1f5f9;
      border-radius: 3px;
      color: #94a3b8;
      font-size: 11px;
    }
  }

  // 不同层级的海报尺寸（与师哥示例一致）：show/season 56×80，episode 72×40
  .poster-img--movie,
  .poster-img--series,
  .poster-img--season,
  :deep(.poster-img--movie),
  :deep(.poster-img--series),
  :deep(.poster-img--season) {
    width: 56px;
    height: 80px;
    border-radius: 4px;
    object-fit: cover;
    flex-shrink: 0;
  }
  .poster-img--episode,
  :deep(.poster-img--episode) {
    width: 72px;
    height: 40px;
    border-radius: 4px;
    object-fit: cover;
    flex-shrink: 0;
  }

  .item-link {
    color: inherit;       // 跟随表格默认文字色
    text-decoration: none;
    font-size: 14px;
    transition: color 0.15s;

    &:hover {
      color: #1d9e75;
      text-decoration: underline;
    }
  }
  // 顶层（电影 / 剧集）标题：稍大稍粗
  .title--movie,
  .title--series {
    font-size: 15px;
    font-weight: 500;
  }

  // 演员图：不完整 → 红色加粗，完整 → 正常
  .actor-incomplete {
    color: #ef4444;
    font-weight: 600;
  }
  .actor-ok {
    color: #16a34a;
    font-weight: 500;
  }
  .muted { color: #94a3b8; }

  // 统计卡设置 popover：checkbox 列表
  :deep(.stats-toggle-list) {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  :deep(.stats-toggle-title) {
    font-size: 12px;
    color: #909399;
    margin-bottom: 4px;
  }

  // 集数列：Series 行显示季数 + 集数两行
  .count-stack {
    display: flex;
    flex-direction: column;
    align-items: center;
    line-height: 1.3;

    .count-sub {
      font-size: 12px;
      color: #64748b;
    }
  }

  // 评分：统一星标颜色，不分档
  .rating {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-weight: 600;
    font-size: 13px;
    color: #f59e0b;  // 金黄色（star 的语义色）

    .el-icon { font-size: 12px; }
  }

  // ============ Tree-table 展开按钮（完全复刻师哥的实现）============
  // 隐藏 Element Plus 默认箭头图标
  // ============ 隐藏 Element Plus 自动注入的 chevron（我们手画在 row-content 里）============
  :deep(.el-table__expand-icon),
  :deep(.el-table__indent),
  :deep(.el-table__placeholder) {
    display: none !important;
  }
  // 收掉 cell 默认 padding，我们自己在 .row-content 上控
  // 数据行 AND 表头行第一列都要收，否则表头里的"全选"checkbox 会因 el-table 默认 12px padding
  // 比数据行整体右偏 12px（数据行 .row-content padding-left 16，表头同样 16，但表头 cell 还多 12 默认）
  :deep(.el-table__row > td:first-child .cell),
  :deep(.el-table__header-wrapper th:first-child .cell) {
    padding-left: 0;
    padding-right: 0;
  }

  // ============ 左侧大 cell 的内容容器：checkbox + chevron + 海报 + 标题 ============
  :deep(.row-content) {
    display: flex;
    align-items: center;
    gap: 12px;
    padding-top: 6px;
    padding-bottom: 6px;
    padding-right: 16px;
  }
  :deep(.row-content--header) {
    padding-top: 0;
    padding-bottom: 0;
    padding-left: 16px;
  }
  :deep(.row-content .hdr-spacer) { width: 22px; }
  :deep(.row-content .hdr-label) { font-weight: 500; color: #303133; }
  :deep(.row-content .hdr-label--title) { margin-left: 88px; }

  // chevron：22×22 绿色圆按钮，▼ 由 ::before 画
  :deep(.row-chevron) {
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    border: none;
    background: #1d9e75;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: transform 0.25s ease, background 0.2s;
    padding: 0;
  }
  // 默认（折叠态）：▶ 指右 —— 用 border-left 染色画右指三角
  :deep(.row-chevron::before) {
    content: '';
    width: 0;
    height: 0;
    border-top: 5px solid transparent;
    border-bottom: 5px solid transparent;
    border-left: 6px solid #fff;
    margin-left: 2px;  // 视觉居中微调（三角偏左 1-2px 看着才居中）
  }
  :deep(.row-chevron:hover) { background: #0f6e56; }
  // 展开态：旋转 90° 顺时针 → ▼ 指下
  :deep(.row-chevron--expanded) { transform: rotate(90deg); }

  // 没子节点的占位（保持其他列对齐）
  :deep(.row-chevron-spacer) {
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    display: inline-block;
  }


  // 评分单元格容器：Jellyfin 社区评分 + 多源评分 + 字幕覆盖三行
  .rating-cell {
    display: flex;
    flex-direction: column;
    gap: 4px;
    align-items: flex-start;
  }

  // 字幕列：双行 —— 第一行 chip，第二行下载按钮
  .sub-cell {
    display: flex;
    flex-direction: column;
    gap: 4px;
    align-items: flex-start;

    .sub-lang-row {
      display: flex;
      flex-wrap: wrap;
      gap: 3px;
      align-items: center;
    }
    .sub-lang-chip {
      :deep(.el-tag) { padding: 0 6px; }
    }
    .sub-lang-more {
      font-size: 12px;
      color: #6366f1;
      cursor: help;
      padding: 0 4px;
    }
    .sub-dl-btn {
      :deep(.el-button) {
        padding: 2px 6px;
        height: 22px;
        font-size: 12px;
      }
    }
  }

  // 风格类型列：最多显示 3 个 chip，多余的悬停 +N 提示展开
  .genre-cell {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 3px;

    .genre-chip {
      // 紧凑：单行可塞下多个
      :deep(.el-tag) { padding: 0 6px; }
    }
    .genre-more {
      font-size: 12px;
      color: #6366f1;
      cursor: help;
      padding: 0 4px;
    }
  }

  // 字幕覆盖 chip（仅 Series 行显示）
  .subtitle-coverage-chip {
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 8px;
    font-size: 10px;
    font-weight: 500;
    line-height: 14px;
    border: 1px solid;

    &.sub-cov-good { color: #166534; border-color: #86efac; background: #f0fdf4; }
    &.sub-cov-warn { color: #b45309; border-color: #fcd34d; background: #fffbeb; }
    &.sub-cov-bad  { color: #b91c1c; border-color: #fca5a5; background: #fef2f2; }
  }

  // TMDB 详情页链接
  .tmdb-link {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    color: #0ea5e9;
    text-decoration: none;
    font-size: 12px;
    font-family: ui-monospace, monospace;
    transition: color 0.15s;

    &:hover {
      color: #0284c7;
      text-decoration: underline;
    }

    .el-icon {
      font-size: 12px;
    }
  }

  // 忽略 Folder 开关（卡片头右侧）
  .toggle-folder {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;  // 推到最右

    .switch-label {
      font-size: 13px;
      color: #475569;
    }

    .hint-icon {
      color: #94a3b8;
      cursor: help;
      font-size: 14px;
    }
  }

  // 排序栏：chip 风格，活动项品牌色 + 方向箭头
  .sort-bar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px;
    // 不再 margin-right: auto —— toggle-folder 用 margin-left: auto 推到右

    .sort-label {
      font-size: 13px;
      color: #64748b;
      margin-right: 4px;
    }

    .sort-chip {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      padding: 4px 10px;
      font-size: 12px;
      color: #475569;
      background: transparent;
      border: 1px solid #e2e8f0;
      border-radius: 14px;
      cursor: pointer;
      transition: all 0.15s;

      &:hover {
        border-color: #c7d2fe;
        color: #4f46e5;
      }

      &.active {
        background: #6366f1;
        border-color: #6366f1;
        color: #fff;
        font-weight: 500;

        .sort-arrow {
          font-size: 11px;
        }
      }
    }
  }

  // ---- 网格视图 ----
  // 16:9 横版卡片：宽度固定 280px、海报高度固定 158px（≈ 280 * 9/16）。
  // 不依赖 aspect-ratio（在 flex column / grid stretch 场景偶尔被外层布局规则覆盖），
  // 直接写死 width/height 最稳；grid-template-columns 用 auto-fill + 固定值（不要 1fr，
  // 否则多余空间会均摊给已有列 → 卡片宽度跟容器宽度动态变）。
  $grid-card-w: 280px;
  $grid-poster-h: 158px;

  .grid-view {
    display: grid;
    grid-template-columns: repeat(auto-fill, $grid-card-w);
    grid-auto-rows: max-content;  // 行高跟随内容，不被 flex 父级拉伸
    justify-content: start;       // 卡片左对齐，剩余空间留白
    align-content: start;         // 不被父级垂直拉伸
    gap: 18px;
    padding: 18px;
    min-height: 200px;
  }
  .grid-card {
    width: $grid-card-w;
    display: flex;
    flex-direction: column;
    background: #fff;
    border: 1px solid #f1f5f9;
    border-radius: 8px;
    overflow: hidden;
    cursor: pointer;
    transition: transform 0.15s ease, box-shadow 0.15s ease;

    &:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 14px rgba(15, 23, 42, 0.08);
    }

    // 骨架卡片：wanted 推进、数据池还没补到时撑出占位行；Trending 同款 shimmer
    &.grid-card--skeleton {
      pointer-events: none;
      cursor: default;
      &:hover { transform: none; box-shadow: none; }

      .sk-block, .sk-line {
        background: linear-gradient(90deg, #eef2f6 0%, #f7f9fb 50%, #eef2f6 100%);
        background-size: 800px 100%;
        animation: shimmer 1.4s linear infinite;
        border-radius: 3px;
      }
      .sk-poster {
        width: 100%;
        height: $grid-poster-h;
        border-radius: 0;
      }
      .grid-meta {
        padding: 8px 10px;
        .sk-title { height: 16px; width: 78%; margin-bottom: 6px; }
        .sk-year  { height: 12px; width: 38%; }
      }
    }
  }

  // shimmer 动画：跟 Trending.vue 完全一致（反光带从左滑到右）
  @keyframes shimmer {
    0%   { background-position: -800px 0; }
    100% { background-position:  800px 0; }
  }
  .grid-poster-wrap {
    position: relative;
    width: 100%;
    height: $grid-poster-h;     // 固定高，不依赖 aspect-ratio
    flex: 0 0 $grid-poster-h;   // 防止被 flex column 父级压缩 / 拉伸
    background: linear-gradient(135deg, #e2e8f0, #cbd5e1);
  }
  .grid-poster {
    width: 100%;
    height: 100%;
    display: block;
    // 浏览器在缩放时尽量用高质量插值，进一步降低糊感
    :deep(img) {
      image-rendering: -webkit-optimize-contrast;
      image-rendering: high-quality;
    }
  }
  .grid-placeholder {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 24px;
    font-weight: 600;
    color: #64748b;
  }
  .grid-health-dot {
    position: absolute;
    top: 6px;
    left: 6px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.85);

    &--error   { background: #ef4444; }
    &--warning { background: #f59e0b; }
  }
  .grid-meta {
    padding: 10px 12px 12px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
  }
  .grid-title {
    font-size: 14px;
    font-weight: 500;
    color: #1e293b;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .grid-year {
    font-size: 12px;
    color: #94a3b8;
  }

  // 健康单元格：双行布局（第一行圆点+错误码，第二行刮削按钮）
  .health-cell {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .health-cell--problem {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
  }

  .health-line-top {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  // 健康列下方的操作按钮组
  .health-actions {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
  }

  // 紧凑型行内按钮 —— 用原生 <button> 自己控样式，避免 el-button 的多层 padding
  .row-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 22px;
    padding: 0 8px;
    font-size: 12px;
    line-height: 1;
    border-radius: 4px;
    border: 1px solid transparent;
    background: transparent;
    cursor: pointer;
    user-select: none;
    transition: background 0.15s, border-color 0.15s;

    &--primary {
      color: #4f46e5;
      border-color: #c7d2fe;
      background: #eef2ff;

      &:hover {
        background: #6366f1;
        border-color: #6366f1;
        color: #fff;
      }
    }

    &--danger {
      color: #b91c1c;
      border-color: #fecaca;
      background: #fef2f2;

      &:hover {
        background: #ef4444;
        border-color: #ef4444;
        color: #fff;
      }
    }

    // 中性变体（用于"复制路径"这种非破坏性操作）
    &--ghost {
      color: #475569;
      border-color: #e2e8f0;
      background: #f8fafc;

      &:hover {
        background: #475569;
        border-color: #475569;
        color: #fff;
      }
    }
  }

  // 路径单元格：与成人库 AdultLibraryView 同款 —— 同行 [path 文字] [复制图标]
  .path-cell {
    display: flex;
    align-items: center;
    gap: 4px;
    min-width: 0;

    .path-text {
      flex: 1;
      min-width: 0;
      font-family: ui-monospace, monospace;
      font-size: 12px;
      color: #475569;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .path-copy-btn {
      flex-shrink: 0;
      padding: 0 4px;
      height: 22px;
      color: #94a3b8;

      &:hover {
        color: #4f46e5;
      }
      .el-icon {
        font-size: 14px;
      }
    }
  }

  .health-codes {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 3px;

    .health-code-tag {
      padding: 1px 6px;
      font-size: 11px;
      line-height: 1.5;
      border-radius: 8px;
      background: #fef2f2;
      color: #b91c1c;
      border: 1px solid #fecaca;
      white-space: nowrap;

      // warning 类用橙色
      &.code-short_runtime,
      &.code-sample_path,
      &.code-empty_series,
      &.code-nested_main_file {
        background: #fef3c7;
        color: #b45309;
        border-color: #fde68a;
      }
    }
  }

  // 健康状态圆点
  .health-dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    cursor: help;

    &.level-ok      { background: #d1fae5; border: 1px solid #6ee7b7; }
    &.level-warning { background: #f59e0b; box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.18); }
    &.level-error   { background: #ef4444; box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.20); }
  }

  // 表头右侧组：分页 + Folder 开关绑定在一起整体推到行尾
  .header-right-group {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
    flex-wrap: nowrap;  // 组内不允许换行，分页和开关永远贴一起
  }

  // 无限滚动进度文字（替代原 el-pagination）
  .items-progress {
    flex-shrink: 0;
    color: #6b7280;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  // 旧 header-pagination 已被无限滚动取代；样式保留以兼容其他可能引用，flex-shrink 防止压缩
  .header-pagination {
    flex-shrink: 0;
  }
}

// 无限滚动哨兵：默认极薄（仅作 IntersectionObserver 触发器），
// 仅在加载中 / 到底时显示提示文字，避免列表底部出现一段莫名的空白
// 无限滚动哨兵：仅作位置锚点，不可见
.scroll-sentinel {
  height: 1px;
}

// 行号列：弱化展示
.row-index {
  color: #94a3b8;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

// 让表格行的 vertical-align 居中，让海报和文字共存时不偏上
:deep(.item-row td) {
  vertical-align: middle;
}

// 健康有问题的行：hover 时浅色高亮（仅视觉提示，不可点击）
:deep(.row-health-error:hover td) {
  background-color: #fef2f2 !important;
}
:deep(.row-health-warning:hover td) {
  background-color: #fffbeb !important;
}

// 让选择列的复选框更显眼：边框加粗 + 颜色加深 + 尺寸略大
.items-card :deep(.el-table) {
  .el-checkbox__inner {
    width: 18px;
    height: 18px;
    border-color: #94a3b8;
    border-width: 2px;

    &::after {
      // 对勾粗一些
      border-width: 2px;
      height: 9px;
      left: 5px;
    }
  }

  .el-checkbox__input.is-checked .el-checkbox__inner,
  .el-checkbox__input.is-indeterminate .el-checkbox__inner {
    background-color: #6366f1;
    border-color: #6366f1;
  }

  // 表头全选框 hover 时颜色更明显
  .el-checkbox__inner:hover {
    border-color: #6366f1;
  }
}

.loading-block, .error-block {
  padding: 40px;
  text-align: center;
  color: #94a3b8;

  .spin {
    animation: spin 1.2s linear infinite;
    margin-right: 6px;
  }
}

.dup-mode-pick,
.dup-path-pick {
  margin-bottom: 12px;
  padding: 8px 12px;
  background: #f8fafc;
  border-radius: 4px;

  .info-ic {
    margin-left: 4px;
    color: #94a3b8;
    cursor: help;
  }
}

.dup-pick-label {
  color: #475569;
  font-size: 13px;
  margin-right: 8px;
}

.dup-group-title {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;

  .dup-group-name-stack {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
    line-height: 1.3;
  }

  .dup-group-name {
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 13px;
  }

  .dup-group-sub {
    color: #94a3b8;
    font-size: 12px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dup-group-count {
    color: #94a3b8;
    font-size: 12px;
    white-space: nowrap;
    flex-shrink: 0;
  }
}

.dup-summary {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.dup-groups {
  .file-list {
    display: flex;
    flex-direction: column;
    width: 100%;
  }

  // 每个 file 行：3 列 grid（保留 radio | 文件信息 | 大小）
  // 文件信息列允许 name + path 都换行，长名字不再挤压 path
  .file-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
    padding: 10px 4px;
    border-bottom: 1px solid #f1f5f9;
    cursor: pointer;
    transition: background 0.1s ease;
    margin: 0;

    &:last-child {
      border-bottom: none;
    }

    &:hover {
      background: #f8fafc;
    }

    .file-radio {
      // el-radio 内部默认 margin-right: 30px，挤窄；缩到 0
      margin-right: 0;
    }

    .file-meta {
      min-width: 0;     // 允许被 grid 列正确收缩；不加这个长字会撑爆 grid
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .file-name {
      font-weight: 500;
      font-size: 13px;
      color: #1e293b;
      word-break: break-word;
      overflow-wrap: anywhere;       // 长 .release.tag 串也能在任意位置断
      line-height: 1.4;
    }

    .file-path {
      color: #94a3b8;
      font-size: 12px;
      word-break: break-all;
      line-height: 1.4;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }

    .file-size {
      flex-shrink: 0;
    }

    .version-label {
      color: #94a3b8;
      font-size: 12px;
      font-weight: normal;
      margin-left: 4px;
    }
  }
}

.dup-group-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 4px 4px;
  border-top: 1px solid #f1f5f9;
  margin-top: 4px;

  .dup-group-hint {
    color: #94a3b8;
    font-size: 12px;
  }
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>

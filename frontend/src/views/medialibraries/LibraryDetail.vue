<template>
  <div class="page-container lib-detail-root">
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

    <!-- 普通库：以下是原有的 paths / stats / toolbar / 内容预览 -->
    <template v-if="library && !library.is_adult">

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

    <!-- 媒体批量工具栏：inline 显示在路径/统计下方 -->
    <MediaToolbar
      v-if="library"
      ref="mediaToolbarRef"
      :scope="toolbarScope"
      @clear-selection="clearSelection"
    />

    <!-- 排序/筛选栏：独立 sticky div，直接在 .app-main 的 flow 里 sticky，避免 el-card
         overflow:hidden 把 sticky 的滚动容器限定为 el-card 自身（el-card 不滚 → 永不触发）-->
    <div v-if="library" class="items-sort-bar">
      <div class="card-header">
        <!-- 排序栏 + 派生字段 filter -->
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
          <span class="filter-divider" aria-hidden="true">·</span>
          <button
            :class="['sort-chip', 'filter-chip', { active: filterHasHealthIssue }]"
            :title="filterHasHealthIssue ? '点击取消「健康度」过滤' : '只看健康有问题的条目'"
            @click="filterHasHealthIssue = !filterHasHealthIssue"
          >
            <el-icon v-if="filterHasHealthIssue" class="filter-check"><Check /></el-icon>
            健康度
          </button>
          <button
            :class="['sort-chip', 'filter-chip', { active: filterMissingTmdb }]"
            :title="filterMissingTmdb ? '点击取消「缺 TMDB」过滤' : '只看没有 TMDB ID 的条目'"
            @click="filterMissingTmdb = !filterMissingTmdb"
          >
            <el-icon v-if="filterMissingTmdb" class="filter-check"><Check /></el-icon>
            缺 TMDB
          </button>
          <button
            :class="['sort-chip', 'filter-chip', { active: filterMissingSubtitle }]"
            :title="filterMissingSubtitle ? '点击取消「缺字幕」过滤' : '只看缺字幕的条目（依据最近一次字幕扫描结果）'"
            @click="filterMissingSubtitle = !filterMissingSubtitle"
          >
            <el-icon v-if="filterMissingSubtitle" class="filter-check"><Check /></el-icon>
            缺字幕
          </button>
        </div>

        <!-- 搜索框 -->
        <el-input
          v-model="searchInput"
          placeholder="按标题搜索本库..."
          clearable
          size="small"
          style="width: 220px"
          @keyup.enter="onSearchSubmit"
          @clear="onSearchSubmit"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>

        <!-- 年份过滤 -->
        <el-select
          v-model="searchYears"
          multiple collapse-tags collapse-tags-tooltip filterable allow-create
          placeholder="年份" size="small" style="width: 160px"
          @change="onSearchSubmit"
        >
          <el-option v-for="y in yearOptions" :key="y" :label="String(y)" :value="String(y)" />
        </el-select>

        <!-- 风格过滤 -->
        <el-select
          v-model="searchGenres"
          multiple collapse-tags collapse-tags-tooltip filterable
          placeholder="风格/类型" size="small" style="width: 200px"
          @change="onSearchSubmit"
          @visible-change="onGenrePopoverOpen"
        >
          <el-option v-for="g in genreOptions" :key="g" :label="g" :value="g" />
        </el-select>

        <!-- 右侧组 -->
        <div class="header-right-group">
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
    </div>

    <!-- 内容预览：el-card 只包正文，不再使用 header slot -->
    <el-card shadow="never" class="items-card">

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
                loading="lazy"
                decoding="async"
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
              <!-- Jellyfin 社区评分（5 角星）：点击展开多维评分；只 Movie/Series 才有意义 -->
              <div
                v-if="row.community_rating != null && (row.type === 'Movie' || row.type === 'Series')"
                class="grid-rating"
                :class="{ expanded: expandedRatings[row.id] }"
                :title="expandedRatings[row.id] ? '点击收起多维评分' : '点击展开多维评分'"
                @click.stop="toggleRatings(row)"
              >
                <el-icon><Star /></el-icon>
                {{ row.community_rating.toFixed(1) }}
              </div>
              <!-- 5 角星正下方展开的多维评分（竖排）-->
              <RatingsBadges
                v-if="expandedRatings[row.id]
                  && (row.type === 'Movie' || row.type === 'Series')
                  && ratingFor(row)"
                compact
                direction="column"
                :rating="ratingFor(row)"
                class="grid-ratings-expanded"
                @click.stop
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
                  loading="lazy"
                  decoding="async"
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
        <!-- 时长：Movie / Episode 显示单作品时长；Series / Season 显示总时长（Series 从后端聚合，Season 展开后实时算）-->
        <el-table-column label="时长" width="100" align="center" fixed="right">
          <template #default="{ row }">
            <span v-if="(row.type === 'Episode' || row.type === 'Movie') && row.runtime_min">
              {{ formatRuntimeMin(row.runtime_min) }}
            </span>
            <span v-else-if="(row.type === 'Series' || row.type === 'Season') && row.total_runtime_min">
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
            <!-- Episode 行不显示「集数」（本身就是单集，— 没意义）-->
            <span v-else-if="row.type === 'Episode'"></span>
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
              <!-- Series/Season：显示子项字幕覆盖统计 "X / Y"（来自最近一次 subtitle_scan
                   或 Season 展开后的实时聚合，见 loadChildren）。比单写 — 直观很多 -->
              <template v-if="row.type === 'Series' || row.type === 'Season'">
                <span
                  v-if="row.subtitle_coverage"
                  :class="['sub-coverage', subtitleCoverageClass(row.subtitle_coverage.coverage_pct)]"
                  :title="`有字幕 ${row.subtitle_coverage.with_required} / 共 ${row.subtitle_coverage.total_videos} 集`"
                >
                  {{ row.subtitle_coverage.with_required }} / {{ row.subtitle_coverage.total_videos }}
                </span>
                <span v-else class="muted" title="展开查看子项后会聚合显示">—</span>
              </template>

              <!-- Movie / Episode：原行为，语言 chip + 下载字幕按钮 -->
              <template v-else>
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

                <el-button
                  v-if="row.path"
                  size="small"
                  text
                  type="primary"
                  class="sub-dl-btn"
                  @click.stop="openSubtitleDownload(row)"
                >
                  <el-icon><Search /></el-icon>
                  下载字幕
                </el-button>
              </template>
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
            <!-- Season / Episode 没有独立的风格/类型概念，留空（不用 —）-->
            <span v-else-if="row.type === 'Season' || row.type === 'Episode'"></span>
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
              <!-- 评分占位 — Season/Episode 没有独立评分，留空；Series/Movie 没拉到时显示 — -->
              <span
                v-if="row.community_rating == null
                  && !((row.type === 'Series' || row.type === 'Movie') && ratingFor(row))
                  && row.type !== 'Season' && row.type !== 'Episode'"
                class="muted"
              >—</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="TMDB" width="90" align="center" fixed="right">
          <template #default="{ row }">
            <!-- Episode / Season 多数情况下不带独立 TMDB ID（Jellyfin TMDB 插件通常只挂 Series 层），留空 -->
            <span v-if="row.type === 'Episode' || row.type === 'Season'"></span>
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
                  <div class="file-name-row">
                    <span class="file-name">
                      {{ file.name }}
                      <span v-if="file.version_label && file.version_label !== file.name" class="version-label">[{{ file.version_label }}]</span>
                    </span>
                    <el-tag v-if="file.resolution" size="small" type="success" effect="dark" class="tag-resolution">
                      {{ file.resolution }}
                    </el-tag>
                    <el-tag v-if="file.duration_sec" size="small" type="info" class="tag-duration">
                      {{ formatDupDuration(file.duration_sec) }}
                    </el-tag>
                    <el-tag size="small" class="tag-size">{{ formatSize(file.size) }}</el-tag>
                  </div>
                  <div class="file-path">{{ file.path }}</div>
                </div>
              </label>
            </el-radio-group>

            <div class="dup-group-actions">
              <el-button
                size="default"
                type="danger"
                plain
                class="dup-delete-btn"
                :disabled="!canDeleteOthers(group, idx)"
                :loading="dupDeleting[dupGroupKey(group, idx)] || false"
                @click="deleteOthersInGroup(group, idx)"
              >
                <el-icon><Delete /></el-icon>
                删除其它 {{ group.files.length - 1 }} 项（保留勾选的）
              </el-button>
              <span class="dup-group-hint">
                同时会删除磁盘上的视频文件（需要 Jellyfin 设置里允许删除媒体）
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
                  <div class="file-name-row">
                    <span class="file-name">{{ file.name }}</span>
                    <el-tag v-if="file.resolution" size="small" type="success" effect="dark" class="tag-resolution">
                      {{ file.resolution }}
                    </el-tag>
                    <el-tag v-if="file.duration_sec" size="small" type="info" class="tag-duration">
                      {{ formatDupDuration(file.duration_sec) }}
                    </el-tag>
                    <el-tag size="small" class="tag-size">{{ formatSize(file.size) }}</el-tag>
                  </div>
                  <div class="file-path">{{ file.path }}</div>
                </div>
              </label>
            </el-radio-group>

            <div class="dup-group-actions">
              <el-button
                size="default"
                type="danger"
                plain
                class="dup-delete-btn"
                :disabled="!canDeleteOthers(group, idx)"
                :loading="dupDeleting[dupGroupKey(group, idx)] || false"
                @click="deleteOthersInGroup(group, idx)"
              >
                <el-icon><Delete /></el-icon>
                删除其它 {{ group.files.length - 1 }} 项（保留勾选的）
              </el-button>
              <span class="dup-group-hint">
                按文件路径定位 Jellyfin 里的条目后删除，磁盘文件一并清掉
              </span>
            </div>
          </el-collapse-item>
        </el-collapse>

        <el-empty v-else description="未发现重复文件" />
      </div>

      <!-- 进度卡片：扫描中显示 phase/message/percent + 当前文件 -->
      <div v-else-if="dupLoading && dupProgress" class="dup-progress">
        <div class="dup-progress-head">
          <el-tag size="small" :type="dupPhaseTagType(dupProgress.phase)" effect="dark">
            {{ dupPhaseLabel(dupProgress.phase) }}
          </el-tag>
          <span class="dup-progress-msg">{{ dupProgress.message || '处理中...' }}</span>
        </div>
        <el-progress
          :percentage="dupProgress.percent || 0"
          :show-text="true"
          :status="dupProgress.percent >= 100 ? 'success' : ''"
        />
        <div v-if="dupProgress.current" class="dup-progress-current" :title="dupProgress.current">
          <el-icon><Document /></el-icon>
          {{ dupProgress.current }}
        </div>
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
      :selected-count="selectedItems.length"
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
import { ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft, Refresh, MagicStick, Loading, Check, Close, Search, Link, Star,
  VideoCamera, VideoPlay, Headset, Folder, Setting, Delete, DocumentCopy,
  CaretTop, CaretBottom, InfoFilled, Document,
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

// SSE 进度卡片：phase → 标签文案 & tag type
const dupPhaseLabel = (p) => ({
  starting: '准备',
  scanning: '扫盘',
  hashing: '计算 hash',
  probing: 'ffprobe',
  fetching: '拉取元数据',
  grouping: '分组',
}[p] || p || '处理中')

const dupPhaseTagType = (p) => ({
  starting: 'info',
  scanning: 'info',
  hashing: 'warning',
  probing: 'warning',
  fetching: 'info',
  grouping: 'success',
}[p] || 'info')

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
const mediaToolbarRef = ref(null)
const selectedItems = ref([])
// 已展开行 id 集合（仅用于 chevron 状态显示；展开/折叠靠 el-table 内部 store 处理）
const expandedSet = ref(new Set())
// grid 视图：点击海报右上角 5 角星 → 展开/收起该卡的多维评分徽章
const expandedRatings = reactive({})
const toggleRatings = (row) => {
  if (!row || !row.id) return
  expandedRatings[row.id] = !expandedRatings[row.id]
}
// 已懒加载的子节点：{ [parentId]: childrenArray }
// el-table lazy 模式下 row._children 不可靠（取决于 store 内部），自管一份用于级联选择
const childrenMap = ref({})
// 后端单次拉取批量；wanted 推进步长见 stepSize()；预取阈值 = stepSize × 2 见 prefetchIfNeeded
const FETCH_BATCH = 30
const nextStartIndex = ref(0)           // 下一批的 start_index（offset 模型）
// reqSeq 防竞态：任何 reset / 切库 / 改 filter 都 ++；过期回调按 seq 不一致丢弃
let reqSeq = 0
// 首批渲染后直接调 prefetchIfNeeded（async 不阻塞），不再用 setTimeout 延迟
// 触发判定：window scroll capture 抓所有滚动事件（.app-main / .el-card__body / window 都能 catch），
// 然后用 sentinel 的 getBoundingClientRect() 看它离视口底部多近 —— 这个值不在乎谁是真正的滚动容器，
// 永远反映"sentinel 当前显示在视口的哪个位置"
let _loadMoreBusy = false               // 渲染锁：loadMore 后到 DOM 更新完之间不重复触发
const SCROLL_TRIGGER_PX = 600           // sentinel 离视口底 ≤ 600px 触发 loadMore（提前量大些避免滚到底卡住）
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

// 经过过滤后的列表
// hideFolders 主路径已下推到 Jellyfin（ExcludeItemTypes=Folder），这里前端再做一遍是
// 防御性兜底——万一 jellyfin 某种 collection_type 不严格遵守，前端能再剔一道
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
// 只列 Jellyfin 服务端原生支持的字段——这样 sort 永远基于"整库"排序后分页，
// 跟无限滚动天然兼容。原来的"健康度"、"TMDB"两个派生字段改为 filter（见 filterHasHealthIssue / filterMissingTmdb）：
// 用户的真实需求是"列出有问题的"、"列出缺 TMDB 的"，filter 比 sort 更对路也更便宜。
const sortOptions = [
  { field: 'name',   label: '名称' },
  { field: 'type',   label: '类型' },
  { field: 'year',   label: '年份' },
  { field: 'rating', label: '评分' },
  { field: 'added',  label: '加入时间' },
]

const sortField = ref('name')
const sortDir = ref('asc') // 'asc' | 'desc'

// 派生字段 filter（jellyfin 不知道，后端拉全量 + 内存过滤；5min 缓存）
const filterHasHealthIssue = ref(false)
const filterMissingTmdb = ref(false)
// 缺字幕 filter：依据最近一次 subtitle_scan 任务结果，逻辑与"字幕覆盖"统计同源
const filterMissingSubtitle = ref(false)

// 切到不同字段时给个合理默认方向（找高分 / 最近加入时降序更顺手）
const _defaultDir = (field) =>
  ['rating', 'year', 'added'].includes(field) ? 'desc' : 'asc'

const setSort = (field) => {
  if (sortField.value === field) {
    sortDir.value = sortDir.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortField.value = field
    sortDir.value = _defaultDir(field)
  }
}

// 所有排序字段（name/year/rating/type）都下推到 Jellyfin 服务端排好序再分页，前端不再二次排
// hideFolders 过滤只剔除不重排，order 不破坏 → 直接复用 filteredItems
// （旧版 _fieldKey/_compare 前端排序 helper 已删——派生字段改成 filter 后不再需要）
const sortedItems = computed(() => filteredItems.value)

// ============ wanted / 行步长 / 视口测量（对齐 Trending.vue 同款套路）============
// 网格卡片宽度（来自 CSS $grid-card-w）+ gap：cardsPerRow = floor((containerW + gap) / (cardW + gap))
const GRID_CARD_W = 280
const GRID_CARD_GAP = 18
const GRID_POSTER_H = 158
const GRID_VIEW_PADDING = 18    // .grid-view 自身 CSS padding（左右各 18 → 卡片可用宽度需扣 36）

// 网格列数：list 模式恒为 1（el-table 单列），grid 实测容器宽度并扣掉 grid-view 自身 padding
// CSS auto-fill 公式：N <= (content_w + gap) / (card_w + gap)，content_w = clientWidth - 2*padding
const cardsPerRow = () => {
  if (viewMode.value !== 'grid') return 1
  const el = gridViewRef.value
  // 容器没挂上时按 viewport - 侧边栏(220) - app-main padding(40) - el-card body padding(40) - grid-view padding(36) 估
  const clientW = el ? el.clientWidth : Math.max(0, window.innerWidth - 220 - 40 - 40)
  const contentW = Math.max(0, clientW - 2 * GRID_VIEW_PADDING)
  return Math.max(1, Math.floor((contentW + GRID_CARD_GAP) / (GRID_CARD_W + GRID_CARD_GAP)))
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
// 输入按 jellyfin/MediaStreams 的 ISO 639-2/T 小写代码，全部走中文短名
const _SUB_LANG_LABEL = {
  // 常见
  chs: '简', cht: '繁', eng: 'EN', jpn: '日', kor: '韩',
  // 拉丁/欧洲主流
  fre: '法', fra: '法',
  ger: '德', deu: '德',
  spa: '西', ita: '意', rus: '俄', por: '葡',
  // 北欧 / 北海
  dan: '丹', swe: '瑞', nob: '挪', nor: '挪', fin: '芬', nld: '荷', dut: '荷',
  // 中东欧
  ces: '捷', cze: '捷', pol: '波', hun: '匈', ron: '罗', rum: '罗',
  hrv: '克', srp: '塞', slv: '斯洛文', slk: '斯洛伐',
  // 地中海 / 中东
  ell: '希', gre: '希', tur: '土', heb: '希伯',
  ara: '阿拉伯', ukr: '乌',
  // 东南亚 / 南亚
  tha: '泰', vie: '越', ind: '印尼', msa: '马来', may: '马来',
  hin: '印地', ben: '孟加拉',
  // 其它
  yue: '粤', cmn: '简',
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
const subLangLabel = (code) => {
  const k = (code || '').toLowerCase()
  return _SUB_LANG_LABEL[k] || k.toUpperCase().slice(0, 4)
}
const subLangTagType = (code) => _SUB_LANG_TAG_TYPE[(code || '').toLowerCase()] || 'info'

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
      // 回填 Season.child_count：/Shows/{id}/Seasons 端点常不返子 ChildCount，
      // 集数列只能用展开后实际拿到的 children.length 兜底（用户展开过的 Season 都能看到）
      if (row.child_count == null) {
        row.child_count = children.length
      }
      // Season 总时长：把子集 runtime_min 求和；Season 自身的 RunTimeTicks 是空的，
      // 只能展开后聚合（跟 child_count / subtitle_coverage 一个套路）
      const totalMin = children.reduce((s, e) => s + (e.runtime_min || 0), 0)
      if (totalMin > 0) {
        row.total_runtime_min = totalMin
      }
      // Season 字幕覆盖：用刚拉到的子集 subtitle_langs 实时聚合（has any sub → 计入分子）
      // Series 同字段由后端 aggregates 给（精确按 required langs 算），此处仅 Season 用客户端 best-effort
      if (children.length > 0) {
        const total = children.length
        const withSub = children.filter(e => (e.subtitle_langs?.length || 0) > 0).length
        row.subtitle_coverage = {
          total_videos: total,
          with_required: withSub,
          without_required: total - withSub,
          coverage_pct: Math.round(withSub * 100 / total),
        }
      }
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

// 重复检测对话框里的时长单元格：紧凑字符串（"1h32m" / "45m" / "1h"）
// 用于横向比较"同组不同版本的时长是否一致"——差几秒/几分时一眼能看出
const formatDupDuration = (seconds) => {
  if (!seconds || seconds <= 0) return ''
  const total_min = Math.round(seconds / 60)
  if (total_min < 60) return `${total_min}m`
  const h = Math.floor(total_min / 60)
  const m = total_min % 60
  return m ? `${h}h${String(m).padStart(2, '0')}m` : `${h}h`
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
  // 顺序：电影/剧集数（首位，最直观）→ 健康度 → 资源文件数 → 空间 / 时长
  const result = []
  result.push({ label: '电影/剧集数', value: movieSeriesCount })
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
 * 页头"强制刷新"按钮：旁路所有缓存 + F5 当前页面
 *   1. 服务端缓存预清（先 await，让 reload 后默认拉取就是 fresh 数据）：
 *      - seasons / episodes / aggregates 内存缓存（清零）
 *      - 库统计 KV (force=true 让后端 invalidate + 重算)
 *      - 字幕扫描 (force=true 启新 subtitle_scan 任务，reload 后 mount 会接力轮询)
 *   2. 字幕轮询停掉，避免 reload 前最后一刻的状态干扰
 *   3. window.location.reload() —— 前端所有 Vue 状态（selection / expanded / wanted /
 *      滚动位置 / 字幕统计 / 缓存的 children）全部清零，从挂载流程重新拉
 */
const forceRefresh = async () => {
  stopSubtitlePoll()
  const tasks = [
    jellyfinApi.clearChildrenCache(),
    loadStats(true),
  ]
  if (visibleStats.value.subtitle) {
    tasks.push(loadSubtitleStats(true))
  }
  // 全部 settled 后再 reload —— 失败也不阻塞 F5（最坏情况是部分数据走缓存，比卡住强）
  await Promise.allSettled(tasks)
  window.location.reload()
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
    // 缺字幕过滤但还没有扫描结果：提示用户先跑一次扫描
    if (res.data.missing_subtitle_unavailable) {
      ElMessage.warning('还没有可用的字幕扫描结果，请先在工具栏运行"扫描字幕缺失"')
    }
    _fireBatchEnrichments()
  } catch (e) {
    ElMessage.error('加载内容失败: ' + (e.response?.data?.detail || e.message))
    hasMore.value = false
  } finally {
    if (seq === reqSeq) {
      itemsLoading.value = false
      _loadMoreBusy = false  // 释放渲染锁
      // 首批渲染完立刻无条件预取一批；prefetchIfNeeded 自己是 async，不阻塞首批 DOM 渲染
      // force=true 跳过 buffer 阈值（list 模式 wanted 太小会压在阈值边缘漏掉这次预取）
      prefetchIfNeeded(/* force */ true)
    }
  }
}

// 后台预取：数据池剩余不足 4 个 wanted 步长 → 拉下一批补给；不阻塞 wanted 推进
// 库视图卡片偏矮（grid ~210px，list ~80px），一次视口能放 4-7 行 → 阈值用 4 步长才够缓冲
// 设计目标：用户滚到末行时，items 池里已经有数据；首批后立即放出，看不到骨架占位时间
// 自递归：上游一批数量小、用户滚得快 → 一次预取不够时继续预取
// force=true：跳过 buffer 阈值检查（首次 1.5s 延迟预取专用，保证 items 池一定能长起来）
const prefetchIfNeeded = async (force = false) => {
  if (loadingMore.value || !hasMore.value) return
  if (!force && items.value.length - wanted.value >= stepSize() * 4) return
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
  if (seq === reqSeq && hasMore.value && items.value.length - wanted.value < stepSize() * 4) {
    prefetchIfNeeded()
  }
}

// 触底（scroll 监听调）：wanted = max(wanted, (scrolled + visible × 2) × 列数)
// 在视口下方多留一个视口的内容做缓冲（visible 个 row），列表模式下尤其需要 —— 行矮、视口能塞 8~10 行，
// 之前 "+1 行" buffer 实测太薄。grid 视口 3-4 行，加这一个视口也合理
// presetState 由调用方传入（onWindowScroll 那一帧已经测过），避免重复 rect-walk
const loadMore = (presetState = null) => {
  if (itemsLoading.value) return
  if (!hasMore.value && items.value.length <= wanted.value) return
  const { scrolled, visible } = presetState || _currentScrollState()
  // 至少多放一个视口的行做提前量（visible 行）
  const target = Math.max(1, scrolled + visible * 2) * cardsPerRow()
  if (target > wanted.value) {
    wanted.value = target
  }
  // 不管 target 涨没涨，都给 prefetch 一次机会：buffer 阈值才是池子是否补给的决定条件
  prefetchIfNeeded()
}

// 用 rect-walk 实测的滚动状态：scrolled = 已滚过的行数；visible = 视口内可见的行数
// 返回对象以便 loadMore 用 visible 做 buffer，debug 用 total = scrolled + visible
// 滚动容器是 .app-main（整页滚），rect-walk 用它的 getBoundingClientRect 顶/底做视口边界
const _currentScrollState = () => {
  const scroller = document.querySelector('.app-main')
  if (!scroller) return { scrolled: 0, visible: 0 }
  const scrollerRect = scroller.getBoundingClientRect()
  let viewportTop = scrollerRect.top
  // 多层 sticky 元素遮挡内容时把高度补偿到 viewportTop（rect-walk 判定行可见时算"未被遮挡区"）
  // 仅当元素 *当前实际* 钉在视口顶端（rect.top 接近 scrollerRect.top）才补偿——
  // 否则它还在自然位置（在视口下方），并不遮挡 items
  const _stickyOffsetIfAtTop = (selector, expectedStickyTopPx = 0) => {
    const el = document.querySelector(selector)
    if (!el) return 0
    const r = el.getBoundingClientRect()
    // 误差容忍 2px：sticky 渲染时偶尔有 fractional pixel 偏移
    // 返回"视口内可见高度"（处理 top:-20 这种向上 bleed 的情况，bar.top 在视口外）
    return Math.abs(r.top - (scrollerRect.top + expectedStickyTopPx)) < 2
      ? Math.max(0, r.bottom - scrollerRect.top)
      : 0
  }
  // items-sort-bar sticky at top:0（.app-main padding-top 已被 :has 全局规则清 0）；被遮挡时补偿高度
  viewportTop += _stickyOffsetIfAtTop('.items-sort-bar', 0)
  if (viewMode.value === 'list') {
    const header = document.querySelector('.items-card .el-table__header-wrapper')
    if (header) viewportTop += header.offsetHeight
  }
  const viewportBottom = scrollerRect.bottom

  let scrolled = 0
  let visible = 0
  if (viewMode.value === 'list') {
    const bodyEl = document.querySelector('.items-card .el-table__body')
    const rows = bodyEl ? bodyEl.querySelectorAll('tr') : []
    let sawVisible = false
    for (const r of rows) {
      const rect = r.getBoundingClientRect()
      if (rect.bottom <= viewportTop) {
        if (!sawVisible) scrolled++
      } else if (rect.top < viewportBottom) {
        sawVisible = true
        visible++
      } else {
        break
      }
    }
  } else {
    const gridEl = document.querySelector('.items-card .grid-view')
    const cards = gridEl ? gridEl.querySelectorAll('.grid-card') : []
    if (cards.length) {
      let firstColLeft = null
      for (const c of cards) {
        const r = c.getBoundingClientRect()
        if (r.height > 0) { firstColLeft = r.left; break }
      }
      for (const c of cards) {
        const rect = c.getBoundingClientRect()
        if (rect.height < 1) continue
        if (firstColLeft !== null && Math.abs(rect.left - firstColLeft) > 5) continue
        if (rect.bottom <= viewportTop) scrolled++
        else if (rect.top < viewportBottom) visible++
        else break
      }
    }
  }
  return { scrolled, visible }
}

// 简单包装：返回 scrolled + visible（debug 面板的 scrollRow 用这个总数）
const _currentScrollRow = () => {
  const s = _currentScrollState()
  return s.scrolled + s.visible
}

// （_computeWantedFromScroll 已合并到 loadMore；逻辑：target = (row + 1) × cardsPerRow）

// 公共 params 构造：filter / search 共用
// 我们的 sortField → Jellyfin 原生 SortBy 字段映射
// 4 个排序字段都在 jellyfin 原生支持范围内（之前 health/tmdb_bound 派生字段已改成 filter）
const _JELLYFIN_SORT_MAP = {
  name:   'SortName',
  year:   'ProductionYear',
  rating: 'CommunityRating',
  type:   'Type',
  added:  'DateCreated',     // Jellyfin item 加入媒体库的时间
}

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
  // 排序全部下推到 jellyfin（4 个字段都是原生支持的）
  params.sort_by = _JELLYFIN_SORT_MAP[sortField.value] || 'SortName'
  params.sort_order = sortDir.value === 'asc' ? 'Ascending' : 'Descending'
  // hideFolders 下推到后端 ExcludeItemTypes=Folder
  if (hideFolders.value) {
    params.exclude_item_types = 'Folder'
  }
  // 派生字段 filter：后端拉全量 + 内存过滤 + 5min 缓存
  if (filterHasHealthIssue.value) {
    params.has_health_issue = true
  }
  if (filterMissingTmdb.value) {
    params.missing_tmdb = true
  }
  if (filterMissingSubtitle.value) {
    params.missing_subtitle = true
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
//
// 用渲染锁代替时间节流：loadMore 立即推 wanted，新卡片要等 Vue nextTick 才进 DOM。
// 这期间用户继续滚动可能撞到容器 scrollTop 上限（旧 DOM 还没更新）→ 滚轮事件被边界吃掉 →
// 用户感觉"卡死，必须往上滚一点才能再向下" —— 故用 busy flag 而非时间窗，DOM 一更新就放行
// presetState 由 onWindowScroll 那一帧测好的 { scrolled, visible } 传进来
const _maybeLoadMoreOnScroll = (presetState) => {
  if (_loadMoreBusy) return
  if (itemsLoading.value) return
  if (!hasMore.value && items.value.length <= wanted.value) return
  if (!sentinelRef.value) return
  const rect = sentinelRef.value.getBoundingClientRect()
  const viewportBottom = window.innerHeight || document.documentElement.clientHeight
  if (rect.top - viewportBottom > SCROLL_TRIGGER_PX) return
  _loadMoreBusy = true
  loadMore(presetState)
  nextTick(() => { _loadMoreBusy = false })
}

// ============ DEBUG（事后删）：写共享 debugInfo，由 App.vue 侧边栏读取展示 ============
// 跟 Trending.vue 用同一套字段；source 区分页面来源
const writeDebug = () => {
  debugInfo.enabled = true
  debugInfo.source = `library:${viewMode.value}`
  // 网格视图下行/列才有意义；列表视图固定 1 列
  const cols = cardsPerRow()
  const visibleCount = sortedItems.value.length
  debugInfo.cols = cols
  debugInfo.totalRows = cols ? Math.max(1, Math.ceil(visibleCount / cols)) : 0
  // items = 数据池大小（后端拉到本地的条数）
  // wanted = 当前展示目标条数（loadMore 一步加 stepSize 行；items >= wanted 时池里足够）
  debugInfo.items = items.value.length
  debugInfo.wanted = wanted.value
}


// 用户当前已经看到第几行 = 已滚过的行 + 当前视口内可见的行
// offset 用 (scrollerRect.top - elRect.top) —— 两个 boundingClientRect 都在 window 坐标系，
// 相减消掉了滚动容器自身的 window 偏移，得到的是"基准元素的顶被推出滚动容器顶部多少 px"，
// 跟 viewportH = scroller.clientHeight 同坐标系
// 写 debug 面板的 scrollRow。实测逻辑跟 _currentScrollRow 共享，避免两边算法漂移
const updateScrollRow = () => {
  debugInfo.scrollRow = _currentScrollRow()
  writeDebug()
}

let _scrollRaf = null
const onWindowScroll = () => {
  if (_scrollRaf) return
  _scrollRaf = requestAnimationFrame(() => {
    // 一帧只跑一次 _currentScrollState（含 tr 遍历 + rect 计算），结果给 debug 和 loadMore 共用
    const state = _currentScrollState()
    debugInfo.scrollRow = state.scrolled + state.visible
    writeDebug()
    _maybeLoadMoreOnScroll(state)
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

// 进度状态：组件全局，方便 dialog 模板里实时显示
// 形如 {phase: 'scanning'|'hashing'|'probing'|'fetching'|'grouping', message, percent, current?}
const dupProgress = ref(null)
let _dupEs = null   // 当前 EventSource，关闭对话框时主动断流

// 把一系列 SSE event 累加到一个 merged 结果（hash 模式 __all__ 多 location 时用）
const _withToken = (url) => {
  const token = localStorage.getItem('token')
  if (!token) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}token=${encodeURIComponent(token)}`
}
const _runDupStream = (url) => new Promise((resolve, reject) => {
  const es = new EventSource(_withToken(url))
  _dupEs = es
  let resolved = false
  es.onmessage = (e) => {
    let evt
    try { evt = JSON.parse(e.data) } catch { return }
    if (evt.phase === 'done') {
      resolved = true
      es.close()
      _dupEs = null
      resolve(evt.result || {})
    } else if (evt.phase === 'error') {
      resolved = true
      es.close()
      _dupEs = null
      reject(new Error(evt.message || '检测失败'))
    } else {
      dupProgress.value = evt
    }
  }
  es.onerror = () => {
    if (resolved) return    // 服务端推完 done 后浏览器还会触发一次 readyState=CLOSED 的 onerror
    es.close()
    _dupEs = null
    reject(new Error('连接中断'))
  }
})

const findDuplicates = async () => {
  dupLoading.value = true
  dupProgress.value = { phase: 'starting', message: '准备开始...', percent: 0 }
  dupResult.value = null
  try {
    if (dupMode.value === 'metadata') {
      const url = mediaApi.findDuplicatesByMetadataStreamUrl(id.value)
      dupResult.value = await _runDupStream(url)
    } else {
      if (!library.value?.locations.length) return
      if (dupPath.value === '__all__') {
        // 多路径：依次跑每个 location 的流，合并结果
        const merged = { total_videos: 0, potential_duplicates: 0, groups: [] }
        const locs = library.value.locations
        for (let i = 0; i < locs.length; i++) {
          const loc = locs[i]
          dupProgress.value = {
            phase: 'starting',
            message: `路径 ${i + 1}/${locs.length}：${loc}`,
            percent: 0,
          }
          try {
            const r = await _runDupStream(mediaApi.findDuplicatesStreamUrl(loc))
            merged.total_videos += r.total_videos || 0
            merged.potential_duplicates += r.potential_duplicates || 0
            merged.groups.push(...(r.groups || []))
          } catch (e) {
            console.warn('path 检测失败', loc, e)
          }
        }
        dupResult.value = merged
      } else {
        const path = dupPath.value || library.value.locations[0]
        dupResult.value = await _runDupStream(mediaApi.findDuplicatesStreamUrl(path))
      }
    }
  } catch (e) {
    ElMessage.error('检测失败: ' + (e.response?.data?.detail || e.message))
  } finally {
    dupLoading.value = false
    dupProgress.value = null
  }
}

// 切换 mode 后清空旧结果，避免显示错位
watch(dupMode, () => {
  dupResult.value = null
  dupKeepMap.value = {}
})

// 切回 dialog 关闭时清空选择 + 断掉正在跑的 SSE 流
watch(showDupDialog, (v) => {
  if (!v) {
    dupKeepMap.value = {}
    if (_dupEs) {
      try { _dupEs.close() } catch {}
      _dupEs = null
    }
    dupLoading.value = false
    dupProgress.value = null
  }
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
    const picks = selectedItems.value
    if (picks.length > 0) {
      // 选中项：逐个调 /Items/{id}/Refresh（jellyfin 同一端点，传 item id 即可，Recursive=true 默认递归）
      // 并发限制为 4，避免大批选中时一次性轰炸 jellyfin
      const ids = picks.map(it => it.id).filter(Boolean)
      let ok = 0
      let fail = 0
      const CONCURRENCY = 4
      for (let i = 0; i < ids.length; i += CONCURRENCY) {
        const batch = ids.slice(i, i + CONCURRENCY)
        const results = await Promise.allSettled(batch.map(iid => jellyfinApi.refreshLibrary(iid, mode)))
        results.forEach(r => r.status === 'fulfilled' ? ok++ : fail++)
      }
      if (fail === 0) {
        ElMessage.success({
          message: `已通知 Jellyfin 刷新选中的 ${ok} 项（模式：${MODE_LABELS[mode]}）`,
          duration: 4000,
        })
      } else {
        ElMessage.warning({
          message: `刷新完成：成功 ${ok} 项，失败 ${fail} 项（模式：${MODE_LABELS[mode]}）`,
          duration: 5000,
        })
      }
    } else {
      await jellyfinApi.refreshLibrary(id.value, mode)
      ElMessage.success({
        message: `已通知 Jellyfin 刷新（模式：${MODE_LABELS[mode]}）`,
        duration: 4000,
      })
    }
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
  // window capture 抓所有滚动事件（含 .app-main 内部）
  window.addEventListener('scroll', onWindowScroll, { passive: true, capture: true })
  writeDebug()
  updateScrollRow()
})

// 切换不同库（router 复用同组件）：渲染锁释放
watch(() => id.value, async (newId) => {
  loadStatsPrefs(newId)
  await nextTick()
  _loadMoreBusy = false
  writeDebug()
})

// sort / hideFolders / 派生 filter 切换：全部下推到后端 → 触发 reload 重新拉首页
// 派生字段（health_issue / missing_tmdb）由后端拉全量 + 内存过滤 + 5min 缓存承担，
// 前端流程跟普通分页一致，无限滚动天然兼容
watch([sortField, sortDir, hideFolders, filterHasHealthIssue, filterMissingTmdb, filterMissingSubtitle], () => {
  loadItems()
})

// items 数量 / wanted / 视图模式变化 → 刷新 debug
watch([() => items.value.length, wanted, viewMode], () => writeDebug())

// 切视图（list ↔ grid）时回顶部 —— 列表和网格的行高 / 卡片高完全不一样，
// 停留在 list 第 80 行的位置切到 grid 会落到完全不相关的卡片中间，体验割裂
watch(viewMode, () => {
  // 整页改回外层滚动后，回顶 target = .app-main
  const scroller = document.querySelector('.app-main')
  if (scroller) scroller.scrollTop = 0
})

onUnmounted(() => {
  stopSubtitlePoll()
  window.removeEventListener('scroll', onWindowScroll, { capture: true })
  if (_scrollRaf) cancelAnimationFrame(_scrollRaf)
  // 离开本页时关掉侧边栏的 debug 显示
  debugInfo.enabled = false
})
</script>

<!--
  非 scoped 全局规则：仅在本页挂载时（DOM 里存在 .lib-detail-root）生效，
  把 .app-main 的 padding-top 清 0，让 .items-sort-bar 的 sticky top:0 真正贴在视口最顶。
  组件卸载后 .lib-detail-root 消失，:has 不匹配 → .app-main 回到默认 padding:20px，对其他页面无影响。
  顶部呼吸感由 .page-container 自身 padding-top: 20px 补回。
-->
<style>
body:has(.lib-detail-root) .app-main {
  padding-top: 0;
}
</style>

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
        color: var(--jt-brand);
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
  color: var(--jt-text-muted);
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
      color: var(--jt-text-regular);
      line-height: 1.6;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;

      .path-text {
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .ok { color: var(--jt-success); }
      .fail { color: var(--jt-danger); }
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
      background: var(--jt-fill-light);
      border: 1px solid var(--jt-card-border);
      border-radius: 6px;
      transition: border-color 0.15s, transform 0.15s;

      &:hover {
        border-color: var(--jt-brand-light-7);
        transform: translateY(-1px);
      }

      &.warn {
        border-color: var(--jt-danger-border);
        background: var(--jt-danger-tint);

        .stat-value {
          color: var(--jt-danger);
        }
      }

      .stat-label {
        font-size: 12px;
        color: var(--jt-text-secondary);
        line-height: 1.2;
        white-space: nowrap;
        flex-shrink: 0;
      }

      .stat-value {
        font-size: 18px;
        font-weight: 700;
        color: var(--jt-text-primary);
        line-height: 1.2;
        text-align: right;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;

        small {
          font-size: 11px;
          color: var(--jt-text-secondary);
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

// 整页正常 block flow：滚动发生在外层 .app-main，paths/stats 滚走时 MediaToolbar 才能 sticky 起来
// （之前的内部双层滚动模型让 MediaToolbar 的 position:sticky 永远不会触发 stuck，IO 检测也失效）
.page-container {
  // 不再 flex column / height:100%；让内容自然撑开高度，整页在 .app-main 里滚
  // ★ 顶部 20px 补偿：因为 :has 全局规则在本页期间清掉了 .app-main 的 padding-top，
  //   把那段呼吸感搬到 .page-container 自己身上（让 page-header / library 信息卡保持距视口顶的间距），
  //   而 sticky 元素的 top:0 不受这层 padding 影响（sticky 锚点是滚动祖先 .app-main 的 padding-box）
  padding-top: 20px;
}

// 排序/筛选栏：独立 sticky，直接在 .app-main 的 flow 里，不受 el-card overflow 限制
// 视觉上与下方 el-card 拼合（下边框缺省，由 el-card 上边框补）
// ★ 配合本文件末尾 :has 全局规则：本页期间 .app-main 的 padding-top 被清 0，所以这里
//   直接 top: 0 就能贴视口顶（padding 保持对称 12/12，无 bleed）
.items-sort-bar {
  position: sticky;
  top: 0;
  z-index: 5;
  background: var(--jt-card-bg);
  border: 1px solid var(--el-card-border-color, var(--jt-card-border));
  border-radius: var(--el-card-border-radius, 4px) var(--el-card-border-radius, 4px) 0 0;
  padding: 12px 20px;
  margin-bottom: 0;

  // 忽略 Folder 开关（右侧）
  .toggle-folder {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;

    .switch-label {
      font-size: 13px;
      color: var(--jt-text-regular);
    }

    .hint-icon {
      color: var(--jt-text-muted);
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

    .sort-label {
      font-size: 13px;
      color: var(--jt-text-secondary);
      margin-right: 4px;
    }

    .sort-chip {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      padding: 4px 10px;
      font-size: 12px;
      color: var(--jt-text-regular);
      background: transparent;
      border: 1px solid var(--jt-card-border);
      border-radius: 14px;
      cursor: pointer;
      transition: all 0.15s;

      &:hover {
        border-color: var(--jt-brand-light-7);
        color: var(--jt-brand-dark);
      }

      &.active {
        background: var(--jt-brand);
        border-color: var(--jt-brand);
        color: #fff;
        font-weight: 500;

        .sort-arrow {
          font-size: 11px;
        }
      }

      &.filter-chip {
        &.active {
          background: var(--jt-warning);
          border-color: var(--jt-warning);
          color: #fff;
        }
        .filter-check {
          font-size: 11px;
          margin-right: 2px;
        }
      }
    }

    .filter-divider {
      color: var(--jt-text-muted);
      margin: 0 2px;
      user-select: none;
    }
  }

  // 表头右侧组：进度文字 + Folder 开关 + 视图切换，整体推到行尾
  .header-right-group {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
    flex-wrap: nowrap;
  }

  .header-pagination {
    flex-shrink: 0;
  }
}

.items-card {
  margin-top: 0;
  // el-card 顶部圆角去掉、顶边去掉：让上方 sort-bar 的 border-bottom 充当两者共用的分割线
  // （避免 sort-bar 底边 + el-card 顶边叠成双线）
  border-top: none;
  border-top-left-radius: 0;
  border-top-right-radius: 0;

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
      background: var(--jt-divider-light);
      border-radius: 3px;
      color: var(--jt-text-muted);
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
      color: var(--jt-brand);
      text-decoration: underline;
    }
  }
  // 顶层（电影 / 剧集）标题：稍大稍粗
  .title--movie,
  .title--series {
    font-size: 15px;
    font-weight: 500;
  }

  .actor-incomplete {
    color: var(--jt-danger);
    font-weight: 600;
  }
  .actor-ok {
    color: var(--jt-success);
    font-weight: 500;
  }
  .muted { color: var(--jt-text-muted); }

  // 统计卡设置 popover：checkbox 列表
  :deep(.stats-toggle-list) {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  :deep(.stats-toggle-title) {
    font-size: 12px;
    color: var(--jt-text-muted);
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
      color: var(--jt-text-secondary);
    }
  }

  .rating {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-weight: 600;
    font-size: 13px;
    color: var(--jt-warning);

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
  :deep(.row-content .hdr-label) { font-weight: 500; color: var(--jt-text-primary); }
  :deep(.row-content .hdr-label--title) { margin-left: 88px; }

  :deep(.row-chevron) {
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    border: none;
    background: var(--jt-success);
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: transform 0.25s ease, background 0.2s;
    padding: 0;
  }
  :deep(.row-chevron::before) {
    content: '';
    width: 0;
    height: 0;
    border-top: 5px solid transparent;
    border-bottom: 5px solid transparent;
    border-left: 6px solid #fff;
    margin-left: 2px;
  }
  :deep(.row-chevron:hover) { background: var(--jt-brand-dark); }
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
      color: var(--jt-brand);
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
      color: var(--jt-brand);
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

    &.sub-cov-good { color: var(--jt-success-text); border-color: var(--jt-success-border); background: var(--jt-success-tint); }
    &.sub-cov-warn { color: var(--jt-warning-text); border-color: var(--jt-warning-border); background: var(--jt-warning-tint); }
    &.sub-cov-bad  { color: var(--jt-danger-text); border-color: var(--jt-danger-border); background: var(--jt-danger-tint); }
  }

  // 字幕列 Series/Season 行的子项覆盖统计文本（"3 / 24"）
  // 跟 subtitle-coverage-chip 共用三档颜色，但样式更轻量（不画 chip 边框）
  .sub-coverage {
    font-size: 12px;
    font-family: ui-monospace, monospace;
    font-variant-numeric: tabular-nums;
    cursor: help;

    &.sub-cov-good { color: var(--jt-success-text); }
    &.sub-cov-warn { color: var(--jt-warning-text); }
    &.sub-cov-bad  { color: var(--jt-danger-text); }
  }

  .tmdb-link {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    color: var(--jt-link);
    text-decoration: none;
    font-size: 12px;
    font-family: ui-monospace, monospace;
    transition: color 0.15s;

    &:hover {
      color: var(--jt-link-hover);
      text-decoration: underline;
    }

    .el-icon {
      font-size: 12px;
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
    background: var(--jt-card-bg);
    border: 1px solid var(--jt-divider-light);
    border-radius: 8px;
    overflow: hidden;
    cursor: pointer;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
    // 跳过视口外卡片的渲染 / 图片解码，滚动期间主线程压力骤降
    // contain-intrinsic-size 给浏览器一个估算（卡宽 280 + 卡高约 220），避免布局抖动
    content-visibility: auto;
    contain-intrinsic-size: #{$grid-card-w} 220px;

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
        background: linear-gradient(90deg, var(--jt-skeleton-from) 0%, var(--jt-skeleton-to) 50%, var(--jt-skeleton-from) 100%);
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
    height: $grid-poster-h;
    flex: 0 0 $grid-poster-h;
    background: linear-gradient(135deg, var(--jt-card-border), var(--jt-divider));
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
    color: var(--jt-text-secondary);
  }
  .grid-health-dot {
    position: absolute;
    top: 6px;
    left: 6px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.85);

    &--error   { background: var(--jt-danger); }
    &--warning { background: var(--jt-warning); }
  }
  // 海报右上角的 jellyfin 评分胶囊（5 角星 + 数字）—— 点击展开多维评分
  .grid-rating {
    position: absolute;
    top: 6px;
    right: 6px;
    background: rgba(0, 0, 0, 0.7);
    color: #ffd700;
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 12px;
    display: flex;
    align-items: center;
    gap: 3px;
    cursor: pointer;
    transition: background 0.15s, transform 0.15s;
    z-index: 3;
    &:hover { background: rgba(0, 0, 0, 0.85); transform: scale(1.05); }
    &.expanded {
      background: rgba(99, 102, 241, 0.92);
      color: #fff;
    }
  }
  // 5 角星正下方展开的多维评分（竖排，半透明背景在海报上可读）
  .grid-ratings-expanded {
    position: absolute;
    top: 32px;
    right: 6px;
    z-index: 2;
    padding: 4px;
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(4px);
    border-radius: 4px;
    max-width: calc(100% - 12px);
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
    color: var(--jt-text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .grid-year {
    font-size: 12px;
    color: var(--jt-text-muted);
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
      color: var(--jt-brand-dark);
      border-color: var(--jt-brand-light-7);
      background: var(--jt-brand-light-9);

      &:hover {
        background: var(--jt-brand);
        border-color: var(--jt-brand);
        color: #fff;
      }
    }

    &--danger {
      color: var(--jt-danger-text);
      border-color: var(--jt-danger-border);
      background: var(--jt-danger-tint);

      &:hover {
        background: var(--jt-danger);
        border-color: var(--jt-danger);
        color: #fff;
      }
    }

    &--ghost {
      color: var(--jt-text-regular);
      border-color: var(--jt-card-border);
      background: var(--jt-fill-light);

      &:hover {
        background: var(--jt-text-regular);
        border-color: var(--jt-text-regular);
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
      color: var(--jt-text-regular);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .path-copy-btn {
      flex-shrink: 0;
      padding: 0 4px;
      height: 22px;
      color: var(--jt-text-muted);

      &:hover {
        color: var(--jt-brand-dark);
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
      background: var(--jt-danger-tint);
      color: var(--jt-danger-text);
      border: 1px solid var(--jt-danger-border);
      white-space: nowrap;

      &.code-short_runtime,
      &.code-sample_path,
      &.code-empty_series,
      &.code-nested_main_file {
        background: var(--jt-warning-tint);
        color: var(--jt-warning-text);
        border-color: var(--jt-warning-border);
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

    &.level-ok      { background: var(--jt-success-tint); border: 1px solid var(--jt-success-border); }
    &.level-warning { background: var(--jt-warning); box-shadow: 0 0 0 3px rgba(var(--jt-warning-rgb), 0.18); }
    &.level-error   { background: var(--jt-danger); box-shadow: 0 0 0 3px rgba(var(--jt-danger-rgb), 0.20); }
  }

}

// 无限滚动哨兵：默认极薄（仅作 IntersectionObserver 触发器），
// 仅在加载中 / 到底时显示提示文字，避免列表底部出现一段莫名的空白
// 无限滚动哨兵：仅作位置锚点，不可见
.scroll-sentinel {
  height: 1px;
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
  background-color: var(--jt-danger-tint) !important;
}
:deep(.row-health-warning:hover td) {
  background-color: var(--jt-warning-tint) !important;
}

// 让选择列的复选框更显眼：box 放大到 18×18 + 2px 边框 + 紫色高亮
// 关键：内部可用空间 = 18 - 2×2 = 14×14，正好是 Element Plus 默认 box 大小，
// 所以钩子 ::after **完全不动**（默认 3×7 / left:4 / top:1 / border:1px），自然居中不歪
.items-card :deep(.el-table) {
  .el-checkbox__inner {
    width: 18px;
    height: 18px;
    border-color: var(--jt-text-muted);
    border-width: 2px;
    border-radius: 3px;
  }

  .el-checkbox__input.is-checked .el-checkbox__inner,
  .el-checkbox__input.is-indeterminate .el-checkbox__inner {
    background-color: var(--jt-brand);
    border-color: var(--jt-brand);
  }

  .el-checkbox__inner:hover {
    border-color: var(--jt-brand);
  }
}

.loading-block, .error-block {
  padding: 40px;
  text-align: center;
  color: var(--jt-text-muted);

  .spin {
    animation: spin 1.2s linear infinite;
    margin-right: 6px;
  }
}

.dup-mode-pick,
.dup-path-pick {
  margin-bottom: 12px;
  padding: 8px 12px;
  background: var(--jt-fill-light);
  border-radius: 4px;

  .info-ic {
    margin-left: 4px;
    color: var(--jt-text-muted);
    cursor: help;
  }
}

.dup-pick-label {
  color: var(--jt-text-regular);
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
    color: var(--jt-text-muted);
    font-size: 12px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dup-group-count {
    color: var(--jt-text-muted);
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

// 扫描进度卡片：phase 标签 + message + el-progress + current file
.dup-progress {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 24px 20px;
  background: var(--jt-fill-light);
  border-radius: 8px;
  border: 1px solid var(--jt-card-border);

  .dup-progress-head {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .dup-progress-msg {
    font-size: 13px;
    color: var(--jt-text-regular);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  .dup-progress-current {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--jt-text-muted);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    padding-left: 4px;
  }
}

.dup-groups {
  .file-list {
    display: flex;
    flex-direction: column;
    width: 100%;
  }

  // 每个 file 行：2 列 grid（保留 radio | 文件信息）
  // 文件信息列：标题行 (name + 分辨率/时长/大小 tag) + 路径行
  .file-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
    gap: 12px;
    padding: 10px 4px;
    border-bottom: 1px solid var(--jt-divider-light);
    cursor: pointer;
    transition: background 0.1s ease;
    margin: 0;

    &:last-child {
      border-bottom: none;
    }

    &:hover {
      background: var(--jt-fill-light);
    }

    .file-radio {
      // el-radio 内部默认 margin-right: 30px，挤窄；缩到 0
      margin-right: 0;
    }

    .file-meta {
      min-width: 0;     // 允许被 grid 列正确收缩；不加这个长字会撑爆 grid
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    // 标题行：文件名 + 分辨率/时长/大小 tag 同排
    // flex-wrap 让窄屏时 tag 整体掉到下一行，但不会被截断
    .file-name-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
      min-width: 0;
    }

    .file-name {
      font-weight: 500;
      font-size: 13px;
      color: var(--jt-text-primary);
      word-break: break-word;
      overflow-wrap: anywhere;       // 长 .release.tag 串也能在任意位置断
      line-height: 1.4;
      flex: 1 1 auto;                // 占满剩余宽度，把 tag 推到右侧
      min-width: 0;
    }

    .file-path {
      color: var(--jt-text-muted);
      font-size: 12px;
      word-break: break-all;
      line-height: 1.4;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }

    // 标题行内的 tag：分辨率（success 绿）/ 时长（info 灰蓝）/ 大小（默认灰）
    // 三个一组紧贴文件名右侧，size=small 视觉更轻
    .tag-resolution,
    .tag-duration,
    .tag-size {
      flex-shrink: 0;
    }

    .version-label {
      color: var(--jt-text-muted);
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
  border-top: 1px solid var(--jt-divider-light);
  margin-top: 4px;

  .dup-delete-btn {
    padding-inline: 18px;
    font-weight: 600;
    letter-spacing: 0.5px;
    border-width: 1.5px;

    &:not(:disabled):hover {
      transform: translateY(-1px);
      box-shadow: 0 2px 6px rgba(220, 38, 38, 0.25);
    }
    &:not(:disabled):active {
      transform: translateY(0);
    }

    .el-icon {
      margin-right: 4px;
    }
  }

  .dup-group-hint {
    color: var(--jt-text-muted);
    font-size: 12px;
  }
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>

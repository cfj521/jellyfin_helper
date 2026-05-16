<template>
  <div class="page-container">
    <div class="page-header">
      <h2>下载管理 (qBittorrent)</h2>
      <div class="header-actions">
        <a
          v-if="qbitUrl"
          class="ext-link"
          :href="qbitUrl"
          target="_blank"
          rel="noopener noreferrer"
          title="在新窗口打开 qBittorrent Web UI"
        >
          <el-icon><Link /></el-icon>
          打开 qBittorrent
        </a>
      </div>
    </div>

    <!-- 顶部全局状态：配额 + 全局速度 + 限速 -->
    <el-card shadow="never" class="status-card">
      <div class="status-row">
        <!-- 配额状态条 -->
        <div class="quota-block">
          <div class="quota-line">
            <span class="quota-label">下载目录配额</span>
            <span v-if="quota.state === 'disabled'" class="quota-value state-disabled" title="后端 stat 不到下载盘（路径不存在或容器未挂载），配额监视/清理已禁用">
              未挂载下载盘 · 配额功能禁用
            </span>
            <span v-else :class="['quota-value', `state-${quota.state}`]">
              {{ formatSize(quota.used) }} / {{ formatSize(quota.limit) }}
              ({{ Math.round(quota.ratio * 100) }}%)
            </span>
            <el-button
              size="small"
              type="warning"
              :icon="Brush"
              :loading="cleanupLoading"
              :disabled="quota.state === 'disabled'"
              @click="onCleanup"
              title="清理已成功转移到媒体库、且分享率 / 做种天数达标的种子（未转移的不会动）"
            >清理</el-button>
          </div>
          <div class="quota-bar-wrap" v-if="quota.state !== 'disabled'">
            <div class="quota-bar">
              <div :class="['quota-bar-fill', `state-${quota.state}`]" :style="{ width: Math.min(quota.ratio * 100, 100) + '%' }"></div>
            </div>
          </div>
        </div>

        <!-- 全局速度 -->
        <div class="speed-block">
          <div class="speed-line">
            <span class="speed-icon">⬇</span>
            <span class="speed-value">{{ formatSize(transfer.dl_info_speed) }}/s</span>
            <span class="speed-icon" style="margin-left: 16px">⬆</span>
            <span class="speed-value">{{ formatSize(transfer.up_info_speed) }}/s</span>
          </div>
          <div class="speed-line speed-totals">
            总下载 {{ formatSize(transfer.dl_info_data) }} · 总上传 {{ formatSize(transfer.up_info_data) }}
          </div>
        </div>

        <!-- 限速控件 -->
        <div class="speed-limit-block">
          <el-popover
            placement="bottom"
            trigger="click"
            :width="220"
            v-model:visible="speedPopoverVisible"
          >
            <template #reference>
              <el-button size="small" :icon="Setting">限速</el-button>
            </template>
            <div class="speed-limit-form">
              <div class="row">
                <span class="lbl">下载</span>
                <el-input-number
                  v-model="speedLimitForm.dl_kbs"
                  :min="0"
                  controls-position="right"
                  size="small"
                  style="width: 110px"
                  @blur="applySpeedLimit"
                />
                <span class="unit">KB/s</span>
              </div>
              <div class="row">
                <span class="lbl">上传</span>
                <el-input-number
                  v-model="speedLimitForm.up_kbs"
                  :min="0"
                  controls-position="right"
                  size="small"
                  style="width: 110px"
                  @blur="applySpeedLimit"
                />
                <span class="unit">KB/s</span>
              </div>
            </div>
          </el-popover>
          <el-button
            size="small"
            :type="transfer.alt_speed_enabled ? 'success' : 'default'"
            :icon="Moon"
            @click="onToggleAltSpeed"
            title="备用限速模式（夜间限速）"
          >
            备用限速
          </el-button>
        </div>
      </div>
    </el-card>

    <!-- 主内容 tabs：种子列表 / RSS（待审核改为行内"人工审核"按钮 + modal） -->
    <el-tabs v-model="activeTab" class="main-tabs">
      <el-tab-pane label="种子列表" name="torrents" />
      <el-tab-pane label="RSS 订阅" name="rss" />
    </el-tabs>

    <div v-show="activeTab === 'torrents'">
    <!-- 种子列表工具栏：添加 + 过滤 + 选中后内嵌批量按钮（实时静默轮询，无需刷新按钮） -->
    <div class="torrents-toolbar">
      <el-button type="primary" :icon="Plus" @click="showAddDialog = true">
        添加种子
      </el-button>
      <el-select v-model="filter" style="width: 140px" @change="load()">
        <el-option label="全部" value="all" />
        <el-option label="下载中" value="downloading" />
        <el-option label="已完成" value="completed" />
        <el-option label="做种中" value="seeding" />
        <el-option label="已暂停" value="paused" />
      </el-select>

      <!-- 选中后才出现的批量按钮组（靠右对齐，无背景）-->
      <div v-if="selected.length" class="bulk-group">
        <span class="bulk-info">已选 {{ selected.length }} 项</span>
        <el-button size="small" @click="bulkAction('pause')">暂停</el-button>
        <el-button size="small" @click="bulkAction('resume')">恢复</el-button>
        <el-button size="small" @click="bulkAction('force_start')">强制启动</el-button>
        <el-button size="small" @click="bulkAction('recheck')">重新校验</el-button>
        <el-button size="small" type="danger" @click="bulkDelete">删除</el-button>
        <el-button size="small" :icon="Close" circle title="取消选择" @click="clearSelected" />
      </div>
    </div>

    <el-card shadow="never">
      <el-table
        :data="qbitTorrents"
        v-loading="loading"
        max-height="600"
        row-key="hash"
        size="small"
        class="downloads-table"
        @selection-change="onSelectionChange"
        ref="tableRef"
      >
        <el-table-column type="selection" width="36" reserve-selection />
        <el-table-column prop="name" label="名称" min-width="220" show-overflow-tooltip />
        <el-table-column label="进度" width="120">
          <template #default="{ row }">
            <el-progress :percentage="Math.round((row.progress || 0) * 100)" :status="row.progress >= 1 ? 'success' : ''" />
          </template>
        </el-table-column>
        <el-table-column label="大小" width="70">
          <template #default="{ row }">{{ formatSize(row.size) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="62">
          <template #default="{ row }">
            <el-tag :type="stateType(row.state)" size="small">{{ stateLabel(row.state) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="下载速度" width="92">
          <template #default="{ row }">{{ formatSize(row.dlspeed) }}/s</template>
        </el-table-column>
        <el-table-column label="上传速度" width="92">
          <template #default="{ row }">{{ formatSize(row.upspeed) }}/s</template>
        </el-table-column>
        <el-table-column label="ETA" width="54">
          <template #default="{ row }">{{ formatEta(row.eta) }}</template>
        </el-table-column>
        <el-table-column label="分享率" width="58">
          <template #default="{ row }">{{ row.ratio?.toFixed(2) || '-' }}</template>
        </el-table-column>
        <el-table-column label="目标库" width="78">
          <template #default="{ row }">
            <span v-if="row.dispatch?.target_library_name" class="lib-tag" :title="`media_type: ${row.dispatch.media_type || '未识别'}`">
              {{ row.dispatch.target_library_name }}
            </span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="保存路径" width="260" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.content_path" class="path-text" :title="row.content_path">
              {{ row.content_path }}
            </span>
            <span v-else class="status-error" title="qB 未返回 content_path（种子可能还没拿到 metadata）">
              缺 content_path
            </span>
          </template>
        </el-table-column>
        <el-table-column label="目标路径" width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.dispatch?.target_path" class="path-text" :title="row.dispatch.target_path">
              {{ row.dispatch.target_path }}
            </span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="阶段" width="92">
          <template #default="{ row }">
            <!-- 待审核状态特殊渲染：橙色 tag，跟普通阶段视觉区分 -->
            <el-tag
              v-if="row.dispatch?.phase_status === 'needs_review'"
              size="small"
              type="warning"
              effect="dark"
            >待审核</el-tag>
            <el-tag
              v-else-if="row.dispatch?.phase"
              size="small"
              :type="phaseTagType(row.dispatch.phase, row.dispatch.phase_status)"
            >{{ phaseLabel(row.dispatch.phase) }}</el-tag>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="状态详情" width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span
              v-if="row.dispatch?.status_message"
              :class="['status-msg', `status-${row.dispatch.phase_status || 'running'}`]"
              :title="row.dispatch.status_message"
            >{{ row.dispatch.status_message }}</span>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="添加时间" width="100">
          <template #default="{ row }">{{ formatAddedOn(row.added_on) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="132" fixed="right">
          <template #default="{ row }">
            <!-- 待审核行：主操作变成"人工审核"，点开 modal -->
            <el-button
              v-if="row.dispatch?.phase_status === 'needs_review'"
              size="small"
              type="warning"
              @click="openReview(row)"
            >人工审核</el-button>
            <template v-else>
              <el-tooltip
                v-if="isPipelineBusy(row)"
                :content="busyTooltip(row)"
                placement="top"
              >
                <!-- disabled 按钮要包一层 span 才能正常响应 hover 触发 tooltip -->
                <span>
                  <el-button v-if="isPaused(row.state)" size="small" disabled>恢复</el-button>
                  <el-button v-else size="small" disabled>暂停</el-button>
                </span>
              </el-tooltip>
              <template v-else>
                <el-button v-if="isPaused(row.state)" size="small" @click="resume(row)">恢复</el-button>
                <el-button v-else size="small" @click="pause(row)">暂停</el-button>
              </template>
            </template>
            <el-dropdown trigger="click" @command="cmd => onRowCommand(cmd, row)">
              <el-button size="small">
                更多<el-icon><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item
                    v-if="row.dispatch?.phase_status === 'failed'"
                    command="retry_dispatch"
                    style="color: #409eff"
                  >重试此阶段</el-dropdown-item>
                  <el-dropdown-item
                    v-if="canEditRow(row)"
                    command="edit_dispatch"
                    style="color: #409eff"
                  >编辑识别/目标</el-dropdown-item>
                  <el-dropdown-item
                    v-if="canRedispatchRow(row)"
                    command="redispatch"
                    style="color: #409eff"
                  >重新入库</el-dropdown-item>
                  <!-- pipeline 后端正在跑（copying / organizing）时禁用 qB 类操作，
                       不阻断也不报错，但让用户知道现在按了没意义 -->
                  <el-dropdown-item command="force_start" :disabled="isPipelineBusy(row)">强制启动</el-dropdown-item>
                  <el-dropdown-item command="recheck" :disabled="isPipelineBusy(row)">重新校验</el-dropdown-item>
                  <el-dropdown-item command="reannounce" :disabled="isPipelineBusy(row)">重新汇报</el-dropdown-item>
                  <el-dropdown-item divided command="delete">删除任务</el-dropdown-item>
                  <el-dropdown-item command="delete_files" style="color: #f56c6c">删除（含文件）</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!loading && !qbitTorrents.length" description="qBittorrent 暂无任务（也可能是连接失败，请检查配置）" />
    </el-card>
    </div>

    <!-- 人工审核 / 编辑识别 modal：复用同一界面
         - phase_status='needs_review'（adopt 低置信认领）→ 审核入流水线
         - 其它非终态行 → 仅编辑字段（不强制 phase 流转） -->
    <el-dialog
      v-model="reviewVisible"
      :title="reviewIsConfirm ? '人工审核：自动认领的低置信种子' : '编辑识别 / 目标'"
      width="640"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
    >
      <div v-if="reviewTarget" class="review-modal">
        <!-- 顶部：种子基本信息（只读） -->
        <div class="review-meta">
          <div class="meta-line">
            <span class="meta-label">种子名</span>
            <span class="meta-name" :title="reviewTarget.name">{{ reviewTarget.name }}</span>
          </div>
          <div class="meta-line">
            <span class="meta-label">大小</span>
            <span>{{ formatSize(reviewTarget.size) }}</span>
            <span class="meta-label" style="margin-left: 16px">qB 状态</span>
            <el-tag size="small" :type="stateType(reviewTarget.state)">{{ stateLabel(reviewTarget.state) || '-' }}</el-tag>
          </div>
          <div class="meta-line review-reason" v-if="reviewTarget.dispatch?.status_message">
            <el-icon><Warning /></el-icon>
            {{ reviewTarget.dispatch.status_message }}
          </div>

          <!-- 重复检测结果（analyzer 写入 phase_timings.duplicates）-->
          <div v-if="reviewTarget.dispatch?.duplicates && reviewTarget.dispatch.duplicates.type !== 'none'"
               class="meta-line review-dup">
            <template v-if="reviewTarget.dispatch.duplicates.type === 'movie'">
              <el-tag size="small" type="warning">完整重复</el-tag>
              <span>电影已在媒体库存在 ·
                <span class="muted">{{ (reviewTarget.dispatch.duplicates.existing || []).map(e => e.name || e.path).join(' / ') }}</span>
              </span>
            </template>
            <template v-else-if="reviewTarget.dispatch.duplicates.type === 'tv'">
              <el-tag size="small" type="warning">全集已存在</el-tag>
              <span>{{ (reviewTarget.dispatch.duplicates.existing || []).length }} 集都已在库</span>
            </template>
            <template v-else-if="reviewTarget.dispatch.duplicates.type === 'partial'">
              <el-tag size="small" type="success">部分重复</el-tag>
              <span>
                <strong>{{ (reviewTarget.dispatch.duplicates.new || []).length }}</strong> 集新增
                · <span class="muted">{{ (reviewTarget.dispatch.duplicates.existing || []).length }} 集已 skip</span>
              </span>
            </template>
            <template v-else-if="reviewTarget.dispatch.duplicates.type === 'adult'">
              <el-tag size="small" type="warning">番号已存在</el-tag>
              <span>
                <strong>{{ (reviewTarget.dispatch.duplicates.existing || [])[0]?.code }}</strong>
                · <span class="muted">{{ (reviewTarget.dispatch.duplicates.existing || [])[0]?.name }}</span>
              </span>
            </template>
          </div>
        </div>

        <el-divider style="margin: 12px 0" />

        <!-- 表单：可改 media_type，目标 library/path 由后端按规则自动重算（除非用户进高级模式） -->
        <el-form :model="reviewForm" label-width="100px" size="default">
          <el-form-item label="媒体类型">
            <el-radio-group v-model="reviewForm.media_type" @change="onReviewTypeChange">
              <el-radio value="movie">电影</el-radio>
              <el-radio value="tv">剧集</el-radio>
              <el-radio value="anime">动漫</el-radio>
              <el-radio value="adult">成人</el-radio>
            </el-radio-group>
          </el-form-item>

          <el-form-item label="标题">
            <el-input v-model="reviewForm.title" placeholder="自动从种子名识别" clearable />
          </el-form-item>

          <el-form-item label="年份" v-if="reviewForm.media_type === 'movie'">
            <el-input-number v-model="reviewForm.year" :min="1900" :max="2100" :step="1" />
          </el-form-item>

          <el-form-item label="剧名" v-if="reviewForm.media_type === 'tv' || reviewForm.media_type === 'anime'">
            <el-input v-model="reviewForm.series_name" placeholder="剧集 / 番剧名" clearable />
          </el-form-item>

          <el-form-item label="目标解析">
            <el-radio-group v-model="reviewForm._target_mode">
              <el-radio value="auto">按规则自动重算（推荐）</el-radio>
              <el-radio value="manual">手动指定</el-radio>
            </el-radio-group>
          </el-form-item>

          <template v-if="reviewForm._target_mode === 'auto'">
            <el-form-item label="目标库">
              <span class="auto-target">
                <strong v-if="reviewTarget.dispatch?.target_library_name">{{ reviewTarget.dispatch.target_library_name }}</strong>
                <span v-else class="muted">⚠ 该 media_type 未配置库</span>
                <span class="muted" style="margin-left: 8px">（保存后按 media_type 规则自动重算）</span>
              </span>
            </el-form-item>
            <el-form-item label="目标路径">
              <span class="auto-target path-text" v-if="reviewTarget.dispatch?.target_path">{{ reviewTarget.dispatch.target_path }}</span>
              <span v-else class="muted">—</span>
            </el-form-item>
          </template>

          <template v-else>
            <el-form-item label="目标库 ID">
              <el-input v-model="reviewForm.target_library_id" placeholder="Jellyfin 库 ID（在 设置 → 入库流水线 里找）" clearable />
            </el-form-item>
            <el-form-item label="目标路径">
              <el-input v-model="reviewForm.target_path" placeholder="完整路径，例如 /library/videos/movie/Movie (2024)" clearable />
            </el-form-item>
          </template>

          <el-form-item v-if="reviewAfterCopy">
            <span class="muted" style="font-size: 12px; color: var(--jt-warning)">
              ⚠ 文件已经在复制了，改这里不会自动搬已经下载好的文件，需要自己处理
            </span>
          </el-form-item>
        </el-form>
      </div>

      <template #footer>
        <div class="review-footer">
          <div class="footer-left">
            <!-- 重新识别：跑一次完整识别链拿建议回填表单（不写库；用户点保存才落盘） -->
            <el-button
              :icon="Refresh"
              :loading="reviewReidentifying"
              :disabled="!reviewTarget"
              title="重新自动识别一次，结果填到表单里"
              @click="reidentifyInReview"
            >重新识别</el-button>
            <!-- 拒绝 3 按钮：仅 needs_review 流程显示 -->
            <template v-if="reviewIsConfirm">
              <el-button title="保留种子和文件" @click="onReviewDismissCmd('dismiss')">
                拒绝
              </el-button>
              <el-button title="不保留种子但保留文件" @click="onReviewDismissCmd('remove_torrent')">
                移除种子
              </el-button>
              <el-button type="danger" plain title="种子和文件都不保留" @click="onReviewDismissCmd('remove_files')">
                移除文件
              </el-button>
            </template>
          </div>
          <div class="footer-right">
            <el-button @click="reviewVisible = false">取消</el-button>
            <el-button type="primary" :loading="reviewSaving" @click="submitReview">
              {{ reviewIsConfirm ? '确认入流水线' : '保存' }}
            </el-button>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- copy-phase 冲突审核 modal：目标位置已被另一种子占用，用户决策 -->
    <el-dialog
      v-model="copyConflictVisible"
      title="目标位置已有同名文件，请选择处理方式"
      width="640"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
    >
      <div v-if="copyConflictInfo" class="review-modal">
        <div class="review-section">
          <div class="meta-line">
            <span class="meta-label">本种子</span>
            <span class="meta-name">{{ copyConflictInfo.my_release_name || copyConflictInfo.title || '-' }}</span>
          </div>
          <div class="meta-line">
            <span class="meta-label">目标位置</span>
            <span class="path-text">{{ copyConflictInfo.dst }}</span>
          </div>
          <div class="meta-line">
            <span class="meta-label">已被</span>
            <span class="meta-name">{{ copyConflictInfo.existing_release_name }}</span>
            <span class="muted"> · 种子 {{ (copyConflictInfo.existing_hash || '').slice(0, 16) }}.. · 当前 {{ copyConflictInfo.existing_phase }}</span>
          </div>
          <div class="meta-line" v-if="copyConflictInfo.reason">
            <span class="meta-label">原因</span>
            <span class="muted">{{ copyConflictInfo.reason }}</span>
          </div>
        </div>
        <div class="footer-hint muted">
          覆盖：旧文件挪到回收站，再写入新的；跳过：保留旧版本，丢掉新的
        </div>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="copyConflictVisible = false">取消</el-button>
          <el-button :loading="copyConflictBusy" @click="resolveCopyConflict('skip')">跳过（保留旧）</el-button>
          <el-button type="primary" :loading="copyConflictBusy" @click="resolveCopyConflict('replace')">覆盖（用新的）</el-button>
        </div>
      </template>
    </el-dialog>

    <!-- RSS tab -->
    <div v-show="activeTab === 'rss'">
      <el-card shadow="never" class="rss-card">
        <div class="rss-header">
          <el-button-group>
            <el-button
              size="small"
              :type="rssView === 'unread' ? 'primary' : ''"
              @click="rssView = 'unread'"
            >未读聚合（{{ unreadArticles.length }}）</el-button>
            <el-button
              size="small"
              :type="rssView === 'feeds' ? 'primary' : ''"
              @click="rssView = 'feeds'"
            >按 Feed 浏览</el-button>
            <el-button
              size="small"
              :type="rssView === 'rules' ? 'primary' : ''"
              @click="rssView = 'rules'"
            >自动规则（{{ rssRules.length }}）</el-button>
          </el-button-group>

          <div class="rss-header-right">
            <el-button size="small" type="primary" :icon="Plus" @click="showAddRssDialog = true">
              添加订阅
            </el-button>
            <el-button
              size="small"
              :icon="Refresh"
              @click="rssRefreshAllFeeds"
              :loading="rssRefreshingAll"
            >刷新</el-button>
          </div>
        </div>

        <!-- qB RSS 全局设置（启用 / 间隔 / 上限） -->
        <div class="rss-settings-bar">
          <el-tooltip content="qB 是否抓取 RSS feeds（关闭后所有订阅都停）" placement="top">
            <span class="setting-item">
              <el-switch
                v-model="rssQbSettings.rss_processing_enabled"
                size="small"
                @change="onRssSettingChange('rss_processing_enabled', $event)"
              />
              <label>启用 RSS 订阅</label>
            </span>
          </el-tooltip>
          <el-tooltip content="qB 是否按规则自动下载匹配到的 RSS 条目" placement="top">
            <span class="setting-item">
              <el-switch
                v-model="rssQbSettings.rss_auto_downloading_enabled"
                size="small"
                @change="onRssSettingChange('rss_auto_downloading_enabled', $event)"
              />
              <label>自动下载</label>
            </span>
          </el-tooltip>
          <span class="setting-item">
            <label>刷新间隔 (分)</label>
            <el-input-number
              v-model="rssQbSettings.rss_refresh_interval"
              size="small" :min="1" :max="1440" :step="5"
              controls-position="right"
              @change="onRssSettingChange('rss_refresh_interval', $event)"
              style="width: 110px"
            />
          </span>
          <span class="setting-item">
            <label>每 feed 保留</label>
            <el-input-number
              v-model="rssQbSettings.rss_max_articles_per_feed"
              size="small" :min="10" :max="2000" :step="50"
              controls-position="right"
              @change="onRssSettingChange('rss_max_articles_per_feed', $event)"
              style="width: 120px"
            />
            <span class="muted" style="font-size: 11px">条</span>
          </span>
        </div>

        <!-- 未读聚合：跨所有 feed 的未读条目，按时间倒序 -->
        <div v-show="rssView === 'unread'" style="margin-top: 12px">
          <el-table
            v-if="unreadArticles.length"
            :data="unreadArticles"
            size="small"
            max-height="600"
          >
            <el-table-column prop="_feedName" label="Feed" width="160" show-overflow-tooltip />
            <el-table-column prop="title" label="标题" min-width="380" show-overflow-tooltip />
            <el-table-column prop="date" label="发布时间" width="160" />
            <el-table-column label="操作" width="170" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small"
                  type="primary"
                  :icon="Download"
                  :disabled="!row.torrentURL && !row.link"
                  @click="rssDownload(row)"
                >下载</el-button>
                <el-button size="small" @click="rssMarkOne(row)">已读</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="没有未读条目" :image-size="60" />
        </div>

        <!-- 按 Feed 浏览 -->
        <div v-show="rssView === 'feeds'" style="margin-top: 12px">
          <el-collapse v-if="rssFeedKeys.length">
            <el-collapse-item
              v-for="key in rssFeedKeys"
              :key="key"
              :name="key"
            >
              <template #title>
                <div class="feed-title-row">
                  <span class="feed-name">{{ key }}</span>
                  <span class="feed-meta">
                    {{ rssFeeds[key]?.articles?.length || 0 }} 条
                    <span v-if="feedUnreadCount(key)" class="unread-badge">
                      {{ feedUnreadCount(key) }} 未读
                    </span>
                  </span>
                </div>
              </template>
              <div style="margin-bottom: 8px; display: flex; gap: 8px">
                <el-button
                  size="small"
                  :icon="Refresh"
                  :loading="rssRefreshingMap[key]"
                  @click="rssRefreshFeed(key)"
                >刷新</el-button>
                <el-button size="small" @click="rssMarkFeedAllRead(key)">全部标已读</el-button>
                <el-button size="small" type="danger" plain @click="rssRemoveFeed(key)">删除订阅</el-button>
              </div>
              <el-table
                v-if="rssFeeds[key]?.articles?.length"
                :data="rssFeeds[key].articles"
                size="small"
                :row-class-name="rssRowClass"
              >
                <el-table-column prop="title" label="标题" min-width="380" show-overflow-tooltip />
                <el-table-column label="发布时间" width="200">
                <template #default="{ row }">
                  <span :title="row.date || ''">{{ formatRssDate(row.date) }}</span>
                </template>
              </el-table-column>
                <el-table-column label="操作" width="170" fixed="right">
                  <template #default="{ row }">
                    <el-button
                      size="small"
                      type="primary"
                      :icon="Download"
                      :disabled="!row.torrentURL && !row.link"
                      @click="rssDownload(row, key)"
                    >下载</el-button>
                    <el-button v-if="!row.isRead" size="small" @click="rssMarkOne(row, key)">已读</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-collapse-item>
          </el-collapse>
          <el-empty v-else description="qB 当前未订阅任何 RSS feeds" :image-size="60" />
        </div>

        <!-- 自动规则 -->
        <div v-show="rssView === 'rules'" style="margin-top: 12px">
          <div style="margin-bottom: 8px; display: flex; gap: 8px">
            <el-button size="small" type="primary" :icon="Plus" @click="openRuleEditor()">
              新建规则
            </el-button>
            <span class="muted" style="font-size: 12px; align-self: center">
              规则按"必含 / 必不含"匹配 feed 条目，命中后自动加种到 qB
            </span>
          </div>
          <el-table v-if="rssRules.length" :data="rssRules" size="small" max-height="600">
            <el-table-column prop="name" label="规则名" min-width="160" />
            <el-table-column label="启用" width="74">
              <template #default="{ row }">
                <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
                  {{ row.enabled ? '启用' : '禁用' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="mustContain" label="必含" min-width="160" show-overflow-tooltip />
            <el-table-column prop="mustNotContain" label="必不含" min-width="120" show-overflow-tooltip />
            <el-table-column prop="savePath" label="保存路径" min-width="160" show-overflow-tooltip />
            <el-table-column prop="assignedCategory" label="分类" width="100" />
            <el-table-column label="操作" width="160" fixed="right">
              <template #default="{ row }">
                <el-button size="small" @click="openRuleEditor(row)">编辑</el-button>
                <el-button size="small" type="danger" plain @click="deleteRule(row.name)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="还没有自定义规则。可以从模板创建，或点右上角「新建规则」" :image-size="60" />
        </div>
      </el-card>
    </div>

    <!-- 添加 RSS 订阅 dialog -->
    <el-dialog v-model="showAddRssDialog" title="添加 RSS 订阅" width="520">
      <el-form label-width="80px">
        <el-form-item label="Feed URL" required>
          <el-input
            v-model="newRssForm.url"
            placeholder="https://... 或 Jackett Torznab feed URL"
          />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input
            v-model="newRssForm.path"
            placeholder="可选，留空 qB 自动取 feed title"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showAddRssDialog = false">取消</el-button>
        <el-button
          type="primary"
          :loading="rssAdding"
          :disabled="!newRssForm.url.trim()"
          @click="submitAddRss"
        >添加</el-button>
      </template>
    </el-dialog>

    <!-- RSS 规则编辑 dialog（新建 / 编辑共用）-->
    <!-- align-center：垂直居中（默认是 top:15vh 顶部对齐）；
         class rule-edit-dialog：让 body 内部滚动，不超出视口（避免 dialog 整个被推出去出现外层滚动条） -->
    <el-dialog
      v-model="showRuleDialog"
      :title="ruleForm._isNew ? '新建规则' : `编辑规则：${ruleForm._origName}`"
      width="640"
      align-center
      class="rule-edit-dialog"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
    >
      <!-- AI 辅助横幅：用自然语言描述意图 → 自动生成必含 / 必不含 / 是否正则 -->
      <div class="llm-helper">
        <div class="llm-helper-row">
          <el-input
            v-model="llmDescription"
            placeholder="用自然语言描述：例如 整季蓝光 1080p 简中，排单集和重制"
            size="small"
            clearable
            @keyup.enter="onLlmGenerate"
          >
            <template #prefix>
              <el-icon><MagicStick /></el-icon>
            </template>
          </el-input>
          <el-button
            size="small"
            type="primary"
            :loading="llmLoading"
            :disabled="!llmDescription.trim()"
            @click="onLlmGenerate"
          >AI 生成</el-button>
        </div>
        <div v-if="llmExplanation" class="llm-helper-explain">
          <el-icon><InfoFilled /></el-icon>
          <span>{{ llmExplanation }}</span>
        </div>
      </div>

      <el-form label-width="110px" size="small">
        <el-form-item label="规则名" required>
          <div class="name-row">
            <el-input v-model="ruleForm.name" placeholder="如 Anime-1080p" style="flex: 1" />
            <span class="name-row-switch">
              <el-switch v-model="ruleForm.enabled" />
              <label>启用</label>
            </span>
          </div>
        </el-form-item>
        <el-form-item label="必含关键词">
          <el-input v-model="ruleForm.mustContain" placeholder="如 1080p BluRay" />
          <div class="form-hint hint-tight-global">
            关键词(默认)多词空格 = AND；勾"使用正则"则整段当正则。例：
            <ul class="hint-list-global">
              <li><code class="re-code-global">1080p WEB-DL 简体</code> — 三个词都要在标题里</li>
              <li><code class="re-code-global">S\d{2}E\d{2}.*1080p.*WEB</code> — 抓 SxxExx 单集 1080p WEB</li>
              <li><code class="re-code-global">\.S\d{2}\..*BluRay.*1080p</code> — 抓 .S01. 整季蓝光</li>
            </ul>
          </div>
        </el-form-item>
        <el-form-item label="必不含关键词">
          <el-input v-model="ruleForm.mustNotContain" placeholder="如 720p HDTV CAM" />
          <div class="form-hint hint-tight-global">
            命中即排除（同样跟"使用正则"开关联动）。例：
            <ul class="hint-list-global">
              <li><code class="re-code-global">720p HDTV CAM TS</code> — 排除低画质 / 录制版 / 枪版</li>
              <li><code class="re-code-global">(720p|HDTV|REPACK|PROPER)</code> — 同上 + 重制版</li>
              <li>反向用：必含 <code class="re-code-global">\.S\d{2}\.</code> + 必不含 <code class="re-code-global">S\d{2}E\d{2}</code> — 抓整季排单集</li>
            </ul>
          </div>
        </el-form-item>
        <el-form-item label="使用正则">
          <el-switch v-model="ruleForm.useRegex" />
          <span class="form-hint" style="margin-left: 8px">必含/必不含都改用正则解析</span>
        </el-form-item>
        <el-form-item label="集数过滤">
          <el-input v-model="ruleForm.episodeFilter" placeholder="如 1x01-99 (可空)" />
          <div class="form-hint hint-tight-global">
            qB 自己的集数语法(独立于关键词，空 = 不限)。例：
            <ul class="hint-list-global">
              <li><code class="re-code-global">1x01-99</code> — S01 的 01 到 99 集</li>
              <li><code class="re-code-global">1x01-99;2x01-50</code> — 多季用分号串联</li>
              <li><code class="re-code-global">1x;</code> — S01 任意集</li>
              <li><code class="re-code-global">1x05-</code> — S01 从 05 集开始全要</li>
              <li><code class="re-code-global">2017x;</code> — 部分剧用年份当季</li>
            </ul>
          </div>
        </el-form-item>
        <el-form-item label="智能去重">
          <el-switch v-model="ruleForm.smartFilter" />
          <span class="form-hint" style="margin-left: 8px">避免下载同集不同发布版本（PROPER/REPACK 除外）</span>
        </el-form-item>
        <el-form-item label="作用 feeds">
          <el-select
            v-model="ruleForm.affectedFeeds"
            multiple
            collapse-tags
            collapse-tags-tooltip
            placeholder="留空 = 对所有订阅生效"
            style="width: 100%"
          >
            <el-option
              v-for="key in rssFeedKeys"
              :key="key"
              :label="key"
              :value="rssFeeds[key]?.url || key"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="保存路径">
          <el-input v-model="ruleForm.savePath" placeholder="留空 = qB 默认下载路径" />
        </el-form-item>
        <el-form-item label="qB 分类">
          <el-input v-model="ruleForm.assignedCategory" placeholder="可选" />
        </el-form-item>
        <el-form-item label="冷却天数">
          <el-input-number v-model="ruleForm.ignoreDays" :min="0" :max="365" controls-position="right" />
          <div class="form-hint">同名条目在这段时间内不再下载（避免重复种子）</div>
        </el-form-item>
        <el-form-item label="加种后暂停">
          <el-radio-group v-model="ruleForm.addPaused">
            <el-radio :label="null">默认</el-radio>
            <el-radio :label="false">立即开始</el-radio>
            <el-radio :label="true">暂停</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showRuleDialog = false">取消</el-button>
        <el-button
          type="primary"
          :loading="ruleSaving"
          :disabled="!ruleForm.name.trim()"
          @click="submitRule"
        >保存</el-button>
      </template>
    </el-dialog>

    <!-- 添加种子对话框 -->
    <AddTorrentDialog
      v-model="showAddDialog"
      @submitted="onTorrentAdded"
    />
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import { Refresh, Link, ArrowDown, Brush, Setting, Moon, Plus, Warning, Close, Download, MagicStick, InfoFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { discoverApi, configApi, dispatchApi } from '@/api'
import AddTorrentDialog from '@/components/AddTorrentDialog.vue'
import dayjs from 'dayjs'

// RSS article 发布时间格式化：今天显示 HH:mm，今年显示 MM-DD HH:mm，更早 YYYY-MM-DD
const formatRssDate = (s) => {
  if (!s) return '—'
  const d = dayjs(s)
  if (!d.isValid()) return s
  const now = dayjs()
  if (d.isSame(now, 'day')) return '今天 ' + d.format('HH:mm')
  if (d.isSame(now.subtract(1, 'day'), 'day')) return '昨天 ' + d.format('HH:mm')
  if (d.isSame(now, 'year')) return d.format('MM-DD HH:mm')
  return d.format('YYYY-MM-DD HH:mm')
}

const showAddDialog = ref(false)
// 手动添加的 torrent_hash 集合：等其在主表 phase_status 变成 needs_review 时自动弹审核 modal
// （Jackett 推送的不进这个 set，待审核就静默放主表上等用户主动点）
const pendingManualHashes = ref(new Set())
const onTorrentAdded = (hash) => {
  if (hash) pendingManualHashes.value.add(hash)
  load()
}

// ---- Tabs ----
const activeTab = ref('torrents')

// ---- RSS ----
const rssRules = ref([])
const rssFeeds = ref({})
const rssLoading = ref(false)

const rssFeedKeys = computed(() => {
  // qB rss/items 返回的是嵌套结构，第一层是 feed name → {url, articles, ...}
  return Object.keys(rssFeeds.value).filter(k => k && k !== 'uid')
})

const loadRss = async () => {
  rssLoading.value = true
  try {
    const [r, f] = await Promise.allSettled([
      dispatchApi.rssRules(),
      dispatchApi.rssFeeds(),
    ])
    if (r.status === 'fulfilled') {
      const obj = r.value.data || {}
      rssRules.value = Object.keys(obj).map(name => ({ name, ...obj[name] }))
    }
    if (f.status === 'fulfilled') {
      rssFeeds.value = f.value.data || {}
    }
  } catch (e) {
    console.error('RSS 加载失败', e)
  } finally {
    rssLoading.value = false
  }
}

// ===== RSS 视图相关 =====
const rssView = ref('unread')          // 'unread' / 'feeds' / 'rules'
const showAddRssDialog = ref(false)
const newRssForm = reactive({ url: '', path: '' })
const rssAdding = ref(false)
const rssRefreshingAll = ref(false)
const rssRefreshingMap = reactive({})  // {feedKey: bool}

// qB RSS 全局设置（双向绑定，change 时即时写回 qB）
const rssQbSettings = reactive({
  rss_processing_enabled: true,
  rss_auto_downloading_enabled: false,
  rss_refresh_interval: 30,
  rss_max_articles_per_feed: 50,
})

const loadRssSettings = async () => {
  try {
    const r = await dispatchApi.rssSettingsGet()
    Object.assign(rssQbSettings, r.data || {})
  } catch (e) {
    console.error('RSS 设置读取失败', e)
  }
}

const onRssSettingChange = async (field, value) => {
  try {
    await dispatchApi.rssSettingsSet({ [field]: value })
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
    // 失败回滚
    loadRssSettings()
  }
}

// ===== 规则编辑 =====
const showRuleDialog = ref(false)
const ruleSaving = ref(false)
const ruleForm = reactive({
  _isNew: true,
  _origName: '',
  name: '',
  enabled: true,
  mustContain: '',
  mustNotContain: '',
  useRegex: false,
  episodeFilter: '',
  smartFilter: false,
  affectedFeeds: [],
  savePath: '',
  assignedCategory: '',
  ignoreDays: 0,
  addPaused: null,
})

const resetRuleForm = (existing = null) => {
  Object.assign(ruleForm, {
    _isNew: !existing,
    _origName: existing?.name || '',
    name: existing?.name || '',
    enabled: existing?.enabled ?? true,
    mustContain: existing?.mustContain || '',
    mustNotContain: existing?.mustNotContain || '',
    useRegex: existing?.useRegex ?? false,
    episodeFilter: existing?.episodeFilter || '',
    smartFilter: existing?.smartFilter ?? false,
    affectedFeeds: Array.isArray(existing?.affectedFeeds) ? [...existing.affectedFeeds] : [],
    savePath: existing?.savePath || '',
    assignedCategory: existing?.assignedCategory || '',
    ignoreDays: existing?.ignoreDays ?? 0,
    addPaused: existing?.addPaused ?? null,
  })
}

const openRuleEditor = (existing = null) => {
  resetRuleForm(existing)
  // 重置 LLM 辅助状态（每次打开都是干净的）
  llmDescription.value = ''
  llmExplanation.value = ''
  showRuleDialog.value = true
}

// ===== LLM 辅助生成正则 =====
const llmDescription = ref('')
const llmLoading = ref(false)
const llmExplanation = ref('')

const onLlmGenerate = async () => {
  const desc = llmDescription.value.trim()
  if (!desc) return
  llmLoading.value = true
  try {
    // 把已订阅 feed 里前几条标题作为样本（让 LLM 看着真实的命名风格写规则）
    const samples = []
    for (const k of Object.keys(rssFeeds.value || {})) {
      const arts = rssFeeds.value[k]?.articles || []
      for (const a of arts.slice(0, 4)) {
        if (a.title) samples.push(a.title)
        if (samples.length >= 12) break
      }
      if (samples.length >= 12) break
    }
    const r = await dispatchApi.rssLlmRegex(desc, samples.length ? samples : null)
    const data = r.data || {}
    ruleForm.mustContain = data.must_contain || ''
    ruleForm.mustNotContain = data.must_not_contain || ''
    ruleForm.useRegex = !!data.use_regex
    llmExplanation.value = data.explanation || ''
    ElMessage.success('已生成规则，请检查后保存')
  } catch (e) {
    // axios response interceptor 已经 toast 过 error.response.data.detail，这里不再重复弹
    console.error('LLM 规则生成失败:', e)
  } finally {
    llmLoading.value = false
  }
}

const submitRule = async () => {
  ruleSaving.value = true
  try {
    const newName = ruleForm.name.trim()
    // 重命名场景：先 rename 再 set
    if (!ruleForm._isNew && ruleForm._origName && ruleForm._origName !== newName) {
      await dispatchApi.rssRuleRename(ruleForm._origName, newName)
    }
    const def = {
      enabled: ruleForm.enabled,
      mustContain: ruleForm.mustContain,
      mustNotContain: ruleForm.mustNotContain,
      useRegex: ruleForm.useRegex,
      episodeFilter: ruleForm.episodeFilter,
      smartFilter: ruleForm.smartFilter,
      affectedFeeds: ruleForm.affectedFeeds || [],
      savePath: ruleForm.savePath,
      assignedCategory: ruleForm.assignedCategory,
      ignoreDays: ruleForm.ignoreDays,
      addPaused: ruleForm.addPaused,
    }
    await dispatchApi.rssRuleSet(newName, def)
    ElMessage.success(ruleForm._isNew ? '规则已创建' : '规则已保存')
    showRuleDialog.value = false
    await loadRss()
  } catch (e) {
    ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
  } finally {
    ruleSaving.value = false
  }
}

const deleteRule = async (name) => {
  try {
    await ElMessageBox.confirm(`删除规则「${name}」？`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await dispatchApi.rssRuleDelete(name)
    ElMessage.success('已删除')
    await loadRss()
  } catch (e) {
    ElMessage.error('删除失败：' + (e.response?.data?.detail || e.message))
  }
}

// 刷新所有 feeds：让 qB 抓上游 → 等 qB 拉完 → 自动同步本地缓存
const rssRefreshAllFeeds = async () => {
  rssRefreshingAll.value = true
  try {
    const r = await dispatchApi.rssRefreshAll()
    // qB 抓上游是异步的，等 ~2.5s 让它有时间拉到数据再同步
    await new Promise(resolve => setTimeout(resolve, 2500))
    await loadRss()
    ElMessage.success(`已刷新 ${r.data?.success || 0}/${r.data?.total || 0} 个 feed`)
  } catch (e) {
    ElMessage.error('刷新失败：' + (e.response?.data?.detail || e.message))
  } finally {
    rssRefreshingAll.value = false
  }
}

// 刷新单个 feed：同上，触发 + 等 + 同步
const rssRefreshFeed = async (key) => {
  rssRefreshingMap[key] = true
  try {
    await dispatchApi.rssRefresh(key)
    await new Promise(resolve => setTimeout(resolve, 2500))
    await loadRss()
    ElMessage.success(`已刷新「${key}」`)
  } catch (e) {
    ElMessage.error('刷新失败：' + (e.response?.data?.detail || e.message))
  } finally {
    rssRefreshingMap[key] = false
  }
}

// 找到 article 所属 feed key（路径化展示用）
const findFeedKeyForArticle = (article) => {
  for (const k of rssFeedKeys.value) {
    const arts = rssFeeds.value[k]?.articles || []
    if (arts.some(a => a === article || (a.id && article.id && a.id === article.id))) {
      return k
    }
  }
  return null
}

// 未读聚合：跨所有 feed 的 isRead=false 条目，按 date 倒序
const unreadArticles = computed(() => {
  const list = []
  for (const k of rssFeedKeys.value) {
    const arts = rssFeeds.value[k]?.articles || []
    for (const a of arts) {
      if (!a.isRead) {
        list.push({ ...a, _feedName: k, _feedPath: k })
      }
    }
  }
  // 按 date 字符串倒序（qB 的 date 是 ISO 形式）
  list.sort((a, b) => (b.date || '').localeCompare(a.date || ''))
  return list
})

const feedUnreadCount = (key) => {
  const arts = rssFeeds.value[key]?.articles || []
  return arts.filter(a => !a.isRead).length
}

// 已读条目灰一点
const rssRowClass = ({ row }) => row.isRead ? 'rss-row-read' : ''

// 添加 RSS 订阅
const submitAddRss = async () => {
  rssAdding.value = true
  try {
    await dispatchApi.rssAddFeed(newRssForm.url.trim(), newRssForm.path.trim() || null)
    ElMessage.success('已添加')
    newRssForm.url = ''
    newRssForm.path = ''
    showAddRssDialog.value = false
    await loadRss()
  } catch (e) {
    ElMessage.error('添加失败：' + (e.response?.data?.detail || e.message))
  } finally {
    rssAdding.value = false
  }
}

// 删除 feed 订阅
const rssRemoveFeed = async (key) => {
  try {
    await ElMessageBox.confirm(`删除订阅「${key}」？`, '确认', { type: 'warning' })
  } catch { return }
  try {
    await dispatchApi.rssRemove(key)
    ElMessage.success('已删除')
    await loadRss()
  } catch (e) {
    ElMessage.error('删除失败：' + (e.response?.data?.detail || e.message))
  }
}

// 标单条已读
const rssMarkOne = async (article, feedKey) => {
  const key = feedKey || article._feedPath || findFeedKeyForArticle(article)
  if (!key) return
  try {
    await dispatchApi.rssMarkRead(key, article.id || undefined)
    // 本地立刻反映（避免等下一次 loadRss）
    article.isRead = true
  } catch (e) {
    ElMessage.error('标已读失败：' + (e.response?.data?.detail || e.message))
  }
}

// 整 feed 标已读
const rssMarkFeedAllRead = async (key) => {
  try {
    await dispatchApi.rssMarkRead(key, null)
    const arts = rssFeeds.value[key]?.articles || []
    arts.forEach(a => { a.isRead = true })
    ElMessage.success(`${key} 全部标已读`)
  } catch (e) {
    ElMessage.error('操作失败：' + (e.response?.data?.detail || e.message))
  }
}

// RSS article 下载：下载成功后自动标已读
const rssDownload = async (article, feedKey) => {
  const url = article.torrentURL || article.link
  if (!url) {
    ElMessage.warning('该条目没有可下载的种子链接')
    return
  }
  const isMagnet = url.startsWith('magnet:')
  try {
    await discoverApi.push({
      title: article.title,
      magnet: isMagnet ? url : undefined,
      torrent_url: isMagnet ? undefined : url,
      source: 'rss',
    })
    ElMessage.success(`${article.title.slice(0, 60)}：已加入分析队列`)
    // 顺手标已读
    rssMarkOne(article, feedKey).catch(() => {})
  } catch (e) {
    ElMessage.error('推送失败：' + (e.response?.data?.detail || e.message))
  }
}

watch(activeTab, (v) => {
  if (v === 'rss') {
    loadRssSettings()
    if (!Object.keys(rssFeeds.value).length) {
      loadRss()
    }
  }
})

// ---- 人工审核（待审核行的 modal 编辑）----
// 不调 listNeedsReview endpoint —— 主表的 dispatch 字段已经把所需元数据全带过来了。
// 后端 scheduler 默默扫描孤儿种子，前端只在主轮询里被动看到结果。
const reviewVisible = ref(false)
const reviewTarget = ref(null)             // 当前正在审核的 torrent row（来自 qbitTorrents 的某条）
const reviewSaving = ref(false)
const reviewReidentifying = ref(false)     // "重新识别"按钮的 loading 态
const reviewForm = reactive({
  media_type: 'movie',
  title: '',
  year: null,
  series_name: '',
  // 目标解析模式：'auto'（按规则）/ 'manual'（用户手填）
  _target_mode: 'auto',
  target_library_id: '',
  target_path: '',
})

// copy-phase 冲突弹窗状态（跟 reviewVisible 互斥；同样从"人工审核"按钮触发）
const copyConflictVisible = ref(false)
const copyConflictInfo = ref(null)   // 由 /copy-conflict/{hash} 拉来的上下文
const copyConflictHash = ref('')
const copyConflictBusy = ref(false)

// 打开 modal：按 phase 路由到 analyzing-审核 / copying-冲突 两套弹窗
const openReview = async (row) => {
  const d = row.dispatch || {}
  // copy-phase needs_review = 跨种子目标冲突 → 拉冲突上下文 + 弹专用 dialog
  if (d.phase === 'copying' && d.phase_status === 'needs_review') {
    copyConflictHash.value = row.hash
    copyConflictBusy.value = true
    try {
      const r = await dispatchApi.getCopyConflict(row.hash)
      copyConflictInfo.value = { ...(r.data?.conflict || {}), title: r.data?.title }
      copyConflictVisible.value = true
    } catch (e) {
      ElMessage.error('加载冲突上下文失败：' + (e.response?.data?.detail || e.message))
    } finally {
      copyConflictBusy.value = false
    }
    return
  }
  // 默认走 analyzing 低置信审核 / 普通编辑（按 phase_status 在 submit 时分流）
  reviewTarget.value = row
  reviewForm.media_type = (d.media_type && d.media_type !== 'unknown') ? d.media_type : 'movie'
  reviewForm.title = d.title || ''
  reviewForm.year = d.year || null
  reviewForm.series_name = d.series_name || ''
  reviewForm._target_mode = 'auto'
  reviewForm.target_library_id = d.target_library_id || ''
  reviewForm.target_path = d.target_path || ''
  reviewVisible.value = true
}

// copy-phase 冲突决策：'replace'（覆盖）/ 'skip'（跳过）
const resolveCopyConflict = async (action) => {
  if (!copyConflictHash.value) return
  copyConflictBusy.value = true
  try {
    if (action === 'replace') {
      await dispatchApi.copyConflictReplace(copyConflictHash.value)
      ElMessage.success('已决策"覆盖"：旧文件入 trash，新种子重新复制')
    } else {
      await dispatchApi.copyConflictSkip(copyConflictHash.value)
      ElMessage.success('已决策"跳过"：保留旧版本')
    }
    copyConflictVisible.value = false
    load(true)
  } catch (e) {
    ElMessage.error('决策失败：' + (e.response?.data?.detail || e.message))
  } finally {
    copyConflictBusy.value = false
  }
}

// ---- 手动编辑识别 / 目标：复用 review modal 状态 ----
// 终态 phase 拒绝编辑（再编辑无意义）；编辑后已落盘文件不会迁移，仅修 DB
const TERMINAL_PHASES = new Set(['cleaned', 'dismissed', 'all_jobs_done'])
const PRE_COPY_PHASES = new Set(['analyzing', 'dispatch_queued', 'downloading', 'download_done'])

const canEditRow = (row) => {
  const phase = row?.dispatch?.phase
  if (!phase) return false   // 没 dispatch_map 行 → 不能改
  return !TERMINAL_PHASES.has(phase)
}

// 能否"重新入库"：终态任务（已完成 / 已清理 / 已拒绝），以及 duplicate_policy 跳过的
const canRedispatchRow = (row) => {
  const phase = row?.dispatch?.phase
  const status = row?.dispatch?.phase_status
  if (!phase) return false
  if (phase === 'all_jobs_done' || phase === 'cleaned' || phase === 'dismissed') return true
  if (phase === 'copying' && status === 'skipped') return true
  return false
}

// 后端正在跑的"忙阶段"：按 qB 类按钮没意义、暂停按钮也停不下来的状态
// 仅 copying（status=running）算真正阻塞型；organizing 现是 instant noop，列上以防未来扩展
const BUSY_PHASES = new Set(['copying', 'organizing'])
const isPipelineBusy = (row) => {
  const phase = row?.dispatch?.phase
  const status = row?.dispatch?.phase_status
  if (!BUSY_PHASES.has(phase)) return false
  // skipped / failed / succeeded 这些非 running 子状态不算真在忙
  return status === 'running' || status === 'needs_review'
}

const busyTooltip = (row) => {
  const phase = row?.dispatch?.phase
  if (phase === 'copying') return '正在复制文件到媒体库，请等待完成'
  if (phase === 'organizing') return '正在整理文件，请稍候'
  return '任务处理中，暂时无法操作'
}

// 当前 review modal 是否走"审核确认"流程（vs 普通编辑）：看 phase_status
const reviewIsConfirm = computed(
  () => reviewTarget.value?.dispatch?.phase_status === 'needs_review'
)
// 当前行是否已进入复制阶段（warning 用）
const reviewAfterCopy = computed(() => {
  const ph = reviewTarget.value?.dispatch?.phase
  return ph && !PRE_COPY_PHASES.has(ph)
})

const openEdit = (row) => {
  // 复用 openReview 的填表逻辑；走的是同一弹窗
  openReview(row)
}

// 主表数据更新后调一次：手动添加的种子若分析后落到 needs_review，自动弹审核 modal
// （review modal 已开启时不重复弹；多个 manual hash 一次只处理一个，剩下的下一轮 load 再处理）
const _autoOpenManualReview = () => {
  if (reviewVisible.value) return        // 已经在审核某条，等用户处理完再说
  if (!pendingManualHashes.value.size) return

  for (const t of qbitTorrents.value) {
    const h = (t.hash || '').toLowerCase()
    if (!pendingManualHashes.value.has(h)) continue
    const ps = t.dispatch?.phase_status
    const phase = t.dispatch?.phase
    // 还在分析中 → 等下一轮
    if (phase === 'analyzing') continue
    // 落到待审核 → 自动弹 modal + 从队列移除
    if (ps === 'needs_review') {
      openReview(t)
      pendingManualHashes.value.delete(h)
      return
    }
    // 已经被 analyzer 自动 dispatch 了 → 不需要弹，从队列移除
    pendingManualHashes.value.delete(h)
  }
}

// 改了 media_type → 给用户提示：目标库/路径将按规则重算（仅文案，不改 row）
const onReviewTypeChange = () => {
  // 提示通过 dispatch.target_library_name 还是 dispatch 里的旧值，
  // 实际重算在 submit 时由后端做（不传 target 字段就触发）
}

// 提交：phase_status=needs_review 走"审核确认入流水线"（强制 phase 流转），
// 其它走"编辑"（仅改字段，不动 phase）
const submitReview = async () => {
  if (!reviewTarget.value) return
  if (!reviewForm.media_type) {
    ElMessage.warning('请选媒体类型')
    return
  }
  if (reviewForm._target_mode === 'manual'
      && (!reviewForm.target_library_id || !reviewForm.target_path)) {
    ElMessage.warning('手动指定目标时，"目标库 ID" 和 "目标路径" 都得填')
    return
  }

  reviewSaving.value = true
  try {
    const basePayload = {
      media_type: reviewForm.media_type,
      title: reviewForm.title || null,
      year: reviewForm.year || null,
      series_name: reviewForm.series_name || null,
    }
    if (reviewForm._target_mode === 'manual') {
      basePayload.target_library_id = reviewForm.target_library_id
      basePayload.target_path = reviewForm.target_path
    }

    if (reviewIsConfirm.value) {
      // 审核确认入流水线（confirm_needs_review 接口自动推进 phase）
      await dispatchApi.confirmNeedsReview(reviewTarget.value.hash, basePayload)
      ElMessage.success('已通过审核 → 流水线接管')
    } else {
      // 普通编辑（edit_dispatch_row 接口仅改字段）
      const payload = { ...basePayload }
      if (reviewForm._target_mode === 'auto') {
        payload.recompute_target = true
      }
      const r = await dispatchApi.editDispatchRow(reviewTarget.value.hash, payload)
      if (r.data?.warning) {
        ElMessage.warning(r.data.warning)
      } else {
        ElMessage.success(`已保存：→ ${r.data?.after?.target_path || ''}`)
      }
    }
    reviewVisible.value = false
    load(true)
  } catch (e) {
    ElMessage.error('保存失败：' + (e.response?.data?.detail || e.message))
  } finally {
    reviewSaving.value = false
  }
}

// 重新识别：跑后端识别链拿建议回填表单（不写库；保存才落盘）
const reidentifyInReview = async () => {
  if (!reviewTarget.value) return
  reviewReidentifying.value = true
  try {
    const r = await dispatchApi.reidentifyDispatchRow(reviewTarget.value.hash)
    const ident = r.data?.identified || {}
    const target = r.data?.target || {}
    // 回填表单（user 没点保存前不写 DB）
    if (ident.media_type && ident.media_type !== 'unknown') {
      reviewForm.media_type = ident.media_type
    }
    if (ident.title) reviewForm.title = ident.title
    if (ident.year) reviewForm.year = ident.year
    if (ident.series_name) reviewForm.series_name = ident.series_name
    // 目标：跑出来的库 / 路径回填到 manual 字段，方便用户切到 manual 模式直接用
    if (target.target_library_id) reviewForm.target_library_id = target.target_library_id
    if (target.target_path) reviewForm.target_path = target.target_path
    const conf = ident.confidence
    ElMessage.success(
      `识别结果：${ident.media_type || '?'} / ${ident.title || '?'} `
      + `(${ident.source || '?'}, conf=${conf != null ? conf.toFixed(2) : '?'})`
    )
  } catch (e) {
    ElMessage.error('重新识别失败：' + (e.response?.data?.detail || e.message))
  } finally {
    reviewReidentifying.value = false
  }
}

// 拒绝命令分发：dismiss / remove_torrent / remove_files
// 仅"移除文件"要二次确认（不可恢复），其余直接执行
const onReviewDismissCmd = async (cmd) => {
  if (!reviewTarget.value) return
  const name = reviewTarget.value.name

  const conf = {
    dismiss:        { deleteTorrent: false, deleteFiles: false, ok: '已拒绝' },
    remove_torrent: { deleteTorrent: true,  deleteFiles: false, ok: '已移除种子' },
    remove_files:   { deleteTorrent: true,  deleteFiles: true,  ok: '已移除种子和文件' },
  }[cmd]
  if (!conf) return

  // 仅移除文件弹一次确认（其他两档静默执行）
  if (cmd === 'remove_files') {
    try {
      await ElMessageBox.confirm(
        `将从 qB 删除"${name}"，并连同已下载的文件一起删除。\n\n此操作不可恢复！`,
        '⚠️ 确认移除文件',
        {
          type: 'error',
          confirmButtonText: '确认删除',
          cancelButtonText: '取消',
        },
      )
    } catch { return }
  }

  try {
    await dispatchApi.dismissNeedsReview(
      reviewTarget.value.hash,
      conf.deleteTorrent,
      conf.deleteFiles,
    )
    ElMessage.success(conf.ok)
    reviewVisible.value = false
    load(true)
  } catch (e) {
    ElMessage.error('操作失败：' + (e.response?.data?.detail || e.message))
  }
}

const filter = ref('all')
const loading = ref(false)        // 仅首次加载/手动操作时 true，触发表格 v-loading 蒙版
const qbitTorrents = ref([])
const qbitUrl = ref('')
const selected = ref([])
const tableRef = ref(null)
const cleanupLoading = ref(false)
const pollPaused = ref(false)     // 页面 hidden 时暂停轮询（document.visibilitychange）

// 轮询间隔：2s 比之前的 3s 更接近"实时"，但走静默路径不闪 v-loading 也不破坏选择
const POLL_INTERVAL = 2000

// 配额状态
const quota = reactive({
  used: 0, limit: 0, ratio: 0, state: 'unknown', torrents_count: 0,
})
// 全局传输状态
const transfer = reactive({
  dl_info_speed: 0, up_info_speed: 0,
  dl_info_data: 0, up_info_data: 0,
  global_dl_limit: 0, global_up_limit: 0,
  alt_speed_enabled: false,
})
// 限速 popover form
const speedLimitForm = reactive({ dl_kbs: 0, up_kbs: 0 })
const speedPopoverVisible = ref(false)
// 上次成功应用的值，用来 blur 时判断"真改了才发请求"
const lastAppliedSpeed = reactive({ dl_kbs: 0, up_kbs: 0 })

let timer = null

/**
 * 加载下载状态。
 * @param silent  true=后台轮询（不闪 v-loading 蒙版，保留选择状态）
 *                false=首次加载/手动触发（显示蒙版）
 *
 * el-table 已 row-key="hash" + reserve-selection，数据替换不破坏选择。
 */
const load = async (silent = false) => {
  if (!silent) loading.value = true
  try {
    const [r, q, ti] = await Promise.allSettled([
      discoverApi.listDownloads({ filter_status: filter.value }),
      dispatchApi.getQuota(),
      discoverApi.transferInfo(),
    ])
    if (r.status === 'fulfilled') {
      qbitTorrents.value = r.value.data.qbittorrent || []
      // 手动添加的种子分析完后落到 needs_review → 自动弹审核 modal（用户期望立刻处理）
      // Jackett 推的不在 pendingManualHashes 里，会静默挂在主表上等用户主动点
      _autoOpenManualReview()
    }
    if (q.status === 'fulfilled') {
      Object.assign(quota, q.value.data)
    }
    if (ti.status === 'fulfilled') {
      Object.assign(transfer, ti.value.data)
      // 同步限速 form 显示当前值（KB/s）—— 但 popover 打开时不覆盖，避免用户正在
      // 输入时 2s 轮询把值洗掉
      if (!speedPopoverVisible.value) {
        speedLimitForm.dl_kbs = Math.round((transfer.global_dl_limit || 0) / 1024)
        speedLimitForm.up_kbs = Math.round((transfer.global_up_limit || 0) / 1024)
        lastAppliedSpeed.dl_kbs = speedLimitForm.dl_kbs
        lastAppliedSpeed.up_kbs = speedLimitForm.up_kbs
      }
    }
  } catch (e) {
    if (!silent) console.error('加载失败', e)
  } finally {
    if (!silent) loading.value = false
  }
}

const startTimer = () => {
  stopTimer()
  timer = setInterval(async () => {
    // 标签页隐藏时跳过这次轮询，省带宽（用户切回来 visibility change 会立刻重启）
    if (document.hidden) {
      pollPaused.value = true
      return
    }
    pollPaused.value = false
    await load(true)   // 静默：不触发 v-loading，保选择
    // reviewCount 直接 derive 自主表 qbitTorrents（每 2s 轮询带回最新 dispatch 状态），
    // 不需要单独查 needs-review 端点
    // dispatch 流水线 + jellyfin-watcher 已经在后端把完成种子推到 jellyfin，
    // 不再需要前端定时调 /sync-completed
  }, POLL_INTERVAL)
}

const stopTimer = () => {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

// 标签页可见性变化：从隐藏切回可见时立刻 load 一次（视觉上不延迟）
const onVisibilityChange = () => {
  if (!document.hidden) {
    pollPaused.value = false
    load(true)
  } else {
    pollPaused.value = true
  }
}

// ---- 单条操作 ----

const pause = async (row) => {
  await discoverApi.pause(row.hash)
  await load()
}

const resume = async (row) => {
  await discoverApi.resume(row.hash)
  await load()
}

const onRowCommand = async (cmd, row) => {
  try {
    if (cmd === 'force_start') {
      await discoverApi.forceStart(row.hash, true)
      ElMessage.success('已强制启动')
    } else if (cmd === 'recheck') {
      await discoverApi.recheck(row.hash)
      ElMessage.success('已发起重新校验')
    } else if (cmd === 'reannounce') {
      await discoverApi.reannounce(row.hash)
      ElMessage.success('已发起重新汇报')
    } else if (cmd === 'retry_dispatch') {
      const r = await discoverApi.retryDispatchRow(row.hash)
      ElMessage.success(`已重试：${r.data?.old_phase || ''} → ${r.data?.new_phase || ''}`)
    } else if (cmd === 'edit_dispatch') {
      openEdit(row)
      return    // 弹窗自己 load，不走下面的 await load()
    } else if (cmd === 'redispatch') {
      try {
        await ElMessageBox.confirm(
          `重新走一遍完整流程：识别 → 拷贝 → 通知 Jellyfin → 字幕/音轨。\n\n`
          + `已有的文件如果跟新目标一致会跳过；目标变了会重新拷贝到新位置（旧文件保留）。`,
          `重新入库 ${row.name}`,
          {
            type: 'warning',
            confirmButtonText: '开始',
            cancelButtonText: '取消',
          },
        )
      } catch { return }   // 用户取消
      const r = await dispatchApi.redispatchDispatchRow(row.hash)
      ElMessage.success(`已重新提交，${r.data?.old_phase || ''} → 分析中`)
    } else if (cmd === 'delete' || cmd === 'delete_files') {
      const deleteFiles = cmd === 'delete_files'
      try {
        await ElMessageBox.confirm(
          deleteFiles ? `删除 ${row.name} 并连同文件？` : `删除 ${row.name}（保留文件）？`,
          '确认删除',
          { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
        )
      } catch { return }
      await discoverApi.remove(row.hash, deleteFiles)
      ElMessage.success(deleteFiles ? '已删除（含文件）' : '已删除（保留文件）')
    }
    await load()
  } catch (e) {
    console.error(e)
    ElMessage.error(`操作失败：${e?.message || e}`)
  }
}

// ---- 批量操作 ----

const onSelectionChange = (rows) => {
  selected.value = rows
}

const clearSelected = () => {
  tableRef.value?.clearSelection()
  selected.value = []
}

const bulkAction = async (action) => {
  if (!selected.value.length) return
  const hashes = selected.value.map(r => r.hash)
  await discoverApi.bulkAction({ hashes, action })
  ElMessage.success(`批量${actionLabel(action)} ${hashes.length} 项`)
  clearSelected()
  await load()
}

const bulkDelete = async () => {
  if (!selected.value.length) return
  try {
    const action = await ElMessageBox.confirm(
      `删除选中的 ${selected.value.length} 个种子？`,
      '确认删除',
      {
        confirmButtonText: '同时删除文件',
        cancelButtonText: '仅移除任务',
        distinguishCancelAndClose: true,
        type: 'warning',
      },
    )
    const hashes = selected.value.map(r => r.hash)
    await discoverApi.bulkAction({
      hashes, action: 'delete', delete_files: action === 'confirm',
    })
    ElMessage.success(`已批量删除 ${hashes.length} 项`)
  } catch (act) {
    if (act === 'cancel') {
      const hashes = selected.value.map(r => r.hash)
      await discoverApi.bulkAction({
        hashes, action: 'delete', delete_files: false,
      })
      ElMessage.success(`已批量移除 ${hashes.length} 项（保留文件）`)
    }
  }
  clearSelected()
  await load()
}

const actionLabel = (a) => ({
  pause: '暂停', resume: '恢复', force_start: '强制启动',
  recheck: '重新校验', reannounce: '重新汇报', delete: '删除',
}[a] || a)

// ---- 清理 ----
// 按规则清理（分享率 / 做种天数 达标的种子），与配额状态无关。
// 后端 endpoint：/api/dispatch/quota/soft-cleanup → tools.dispatch.quota.regular_cleanup
const onCleanup = async () => {
  cleanupLoading.value = true
  try {
    const r = await dispatchApi.softCleanupNow()
    const n = r.data?.cleaned_count || 0
    if (n) {
      ElMessage.success(`清理 ${n} 个种子，释放 ${formatSize(r.data.freed_bytes)}`)
    } else {
      ElMessage.info('当前没有可清理的种子（已转移完成 + 分享率/做种天数达标 才会被清）')
    }
    await load()
  } finally {
    cleanupLoading.value = false
  }
}

// ---- 限速 ----

// blur 触发：值跟上次应用值一样就跳过，避免每次切换输入框都打 API
const applySpeedLimit = async () => {
  const dl = speedLimitForm.dl_kbs || 0
  const up = speedLimitForm.up_kbs || 0
  if (dl === lastAppliedSpeed.dl_kbs && up === lastAppliedSpeed.up_kbs) return
  try {
    await discoverApi.setSpeedLimit({
      download_limit: dl * 1024,
      upload_limit: up * 1024,
    })
    lastAppliedSpeed.dl_kbs = dl
    lastAppliedSpeed.up_kbs = up
    ElMessage.success(`限速已更新（↓${dl} ↑${up} KB/s）`)
  } catch (e) {
    ElMessage.error('限速设置失败：' + (e.response?.data?.detail || e.message))
  }
}

const onToggleAltSpeed = async () => {
  await discoverApi.toggleAltSpeed()
  await load()
}

// ---- 工具 ----

const isPaused = (state) => /paused|stopped/i.test(state || '')

const stateLabel = (s) => ({
  downloading: '下载中', uploading: '做种中',
  pausedUP: '已暂停', pausedDL: '已暂停',
  stoppedUP: '已停止', stoppedDL: '已停止',          // qB 5.0+ 新增
  queuedUP: '排队中', queuedDL: '排队中',
  stalledUP: '停滞中', stalledDL: '停滞中',
  checkingUP: '校验中', checkingDL: '校验中',
  checkingResumeData: '校验中',                       // 启动时校验
  moving: '移动中',
  error: '错误', missingFiles: '缺文件',
  metaDL: '元数据', allocating: '分配中',
  forcedUP: '强制做种', forcedDL: '强制下载',
  unknown: '未知',
}[s] || s)

const stateType = (s) => {
  if (!s) return ''
  if (/error|missing/i.test(s)) return 'danger'
  if (/paused|stopped|stalled/i.test(s)) return 'info'
  if (/uploading|forcedUP/i.test(s)) return 'success'
  if (/forcedDL/i.test(s)) return 'warning'
  return ''
}

const formatSize = (bytes) => {
  if (!bytes || bytes < 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024
    i++
  }
  return `${bytes.toFixed(1)} ${units[i]}`
}

const formatEta = (seconds) => {
  if (!seconds || seconds < 0 || seconds >= 8640000) return '∞'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

// 流水线 phase 中文化（DownloadDispatchMap.phase 字段，详见 tools/dispatch/phases.py）
const phaseLabel = (p) => ({
  analyzing: '分析中',
  dispatch_queued: '已入队',
  downloading: '下载中',
  download_done: '下载完成',
  copying: '复制中',
  organizing: '整理中',
  jellyfin_recognizing: '入库识别中',
  jellyfin_recognize_done: '入库已识别',
  subtitle_aligning: '字幕对齐中',
  subtitle_fetching: '抓字幕中',
  audio_track_order_adjusting: '调音轨中',
  all_jobs_done: '全部完成',
  cleaned: '已清理',
  dismissed: '已拒绝',
}[p] || p)

const phaseTagType = (phase, status) => {
  if (status === 'failed') return 'danger'
  if (phase === 'all_jobs_done') return 'success'
  if (['cleaned', 'dismissed'].includes(phase)) return 'info'
  if (['copying', 'organizing', 'subtitle_aligning', 'subtitle_fetching', 'audio_track_order_adjusting'].includes(phase)) return 'warning'
  if (['analyzing', 'dispatch_queued', 'downloading'].includes(phase)) return 'info'
  if (['jellyfin_recognizing', 'jellyfin_recognize_done', 'download_done'].includes(phase)) return 'warning'
  return ''
}

const formatAddedOn = (epoch) => {
  if (!epoch) return '-'
  const d = new Date(epoch * 1000)
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString().slice(0, 5)
}

const loadQbitUrl = async () => {
  try {
    const r = await configApi.getFull()
    const host = r.data?.config?.qbittorrent?.host
    if (host) qbitUrl.value = String(host).replace(/\/$/, '') + '/'
  } catch {}
}

onMounted(() => {
  load()
  loadQbitUrl()
  startTimer()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  stopTimer()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<style lang="scss" scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  h2 { margin: 0; }
}

.header-actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

.ext-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 12px;
  font-size: 13px;
  color: var(--jt-text-regular);
  text-decoration: none;
  border: 1px solid var(--jt-card-border);
  border-radius: 4px;
  background: var(--jt-card-bg);
  transition: all 0.15s ease;

  &:hover {
    color: var(--jt-link);
    border-color: var(--jt-link);
  }

  .el-icon { font-size: 14px; }
}

// ---- 状态卡片 ----
.status-card {
  margin-bottom: 12px;
  :deep(.el-card__body) {
    padding: 14px 18px;
  }
}

.status-row {
  display: flex;
  align-items: center;
  gap: 24px;
  flex-wrap: wrap;
}

.quota-block {
  flex: 1;
  min-width: 320px;

  .quota-line {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 6px;
  }
  .quota-label {
    font-size: 13px;
    color: var(--jt-text-regular);
  }
  .quota-value {
    font-size: 13px;
    font-weight: 600;
    margin-right: auto;
    &.state-normal { color: var(--jt-success); }
    &.state-warn { color: var(--jt-warning); }
    &.state-cleanup, &.state-over { color: var(--jt-danger); animation: pulse 1.5s infinite; }
    &.state-disabled { color: var(--jt-text-muted); font-style: italic; }
  }
  .quota-bar-wrap {
    .quota-bar {
      width: 100%;
      height: 8px;
      background: var(--jt-divider-light);
      border-radius: 4px;
      overflow: hidden;
    }
    .quota-bar-fill {
      height: 100%;
      transition: width 0.4s ease, background-color 0.4s;
      &.state-normal { background: linear-gradient(90deg, #6366f1, #10b981); }
      &.state-warn { background: linear-gradient(90deg, #6366f1, #f59e0b); }
      &.state-cleanup, &.state-over { background: linear-gradient(90deg, #f59e0b, #ef4444); }
    }
  }
}

.speed-block {
  display: flex;
  flex-direction: column;
  font-size: 13px;
  color: var(--jt-text-regular);
  min-width: 180px;
  .speed-line { display: flex; align-items: center; gap: 4px; }
  .speed-icon { color: var(--jt-text-muted); }
  .speed-value { font-weight: 600; color: var(--jt-text-primary); }
  .speed-totals {
    margin-top: 4px;
    font-size: 11px;
    color: var(--jt-text-muted);
  }
}

.speed-limit-block {
  display: flex;
  gap: 8px;
}

.speed-limit-form {
  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;

    .lbl {
      width: 32px;
      font-size: 12px;
      color: var(--jt-text-regular);
      flex-shrink: 0;
    }
    .unit {
      font-size: 11px;
      color: var(--jt-text-muted);
    }
  }
  .row:last-of-type { margin-bottom: 0; }
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.55; }
}

// ---- 种子列表工具栏（添加 / 过滤 / 选中后内嵌批量按钮）----
.torrents-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;

  // 选中后出现的批量按钮组：靠右对齐
  .bulk-group {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 6px;

    .el-button + .el-button {
      margin-left: 0;
    }
  }

  // toolbar 内的相邻 el-button 抵消默认 12px margin，让 gap 真正起效
  .el-button + .el-button {
    margin-left: 0;
  }
}

// 批量按钮内嵌在 toolbar，无背景；"已选 N 项"高亮提示，跟 small 按钮字号一致
.bulk-info {
  color: var(--jt-brand-dark);
  font-weight: 600;
  font-size: 12px;
}

// ---- 人工审核 modal 内部样式 ----
.review-modal {
  .review-meta {
    .meta-line {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 6px;
      font-size: 13px;

      .meta-label {
        color: var(--jt-text-secondary);
        flex-shrink: 0;
        min-width: 56px;
      }
      .meta-name {
        color: var(--jt-text-primary);
        font-weight: 500;
        word-break: break-all;
        line-height: 1.4;
      }
    }
    .review-reason {
      color: var(--jt-warning);
      font-size: 12px;
      font-style: italic;
      .el-icon { font-size: 14px; }
    }
    .review-dup {
      gap: 8px;
      font-size: 12px;
      color: var(--jt-text-regular);
      .muted { color: var(--jt-text-muted); }
      strong { color: var(--jt-success); font-weight: 600; }
    }
  }

  .auto-target {
    color: var(--jt-text-regular);
    font-size: 13px;
    strong { color: var(--jt-success); font-weight: 600; }
    .muted { color: var(--jt-text-muted); font-size: 12px; }
    &.path-text {
      font-family: ui-monospace, SFMono-Regular, monospace;
      font-size: 12px;
      color: var(--jt-text-secondary);
      word-break: break-all;
    }
  }
}

.review-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;

  .footer-left,
  .footer-right {
    display: flex;
    gap: 6px;

    // 抵消 el-button 相邻 sibling 的默认 margin-left:12px，让 gap 真正起效
    .el-button + .el-button {
      margin-left: 0;
    }
  }
}

.lib-tag {
  display: inline-block;
  padding: 1px 8px;
  font-size: 11px;
  background: var(--jt-success-tint);
  color: var(--jt-success-text);
  border: 1px solid var(--jt-success-border);
  border-radius: 3px;
}

.path-text {
  font-size: 11px;
  color: var(--jt-text-regular);
  font-family: ui-monospace, SFMono-Regular, monospace;
  word-break: break-all;
}

.muted { color: var(--jt-text-muted); }

// 紧凑表格：缩小列内 padding，让信息密度更高
.downloads-table {
  :deep(.el-table__cell) {
    padding: 3px 0;
  }
  :deep(.cell) {
    padding-left: 4px;
    padding-right: 4px;
    line-height: 1.4;
    font-size: 12px;
  }
  :deep(th .cell) {
    font-weight: 600;
  }
}

// 状态详情列：按 phase_status 着色
.status-msg {
  font-size: 12px;
  display: inline-block;
  max-width: 100%;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  vertical-align: middle;

  &.status-running       { color: var(--jt-text-regular); }
  &.status-succeeded     { color: var(--jt-success); }
  &.status-warned        { color: var(--jt-warning); }
  &.status-skipped       { color: var(--jt-text-muted); }
  &.status-failed        { color: var(--jt-danger); font-weight: 500; }
  &.status-needs_review  { color: var(--jt-warning); font-weight: 500; }
  &.status-metadata_pending { color: var(--jt-brand); }
  &.status-stalled       { color: var(--jt-warning); }
  &.status-paused        { color: var(--jt-text-muted); }
}

.main-tabs {
  margin-bottom: 8px;
  :deep(.el-tabs__nav-wrap::after) {
    height: 1px;
  }
}

.rss-card {
  .rss-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid #e5e7eb;
    flex-wrap: wrap;

    .rss-header-right {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .ext-link {
      font-size: 12px;
      color: var(--jt-brand);
      text-decoration: none;
      &:hover { text-decoration: underline; }
    }
  }
  h4 {
    font-size: 14px;
    margin: 12px 0 8px 0;
  }

  .feed-title-row {
    display: flex;
    align-items: center;
    gap: 12px;
    flex: 1;
    .feed-name { font-weight: 500; }
    .feed-meta {
      color: var(--jt-text-muted);
      font-size: 12px;
      margin-left: auto;
      .unread-badge {
        margin-left: 8px;
        padding: 1px 8px;
        border-radius: 10px;
        background: #fef3c7;
        color: #b45309;
        font-weight: 500;
      }
    }
  }

  // 已读条目淡化
  :deep(.rss-row-read) td {
    color: var(--jt-text-muted);
  }

  // qB RSS 全局设置条
  .rss-settings-bar {
    display: flex;
    align-items: center;
    gap: 18px;
    flex-wrap: wrap;
    padding: 10px 12px;
    margin-top: 12px;
    background: var(--jt-fill-light);
    border: 1px solid var(--jt-card-border);
    border-radius: 6px;

    .setting-item {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--jt-text-regular);

      label { cursor: default; }
    }
  }
}

// 顶层（不嵌在 .rss-card 里）—— 规则编辑器在 el-dialog 里被 teleport 到 body，
// 嵌套选择器 .rss-card .hint-list 因为 .rss-card 不再是祖先，会失效
.re-code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  color: var(--jt-text-regular);
  background: var(--jt-fill-light);
  padding: 1px 4px;
  border-radius: 3px;
  white-space: nowrap;
}
.hint-tight {
  line-height: 1.55;
  margin-top: 2px;
}
.hint-list {
  margin: 2px 0 0;
  padding-left: 30px;
  list-style: none;

  li {
    position: relative;
    margin: 0;
    padding: 0;
    line-height: 1.55;

    &::before {
      content: '·';
      position: absolute;
      left: -12px;
      color: var(--jt-text-muted);
      font-weight: bold;
    }
  }
}
</style>

<!-- 非 scoped 兜底：el-dialog teleport 后 scoped 的 data-v 可能不带到子节点上 -->
<style lang="scss">
.hint-list-global {
  margin: 2px 0 0;
  padding-left: 12px;       /* 跟 el-input 内文本起点对齐（input 默认 padding 约 11-12px）*/
  list-style: none;
}
.hint-list-global li {
  position: relative;
  margin: 0;
  padding: 0;
  line-height: 1.55;
}
.hint-list-global li::before {
  content: '·';
  position: absolute;
  left: -10px;
  color: var(--jt-text-muted);
  font-weight: bold;
}
.re-code-global {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  color: var(--jt-text-regular);
  background: var(--jt-fill-light);
  padding: 1px 4px;
  border-radius: 3px;
  white-space: nowrap;
}
.hint-tight-global {
  line-height: 1.55;
  margin-top: 2px;
}

/* 规则编辑器 dialog：表单行间距收紧 */
.el-dialog .el-form-item {
  margin-bottom: 10px;
}
.el-dialog .form-hint {
  margin-top: 1px;
  line-height: 1.45;
}

/* 规则名 + 启用开关同行 */
.el-dialog .name-row {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
}
.el-dialog .name-row-switch {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--jt-text-regular);
  flex-shrink: 0;
}

/* 规则编辑 dialog：垂直居中 + body 内部滚动（防内容超过视口外层出滚动条） */
.rule-edit-dialog {
  display: flex;
  flex-direction: column;
  /* align-center 之外再加最大高度限制，body 超出在内部滚 */
  max-height: calc(100vh - 80px);
}
.rule-edit-dialog .el-dialog__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

/* AI 辅助横幅 */
.el-dialog .llm-helper {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  margin-bottom: 14px;
  background: linear-gradient(135deg, rgba(var(--jt-brand-rgb), 0.06) 0%, rgba(var(--jt-brand-rgb), 0.12) 100%);
  border: 1px solid rgba(var(--jt-brand-rgb), 0.2);
  border-radius: 6px;
}
.el-dialog .llm-helper-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.el-dialog .llm-helper-row .el-input {
  flex: 1;
}
.el-dialog .llm-helper-explain {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12px;
  color: var(--jt-brand);
  line-height: 1.45;

  .el-icon {
    flex-shrink: 0;
    margin-top: 2px;
  }
}
</style>

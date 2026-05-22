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
      <!-- ============ 可用性检测（放最前，进入页面立即跑本地零成本检查） ============ -->
      <el-tab-pane name="diagnostics">
        <template #label>
          <div class="tab-label">
            <el-icon><Monitor /></el-icon>
            <span>可用性检测</span>
          </div>
        </template>

        <!-- 本地（DB / 系统命令行工具）：打开 tab 自动跑 -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <el-icon><Aim /></el-icon>
              <span>本地环境</span>
              <el-tag size="small" type="info" effect="plain">自动检测，无网络成本</el-tag>
              <el-button
                size="small"
                :icon="Refresh"
                style="margin-left: auto"
                :loading="diagSystemLoading"
                @click="loadDiagnosticsSystem"
              >重新检测</el-button>
            </div>
          </template>
          <el-table
            :data="diagSystemItems"
            stripe
            size="small"
            v-loading="diagSystemLoading"
            empty-text="加载中..."
          >
            <el-table-column label="项目" prop="label" min-width="220" />
            <el-table-column label="状态" width="120">
              <template #default="{ row }">
                <el-tag size="small" :type="diagTagType(row.status)" effect="plain">
                  {{ diagStatusLabel(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="版本" prop="message" min-width="380" show-overflow-tooltip />
            <el-table-column label="耗时" width="100">
              <template #default="{ row }">
                <span class="muted">{{ row.elapsed_ms ? `${row.elapsed_ms} ms` : '-' }}</span>
              </template>
            </el-table-column>
          </el-table>
          <div class="form-hint" style="margin-top: 8px">
            未找到的工具：缺失会导致对应功能退化（不崩溃）。安装指引见 README「系统级依赖」章节
          </div>
        </el-card>

        <!-- 网络服务：手动按钮触发 -->
        <el-card
          v-for="group in diagServiceGroups"
          :key="group.key"
          shadow="never"
          class="cfg-card"
        >
          <template #header>
            <div class="cfg-card-head">
              <span class="badge" :class="group.badgeClass">{{ group.badge }}</span>
              <span>{{ group.title }}</span>
              <el-tag size="small" type="info" effect="plain">{{ group.hint }}</el-tag>
              <el-button
                size="small"
                :icon="VideoPlay"
                style="margin-left: auto"
                :disabled="!group.items.some(i => i.enabled)"
                :loading="diagBatchLoading[group.key]"
                @click="runDiagnosticsGroup(group)"
              >测试本组全部</el-button>
            </div>
          </template>
          <el-table :data="group.items" stripe size="small">
            <el-table-column label="服务" prop="label" min-width="200" />
            <el-table-column label="启用" width="80">
              <template #default="{ row }">
                <el-tag v-if="row.enabled" size="small" type="success" effect="plain">已配置</el-tag>
                <el-tag v-else size="small" type="info" effect="plain">未启用</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="120">
              <template #default="{ row }">
                <el-tag
                  v-if="row.result"
                  size="small"
                  :type="diagTagType(row.result.status)"
                  effect="plain"
                >
                  {{ diagStatusLabel(row.result.status) }}
                </el-tag>
                <span v-else class="muted">未测</span>
              </template>
            </el-table-column>
            <el-table-column
              :label="group.key === 'core' ? '版本' : '信息'"
              min-width="320"
              show-overflow-tooltip
            >
              <template #default="{ row }">
                <span v-if="row.result">{{ row.result.message }}</span>
                <span v-else class="muted">点右侧「测试」获取结果</span>
              </template>
            </el-table-column>
            <el-table-column label="耗时" width="100">
              <template #default="{ row }">
                <span class="muted">{{ row.result?.elapsed_ms ? `${row.result.elapsed_ms} ms` : '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100" align="right">
              <template #default="{ row }">
                <el-button
                  size="small"
                  link
                  :disabled="!row.enabled"
                  :loading="row.loading"
                  @click="runDiagnosticsItem(row)"
                >
                  测试
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- ============ 基础配置（Jellyfin） ============ -->
      <el-tab-pane name="basic">
        <template #label>
          <div class="tab-label">
            <el-icon><Connection /></el-icon>
            <span>基础配置</span>
          </div>
        </template>

        <!-- 服务端口（监听端口，前端页面 / API 都走这里） -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <el-icon><Aim /></el-icon>
              <span>服务端口</span>
              <el-tag size="small" type="info" effect="plain">改动需重启服务</el-tag>
            </div>
          </template>
          <el-form :model="form.server" label-width="120px">
            <el-form-item label="监听端口">
              <el-input-number v-model="form.server.backend_port" :min="1024" :max="65535" controls-position="right" />
              <div class="form-hint">
                FastAPI + 前端 SPA 共用此端口（生产模式）。常见占用：Jellyfin <code>8096</code> / qB <code>8080</code> / Jackett <code>9117</code> / Sonarr <code>8989</code> —— <b>避开它们</b>。建议 <code>8099</code> 或 <code>8088</code> 之外的 808x 段
              </div>
            </el-form-item>
          </el-form>
        </el-card>

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
              <div class="form-hint">后端连接用（内网地址即可）</div>
            </el-form-item>
            <el-form-item label="外部地址">
              <el-input v-model="form.jellyfin.external_url" placeholder="https://jf.example.com" />
              <div class="form-hint">公网可访问的 Jellyfin 地址，用于页面跳转链接。留空则使用上面的服务器地址</div>
            </el-form-item>
            <el-form-item label="API Key">
              <el-input v-model="form.jellyfin.api_key" type="password" show-password />
              <div class="form-hint">
                Jellyfin 后台 → 控制台 → API 密钥 → 创建（必须是管理员账号创建的 key）
              </div>
            </el-form-item>
            <el-form-item label=" ">
              <el-button
                size="small"
                :loading="checkingApiKey"
                :icon="Check"
                @click="onCheckApiKey"
              >检查权限</el-button>
              <el-tag
                v-if="apiKeyCheckResult"
                :type="apiKeyCheckResult.ok ? 'success' : 'danger'"
                size="small"
                effect="light"
                style="margin-left: 12px"
              >
                {{ apiKeyCheckResult.ok ? '✓ 管理员权限 OK' : '✗ 权限不足或不通' }}
              </el-tag>
              <div v-if="apiKeyCheckResult" class="form-hint" style="margin-top: 4px">
                <span v-if="apiKeyCheckResult.server_name">
                  服务器：{{ apiKeyCheckResult.server_name }}
                  <span v-if="apiKeyCheckResult.server_version">(v{{ apiKeyCheckResult.server_version }})</span>
                  ·
                </span>
                {{ apiKeyCheckResult.message }}
              </div>
              <div v-if="apiKeyCheckResult?.db_check" class="form-hint" style="margin-top: 4px">
                数据库直读：
                <el-tag
                  :type="apiKeyCheckResult.db_check.ok ? 'success' : 'danger'"
                  size="small" effect="light"
                >{{ apiKeyCheckResult.db_check.ok ? '✓' : '✗' }} {{ apiKeyCheckResult.db_check.message }}</el-tag>
              </div>
            </el-form-item>
            <el-form-item label="数据库路径">
              <el-input
                v-model="form.jellyfin.db_path"
                placeholder="留空走 REST；填了走 SQLite 直读"
              />
              <div class="form-hint">
                可选，仅用于 <code>lookup_jellyfin_item</code>（path → item 反查）加速；失败自动 fallback REST。
                Linux 默认 <code>/var/lib/jellyfin/data/jellyfin.db</code>，
                后端 user 需加入 jellyfin 组：
                <code>sudo usermod -a -G jellyfin &lt;user&gt;</code> 后重启后端。
              </div>
            </el-form-item>
            <el-form-item label="库信息缓存(天)">
              <el-input-number v-model="form.cache.library_days" :min="1" :max="90" controls-position="right" />
              <span class="form-hint" style="margin-left: 8px">
                视频数 / 总占用 / 总时长 / 缺海报 / 剧集树形 / 缺字幕统计 共用
              </span>
            </el-form-item>
          </el-form>

          <!-- 开发 / 调试开关 -->
          <el-divider content-position="left">
            <span class="sub-section-title">
              调试开关
              <el-tag size="small" type="info" effect="plain" style="margin-left: 6px">调试用</el-tag>
            </span>
          </el-divider>

          <div class="debug-block">
            <div class="debug-row">
              <span class="switch-label">操作按钮显示"测试模式"</span>
              <el-switch v-model="form.debug.show_dry_run_in_toolbar" size="small" />
              <span class="form-hint">
                媒体库 / 成人库工具栏按钮的 confirm 弹窗里展示 <strong>dry-run 复选框</strong>；
                平时关掉避免干扰，调试或验收影响范围时打开
              </span>
            </div>
          </div>

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

        <!-- 数据库 (PostgreSQL) section 已移除 UI 展示 ——
             连接配置仅在 config.yaml 里维护（database: 段），
             form.database 保留以便保存时不丢字段，但不再渲染表单 -->

        <!-- ===== 媒体元数据 ===== -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge meta-badge">META</span>
              <span>媒体元数据</span>
            </div>
          </template>

          <!-- 缓存策略 -->
          <el-divider content-position="left">
            <span class="sub-section-title">缓存</span>
          </el-divider>
          <el-form :model="form.metadata" label-width="160px" @submit.prevent>
            <el-form-item label="保存豆瓣完整信息">
              <el-switch v-model="form.metadata.store_douban_full" />
              <span class="form-hint" style="margin-left: 8px">
                关闭后只保存类型/时长等基本信息，不保存简介/海报/演员
              </span>
            </el-form-item>
            <el-form-item label="缓存保留天数">
              <el-input-number
                v-model="form.metadata.lru_keep_days"
                :min="0" :max="3650" :step="30"
                controls-position="right"
              />
              <span class="form-hint" style="margin-left: 8px">
                超过该天数未访问的条目会被清掉；填 0 永不清理
              </span>
            </el-form-item>
            <el-form-item label="自动更新天数">
              <el-input-number
                v-model="form.metadata.refresh_ttl_days"
                :min="1" :max="365" :step="1"
                controls-position="right"
              />
              <span class="form-hint" style="margin-left: 8px">
                超过该天数后台自动重新获取
              </span>
            </el-form-item>
          </el-form>

          <!-- 元数据语言 -->
          <el-divider content-position="left">
            <span class="sub-section-title">语言</span>
          </el-divider>
          <el-form :model="form.metadata" label-width="160px" @submit.prevent>
            <el-form-item label="刮削语言">
              <el-select v-model="form.metadata.scrape_language" style="width: 200px">
                <el-option label="English" value="en" />
                <el-option label="简体中文" value="zh" />
                <el-option label="日本語" value="ja" />
                <el-option label="한국어" value="ko" />
              </el-select>
            </el-form-item>
            <el-form-item label="显示语言">
              <el-select v-model="form.metadata.display_language" style="width: 200px">
                <el-option label="English" value="en" />
                <el-option label="简体中文" value="zh" />
              </el-select>
            </el-form-item>
            <div class="form-hint" style="margin: -4px 0 4px 160px">
              豆瓣源不受此选项影响，始终为中文
            </div>
          </el-form>
        </el-card>

        <!-- ===== 工具路径 section（原独立 tab 并入此处）===== -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge tools-badge">TOOLS</span>
              <span>外部命令行工具路径</span>
            </div>
          </template>

          <p class="form-hint" style="margin-bottom: 12px">
            启动时若指定的目录存在，会自动加到当前进程 PATH 最前面，方便 ffprobe / mkvpropedit 直接调用。
            留空则依赖系统 PATH。改完 <strong>需保存配置 + 重启后端</strong> 才生效。
          </p>

          <el-form :model="form.tools" label-width="160px" @submit.prevent>
            <el-form-item label="ffmpeg 目录">
              <el-input v-model="form.tools.ffmpeg_dir"
                        placeholder="如 C:/ffmpeg/bin 或 /usr/local/bin（留空用系统 PATH）"
                        clearable />
              <span class="form-hint">用于音轨扫描（ffprobe）</span>
            </el-form-item>

            <el-form-item label="mkvtoolnix 目录">
              <el-input v-model="form.tools.mkvtoolnix_dir"
                        placeholder="如 C:/Program Files/MKVToolNix 或 /usr/bin（留空用系统 PATH）"
                        clearable />
              <span class="form-hint">用于改默认音轨（mkvpropedit）</span>
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
            <el-form-item label="缓存 TTL(分钟)">
              <el-input-number v-model="form.cache.tmdb_minutes" :min="1" :max="10080" :step="30" controls-position="right" />
              <span class="form-hint" style="margin-left: 8px">热门 / 流行 / 详情列表的响应缓存</span>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- Wikidata 演员元数据 -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge wikidata">WD</span>
              <span>Wikidata 元数据</span>
              <el-tag size="small" :type="form.wikidata.enabled ? 'success' : 'info'" effect="plain">
                {{ form.wikidata.enabled ? '已启用' : '未启用' }}
              </el-tag>
            </div>
          </template>
          <el-form :model="form.wikidata" label-width="120px">
            <el-form-item label="启用">
              <el-switch v-model="form.wikidata.enabled" />
              <div class="form-hint">
                演员管理页面在 TMDB 找不到中文名时，回退到 Wikidata 拉取（公共 SPARQL 端点，无需 Key）
              </div>
            </el-form-item>
            <el-form-item label="User-Agent">
              <el-input v-model="form.wikidata.user_agent" placeholder="如 JellyfinHelper/1.0 (your@email.com)" />
              <div class="form-hint">
                Wikidata 强制要求带联系邮箱的 UA，否则会被 429。改完邮箱保存即可
              </div>
            </el-form-item>
            <el-form-item label="语言优先">
              <el-checkbox-group v-model="form.wikidata.language_order" class="lang-line">
                <el-checkbox label="zh">中文 (zh)</el-checkbox>
                <el-checkbox label="zh-Hans">简体 (zh-Hans)</el-checkbox>
                <el-checkbox label="zh-Hant">繁体 (zh-Hant)</el-checkbox>
                <el-checkbox label="en">英文 (en)</el-checkbox>
                <el-checkbox label="ja">日文 (ja)</el-checkbox>
                <el-checkbox label="ko">韩文 (ko)</el-checkbox>
              </el-checkbox-group>
              <div class="form-hint">
                按顺序找标签；勾的语言都没有时，落到 Wikidata label 默认值
              </div>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- MDB List 评分聚合 -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge mdblist">MDB</span>
              <span>MDB List 评分聚合</span>
              <el-tag size="small" :type="form.mdblist.enabled && form.mdblist.api_key ? 'success' : 'info'" effect="plain">
                {{ form.mdblist.enabled && form.mdblist.api_key ? '已配置' : (form.mdblist.enabled ? '缺 API Key' : '未启用') }}
              </el-tag>
            </div>
          </template>
          <el-form :model="form.mdblist" label-width="120px">
            <el-form-item label="启用">
              <el-switch v-model="form.mdblist.enabled" />
              <div class="form-hint">
                关闭后所有库列表 / 详情页不再显示 IMDb / Rotten Tomatoes / Metacritic / Trakt / Letterboxd 等多源评分
              </div>
            </el-form-item>
            <el-form-item label="API Key">
              <el-input v-model="form.mdblist.api_key" type="password" show-password />
              <div class="form-hint">
                <a href="https://mdblist.com/api" target="_blank">在 mdblist.com 登录后申请</a>
                — 免费额度 1000 req/天，足够单人媒体库使用
              </div>
            </el-form-item>
            <el-form-item label="缓存 TTL(天)">
              <el-input-number v-model="form.mdblist.cache_ttl_days" :min="1" :max="365" />
              <div class="form-hint">同一作品在该天数内不再重新拉取（电影评分变化不大，30 天合理）</div>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- 豆瓣（评分 + 片单 合并卡片）-->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge douban">豆</span>
              <span>豆瓣（评分 + 片单，HTML 爬虫）</span>
              <el-tag size="small" :type="form.douban.enabled ? 'success' : 'info'" effect="plain">
                {{ form.douban.enabled ? '已启用' : '未启用' }}
              </el-tag>
            </div>
          </template>

          <el-form :model="form.douban" label-width="140px">
            <el-form-item label="启用">
              <el-switch v-model="form.douban.enabled" />
              <div class="form-hint">关闭后评分和片单同时不工作</div>
            </el-form-item>

            <!-- 前台同步路径 -->
            <el-form-item label="评分缓存(天)">
              <el-input-number v-model="form.douban.cache_ttl_days" :min="1" :max="365" />
              <div class="form-hint">单条评分缓存有效期（MediaRating 表 douban_fetched_at）</div>
            </el-form-item>
            <el-form-item label="片单缓存(天)" v-if="form.douban_lists">
              <el-input-number v-model="form.douban_lists.cache_days" :min="1" :max="30" />
              <div class="form-hint">doulist 整页结果缓存。片单几乎不变，建议 ≥3</div>
            </el-form-item>

            <!-- 片单白名单 -->
            <el-divider content-position="left">
              <span class="sub-section-title">片单白名单（Discover 豆瓣 tab 下拉）</span>
            </el-divider>
            <el-form-item label="片单列表" v-if="form.douban_lists">
              <div class="doulist-rows">
                <div v-for="(item, idx) in form.douban_lists.lists" :key="idx" class="doulist-row">
                  <el-input v-model="item.name" placeholder="显示名（如 高分韩剧）" style="width: 200px" />
                  <el-input v-model="item.doulist_id" placeholder="doulist_id（数字 / chart / nowplaying / upcoming）" style="width: 220px" />
                  <el-select v-model="item.media_type" style="width: 100px">
                    <el-option label="电影" value="movie" />
                    <el-option label="剧集" value="tv" />
                  </el-select>
                  <el-button text type="danger" :icon="Delete" @click="form.douban_lists.lists.splice(idx, 1)" />
                </div>
                <el-button :icon="Plus" @click="form.douban_lists.lists.push({ name: '', doulist_id: '', media_type: 'movie' })">
                  添加片单
                </el-button>
              </div>
              <div class="form-hint">
                doulist_id 取自 <code>douban.com/doulist/&lt;ID&gt;/</code>；特殊：<code>chart</code>=排行榜 <code>nowplaying</code>=正在上映 <code>upcoming</code>=即将上映
              </div>
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
              <span style="font-size: 13px; color: var(--jt-text-secondary)">搜索偏好</span>
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
              <el-tag size="small" :type="(form.qbittorrent.api_key || form.qbittorrent.username) ? 'success' : 'info'" effect="plain">
                {{ (form.qbittorrent.api_key || form.qbittorrent.username) ? '已配置' : '未配置' }}
              </el-tag>
            </div>
          </template>
          <el-form :model="form.qbittorrent" label-width="120px">
            <el-form-item label="服务器地址">
              <el-input v-model="form.qbittorrent.host" placeholder="http://localhost:8080" />
            </el-form-item>
            <el-form-item label="API Key">
              <el-input
                v-model="form.qbittorrent.api_key"
                type="password"
                show-password
                placeholder="qbt_xxx... (qB ≥ 5.2.0 推荐；优先于下方用户名/密码)"
              />
              <div class="form-hint">
                qB 5.2.0+ 起：Preferences → WebUI → API Key 段点 Generate 复制粘贴这里。
                配了 api_key 就不用填用户名/密码（更安全、stateless）
              </div>
            </el-form-item>
            <el-form-item label="用户名">
              <el-input v-model="form.qbittorrent.username" placeholder="api_key 留空时才用" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="form.qbittorrent.password" type="password" show-password />
            </el-form-item>
            <!-- 下载路径已移到「下载流水线」tab → 配额 / 下载根目录，避免与那边重复 -->
          </el-form>
        </el-card>

        <!-- Trakt：实时观看活动信号 -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge trakt">Trakt</span>
              <span>实时观看活动（互补 TMDB 元数据流行度）</span>
              <el-tag size="small" :type="form.trakt.enabled && form.trakt.client_id ? 'success' : 'info'" effect="plain">
                {{ form.trakt.enabled && form.trakt.client_id ? '已配置' : (form.trakt.enabled ? '缺 Client ID' : '未启用') }}
              </el-tag>
            </div>
          </template>
          <el-form :model="form.trakt" label-width="120px">
            <el-form-item label="启用">
              <el-switch v-model="form.trakt.enabled" />
              <div class="form-hint">
                关闭后"热门推荐 → Trakt" tab 不可用
              </div>
            </el-form-item>
            <el-form-item label="Client ID">
              <el-input v-model="form.trakt.client_id" type="password" show-password placeholder="64 位十六进制" />
              <div class="form-hint">
                <a href="https://trakt.tv/oauth/applications" target="_blank">在 trakt.tv/oauth/applications 创建 application</a>
                — 公开 endpoint 无需 OAuth，只用 Client ID
              </div>
            </el-form-item>
            <el-form-item label="缓存 TTL(分钟)">
              <el-input-number v-model="form.trakt.cache_minutes" :min="1" :max="1440" :step="10" />
              <div class="form-hint">实时观看活动信号建议短一点（默认 60 min）</div>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- AniList：番剧专用，无需 key -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge anilist">A</span>
              <span>AniList 番剧推荐</span>
              <el-tag size="small" :type="form.anilist.enabled ? 'success' : 'info'" effect="plain">
                {{ form.anilist.enabled ? '已启用' : '未启用' }}
              </el-tag>
            </div>
          </template>
          <el-form :model="form.anilist" label-width="120px">
            <el-form-item label="启用">
              <el-switch v-model="form.anilist.enabled" />
              <div class="form-hint">
                公开 GraphQL，<b>不需要账号 / API key</b>。番剧元数据 / 排行优势：原生日文标题、季度划分、社区评分
              </div>
            </el-form-item>
            <el-form-item label="缓存 TTL(分钟)">
              <el-input-number v-model="form.anilist.cache_minutes" :min="1" :max="10080" :step="30" />
              <div class="form-hint">番剧排行变化慢（默认 240 min ≈ 4 小时）</div>
            </el-form-item>
          </el-form>
        </el-card>

      </el-tab-pane>

      <!-- ============ 字幕下载 ============ -->
      <!-- 缓存 TTL tab 已下线：各项搬到对应的服务卡片里
           ┌ TMDB API 缓存 → 第三方服务 / TMDB 卡片
           ├ 库信息缓存    → 基础配置 / Jellyfin 服务器卡片
           ├ MDB List      → 第三方服务 / MDB List 卡片（原本就有，删去重复）
           └ 豆瓣评分      → 第三方服务 / 豆瓣评分卡片（原本就有，删去重复） -->
      <el-tab-pane name="subtitle">
        <template #label>
          <div class="tab-label">
            <el-icon><Document /></el-icon>
            <span>字幕下载</span>
          </div>
        </template>

        <!-- 字幕语言需求 -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge subtitle-common">字幕</span>
              <span>字幕语言需求</span>
            </div>
          </template>
          <el-form :model="form.subtitle" label-width="160px">
            <el-form-item label-width="0" class="lang-stacked">
              <el-checkbox-group
                v-model="form.subtitle.required_langs"
                class="lang-line"
                @change="onRequiredLangsChange"
              >
                <el-checkbox label="chs">简体中文</el-checkbox>
                <el-checkbox label="cht">繁体中文</el-checkbox>
                <el-checkbox label="eng">英语</el-checkbox>
                <el-checkbox label="jpn">日语</el-checkbox>
                <el-checkbox label="kor">韩语</el-checkbox>
              </el-checkbox-group>
              <div class="form-hint">
                作为缺字幕统计的判断，可以多选；自动下载字幕时会补齐缺少的语言字幕。
                <strong>未指定简繁的中文字幕</strong>（如 <code>.chinese.srt</code> / <code>.中文.srt</code>，或内容嗅探不出简繁的）<strong>同时算覆盖简体和繁体</strong>，不会让你既缺简又缺繁。
              </div>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- 自动下载语言优先级 -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge subtitle-common">字幕</span>
              <span>自动下载语言优先级</span>
            </div>
          </template>
          <p class="form-hint" style="margin-bottom: 12px">
            自动下载字幕时按下方顺序匹配，前优先。支持双语复合 <strong>简英双语 / 繁英双语</strong>（对应字幕组的 <code>.chs.eng.srt</code> 这种格式，命中优先级最高）。
          </p>
          <SourcePool
            v-model="form.subtitle.downloading_langs"
            :all-keys="allDownloadingLangKeys"
            :labels="downloadingLangLabels"
            :min-items="1"
          />
        </el-card>

        <!-- 字幕格式偏好（紧跟语言偏好，跟"想要什么字幕"语义聚合） -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge subtitle-common">字幕</span>
              <span>格式偏好</span>
            </div>
          </template>
          <p class="form-hint" style="margin-bottom: 12px">
            压缩包内同语言有多种格式时，按下方顺序只保留排名最高的一份（不再重复下载所有格式）。
            <strong>ASS</strong> 支持丰富样式，<strong>SRT</strong> 通用兼容性好，<strong>SUP</strong> 是蓝光图形字幕。
          </p>
          <SourcePool
            v-model="form.subtitle.preferred_formats"
            :all-keys="allSubtitleFormatKeys"
            :labels="subtitleFormatLabels"
            :min-items="1"
          />
        </el-card>

        <!-- 字幕下载源 -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <el-icon><Files /></el-icon>
              <span>下载源</span>
            </div>
          </template>
          <p class="form-hint" style="margin-bottom: 12px">
            自动下载缺失字幕时按下方顺序尝试，第一个返回结果即采用。assrt / OpenSubtitles 走"搜索"接口；
            Shooter 用文件 hash 命中（中文电影率高、不需 Key）。
          </p>
          <SourcePool
            v-model="form.subtitle.sources"
            :all-keys="allSubtitleSourceKeys"
            :labels="subtitleSourceLabels"
            :min-items="1"
          />
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
          </el-form>
        </el-card>

        <!-- 射手字幕 assrt.net -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge assrt">射手</span>
              <span>assrt.net（射手字幕）</span>
              <el-tag size="small" :type="form.subtitle.assrt_api_token ? 'success' : 'info'" effect="plain">
                {{ form.subtitle.assrt_api_token ? '已配置' : '未配置' }}
              </el-tag>
            </div>
          </template>
          <el-form :model="form.subtitle" label-width="160px">
            <el-form-item label="API Token">
              <el-input v-model="form.subtitle.assrt_api_token" type="password" show-password />
              <div class="form-hint">
                注册账号后在
                <a href="https://secure.assrt.net/usercp.php" target="_blank">secure.assrt.net/usercp.php</a>
                复制 32 位 Token
              </div>
            </el-form-item>
          </el-form>
        </el-card>
      </el-tab-pane>

      <!-- ============ 缓存 TTL ============ -->
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
            <el-form-item label="关联 Jellyfin 库">
              <el-select
                v-model="form.adult.library_ids"
                multiple
                filterable
                collapse-tags
                collapse-tags-tooltip
                :loading="librariesLoading"
                placeholder="选择要纳入成人内容刮削的 Jellyfin 库（可多选）"
                style="min-width: 360px"
              >
                <el-option
                  v-for="lib in jellyfinLibraries"
                  :key="lib.id"
                  :label="`${lib.name} (${lib.collection_type || lib.type || '?'})`"
                  :value="lib.id"
                />
              </el-select>
              <div class="form-hint" style="margin-top: 4px">
                自动监视 / 刮削只在这里勾选的库内生效。一般是你 Jellyfin 里专放成人内容的库。
                <el-button text type="primary" size="small" @click="loadJellyfinLibraries">刷新库列表</el-button>
              </div>
            </el-form-item>
            <el-form-item label="自动监视">
              <el-switch v-model="form.adult.auto_scrape" />
              <span class="form-hint">
                定时轮询 Jellyfin 发现新视频自动入库 + 刮削（默认关闭）。
                走增量 API（MinDateLastSaved），不全库扫；不创建后台任务，仅日志记录
              </span>
            </el-form-item>
            <el-form-item label="轮询间隔">
              <el-input-number
                v-model="form.adult.poll_interval_sec"
                :min="30"
                :max="3600"
                :step="30"
                style="width: 160px"
              />
              <span class="form-hint" style="margin-left: 8px">
                秒。短 → 新文件发现快但请求密；长 → 反之。下限 30s，默认 300s
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
            </div>
          </template>

          <p class="form-hint" style="margin-bottom: 12px">
            按顺序尝试，第一个命中即返回。可拖动 / 上下调优先级。镜像地址由代码常量管理，不在前端配置。
          </p>

          <SourcePool
            v-model="form.adult.sources"
            :all-keys="allSourceKeys"
            :labels="sourceLabels"
            :min-items="1"
          />
        </el-card>

        <!-- 女优库 lazy 构建 -->
        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <el-icon><User /></el-icon>
              <span>女优档案库</span>
              <el-tag
                size="small"
                :type="actressStatus.running ? 'success' : 'info'"
                effect="plain"
              >
                {{ actressStatus.running ? `运行中 · ${actressStatus.current_phase || '...'}` : '未运行' }}
              </el-tag>
            </div>
          </template>

          <p class="form-hint" style="margin-bottom: 12px">
            后台慢爬 javdb 把番号库里出现过的所有女优名字（中/日/英/别名）归一化到同一档案，
            后续可"葵司"="葵つかさ"="Tsukasa Aoi"互相对应。运行期间可随时停，下次再点开始会接着跑。
          </p>

          <el-form label-width="100px" label-position="left">
            <el-form-item label="请求间隔(秒)">
              <el-input-number
                v-model="actressDelay"
                :min="2"
                :max="60"
                :step="1"
                :disabled="actressStatus.running"
                style="width: 140px"
              />
              <span class="form-hint">
                两次请求最小间隔。**保守爬**建议 ≥5s，避免被 javdb 风控。
              </span>
            </el-form-item>
          </el-form>

          <div class="actress-stats">
            <div class="stat-cell">
              <div class="stat-num">{{ actressStatus.total ?? 0 }}</div>
              <div class="stat-label">总数</div>
            </div>
            <div class="stat-cell ok">
              <div class="stat-num">{{ actressStatus.resolved ?? 0 }}</div>
              <div class="stat-label">已解析</div>
            </div>
            <div class="stat-cell pending">
              <div class="stat-num">{{ actressStatus.pending ?? 0 }}</div>
              <div class="stat-label">待解析</div>
            </div>
            <div class="stat-cell miss">
              <div class="stat-num">{{ actressStatus.not_found ?? 0 }}</div>
              <div class="stat-label">未找到</div>
            </div>
          </div>

          <div v-if="actressStatus.running" class="actress-current">
            <el-icon class="spin"><Loading /></el-icon>
            正在解析：<strong>{{ actressStatus.current_query || '(准备中...)' }}</strong>
            <span class="run-counts">
              本轮 +{{ actressStatus.resolved_in_run || 0 }} 解析 ·
              {{ actressStatus.merged_in_run || 0 }} 合并 ·
              {{ actressStatus.not_found_in_run || 0 }} 未找到
            </span>
          </div>

          <div v-if="actressStatus.last_error" class="actress-error">
            <el-icon><Warning /></el-icon> {{ actressStatus.last_error }}
          </div>

          <div class="actress-actions">
            <el-button
              v-if="!actressStatus.running"
              type="primary"
              :icon="VideoPlay"
              @click="startActressBuild"
            >
              开始构建
            </el-button>
            <el-button
              v-else
              type="warning"
              :icon="VideoPause"
              @click="stopActressBuild"
            >
              停止
            </el-button>
            <el-button :icon="Refresh" @click="refreshActressStatus" plain>刷新</el-button>
          </div>
        </el-card>
      </el-tab-pane>

      <!-- ============ 入库流水线 ============ -->
      <el-tab-pane name="dispatch">
        <template #label>
          <div class="tab-label">
            <el-icon><Download /></el-icon>
            <span>入库流水线</span>
          </div>
        </template>

        <!-- 顶部：全局设置 + LLM 识别 双列并排 -->
        <el-row :gutter="12">
          <el-col :span="12">
            <el-card shadow="never" class="cfg-card">
              <template #header>
                <div class="cfg-card-head">
                  <span class="badge dispatch-badge">DISPATCH</span>
                  <span>全局设置</span>
                </div>
              </template>

              <el-form :model="form.dispatch" label-width="140px" size="small" @submit.prevent>
                <el-form-item label="启用流水线">
                  <el-switch v-model="form.dispatch.enabled" />
                  <span class="form-hint">关闭后所有自动转移、孤儿认领暂停</span>
                </el-form-item>

                <el-form-item label="转移模式">
                  <el-radio-group v-model="form.dispatch.default_move_mode">
                    <el-radio value="copy">复制（保种）</el-radio>
                    <el-radio value="move">移动（不保种）</el-radio>
                  </el-radio-group>
                  <span class="form-hint">所有 media_type 统一使用</span>
                </el-form-item>

                <el-form-item label="复制 buffer (MB)">
                  <el-input-number v-model="form.dispatch.copy_buffer_mb" :min="1" :max="64" controls-position="right" />
                </el-form-item>

                <el-form-item label="Sweeper 轮询 (秒)">
                  <el-input-number v-model="form.dispatch.poll_interval_seconds" :min="30" :max="600" controls-position="right" />
                  <span class="form-hint">扫描失败/僵尸任务并重派的兜底周期</span>
                </el-form-item>

                <el-form-item label="下载项认领 (秒)">
                  <el-input-number v-model="form.dispatch.adopt_interval_seconds" :min="60" :max="3600" :step="60" controls-position="right" />
                  <span class="form-hint">认领 qB Web / Jackett RSS 推过来的下载项（流水线之外加进来的种子）</span>
                </el-form-item>

                <el-form-item label="下载目录">
                  <el-input v-model="form.dispatch.download_path" placeholder="X:/downloads 或 /downloads" />
                  <span class="form-hint">
                    <strong>仅用于后端配额监视</strong>（shutil.disk_usage），写**后端能直接 stat 到的路径**。
                    qB 加种子用它自己的默认下载路径，跟这里无关。
                  </span>
                </el-form-item>

                <el-form-item label="垃圾目录">
                  <el-input v-model="form.dispatch.trash_dir" placeholder="/downloads/.trash" />
                  <span class="form-hint">organizer 把 sample/nfo/RARBG.txt 等丢到这里</span>
                </el-form-item>

                <el-form-item label="垃圾清理 (天)">
                  <el-input-number v-model="trashIntervalDays" :min="1" :max="30" :step="1" controls-position="right" />
                  <span class="form-hint">到点清空整个垃圾目录（想保留更久就调大）</span>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>

          <el-col :span="12">
            <el-card shadow="never" class="cfg-card">
              <template #header>
                <div class="cfg-card-head">
                  <span class="badge llm-badge">LLM</span>
                  <span>LLM 媒体识别</span>
                  <el-tag size="small" :type="form.llm.api_key ? 'success' : 'info'" effect="plain">
                    {{ form.llm.api_key ? '已配置' : '未配置' }}
                  </el-tag>
                </div>
              </template>

              <el-form :model="form.llm" label-width="100px" size="small" @submit.prevent>
                <el-form-item label="启用">
                  <el-switch v-model="form.llm.enabled" />
                  <el-button
                    type="primary"
                    size="small"
                    :icon="Aim"
                    :loading="llmTesting"
                    @click="testLlm"
                    style="margin-left: 32px"
                  >测试连接</el-button>
                  <span v-if="llmTestResult" class="llm-test-result" :class="llmTestResult.ok ? 'ok' : 'fail'">
                    {{ llmTestResult.message }}
                  </span>
                </el-form-item>

                <el-form-item label="优先采用 LLM">
                  <el-switch v-model="form.llm.prefer_first" :disabled="!form.llm.enabled" />
                  <span class="form-hint" style="margin-left: 8px">
                    开启后优先用 AI 识别媒体类型；默认是规则识别不行时才用 AI
                  </span>
                </el-form-item>

                <el-form-item label="Provider">
                  <el-select v-model="form.llm.provider" style="width: 100%" @change="onLlmProviderChange">
                    <el-option label="阿里通义千问 (qwen)" value="qwen" />
                    <el-option label="DeepSeek" value="deepseek" />
                    <el-option label="OpenAI" value="openai" />
                    <el-option label="LM Studio（本地）" value="lmstudio" />
                  </el-select>
                </el-form-item>

                <el-form-item label="API Key">
                  <el-input v-model="form.llm.api_key" type="password" show-password placeholder="sk-xxxxxxxx" clearable />
                </el-form-item>

                <el-form-item label="Base URL">
                  <el-input v-model="form.llm.base_url" placeholder="OpenAI 兼容接口地址" />
                </el-form-item>

                <el-form-item label="模型">
                  <el-input v-model="form.llm.model" placeholder="如 qwen-plus / gpt-4o-mini" />
                </el-form-item>

                <el-form-item label="超时(秒)">
                  <el-input-number v-model="form.llm.timeout_seconds" :min="10" :max="600" :step="10" controls-position="right" />
                  <span class="form-hint" style="margin-left: 8px">单次调用上限，长 prompt / reasoning 模型建议 ≥ 120s</span>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
        </el-row>

        <!-- 各类型规则：每行 2 列 -->
        <el-card shadow="never" class="cfg-card" style="margin-top: 12px">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge dispatch-badge">RULES</span>
              <span>各类型的目标库 + 路径模板</span>
              <el-tag size="small" :type="dispatchAllConfigured ? 'success' : 'warning'" effect="plain">
                {{ dispatchAllConfigured ? '已全部配置' : '部分未配置' }}
              </el-tag>
            </div>
          </template>

          <p class="form-hint" style="margin-bottom: 12px">
            未配置 library 的类型，自动认领（adopt）会落到「待审核」队列让用户人工选；
            手动添加种子时会显示库选择下拉，不影响。
          </p>

          <el-row :gutter="12">
            <el-col v-for="mt in dispatchMediaTypes" :key="mt.key" :span="12">
              <div class="dispatch-rule-row">
                <div class="dispatch-rule-head">
                  <el-icon class="mt-icon" :class="`mt-${mt.key}`"><component :is="mt.icon" /></el-icon>
                  <span class="mt-label">{{ mt.label }}</span>
                  <span class="mt-tag">{{ mt.key }}</span>
                  <el-tag
                    size="small"
                    :type="form.dispatch.rules[mt.key]?.library_id ? 'success' : 'warning'"
                    effect="plain"
                  >
                    {{ form.dispatch.rules[mt.key]?.library_id ? '已选库' : '未选库' }}
                  </el-tag>
                </div>

                <el-form :model="form.dispatch.rules[mt.key]" label-width="80px" size="small" @submit.prevent>
                  <el-form-item label="目标库">
                    <el-select
                      v-model="form.dispatch.rules[mt.key].library_id"
                      placeholder="选 Jellyfin 库"
                      filterable
                      clearable
                      style="width: 100%"
                    >
                      <el-option
                        v-for="lib in jellyfinLibraries"
                        :key="lib.id"
                        :label="`${lib.name} (${lib.collection_type || '?'})`"
                        :value="lib.id"
                      />
                    </el-select>
                  </el-form-item>

                  <el-form-item label="路径模板">
                    <el-input v-model="form.dispatch.rules[mt.key].location_template" />
                    <span class="form-hint default-hint">目录默认：{{ DISPATCH_DEFAULT_LOCATION[mt.key] }}</span>
                  </el-form-item>

                  <el-form-item label="文件模板">
                    <el-input v-model="form.dispatch.rules[mt.key].file_template" />
                    <span class="form-hint default-hint">文件默认：{{ DISPATCH_DEFAULT_FILE[mt.key] }}（不含扩展名）</span>
                  </el-form-item>

                  <el-form-item label="去重策略">
                    <el-select v-model="form.dispatch.rules[mt.key].duplicate_policy" style="width: 100%">
                      <el-option label="质量高的留下（旧的进回收站）"    value="higher_quality_wins" />
                      <el-option label="永远保留旧版本，丢掉新的"        value="always_skip" />
                      <el-option label="永远用新的覆盖（旧的进回收站）"  value="always_replace" />
                      <el-option label="先放着等我手动选"                value="needs_review" />
                    </el-select>
                    <span class="form-hint default-hint">下载的影片跟库里已有的重复时怎么办</span>
                  </el-form-item>
                </el-form>
              </div>
            </el-col>
          </el-row>

          <!-- 模板变量速查 + 库列表刷新按钮 -->
          <div class="template-vars-hint">
            <strong>模板变量</strong>
            <code>{library_root}</code>
            <code>{title}</code>
            <code>{year}</code>
            <code>{series_name}</code>
            <code>{anime_name}</code>
            <code>{season:02d}</code>
            <code>{episode:02d}</code>
            <code>{code}</code>
          </div>
        </el-card>
      </el-tab-pane>

      <!-- ============ 日志 ============ -->
      <el-tab-pane name="logs">
        <template #label>
          <div class="tab-label">
            <el-icon><Document /></el-icon>
            <span>日志</span>
          </div>
        </template>

        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <span class="badge logs">LOG</span>
              <span>后端日志（{{ logsState.file || '...' }}）</span>
              <el-tag size="small" type="info" effect="plain">
                root level: {{ logsState.level || '?' }}
              </el-tag>
            </div>
          </template>

          <div class="logs-toolbar">
            <el-form-item label="日志级别" label-width="100px" style="margin-bottom: 0">
              <el-select v-model="logsLevelChoice" size="small" style="width: 130px" @change="onLogLevelChange">
                <el-option label="DEBUG" value="DEBUG" />
                <el-option label="INFO" value="INFO" />
                <el-option label="WARNING" value="WARNING" />
                <el-option label="ERROR" value="ERROR" />
                <el-option label="CRITICAL" value="CRITICAL" />
              </el-select>
              <span class="form-hint">
                修改即生效；重启后端将回到默认 INFO
              </span>
            </el-form-item>

            <el-form-item label="过滤显示" label-width="100px" style="margin-bottom: 0">
              <el-select v-model="logsViewLevel" size="small" style="width: 130px" @change="loadLogs">
                <el-option label="全部" value="" />
                <el-option label="DEBUG+" value="DEBUG" />
                <el-option label="INFO+" value="INFO" />
                <el-option label="WARNING+" value="WARNING" />
                <el-option label="ERROR+" value="ERROR" />
              </el-select>
              <span class="form-hint">
                只过滤显示，不影响后端记录
              </span>
            </el-form-item>

            <el-form-item label="行数" label-width="60px" style="margin-bottom: 0">
              <el-input-number v-model="logsLines" :min="50" :max="5000" :step="100" size="small" @change="loadLogs" />
            </el-form-item>

            <div class="logs-actions">
              <el-switch v-model="logsAutoRefresh" active-text="自动刷新(3s)" />
              <el-button size="small" :icon="Refresh" @click="loadLogs" :loading="logsLoading">手动刷新</el-button>
              <el-button size="small" :icon="Download" @click="downloadLogs" :disabled="!logsContent">下载日志</el-button>
            </div>
          </div>

          <div class="logs-meta" v-if="logsState.size_bytes">
            文件大小: {{ formatBytes(logsState.size_bytes) }} ·
            显示 {{ logsState.count }} 行
          </div>

          <pre ref="logsViewer" class="logs-viewer" v-loading="logsLoading">{{ logsContent || '（无日志）' }}</pre>
        </el-card>

        <!-- ---- 下载日志 ---- -->
      </el-tab-pane>

      <!-- ============ 用户管理 ============ -->
      <el-tab-pane name="users" v-if="isAdmin">
        <template #label>
          <div class="tab-label">
            <el-icon><User /></el-icon>
            <span>用户管理</span>
          </div>
        </template>

        <el-card shadow="never" class="cfg-card">
          <template #header>
            <div class="cfg-card-head">
              <el-icon><User /></el-icon>
              <span>用户列表</span>
              <el-button size="small" type="primary" :icon="Plus" @click="showAddUser = true">
                添加用户
              </el-button>
            </div>
          </template>

          <el-table :data="userList" v-loading="usersLoading" stripe>
            <el-table-column prop="username" label="用户名" width="180" />
            <el-table-column prop="role" label="角色" width="120">
              <template #default="{ row }">
                <el-tag :type="row.role === 'admin' ? 'danger' : 'info'" size="small">
                  {{ row.role === 'admin' ? '管理员' : '访客' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="创建时间" width="180">
              <template #default="{ row }">
                {{ new Date(row.created_at).toLocaleString('zh-CN') }}
              </template>
            </el-table-column>
            <el-table-column label="操作" min-width="200">
              <template #default="{ row }">
                <el-button size="small" @click="openChangePassword(row)">改密码</el-button>
                <el-button
                  size="small"
                  type="danger"
                  :disabled="row.username === currentUser.username"
                  @click="handleDeleteUser(row)"
                >
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <!-- 添加用户对话框 -->
        <el-dialog v-model="showAddUser" title="添加用户" width="400px" destroy-on-close>
          <el-form :model="newUserForm" label-width="80px">
            <el-form-item label="用户名">
              <el-input v-model="newUserForm.username" placeholder="请输入用户名" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="newUserForm.password" type="password" show-password placeholder="请输入密码" />
            </el-form-item>
            <el-form-item label="角色">
              <el-radio-group v-model="newUserForm.role">
                <el-radio value="admin">管理员</el-radio>
                <el-radio value="guest">访客</el-radio>
              </el-radio-group>
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="showAddUser = false">取消</el-button>
            <el-button type="primary" :loading="addingUser" @click="handleAddUser">确定</el-button>
          </template>
        </el-dialog>

        <!-- 修改密码对话框 -->
        <el-dialog v-model="showChangePassword" title="修改密码" width="400px" destroy-on-close>
          <el-form label-width="80px">
            <el-form-item label="用户">
              <el-input :model-value="changePwdTarget?.username" disabled />
            </el-form-item>
            <el-form-item label="新密码">
              <el-input v-model="newPassword" type="password" show-password placeholder="请输入新密码" />
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="showChangePassword = false">取消</el-button>
            <el-button type="primary" :loading="changingPwd" @click="handleChangePassword">确定</el-button>
          </template>
        </el-dialog>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import {
  Refresh, Check, Connection, Link, Lock,
  Files, Plus, Delete, ArrowUp, ArrowDown, Right,
  Document, User, Loading, VideoPlay, VideoPause, Warning,
  Aim, Download, Monitor,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { configApi, adultApi, logsApi, jellyfinApi, authApi, diagnosticsApi } from '@/api'
import SourcePool from '@/components/SourcePool.vue'

// 默认 tab 设为「可用性检测」（位列第一，进入页面立即跑本地零成本检查）
const activeTab = ref('diagnostics')
const loading = ref(false)
const saving = ref(false)

// Jellyfin API key 权限检查
const checkingApiKey = ref(false)
const apiKeyCheckResult = ref(null)
const onCheckApiKey = async () => {
  checkingApiKey.value = true
  apiKeyCheckResult.value = null
  try {
    const r = await jellyfinApi.checkApiKey()
    apiKeyCheckResult.value = r.data || {}
    if (r.data?.ok) {
      ElMessage.success('Jellyfin 管理员权限 OK')
    } else {
      ElMessage.warning(r.data?.message || '权限检查失败')
    }
  } catch (e) {
    apiKeyCheckResult.value = {
      ok: false,
      message: e.response?.data?.detail || e.message || '请求失败',
    }
    ElMessage.error('检查失败：' + apiKeyCheckResult.value.message)
  } finally {
    checkingApiKey.value = false
  }
}

// 垃圾清理周期：UI 用"天"输入，db/config 仍用秒存储
const trashIntervalDays = computed({
  get: () => Math.max(1, Math.round((form.dispatch?.trash_interval_seconds || 86400) / 86400)),
  set: (days) => {
    form.dispatch.trash_interval_seconds = Math.max(1, Number(days || 1)) * 86400
  },
})

// 脏数据相关变量（form 在下面定义，watch 注册延后到 form 定义之后）
const dirty = ref(false)
let initialSnapshot = ''
let watchStarted = false

// 成人刮削源：UI 标签直接用域名（移除自定义镜像编辑，base_url 由后端常量决定）
const sourceLabels = {
  missav:     'missav.ai/cn/',
  javbus:     'javbus.com',
  javdb:      'javdb.com',
  javlibrary: 'javlibrary.com/cn/',
  avbase:     'avbase.net',
}
const allSourceKeys = Object.keys(sourceLabels)

// 字幕下载源
const subtitleSourceLabels = {
  assrt: '射手网 (assrt.net)',
  opensubtitles: 'OpenSubtitles',
  shooter: 'Shooter (shooter.cn / hash 命中)',
}
const allSubtitleSourceKeys = Object.keys(subtitleSourceLabels)

// 字幕格式偏好（用 SourcePool 渲染，行为同下载源排序）
const subtitleFormatLabels = {
  ass: 'ASS / SSA（带样式，首选）',
  srt: 'SRT（通用兼容）',
  sup: 'SUP（蓝光图形字幕）',
  vtt: 'VTT（WebVTT）',
}
const allSubtitleFormatKeys = Object.keys(subtitleFormatLabels)

// 字幕自动下载 - 语言优先级（SourcePool；含双语复合）
// 跟"缺字幕判定"分开：缺判只看原子 chs/cht/eng/jpn/kor；下载用这个排序池决定抓什么、什么优先
const downloadingLangLabels = {
  'chs.eng': '简英双语',
  'cht.eng': '繁英双语',
  'chs':     '简体中文',
  'cht':     '繁体中文',
  'eng':     '英语',
  'jpn':     '日语',
  'kor':     '韩语',
}
const allDownloadingLangKeys = Object.keys(downloadingLangLabels)

// 字幕语言需求 checkbox：禁止全部取消（缺字幕统计依赖至少 1 个）
// 用户点最后一个想取消时，立即回填 + 警告
let _lastRequiredLangs = ['chs', 'eng']
const onRequiredLangsChange = (val) => {
  if (!Array.isArray(val) || val.length === 0) {
    ElMessage.warning('字幕语言需求至少选择 1 个')
    // 回填到上一次的非空状态
    form.subtitle.required_langs = [..._lastRequiredLangs]
    return
  }
  _lastRequiredLangs = [...val]
}

// 入库流水线 - 各 media_type 的规则卡片元数据（标签 + 图标）
// 各 media_type 卡片元数据。纪录片不独立成 media_type，按 TMDB 设计走 movie/tv + genre 99
const dispatchMediaTypes = [
  { key: 'movie', label: '电影', icon: VideoPlay },
  { key: 'tv',    label: '剧集', icon: Document },
  { key: 'anime', label: '动漫', icon: Files },
  { key: 'adult', label: '成人', icon: Lock },
]

// 路径模板默认值（跟 blank() / mergeIntoForm rulesDefaults 同步；
// 也跟后端 config_models.py DispatchConfig.rules 默认一致）
// 拆成目录 + 文件名 两套，分别对应 location_template / file_template
const DISPATCH_DEFAULT_LOCATION = {
  movie: '{library_root}/{title} ({year})',
  tv:    '{library_root}/{series_name}/Season {season:02d}',
  anime: '{library_root}/{anime_name}',
  adult: '{library_root}/{code}',
}
const DISPATCH_DEFAULT_FILE = {
  movie: '{title} ({year})',
  tv:    '({series_name})S{season:02d}E{episode:02d}',
  anime: '({anime_name}){episode:03d}',
  adult: '{code}({title})',
}

const blank = () => ({
  server: { backend_port: 8099, frontend_port: 5173 },
  jellyfin: { host: '', external_url: '', api_key: '', db_path: '' },
  tmdb: { api_key: '', language: 'zh-CN' },
  subtitle: {
    opensubtitles_api_key: '',
    opensubtitles_username: '',
    opensubtitles_password: '',
    // 缺字幕判定（原子单语言 checkbox；任一缺即视为缺字幕）
    required_langs: ['chs', 'eng'],
    // 自动下载语言优先级（SourcePool 排序池；可含双语复合 chs.eng / cht.eng）
    downloading_langs: [
      { name: 'chs.eng' }, { name: 'chs' }, { name: 'eng' },
    ],
    assrt_api_token: '',
    sources: [
      { name: 'assrt', enabled: true },
      { name: 'opensubtitles', enabled: true },
    ],
    // 字幕格式偏好（SourcePool 复用：用 [{name}] 形式存储，submit 时拍平）
    preferred_formats: [
      { name: 'ass' }, { name: 'srt' }, { name: 'sup' },
    ],
  },
  jackett: { host: '', api_key: '', search_keywords: [], default_keywords: '' },
  qbittorrent: { host: '', username: '', password: '', api_key: '' },
  // 外部命令行工具路径（启动时注入到 PATH 前缀）
  tools: {
    ffmpeg_dir: '',
    mkvtoolnix_dir: '',
  },
  // LLM 媒体识别配置
  llm: {
    enabled: false,
    provider: 'qwen',
    api_key: '',
    model: 'qwen-plus',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    timeout_seconds: 180,
    max_retries: 1,
    cache_ttl_days: 30,
    prefer_first: false,
  },
  // 入库流水线（每 media_type 仅配 library_id + location_template；
  // file_template 由 organizer 内置；move_mode 由全局 default_move_mode 统一）
  dispatch: {
    enabled: true,
    download_path: '/download',
    poll_interval_seconds: 30,
    adopt_interval_seconds: 300,
    trash_interval_seconds: 86400,
    worker_concurrency: 1,
    copy_buffer_mb: 8,
    default_move_mode: 'copy',
    trash_dir: '/downloads/.trash',
    rules: {
      movie: { library_id: '', location_template: '{library_root}/{title} ({year})',                  file_template: '{title} ({year})',                       duplicate_policy: 'higher_quality_wins' },
      tv:    { library_id: '', location_template: '{library_root}/{series_name}/Season {season:02d}', file_template: '({series_name})S{season:02d}E{episode:02d}', duplicate_policy: 'higher_quality_wins' },
      anime: { library_id: '', location_template: '{library_root}/{anime_name}',                     file_template: '({anime_name}){episode:03d}',            duplicate_policy: 'higher_quality_wins' },
      adult: { library_id: '', location_template: '{library_root}/{code}',                            file_template: '{code}({title})',                        duplicate_policy: 'always_skip' },
    },
  },
  database: { host: '', port: 5432, name: '', user: '', password: '' },
  path_mappings: { enabled: false, rules: [] },
  debug: { show_dry_run_in_toolbar: false },
  cache: {
    tmdb_minutes: 120,
    library_days: 7,
  },
  mdblist: { enabled: true, api_key: '', cache_ttl_days: 30 },
  douban: {
    enabled: true, cache_ttl_days: 30,
  },
  // 媒体元数据实体表 (L3 长缓存) + 全局元数据语言
  // 详见 docs/2026-05-15-media-metadata-store.md
  // 注：豆瓣 / MDB List 无语言选择（爬虫源单语，自动 fallback 处理）
  metadata: {
    store_douban_full: true,
    lru_keep_days: 365,
    refresh_ttl_days: 30,
    scrape_language: 'en',
    display_language: 'en',
  },
  // 第三方推荐源（互补 TMDB）
  trakt: {
    enabled: false,
    client_id: '',
    base_url: 'https://api.trakt.tv',
    cache_minutes: 60,
  },
  anilist: {
    enabled: true,
    base_url: 'https://graphql.anilist.co',
    cache_minutes: 240,
  },
  douban_lists: {
    enabled: true,
    cache_days: 3,
    lists: [
      { name: '豆瓣 Top 250', doulist_id: '240962',  media_type: 'movie' },
      { name: '高分华语电影', doulist_id: '1518184', media_type: 'movie' },
      { name: '高分日剧',     doulist_id: '1631879', media_type: 'tv' },
      { name: '高分韩剧',     doulist_id: '1648104', media_type: 'tv' },
    ],
  },
  wikidata: {
    enabled: true,
    user_agent: 'JellyfinHelper/1.0',
    language_order: ['zh', 'en'],
  },
  adult: {
    enabled: false,
    library_ids: [],
    auto_detect: true,
    auto_scrape: false,
    poll_interval_sec: 300,
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
  // 清洗：把已下线/未知的源（如老配置里残留的 avmoo）过滤掉，避免显示空名行
  form.adult.sources = form.adult.sources.filter(s => allSourceKeys.includes(s.name))
  if (!Array.isArray(form.adult.library_ids)) form.adult.library_ids = []
  if (!Array.isArray(form.jackett.search_keywords)) form.jackett.search_keywords = []
  // 后端 preferred_formats 是 string[]，前端 SourcePool 要 [{name}]，做一次归一化
  const rawFormats = form.subtitle.preferred_formats
  if (Array.isArray(rawFormats) && rawFormats.length && typeof rawFormats[0] === 'string') {
    form.subtitle.preferred_formats = rawFormats.map(f => ({ name: f }))
  } else if (!Array.isArray(rawFormats) || !rawFormats.length) {
    form.subtitle.preferred_formats = [
      { name: 'ass' }, { name: 'srt' }, { name: 'sup' },
    ]
  }
  // 后端 downloading_langs 也是 string[]，前端 SourcePool 同样要 [{name}]
  const rawDLangs = form.subtitle.downloading_langs
  if (Array.isArray(rawDLangs) && rawDLangs.length && typeof rawDLangs[0] === 'string') {
    form.subtitle.downloading_langs = rawDLangs.map(s => ({ name: s }))
  } else if (!Array.isArray(rawDLangs) || !rawDLangs.length) {
    form.subtitle.downloading_langs = [
      { name: 'chs.eng' }, { name: 'chs' }, { name: 'eng' },
    ]
  }
  // required_langs 是 string[]，UI 上是 checkbox-group，类型本来就匹配
  // 空数组 / 非数组都兜底成 ['chs', 'eng']（缺字幕统计依赖至少 1 个）
  if (!Array.isArray(form.subtitle.required_langs) || form.subtitle.required_langs.length === 0) {
    form.subtitle.required_langs = ['chs', 'eng']
  }
  // 同步"上一次合法值"快照，给 onRequiredLangsChange 做回填用
  _lastRequiredLangs = [...form.subtitle.required_langs]
  if (!Array.isArray(form.subtitle.sources)) {
    // 旧 config 没 sources 字段时给个默认值，保证页面有内容
    form.subtitle.sources = [
      { name: 'assrt', enabled: true },
      { name: 'opensubtitles', enabled: true },
    ]
  }
  // dispatch.rules 兜底：用户 yaml 里若只配了部分 media_type，要保证全部 key 都在，
  // 否则 mergeIntoForm 整体替换 rules dict 会让缺失的 media_type 在 UI 上消失
  const rulesDefaults = {
    movie: { library_id: '', location_template: '{library_root}/{title} ({year})',                  file_template: '{title} ({year})',                       duplicate_policy: 'higher_quality_wins' },
    tv:    { library_id: '', location_template: '{library_root}/{series_name}/Season {season:02d}', file_template: '({series_name})S{season:02d}E{episode:02d}', duplicate_policy: 'higher_quality_wins' },
    anime: { library_id: '', location_template: '{library_root}/{anime_name}',                     file_template: '({anime_name}){episode:03d}',            duplicate_policy: 'higher_quality_wins' },
    adult: { library_id: '', location_template: '{library_root}/{code}',                            file_template: '{code}({title})',                        duplicate_policy: 'always_skip' },
  }
  if (!form.dispatch.rules || typeof form.dispatch.rules !== 'object') {
    form.dispatch.rules = rulesDefaults
  } else {
    for (const mt of Object.keys(rulesDefaults)) {
      // 剥掉历史遗留的 move_mode（schema 已删，全局统一）；保留 file_template / location_template
      const incoming = form.dispatch.rules[mt] || {}
      const { move_mode, ...rest } = incoming
      form.dispatch.rules[mt] = { ...rulesDefaults[mt], ...rest }
    }
  }
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

  // 校验：以下列表都必须至少 1 项，空集合会让对应功能完全失能
  const _len = (v) => Array.isArray(v) ? v.length : 0
  if (_len(form.subtitle.required_langs) === 0) {
    ElMessage.warning('字幕语言需求至少选择 1 个')
    return
  }
  if (_len(form.subtitle.downloading_langs) === 0) {
    ElMessage.warning('字幕自动下载语言优先级至少保留 1 个')
    return
  }
  if (_len(form.subtitle.preferred_formats) === 0) {
    ElMessage.warning('字幕格式偏好至少保留 1 个')
    return
  }
  if (_len(form.subtitle.sources) === 0) {
    ElMessage.warning('字幕下载源至少保留 1 个')
    return
  }
  if (_len(form.adult.sources) === 0) {
    ElMessage.warning('成人内容源至少保留 1 个')
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
    // 清理 sources：UI 不再支持自定义 base_url，保存时只保留 name + enabled
    // 即便老 config.yaml 里残留 base_url 字段，下次保存也会被清掉（镜像地址由代码常量管理）
    const cleanedAdult = {
      ...form.adult,
      sources: form.adult.sources
        .filter(s => s.name)
        .map(s => ({ name: s.name, enabled: !!s.enabled })),
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
    // 清理 subtitle.sources：只保留 name + enabled，过滤无 name 项
    // preferred_formats / downloading_langs：UI 用 [{name}] 形式，submit 时拍平为 string[]
    const cleanedSubtitle = {
      ...form.subtitle,
      sources: (form.subtitle.sources || [])
        .filter(s => s.name)
        .map(s => ({ name: s.name, enabled: !!s.enabled })),
      preferred_formats: (form.subtitle.preferred_formats || [])
        .map(f => (typeof f === 'string' ? f : f?.name))
        .filter(Boolean),
      downloading_langs: (form.subtitle.downloading_langs || [])
        .map(s => (typeof s === 'string' ? s : s?.name))
        .filter(Boolean),
      // required_langs 在 UI 上已经是 string[]（checkbox-group 直接绑），无需转换
    }
    const payload = { ...form, adult: cleanedAdult, jackett: cleanedJackett, subtitle: cleanedSubtitle }
    await configApi.saveFull(payload)
    ElMessage.success('配置已保存')
    await loadConfig()
  } catch (e) {
    console.error('保存失败', e)
  } finally {
    saving.value = false
  }
}

// ---- 女优库构建状态 ----
const actressStatus = ref({})
const actressDelay = ref(5)
let actressPollTimer = null

const refreshActressStatus = async () => {
  try {
    const r = await adultApi.actressBuildStatus()
    actressStatus.value = r.data || {}
    if (actressStatus.value.request_delay) {
      actressDelay.value = actressStatus.value.request_delay
    }
  } catch (e) {
    // 后端没起 / 没启用 adult 模块时，保持原状不报错
  }
}
const startActressBuild = async () => {
  try {
    await adultApi.actressBuildStart(actressDelay.value)
    ElMessage.success('女优库构建已启动，后台慢慢爬中')
    refreshActressStatus()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '启动失败')
  }
}
const stopActressBuild = async () => {
  try {
    await adultApi.actressBuildStop()
    ElMessage.info('已请求停止；线程会在当前 query 完成后退出')
    refreshActressStatus()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '停止失败')
  }
}

// ============================================================================
// 日志查看 tab
// ============================================================================
const logsState = reactive({ file: '', size_bytes: 0, count: 0, level: '' })
const logsContent = ref('')
const logsLines = ref(500)
const logsViewLevel = ref('')         // 前端过滤显示的级别（不影响后端）
const logsLevelChoice = ref('INFO')   // 后端 root logger 实际级别
const logsLoading = ref(false)
const logsAutoRefresh = ref(false)
const logsViewer = ref(null)
let logsPollTimer = null


const downloadLogs = () => {
  if (!logsContent.value) return
  const blob = new Blob([logsContent.value], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  const now = new Date()
  const ts = `${now.getFullYear()}${String(now.getMonth()+1).padStart(2,'0')}${String(now.getDate()).padStart(2,'0')}_${String(now.getHours()).padStart(2,'0')}${String(now.getMinutes()).padStart(2,'0')}`
  a.download = `jellyfin-helper_${ts}.log`
  a.click()
  URL.revokeObjectURL(url)
}

const loadLogs = async () => {
  logsLoading.value = true
  try {
    const r = await logsApi.tail(logsLines.value, logsViewLevel.value)
    logsState.file = r.data.file
    logsState.size_bytes = r.data.size_bytes || 0
    logsState.count = r.data.count || 0
    logsContent.value = (r.data.lines || []).join('')
    // 滚到底部（看最新日志）
    nextTick(() => {
      const el = logsViewer.value
      if (el) el.scrollTop = el.scrollHeight
    })
  } catch (e) {
    ElMessage.error('日志加载失败：' + (e.response?.data?.detail || e.message))
  } finally {
    logsLoading.value = false
  }
}

const refreshLogLevel = async () => {
  try {
    const r = await logsApi.getLevel()
    logsState.level = r.data.root_level
    logsLevelChoice.value = r.data.root_level
  } catch {}
}

// ---- LLM 测试 / 切换 provider 时填默认 base_url + model ----
const llmTesting = ref(false)
const llmTestResult = ref(null)

const _LLM_PROVIDER_DEFAULTS = {
  qwen: {
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    model: 'qwen-plus',
  },
  deepseek: {
    base_url: 'https://api.deepseek.com/v1',
    model: 'deepseek-chat',
  },
  openai: {
    base_url: 'https://api.openai.com/v1',
    model: 'gpt-4o-mini',
  },
  lmstudio: {
    // LM Studio 本地默认 OpenAI 兼容端点；model 留空让用户填实际加载的模型名
    // （LM Studio 启动时会显示 model id，比如 'qwen2.5-7b-instruct'）
    base_url: 'http://localhost:1234/v1',
    model: '',
  },
}

const onLlmProviderChange = (provider) => {
  const defaults = _LLM_PROVIDER_DEFAULTS[provider]
  if (!defaults) return
  // 若用户没填或填的是别的 provider 默认值 → 自动切换
  const allDefaults = Object.values(_LLM_PROVIDER_DEFAULTS)
  if (!form.llm.base_url || allDefaults.some(d => d.base_url === form.llm.base_url)) {
    form.llm.base_url = defaults.base_url
  }
  if (!form.llm.model || allDefaults.some(d => d.model === form.llm.model)) {
    form.llm.model = defaults.model
  }
}

const testLlm = async () => {
  if (!form.llm.api_key) {
    llmTestResult.value = { ok: false, message: '请先填 API Key' }
    return
  }
  llmTesting.value = true
  llmTestResult.value = null
  try {
    // 简单方案：用一个真实种子名调一次 LLM，确认通路
    // 直接 fetch LLM 接口（OpenAI 兼容 chat/completions），10s 超时
    const r = await fetch(form.llm.base_url.replace(/\/$/, '') + '/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${form.llm.api_key}`,
      },
      body: JSON.stringify({
        model: form.llm.model,
        messages: [
          { role: 'user', content: 'reply with json {"ok":true}' },
        ],
        response_format: { type: 'json_object' },
        temperature: 0,
      }),
      signal: AbortSignal.timeout((form.llm.timeout_seconds || 10) * 1000),
    })
    if (!r.ok) {
      const txt = await r.text()
      llmTestResult.value = { ok: false, message: `HTTP ${r.status}: ${txt.slice(0, 200)}` }
      return
    }
    const data = await r.json()
    const content = data?.choices?.[0]?.message?.content || '(no content)'
    llmTestResult.value = {
      ok: true,
      message: `✓ 连接 OK，模型 ${data.model || form.llm.model}，返回: ${String(content).slice(0, 80)}`,
    }
  } catch (e) {
    llmTestResult.value = { ok: false, message: '连接失败: ' + (e.message || String(e)) }
  } finally {
    llmTesting.value = false
  }
}

const onLogLevelChange = async (lvl) => {
  try {
    await logsApi.setLevel(lvl)
    logsState.level = lvl
    ElMessage.success(`日志级别已改为 ${lvl}（重启后端会失效）`)
  } catch (e) {
    ElMessage.error('修改失败：' + (e.response?.data?.detail || e.message))
    refreshLogLevel()  // 同步真实状态
  }
}

watch(logsAutoRefresh, (v) => {
  if (logsPollTimer) {
    clearInterval(logsPollTimer)
    logsPollTimer = null
  }
  if (v) {
    logsPollTimer = setInterval(loadLogs, 3000)
  }
})

// 切到 logs tab 时首次加载
watch(activeTab, (v) => {
  if (v === 'logs') {
    refreshLogLevel()
    loadLogs()
  }
  if (v === 'diagnostics') {
    if (!diagSystemItems.value.length) loadDiagnosticsSystem()
    if (!diagServices.value.length) loadDiagnosticsServices()
    // 核心服务自动跑（jellyfin / qb / jackett）。已有结果不重跑，点测试按钮可手动刷新
    const coreKeys = ['core/jellyfin', 'download/qbittorrent', 'download/jackett']
    if (coreKeys.every(k => !diagServiceResults[k])) {
      runDiagnosticsCore()
    }
  }
})

// ============================================================================
// 可用性检测 tab
// ============================================================================
const diagSystemItems = ref([])
const diagSystemLoading = ref(false)
const diagServices = ref([])             // 后端返回的服务列表（含 enabled）
const diagServiceResults = reactive({})  // { 'group/name': { status, message, elapsed_ms, ... } }
const diagItemLoading = reactive({})     // { 'group/name': true/false }
const diagBatchLoading = reactive({})    // { groupKey: true/false }

const diagStatusLabel = (s) => ({
  ok: '正常',
  fail: '失败',
  not_configured: '未配置',
}[s] || s || '?')

const diagTagType = (s) => ({
  ok: 'success',
  fail: 'danger',
  not_configured: 'info',
}[s] || 'info')

const loadDiagnosticsSystem = async () => {
  diagSystemLoading.value = true
  try {
    const r = await diagnosticsApi.system()
    diagSystemItems.value = r.data.items || []
  } catch (e) {
    ElMessage.error('加载本地环境检测失败: ' + (e?.message || e))
  } finally {
    diagSystemLoading.value = false
  }
}

const loadDiagnosticsServices = async () => {
  try {
    const r = await diagnosticsApi.services()
    diagServices.value = r.data.items || []
  } catch (e) {
    ElMessage.error('加载服务列表失败: ' + (e?.message || e))
  }
}

// 后端 group 保留语义分类（core / download / ...），UI 合并展示：
// core + download 共属"核心服务 & 下载链路"一段
const _GROUP_DISPLAY_MAP = {
  core:     'core',
  download: 'core',
  metadata: 'metadata',
  subtitle: 'subtitle',
  adult:    'adult',
}

// 按组组织 + 给每行注入 result/loading 响应式字段
const diagServiceGroups = computed(() => {
  const meta = {
    core:     { title: '核心服务 & 下载链路',      badge: 'CORE', badgeClass: 'qbit',    hint: 'Jellyfin · qBittorrent · Jackett' },
    metadata: { title: '元数据 / 评分 / 推荐源',   badge: 'META', badgeClass: 'tmdb',    hint: '按已启用项测试' },
    subtitle: { title: '字幕源',                   badge: 'SUB',  badgeClass: 'assrt',   hint: '按已启用项测试' },
    adult:    { title: '成人刮削源',               badge: '18+',  badgeClass: 'mdblist', hint: '逐站可达性' },
  }
  const groups = {}
  for (const it of diagServices.value) {
    const key = `${it.group}/${it.name}`
    const row = {
      ...it,
      result: diagServiceResults[key] || null,
      loading: !!diagItemLoading[key],
    }
    const displayKey = _GROUP_DISPLAY_MAP[it.group] || it.group
    if (!groups[displayKey]) {
      groups[displayKey] = { key: displayKey, items: [], ...meta[displayKey] }
    }
    groups[displayKey].items.push(row)
  }
  return Object.values(groups)
})

const runDiagnosticsItem = async (row) => {
  const key = `${row.group}/${row.name}`
  diagItemLoading[key] = true
  try {
    const r = await diagnosticsApi.check(row.group, row.name)
    diagServiceResults[key] = r.data
  } catch (e) {
    diagServiceResults[key] = {
      status: 'fail',
      message: e?.response?.data?.detail || e?.message || String(e),
      elapsed_ms: 0,
    }
  } finally {
    diagItemLoading[key] = false
  }
}

const runDiagnosticsGroup = async (group) => {
  diagBatchLoading[group.key] = true
  try {
    // 串行跑（避免同时打太多外部请求）
    for (const row of group.items) {
      if (!row.enabled) continue
      await runDiagnosticsItem(row)
    }
  } finally {
    diagBatchLoading[group.key] = false
  }
}

// 核心服务自动检测：jellyfin / qBittorrent / Jackett 都是用户自己的局域网服务，
// 不像第三方源会限流 / 触发反爬 —— 进入诊断页就并行跑一次，结果立即可见
// 第三方源（TMDB / 豆瓣 / 字幕源 / 成人源）继续手动按钮触发
const _AUTO_CHECK_TARGETS = [
  { group: 'core',     name: 'jellyfin' },
  { group: 'download', name: 'qbittorrent' },
  { group: 'download', name: 'jackett' },
]

const runDiagnosticsCore = async () => {
  // 等服务列表先加载完，否则 enabled 标记拿不到（不强依赖，但有了 enabled 检查更友好）
  if (!diagServices.value.length) {
    await loadDiagnosticsServices()
  }
  // 并行跑（局域网请求，毫秒级；任意一个挂死不会拖累其他两个）
  await Promise.all(
    _AUTO_CHECK_TARGETS.map(({ group, name }) => {
      const enabledMap = Object.fromEntries(
        diagServices.value.map(it => [`${it.group}/${it.name}`, it.enabled])
      )
      // 未配置的跳过（避免无意义请求）
      if (!enabledMap[`${group}/${name}`]) return Promise.resolve()
      return runDiagnosticsItem({ group, name })
    })
  )
}

// 字节人类化格式（与日志元信息显示用）
const formatBytes = (b) => {
  if (!b) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB']
  let i = 0
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++ }
  return `${b.toFixed(i ? 1 : 0)} ${u[i]}`
}

// ---- Jellyfin 库列表（流水线 tab 用） ----
const jellyfinLibraries = ref([])
const librariesLoading = ref(false)
const loadJellyfinLibraries = async () => {
  librariesLoading.value = true
  try {
    const r = await jellyfinApi.libraries(false)
    jellyfinLibraries.value = r.data?.libraries || []
  } catch (e) {
    console.warn('加载 jellyfin 库列表失败', e)
    ElMessage.warning('加载 Jellyfin 库列表失败：' + (e.response?.data?.detail || e.message))
  } finally {
    librariesLoading.value = false
  }
}

// 已配 library_id 的 media_type 数 / 全部 → 显示徽章状态
const dispatchAllConfigured = computed(() => {
  return dispatchMediaTypes.every(mt => !!form.dispatch.rules[mt.key]?.library_id)
})

// ==================== 用户管理 ====================
const currentUser = JSON.parse(localStorage.getItem('user') || '{}')
const isAdmin = currentUser.role === 'admin'
const userList = ref([])
const usersLoading = ref(false)
const showAddUser = ref(false)
const addingUser = ref(false)
const newUserForm = reactive({ username: '', password: '', role: 'guest' })
const showChangePassword = ref(false)
const changingPwd = ref(false)
const changePwdTarget = ref(null)
const newPassword = ref('')

async function loadUsers() {
  if (!isAdmin) return
  usersLoading.value = true
  try {
    const { data } = await authApi.listUsers()
    userList.value = data
  } catch { /* interceptor handles */ }
  finally { usersLoading.value = false }
}

async function handleAddUser() {
  if (!newUserForm.username || !newUserForm.password) {
    ElMessage.warning('请填写用户名和密码')
    return
  }
  addingUser.value = true
  try {
    await authApi.createUser(newUserForm)
    ElMessage.success('添加成功')
    showAddUser.value = false
    newUserForm.username = ''
    newUserForm.password = ''
    newUserForm.role = 'guest'
    loadUsers()
  } catch { /* interceptor handles */ }
  finally { addingUser.value = false }
}

function openChangePassword(user) {
  changePwdTarget.value = user
  newPassword.value = ''
  showChangePassword.value = true
}

async function handleChangePassword() {
  if (!newPassword.value) {
    ElMessage.warning('请输入新密码')
    return
  }
  changingPwd.value = true
  try {
    await authApi.changePassword(changePwdTarget.value.id, newPassword.value)
    ElMessage.success('密码已修改')
    showChangePassword.value = false
  } catch { /* interceptor handles */ }
  finally { changingPwd.value = false }
}

async function handleDeleteUser(user) {
  await ElMessageBox.confirm(`确定删除用户 "${user.username}" 吗？`, '删除确认', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消',
  })
  try {
    await authApi.deleteUser(user.id)
    ElMessage.success('已删除')
    loadUsers()
  } catch { /* interceptor handles */ }
}

onMounted(() => {
  loadConfig()
  loadJellyfinLibraries()
  refreshActressStatus()
  loadUsers()
  // 默认 tab 是 diagnostics → 首次进入主动加载（watch 不会触发，因为 activeTab 没变化）
  if (activeTab.value === 'diagnostics') {
    loadDiagnosticsSystem()
    // 加载 services 列表完后自动跑核心三项（jellyfin / qb / jackett）
    loadDiagnosticsServices().then(() => runDiagnosticsCore())
  }
  // 运行中每 3 秒轮询一次；闲时 10 秒一次（保留检测重启 / 别处启动）
  actressPollTimer = setInterval(() => {
    refreshActressStatus()
  }, 5000)
})
onBeforeUnmount(() => {
  if (actressPollTimer) clearInterval(actressPollTimer)
  if (logsPollTimer) clearInterval(logsPollTimer)
})

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
  background: var(--jt-card-bg);
  border-radius: 8px;
  box-shadow: var(--jt-shadow-sm);
  min-height: 600px;

  :deep(.el-tabs__nav-wrap) {
    padding-top: 16px;
    background: var(--jt-fill-light);
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

  // 豆瓣片单白名单：每行 3 列输入 + 删除按钮
  .doulist-rows {
    display: flex;
    flex-direction: column;
    gap: 6px;

    .doulist-row {
      display: flex;
      align-items: center;
      gap: 8px;
    }
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
    &.mdblist { background: linear-gradient(135deg, #ef4444 0%, #f59e0b 100%); }
    &.douban { background: linear-gradient(135deg, #2c8c1d 0%, #6cc24a 100%); font-size: 13px; }
    &.wikidata { background: linear-gradient(135deg, #006699 0%, #339cd1 100%); }
    &.trakt { background: linear-gradient(135deg, #ed1c24 0%, #ff6b6b 100%); font-size: 11px; }
    &.anilist { background: linear-gradient(135deg, #02a9ff 0%, #74d3ff 100%); }
    &.subtitle-common { background: linear-gradient(135deg, #475569 0%, #94a3b8 100%); font-size: 11px; }
    &.assrt { background: linear-gradient(135deg, #d97706 0%, #f59e0b 100%); font-size: 11px; }
    &.logs { background: linear-gradient(135deg, #1e293b 0%, #475569 100%); font-size: 12px; }
  }
}

// 日志 tab
.logs-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  align-items: center;
  margin-bottom: 12px;

  .logs-actions {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-left: auto;
  }
}

.logs-meta {
  font-size: 12px;
  color: var(--jt-text-muted);
  margin-bottom: 6px;
}

.logs-viewer {
  height: 600px;
  margin: 0;
  padding: 12px;
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  overflow: auto;
  white-space: pre;
  word-wrap: normal;
}


.form-hint {
  margin-top: 4px;
  font-size: 12px;
  color: var(--jt-text-muted);

  a { color: var(--jt-brand); }
}

// 表单项里跟在控件后面的 form-hint（任何标签 span / div / p）：
// 跟控件之间留 12px 间距 + 与控件中线垂直居中对齐。
// 长文本会因 el-form-item__content 是 flex-wrap 自动换到下一行。
// 卡片顶部直接挂的 <p class="form-hint">（el-card 直属，不在 el-form-item__content 内）不受影响。
:deep(.el-form-item__content) {
  > .form-hint {
    margin-top: 0;
    margin-left: 12px;
    align-self: center;
    line-height: 1.4;
  }
}


// 路径模板下方的默认值提示：mono 字体方便对比 + 浅色不抢眼
.form-hint.default-hint {
  display: block;
  font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 11px;
  color: var(--jt-text-muted);
  word-break: break-all;
  line-height: 1.4;
}

// 女优库卡片
.actress-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin: 16px 0 12px;

  .stat-cell {
    border: 1px solid var(--jt-card-border);
    border-radius: 6px;
    padding: 12px;
    text-align: center;
    background: var(--jt-fill-light);

    .stat-num {
      font-size: 22px;
      font-weight: 600;
      color: var(--jt-text-regular);
      line-height: 1.2;
    }
    .stat-label {
      font-size: 12px;
      color: var(--jt-text-muted);
      margin-top: 4px;
    }

    &.ok    { background: var(--jt-success-tint); border-color: var(--jt-success-border); .stat-num { color: var(--jt-success); } }
    &.pending { background: rgba(var(--jt-brand-rgb), 0.08); border-color: rgba(var(--jt-brand-rgb), 0.3); .stat-num { color: var(--jt-brand); } }
    &.miss  { background: var(--jt-warning-tint); border-color: var(--jt-warning-border); .stat-num { color: var(--jt-warning); } }
  }
}

.actress-current {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  background: var(--jt-brand-light-9);
  border-left: 3px solid var(--jt-brand);
  border-radius: 4px;
  font-size: 13px;
  color: var(--jt-text-regular);
  margin-bottom: 10px;

  .spin {
    animation: spin 1.5s linear infinite;
    color: var(--jt-brand);
  }
  .run-counts {
    margin-left: auto;
    color: var(--jt-text-muted);
    font-size: 12px;
  }
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

.actress-error {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  background: var(--jt-danger-tint);
  border-left: 3px solid var(--jt-danger);
  border-radius: 4px;
  color: var(--jt-danger-text);
  font-size: 13px;
  margin-bottom: 10px;
}

.actress-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

// 期望语言：3 行（label / 选项 / 说明）
.lang-stacked {
  // 强制 el-form-item 内容区改成纵向排列；默认是横向 flex 会把 3 个块挤一行
  :deep(.el-form-item__content) {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    line-height: 1.4;
  }

  .lang-label {
    font-size: 14px;
    color: var(--jt-text-regular);
    font-weight: 500;
    margin-bottom: 8px;
  }

  .lang-line {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    margin-bottom: 4px;
  }

  // 覆盖全局的 inline hint 规则（column flex 下 align-self:center 会变成水平居中）
  :deep(.el-form-item__content) > .form-hint {
    margin-left: 0;
    align-self: stretch;
    text-align: left;
  }
}

.sub-section-title {
  font-weight: 500;
  color: var(--jt-text-regular);
}

// 调试开关 block：跟路径映射同款风格，避免在视觉上跳跃
.debug-block {
  .debug-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
    flex-wrap: wrap;

    .switch-label {
      font-size: 13px;
      color: var(--jt-text-regular);
      font-weight: 500;
    }
    .form-hint {
      margin-top: 0;
      flex: 1;
    }
  }
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
      color: var(--jt-text-regular);
      font-weight: 500;
    }

    .form-hint {
      margin-top: 0;
      flex: 1;

      code {
        background: rgba(var(--jt-brand-rgb), 0.1);
        color: var(--jt-brand-dark);
        padding: 1px 4px;
        border-radius: 3px;
        font-size: 11px;
      }
    }
  }

  .empty-rules {
    text-align: center;
    color: var(--jt-text-muted);
    font-size: 12px;
    padding: 16px;
    background: var(--jt-fill-light);
    border: 1px dashed var(--jt-card-border);
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
        color: var(--jt-text-muted);
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
    background: var(--jt-fill-light);
    border: 1px solid var(--jt-card-border);
    border-radius: 6px;
    transition: all 0.2s;

    &.disabled {
      opacity: 0.6;
      background: var(--jt-divider-light);
    }

    .el-input {
      flex: 1;
    }
  }
}

// ---- LLM tab ----
.llm-test-result {
  margin-left: 12px;
  font-size: 12px;
  &.ok { color: var(--jt-success); }
  &.fail { color: var(--jt-danger); }
}
.badge.tools-badge {
  background: #e0e7ff;
  color: #4338ca;
}
.badge.llm-badge {
  background: linear-gradient(135deg, #fce7f3, #ddd6fe);
  color: #7e22ce;
}
.badge.dispatch-badge {
  background: #ecfdf5;
  color: #047857;
}
.badge.meta-badge {
  background: linear-gradient(135deg, #fef3c7, #fde68a);
  color: #92400e;
}

// ---- 流水线规则卡（双列布局，每条规则一个紧凑 row） ----
.dispatch-rule-row {
  border: 1px solid var(--jt-card-border);
  border-radius: 6px;
  padding: 10px 14px 0;
  margin-bottom: 12px;
  background: var(--jt-fill-light);

  .dispatch-rule-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    padding-bottom: 6px;
    border-bottom: 1px dashed var(--jt-card-border);

    .mt-icon {
      font-size: 16px;
      &.mt-movie       { color: #3b82f6; }
      &.mt-tv          { color: #8b5cf6; }
      &.mt-anime       { color: #ec4899; }
      &.mt-documentary { color: #0891b2; }
      &.mt-adult       { color: #ef4444; }
    }
    .mt-label {
      font-weight: 600;
      color: var(--jt-text-primary);
      font-size: 13px;
    }
    .mt-tag {
      font-size: 11px;
      font-family: ui-monospace, SFMono-Regular, monospace;
      color: var(--jt-text-secondary);
      background: var(--jt-divider-light);
      padding: 1px 6px;
      border-radius: 3px;
    }
  }

  // 紧凑 form 减少 vertical 占位
  :deep(.el-form-item) { margin-bottom: 8px; }
  :deep(.el-form-item__label) { line-height: 28px; }
}

.template-vars-hint {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  padding: 8px 12px;
  background: var(--jt-fill-light);
  border-radius: 4px;
  font-size: 12px;
  color: var(--jt-text-regular);

  strong { color: var(--jt-text-primary); }
  code {
    padding: 1px 6px;
    background: var(--jt-card-bg);
    border: 1px solid var(--jt-card-border);
    border-radius: 3px;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 11px;
    color: #0369a1;
  }
}

</style>

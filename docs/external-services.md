# 外部服务与依赖站点清单

整理本项目所有调用的第三方服务/网站，按用途分类。**自部署服务**（Jellyfin / qBittorrent / Jackett）跟**外网公共服务**（TMDB / OpenSubtitles 等）分开列。

---

## 🔧 自部署服务（必需）

用户自己运行的服务，本项目通过 HTTP API 对接。

| 服务 | 默认地址 | 用途 | 配置项 | 备注 |
|---|---|---|---|---|
| **Jellyfin** | `http://localhost:8096` | 媒体库管理（库列表/扫描刷新/元数据/演员图片） | `jellyfin_host` + `jellyfin_api_key` | API key 必须管理员级，`/Library/Media/Updated` 等端点要求 elevation |
| **qBittorrent** | `http://localhost:8080` | 种子下载（加种/暂停/恢复/删除/RSS 订阅与规则） | `qbittorrent_host` + `username` + `password` | 需 4.6+ 支持 `stop_condition='MetadataReceived'` |
| **Jackett** | `http://localhost:9117` | 搜索 PT/公开站索引器（包装成 Torznab/RSS） | `jackett_host` + `jackett_api_key` | RSS feed URL 可直接给 qB 订阅自动下载 |

---

## 🎬 影视元数据 / 推荐

| 服务 | URL | 用途 | 配置 / Key | 备注 |
|---|---|---|---|---|
| **TMDB** | `https://api.themoviedb.org` | 影视元数据主源（电影/剧集/演员/海报/热门/趋势） | `tmdb_api_key` | 必需，免费注册申请；图片走 `https://image.tmdb.org` |
| **TMDB Web** | `https://www.themoviedb.org` | 用户跳转链接 | — | 前端 item 链接 |
| **IMDB** | `https://www.imdb.com` | 跳转链接（imdb_id 已记录在 db） | — | 不调 API，仅生成跳转 URL |
| **MDBList** | `https://api.mdblist.com` | 第三方榜单聚合（Top / Trending） | `mdblist_api_key` | 可选；TMDB 之外的另一份热门数据源 |
| **MDBList Web** | `https://mdblist.com` | 跳转链接 | — | |
| **豆瓣** | `https://www.douban.com` `https://movie.douban.com` `https://search.douban.com` | 中文影视评分/简介补充 | — | 网页爬取，无 API key；有节流限制 |

---

## 📝 字幕

| 服务 | URL | 用途 | 配置 / Key | 备注 |
|---|---|---|---|---|
| **OpenSubtitles** | `https://api.opensubtitles.com` | 多语言字幕（API） | `opensubtitles_api_key` + `username` + `password` | 必需登录获取 token |
| **OpenSubtitles Web** | `https://www.opensubtitles.com` | 跳转链接 | — | |
| **OpenSubtitles 文档** | `https://opensubtitles.stoplight.io` | API 文档 | — | 仅参考 |
| **ASSRT 伪射手** | `https://api.assrt.net` `https://secure.assrt.net` | 中文字幕（API） | `assrt_api_token` | 中文字幕首选源 |
| **Shooter（射手）** | `https://www.shooter.cn` | 字幕 hash 匹配 | — | 老 API，按视频 hash 直接匹配 |
| **Bazarr 文档** | `https://wiki.bazarr.media` | 字幕格式参考 | — | 仅文档参考 |

---

## 🎭 演员图片 / 信息

| 服务 | URL | 用途 | 配置 | 备注 |
|---|---|---|---|---|
| **Wikidata SPARQL** | `https://query.wikidata.org` | 演员维基数据（备选演员图） | — | 公开 SPARQL，需 User-Agent，建议 ≥0.5s 间隔 |
| **Wikimedia Commons** | `http://commons.wikimedia.org` | 演员图片 CDN | — | 跟随 wikidata 图片实体跳转 |
| **Meta Wikimedia** | `https://meta.wikimedia.org` | API 兜底 | — | |

---

## 🎥 成人内容（adult library）

⚠️ 这些站经常被地理屏蔽，用户需要配代理；多数走 cffi + Cloudflare 绕过。

| 服务 | URL | 用途 | 当前状态 | 备注 |
|---|---|---|---|---|
| **JavDB** | `https://javdb.com` | 番号刮削 + 女优归一化（jp/zh/en/aliases） | ⚠️ **地理屏蔽常见** | 主刮削源；resolver 用它做女优档案库 |
| **JavBus** | `https://www.javbus.com` | 番号刮削备选 | 通常可用 | 备用源 |
| **JavLibrary** | `https://www.javlibrary.com` | 番号刮削备选 | 通常可用 | 备用源 |
| **AVBase** | `https://www.avbase.net` | 番号刮削 + 大量元数据 | 通常可用 | 较新的源 |
| **MissAV** | `https://missav.ai` | 在线观影/封面 | — | 最近域名 .ai；遇 .ws/.com 等需切 |

scraper 优先级在 `tools/adult_manager/scrapers/manager.py` 配置。

---

## 🤖 LLM（媒体识别兜底）

跑本地启发式 + TMDB 都识别不出时，调 LLM 兜底分类（`tools/dispatch/identify.py`）。

| 服务 | URL | 用途 | 配置 / Key | 备注 |
|---|---|---|---|---|
| **DeepSeek** | `https://api.deepseek.com` | 主推 LLM 提供商（性价比高） | `llm.api_key` + `llm.base_url` | OpenAI 兼容 API |
| **OpenAI** | `https://api.openai.com` | LLM 提供商 | 同上 | OpenAI 兼容 API |
| **阿里云 DashScope（通义）** | `https://dashscope.aliyuncs.com` | LLM 提供商（千问） | 同上 | OpenAI 兼容 API |

任何 OpenAI 兼容的 endpoint 都能用——配 base_url + api_key 即可。

---

## 📦 其它

| 服务 | URL | 用途 | 备注 |
|---|---|---|---|
| **YouTube** | `https://www.youtube.com` `https://img.youtube.com` | 预告片跳转 / 缩略图 | 仅生成 URL，不调 API |
| **GitHub** | `https://github.com` | 仓库自身 / 文档链接 | 不调 API |
| **Jellyfin 官网** | `https://jellyfin.org` | 文档参考 | 不调 API |

---

## 🚦 调用频率与代理建议

| 类别 | 建议节流 | 备注 |
|---|---|---|
| TMDB | 30-50 req/s（官方限制 50） | 全球 CDN，代理可不开 |
| Jackett / qB / Jellyfin | 内网无限制 | 自部署 |
| OpenSubtitles | 1-2 req/s | 免费 token 有日配额 |
| ASSRT | 1-2 req/s | 中文字幕 |
| 豆瓣 | ≥2s 间隔 | 易触发风控，建议 IP 池 |
| **JavDB** | **≥5s 间隔** | 地理屏蔽 + 风控双高，必须代理 |
| AVBase / JavBus / JavLibrary | ≥3s 间隔 | 同上但风控弱一些 |
| LLM (DeepSeek / OpenAI) | 看 provider 限速 | 内置缓存 `llm_classify_cache` 表 |

## 🌍 已知地理屏蔽问题

| 站点 | 触发条件 | 表现 | 解决 |
|---|---|---|---|
| **JavDB** | 出口 IP 被 javdb 列入版权管制地区 | HTTP 200 + 1.5KB 拦截页（"copyright restrictions"） | 换日本 / 美国 / 香港代理节点 |
| MissAV | 域名经常变（.ai → .ws → .com → ...） | DNS 解析失败 | 改 `missav.py` 里的 `BASE` 常量 |
| 豆瓣 | 高频访问 | 403 / 验证码 | 拉长间隔到 5s+ |

---

## 🛡️ OpenClash 分流规则（成人内容站走指定节点）

把 JavDB / JavBus / JavLibrary / AVBase / MissAV 路由到指定节点（如日本线路），
失败自动 fallback 到主节点 → DIRECT。粘到 OpenClash → 配置文件 → 规则 / 自定义规则。

### 1. 在 `proxy-groups:` 段加一个新组

```yaml
proxy-groups:
  # …你已有的组…

  # 🔞 成人内容专用分组：fallback 自动切换
  # 顺序：先试"AV-专用节点"；不通则用"节点选择"（你的主分组）；都不行 DIRECT 兜底
  - name: "🔞 AV-Sites"
    type: fallback
    proxies:
      - "🇯🇵 AV-专用节点"      # ← 改成你想专用的节点名（可见于 OpenClash 节点列表）
      - "🚀 节点选择"            # ← 改成你的主分组名（通常叫 "节点选择" / "Proxy" / "PROXY"）
      - DIRECT
    url: "https://www.gstatic.com/generate_204"
    interval: 300
    tolerance: 50
```

> **如果你想手动选**（不要 fallback 自动切），把 `type: fallback` 改成 `type: select`，
> 然后在 OpenClash 面板里随时点选当前用哪个节点即可。

### 2. 在 `rules:` 段顶部加这些规则（**位置要在 MATCH/FINAL 之前**）

```yaml
rules:
  # ===== 🔞 成人内容站点（走 AV-Sites 组，fallback 主节点）=====
  - DOMAIN-SUFFIX,javdb.com,🔞 AV-Sites
  - DOMAIN-SUFFIX,javbus.com,🔞 AV-Sites
  - DOMAIN-SUFFIX,javlibrary.com,🔞 AV-Sites
  - DOMAIN-SUFFIX,avbase.net,🔞 AV-Sites
  # MissAV 域名经常切换，连同已知备用域名 + keyword 兜底
  - DOMAIN-SUFFIX,missav.ai,🔞 AV-Sites
  - DOMAIN-SUFFIX,missav.ws,🔞 AV-Sites
  - DOMAIN-SUFFIX,missav.com,🔞 AV-Sites
  - DOMAIN-KEYWORD,missav,🔞 AV-Sites
  # ===== 🔞 END =====

  # …你原有的规则继续放这下面…
```

### 3. （可选）DNS 段：让这些域名走代理 DNS 解析

很多 javdb 故障表现是"DNS 投毒返回错误 IP" → 走代理 DNS 能避免。
在 `dns:` 段的 `nameserver-policy` 加几条：

```yaml
dns:
  nameserver-policy:
    "geosite:javdb": [https://1.1.1.1/dns-query, https://dns.google/dns-query]
    # 或者按域名精确写：
    "+.javdb.com": [https://1.1.1.1/dns-query]
    "+.javbus.com": [https://1.1.1.1/dns-query]
    "+.javlibrary.com": [https://1.1.1.1/dns-query]
    "+.avbase.net": [https://1.1.1.1/dns-query]
    "+.missav.ai": [https://1.1.1.1/dns-query]
```

### 4. Provider 规则集版（更省心，自动维护）

如果用 [@blackmatrix7 的规则集](https://github.com/blackmatrix7/ios_rule_script) 或类似：

```yaml
rule-providers:
  blackmatrix7-jav:
    type: http
    behavior: classical
    url: "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/JAV/JAV.yaml"
    path: ./ruleset/jav.yaml
    interval: 86400

rules:
  - RULE-SET,blackmatrix7-jav,🔞 AV-Sites
  # …
```

⚠️ 第三方规则集列表里**可能不含 AVBase**（较新），AVBase 那条最好仍用上面的 DOMAIN-SUFFIX 写死。

### 5. 验证

应用规则后，在 OpenClash → 实时连接里观察 `javdb.com:443` 这种连接：
- "代理"列应显示 **🔞 AV-Sites** 组（而不是默认组）
- 该组当前实际选中的节点要能解析到 javdb 的真实 IP

后端这边再跑一次 `python -c "from tools.adult_manager.actress_resolver import ActressResolver; ..."` 测试调用，看返回的不再是版权拦截页。


/**
 * 外部服务后台链接解析（qBittorrent / Jackett 的「打开后台」入口）。
 *
 * 背景：这两个服务有两个地址字段
 *   - host        后端连接用，同时也是内网地址（如 http://192.168.89.6）
 *   - external_url 容器部署时给浏览器的地址（host 变成容器名 http://qbittorrent:8080）
 *
 * 用户要求入口用**内网链接**，所以取址顺序是「内网优先」：
 *   1. host 浏览器可达（IP / 带点的域名 / localhost）→ 用它
 *   2. host 是单段主机名（qbittorrent、jackett 这类容器名，浏览器解析不了）
 *      → 回落到 external_url；都没有则不显示按钮，避免点了必然报错
 */

const _trim = (u) => String(u || '').trim().replace(/\/+$/, '')

/** 补协议：href 里没有 http(s):// 时浏览器会当成相对路径，必须补上。 */
const _withProtocol = (u) => (/^[a-z][a-z0-9+.-]*:\/\//i.test(u) ? u : `http://${u}`)

/**
 * host 是否可能被浏览器直接访问。
 * 单段主机名（无点、非 IP、非 localhost）= 容器名 / 内部 DNS 名，浏览器解析不了。
 */
const _browserReachable = (url) => {
  if (!url) return false
  try {
    // URL 构造需要协议；缺协议时按 http 补一个再判断
    const withProto = /^[a-z][a-z0-9+.-]*:\/\//i.test(url) ? url : `http://${url}`
    const hostname = new URL(withProto).hostname
    if (!hostname) return false
    if (hostname === 'localhost') return true
    if (/^\d{1,3}(\.\d{1,3}){3}$/.test(hostname)) return true    // IPv4
    if (hostname.includes(':')) return true                       // IPv6（URL 里带方括号）
    return hostname.includes('.')                                 // 域名 / 局域网主机名
  } catch {
    return false
  }
}

/**
 * 找到给浏览器用的服务基地址（不带结尾斜杠）；找不到返回 ''。
 * 优先级：host（内网，需浏览器可达）> external_url
 */
export const resolveServiceBaseUrl = (svc) => {
  if (!svc) return ''
  const host = _trim(svc.host)
  if (_browserReachable(host)) return _withProtocol(host)
  const external = _trim(svc.external_url)
  return external ? _withProtocol(external) : ''
}

/** qBittorrent Web UI 地址（带结尾斜杠，qB 不认无斜杠的根路径）。 */
export const qbWebUrl = (svc) => {
  const base = resolveServiceBaseUrl(svc)
  return base ? `${base}/` : ''
}

/** Jackett Web UI 地址（直接落到 Dashboard）。 */
export const jackettWebUrl = (svc) => {
  const base = resolveServiceBaseUrl(svc)
  return base ? `${base}/UI/Dashboard` : ''
}

/**
 * 时间格式化工具
 *
 * 后端 datetime 字段全部是 naive UTC（datetime.utcnow()），
 * 序列化为 ISO 字符串时不带 tz 后缀（如 "2026-05-04T16:29:15"）。
 * 直接喂给 dayjs() 会被当作本地时间，造成"显示差几小时"的 bug。
 *
 * 这里统一用 dayjs.utc() 显式按 UTC 解析，再 .local() 转本机时区显示。
 * dayjs.extend(utc) 在 main.js 已全局加载。
 */
import dayjs from 'dayjs'

/**
 * 把后端返回的 UTC 时间字符串格式化为本机时区的字符串。
 * @param {string} utcStr - ISO 时间字符串（无 tz 后缀的 naive UTC，或带 Z/+offset 的也行）
 * @param {string} fmt - dayjs 格式（默认 'YYYY-MM-DD HH:mm:ss'）
 * @param {string} fallback - 输入为空时的兜底（默认 '—'）
 */
export const formatLocalTime = (utcStr, fmt = 'YYYY-MM-DD HH:mm:ss', fallback = '—') => {
  if (!utcStr) return fallback
  return dayjs.utc(utcStr).local().format(fmt)
}

/** 简短形式：MM-DD HH:mm:ss */
export const formatLocalTimeShort = (utcStr, fallback = '—') => {
  return formatLocalTime(utcStr, 'MM-DD HH:mm:ss', fallback)
}

/** 把 UTC 字符串转为 dayjs 本地对象（让调用方做后续操作，如 isAfter / diff）*/
export const localDayjs = (utcStr) => {
  if (!utcStr) return null
  return dayjs.utc(utcStr).local()
}

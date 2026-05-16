/**
 * 判断当前浏览器是否从内网访问。
 * 内网条件：hostname 是 localhost / 127.x / 10.x / 172.16-31.x / 192.168.x
 */
export function useNetwork() {
  const hostname = window.location.hostname

  const isLan = (
    hostname === 'localhost' ||
    hostname === '127.0.0.1' ||
    hostname.startsWith('10.') ||
    hostname.startsWith('192.168.') ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(hostname)
  )

  return { isLan }
}

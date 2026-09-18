/**
 * 复制到剪贴板。
 *
 * navigator.clipboard 只在安全上下文（https / localhost）可用，
 * 内网 http 访问控制台时会抛错，所以留一条 textarea + execCommand 的老路兜底。
 */
export async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    return
  } catch {
    /* 非安全上下文：走下面的兜底 */
  }
  const area = document.createElement('textarea')
  area.value = text
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.opacity = '0'
  document.body.appendChild(area)
  area.select()
  document.execCommand('copy')
  document.body.removeChild(area)
}

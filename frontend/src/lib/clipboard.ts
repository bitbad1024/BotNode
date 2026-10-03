/**
 * 剪贴板读写。
 *
 * navigator.clipboard 只在安全上下文（https / localhost）可用，
 * 内网 http 访问控制台时会抛错，所以写留一条 textarea + execCommand 的老路兜底。
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

/**
 * 从系统剪贴板读回文本；读不到返回空串（**不抛**，由调用方决定怎么兜底）。
 *
 * 读比写严得多：非安全上下文没有 navigator.clipboard，Firefox / Safari 还要求用户手势
 * 或显式授权，被拒时也是抛异常。这些都不算错误 —— 它只是「第二重保险」，
 * 读不到就退回调用方自己那份内存里的内容。
 */
export async function readText(): Promise<string> {
  try {
    return await navigator.clipboard.readText()
  } catch {
    return ''
  }
}

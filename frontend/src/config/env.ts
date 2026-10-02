/**
 * 运行时环境配置（构建期由 Vite 注入 import.meta.env）。
 *
 * VITE_API_BASE_URL：
 *   留空  -> 同源，开发走 Vite 代理、部署走反向代理；
 *   填写  -> 直连该后端根地址（跨域需后端放行 CORS）。
 */

function trimSlash(s: string): string {
  return s.replace(/\/+$/, '')
}

const rawBase = (import.meta.env.VITE_API_BASE_URL ?? '').trim()

/** 后端根地址（不含末尾斜杠），留空为 ''。 */
export const BACKEND_BASE_URL = trimSlash(rawBase)

/** axios baseURL：留空时走同源 /api。 */
export const API_BASE_URL = BACKEND_BASE_URL ? `${BACKEND_BASE_URL}/api` : '/api'

/** 拼接后端文档等绝对/相对地址。 */
export function backendUrl(path: string): string {
  return `${BACKEND_BASE_URL}${path}`
}

/** 是否显示演示账号（仅开发环境）。 */
export const SHOW_DEMO_ACCOUNTS =
  import.meta.env.VITE_SHOW_DEMO_ACCOUNTS === 'true'

export interface DemoAccount {
  account: string
  password: string
}

export const DEMO_ACCOUNTS: DemoAccount[] = [
  { account: 'admin', password: 'botnode-admin' },
  { account: 'robot', password: 'botnode-robot' },
]

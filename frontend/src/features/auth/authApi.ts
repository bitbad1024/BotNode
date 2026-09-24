/** 鉴权接口：与后端 /api/auth/* 一一对应。 */
import { http } from '../../lib/http'

export interface UserProfile {
  id: string
  account: string
  nickname: string
  roles: string[]
}

export interface LoginResult {
  token: string
  token_type: 'bearer'
  /** 剩余有效秒数；带令牌的请求会滑动续期，0 表示不过期 */
  expires_in: number
  user: UserProfile
  /** 这条登录的令牌摘要（设备列表里的 id，吊销按它定位） */
  token_hash: string
  /** 服务端按 UA 推断的设备名，用于「已在新设备登录」提示 */
  device_name: string
  /** true=复用了这台设备上的旧登录（设备列表不新增条目） */
  reused: boolean
}

/**
 * POST /auth/login：账号 + 密码换令牌。
 *
 * :param remember: 勾了「记住这台设备」才发长期令牌（30 天免登录），否则关闭浏览器即失效。
 */
export function login(account: string, password: string, remember: boolean) {
  return http.post<
    LoginResult,
    { account: string; password: string; remember: boolean }
  >('/auth/login', { account, password, remember })
}

/** GET /auth/me：用当前令牌换取用户资料。 */
export function fetchProfile() {
  return http.get<UserProfile>('/auth/me')
}

// --------------------------------------------------------------------------- 登录设备

export interface SessionInfo {
  token_hash: string
  current: boolean
  device_name: string
  device_type: string
  browser: string
  os: string
  ip: string
  created_at: number
  remembered: boolean
}

/** GET /auth/sessions：我开着的全部登录。 */
export function listSessions() {
  return http.get<SessionInfo[]>('/auth/sessions')
}

/** DELETE /auth/sessions/{token_hash}：吊销一条登录。 */
export function revokeSession(tokenHash: string) {
  return http.del<{ token_hash: string; removed: boolean }>(
    `/auth/sessions/${encodeURIComponent(tokenHash)}`,
  )
}

/** DELETE /auth/sessions：全部下线（含当前这条）。 */
export function revokeAllSessions() {
  return http.del<{ count: number }>('/auth/sessions')
}

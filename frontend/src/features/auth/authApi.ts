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
  expires_in: number
  user: UserProfile
}

/** POST /auth/login：账号 + 密码换令牌。 */
export function login(account: string, password: string) {
  return http.post<LoginResult, { account: string; password: string }>(
    '/auth/login',
    { account, password },
  )
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

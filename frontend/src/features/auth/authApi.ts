/**
 * 鉴权接口：与后端 /api/auth/* 一一对应。
 *
 * 认证模型：后端只发一种不透明会话令牌，登录成功同时写进 HttpOnly Cookie（浏览器自动带，
 * 只管 /api/auth/* 这一段）和响应体（非浏览器客户端 / /api/onebot/* 走 Bearer）。
 */
import { http } from '../../lib/http'

export interface UserProfile {
  id: string
  account: string
  nickname: string
  roles: string[]
}

export interface LoginResult {
  /** 会话令牌明文；浏览器里另由 HttpOnly Cookie 承载，这份用于 Bearer 头 */
  token: string
  token_type: 'bearer'
  /** 剩余有效秒数；带令牌的请求会滑动续期，0 表示不过期 */
  expires_in: number
  user: UserProfile
  /** 本会话令牌摘要（设备列表里的 id，吊销靠它） */
  token_hash: string
  /** 服务端按 UA 推断的设备名，可用于「已在新设备登录」提示 */
  device_name: string
  /** true=复用了旧令牌（设备列表不新增条目）；false=新开了一条会话 */
  reused: boolean
}

/** 「登录设备」列表里的一行（GET /auth/sessions）。 */
export interface SessionInfo {
  /** 令牌摘要 = 这条登录的 id，吊销时按它定位 */
  token_hash: string
  /** 是不是当前这台设备 */
  current: boolean
  device_name: string
  /** mobile / desktop / unknown */
  device_type: string
  browser: string
  os: string
  ip: string
  /** 登录时刻（Unix 秒） */
  created_at: number
  /** 当时是否勾了「记住设备」 */
  remembered: boolean
}

export interface RevokeSessionResult {
  token_hash: string
  removed: boolean
}

export interface RevokeAllResult {
  /** 下线条数（包含当前这条） */
  count: number
}

/**
 * POST /auth/login：账号 + 密码换会话。
 * @param remember 记住设备：勾了滑动有效期长（默认 30 天）、Cookie 持久；
 *                 不勾是会话 Cookie + 短有效期（默认 2 小时）。
 * 旧令牌不用传：HttpOnly Cookie 会自动带上，服务端认得出就复用。
 */
export function login(account: string, password: string, remember: boolean) {
  return http.post<LoginResult, { account: string; password: string; remember: boolean }>(
    '/auth/login',
    { account, password, remember },
  )
}

/** GET /auth/me：探测登录态 / 拿最新资料；响应头带滑动续期后的剩余秒数。 */
export function fetchProfile() {
  return http.get<UserProfile>('/auth/me')
}

/** GET /auth/sessions：我开着的全部登录（新的在前，当前这条 current=true）。 */
export function listSessions() {
  return http.get<SessionInfo[]>('/auth/sessions')
}

/** DELETE /auth/sessions/{tokenHash}：把某台设备下线；吊销当前这条时服务端顺带清 Cookie。 */
export function revokeSession(tokenHash: string) {
  return http.del<RevokeSessionResult>(
    `/auth/sessions/${encodeURIComponent(tokenHash)}`,
  )
}

/** DELETE /auth/sessions：全部下线（包含当前这条），调用方随后应回登录页。 */
export function revokeAllSessions() {
  return http.del<RevokeAllResult>('/auth/sessions')
}

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

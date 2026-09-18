/** OneBot 令牌与在线客户端接口：与后端 /api/onebot/* 一一对应。 */
import { http } from '../../lib/http'

/** 在线客户端列表里的一行（握手时由令牌定下归属）。 */
export interface OneBotClient {
  /** 连接编号：踢下线时按它定位 */
  id: string
  /** 归属账号 */
  account: string
  /** 机器人号；还没收到事件时是 null */
  self_id: number | null
  /** 对端地址 */
  remote: string
  /** 连上的时刻（Unix 秒） */
  connected_at: number
}

/** 一条令牌记录（**不含明文**：后端只给记录，明文只在签发那一次返回）。 */
export interface OneBotToken {
  id: string
  account: string
  enabled: boolean
  remark: string
  created_at: number
}

export interface IssuedToken {
  record: OneBotToken
  /** 明文令牌：**只在这一次响应里出现**，之后查不回来 */
  token: string
}

export interface KickResult {
  client_id: string
  account: string
  revoked: boolean
}

export interface RevokeResult {
  token_id: string
  removed: boolean
}

/** GET /onebot/clients：在线客户端；给 account 就只看那一个账号下的。 */
export function fetchClients(account?: string) {
  const query = account ? `?account=${encodeURIComponent(account)}` : ''
  return http.get<OneBotClient[]>(`/onebot/clients${query}`)
}

/**
 * DELETE /onebot/clients/{id}：断开这条连接。
 *
 * revoke=true 连令牌一起吊销——OneBot 实现断线都会自动重连，只断开不吊销的话
 * 过几秒它又会回到列表里。
 */
export function kickClient(clientId: string, revoke = false) {
  const query = revoke ? '?revoke=true' : ''
  return http.del<KickResult>(
    `/onebot/clients/${encodeURIComponent(clientId)}${query}`,
  )
}

/** GET /onebot/tokens：令牌列表（不含明文）。 */
export function fetchTokens() {
  return http.get<OneBotToken[]>('/onebot/tokens')
}

/** POST /onebot/tokens：签发一个令牌，明文只在这一次返回。 */
export function issueToken(account: string, remark = '') {
  return http.post<IssuedToken, { account: string; remark: string }>(
    '/onebot/tokens',
    { account, remark },
  )
}

/**
 * PATCH /onebot/tokens/{id}：启用 / 停用。
 *
 * 停用 = 不许再连（握手 401），并把正用它连着的客户端一并断开；记录还在，
 * 随时能启用回来 —— 和 revokeToken（吊销，删记录、不可逆）是两回事。
 */
export function setTokenEnabled(tokenId: string, enabled: boolean) {
  return http.patch<OneBotToken, { enabled: boolean }>(
    `/onebot/tokens/${encodeURIComponent(tokenId)}`,
    { enabled },
  )
}

/** DELETE /onebot/tokens/{id}：吊销令牌，并把正用它连着的客户端断开。 */
export function revokeToken(tokenId: string) {
  return http.del<RevokeResult>(`/onebot/tokens/${encodeURIComponent(tokenId)}`)
}

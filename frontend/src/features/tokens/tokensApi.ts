/** OneBot 令牌与在线客户端接口：与后端 /api/onebot/* 一一对应。 */
import { http } from '../../lib/http'

/** 在线客户端列表里的一行（握手时由令牌定下归属）。 */
export interface OneBotClient {
  /** 这条连接自己的编号：踢下线时按它定位 */
  client_id: string
  /** 这条连接属于谁（握手时由令牌定下来） */
  id: string
  /** 归属那个 id 在用户表里的昵称（查不到就是空串） */
  nickname: string
  /** 接入 WS 的那个 OneBot 机器人账号 */
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
  /** 归属标识（谁的），也是记录主键 */
  id: string
  /** 接入 WS 的那个 OneBot 机器人账号（自由填，只用来展示） */
  account: string
  enabled: boolean
  remark: string
  created_at: number
  /** 归属那个 id 在用户表里的昵称（查不到就是空串） */
  nickname: string
  /** 派生态：此刻有没有正用它连着的在线客户端（不落库，接口层实时聚合） */
  online: boolean
  /** 派生态：正用它连着的在线客户端快照（一个令牌可能同时挂着多条连接） */
  clients: OneBotClient[]
}

export interface IssuedToken {
  record: OneBotToken
  /** 明文令牌：**只在这一次响应里出现**，之后查不回来 */
  token: string
}

export interface KickResult {
  client_id: string
  id: string
  revoked: boolean
}

export interface RevokeResult {
  token_id: string
  removed: boolean
}

/** GET /onebot/clients：在线客户端；给 id 就只看那一个归属（谁的）下的。 */
export function fetchClients(id?: string) {
  const query = id ? `?id=${encodeURIComponent(id)}` : ''
  return http.get<OneBotClient[]>(`/onebot/clients${query}`)
}

/**
 * DELETE /onebot/clients/{client_id}：断开这条连接。
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

/** GET /onebot/tokens：令牌列表（不含明文，带归属昵称）。 */
export function fetchTokens() {
  return http.get<OneBotToken[]>('/onebot/tokens')
}

/**
 * POST /onebot/tokens：签发一个令牌，明文只在这一次返回。
 *
 * 归属（谁的）不用填——后端永远签给当前登录用户；account 是接入 WS 的机器人账号。
 */
export function issueToken(account = '', remark = '') {
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

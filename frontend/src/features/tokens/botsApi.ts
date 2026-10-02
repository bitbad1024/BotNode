/** 机器人接口：与后端 /api/bots/* 一一对应（P5 泛化：从 /api/onebot/* 迁来）。 */
import { http } from '../../lib/http'

/** 在线客户端列表里的一行（握手时由令牌定下归属）。 */
export interface BotClient {
  /** 这条连接自己的编号：踢下线时按它定位 */
  client_id: string
  /** 这条连接属于谁（握手时由令牌定下来） */
  id: string
  /** 归属那个 id 在用户表里的昵称（查不到就是空串） */
  nickname: string
  /** 接入 WS 的那个机器人账号 */
  account: string
  /** 机器人号；还没收到事件时是空串（多平台后统一字符串口径） */
  self_id: string
  /** 对端地址（Kook 正向 WS 没有，空串） */
  remote: string
  /** 连上的时刻（Unix 秒） */
  connected_at: number
}

/** 一条机器人记录（**不含明文**：后端只给记录，明文只在签发那一次返回）。 */
export interface Bot {
  /** 归属标识（谁的），也是记录主键 */
  id: string
  /** 底层适配器：onebot / kook（缺省 onebot） */
  platform: string
  /** 机器人账号（OneBot 是接入 WS 的机器人号，自由填，只用来展示） */
  account: string
  enabled: boolean
  remark: string
  created_at: number
  /** 归属那个 id 在用户表里的昵称（查不到就是空串） */
  nickname: string
  /** 派生态：此刻有没有正用它连着的在线客户端（不落库，接口层实时聚合） */
  online: boolean
  /** 派生态：正用它连着的在线客户端快照（一个机器人可能同时挂着多条连接） */
  clients: BotClient[]
}

export interface IssuedBot {
  record: Bot
  /** 明文令牌：**只在这一次响应里出现**，之后查不回来 */
  token: string
}

/**
 * GET /bots：机器人列表（不含明文，带归属昵称）。
 *
 * `ownerId` 只对管理员有意义（普通用户传了也只看得见自己的）：按归属筛时传它。
 */
export function fetchBots(ownerId = '') {
  return http.get<Bot[]>('/bots', { params: ownerId ? { owner_id: ownerId } : undefined })
}

/**
 * POST /bots：添加一个机器人（选底层适配器），明文只在这一次返回。
 *
 * 归属（谁的）不用填——后端永远签给当前登录用户；account 是机器人账号。
 * token 是 Kook 的 Bot Token（platform=kook 时必填；OneBot 自动签发、传空串即可）。
 */
export function addBot(
  platform = 'onebot',
  account = '',
  remark = '',
  token = '',
) {
  return http.post<IssuedBot, { platform: string; account: string; remark: string; token: string }>(
    '/bots',
    { platform, account, remark, token },
  )
}

/**
 * PATCH /bots/{id}：启用 / 停用。
 *
 * 停用 = 不许再连，并把正用它连着的客户端一并断开；记录还在，随时能启用回来。
 */
export function setBotEnabled(botId: string, enabled: boolean) {
  return http.patch<Bot, { enabled: boolean }>(
    `/bots/${encodeURIComponent(botId)}`,
    { enabled },
  )
}

/** DELETE /bots/{id}：删除机器人，并把正用它连着的客户端断开。 */
export function deleteBot(botId: string) {
  return http.del<{ removed: boolean }>(`/bots/${encodeURIComponent(botId)}`)
}

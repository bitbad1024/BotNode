/**
 * 把 OneBot 消息（段数组 / CQ 码字符串）解析成聊天预览的片段，
 * 并从收发帧里抽出「聊天记录」（用户消息靠左、框架 send_msg 动作靠右）。
 */
import type { Frame } from './debugStore'

export type Segment =
  | { type: 'text'; text: string }
  | { type: 'image'; url: string }
  | { type: 'at'; qq: string }
  | { type: 'face'; id: string }
  | { type: 'other'; label: string }

export interface ChatItem {
  key: number
  side: 'left' | 'right'
  name: string
  /** QQ 号：用来拼 qlogo 头像地址；空串走占位头像 */
  avatarQq: string
  /** 「群 xxx」/「私聊」这类范围标签；没有就 null */
  scope: string | null
  segments: Segment[]
  at: number
}

/** 框架要求客户端发消息的动作（渲染成机器人自己说话，靠右） */
const SEND_ACTIONS = new Set(['send_msg', 'send_private_msg', 'send_group_msg'])

/** QQ 头像（qlogo CDN），s=100 够头像框用了 */
export function qqAvatar(qq: string, s = 100): string {
  return `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(qq)}&s=${s}`
}

/** OneBot 文本反转义：&#91; [ &#93; ] &#44; , &amp; & */
function unescapeCq(text: string): string {
  return text
    .replace(/&#91;/g, '[')
    .replace(/&#93;/g, ']')
    .replace(/&#44;/g, ',')
    .replace(/&amp;/g, '&')
}

const CQ_RE = /\[CQ:([a-zA-Z0-9_]+)((?:,[^\],]+=[^\],]*)*)\]/g

function segFrom(type: string, data: Record<string, unknown>): Segment {
  if (type === 'text') return { type: 'text', text: String(data.text ?? '') }
  if (type === 'at') return { type: 'at', qq: String(data.qq ?? '') }
  if (type === 'face') return { type: 'face', id: String(data.id ?? '') }
  if (type === 'image') {
    const url = [data.url, data.file].find(
      (v) => typeof v === 'string' && /^https?:\/\//.test(v),
    )
    if (typeof url === 'string') return { type: 'image', url }
    return { type: 'other', label: `图片 ${String(data.file ?? '')}`.trim() }
  }
  return { type: 'other', label: type }
}

/** 消息段数组 → 片段 */
function segsFromArray(message: unknown[]): Segment[] {
  const segs = message
    .map((raw) => {
      const obj = (raw ?? {}) as Record<string, unknown>
      return segFrom(String(obj.type ?? 'other'), (obj.data ?? {}) as Record<string, unknown>)
    })
    .filter((s) => s.type !== 'text' || s.text !== '')
  return segs
}

/** CQ 码字符串 → 片段（文本与 [CQ:...] 混排） */
function segsFromString(message: string): Segment[] {
  const segs: Segment[] = []
  let last = 0
  for (const m of message.matchAll(CQ_RE)) {
    const idx = m.index ?? 0
    if (idx > last) segs.push({ type: 'text', text: unescapeCq(message.slice(last, idx)) })
    const params: Record<string, string> = {}
    for (const kv of (m[2] ?? '').split(',')) {
      const eq = kv.indexOf('=')
      if (eq > 0) params[kv.slice(0, eq)] = kv.slice(eq + 1)
    }
    segs.push(segFrom(m[1].toLowerCase(), params))
    last = idx + m[0].length
  }
  if (last < message.length) segs.push({ type: 'text', text: unescapeCq(message.slice(last)) })
  return segs.filter((s) => s.type !== 'text' || s.text !== '')
}

/** 统一入口：数组优先，其次 CQ 码字符串，都不行给占位 */
export function parseMessage(message: unknown): Segment[] {
  if (Array.isArray(message)) {
    const segs = segsFromArray(message)
    if (segs.length) return segs
  }
  if (typeof message === 'string' && message) {
    const segs = segsFromString(message)
    if (segs.length) return segs
  }
  return [{ type: 'other', label: '不支持的消息' }]
}

/** 从收发帧里抽出聊天记录：post_type=message 的都算用户消息（靠左） */
export function buildChatItems(frames: Frame[], selfQq: string): ChatItem[] {
  const items: ChatItem[] = []
  for (const f of frames) {
    const p = f.payload
    if (!p) continue
    if (p.post_type === 'message') {
      const sender = (p.sender ?? {}) as Record<string, unknown>
      const name = String(sender.card || sender.nickname || p.user_id || '用户')
      items.push({
        key: f.id,
        side: 'left',
        name,
        avatarQq: String(p.user_id ?? ''),
        scope:
          p.message_type === 'group' && p.group_id != null ? `群 ${p.group_id}` : null,
        segments: parseMessage(p.message ?? p.raw_message),
        at: f.at,
      })
    } else if (f.dir === 'in' && SEND_ACTIONS.has(String(p.action))) {
      const params = (p.params ?? {}) as Record<string, unknown>
      items.push({
        key: f.id,
        side: 'right',
        name: '机器人',
        avatarQq: selfQq,
        scope:
          p.action === 'send_group_msg' && params.group_id != null
            ? `群 ${params.group_id}`
            : p.action === 'send_private_msg'
              ? '私聊'
              : null,
        segments: parseMessage(params.message),
        at: f.at,
      })
    }
  }
  return items
}

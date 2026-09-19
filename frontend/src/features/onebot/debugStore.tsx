/**
 * WS 调试会话的常驻状态：挂在主框架（AppLayout）上而不是路由页里。
 * 切页面不卸载 —— 连接、令牌、草稿、收发记录都还在；退出登录才随主框架一起断开。
 */
import { createContext, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { WS_PRESETS } from './presets'

export type ConnStatus = 'idle' | 'connecting' | 'open' | 'closed' | 'error'

/** 一条收发记录：dir 定方向，kind 是给用户看的细分标签 */
export interface Frame {
  id: number
  dir: 'out' | 'in' | 'sys'
  kind: string
  text: string
  at: number
  /** in 且是动作时存原始报文，「回应 ok」要从里面抄 echo */
  payload?: Record<string, unknown>
}

interface DebugContextValue {
  url: string
  accessToken: string
  status: ConnStatus
  draft: string
  presetId: string
  autoReply: boolean
  sendError: string
  frames: Frame[]
  setUrl: (value: string) => void
  setAccessToken: (value: string) => void
  setAutoReply: (value: boolean) => void
  /** 手改草稿：自动切成“自定义”，并清掉上次的校验错误 */
  editDraft: (value: string) => void
  pickPreset: (id: string) => void
  connect: () => void
  disconnect: () => void
  send: () => void
  replyOk: (frame: Frame) => void
  clearFrames: () => void
}

const DebugContext = createContext<DebugContextValue | null>(null)

/** 帧数上限：防心跳刷爆内存，超了丢最老的 */
const MAX_FRAMES = 300

/** 收到的报文归个类：事件（带 post_type）/ 动作（带 action）/ 回应（带 status） */
function classify(payload: Record<string, unknown>): string {
  if (typeof payload.post_type === 'string') return '事件'
  if (typeof payload.action === 'string') return '动作'
  if ('status' in payload || 'retcode' in payload) return '回应'
  return '报文'
}

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

export function DebugProvider({ children }: { children: ReactNode }) {
  const [url, setUrl] = useState('ws://127.0.0.1:6700/')
  const [accessToken, setAccessToken] = useState('')
  const [status, setStatus] = useState<ConnStatus>('idle')
  const [draft, setDraft] = useState(() => JSON.stringify(WS_PRESETS[0].build(), null, 2))
  const [presetId, setPresetId] = useState(WS_PRESETS[0].id)
  const [autoReply, setAutoReply] = useState(true)
  const [sendError, setSendError] = useState('')
  const [frames, setFrames] = useState<Frame[]>([])

  const wsRef = useRef<WebSocket | null>(null)
  const seqRef = useRef(0)
  const autoReplyRef = useRef(autoReply)

  useEffect(() => {
    autoReplyRef.current = autoReply
  }, [autoReply])

  // 主框架卸载（退出登录）时断开；摘掉回调，避免卸载后再碰状态
  useEffect(
    () => () => {
      const ws = wsRef.current
      if (ws) {
        ws.onclose = null
        ws.onmessage = null
        ws.onerror = null
        ws.close()
        wsRef.current = null
      }
    },
    [],
  )

  function append(frame: Omit<Frame, 'id'>) {
    seqRef.current += 1
    setFrames((prev) => [...prev.slice(-(MAX_FRAMES - 1)), { id: seqRef.current, ...frame }])
  }

  function pickPreset(id: string) {
    setPresetId(id)
    if (id === 'custom') return // 自定义：不动已编辑的内容
    const found = WS_PRESETS.find((p) => p.id === id)
    if (found) {
      setDraft(JSON.stringify(found.build(), null, 2))
      setSendError('')
    }
  }

  function editDraft(value: string) {
    setDraft(value)
    setPresetId('custom')
    setSendError('')
  }

  function connect() {
    if (status === 'open' || status === 'connecting') return
    // 旧连接还挂着就先静默关掉（摘掉回调，避免它的 onclose 污染新一轮状态）
    const old = wsRef.current
    if (old) {
      old.onclose = null
      old.onmessage = null
      old.onerror = null
      old.close()
      wsRef.current = null
    }
    setFrames([])
    const trimmed = url.trim()
    const target =
      accessToken.trim() && !trimmed.includes('access_token=')
        ? `${trimmed}${trimmed.includes('?') ? '&' : '?'}access_token=${encodeURIComponent(accessToken.trim())}`
        : trimmed
    let ws: WebSocket
    try {
      ws = new WebSocket(target)
    } catch {
      setStatus('error')
      append({ dir: 'sys', kind: '错误', text: `地址不合法：${target}`, at: Date.now() })
      return
    }
    wsRef.current = ws
    setStatus('connecting')
    append({ dir: 'sys', kind: '信息', text: `正在连接 ${target}`, at: Date.now() })

    ws.onopen = () => {
      setStatus('open')
      append({ dir: 'sys', kind: '信息', text: '握手成功，可以发报文了', at: Date.now() })
    }
    ws.onmessage = (ev) => {
      const at = Date.now()
      let parsed: Record<string, unknown> | null = null
      try {
        const value: unknown = JSON.parse(String(ev.data))
        if (value !== null && typeof value === 'object') {
          parsed = value as Record<string, unknown>
        }
      } catch {
        /* 非 JSON：下面原样展示 */
      }
      if (parsed !== null) {
        append({ dir: 'in', kind: classify(parsed), text: pretty(parsed), at, payload: parsed })
        // 自动回应：框架的动作要应一声，业务里 conn.call 才等得到
        if (autoReplyRef.current && typeof parsed.action === 'string') {
          const reply: Record<string, unknown> = { status: 'ok', retcode: 0, data: null }
          if (typeof parsed.echo === 'string') reply.echo = parsed.echo
          const text = JSON.stringify(reply)
          ws.send(text)
          append({ dir: 'out', kind: '回应', text: pretty(reply), at: Date.now() })
        }
      } else {
        append({ dir: 'in', kind: '报文', text: String(ev.data), at })
      }
    }
    ws.onerror = () => {
      // 浏览器拿不到握手失败的 HTTP 状态码，onclose 紧随其后；这里只提示最常见的原因
      append({
        dir: 'sys',
        kind: '错误',
        text: '连接出错：服务没起？地址/路径对吗？令牌有效吗？',
        at: Date.now(),
      })
    }
    ws.onclose = (ev) => {
      if (wsRef.current === ws) {
        wsRef.current = null
        setStatus('closed')
      }
      append({
        dir: 'sys',
        kind: '信息',
        text: `连接已关闭（code=${ev.code}${ev.reason ? `，${ev.reason}` : ''}）`,
        at: Date.now(),
      })
    }
  }

  function disconnect() {
    wsRef.current?.close(1000, '调试页主动断开')
  }

  function send() {
    const text = draft.trim()
    if (!text) {
      setSendError('报文是空的：先选个模板，或写一段 JSON')
      return
    }
    if (status !== 'open') {
      setSendError('还没连上：先点「连接」')
      return
    }
    let value: unknown
    try {
      value = JSON.parse(text) // 先校验再发：发出去的必须是合法 JSON
    } catch (err) {
      setSendError(`JSON 解析失败：${err instanceof Error ? err.message : String(err)}`)
      return
    }
    setSendError('')
    wsRef.current?.send(text)
    append({
      dir: 'out',
      kind: classify(value as Record<string, unknown>),
      text: pretty(value),
      at: Date.now(),
    })
  }

  /** 手动回应一条动作：echo 照抄，业务里 conn.call 就能等到 */
  function replyOk(frame: Frame) {
    if (status !== 'open') return
    const reply: Record<string, unknown> = { status: 'ok', retcode: 0, data: null }
    const echo = frame.payload?.echo
    if (typeof echo === 'string') reply.echo = echo
    const text = JSON.stringify(reply)
    wsRef.current?.send(text)
    append({ dir: 'out', kind: '回应', text: pretty(reply), at: Date.now() })
  }

  function clearFrames() {
    setFrames([])
  }

  const value: DebugContextValue = {
    url,
    accessToken,
    status,
    draft,
    presetId,
    autoReply,
    sendError,
    frames,
    setUrl,
    setAccessToken,
    setAutoReply,
    editDraft,
    pickPreset,
    connect,
    disconnect,
    send,
    replyOk,
    clearFrames,
  }
  return <DebugContext.Provider value={value}>{children}</DebugContext.Provider>
}

export function useDebug(): DebugContextValue {
  const ctx = useContext(DebugContext)
  if (!ctx) throw new Error('useDebug 必须用在 DebugProvider 之内（主框架里）')
  return ctx
}

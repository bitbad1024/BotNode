/**
 * WS 调试页：模拟 OneBot 实现连框架的反向 WS。
 * 左边选模板 / 编辑报文发送，右边看框架下发的一切（动作 / 回应 / 系统消息）。
 * 直连 ws://<onebot host>:<port>，不走 Vite 代理（WebSocket 不受 CORS 限制）。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { WS_PRESETS } from './presets'
import { IconTrash } from '../../common/icons'
import styles from './DebugPage.module.css'

type ConnStatus = 'idle' | 'connecting' | 'open' | 'closed' | 'error'

/** 一条收发记录：dir 定方向，kind 是给用户看的细分标签 */
interface Frame {
  id: number
  dir: 'out' | 'in' | 'sys'
  kind: string
  text: string
  at: number
  /** in 且是动作时存原始报文，「回应 ok」要从里面抄 echo */
  payload?: Record<string, unknown>
}

const STATUS_TEXT: Record<ConnStatus, string> = {
  idle: '未连接',
  connecting: '连接中…',
  open: '已连接',
  closed: '已断开',
  error: '连接失败',
}

const STATUS_CLASS: Record<ConnStatus, string> = {
  idle: styles.dotIdle,
  connecting: styles.dotConnecting,
  open: styles.dotOpen,
  closed: styles.dotClosed,
  error: styles.dotError,
}

const DIR_TEXT = { out: '↑ 发送', in: '↓ 接收', sys: '◦ 系统' } as const
const DIR_CLASS = {
  out: styles.dirOut,
  in: styles.dirIn,
  sys: styles.dirSys,
} as const

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

function hhmmss(at: number): string {
  const d = new Date(at)
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':')
}

/** 帧数上限：防心跳刷爆内存，超了丢最老的 */
const MAX_FRAMES = 300

export default function DebugPage() {
  const [url, setUrl] = useState('ws://127.0.0.1:6700/')
  const [accessToken, setAccessToken] = useState('')
  const [status, setStatus] = useState<ConnStatus>('idle')
  const [presetId, setPresetId] = useState(WS_PRESETS[0].id)
  const [draft, setDraft] = useState(() => JSON.stringify(WS_PRESETS[0].build(), null, 2))
  const [sendError, setSendError] = useState('')
  const [autoReply, setAutoReply] = useState(true)
  const [frames, setFrames] = useState<Frame[]>([])

  const wsRef = useRef<WebSocket | null>(null)
  const seqRef = useRef(0)
  const autoReplyRef = useRef(autoReply)
  const listRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    autoReplyRef.current = autoReply
  }, [autoReply])

  // 卸载时断开，别让页面切走了连接还挂着
  useEffect(
    () => () => {
      const ws = wsRef.current
      if (ws) {
        ws.onclose = null
        ws.close()
      }
    },
    [],
  )

  // 新帧自动滚到底
  useEffect(() => {
    const el = listRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [frames])

  const preset = useMemo(() => WS_PRESETS.find((p) => p.id === presetId), [presetId])

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

  const open = status === 'open'

  return (
    <div className={`rise ${styles.page}`}>
      <header className={styles.head}>
        <h2 className={styles.title}>WS 调试</h2>
        <p className={styles.sub}>
          模拟 OneBot 实现连框架的反向 WS：左边选报文模板发事件，右边看框架下发的动作并回应。
          令牌在
          <Link to="/tokens" className={styles.link}>
            令牌管理
          </Link>
          签发（明文只显示一次）。
        </p>
      </header>

      {/* 连接条 */}
      <section className={`card ${styles.conn}`}>
        <label className={styles.fieldUrl}>
          <span className={styles.fieldLabel}>WS 地址</span>
          <input
            className={styles.input}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            spellCheck={false}
          />
        </label>
        <label className={styles.fieldToken}>
          <span className={styles.fieldLabel}>access_token</span>
          <input
            className={styles.input}
            value={accessToken}
            onChange={(e) => setAccessToken(e.target.value)}
            placeholder="必填：服务端配了令牌注册表"
            spellCheck={false}
          />
        </label>
        <div className={styles.connActions}>
          {open ? (
            <button type="button" className="btn" onClick={disconnect}>
              断开
            </button>
          ) : (
            <button
              type="button"
              className={`btn ${styles.primaryBtn}`}
              onClick={connect}
              disabled={status === 'connecting'}
            >
              连接
            </button>
          )}
          <span className={styles.status}>
            <i className={`${styles.dot} ${STATUS_CLASS[status]}`} />
            {STATUS_TEXT[status]}
          </span>
        </div>
      </section>

      <section className={styles.grid}>
        {/* 发送区 */}
        <div className={`card ${styles.pane}`}>
          <div className={styles.paneHead}>
            <h3 className={styles.paneTitle}>发送区</h3>
            <span className={styles.paneNote}>客户端 → 框架（事件 / 回应）</span>
          </div>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>消息模板</span>
            <select
              className={styles.input}
              value={presetId}
              onChange={(e) => pickPreset(e.target.value)}
            >
              {WS_PRESETS.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
              <option value="custom">自定义…</option>
            </select>
          </label>
          {preset && <p className={styles.hint}>{preset.hint}</p>}
          <textarea
            className={styles.textarea}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value)
              setPresetId('custom')
              setSendError('')
            }}
            rows={12}
            spellCheck={false}
          />
          {sendError && <p className={styles.sendError}>{sendError}</p>}
          <div className={styles.sendRow}>
            <button
              type="button"
              className={`btn ${styles.primaryBtn}`}
              onClick={send}
              disabled={!open}
            >
              发送
            </button>
            <span className={styles.sendHint}>{open ? '先做 JSON 校验再发' : '连接后才能发送'}</span>
          </div>
        </div>

        {/* 接收区 */}
        <div className={`card ${styles.pane}`}>
          <div className={styles.paneHead}>
            <h3 className={styles.paneTitle}>接收区</h3>
            <div className={styles.recvTools}>
              <label className={styles.check}>
                <input
                  type="checkbox"
                  checked={autoReply}
                  onChange={(e) => setAutoReply(e.target.checked)}
                />
                自动回应动作
              </label>
              <span className={styles.frameCount}>{frames.length} 条</span>
              <button
                type="button"
                className="icon-btn"
                onClick={clearFrames}
                title="清空接收区"
              >
                <IconTrash size={16} />
              </button>
            </div>
          </div>
          <div className={styles.frames} ref={listRef}>
            {frames.length === 0 && (
              <p className={styles.empty}>
                还没有报文。连接后发一条「私聊消息」—— 框架会回一个 send_msg 动作。
              </p>
            )}
            {frames.map((f) => (
              <div
                key={f.id}
                className={`${styles.frame} ${
                  f.dir === 'in'
                    ? styles.frameIn
                    : f.dir === 'out'
                      ? styles.frameOut
                      : styles.frameSys
                }`}
              >
                <div className={styles.frameMeta}>
                  <span className={`${styles.dirTag} ${DIR_CLASS[f.dir]}`}>{DIR_TEXT[f.dir]}</span>
                  <span className={styles.kindTag}>{f.kind}</span>
                  <span className={styles.frameTime}>{hhmmss(f.at)}</span>
                  {f.dir === 'in' && f.kind === '动作' && (
                    <button
                      type="button"
                      className={styles.replyBtn}
                      onClick={() => replyOk(f)}
                    >
                      回应 ok
                    </button>
                  )}
                </div>
                <pre className={styles.frameBody}>{f.text}</pre>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  )
}
